"""Interactive Qt 6 (PySide6) Fan Curve Editor and Live Telemetry History Chart."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from .config import (
    EMERGENCY_CPU_TEMP_C,
    HARD_MAX_CURVE_RPM,
    HARD_MIN_MANUAL_RPM,
    MAX_POINT_TEMP_C,
    MIN_POINT_TEMP_C,
    RPM_STEP,
    interpolate_curve_rpm,
    round_rpm,
)


class FanCurveWidget(QWidget):
    """Interactive HiDPI fan curve editor with live sensor & RPM tracking."""

    curveChanged = Signal(list)

    TEMP_MIN = 30.0
    TEMP_MAX = 100.0
    RPM_MIN = 0.0
    RPM_MAX = 5200.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(520, 320)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)

        self._points: list[list[int]] = [
            [50, 2400],
            [60, 3000],
            [70, 3700],
            [80, 4400],
            [88, 4800],
        ]
        self._auto_below_first = True
        self._enabled = True
        self._hysteresis_c = 3.0

        # Live telemetry overlay
        self._live_temp_c: float | None = None
        self._live_fan1_rpm: int | None = None
        self._live_fan2_rpm: int | None = None
        self._live_target_rpm: int = 0
        self._live_mode: str = "curve"

        # Interaction state
        self._drag_index: int | None = None
        self._hover_index: int | None = None

        self._margin_left = 62
        self._margin_right = 24
        self._margin_top = 24
        self._margin_bottom = 42

    def set_curve(
        self,
        points: list[list[int]],
        auto_below_first: bool = True,
        hysteresis_c: float = 3.0,
        enabled: bool = True,
    ) -> None:
        if self._drag_index is not None:
            return
        self._points = [list(p) for p in points]
        self._auto_below_first = auto_below_first
        self._hysteresis_c = hysteresis_c
        self._enabled = enabled
        self.update()

    def set_live_state(
        self,
        eff_temp_c: float | None,
        fan1_rpm: int | None,
        fan2_rpm: int | None,
        target_rpm: int,
        mode: str,
    ) -> None:
        self._live_temp_c = eff_temp_c
        self._live_fan1_rpm = fan1_rpm
        self._live_fan2_rpm = fan2_rpm
        self._live_target_rpm = target_rpm
        self._live_mode = mode
        self.update()

    def points(self) -> list[list[int]]:
        return [list(p) for p in self._points]

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self._margin_left,
            self._margin_top,
            max(10.0, self.width() - self._margin_left - self._margin_right),
            max(10.0, self.height() - self._margin_top - self._margin_bottom),
        )

    def _to_screen(self, temp_c: float, rpm: float) -> QPointF:
        rect = self._plot_rect()
        nx = (temp_c - self.TEMP_MIN) / (self.TEMP_MAX - self.TEMP_MIN)
        ny = (rpm - self.RPM_MIN) / (self.RPM_MAX - self.RPM_MIN)
        x = rect.left() + nx * rect.width()
        y = rect.bottom() - ny * rect.height()
        return QPointF(x, y)

    def _from_screen(self, pos: QPointF) -> tuple[float, float]:
        rect = self._plot_rect()
        nx = (pos.x() - rect.left()) / rect.width()
        ny = (rect.bottom() - pos.y()) / rect.height()
        temp_c = self.TEMP_MIN + nx * (self.TEMP_MAX - self.TEMP_MIN)
        rpm = self.RPM_MIN + ny * (self.RPM_MAX - self.RPM_MIN)
        return temp_c, rpm

    def _hit_test(self, pos: QPointF, radius: float = 14.0) -> int | None:
        best_idx: int | None = None
        best_dist = radius * radius
        for idx, (t, r) in enumerate(self._points):
            pt = self._to_screen(t, r)
            d2 = (pt.x() - pos.x()) ** 2 + (pt.y() - pos.y()) ** 2
            if d2 <= best_dist:
                best_dist = d2
                best_idx = idx
        return best_idx

    def mousePressEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        idx = self._hit_test(pos)
        if event.button() == Qt.LeftButton:
            if idx is not None:
                self._drag_index = idx
                self.setCursor(Qt.ClosedHandCursor)
                self.update()
        elif event.button() == Qt.RightButton:
            if idx is not None and len(self._points) > 2:
                self._points.pop(idx)
                self._hover_index = None
                self.curveChanged.emit(self.points())
                self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.LeftButton or len(self._points) >= 8:
            return
        pos = event.position()
        if not self._plot_rect().contains(pos):
            return
        if self._hit_test(pos, radius=18.0) is not None:
            return

        raw_t, raw_r = self._from_screen(pos)
        t = int(round(max(MIN_POINT_TEMP_C, min(MAX_POINT_TEMP_C, raw_t))))
        # Avoid colliding within 2°C of an existing point
        if any(abs(pt[0] - t) < 2 for pt in self._points):
            return
        r = max(HARD_MIN_MANUAL_RPM, min(HARD_MAX_CURVE_RPM, round_rpm(raw_r)))
        self._points.append([t, r])
        self._points.sort(key=lambda p: p[0])
        self._enforce_monotonic()
        self.curveChanged.emit(self.points())
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        if self._drag_index is not None:
            idx = self._drag_index
            raw_t, raw_r = self._from_screen(pos)

            # Constrain X between neighbor points (at least 2°C gap)
            min_t = MIN_POINT_TEMP_C if idx == 0 else self._points[idx - 1][0] + 2
            max_t = (
                MAX_POINT_TEMP_C
                if idx == len(self._points) - 1
                else self._points[idx + 1][0] - 2
            )
            t = int(round(max(min_t, min(max_t, raw_t))))

            # Constrain Y monotonically between neighbor points within safe RPM bounds
            min_r = (
                HARD_MIN_MANUAL_RPM if idx == 0 else self._points[idx - 1][1]
            )
            max_r = (
                HARD_MAX_CURVE_RPM
                if idx == len(self._points) - 1
                else self._points[idx + 1][1]
            )
            r = round_rpm(max(min_r, min(max_r, raw_r)))
            r = max(HARD_MIN_MANUAL_RPM, min(HARD_MAX_CURVE_RPM, r))

            if self._points[idx] != [t, r]:
                self._points[idx] = [t, r]
                self.update()
            return

        hit = self._hit_test(pos)
        if hit != self._hover_index:
            self._hover_index = hit
            self.setCursor(Qt.OpenHandCursor if hit is not None else Qt.ArrowCursor)
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self._drag_index is not None:
            self._drag_index = None
            self.setCursor(Qt.ArrowCursor)
            self.curveChanged.emit(self.points())
            self.update()

    def _enforce_monotonic(self) -> None:
        running = HARD_MIN_MANUAL_RPM
        for pt in self._points:
            pt[1] = max(running, min(HARD_MAX_CURVE_RPM, round_rpm(pt[1])))
            running = pt[1]

    def paintEvent(self, _event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        rect = self._plot_rect()

        # Card background
        p.fillRect(self.rect(), QColor("#141821"))
        p.setPen(QPen(QColor("#262f40"), 1))
        p.setBrush(QColor("#1a202c"))
        p.drawRoundedRect(rect, 8, 8)

        # 1. Shaded safety zones inside plot
        p.save()
        clip_path = QPainterPath()
        clip_path.addRoundedRect(rect, 8, 8)
        p.setClipPath(clip_path)

        # Dead-zone guard (< 2000 RPM)
        y_2000 = self._to_screen(self.TEMP_MIN, HARD_MIN_MANUAL_RPM).y()
        dead_rect = QRectF(rect.left(), y_2000, rect.width(), rect.bottom() - y_2000)
        p.fillRect(dead_rect, QColor(120, 130, 150, 22))

        # BIOS Auto cool zone (left of first curve point)
        if self._auto_below_first and self._points:
            x_first = self._to_screen(self._points[0][0], 0).x()
            auto_rect = QRectF(rect.left(), rect.top(), max(0.0, x_first - rect.left()), rect.height())
            p.fillRect(auto_rect, QColor(56, 189, 248, 20))

        # Emergency failsafe zone (>= 92°C)
        x_emerg = self._to_screen(EMERGENCY_CPU_TEMP_C, 0).x()
        emerg_rect = QRectF(x_emerg, rect.top(), max(0.0, rect.right() - x_emerg), rect.height())
        p.fillRect(emerg_rect, QColor(239, 68, 68, 32))

        # Gridlines
        grid_pen = QPen(QColor("#273144"), 1, Qt.DashLine)
        p.setPen(grid_pen)
        for t in range(40, 100, 10):
            x = self._to_screen(t, 0).x()
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        for r in range(1000, 5001, 1000):
            y = self._to_screen(self.TEMP_MIN, r).y()
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))

        # Safe min floor line (2000 RPM) & emergency line (92°C)
        p.setPen(QPen(QColor(148, 163, 184, 90), 1, Qt.DotLine))
        p.drawLine(QPointF(rect.left(), y_2000), QPointF(rect.right(), y_2000))
        p.setPen(QPen(QColor(239, 68, 68, 140), 1.2, Qt.DashLine))
        p.drawLine(QPointF(x_emerg, rect.top()), QPointF(x_emerg, rect.bottom()))

        # Zone labels
        small_font = QFont(self.font())
        small_font.setPointSize(8)
        p.setFont(small_font)
        p.setPen(QColor(148, 163, 184, 150))
        p.drawText(QPointF(rect.left() + 8, rect.bottom() - 8), "BIOS Auto / Anti-Stall Guard (< 2000 RPM)")
        if self._auto_below_first and self._points and (x_first - rect.left()) > 75:
            p.setPen(QColor(56, 189, 248, 175))
            p.drawText(QPointF(rect.left() + 8, rect.top() + 18), "BIOS Auto")
        p.setPen(QColor(248, 113, 113, 200))
        p.drawText(QPointF(x_emerg + 4, rect.top() + 18), "92°C")
        p.drawText(QPointF(x_emerg + 4, rect.top() + 31), "5000")

        # 2. Curve path + gradient fill
        if self._points:
            pts_sorted = sorted(self._points, key=lambda pt: pt[0])
            line_color = QColor("#38bdf8") if self._enabled else QColor("#64748b")

            curve_path = QPainterPath()
            fill_path = QPainterPath()

            first_pt = self._to_screen(pts_sorted[0][0], pts_sorted[0][1])
            if not self._auto_below_first:
                start_left = self._to_screen(self.TEMP_MIN, pts_sorted[0][1])
                curve_path.moveTo(start_left)
                curve_path.lineTo(first_pt)
                fill_path.moveTo(QPointF(start_left.x(), rect.bottom()))
                fill_path.lineTo(start_left)
                fill_path.lineTo(first_pt)
            else:
                # Dashed lead-in at 0 (BIOS Auto) up to first point
                auto_start = self._to_screen(self.TEMP_MIN, 0)
                auto_knee = self._to_screen(pts_sorted[0][0], 0)
                p.setPen(QPen(QColor(56, 189, 248, 120), 2, Qt.DashLine))
                p.drawLine(auto_start, auto_knee)
                p.drawLine(auto_knee, first_pt)

                curve_path.moveTo(first_pt)
                fill_path.moveTo(QPointF(first_pt.x(), rect.bottom()))
                fill_path.lineTo(first_pt)

            for t, r in pts_sorted[1:]:
                sp = self._to_screen(t, r)
                curve_path.lineTo(sp)
                fill_path.lineTo(sp)

            # Extend flat to emergency threshold, then step up to 5000 RPM
            last_t, last_r = pts_sorted[-1]
            if last_t < EMERGENCY_CPU_TEMP_C:
                pre_emerg = self._to_screen(EMERGENCY_CPU_TEMP_C, last_r)
                curve_path.lineTo(pre_emerg)
                fill_path.lineTo(pre_emerg)
            emerg_top = self._to_screen(EMERGENCY_CPU_TEMP_C, 5000)
            right_top = self._to_screen(self.TEMP_MAX, 5000)
            fill_path.lineTo(QPointF(fill_path.currentPosition().x(), rect.bottom()))
            fill_path.closeSubpath()

            grad = QLinearGradient(0, rect.top(), 0, rect.bottom())
            if self._enabled:
                grad.setColorAt(0.0, QColor(56, 189, 248, 90))
                grad.setColorAt(1.0, QColor(56, 189, 248, 8))
            else:
                grad.setColorAt(0.0, QColor(100, 116, 139, 55))
                grad.setColorAt(1.0, QColor(100, 116, 139, 5))
            p.fillPath(fill_path, QBrush(grad))

            # Draw active curve stroke
            p.setPen(QPen(line_color, 2.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(curve_path)

            # Draw emergency red segment
            p.setPen(QPen(QColor("#f87171"), 2.4, Qt.DashLine))
            p.drawLine(self._to_screen(EMERGENCY_CPU_TEMP_C, last_r), emerg_top)
            p.drawLine(emerg_top, right_top)

            # 3. Live operating point overlay
            if self._live_temp_c is not None:
                clamped_t = max(self.TEMP_MIN, min(self.TEMP_MAX, self._live_temp_c))
                lx = self._to_screen(clamped_t, 0).x()
                p.setPen(QPen(QColor(251, 191, 36, 150), 1.5, Qt.DashLine))
                p.drawLine(QPointF(lx, rect.top()), QPointF(lx, rect.bottom()))

                # Actual Fan RPM marker (amber diamond/dot)
                rpms = [r for r in (self._live_fan1_rpm, self._live_fan2_rpm) if r is not None]
                if rpms:
                    avg_rpm = sum(rpms) / len(rpms)
                    live_pt = self._to_screen(clamped_t, avg_rpm)
                    p.setPen(QPen(QColor("#0f172a"), 2))
                    p.setBrush(QColor("#fbbf24"))
                    p.drawEllipse(live_pt, 6.5, 6.5)

                    # Badge showing live temp & actual RPM
                    badge = f"{self._live_temp_c:.1f}°C · {int(round(avg_rpm))} RPM"
                    fm = p.fontMetrics()
                    bw = fm.horizontalAdvance(badge) + 14
                    bx = min(max(rect.left() + 6, live_pt.x() - bw / 2), rect.right() - bw - 6)
                    by = max(rect.top() + 6, live_pt.y() - 30)
                    p.setPen(QPen(QColor("#fbbf24"), 1))
                    p.setBrush(QColor(15, 23, 42, 225))
                    p.drawRoundedRect(QRectF(bx, by, bw, 20), 5, 5)
                    p.setPen(QColor("#fde68a"))
                    p.drawText(QRectF(bx, by, bw, 20), Qt.AlignCenter, badge)

            # 4. Draggable control handles
            for idx, (t, r) in enumerate(self._points):
                sp = self._to_screen(t, r)
                is_active = idx == self._drag_index or idx == self._hover_index
                radius = 8.5 if is_active else 6.5

                if is_active:
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(56, 189, 248, 70))
                    p.drawEllipse(sp, 14, 14)

                p.setPen(QPen(QColor("#0f172a"), 2))
                p.setBrush(QColor("#e0f2fe") if is_active else line_color)
                p.drawEllipse(sp, radius, radius)

                if is_active:
                    tip = f"{t}°C → {r} RPM"
                    fm = p.fontMetrics()
                    tw = fm.horizontalAdvance(tip) + 14
                    tx = min(max(rect.left() + 4, sp.x() - tw / 2), rect.right() - tw - 4)
                    ty = sp.y() + 14 if sp.y() < rect.top() + 45 else sp.y() - 32
                    p.setPen(QPen(QColor("#38bdf8"), 1))
                    p.setBrush(QColor(15, 23, 42, 235))
                    p.drawRoundedRect(QRectF(tx, ty, tw, 22), 5, 5)
                    p.setPen(QColor("#f8fafc"))
                    p.drawText(QRectF(tx, ty, tw, 22), Qt.AlignCenter, tip)

        p.restore()

        # Axis ticks & labels
        axis_font = QFont(self.font())
        axis_font.setPointSize(8)
        p.setFont(axis_font)
        p.setPen(QColor("#94a3b8"))

        for t in range(30, 101, 10):
            x = self._to_screen(t, 0).x()
            p.drawText(QRectF(x - 20, rect.bottom() + 6, 40, 18), Qt.AlignCenter, f"{t}°C")

        for r in range(0, 5001, 1000):
            y = self._to_screen(self.TEMP_MIN, r).y()
            label = "Auto" if r == 0 else f"{r}"
            p.drawText(
                QRectF(4, y - 9, self._margin_left - 12, 18),
                Qt.AlignRight | Qt.AlignVCenter,
                label,
            )

        # Axis titles
        p.setPen(QColor("#64748b"))
        p.drawText(
            QRectF(rect.left(), self.height() - 18, rect.width(), 16),
            Qt.AlignCenter,
            "Driving Temperature (max CPU / Active GPU) — Drag dots to edit · Double-click to add · Right-click to remove",
        )


class HistoryChartWidget(QWidget):
    """Compact scrolling history graph of CPU/GPU temps and actual Fan RPM."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(115)
        self.setMaximumHeight(145)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._history: list[dict[str, Any]] = []

    def set_history(self, history: list[dict[str, Any]]) -> None:
        self._history = history[-90:]
        self.update()

    def paintEvent(self, _event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        p.fillRect(self.rect(), QColor("#141821"))
        rect = QRectF(42, 22, max(10.0, self.width() - 88), max(10.0, self.height() - 32))
        p.setPen(QPen(QColor("#262f40"), 1))
        p.setBrush(QColor("#1a202c"))
        p.drawRoundedRect(rect, 6, 6)

        font = QFont(self.font())
        font.setPointSize(8)
        p.setFont(font)

        # Header legend
        p.setPen(QColor("#fb923c"))
        p.drawText(QPointF(rect.left(), 14), "■ CPU °C")
        p.setPen(QColor("#c084fc"))
        p.drawText(QPointF(rect.left() + 68, 14), "■ GPU °C")
        p.setPen(QColor("#38bdf8"))
        p.drawText(QPointF(rect.left() + 136, 14), "■ Fan RPM")
        p.setPen(QColor("#64748b"))
        p.drawText(QRectF(rect.left(), 2, rect.width(), 16), Qt.AlignRight, "Last ~2 minutes")

        # Left (temp) and right (RPM) labels
        p.setPen(QColor("#94a3b8"))
        for temp in (30, 65, 100):
            ny = (temp - 30.0) / 70.0
            y = rect.bottom() - ny * rect.height()
            p.drawText(QRectF(2, y - 8, 34, 16), Qt.AlignRight | Qt.AlignVCenter, f"{temp}°")
        for rpm in (0, 2500, 5000):
            ny = rpm / 5000.0
            y = rect.bottom() - ny * rect.height()
            p.drawText(QRectF(rect.right() + 6, y - 8, 40, 16), Qt.AlignLeft | Qt.AlignVCenter, f"{rpm}")

        if len(self._history) < 2:
            p.setPen(QColor("#64748b"))
            p.drawText(rect, Qt.AlignCenter, "Collecting live sensor history…")
            return

        n = len(self._history)

        def draw_series(values: list[float | None], vmin: float, vmax: float, color: str, width: float = 1.8) -> None:
            path = QPainterPath()
            started = False
            for i, val in enumerate(values):
                if val is None:
                    started = False
                    continue
                nx = i / max(1, n - 1)
                ny = max(0.0, min(1.0, (val - vmin) / (vmax - vmin)))
                pt = QPointF(rect.left() + nx * rect.width(), rect.bottom() - ny * rect.height())
                if not started:
                    path.moveTo(pt)
                    started = True
                else:
                    path.lineTo(pt)
            p.setPen(QPen(QColor(color), width))
            p.drawPath(path)

        cpus = [item.get("cpu") for item in self._history]
        gpus = [item.get("gpu") for item in self._history]
        rpms = [((item.get("f1", 0) + item.get("f2", 0)) / 2.0) for item in self._history]

        draw_series(rpms, 0.0, 5000.0, "#38bdf8", 1.8)
        draw_series(gpus, 30.0, 100.0, "#c084fc", 1.6)
        draw_series(cpus, 30.0, 100.0, "#fb923c", 2.0)

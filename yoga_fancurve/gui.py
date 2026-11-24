"""Native Qt 6 (PySide6) Desktop GUI & System Tray for Yoga Pro 9 16IMH9 Fan Control."""

from __future__ import annotations

import argparse
import copy
import sys
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .config import PRESETS, SOCKET_PATH, default_config, validate_and_sanitize_config
from .curve_widget import FanCurveWidget, HistoryChartWidget
from .daemon import send_ipc_command
from .hardware import YogaHardware

DARK_STYLESHEET = """
QMainWindow, QWidget {
    background-color: #141821;
    color: #e2e8f0;
    font-family: "Noto Sans", "Inter", sans-serif;
    font-size: 13px;
}
QFrame#Card {
    background-color: #1a202c;
    border: 1px solid #262f40;
    border-radius: 10px;
}
QLabel#CardTitle {
    color: #94a3b8;
    font-size: 11px;
    font-weight: 600;
}
QLabel#CardValue {
    color: #f8fafc;
    font-size: 20px;
    font-weight: 700;
}
QLabel#CardSub {
    color: #64748b;
    font-size: 11px;
}
QPushButton {
    background-color: #1e293b;
    color: #e2e8f0;
    border: 1px solid #334155;
    border-radius: 7px;
    padding: 7px 14px;
    font-weight: 600;
}
QPushButton:hover {
    background-color: #273449;
    border-color: #38bdf8;
}
QPushButton:pressed {
    background-color: #0f172a;
}
QPushButton#PrimaryBtn {
    background-color: #0284c7;
    border-color: #38bdf8;
    color: #ffffff;
}
QPushButton#PrimaryBtn:hover {
    background-color: #0369a1;
}
QPushButton#DangerBtn {
    background-color: #3f1d24;
    border-color: #ef4444;
    color: #fecaca;
}
QPushButton#DangerBtn:hover {
    background-color: #5c1f29;
}
QComboBox, QDoubleSpinBox {
    background-color: #1e293b;
    color: #f8fafc;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 5px 10px;
}
QCheckBox {
    spacing: 8px;
    font-weight: 500;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 1px solid #475569;
    background-color: #1e293b;
}
QCheckBox::indicator:checked {
    background-color: #0284c7;
    border-color: #38bdf8;
}
"""


def _make_fan_icon(active: bool = True) -> QIcon:
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    ring_color = QColor("#38bdf8") if active else QColor("#94a3b8")
    p.setPen(QPen(ring_color, 4))
    p.setBrush(QColor("#141821"))
    p.drawEllipse(6, 6, 52, 52)

    p.translate(32, 32)
    p.setPen(Qt.NoPen)
    p.setBrush(ring_color)
    for angle in (0, 90, 180, 270):
        p.save()
        p.rotate(angle)
        p.drawRoundedRect(-5, -20, 10, 16, 4, 4)
        p.restore()
    p.setBrush(QColor("#f8fafc"))
    p.drawEllipse(-5, -5, 10, 10)
    p.end()
    return QIcon(pix)


def _make_metric_card(title: str, initial: str = "—", sub: str = "") -> tuple[QFrame, QLabel, QLabel]:
    card = QFrame()
    card.setObjectName("Card")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(12, 10, 12, 10)
    lay.setSpacing(2)

    lbl_t = QLabel(title)
    lbl_t.setObjectName("CardTitle")
    lbl_v = QLabel(initial)
    lbl_v.setObjectName("CardValue")
    lbl_s = QLabel(sub)
    lbl_s.setObjectName("CardSub")

    lay.addWidget(lbl_t)
    lay.addWidget(lbl_v)
    lay.addWidget(lbl_s)
    return card, lbl_v, lbl_s


class YogaFanWindow(QMainWindow):
    def __init__(self, start_minimized: bool = False) -> None:
        super().__init__()
        self.setWindowTitle("Yoga Pro 9 16IMH9 · Fan Curve Control")
        self.resize(900, 740)
        self.setMinimumSize(780, 640)
        self.setStyleSheet(DARK_STYLESHEET)

        self._icon_active = _make_fan_icon(True)
        self._icon_auto = _make_fan_icon(False)
        self.setWindowIcon(self._icon_active)

        self._hw_fallback = YogaHardware()
        self._cfg: dict[str, Any] = default_config()
        self._daemon_connected = False
        self._updating_ui = False
        self._dirty_curve = False
        self._allow_quit = False

        self._build_ui()
        self._build_tray()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(1500)
        self._poll_timer.timeout.connect(self._poll_state)
        self._poll_state()
        self._poll_timer.start()

        if start_minimized and QSystemTrayIcon.isSystemTrayAvailable():
            self.hide()
        else:
            self.show()

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        main = QVBoxLayout(root)
        main.setContentsMargins(16, 14, 16, 14)
        main.setSpacing(12)

        # Header row
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("Lenovo Yoga Pro 9 16IMH9 · Safe Fan Controller")
        f = QFont()
        f.setPointSize(14)
        f.setBold(True)
        title.setFont(f)
        self._subtitle = QLabel("Upstream lenovo_wmi_other · Firmware EC Mutex Protected · 2000–4800 RPM Safe Range")
        self._subtitle.setStyleSheet("color: #94a3b8; font-size: 11px;")
        title_box.addWidget(title)
        title_box.addWidget(self._subtitle)
        header.addLayout(title_box, 1)

        self._badge_mode = QLabel("CONNECTING…")
        self._badge_mode.setStyleSheet(
            "background-color: #1e293b; color: #94a3b8; border: 1px solid #334155;"
            "border-radius: 12px; padding: 5px 12px; font-weight: 700; font-size: 11px;"
        )
        header.addWidget(self._badge_mode)
        main.addLayout(header)

        # Live telemetry cards
        cards = QGridLayout()
        cards.setSpacing(10)

        c_cpu, self._val_cpu, self._sub_cpu = _make_metric_card("CPU PACKAGE", "— °C", "Smoothed: — °C")
        c_gpu, self._val_gpu, self._sub_gpu = _make_metric_card("RTX 4050 GPU", "— °C", "Checking PM…")
        c_f1, self._val_f1, self._sub_f1 = _make_metric_card("FAN 1", "— RPM", "Target: Auto")
        c_f2, self._val_f2, self._sub_f2 = _make_metric_card("FAN 2", "— RPM", "Target: Auto")
        c_pwr, self._val_pwr, self._sub_pwr = _make_metric_card("POWER", "—", "Profile: —")

        cards.addWidget(c_cpu, 0, 0)
        cards.addWidget(c_gpu, 0, 1)
        cards.addWidget(c_f1, 0, 2)
        cards.addWidget(c_f2, 0, 3)
        cards.addWidget(c_pwr, 0, 4)
        main.addLayout(cards)

        # Control bar card
        ctrl_card = QFrame()
        ctrl_card.setObjectName("Card")
        ctrl_lay = QVBoxLayout(ctrl_card)
        ctrl_lay.setContentsMargins(14, 12, 14, 12)
        ctrl_lay.setSpacing(10)

        row1 = QHBoxLayout()
        self._chk_enabled = QCheckBox("Enable Dynamic Fan Curve")
        self._chk_enabled.toggled.connect(self._on_toggle_changed)

        self._chk_ac_only = QCheckBox("Only Active When Plugged In (AC Only)")
        self._chk_ac_only.toggled.connect(self._on_toggle_changed)

        self._chk_gpu = QCheckBox("Track Active GPU Temp")
        self._chk_gpu.toggled.connect(self._on_toggle_changed)

        row1.addWidget(self._chk_enabled)
        row1.addSpacing(12)
        row1.addWidget(self._chk_ac_only)
        row1.addSpacing(12)
        row1.addWidget(self._chk_gpu)
        row1.addStretch(1)
        ctrl_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Preset:"))
        self._cmb_preset = QComboBox()
        for name in list(PRESETS.keys()) + ["Custom"]:
            self._cmb_preset.addItem(name)
        self._cmb_preset.currentTextChanged.connect(self._on_preset_selected)
        row2.addWidget(self._cmb_preset)

        row2.addSpacing(14)
        row2.addWidget(QLabel("Hysteresis:"))
        self._spn_hyst = QDoubleSpinBox()
        self._spn_hyst.setRange(1.0, 8.0)
        self._spn_hyst.setSingleStep(0.5)
        self._spn_hyst.setSuffix(" °C")
        self._spn_hyst.valueChanged.connect(self._on_toggle_changed)
        row2.addWidget(self._spn_hyst)

        row2.addSpacing(14)
        self._chk_auto_below = QCheckBox("BIOS Auto below 1st point")
        self._chk_auto_below.toggled.connect(self._on_toggle_changed)
        row2.addWidget(self._chk_auto_below)

        row2.addStretch(1)

        self._btn_apply = QPushButton("Apply & Save Curve")
        self._btn_apply.setObjectName("PrimaryBtn")
        self._btn_apply.clicked.connect(self._apply_config)
        row2.addWidget(self._btn_apply)

        self._btn_auto = QPushButton("BIOS Auto Now")
        self._btn_auto.setObjectName("DangerBtn")
        self._btn_auto.clicked.connect(self._restore_bios_auto)
        row2.addWidget(self._btn_auto)

        ctrl_lay.addLayout(row2)
        main.addWidget(ctrl_card)

        # Interactive fan curve canvas
        self._curve_widget = FanCurveWidget()
        self._curve_widget.curveChanged.connect(self._on_curve_edited)
        main.addWidget(self._curve_widget, 1)

        # Live history sparkline
        self._history_widget = HistoryChartWidget()
        main.addWidget(self._history_widget)

        # Bottom status line
        self._lbl_footer = QLabel(
            "Safety rails active: 2000–4800 RPM curve bounds · 92°C CPU / 85°C GPU emergency 5000 RPM · Auto-reverts to BIOS on sleep/shutdown."
        )
        self._lbl_footer.setStyleSheet("color: #64748b; font-size: 11px;")
        main.addWidget(self._lbl_footer)

    def _build_tray(self) -> None:
        self._tray: QSystemTrayIcon | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(self._icon_active, self)
        menu = QMenu()

        act_show = QAction("Open Fan Curve GUI", self)
        act_show.triggered.connect(self._show_and_raise)
        menu.addAction(act_show)
        menu.addSeparator()

        self._act_tray_enable = QAction("Enable Custom Curve", self)
        self._act_tray_enable.setCheckable(True)
        self._act_tray_enable.triggered.connect(self._tray_toggle_enable)
        menu.addAction(self._act_tray_enable)

        self._act_tray_ac = QAction("Plugged-In Only (AC Only)", self)
        self._act_tray_ac.setCheckable(True)
        self._act_tray_ac.triggered.connect(self._tray_toggle_ac)
        menu.addAction(self._act_tray_ac)

        menu.addSeparator()
        for preset_name in PRESETS:
            act = QAction(f"Preset: {preset_name}", self)
            act.triggered.connect(lambda _chk=False, n=preset_name: self._send_preset(n))
            menu.addAction(act)

        menu.addSeparator()
        act_quit = QAction("Quit GUI (Daemon Keeps Running)", self)
        act_quit.triggered.connect(self._quit_app)
        menu.addAction(act_quit)

        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._tray = tray

    def _show_and_raise(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.isVisible():
                self.hide()
            else:
                self._show_and_raise()

    def _quit_app(self) -> None:
        self._allow_quit = True
        if self._tray:
            self._tray.hide()
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._allow_quit and self._tray is not None and self._tray.isVisible():
            self.hide()
            event.ignore()
            return
        super().closeEvent(event)

    def _tray_toggle_enable(self, checked: bool) -> None:
        self._chk_enabled.setChecked(checked)
        self._apply_config()

    def _tray_toggle_ac(self, checked: bool) -> None:
        self._chk_ac_only.setChecked(checked)
        self._apply_config()

    def _send_preset(self, preset_name: str) -> None:
        if not self._daemon_connected:
            return
        try:
            resp = send_ipc_command({"cmd": "set_preset", "preset": preset_name}, SOCKET_PATH)
            if resp.get("ok"):
                self._cfg = resp["config"]
                self._dirty_curve = False
                self._sync_controls_from_cfg()
                self._poll_state()
        except OSError as exc:
            self._lbl_footer.setText(f"Could not contact daemon: {exc}")

    def _on_preset_selected(self, text: str) -> None:
        if self._updating_ui or text == "Custom" or text not in PRESETS:
            return
        p = PRESETS[text]
        self._updating_ui = True
        self._cfg["preset"] = text
        if text == "BIOS Auto":
            self._cfg["enabled"] = False
            self._chk_enabled.setChecked(False)
        else:
            self._cfg["enabled"] = True
            self._cfg["points"] = copy.deepcopy(p["points"])
            self._cfg["hysteresis_c"] = p["hysteresis_c"]
            self._cfg["auto_below_first_point"] = p["auto_below_first_point"]
            self._chk_enabled.setChecked(True)
            self._spn_hyst.setValue(p["hysteresis_c"])
            self._chk_auto_below.setChecked(p["auto_below_first_point"])
            self._curve_widget.set_curve(
                self._cfg["points"],
                auto_below_first=self._cfg["auto_below_first_point"],
                hysteresis_c=self._cfg["hysteresis_c"],
                enabled=self._cfg["enabled"],
            )
        self._updating_ui = False
        self._dirty_curve = False
        self._apply_config()

    def _on_toggle_changed(self) -> None:
        if self._updating_ui:
            return
        self._cfg["enabled"] = self._chk_enabled.isChecked()
        self._cfg["ac_only"] = self._chk_ac_only.isChecked()
        self._cfg["include_gpu_temp"] = self._chk_gpu.isChecked()
        self._cfg["hysteresis_c"] = float(self._spn_hyst.value())
        self._cfg["auto_below_first_point"] = self._chk_auto_below.isChecked()
        if self._cfg["enabled"] and self._cfg["preset"] == "BIOS Auto":
            self._cfg["preset"] = "Custom"
            self._updating_ui = True
            self._cmb_preset.setCurrentText("Custom")
            self._updating_ui = False
        self._curve_widget.set_curve(
            self._cfg["points"],
            auto_below_first=self._cfg["auto_below_first_point"],
            hysteresis_c=self._cfg["hysteresis_c"],
            enabled=self._cfg["enabled"],
        )
        self._apply_config()

    def _on_curve_edited(self, new_points: list[list[int]]) -> None:
        self._cfg["points"] = new_points
        self._cfg["preset"] = "Custom"
        self._dirty_curve = True
        self._updating_ui = True
        self._cmb_preset.setCurrentText("Custom")
        self._updating_ui = False
        self._btn_apply.setText("Apply & Save Curve *")

    def _apply_config(self) -> None:
        self._cfg["points"] = self._curve_widget.points()
        clean = validate_and_sanitize_config(self._cfg)
        self._cfg = clean
        if not self._daemon_connected:
            QMessageBox.warning(
                self,
                "Daemon Not Running",
                "The background service (yoga-pro-9-fancurve.service) is not running or not reachable.\n"
                "Run install.sh (or start the service) to enable live fan control.",
            )
            return
        try:
            resp = send_ipc_command({"cmd": "set_config", "config": clean}, SOCKET_PATH)
            if resp.get("ok"):
                self._cfg = resp["config"]
                self._dirty_curve = False
                self._btn_apply.setText("Apply & Save Curve")
                self._sync_controls_from_cfg()
                self._poll_state()
            else:
                self._lbl_footer.setText(f"Daemon rejected config: {resp.get('error')}")
        except OSError as exc:
            self._lbl_footer.setText(f"IPC error: {exc}")

    def _restore_bios_auto(self) -> None:
        if not self._daemon_connected:
            return
        try:
            resp = send_ipc_command({"cmd": "restore_auto"}, SOCKET_PATH)
            if resp.get("ok"):
                self._cfg = resp["config"]
                self._dirty_curve = False
                self._btn_apply.setText("Apply & Save Curve")
                self._sync_controls_from_cfg()
                self._poll_state()
        except OSError as exc:
            self._lbl_footer.setText(f"Failed restoring BIOS Auto: {exc}")

    def _sync_controls_from_cfg(self) -> None:
        self._updating_ui = True
        self._chk_enabled.setChecked(bool(self._cfg.get("enabled", True)))
        self._chk_ac_only.setChecked(bool(self._cfg.get("ac_only", True)))
        self._chk_gpu.setChecked(bool(self._cfg.get("include_gpu_temp", True)))
        self._chk_auto_below.setChecked(bool(self._cfg.get("auto_below_first_point", True)))
        self._spn_hyst.setValue(float(self._cfg.get("hysteresis_c", 3.0)))
        preset = self._cfg.get("preset", "Custom")
        idx = self._cmb_preset.findText(preset)
        if idx >= 0:
            self._cmb_preset.setCurrentIndex(idx)
        self._curve_widget.set_curve(
            self._cfg.get("points", default_config()["points"]),
            auto_below_first=bool(self._cfg.get("auto_below_first_point", True)),
            hysteresis_c=float(self._cfg.get("hysteresis_c", 3.0)),
            enabled=bool(self._cfg.get("enabled", True)),
        )
        if self._tray is not None:
            self._act_tray_enable.setChecked(bool(self._cfg.get("enabled", True)))
            self._act_tray_ac.setChecked(bool(self._cfg.get("ac_only", True)))
        self._updating_ui = False

    def _poll_state(self) -> None:
        try:
            resp = send_ipc_command({"cmd": "get_state"}, SOCKET_PATH)
            if resp.get("ok"):
                was_disconnected = not self._daemon_connected
                self._daemon_connected = True
                if not self._dirty_curve or was_disconnected:
                    self._cfg = resp["config"]
                    self._sync_controls_from_cfg()
                status = resp.get("status") or {}
                history = resp.get("history") or []
                self._render_status(status, history)
                return
        except OSError:
            pass

        # Fallback: direct read-only hardware preview when daemon is stopped
        self._daemon_connected = False
        snap = self._hw_fallback.read_snapshot(query_gpu=True)
        fallback_status = {
            "ready": self._hw_fallback.is_fan_interface_ready(),
            "cpu_temp_c": snap.cpu_temp_c,
            "gpu_temp_c": snap.gpu_temp_c,
            "gpu_state": snap.gpu_state,
            "effective_temp_c": snap.cpu_temp_c,
            "fan1_rpm": snap.fan1_rpm,
            "fan2_rpm": snap.fan2_rpm,
            "fan1_target": snap.fan1_target_sysfs or 0,
            "fan2_target": snap.fan2_target_sysfs or 0,
            "ac_online": snap.ac_online,
            "ac_source": snap.ac_source,
            "platform_profile": snap.platform_profile,
            "mode": "daemon_offline",
        }
        self._render_status(fallback_status, [])

    def _render_status(self, st: dict[str, Any], history: list[dict[str, Any]]) -> None:
        cpu = st.get("cpu_temp_c")
        gpu = st.get("gpu_temp_c")
        gpu_state = st.get("gpu_state", "unavailable")
        eff = st.get("effective_temp_c")
        f1 = st.get("fan1_rpm")
        f2 = st.get("fan2_rpm")
        t1 = st.get("fan1_target", 0)
        t2 = st.get("fan2_target", 0)
        ac_online = st.get("ac_online", False)
        ac_source = st.get("ac_source", "Battery")
        profile = st.get("platform_profile", "unknown")
        mode = st.get("mode", "unknown")

        self._val_cpu.setText(f"{cpu:.1f} °C" if cpu is not None else "— °C")
        self._sub_cpu.setText(f"Smoothed: {eff:.1f} °C" if eff is not None else "Smoothed: —")

        if gpu is not None:
            self._val_gpu.setText(f"{gpu:.1f} °C")
            self._sub_gpu.setText("Active (PCI D0)")
        elif gpu_state == "suspended":
            self._val_gpu.setText("Sleeping")
            self._sub_gpu.setText("D3cold (not woken)")
        else:
            self._val_gpu.setText("—")
            self._sub_gpu.setText(gpu_state)

        self._val_f1.setText(f"{f1} RPM" if f1 is not None else "— RPM")
        self._sub_f1.setText(f"Target: {t1} RPM" if t1 else "Target: BIOS Auto")

        self._val_f2.setText(f"{f2} RPM" if f2 is not None else "— RPM")
        self._sub_f2.setText(f"Target: {t2} RPM" if t2 else "Target: BIOS Auto")

        self._val_pwr.setText("Plugged In" if ac_online else "On Battery")
        self._val_pwr.setStyleSheet(
            "color: #4ade80; font-size: 18px; font-weight: 700;"
            if ac_online
            else "color: #fbbf24; font-size: 18px; font-weight: 700;"
        )
        self._sub_pwr.setText(f"{ac_source} · {profile}")

        badge_styles = {
            "curve": ("ACTIVE CURVE", "#0c4a6e", "#38bdf8", "#e0f2fe"),
            "emergency": ("EMERGENCY 5000 RPM", "#7f1d1d", "#ef4444", "#fecaca"),
            "bios_auto_cool": ("BIOS AUTO (COOL)", "#1e293b", "#38bdf8", "#bae6fd"),
            "bios_auto_battery": ("BIOS AUTO (BATTERY)", "#422006", "#f59e0b", "#fde68a"),
            "bios_auto_disabled": ("BIOS AUTO (OFF)", "#1e293b", "#64748b", "#cbd5e1"),
            "daemon_offline": ("DAEMON OFFLINE (PREVIEW)", "#3b1d2e", "#f43f5e", "#fecdd3"),
            "unavailable": ("DRIVER NOT EXPOSED", "#3b1d2e", "#f43f5e", "#fecdd3"),
        }
        text, bg, border, fg = badge_styles.get(
            mode, (mode.upper(), "#1e293b", "#475569", "#cbd5e1")
        )
        self._badge_mode.setText(text)
        self._badge_mode.setStyleSheet(
            f"background-color: {bg}; color: {fg}; border: 1px solid {border};"
            "border-radius: 12px; padding: 5px 12px; font-weight: 700; font-size: 11px;"
        )

        self._curve_widget.set_live_state(eff, f1, f2, t1, mode)
        if history:
            self._history_widget.set_history(history)

        if self._tray is not None:
            is_active = mode in ("curve", "emergency")
            self._tray.setIcon(self._icon_active if is_active else self._icon_auto)
            cpu_str = f"{cpu:.0f}°C" if cpu is not None else "—"
            rpm_str = f"{f1}/{f2} RPM" if (f1 is not None and f2 is not None) else "—"
            self._tray.setToolTip(f"Yoga Pro 9 Fans [{text}]\nCPU: {cpu_str} · Fans: {rpm_str}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Yoga Pro 9 16IMH9 Fan Curve GUI")
    parser.add_argument("--minimized", action="store_true", help="Start minimized in the system tray")
    args, qt_args = parser.parse_known_args(argv)

    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("Yoga Pro 9 Fan Curve")
    app.setQuitOnLastWindowClosed(False)

    # Single-instance check: if already running (e.g. in tray), signal it to raise & focus
    server_name = "yoga-pro-9-fancurve-gui"
    probe = QLocalSocket()
    probe.connectToServer(server_name)
    if probe.waitForConnected(250):
        probe.write(b"SHOW\n")
        probe.flush()
        probe.waitForBytesWritten(250)
        probe.disconnectFromServer()
        return 0

    win = YogaFanWindow(start_minimized=args.minimized)

    QLocalServer.removeServer(server_name)
    ipc_server = QLocalServer(win)
    if ipc_server.listen(server_name):
        def _on_new_conn() -> None:
            sock = ipc_server.nextPendingConnection()
            if sock is not None:
                sock.waitForReadyRead(200)
                sock.readAll()
                sock.close()
                win._show_and_raise()
        ipc_server.newConnection.connect(_on_new_conn)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

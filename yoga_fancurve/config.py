"""Configuration, presets, curve evaluation, and hard safety bounds."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Hard hardware safety limits verified against Yoga Pro 9 16IMH9 (83DN)
# BIOS NKCN35WW DSDT (WMAE 0x04030001/2) & SSDT2 (FPS0 max 0x1388 = 5000 RPM).
HARD_MIN_MANUAL_RPM = 2000  # Avoids <1800 RPM EC stall/dead-zone & bearing stutter
HARD_MAX_CURVE_RPM = 4800   # Keeps sustained fan curves slightly under 5000 RPM ceiling
HARD_EMERGENCY_RPM = 5000   # Matches ACPI FPS0 100% spec (0x1388)
RPM_STEP = 100              # Firmware divides SFV1 by 0x64 (100)

MIN_POINT_TEMP_C = 35
MAX_POINT_TEMP_C = 92

EMERGENCY_CPU_TEMP_C = 92.0
EMERGENCY_GPU_TEMP_C = 85.0
EMERGENCY_RELEASE_DELTA_C = 6.0

CONFIG_DIR = Path("/etc/yoga-pro-9-fancurve")
CONFIG_PATH = CONFIG_DIR / "config.json"
RUN_DIR = Path("/run/yoga-pro-9-fancurve")
STATUS_PATH = RUN_DIR / "status.json"
SOCKET_PATH = RUN_DIR / "control.sock"

PRESETS: dict[str, dict[str, Any]] = {
    "Balanced-Cool": {
        "description": "Windows-like responsive cooling: quiet BIOS auto below 48°C, smooth dynamic ramp under load.",
        "points": [
            [50, 2400],
            [60, 3000],
            [70, 3700],
            [80, 4400],
            [88, 4800],
        ],
        "hysteresis_c": 3.0,
        "auto_below_first_point": True,
    },
    "Proactive Cool": {
        "description": "Starts airflow earlier (45°C) to keep chassis and keyboard deck cooler while plugged in.",
        "points": [
            [45, 2400],
            [55, 3100],
            [65, 3800],
            [75, 4400],
            [85, 4800],
        ],
        "hysteresis_c": 3.0,
        "auto_below_first_point": True,
    },
    "Quiet": {
        "description": "Low-noise curve for light work; ramps up cleanly once temperatures exceed 65°C.",
        "points": [
            [56, 2200],
            [66, 2800],
            [76, 3600],
            [84, 4300],
            [90, 4800],
        ],
        "hysteresis_c": 4.0,
        "auto_below_first_point": True,
    },
    "BIOS Auto": {
        "description": "Hands 100% control back to Lenovo firmware automatic fan management.",
        "points": [
            [50, 2400],
            [60, 3000],
            [70, 3700],
            [80, 4400],
            [88, 4800],
        ],
        "hysteresis_c": 3.0,
        "auto_below_first_point": True,
    },
}


def default_config() -> dict[str, Any]:
    preset = PRESETS["Balanced-Cool"]
    return {
        "enabled": True,
        "ac_only": True,
        "preset": "Balanced-Cool",
        "points": copy.deepcopy(preset["points"]),
        "hysteresis_c": preset["hysteresis_c"],
        "auto_below_first_point": True,
        "poll_interval_s": 1.5,
        "smoothing_alpha": 0.35,
        "max_ramp_up_rpm": 400,
        "max_ramp_down_rpm": 200,
        "fan2_offset_rpm": 0,
        "include_gpu_temp": True,
    }


def round_rpm(rpm: float) -> int:
    """Round an RPM value to the nearest 100 RPM step."""
    return int(round(rpm / RPM_STEP) * RPM_STEP)


def clamp_safe_manual_rpm(rpm: int, allow_emergency: bool = False) -> int:
    """Enforce hardware safety limits. Never allows values in the 1..1999 dead/stop zone."""
    if rpm <= 0:
        return 0
    max_rpm = HARD_EMERGENCY_RPM if allow_emergency else HARD_MAX_CURVE_RPM
    clamped = max(HARD_MIN_MANUAL_RPM, min(max_rpm, round_rpm(rpm)))
    return clamped


def validate_and_sanitize_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and clamp all configuration fields against hard hardware safety bounds."""
    cfg = default_config()
    if not isinstance(raw, dict):
        return cfg

    cfg["enabled"] = bool(raw.get("enabled", cfg["enabled"]))
    cfg["ac_only"] = bool(raw.get("ac_only", cfg["ac_only"]))
    cfg["auto_below_first_point"] = bool(
        raw.get("auto_below_first_point", cfg["auto_below_first_point"])
    )
    cfg["include_gpu_temp"] = bool(raw.get("include_gpu_temp", cfg["include_gpu_temp"]))

    preset = raw.get("preset", cfg["preset"])
    if isinstance(preset, str) and (preset in PRESETS or preset == "Custom"):
        cfg["preset"] = preset
    else:
        cfg["preset"] = "Custom"

    # Hysteresis: 1.0 to 8.0 °C
    try:
        hyst = float(raw.get("hysteresis_c", cfg["hysteresis_c"]))
        cfg["hysteresis_c"] = round(max(1.0, min(8.0, hyst)), 1)
    except (TypeError, ValueError):
        pass

    # Poll interval: 1.0 to 5.0 seconds
    try:
        poll = float(raw.get("poll_interval_s", cfg["poll_interval_s"]))
        cfg["poll_interval_s"] = round(max(1.0, min(5.0, poll)), 2)
    except (TypeError, ValueError):
        pass

    # Smoothing alpha: 0.15 to 1.0
    try:
        alpha = float(raw.get("smoothing_alpha", cfg["smoothing_alpha"]))
        cfg["smoothing_alpha"] = round(max(0.15, min(1.0, alpha)), 2)
    except (TypeError, ValueError):
        pass

    # Slew limits
    try:
        up = int(raw.get("max_ramp_up_rpm", cfg["max_ramp_up_rpm"]))
        cfg["max_ramp_up_rpm"] = max(200, min(1000, round_rpm(up)))
    except (TypeError, ValueError):
        pass

    try:
        down = int(raw.get("max_ramp_down_rpm", cfg["max_ramp_down_rpm"]))
        cfg["max_ramp_down_rpm"] = max(100, min(600, round_rpm(down)))
    except (TypeError, ValueError):
        pass

    # Fan 2 offset: -400 to +400 RPM
    try:
        offset = int(raw.get("fan2_offset_rpm", cfg["fan2_offset_rpm"]))
        cfg["fan2_offset_rpm"] = max(-400, min(400, round_rpm(offset)))
    except (TypeError, ValueError):
        pass

    # Curve points: list of [temp_c, rpm], sorted by temp, non-decreasing RPM
    raw_points = raw.get("points")
    clean_points: list[list[int]] = []
    if isinstance(raw_points, list) and len(raw_points) >= 2:
        parsed: list[tuple[int, int]] = []
        for item in raw_points[:10]:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                try:
                    t = int(round(float(item[0])))
                    r = int(round(float(item[1])))
                except (TypeError, ValueError):
                    continue
                t = max(MIN_POINT_TEMP_C, min(MAX_POINT_TEMP_C, t))
                r = clamp_safe_manual_rpm(r, allow_emergency=False)
                if r == 0:
                    r = HARD_MIN_MANUAL_RPM
                parsed.append((t, r))
        # Deduplicate temperatures (keep higher RPM if duplicate temp)
        by_temp: dict[int, int] = {}
        for t, r in parsed:
            by_temp[t] = max(by_temp.get(t, 0), r)
        sorted_pts = sorted(by_temp.items(), key=lambda x: x[0])
        if len(sorted_pts) >= 2:
            # Enforce monotonically non-decreasing RPM so hotter temps never lower fan speed
            running_rpm = HARD_MIN_MANUAL_RPM
            for t, r in sorted_pts:
                running_rpm = max(running_rpm, r)
                clean_points.append([t, running_rpm])

    if len(clean_points) >= 2:
        cfg["points"] = clean_points

    return cfg


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return default_config()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return validate_and_sanitize_config(data)
    except Exception:
        return default_config()


def save_config(cfg: dict[str, Any], path: Path = CONFIG_PATH) -> dict[str, Any]:
    clean = validate_and_sanitize_config(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return clean


def interpolate_curve_rpm(temp_c: float, points: list[list[int]], auto_below_first: bool = True) -> int:
    """Linearly interpolate target RPM from sorted [temp_c, rpm] points."""
    if not points:
        return 0
    pts = sorted(points, key=lambda p: p[0])
    if temp_c < pts[0][0]:
        return 0 if auto_below_first else clamp_safe_manual_rpm(pts[0][1])
    if temp_c >= pts[-1][0]:
        return clamp_safe_manual_rpm(pts[-1][1])

    for i in range(len(pts) - 1):
        t0, r0 = pts[i]
        t1, r1 = pts[i + 1]
        if t0 <= temp_c <= t1:
            if t1 == t0:
                return clamp_safe_manual_rpm(r1)
            frac = (temp_c - t0) / (t1 - t0)
            rpm = r0 + frac * (r1 - r0)
            return clamp_safe_manual_rpm(round_rpm(rpm))

    return clamp_safe_manual_rpm(pts[-1][1])


@dataclass
class ControllerDecision:
    fan1_target: int
    fan2_target: int
    mode: str  # "curve", "bios_auto_disabled", "bios_auto_battery", "bios_auto_cool", "emergency"
    effective_temp_c: float
    raw_curve_rpm: int


class FanCurveController:
    """Stateful thermal controller with EMA smoothing, hysteresis, slew limiting, and emergency failsafe."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = validate_and_sanitize_config(cfg)
        self.smoothed_temp_c: float | None = None
        self.current_base_rpm: int = 0  # 0 means BIOS Auto
        self.in_emergency: bool = False
        self.peak_temp_for_hyst: float | None = None
        self.active_manual: bool = False

    def update_config(self, cfg: dict[str, Any]) -> None:
        self.cfg = validate_and_sanitize_config(cfg)

    def reset_state(self) -> None:
        self.current_base_rpm = 0
        self.in_emergency = False
        self.active_manual = False
        self.peak_temp_for_hyst = None

    def step(
        self,
        cpu_temp_c: float,
        gpu_temp_c: float | None,
        ac_online: bool,
    ) -> ControllerDecision:
        """Compute next safe fan targets from latest sensor readings."""
        # 1. Determine raw driving temperature
        driving_raw = cpu_temp_c
        if self.cfg["include_gpu_temp"] and gpu_temp_c is not None:
            driving_raw = max(cpu_temp_c, gpu_temp_c)

        # 2. EMA smoothing to ignore 0.5s CPU turbo micro-spikes
        alpha = self.cfg["smoothing_alpha"]
        if self.smoothed_temp_c is None:
            self.smoothed_temp_c = driving_raw
        else:
            self.smoothed_temp_c = alpha * driving_raw + (1.0 - alpha) * self.smoothed_temp_c

        eff_temp = self.smoothed_temp_c

        # 3. Check master enable & BIOS Auto preset FIRST so "OFF" truly hands full control to BIOS
        if not self.cfg["enabled"] or self.cfg["preset"] == "BIOS Auto":
            self.reset_state()
            return ControllerDecision(
                fan1_target=0,
                fan2_target=0,
                mode="bios_auto_disabled",
                effective_temp_c=round(eff_temp, 1),
                raw_curve_rpm=0,
            )

        # 4. Check AC-only mode (when unplugged, hand 100% control to BIOS Auto)
        if self.cfg["ac_only"] and not ac_online:
            self.reset_state()
            return ControllerDecision(
                fan1_target=0,
                fan2_target=0,
                mode="bios_auto_battery",
                effective_temp_c=round(eff_temp, 1),
                raw_curve_rpm=0,
            )

        # 5. Hardware emergency failsafe while custom curve is active (uses unsmoothed temps for fast reaction)
        gpu_hot = gpu_temp_c is not None and gpu_temp_c >= EMERGENCY_GPU_TEMP_C
        gpu_cool = gpu_temp_c is None or gpu_temp_c <= (EMERGENCY_GPU_TEMP_C - EMERGENCY_RELEASE_DELTA_C)
        if cpu_temp_c >= EMERGENCY_CPU_TEMP_C or gpu_hot:
            self.in_emergency = True
        elif self.in_emergency and cpu_temp_c <= (EMERGENCY_CPU_TEMP_C - EMERGENCY_RELEASE_DELTA_C) and gpu_cool:
            self.in_emergency = False

        if self.in_emergency:
            self.current_base_rpm = HARD_EMERGENCY_RPM
            self.active_manual = True
            return ControllerDecision(
                fan1_target=HARD_EMERGENCY_RPM,
                fan2_target=HARD_EMERGENCY_RPM,
                mode="emergency",
                effective_temp_c=round(eff_temp, 1),
                raw_curve_rpm=HARD_EMERGENCY_RPM,
            )

        # 6. Evaluate curve with hysteresis on ramp-down & auto-zone entry
        points = self.cfg["points"]
        hyst = self.cfg["hysteresis_c"]
        auto_below = self.cfg["auto_below_first_point"]
        first_temp = points[0][0]

        # Enter/exit manual curve cleanly with hysteresis around first_temp
        if auto_below:
            if not self.active_manual:
                if eff_temp >= first_temp:
                    self.active_manual = True
                else:
                    self.current_base_rpm = 0
                    return ControllerDecision(
                        fan1_target=0,
                        fan2_target=0,
                        mode="bios_auto_cool",
                        effective_temp_c=round(eff_temp, 1),
                        raw_curve_rpm=0,
                    )
            else:
                if eff_temp <= (first_temp - hyst):
                    self.active_manual = False
                    self.current_base_rpm = 0
                    self.peak_temp_for_hyst = None
                    return ControllerDecision(
                        fan1_target=0,
                        fan2_target=0,
                        mode="bios_auto_cool",
                        effective_temp_c=round(eff_temp, 1),
                        raw_curve_rpm=0,
                    )
        else:
            self.active_manual = True

        desired_rpm = interpolate_curve_rpm(eff_temp, points, auto_below_first=False)

        # Asymmetric hysteresis: ramp UP immediately, require cooling by `hyst` before stepping DOWN
        if self.current_base_rpm == 0:
            next_rpm = desired_rpm
            self.peak_temp_for_hyst = eff_temp
        elif desired_rpm > self.current_base_rpm:
            # Slew-limit ramp up for acoustic smoothness
            max_up = self.cfg["max_ramp_up_rpm"]
            next_rpm = min(desired_rpm, self.current_base_rpm + max_up)
            self.peak_temp_for_hyst = eff_temp
        elif desired_rpm < self.current_base_rpm:
            # Only step down if temp dropped below the curve temp for (current_base_rpm) minus hysteresis
            # Approximate cleanly: compare eff_temp + hyst on the curve
            hyst_rpm = interpolate_curve_rpm(eff_temp + hyst, points, auto_below_first=False)
            if hyst_rpm < self.current_base_rpm:
                max_down = self.cfg["max_ramp_down_rpm"]
                target_down = max(desired_rpm, hyst_rpm)
                next_rpm = max(target_down, self.current_base_rpm - max_down)
            else:
                next_rpm = self.current_base_rpm
        else:
            next_rpm = self.current_base_rpm

        next_rpm = clamp_safe_manual_rpm(next_rpm, allow_emergency=False)
        self.current_base_rpm = next_rpm

        offset = self.cfg["fan2_offset_rpm"]
        fan1 = clamp_safe_manual_rpm(next_rpm, allow_emergency=False)
        fan2 = clamp_safe_manual_rpm(next_rpm + offset, allow_emergency=False)

        return ControllerDecision(
            fan1_target=fan1,
            fan2_target=fan2,
            mode="curve",
            effective_temp_c=round(eff_temp, 1),
            raw_curve_rpm=desired_rpm,
        )

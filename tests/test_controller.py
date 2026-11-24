"""Unit tests for safety bounds, curve interpolation, hysteresis, slew limits, and failsafes."""

from __future__ import annotations

import unittest

from yoga_fancurve.config import (
    EMERGENCY_CPU_TEMP_C,
    EMERGENCY_GPU_TEMP_C,
    HARD_EMERGENCY_RPM,
    HARD_MAX_CURVE_RPM,
    HARD_MIN_MANUAL_RPM,
    FanCurveController,
    clamp_safe_manual_rpm,
    default_config,
    interpolate_curve_rpm,
    validate_and_sanitize_config,
)
from yoga_fancurve.hardware import YogaHardware
from yoga_fancurve.hotkeys import parse_edp_refresh_modes


class SafetyBoundsTests(unittest.TestCase):
    def test_never_allows_dead_zone_or_fan_stop_value_one(self) -> None:
        # Critical BIOS safety invariant: writing 1 stops the fan via MBEY(0xEF, 0x61, 0)
        self.assertEqual(clamp_safe_manual_rpm(0), 0)
        self.assertEqual(clamp_safe_manual_rpm(-500), 0)
        for dangerous in (1, 2, 100, 500, 1200, 1799, 1999):
            self.assertEqual(clamp_safe_manual_rpm(dangerous), HARD_MIN_MANUAL_RPM)
            self.assertEqual(YogaHardware._sanitize_hw_rpm(dangerous), HARD_MIN_MANUAL_RPM)

    def test_clamps_high_rpm_and_rounds_to_100(self) -> None:
        self.assertEqual(clamp_safe_manual_rpm(2540), 2500)
        self.assertEqual(clamp_safe_manual_rpm(2560), 2600)
        self.assertEqual(clamp_safe_manual_rpm(6500, allow_emergency=False), HARD_MAX_CURVE_RPM)
        self.assertEqual(clamp_safe_manual_rpm(6500, allow_emergency=True), HARD_EMERGENCY_RPM)
        self.assertEqual(YogaHardware._sanitize_hw_rpm(9999), HARD_EMERGENCY_RPM)

    def test_config_sanitization_enforces_monotonic_safe_points(self) -> None:
        raw = {
            "enabled": True,
            "ac_only": True,
            "hysteresis_c": 99.0,
            "points": [[80, 3000], [50, 1], [65, 9000], [60, 2600]],
        }
        clean = validate_and_sanitize_config(raw)
        self.assertEqual(clean["hysteresis_c"], 8.0)
        pts = clean["points"]
        # Sorted by temp, safe RPM floor/ceiling, non-decreasing RPM
        self.assertEqual([p[0] for p in pts], [50, 60, 65, 80])
        for i in range(len(pts)):
            self.assertGreaterEqual(pts[i][1], HARD_MIN_MANUAL_RPM)
            self.assertLessEqual(pts[i][1], HARD_MAX_CURVE_RPM)
            if i > 0:
                self.assertGreaterEqual(pts[i][1], pts[i - 1][1])


class ControllerBehaviorTests(unittest.TestCase):
    def test_ac_only_forces_bios_auto_on_battery(self) -> None:
        cfg = default_config()
        cfg["ac_only"] = True
        cfg["smoothing_alpha"] = 1.0
        ctrl = FanCurveController(cfg)

        dec = ctrl.step(cpu_temp_c=72.0, gpu_temp_c=60.0, ac_online=False)
        self.assertEqual(dec.mode, "bios_auto_battery")
        self.assertEqual((dec.fan1_target, dec.fan2_target), (0, 0))

        # Plugging in immediately activates the curve
        dec_ac = ctrl.step(cpu_temp_c=72.0, gpu_temp_c=60.0, ac_online=True)
        self.assertEqual(dec_ac.mode, "curve")
        self.assertGreaterEqual(dec_ac.fan1_target, 3700)

    def test_disabled_or_bios_auto_preset_returns_zero(self) -> None:
        cfg = default_config()
        cfg["enabled"] = False
        ctrl = FanCurveController(cfg)
        dec = ctrl.step(cpu_temp_c=75.0, gpu_temp_c=70.0, ac_online=True)
        self.assertEqual(dec.mode, "bios_auto_disabled")
        self.assertEqual((dec.fan1_target, dec.fan2_target), (0, 0))

    def test_hysteresis_prevents_rapid_oscillation(self) -> None:
        cfg = default_config()
        cfg["smoothing_alpha"] = 1.0
        cfg["hysteresis_c"] = 4.0
        cfg["max_ramp_up_rpm"] = 1000
        ctrl = FanCurveController(cfg)

        # Ramp up to 70C -> 3700 RPM
        d1 = ctrl.step(cpu_temp_c=70.0, gpu_temp_c=None, ac_online=True)
        self.assertEqual(d1.fan1_target, 3700)

        # Small 2C dip (within 4C hysteresis) must hold RPM steady
        d2 = ctrl.step(cpu_temp_c=68.0, gpu_temp_c=None, ac_online=True)
        self.assertEqual(d2.fan1_target, 3700)

        # Cooling 6C (past 4C hysteresis) begins gentle slew-limited ramp down
        d3 = ctrl.step(cpu_temp_c=64.0, gpu_temp_c=None, ac_online=True)
        self.assertLess(d3.fan1_target, 3700)
        self.assertGreaterEqual(d3.fan1_target, 3700 - cfg["max_ramp_down_rpm"])

    def test_cool_auto_zone_entry_and_exit_with_hysteresis(self) -> None:
        cfg = default_config()
        cfg["smoothing_alpha"] = 1.0
        cfg["hysteresis_c"] = 3.0
        ctrl = FanCurveController(cfg)

        # Below 50C -> BIOS Auto
        d1 = ctrl.step(cpu_temp_c=45.0, gpu_temp_c=None, ac_online=True)
        self.assertEqual(d1.mode, "bios_auto_cool")
        self.assertEqual(d1.fan1_target, 0)

        # Cross 50C -> enters manual curve
        d2 = ctrl.step(cpu_temp_c=52.0, gpu_temp_c=None, ac_online=True)
        self.assertEqual(d2.mode, "curve")
        self.assertGreaterEqual(d2.fan1_target, 2400)

        # Dip to 48C (still > 50 - 3 = 47C) -> stays in curve, doesn't flap
        d3 = ctrl.step(cpu_temp_c=48.0, gpu_temp_c=None, ac_online=True)
        self.assertEqual(d3.mode, "curve")

        # Cool to 46C (<= 47C) -> cleanly exits back to BIOS Auto
        d4 = ctrl.step(cpu_temp_c=46.0, gpu_temp_c=None, ac_online=True)
        self.assertEqual(d4.mode, "bios_auto_cool")
        self.assertEqual(d4.fan1_target, 0)

    def test_emergency_failsafe_overrides_low_manual_curve_and_respects_bios_auto_off(self) -> None:
        cfg = default_config()
        cfg["enabled"] = True
        cfg["ac_only"] = True
        cfg["points"] = [[50, 2000], [90, 2400]]  # Artificially weak curve
        ctrl = FanCurveController(cfg)

        # CPU emergency overrides weak manual curve -> 5000 RPM
        d_cpu = ctrl.step(cpu_temp_c=EMERGENCY_CPU_TEMP_C, gpu_temp_c=None, ac_online=True)
        self.assertEqual(d_cpu.mode, "emergency")
        self.assertEqual((d_cpu.fan1_target, d_cpu.fan2_target), (HARD_EMERGENCY_RPM, HARD_EMERGENCY_RPM))

        # GPU emergency overrides weak manual curve -> 5000 RPM
        ctrl2 = FanCurveController(cfg)
        d_gpu = ctrl2.step(cpu_temp_c=70.0, gpu_temp_c=EMERGENCY_GPU_TEMP_C, ac_online=True)
        self.assertEqual(d_gpu.mode, "emergency")
        self.assertEqual((d_gpu.fan1_target, d_gpu.fan2_target), (HARD_EMERGENCY_RPM, HARD_EMERGENCY_RPM))

        # When explicitly disabled, BIOS handles thermal safety directly (stays 0)
        cfg_off = default_config()
        cfg_off["enabled"] = False
        ctrl_off = FanCurveController(cfg_off)
        d_off = ctrl_off.step(cpu_temp_c=EMERGENCY_CPU_TEMP_C, gpu_temp_c=None, ac_online=True)
        self.assertEqual(d_off.mode, "bios_auto_disabled")
        self.assertEqual((d_off.fan1_target, d_off.fan2_target), (0, 0))

    def test_curve_interpolation_exact_points(self) -> None:
        pts = [[50, 2400], [60, 3000], [70, 3700]]
        self.assertEqual(interpolate_curve_rpm(45, pts, auto_below_first=True), 0)
        self.assertEqual(interpolate_curve_rpm(50, pts), 2400)
        self.assertEqual(interpolate_curve_rpm(55, pts), 2700)
        self.assertEqual(interpolate_curve_rpm(70, pts), 3700)
        self.assertEqual(interpolate_curve_rpm(85, pts), 3700)


class HotkeyParserTests(unittest.TestCase):
    def test_parse_edp_refresh_modes_strips_ansi_and_finds_60_and_165(self) -> None:
        sample = (
            "Output: 1 HDMI-A-1 uuid1\n"
            "\tModes:  1:1920x1080@60.00!  2:1920x1080@75.00*\n"
            "Output: 3 eDP-1 uuid3\n"
            "\tModes:  \x1b[32m15:3200x2000@60.00*!\x1b[0m  16:3200x2000@165.00  25:1920x1200@59.88\n"
        )
        cur_id, modes = parse_edp_refresh_modes(sample, "eDP-1")
        self.assertEqual(cur_id, "15")
        self.assertEqual(modes, [("15", 60.0), ("16", 165.0)])


if __name__ == "__main__":
    unittest.main()

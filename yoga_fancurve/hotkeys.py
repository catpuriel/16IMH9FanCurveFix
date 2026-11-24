"""Lenovo Yoga Pro 9 16IMH9 Hotkey & Native KDE Plasma OSD Daemon.

Runs in the user's KDE Plasma graphical session (no root required once the
uaccess udev rule for 'Ideapad extra buttons' is installed).

Handles:
- Fn+Q (platform_profile sysfs change) -> Native KDE Power Profile OSD popup
- Fn+Space (kbd_backlight sysfs change) -> Native KDE Keyboard Brightness bar OSD
- Fn+Esc (fn_lock sysfs change) -> Native KDE OSD popup ("Fn Lock: ON / OFF")
- Camera switch (camera_power sysfs change) -> Native KDE OSD popup ("Camera: ON / OFF")
- Fn+R (KEY_REFRESH_RATE_TOGGLE) -> Toggles eDP-1 between 60 Hz and 165 Hz + KDE OSD
- Star / Vantage key (KEY_FAVORITES) -> Opens Yoga Pro 9 Fan Curve GUI + KDE OSD
- Smart key (scancode 0x141 / KEY_UNKNOWN) -> Cycles Fan Curve Presets + KDE OSD
"""

from __future__ import annotations

import logging
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import evdev
from evdev import ecodes

from .config import PRESETS, SOCKET_PATH
from .daemon import send_ipc_command

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("yoga_fancurve.hotkeys")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

SYSFS_PLATFORM_PROFILE = Path("/sys/firmware/acpi/platform_profile")
SYSFS_KBD_BACKLIGHT = Path("/sys/class/leds/platform::kbd_backlight/brightness")
SYSFS_KBD_BACKLIGHT_MAX = Path("/sys/class/leds/platform::kbd_backlight/max_brightness")
SYSFS_FN_LOCK = Path("/sys/class/leds/platform::fnlock/brightness")
SYSFS_CAMERA_POWER = Path("/sys/bus/platform/devices/VPC2004:00/camera_power")

PRESET_CYCLE_ORDER = ["Balanced-Cool", "Proactive Cool", "Quiet", "BIOS Auto"]

PROFILE_TO_KDE = {
    "low-power": ("power-saver", "battery-profile-powersave", "Power Saver (Quiet)"),
    "quiet": ("power-saver", "battery-profile-powersave", "Power Saver (Quiet)"),
    "balanced": ("balanced", "battery-profile-balanced", "Balanced Mode"),
    "performance": ("performance", "battery-profile-performance", "Performance Mode"),
}


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def kde_osd_call(method: str, *args: str) -> None:
    """Invoke KDE Plasma 6 native OSD D-Bus service."""
    try:
        subprocess.run(
            [
                "qdbus6",
                "org.kde.plasmashell",
                "/org/kde/osdService",
                f"org.kde.osdService.{method}",
                *args,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("KDE OSD call %s failed: %s", method, exc)


def osd_show_text(icon: str, text: str) -> None:
    kde_osd_call("showText", icon, text)


def osd_power_profile(sysfs_profile: str) -> None:
    kde_name, icon, label = PROFILE_TO_KDE.get(
        sysfs_profile, ("balanced", "speedometer", sysfs_profile.title())
    )
    # Use KDE's native powerProfileChanged OSD for authentic Plasma styling
    kde_osd_call("powerProfileChanged", kde_name)
    log.info("OSD power profile: %s (%s)", label, kde_name)


def osd_kbd_backlight(level: int, max_level: int) -> None:
    if max_level <= 0:
        max_level = 2
    pct = int(round((level / max_level) * 100))
    kde_osd_call("keyboardBrightnessChanged", str(pct))
    log.info("OSD keyboard backlight: %d/%d (%d%%)", level, max_level, pct)


def parse_edp_refresh_modes(kscreen_output: str, output_name: str = "eDP-1") -> tuple[str | None, list[tuple[str, float]]]:
    """Parse kscreen-doctor -o output for an output's current mode and sibling refresh-rate modes.

    Returns (current_mode_id, [(mode_id, hz), ...]) for the same resolution as the active mode.
    """
    clean = ANSI_RE.sub("", kscreen_output)
    in_target = False
    modes_line = ""
    for line in clean.splitlines():
        if line.startswith("Output:"):
            in_target = output_name in line.split()
            continue
        if in_target and "Modes:" in line:
            modes_line = line.split("Modes:", 1)[1]
            break

    if not modes_line:
        return None, []

    # Tokens look like: 15:3200x2000@60.00*!  16:3200x2000@165.00
    pattern = re.compile(r"(\d+):(\d+x\d+)@(\d+(?:\.\d+)?)([*!]*)")
    parsed: list[tuple[str, str, float, bool]] = []
    current_res: str | None = None
    current_id: str | None = None

    for m in pattern.finditer(modes_line):
        mid, res, hz_s, flags = m.groups()
        hz = float(hz_s)
        is_cur = "*" in flags
        parsed.append((mid, res, hz, is_cur))
        if is_cur:
            current_res = res
            current_id = mid

    if current_res is None:
        return None, []

    same_res = sorted(
        [(mid, hz) for (mid, res, hz, _) in parsed if res == current_res],
        key=lambda item: item[1],
    )
    return current_id, same_res


def toggle_refresh_rate(output_name: str = "eDP-1") -> None:
    """Toggle internal display refresh rate (e.g. 60 Hz <-> 165 Hz) via kscreen-doctor."""
    try:
        res = subprocess.run(
            ["kscreen-doctor", "-o"],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("kscreen-doctor query failed: %s", exc)
        osd_show_text("video-display", "Refresh Rate Switch Failed")
        return

    cur_id, modes = parse_edp_refresh_modes(res.stdout, output_name)
    if not cur_id or len(modes) < 2:
        osd_show_text("video-display", f"{output_name}: Single Refresh Rate")
        return

    # Pick next mode in cycle (typically toggles 60 Hz <-> 165 Hz)
    idx = next((i for i, (mid, _) in enumerate(modes) if mid == cur_id), 0)
    next_id, next_hz = modes[(idx + 1) % len(modes)]

    try:
        apply_res = subprocess.run(
            ["kscreen-doctor", f"output.{output_name}.mode.{next_id}"],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        if apply_res.returncode == 0:
            hz_int = int(round(next_hz))
            osd_show_text("video-display", f"Refresh Rate: {hz_int} Hz")
            log.info("Switched %s to mode %s (%d Hz)", output_name, next_id, hz_int)
        else:
            osd_show_text("video-display", "Refresh Rate Switch Failed")
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("kscreen-doctor apply failed: %s", exc)


def cycle_fan_preset() -> None:
    """Cycle Yoga Pro 9 Fan Curve presets via daemon IPC and show OSD popup."""
    try:
        state = send_ipc_command({"cmd": "get_state"}, SOCKET_PATH)
        if not state.get("ok"):
            osd_show_text("dialog-warning", "Fan Daemon Unavailable")
            return
        cfg = state.get("config", {})
        cur = "BIOS Auto" if not cfg.get("enabled", True) else cfg.get("preset", "Balanced-Cool")
        if cur in PRESET_CYCLE_ORDER:
            nxt = PRESET_CYCLE_ORDER[(PRESET_CYCLE_ORDER.index(cur) + 1) % len(PRESET_CYCLE_ORDER)]
        else:
            nxt = PRESET_CYCLE_ORDER[0]

        resp = send_ipc_command({"cmd": "set_preset", "preset": nxt}, SOCKET_PATH)
        if resp.get("ok"):
            icon = "weather-windy" if nxt != "BIOS Auto" else "preferences-system-power"
            osd_show_text(icon, f"Fan Curve: {nxt}")
            log.info("Cycled fan preset: %s -> %s", cur, nxt)
    except OSError as exc:
        log.warning("Fan preset cycle IPC failed: %s", exc)
        osd_show_text("dialog-warning", "Fan Daemon Offline")


def launch_fan_gui() -> None:
    """Open the Yoga Pro 9 Fan Curve GUI (Star / Vantage hotkey)."""
    repo_dir = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_dir)
    try:
        subprocess.Popen(
            [sys.executable, "-m", "yoga_fancurve.gui"],
            env=env,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        osd_show_text("utilities-system-monitor", "Yoga Pro 9 Fan Curve")
        log.info("Launched Fan Curve GUI via Star/Vantage key")
    except OSError as exc:
        log.warning("Failed launching GUI: %s", exc)


def find_ideapad_input_device() -> evdev.InputDevice | None:
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue
        if dev.name == "Ideapad extra buttons":
            return dev
        dev.close()
    return None


class LenovoHotkeyDaemon:
    def __init__(self) -> None:
        self._running = True
        self._dev: evdev.InputDevice | None = None
        self._last_scancode: int | None = None
        self._last_dev_retry = 0.0

        self._last_profile = _read_text(SYSFS_PLATFORM_PROFILE)
        self._last_kbd_bl = _read_text(SYSFS_KBD_BACKLIGHT)
        self._kbd_bl_max = int(_read_text(SYSFS_KBD_BACKLIGHT_MAX) or "2")
        self._last_fn_lock = _read_text(SYSFS_FN_LOCK)
        self._last_camera = _read_text(SYSFS_CAMERA_POWER)

    def _ensure_device(self) -> None:
        if self._dev is not None:
            return
        now = time.monotonic()
        if now - self._last_dev_retry < 5.0:
            return
        self._last_dev_retry = now
        dev = find_ideapad_input_device()
        if dev is not None:
            self._dev = dev
            log.info("Listening on %s (%s)", dev.path, dev.name)

    def _handle_evdev_event(self, ev: Any) -> None:
        if ev.type == ecodes.EV_MSC and ev.code == ecodes.MSC_SCAN:
            self._last_scancode = ev.value
            return

        if ev.type != ecodes.EV_KEY or ev.value != 1:
            return

        scancode = self._last_scancode
        self._last_scancode = None
        code = ev.code

        if code == ecodes.KEY_REFRESH_RATE_TOGGLE or scancode == 0x110:
            toggle_refresh_rate("eDP-1")
        elif code == ecodes.KEY_FAVORITES or scancode == 0x101:
            launch_fan_gui()
        elif scancode == 0x141 or (code == ecodes.KEY_UNKNOWN and scancode is not None):
            cycle_fan_preset()

    def _poll_sysfs(self) -> None:
        prof = _read_text(SYSFS_PLATFORM_PROFILE)
        if prof is not None and prof != self._last_profile:
            self._last_profile = prof
            osd_power_profile(prof)

        kbd = _read_text(SYSFS_KBD_BACKLIGHT)
        if kbd is not None and kbd != self._last_kbd_bl:
            self._last_kbd_bl = kbd
            try:
                osd_kbd_backlight(int(kbd), self._kbd_bl_max)
            except ValueError:
                pass

        fnl = _read_text(SYSFS_FN_LOCK)
        if fnl is not None and fnl != self._last_fn_lock:
            self._last_fn_lock = fnl
            osd_show_text("input-keyboard", "Fn Lock: ON" if fnl == "1" else "Fn Lock: OFF")
            log.info("OSD Fn Lock: %s", fnl)

        cam = _read_text(SYSFS_CAMERA_POWER)
        if cam is not None and cam != self._last_camera:
            self._last_camera = cam
            osd_show_text("camera-web", "Camera: ON" if cam == "1" else "Camera: OFF")
            log.info("OSD Camera: %s", cam)

    def run(self) -> None:
        def _stop(_sig: int, _frame: Any) -> None:
            self._running = False

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        log.info("Yoga Pro 9 16IMH9 Hotkey & OSD daemon started")
        while self._running:
            self._ensure_device()
            if self._dev is not None:
                try:
                    r, _, _ = select.select([self._dev.fd], [], [], 0.25)
                    if r:
                        for ev in self._dev.read():
                            self._handle_evdev_event(ev)
                except OSError as exc:
                    log.warning("Input device closed (%s), will retry", exc)
                    try:
                        self._dev.close()
                    except OSError:
                        pass
                    self._dev = None
            else:
                time.sleep(0.25)

            self._poll_sysfs()

        if self._dev is not None:
            try:
                self._dev.close()
            except OSError:
                pass


def main() -> int:
    LenovoHotkeyDaemon().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

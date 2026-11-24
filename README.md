# Lenovo Yoga Pro 9 16IMH9 (`83DN`) — Safe Linux Fan Curve & Hotkey OSD

Safe, dynamic dual-fan curve control, native Qt 6 (KDE) GUI, and Lenovo hotkey + OSD popup support for the **Lenovo Yoga Pro 9 16IMH9 (Gen 9, Board `83DN`)** on Linux.

## How It Works (No Raw EC Hacks)

Instead of writing raw Embedded Controller ports directly, this project uses the **upstream Linux kernel driver `lenovo_wmi_other`**, which communicates through Lenovo's own ACPI/WMI firmware method (`\_SB_.WM10.WMAE`, GUID `DC2A8805-3A8C-41BA-A6F7-092E0089CD3B`). The BIOS acquires the EC mutex (`LFCM`) around every fan read/write:

- `0x04030001` / `0x04030002` (`GetFeatureValue`): Reads live Fan 1 / Fan 2 RPM.
- `0x04030001` / `0x04030002` (`SetFeatureValue`): Sets Fan 1 / Fan 2 target RPM in `100 RPM` steps, or restores full **BIOS Automatic Control** when written `0`.

Because the 16IMH9 BIOS (`NKCN35WW`) declares both fan channels in `LENOVO_CAPABILITY_DATA_00` (`WQA9`) but omits `LENOVO_FAN_TEST_DATA`, loading `lenovo_wmi_other` with:

```text
options lenovo_wmi_other expose_all_fans=1 relax_fan_constraint=1
```

cleanly exposes native `hwmon` nodes (`fan1_input`, `fan2_input`, `fan1_target`, `fan2_target`).

## Built-In Fan-Longevity & Safety Rails

- **Anti-Stall & Bearing Protection:** Hard two-layer clamping ensures active manual targets are always `0` (BIOS Auto) or **`2000–4800 RPM`** in exact `100 RPM` steps — strictly forbidding `1..1999` (where `1` commands a fan stop in BIOS firmware and `< 1800 RPM` is the EC stall zone).
- **Smooth Acoustics & Low Wear:** EMA temperature smoothing, configurable downward hysteresis (`3 °C` default), and slew-rate limiting (`+400 / -200 RPM` per tick) prevent rapid fan hunting on brief CPU turbo spikes.
- **GPU-Aware Without Waking dGPU:** Tracks `max(CPU Package, Active RTX GPU)` and checks PCI `runtime_status` first so it never wakes a sleeping D3cold NVIDIA GPU.
- **Plugged-In Only Toggle (`ac_only`):** Enabled by default; detects Mains (`ADP0`) and USB-C PD charging. Unplugging immediately hands control back to BIOS Auto.
- **Ironclad Failsafes:**
  - Forces **5000 RPM** emergency cooling if CPU ≥ `92 °C` or active GPU ≥ `85 °C` while a custom curve is active.
  - Restores **BIOS Auto (`0`)** automatically on daemon stop, crash (`ExecStopPost`), repeated sensor read failures, or system suspend (`system-sleep` hook).

## Features

- **Interactive Qt 6 GUI & System Tray** (`yoga_fancurve/gui.py`):
  - Drag control points directly on the curve (double-click to add, right-click to remove)
  - Live temperature & RPM crosshair overlay + 2-minute history chart
  - Presets: **Balanced-Cool**, **Proactive Cool**, **Quiet**, **BIOS Auto**, and **Custom**
  - One-click **BIOS Auto Now** override & **Only Active When Plugged In** toggle
  - Talks to the root daemon over a local Unix socket (no sudo prompts needed day-to-day)
- **Lenovo Hotkeys & Native KDE Plasma 6 OSD Popups** (`yoga_fancurve/hotkeys.py`):
  - **`Fn+Q`**: Native KDE Power Profile OSD (*Power Saver / Balanced / Performance*)
  - **`Fn+Space`**: Native KDE Keyboard Backlight bar OSD (`0% / 50% / 100%`)
  - **`Fn+Esc`**: `Fn Lock: ON / OFF` OSD
  - **Camera switch**: `Camera: ON / OFF` OSD
  - **`Fn+R`**: Toggles internal `eDP-1` panel between **60 Hz ↔ 165 Hz** via `kscreen-doctor` + OSD
  - **Star / Vantage key**: Opens or focuses the Fan Curve GUI
  - **Smart key (`0x141`)**: Cycles Fan Curve presets + OSD

## Requirements

- **Linux kernel** with `lenovo_wmi_other` fan hwmon support (`expose_all_fans` / `relax_fan_constraint`, e.g. Linux 6.19+ / 7.x; verified on CachyOS `7.3.5`).
- **Python 3.11+** with **`PySide6`** (GUI) and **`evdev`** (hotkeys).
- **KDE Plasma 6** (optional — used for native `org.kde.osdService` popups and `kscreen-doctor` refresh rate switching; the fan curve daemon and GUI work on any desktop environment).

### Installing Dependencies on Common Distros

- **Arch / CachyOS / EndeavourOS:**
  ```bash
  sudo pacman -S --needed python pyside6 python-evdev
  ```
- **Fedora:**
  ```bash
  sudo dnf install python3 python3-pyside6 python3-evdev
  ```
- **Ubuntu / Debian:**
  ```bash
  sudo apt install python3 python3-pyside6.qtwidgets python3-pyside6.qtnetwork python3-evdev
  ```

## Installation

```bash
git clone https://github.com/catpuriel/16IMH9FanCurveFix.git
cd 16IMH9FanCurveFix
sudo bash install.sh
```

Launch the GUI from your application menu (**Yoga Pro 9 Fan Curve**), press your **Star** key, or run:

```bash
./run-gui.sh
```

Both daemons (`yoga-pro-9-fancurve.service` and user `yoga-pro-9-hotkeys.service`) are enabled automatically and persist across reboots.

## Uninstall (Full Revert to BIOS Auto)

```bash
sudo bash uninstall.sh
```

## Running Tests

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

## Disclaimer

This tool was specifically verified against **Lenovo Yoga Pro 9 16IMH9 (`83DN`, BIOS `NKCN35WW`)**. While it relies exclusively on Lenovo's firmware-locked WMI interface and includes strict RPM bounds and failsafes, always verify your model's ACPI/WMI fan support before using on other hardware.

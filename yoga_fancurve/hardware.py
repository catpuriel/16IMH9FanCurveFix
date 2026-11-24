"""Safe hardware discovery and sysfs/WMI telemetry for Lenovo Yoga Pro 9 16IMH9."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import HARD_EMERGENCY_RPM, HARD_MIN_MANUAL_RPM, round_rpm

log = logging.getLogger("yoga_fancurve.hardware")

HWMON_ROOT = Path("/sys/class/hwmon")
POWER_SUPPLY_ROOT = Path("/sys/class/power_supply")
PCI_ROOT = Path("/sys/bus/pci/devices")


@dataclass
class SensorSnapshot:
    cpu_temp_c: float | None
    gpu_temp_c: float | None
    gpu_state: str  # "active", "suspended", "unavailable"
    fan1_rpm: int | None
    fan2_rpm: int | None
    fan1_target_sysfs: int | None
    fan2_target_sysfs: int | None
    ac_online: bool
    ac_source: str  # e.g. "ADP0", "USB-C", "Battery"
    platform_profile: str


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    txt = _read_text(path)
    if txt is None:
        return None
    try:
        return int(txt)
    except ValueError:
        return None


class YogaHardware:
    """Interfaces with upstream lenovo_wmi_other hwmon, coretemp, GPU PM, and AC status."""

    def __init__(self) -> None:
        self._lenovo_hwmon: Path | None = None
        self._coretemp_path: Path | None = None
        self._nvidia_pci_dir: Path | None = None
        self._refresh_paths()

    def _refresh_paths(self) -> None:
        self._lenovo_hwmon = self._find_hwmon("lenovo_wmi_other")
        self._coretemp_path = self._find_coretemp_input()
        self._nvidia_pci_dir = self._find_nvidia_pci()

    @staticmethod
    def _find_hwmon(target_name: str) -> Path | None:
        if not HWMON_ROOT.exists():
            return None
        for d in sorted(HWMON_ROOT.iterdir()):
            if _read_text(d / "name") == target_name:
                return d
        return None

    def _find_coretemp_input(self) -> Path | None:
        hw = self._find_hwmon("coretemp")
        if hw is None:
            return None
        # Prefer "Package id 0"
        for label_file in sorted(hw.glob("temp*_label")):
            label = _read_text(label_file) or ""
            if "Package id 0" in label:
                inp = hw / label_file.name.replace("_label", "_input")
                if inp.exists():
                    return inp
        # Fallback to temp1_input
        fallback = hw / "temp1_input"
        return fallback if fallback.exists() else None

    @staticmethod
    def _find_nvidia_pci() -> Path | None:
        if not PCI_ROOT.exists():
            return None
        for d in sorted(PCI_ROOT.iterdir()):
            vendor = _read_text(d / "vendor")
            cls = _read_text(d / "class") or ""
            if vendor == "0x10de" and cls.startswith("0x03"):
                return d
        return None

    @property
    def lenovo_hwmon(self) -> Path | None:
        if self._lenovo_hwmon is None or not (self._lenovo_hwmon / "fan1_input").exists():
            self._lenovo_hwmon = self._find_hwmon("lenovo_wmi_other")
        return self._lenovo_hwmon

    def is_fan_interface_ready(self) -> bool:
        hw = self.lenovo_hwmon
        return hw is not None and (hw / "fan1_target").exists() and (hw / "fan2_target").exists()

    def read_cpu_temp(self) -> float | None:
        if self._coretemp_path is None or not self._coretemp_path.exists():
            self._coretemp_path = self._find_coretemp_input()
        if self._coretemp_path is not None:
            mc = _read_int(self._coretemp_path)
            if mc is not None and 0 < mc < 130000:
                return round(mc / 1000.0, 1)

        # Fallback: x86_pkg_temp thermal zone
        for tz in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
            if _read_text(tz / "type") == "x86_pkg_temp":
                mc = _read_int(tz / "temp")
                if mc is not None and 0 < mc < 130000:
                    return round(mc / 1000.0, 1)
        return None

    def read_gpu_temp(self) -> tuple[float | None, str]:
        """Read RTX GPU temp ONLY when PCI device is active (never wakes D3cold GPU)."""
        if self._nvidia_pci_dir is None or not self._nvidia_pci_dir.exists():
            self._nvidia_pci_dir = self._find_nvidia_pci()
        if self._nvidia_pci_dir is None:
            return None, "unavailable"

        rt_status = _read_text(self._nvidia_pci_dir / "power" / "runtime_status")
        if rt_status and rt_status != "active":
            return None, "suspended"

        try:
            res = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                val = float(res.stdout.strip().splitlines()[0].strip())
                if 0.0 < val < 120.0:
                    return round(val, 1), "active"
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return None, "unavailable"

    @staticmethod
    def read_ac_status() -> tuple[bool, str]:
        """Detect Mains (ADP0) or USB-C PD charger plugged in."""
        if not POWER_SUPPLY_ROOT.exists():
            return False, "Unknown"
        for ps in sorted(POWER_SUPPLY_ROOT.iterdir()):
            ptype = _read_text(ps / "type")
            online = _read_int(ps / "online")
            if ptype == "Mains" and online == 1:
                return True, ps.name
        for ps in sorted(POWER_SUPPLY_ROOT.iterdir()):
            ptype = _read_text(ps / "type")
            online = _read_int(ps / "online")
            if ptype == "USB" and online == 1:
                return True, "USB-C"
        return False, "Battery"

    @staticmethod
    def read_platform_profile() -> str:
        return _read_text(Path("/sys/firmware/acpi/platform_profile")) or "unknown"

    def read_snapshot(self, query_gpu: bool = True) -> SensorSnapshot:
        cpu = self.read_cpu_temp()
        if query_gpu:
            gpu, gpu_state = self.read_gpu_temp()
        else:
            gpu, gpu_state = None, "disabled"

        hw = self.lenovo_hwmon
        f1 = _read_int(hw / "fan1_input") if hw else None
        f2 = _read_int(hw / "fan2_input") if hw else None
        t1 = _read_int(hw / "fan1_target") if hw else None
        t2 = _read_int(hw / "fan2_target") if hw else None
        ac_online, ac_source = self.read_ac_status()
        profile = self.read_platform_profile()

        return SensorSnapshot(
            cpu_temp_c=cpu,
            gpu_temp_c=gpu,
            gpu_state=gpu_state,
            fan1_rpm=f1,
            fan2_rpm=f2,
            fan1_target_sysfs=t1,
            fan2_target_sysfs=t2,
            ac_online=ac_online,
            ac_source=ac_source,
            platform_profile=profile,
        )

    @staticmethod
    def _sanitize_hw_rpm(rpm: int) -> int:
        """Final hardware-layer guard: ONLY 0 (BIOS Auto) or [2000..5000] in 100 RPM steps.
        Never permits writing 1..1999 (which BIOS WMAE treats as fan-stop or EC stall zone).
        """
        if rpm <= 0:
            return 0
        r = round_rpm(rpm)
        return max(HARD_MIN_MANUAL_RPM, min(HARD_EMERGENCY_RPM, r))

    def set_fan_targets(self, fan1_rpm: int, fan2_rpm: int) -> bool:
        """Write safe target RPMs (or 0 for BIOS Auto) via lenovo_wmi_other hwmon."""
        hw = self.lenovo_hwmon
        if hw is None:
            log.error("lenovo_wmi_other hwmon device not found")
            return False

        safe1 = self._sanitize_hw_rpm(fan1_rpm)
        safe2 = self._sanitize_hw_rpm(fan2_rpm)

        try:
            if safe1 == 0 or safe2 == 0:
                # In BIOS NKCN35WW DSDT, writing 0 to either fan invokes MBEY(0xEF, 0x63, 0x03)
                # which restores BIOS automatic control for both fans.
                (hw / "fan1_target").write_text("0\n", encoding="utf-8")
                (hw / "fan2_target").write_text("0\n", encoding="utf-8")
                return True

            (hw / "fan1_target").write_text(f"{safe1}\n", encoding="utf-8")
            (hw / "fan2_target").write_text(f"{safe2}\n", encoding="utf-8")
            return True
        except OSError as exc:
            log.error("Failed writing fan targets (%s, %s): %s", safe1, safe2, exc)
            return False

    def restore_bios_auto(self) -> bool:
        """Immediately return both fans to BIOS automatic control."""
        return self.set_fan_targets(0, 0)

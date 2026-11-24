#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_USER="${SUDO_USER:-$USER}"
TARGET_HOME="$(eval echo "~${TARGET_USER}")"
TARGET_UID="$(id -u "${TARGET_USER}")"

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo: sudo bash $0"
  exit 1
fi

echo "==> Stopping and disabling yoga-pro-9-hotkeys.service (user)..."
sudo -u "${TARGET_USER}" \
  XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${TARGET_UID}/bus" \
  systemctl --user disable --now yoga-pro-9-hotkeys.service 2>/dev/null || true
rm -f "${TARGET_HOME}/.config/systemd/user/yoga-pro-9-hotkeys.service"
rm -f /etc/udev/rules.d/70-ideapad-hotkeys.rules /etc/udev/rules.d/90-ideapad-hotkeys.rules
udevadm control --reload-rules 2>/dev/null || true

echo "==> Stopping and disabling yoga-pro-9-fancurve.service..."
systemctl disable --now yoga-pro-9-fancurve.service 2>/dev/null || true

echo "==> Restoring BIOS automatic fan control..."
PYTHONPATH="${REPO_DIR}" /usr/bin/python3 -m yoga_fancurve.daemon --restore-auto-only || true

echo "==> Removing installed unit, sleep hook, modprobe config, and launcher..."
rm -f /etc/systemd/system/yoga-pro-9-fancurve.service
rm -f /usr/lib/systemd/system-sleep/yoga-pro-9-fancurve
rm -f /etc/modprobe.d/lenovo-yoga-pro-9-fans.conf
rm -f "${TARGET_HOME}/.local/share/applications/yoga-pro-9-fancurve.desktop"
rm -f "${TARGET_HOME}/.config/autostart/yoga-pro-9-fancurve.desktop"
systemctl daemon-reload

echo "Uninstalled cleanly and restored BIOS automatic fan control."

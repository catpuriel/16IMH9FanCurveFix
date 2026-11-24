#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_USER="${SUDO_USER:-$USER}"
TARGET_HOME="$(eval echo "~${TARGET_USER}")"

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo: sudo bash $0"
  exit 1
fi

echo "==> Installing modprobe configuration for lenovo_wmi_other..."
install -Dm644 "${REPO_DIR}/systemd/lenovo-yoga-pro-9-fans.conf" \
  /etc/modprobe.d/lenovo-yoga-pro-9-fans.conf

# Reload lenovo_wmi_other only if fan attributes are not already exposed
if ! grep -ql '^lenovo_wmi_other$' /sys/class/hwmon/hwmon*/name 2>/dev/null; then
  echo "==> Reloading lenovo_wmi_other with expose_all_fans=1 relax_fan_constraint=1..."
  modprobe -r lenovo_wmi_other 2>/dev/null || true
  modprobe lenovo_wmi_other expose_all_fans=1 relax_fan_constraint=1
fi

echo "==> Installing systemd service and suspend/resume safety hook..."
sed "s|@TARGET_USER@|${TARGET_USER}|g; s|@REPO_DIR@|${REPO_DIR}|g" \
  "${REPO_DIR}/systemd/yoga-pro-9-fancurve.service" \
  > /etc/systemd/system/yoga-pro-9-fancurve.service
chmod 644 /etc/systemd/system/yoga-pro-9-fancurve.service

sed "s|@REPO_DIR@|${REPO_DIR}|g" \
  "${REPO_DIR}/systemd/yoga-pro-9-fancurve-sleep" \
  > /usr/lib/systemd/system-sleep/yoga-pro-9-fancurve
chmod 755 /usr/lib/systemd/system-sleep/yoga-pro-9-fancurve

echo "==> Enabling and starting yoga-pro-9-fancurve.service..."
systemctl daemon-reload
systemctl enable --now yoga-pro-9-fancurve.service

echo "==> Installing KDE application launcher for ${TARGET_USER}..."
APP_DIR="${TARGET_HOME}/.local/share/applications"
mkdir -p "${APP_DIR}"
sed "s|@REPO_DIR@|${REPO_DIR}|g" \
  "${REPO_DIR}/systemd/yoga-pro-9-fancurve.desktop" \
  > "${APP_DIR}/yoga-pro-9-fancurve.desktop"
chown -R "${TARGET_USER}:${TARGET_USER}" "${APP_DIR}/yoga-pro-9-fancurve.desktop"

bash "${REPO_DIR}/install-hotkeys.sh"

echo ""
echo "Installation complete!"
systemctl status yoga-pro-9-fancurve.service --no-pager -l | head -n 16
echo ""
echo "You can now launch 'Yoga Pro 9 Fan Curve' from your KDE menu, or run:"
echo "  ${REPO_DIR}/run-gui.sh"

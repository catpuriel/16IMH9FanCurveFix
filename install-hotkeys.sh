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

echo "==> Installing scoped udev rule for 'Ideapad extra buttons'..."
rm -f /etc/udev/rules.d/90-ideapad-hotkeys.rules
install -Dm644 "${REPO_DIR}/systemd/70-ideapad-hotkeys.rules" \
  /etc/udev/rules.d/70-ideapad-hotkeys.rules
udevadm control --reload-rules
udevadm trigger --action=change --subsystem-match=input
udevadm settle

echo "==> Installing user systemd service yoga-pro-9-hotkeys.service for ${TARGET_USER}..."
USER_UNIT_DIR="${TARGET_HOME}/.config/systemd/user"
mkdir -p "${USER_UNIT_DIR}"
sed "s|@REPO_DIR@|${REPO_DIR}|g" \
  "${REPO_DIR}/systemd/yoga-pro-9-hotkeys.service" \
  > "${USER_UNIT_DIR}/yoga-pro-9-hotkeys.service"
chown -R "${TARGET_USER}:${TARGET_USER}" "${TARGET_HOME}/.config/systemd"

echo "==> Enabling and starting yoga-pro-9-hotkeys.service in ${TARGET_USER}'s KDE session..."
sudo -u "${TARGET_USER}" \
  XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${TARGET_UID}/bus" \
  systemctl --user daemon-reload

sudo -u "${TARGET_USER}" \
  XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${TARGET_UID}/bus" \
  systemctl --user enable yoga-pro-9-hotkeys.service

sudo -u "${TARGET_USER}" \
  XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${TARGET_UID}/bus" \
  systemctl --user restart yoga-pro-9-hotkeys.service

echo ""
echo "Hotkeys & KDE OSD installed and running!"
sudo -u "${TARGET_USER}" \
  XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${TARGET_UID}/bus" \
  systemctl --user status yoga-pro-9-hotkeys.service --no-pager -l | head -n 16

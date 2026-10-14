#!/usr/bin/env bash
set -euo pipefail

IFACE="awg0"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this with sudo." >&2
  exit 1
fi

echo "[+] Stopping services..."
systemctl disable --now awg-panel 2>/dev/null || true
systemctl disable --now "awg-quick@${IFACE}" 2>/dev/null || true

echo "[+] Removing files..."
rm -f /etc/systemd/system/awg-panel.service
rm -rf /opt/awg-panel
rm -rf /etc/awg-panel
rm -f "/etc/amnezia/amneziawg/${IFACE}.conf"
rm -f /etc/sysctl.d/99-awg-panel.conf

systemctl daemon-reload

ans=""
if [ -r /dev/tty ]; then
  read -rp "Also remove the amneziawg packages? (y/N): " ans < /dev/tty || ans=""
fi
if [[ "$ans" =~ ^[Yy]$ ]]; then
  apt-get remove -y amneziawg amneziawg-tools || true
fi

echo "[+] Done."

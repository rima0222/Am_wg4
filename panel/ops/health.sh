#!/usr/bin/env bash
# AWG panel watchdog - run every minute by awg-panel-health.timer.
# Keeps the panel reachable without ever touching the VPN tunnel or its users.
ENV_FILE=/etc/awg-panel/panel.env
FAILFILE=/run/awg-panel-health.fail
PORT="$(grep -E '^PANEL_PORT=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"; PORT="${PORT:-8787}"
IFACE="$(grep -E '^AWG_INTERFACE=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"; IFACE="${IFACE:-awg0}"

# 1. panel answers locally? two failures in a row -> restart the PANEL only
if curl -sk --max-time 10 -f -o /dev/null "https://127.0.0.1:${PORT}/api/health"; then
  rm -f "$FAILFILE"
else
  n=$(( $(cat "$FAILFILE" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$FAILFILE"
  if [ "$n" -ge 2 ]; then
    logger -t awg-panel-health "panel not answering on :${PORT} - restarting awg-panel"
    systemctl restart awg-panel
    rm -f "$FAILFILE"
  fi
fi

# 2. tunnel interface missing (e.g. crashed)? start it (a no-op while it is up)
ip link show "$IFACE" >/dev/null 2>&1 || systemctl start "awg-quick@${IFACE}"

# 3. fair-queue on the tunnel disappears whenever the interface restarts - restore it
"$(dirname "$0")/qos.sh" ensure >/dev/null 2>&1 || true

# 4. firewall rule for the panel port can silently disappear (ufw reset/reinstall)
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw status | grep -qE "^${PORT}/tcp +ALLOW" || ufw allow "${PORT}/tcp" >/dev/null 2>&1
  # panel also reachable through the VPN itself (https://<server tunnel IP>:PORT) as a fallback
  ufw status | grep -q "${PORT}/tcp on ${IFACE}" || ufw allow in on "${IFACE}" to any port "${PORT}" proto tcp >/dev/null 2>&1
fi
exit 0

#!/usr/bin/env bash
#
# AmneziaWG Panel - repair after a re-run of install.sh
#
# Symptom this fixes: users created AFTER a re-install get a config that never
# connects, while older users keep working. Cause: the old installer generated
# a fresh server key / port / obfuscation parameters on every run, rewrote the
# files on disk, but left the already-running tunnel on the OLD values. New
# configs were built from the new (wrong) values.
#
# This script reads the values the tunnel is ACTUALLY running with, and makes
# panel.env + the tunnel .conf match them. It does not restart the tunnel, so
# connected users are not dropped.
#
# Usage:  curl -fsSL https://raw.githubusercontent.com/rima0222/Am_wg3/main/repair.sh | sudo bash
#
set -euo pipefail

ETC_DIR="/etc/awg-panel"
ENV_FILE="${ETC_DIR}/panel.env"
IFACE="awg0"
CONF="/etc/amnezia/amneziawg/${IFACE}.conf"

log()  { echo "[+] $*"; }
warn() { echo "[!] $*"; }
err()  { echo "[x] $*" >&2; }

[ "$(id -u)" -eq 0 ] || { err "Run as root (sudo bash repair.sh)"; exit 1; }
[ -f "$ENV_FILE" ] || { err "${ENV_FILE} not found - is the panel installed?"; exit 1; }
[ -f "$CONF" ]     || { err "${CONF} not found."; exit 1; }
command -v awg >/dev/null 2>&1 || { err "'awg' not found."; exit 1; }

envget() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }

# ---- 1. read what the RUNNING tunnel actually uses ----
if ! awg show "$IFACE" >/dev/null 2>&1; then
  err "Interface ${IFACE} is not up, so there are no live values to recover."
  err "Start it first:  systemctl start awg-quick@${IFACE}"
  exit 1
fi

LIVE_PRIV="$(awg show "$IFACE" private-key)"
LIVE_PORT="$(awg show "$IFACE" listen-port)"
LIVE_PUB="$(echo "$LIVE_PRIV" | awg pubkey)"

SHOWCONF="$(awg showconf "$IFACE" 2>/dev/null || true)"
SHOW="$(awg show "$IFACE" 2>/dev/null || true)"

# obfuscation params: try `showconf` ("Jc = 4"), fall back to `show` ("jc: 4")
live_param() {
  local key="$1" lower val
  lower="$(echo "$key" | tr 'A-Z' 'a-z')"
  val="$(echo "$SHOWCONF" | sed -n "s/^${key}[[:space:]]*=[[:space:]]*//p" | head -1)"
  if [ -z "$val" ]; then
    val="$(echo "$SHOW" | sed -n "s/^[[:space:]]*${lower}:[[:space:]]*//p" | head -1)"
  fi
  echo "$val"
}

LIVE_JC="$(live_param Jc)";     LIVE_JMIN="$(live_param Jmin)"; LIVE_JMAX="$(live_param Jmax)"
LIVE_S1="$(live_param S1)";     LIVE_S2="$(live_param S2)"
LIVE_H1="$(live_param H1)";     LIVE_H2="$(live_param H2)"
LIVE_H3="$(live_param H3)";     LIVE_H4="$(live_param H4)"

for v in LIVE_PRIV LIVE_PORT LIVE_JC LIVE_JMIN LIVE_JMAX LIVE_S1 LIVE_S2 LIVE_H1 LIVE_H2 LIVE_H3 LIVE_H4; do
  if [ -z "${!v}" ]; then
    err "Could not read ${v} from the running tunnel - aborting without changing anything."
    err "Please send the output of:  awg show ${IFACE}"
    exit 1
  fi
done

# --if-needed: used by the installer's upgrade mode. Do nothing unless panel.env
# actually disagrees with the running tunnel.
if [ "${1:-}" = "--if-needed" ]; then
  if [ "$(envget AWG_PORT)" = "$LIVE_PORT" ] && [ "$(envget AWG_SERVER_PUBLIC_KEY)" = "$LIVE_PUB" ]; then
    exit 0
  fi
  warn "panel.env disagrees with the running tunnel - repairing automatically."
fi

echo ""
echo "Running tunnel values (these are what existing clients use):"
echo "  port ${LIVE_PORT}   public key ${LIVE_PUB}"
echo "  Jc=${LIVE_JC} Jmin=${LIVE_JMIN} Jmax=${LIVE_JMAX} S1=${LIVE_S1} S2=${LIVE_S2}"
echo "  H1=${LIVE_H1} H2=${LIVE_H2} H3=${LIVE_H3} H4=${LIVE_H4}"
echo ""
echo "panel.env currently says:"
echo "  port $(envget AWG_PORT)   public key $(envget AWG_SERVER_PUBLIC_KEY)"
echo ""

# ---- 2. back everything up ----
STAMP="$(date +%Y%m%d-%H%M%S)"
cp -a "$ENV_FILE" "${ENV_FILE}.bak-${STAMP}"
cp -a "$CONF"     "${CONF}.bak-${STAMP}"
log "Backups: ${ENV_FILE}.bak-${STAMP}  ${CONF}.bak-${STAMP}"

# ---- 3. rewrite the tunnel .conf: live values + peers rebuilt from the panel DB ----
DB_PATH="$(envget PANEL_DB_PATH)"; DB_PATH="${DB_PATH:-${ETC_DIR}/panel.db}"
# awg-quick/wg-quick only accept a path whose file name is "<iface>.conf"
# (<=15 chars before .conf), so build the candidate inside a temp dir under that name
TMP_DIR="$(mktemp -d)"
NEW_CONF="${TMP_DIR}/${IFACE}.conf"

LIVE_PRIV="$LIVE_PRIV" LIVE_PORT="$LIVE_PORT" \
LIVE_JC="$LIVE_JC" LIVE_JMIN="$LIVE_JMIN" LIVE_JMAX="$LIVE_JMAX" \
LIVE_S1="$LIVE_S1" LIVE_S2="$LIVE_S2" \
LIVE_H1="$LIVE_H1" LIVE_H2="$LIVE_H2" LIVE_H3="$LIVE_H3" LIVE_H4="$LIVE_H4" \
CONF="$CONF" DB_PATH="$DB_PATH" NEW_CONF="$NEW_CONF" \
python3 - <<'PYEOF'
import os, re, sqlite3, time

conf = open(os.environ["CONF"]).read()
header = conf.split("\n[Peer]\n")[0]

live = {
    "PrivateKey": os.environ["LIVE_PRIV"],
    "ListenPort": os.environ["LIVE_PORT"],
    "Jc": os.environ["LIVE_JC"], "Jmin": os.environ["LIVE_JMIN"], "Jmax": os.environ["LIVE_JMAX"],
    "S1": os.environ["LIVE_S1"], "S2": os.environ["LIVE_S2"],
    "H1": os.environ["LIVE_H1"], "H2": os.environ["LIVE_H2"],
    "H3": os.environ["LIVE_H3"], "H4": os.environ["LIVE_H4"],
}
for key, val in live.items():
    pattern = re.compile(rf"^{key}\s*=.*$", re.MULTILINE)
    line = f"{key} = {val}"
    if pattern.search(header):
        header = pattern.sub(lambda m: line, header, count=1)
    else:
        header = header.replace("[Interface]", f"[Interface]\n{line}", 1)

out = header.rstrip("\n") + "\n"

db = sqlite3.connect(os.environ["DB_PATH"])
db.row_factory = sqlite3.Row
now = int(time.time())
count = 0
for p in db.execute("SELECT * FROM peers WHERE enabled=1 ORDER BY id"):
    if p["expires_at"] and now > p["expires_at"]:
        continue
    allowed = f"{p['ip_address']}/32"
    if p["ipv6_address"]:
        allowed += f", {p['ipv6_address']}/128"
    out += (
        f"\n# peer: {p['name']}\n[Peer]\n"
        f"PublicKey = {p['public_key']}\n"
        f"PresharedKey = {p['preshared_key']}\n"
        f"AllowedIPs = {allowed}\n"
    )
    count += 1

open(os.environ["NEW_CONF"], "w").write(out)
print(f"[+] Rebuilt tunnel config with {count} enabled user(s) from the panel database")
PYEOF

# validate before touching the real file
if ! STRIP_ERR="$(awg-quick strip "$NEW_CONF" 2>&1 >/dev/null)"; then
  err "Generated config failed validation - nothing was changed. (Backups are untouched.)"
  err "awg-quick said: ${STRIP_ERR}"
  rm -rf "$TMP_DIR"
  exit 1
fi
install -m 600 "$NEW_CONF" "$CONF"
rm -rf "$TMP_DIR"
log "Tunnel config rewritten: ${CONF}"

# ---- 4. fix panel.env ----
set_env() {
  local key="$1" val="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    echo "${key}=${val}" >> "$ENV_FILE"
  fi
}
set_env AWG_PORT              "$LIVE_PORT"
set_env AWG_SERVER_PUBLIC_KEY "$LIVE_PUB"
set_env AWG_JC   "$LIVE_JC";   set_env AWG_JMIN "$LIVE_JMIN"; set_env AWG_JMAX "$LIVE_JMAX"
set_env AWG_S1   "$LIVE_S1";   set_env AWG_S2   "$LIVE_S2"
set_env AWG_H1   "$LIVE_H1";   set_env AWG_H2   "$LIVE_H2"
set_env AWG_H3   "$LIVE_H3";   set_env AWG_H4   "$LIVE_H4"
chmod 600 "$ENV_FILE"
log "panel.env updated to match the running tunnel"

# ---- 5. firewall: make sure the REAL port is open ----
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow "${LIVE_PORT}/udp" >/dev/null && log "ufw: allowed ${LIVE_PORT}/udp"
fi

# ---- 6. reload the panel (NOT the tunnel - connected users stay connected) ----
systemctl restart awg-panel
sleep 3

LIVE_PEERS="$(awg show "$IFACE" peers | wc -l)"
echo ""
echo "=================================================================="
echo "Repair complete."
echo "=================================================================="
echo "Users on the live tunnel: ${LIVE_PEERS}"
echo "Server port: ${LIVE_PORT}   (panel + tunnel now agree)"
echo ""
echo "IMPORTANT: any user created since the re-install still holds a config"
echo "with the wrong server key/port. In the panel, open that user's Config"
echo "and send them the fresh one (it is now generated with the right values)."
echo "Users created before the re-install need nothing."

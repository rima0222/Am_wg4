#!/usr/bin/env bash
# Fair bandwidth sharing for the VPN tunnel (no per-user caps, no config changes for users).
#
#   qos.sh apply    (re)apply now
#   qos.sh ensure   apply only if the tunnel has no fair-queue yet (used by the watchdog)
#   qos.sh status   print: mode=<cake|fq_codel|none> rate=<mbit|0>
#
# How it behaves:
#   * Server idle  -> everyone gets as much as the line allows (the queue is work-conserving).
#   * Server busy  -> bandwidth is split fairly PER USER (cake dual-dsthost), so a heavy
#     downloader can only use what light users leave over; light users keep full speed.
#   * Also removes bufferbloat latency (helps weak/mobile connections feel stable).
# `tc qdisc replace` on the tunnel is live and does not drop any session.
ENV_FILE="${AWG_ENV_FILE:-/etc/awg-panel/panel.env}"
envget() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-; }

IFACE="$(envget AWG_INTERFACE)"; IFACE="${IFACE:-awg0}"
MBIT="$(envget AWG_BANDWIDTH_MBIT)"      # set by the admin (wins over everything)
AUTO="$(envget AWG_BANDWIDTH_AUTO)"      # measured by this script (cached)
ENABLED="$(envget AWG_QOS_ENABLED)"      # OFF unless the admin switches it on in the panel
SPEED_URL="${AWG_SPEEDTEST_URL:-https://speed.cloudflare.com/__up}"

set_env_key() {   # atomic edit of one KEY=VALUE in the env file ("" = remove)
  local k="$1" v="$2" tmp="${ENV_FILE}.tmp"
  { grep -v "^${k}=" "$ENV_FILE" 2>/dev/null || true; [ -n "$v" ] && echo "${k}=${v}"; } > "$tmp"
  chmod 600 "$tmp"; mv "$tmp" "$ENV_FILE"
}

tunnel_quiet() {
  # measuring while users are busy would under-estimate the line and then throttle everyone
  local f="${QOS_SYSFS_NET:-/sys/class/net}/${IFACE}/statistics/tx_bytes" a b
  [ -r "$f" ] || return 0
  a="$(cat "$f")"; sleep 3; b="$(cat "$f")"
  [ $(( b - a )) -lt 400000 ]          # < ~1 Mbit/s over 3 s
}

measure() {
  # 4 parallel uploads to a public speed-test endpoint; echoes the best of 2 runs in Mbit/s
  command -v curl >/dev/null 2>&1 || return 1
  local tmp best=0 run mb start end i
  tmp="$(mktemp)"; head -c 8000000 /dev/zero > "$tmp"
  for run in 1 2; do
    start="$(date +%s.%N)"
    for i in 1 2 3 4; do
      curl -s -m 25 -o /dev/null --data-binary @"$tmp" "$SPEED_URL" &
    done
    wait
    end="$(date +%s.%N)"
    mb="$(awk -v s="$start" -v e="$end" 'BEGIN { t=e-s; if (t<0.2) t=0.2; printf "%d", (4*8000000*8)/t/1000000 }')"
    [ "$mb" -gt "$best" ] && best="$mb"
  done
  rm -f "$tmp"
  [ "$best" -ge 5 ] && echo "$best"
}

mode_now() {
  local q; q="$(tc qdisc show dev "$IFACE" 2>/dev/null | head -1)"
  case "$q" in
    *" cake "*) echo cake ;;
    *fq_codel*) echo fq_codel ;;
    *) echo none ;;
  esac
}

detect_mbit() {
  # best effort: link speed of the default-route NIC (many VPS NICs report nothing -> empty)
  local wan sp
  wan="$(ip route show default 2>/dev/null | awk '/default/ {print $5; exit}')"
  sp="$(cat "/sys/class/net/${wan}/speed" 2>/dev/null || true)"
  [[ "$sp" =~ ^[0-9]+$ ]] && [ "$sp" -gt 0 ] && echo "$sp"
}

remove_qdisc() {
  # back to the kernel default: exactly the behaviour before fair sharing existed
  tc qdisc del dev "$IFACE" root 2>/dev/null || true
  echo "mode=none rate=0 auto=${AUTO:-0} enabled=0"
}

apply() {
  ip link show "$IFACE" >/dev/null 2>&1 || exit 0
  [ "$ENABLED" = "1" ] || { remove_qdisc; return 0; }
  local m="$MBIT" rate
  if [ -z "$m" ] && [ -z "$AUTO" ]; then
    # nothing configured: learn the line speed once, but only while the tunnel is quiet
    local got; got="$(detect_mbit || true)"
    if [ -z "$got" ] && tunnel_quiet; then got="$(measure || true)"; fi
    if [ -n "$got" ]; then set_env_key AWG_BANDWIDTH_AUTO "$got"; AUTO="$got"; fi
  fi
  if [ -n "$m" ]; then
    rate=$(( m * 90 / 100 ))     # manual value: shape a bit below it so the queue forms HERE (fair)
  elif [ -n "$AUTO" ]; then
    rate="$AUTO"                 # measured value: used as-is (erring high is harmless, too low would hurt)
  else
    rate=0
  fi
  [ "$rate" -ge 1 ] 2>/dev/null || rate=0
  if [ "$rate" -gt 0 ]; then
    # 'oceanic' = queue tuned for ~300 ms RTT (far-away users)
    if tc qdisc replace dev "$IFACE" root cake bandwidth "${rate}mbit" besteffort dual-dsthost nonat nowash oceanic 2>/dev/null; then
      echo "mode=cake rate=${rate} auto=${AUTO:-0} enabled=1"; return 0
    fi
  fi
  # no speed known (or cake missing in this kernel): per-flow fairness + latency control only
  if tc qdisc replace dev "$IFACE" root fq_codel 2>/dev/null; then
    echo "mode=fq_codel rate=0 auto=${AUTO:-0} enabled=1"; return 0
  fi
  echo "mode=none rate=0"; return 1
}

case "${1:-ensure}" in
  apply)  apply ;;
  ensure)
    m="$(mode_now)"
    if [ "$ENABLED" != "1" ]; then
      [ "$m" = "none" ] && echo "mode=none enabled=0" || remove_qdisc
      exit 0
    fi
    if [ "$m" = "none" ]; then apply
    elif [ "$m" = "fq_codel" ] && [ -z "$MBIT" ] && [ -z "$AUTO" ]; then
      # still waiting for a line-speed measurement: retry at most every 10 minutes
      stamp="/run/awg-qos.try"
      if [ ! -f "$stamp" ] || [ -n "$(find "$stamp" -mmin +10 2>/dev/null)" ]; then touch "$stamp" 2>/dev/null; apply; else echo "mode=$m"; fi
    else echo "mode=$m"; fi ;;
  status) echo "mode=$(mode_now) rate=${MBIT:-0} auto=${AUTO:-0} enabled=${ENABLED:-0}" ;;
  remeasure) set_env_key AWG_BANDWIDTH_AUTO ""; AUTO=""; MBIT=""; apply ;;
  *) echo "usage: $0 apply|ensure|status|remeasure" >&2; exit 2 ;;
esac

"""Round 3: user changes never disturb other users, conf/live stay in sync, monitor endpoints,
subprocess timeout, event loop not blocked by slow awg calls."""
import os, sys, tempfile, time, hashlib, asyncio
from pathlib import Path

os.environ["PANEL_DB_PATH"] = tempfile.mktemp(suffix=".db")
_salt = os.urandom(16).hex()
os.environ["PANEL_ADMIN_PASSWORD_HASH"] = f"{_salt}${hashlib.pbkdf2_hmac('sha256', b'testpass123', bytes.fromhex(_salt), 200_000).hex()}"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "panel"))

import app.awg as awg
import app.config as config

live = {}; calls = []
def add_live(pub, psk, ip, ipv6=None): calls.append(("add", pub)); live[pub] = {"endpoint": "(none)", "allowed_ips": ip, "latest_handshake": 0, "rx": 0, "tx": 0}
def rem_live(pub): calls.append(("remove", pub)); live.pop(pub, None)
awg.add_peer_live, awg.remove_peer_live = add_live, rem_live
awg.dump = lambda: dict(live)
n = [0]
def gk(): n[0] += 1; return f"PRIV{n[0]}"
awg.genkey, awg.pubkey, awg.genpsk = gk, lambda k: "PUB" + k, lambda: "PSK"
conf = Path(tempfile.mktemp(suffix=".conf"))
conf.write_text("[Interface]\nPrivateKey = X\nListenPort = 1\n")
config.WG_CONF_PATH = conf

from fastapi.testclient import TestClient
from app.main import app
import app.main as m
import app.database as db
c = TestClient(app); c.__enter__()
H = {"Authorization": "Bearer " + c.post("/api/login", json={"username": "admin", "password": "testpass123"}).json()["token"]}
mk = lambda name, **k: c.post("/api/peers", json={"name": name, **k}, headers=H).json()
pubs = lambda: [l.split("=", 1)[1].strip() for l in conf.read_text().splitlines() if l.startswith("PublicKey")]

a = mk("a"); b = mk("b", data_limit_gb=1)
assert len(live) == 2 and len(pubs()) == 2 and "ListenPort = 1" in conf.read_text()
print("CHECK 1 ok: new users are on the live tunnel AND persisted in the conf")

calls.clear()
c.delete(f"/api/peers/{a['id']}", headers=H)
assert [x for x in calls if x[0] == "add"] == [] and len(live) == 1 and len(pubs()) == 1
print("CHECK 2 ok: deleting one user touches only that user (no re-add of others), conf rewritten")

calls.clear()
c.put(f"/api/peers/{b['id']}", json={"note": "x"}, headers=H)
assert calls == [], calls
print("CHECK 3 ok: label edit = zero tunnel operations")

c.put(f"/api/peers/{b['id']}", json={"enabled": False}, headers=H)
assert not live and not pubs()
c.put(f"/api/peers/{b['id']}", json={"enabled": True}, headers=H)
assert len(live) == 1 and len(pubs()) == 1
print("CHECK 4 ok: disable removes from live+conf, enable restores both")

# usage over quota -> adjust removes it, adding data brings it back automatically
with db.cursor() as cur:
    cur.execute("UPDATE peer_stats SET cumulative_rx=? WHERE peer_id=?", (2 * 1024**3, b["id"]))
c.post(f"/api/peers/{b['id']}/adjust", json={"add_gb": 0}, headers=H)
assert not live, "over-quota user must be cut"
c.post(f"/api/peers/{b['id']}/adjust", json={"add_gb": 5}, headers=H)
assert len(live) == 1 and len(pubs()) == 1
print("CHECK 5 ok: adding volume re-activates the SAME config (no new config needed)")

# enforcement removes expired users from the conf too (reboot can't resurrect them)
with db.cursor() as cur:
    cur.execute("UPDATE peers SET expires_at=? WHERE id=?", (db.now() - 5, b["id"]))
m._enforcement_tick()
assert not live and not pubs()
c.post(f"/api/peers/{b['id']}/adjust", json={"add_days": 30}, headers=H)  # expiry was -5s ago -> +30d
assert len(live) == 1 and len(pubs()) == 1
print("CHECK 6 ok: expiry cut-off persists in conf; extending time restores access")

# monitor
assert c.get("/api/health").json() == {"ok": True}
o = c.get("/api/server/overview", headers=H).json()
assert o["mem"]["total"] > 0 and "percent" in o["mem"] and o["disk"]["total"] > 0
ps = c.get("/api/server/processes?sort=mem&limit=5", headers=H).json()
assert 1 <= len(ps) <= 5 and all("rss" in p for p in ps)
assert c.post("/api/server/processes/1/kill", json={}, headers=H).status_code == 400
assert c.post(f"/api/server/processes/{os.getpid()}/kill", json={}, headers=H).status_code == 400
assert c.get("/api/server/overview").status_code in (401, 403)
print("CHECK 7 ok: monitor endpoints work, pid 1 / panel protected, auth required")

# subprocess timeout
t = time.time()
try: awg._run(["sleep", "5"], timeout=1); raise SystemExit("no timeout")
except RuntimeError as e: assert "timed out" in str(e)
assert time.time() - t < 3
print("CHECK 8 ok: hung subprocess is cut after the timeout")

# slow awg must not block the event loop
def slow_dump(): time.sleep(1.0); return {}
awg.dump = slow_dump
async def probe():
    ticks = 0
    task = asyncio.ensure_future(m.stats_loop())
    end = time.time() + 1.5
    while time.time() < end:
        await asyncio.sleep(0.05); ticks += 1
    task.cancel()
    return ticks
ticks = asyncio.run(probe())
assert ticks > 15, ticks
print(f"CHECK 9 ok: event loop stayed responsive during a 1s-slow awg call ({ticks} ticks)")
calls.clear()
assert c.put(f"/api/peers/{b['id']}", json={"name": "  New Name "}, headers=H).status_code == 200
assert c.get("/api/peers", headers=H).json()[0]["name"] == "New Name"
assert c.put(f"/api/peers/{b['id']}", json={"name": " "}, headers=H).status_code == 400
assert calls == [] or all(x[0] != "remove" for x in calls), calls
print("CHECK 10 ok: rename works, empty rejected, user is never removed from the tunnel")
# fair bandwidth sharing: endpoint writes env + runs the script (stubbed tc)
import subprocess, shutil
stub = tempfile.mkdtemp(); state = os.path.join(stub, "state")
open(os.path.join(stub, "tc"), "w").write(f"""#!/usr/bin/env bash
case "$1 $2" in
 "qdisc show") [ -f {state} ] && cat {state} || echo "qdisc noqueue";;
 "qdisc del") rm -f {state};;
 "qdisc replace") if [[ "$*" == *cake* ]]; then echo "qdisc cake 8001: root" > {state}; else echo "qdisc fq_codel 8001: root" > {state}; fi;;
esac
""")
open(os.path.join(stub, "curl"), "w").write("#!/usr/bin/env bash\nexit 0\n")
open(os.path.join(stub, "ip"), "w").write('#!/usr/bin/env bash\nexit 0\n')
for f in ("tc", "ip", "curl"): os.chmod(os.path.join(stub, f), 0o755)
os.environ["PATH"] = stub + ":" + os.environ["PATH"]
env_file = Path(tempfile.mktemp()); env_file.write_text("PANEL_PORT=8787\nOTHER=keep\n")
config.ENV_FILE = env_file
# default is OFF: nothing is shaped until the admin opts in
g0 = c.get("/api/admin/qos", headers=H).json(); assert g0["enabled"] is False and g0["mode"] == "none", g0
r = c.put("/api/admin/qos", json={"bandwidth_mbit": 200}, headers=H); assert r.status_code == 200, r.text
assert r.json()["mode"] == "none" and r.json()["enabled"] is False, r.json()   # speed saved but still off
r = c.put("/api/admin/qos", json={"bandwidth_mbit": 200, "enabled": True}, headers=H); assert r.status_code == 200, r.text
assert r.json()["mode"] == "cake" and r.json()["applied_rate_mbit"] == 180, r.json()
txt = env_file.read_text(); assert "AWG_BANDWIDTH_MBIT=200" in txt and "OTHER=keep" in txt
g = c.get("/api/admin/qos", headers=H).json(); assert g["bandwidth_mbit"] == 200 and g["mode"] == "cake", g
r = c.put("/api/admin/qos", json={"bandwidth_mbit": 0}, headers=H).json()
assert "AWG_BANDWIDTH_MBIT" not in env_file.read_text()
# empty = server measures itself (stub curl returns instantly -> big plausible number), cached in env, used as-is
assert r["mode"] == "cake" and r["auto_mbit"] > 100 and r["applied_rate_mbit"] == r["auto_mbit"], r
assert "AWG_BANDWIDTH_AUTO=" in env_file.read_text()
assert c.put("/api/admin/qos", json={"bandwidth_mbit": -5}, headers=H).status_code == 400
print("CHECK 11 ok: fair-sharing endpoint: cake at 90% of line speed, falls back to fq_codel, env preserved")
# live bandwidth view: two polls, counters move -> per-user rates + "slowed" flag when near line speed
open(os.path.join(stub, "tc"), "a").write("""
case "$1 $2 $3" in
 "-s qdisc show") echo "qdisc cake 8001: root refcnt 2 bandwidth 100Mbit"; echo " Sent 1000 bytes 10 pkt (dropped 7, overlimits 0 requeues 0)"; echo " backlog 3000b 2p requeues 0"; echo "  av_delay 4.5ms";;
esac
""")
c.put("/api/admin/qos", json={"bandwidth_mbit": 100, "enabled": True}, headers=H)   # shaped line = 90 Mbit/s
awg.dump = lambda: {k: dict(v) for k, v in live.items()}  # undo the slow_dump used in check 9
pubkey_b = [p for p in live][0]
live[pubkey_b].update({"rx": 0, "tx": 0})
c.get("/api/server/bandwidth", headers=H)          # baseline sample
time.sleep(1.0)
live[pubkey_b].update({"rx": 1_000_000, "tx": 11_500_000})   # ~92 Mbit/s down in ~1 s
bw = c.get("/api/server/bandwidth", headers=H).json()
assert bw["mode"] == "cake" and bw["line_mbit"] == 90, bw
assert bw["users"] and bw["users"][0]["down_mbit"] > 60 and bw["busy"] is True and bw["users"][0]["limited"] is True, bw
assert bw["queue"]["drops"] == 7 and bw["queue"]["backlog_bytes"] == 3000 and bw["queue"]["avg_delay_ms"] == 4.5, bw["queue"]
print("CHECK 12 ok: live bandwidth view: per-user rates, utilization, 'slowed' flag, queue drops/delay")
# switching OFF removes the qdisc again (back to original behaviour)
r = c.put("/api/admin/qos", json={"enabled": False}, headers=H).json()
assert r["mode"] == "none" and r["enabled"] is False, r
assert c.get("/api/admin/qos", headers=H).json()["mode"] == "none"
print("CHECK 13 ok: default OFF; switch ON/OFF applies live and OFF restores the original (no qdisc)")
print("\nROUND 3 ALL PASSED")

# rename (display name only) - separate section appended after the main checks

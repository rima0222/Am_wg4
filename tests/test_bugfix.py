"""
Smoke test for the bug-fix round:
  - unlimited <-> limited toggle (new `unlimited` flag on PUT /api/peers/{id})
  - source_ip / offline_seconds fields on GET /api/peers
  - /api/usage/summary (today/month/all-time rollup) and the daily_usage table
Mocks the `awg` module entirely (no real WireGuard binary in this sandbox).
"""
import os
import sys
import tempfile
import time as time_mod

os.environ["PANEL_DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["PANEL_ADMIN_USER"] = "admin"

# config.py snapshots os.environ at import time, so the admin password hash
# must be computed and set BEFORE anything under app.* gets imported.
import hashlib, os as _os
_salt = _os.urandom(16).hex()
_digest = hashlib.pbkdf2_hmac("sha256", b"testpass123", bytes.fromhex(_salt), 200_000)
os.environ["PANEL_ADMIN_PASSWORD_HASH"] = f"{_salt}${_digest.hex()}"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "panel"))

# ---- mock the awg module (shells out to real wg tools) before main imports it ----
import app.awg as awg

_fake_live = {}

def fake_genkey(): return "FAKEPRIVKEY=="
def fake_pubkey(pk): return "FAKEPUBKEY_" + str(hash(pk))
def fake_genpsk(): return "FAKEPSK=="
def fake_add_peer_live(pub, psk, ip, ipv6=None): pass
def fake_remove_peer_live(pub): pass
def fake_append_peer_to_conf(*a, **k): pass
def fake_remove_peer_from_conf(*a, **k): pass
def fake_dump(): return _fake_live
def fake_build_client_config(*a, **k): return "[Interface]\nfake\n"

awg.genkey = fake_genkey
awg.pubkey = fake_pubkey
awg.genpsk = fake_genpsk
awg.add_peer_live = fake_add_peer_live
awg.remove_peer_live = fake_remove_peer_live
awg.append_peer_to_conf = fake_append_peer_to_conf
awg.remove_peer_from_conf = fake_remove_peer_from_conf
awg.dump = fake_dump
awg.get_identity = lambda: {}
awg.get_identity = lambda: {"private_key": "P", "public_key": "Q", "port": 1, "endpoint": "", "params": {}}
awg.build_client_config = fake_build_client_config

from fastapi.testclient import TestClient
from app.main import app
import app.main as main_mod

client = TestClient(app)
client.__enter__()  # trigger the startup event (database.init_db(), background loops)

def login():
    r = client.post("/api/login", json={"username": "admin", "password": "testpass123"})
    assert r.status_code == 200, r.text
    return r.json()["token"]

TOKEN = login()
H = {"Authorization": f"Bearer {TOKEN}"}

# ---- 1. create a peer with a 10 GB limit ----
r = client.post("/api/peers", json={"name": "alice", "data_limit_gb": 10}, headers=H)
assert r.status_code == 200, r.text
peer = r.json()
peer_id = peer["id"]
print("created peer", peer_id)

r = client.get("/api/peers", headers=H)
rows = r.json()
assert rows[0]["data_limit_bytes"] == 10 * 1024**3, rows[0]
print("CHECK 1 ok: created with 10GB limit ->", rows[0]["data_limit_bytes"])

# ---- 2. toggle limited -> unlimited via the new `unlimited` flag ----
r = client.put(f"/api/peers/{peer_id}", json={"unlimited": True}, headers=H)
assert r.status_code == 200, r.text
r = client.get("/api/peers", headers=H)
row = r.json()[0]
assert row["data_limit_bytes"] is None, row
print("CHECK 2 ok: toggled to unlimited -> data_limit_bytes is None")

# ---- 3. toggle unlimited -> limited (back to a fixed 25GB) ----
r = client.put(f"/api/peers/{peer_id}", json={"data_limit_gb": 25}, headers=H)
assert r.status_code == 200, r.text
r = client.get("/api/peers", headers=H)
row = r.json()[0]
assert row["data_limit_bytes"] == 25 * 1024**3, row
print("CHECK 3 ok: toggled back to limited (25GB) ->", row["data_limit_bytes"])

# ---- 4. old behavior preserved: body with neither field leaves limit untouched ----
r = client.put(f"/api/peers/{peer_id}", json={"note": "just a note update"}, headers=H)
assert r.status_code == 200, r.text
r = client.get("/api/peers", headers=H)
row = r.json()[0]
assert row["data_limit_bytes"] == 25 * 1024**3, row
assert row["note"] == "just a note update"
print("CHECK 4 ok: unrelated update doesn't clobber the limit")

# ---- 5. source_ip / offline_seconds fields present and correct ----
pub_key = client.get("/api/backup", headers=H).json()["peers"][0]["public_key"]
_fake_live[pub_key] = {
    "endpoint": "203.0.113.55:51000",
    "allowed_ips": "10.29.29.2/32",
    "latest_handshake": int(time_mod.time()) - 10,  # recently seen -> online
    "rx": 1000, "tx": 2000,
}
r = client.get("/api/peers", headers=H)
row = r.json()[0]
assert row["source_ip"] == "203.0.113.55", row
assert row["online"] is True
assert row["offline_seconds"] is None  # online peers don't report an offline duration
print("CHECK 5 ok: source_ip parsed from live dump ->", row["source_ip"])

# simulate going offline a while ago
_fake_live[pub_key]["latest_handshake"] = int(time_mod.time()) - 10000
r = client.get("/api/peers", headers=H)
row = r.json()[0]
assert row["online"] is False
assert row["offline_seconds"] is not None and row["offline_seconds"] >= 9999
print("CHECK 6 ok: offline_seconds computed ->", row["offline_seconds"])

# ---- 6. daily_usage rollup + /api/usage/summary ----
import app.database as database
today = time_mod.strftime("%Y-%m-%d", time_mod.gmtime())
with database.cursor() as cur:
    cur.execute(
        "INSERT INTO daily_usage (date, rx, tx) VALUES (?, ?, ?) "
        "ON CONFLICT(date) DO UPDATE SET rx=rx+excluded.rx, tx=tx+excluded.tx",
        (today, 500_000_000, 300_000_000),
    )
r = client.get("/api/usage/summary", headers=H)
assert r.status_code == 200, r.text
s = r.json()
assert s["today_bytes"] == 800_000_000, s
assert s["month_bytes"] >= 800_000_000, s
print("CHECK 7 ok: usage summary ->", s)

# ---- 7. geo enrichment is now READ-ONLY on the request path: /api/peers must
#         never itself call the external API (that's geo_loop's job now) ----
import app.geo as geo

def boom(ips):
    raise AssertionError("list_peers must never call the geo API directly anymore")
main_mod.geo.lookup_batch = boom

r = client.get("/api/peers", headers=H)
row = r.json()[0]
assert row["source_country"] is None and row["source_city"] is None, row
print("CHECK 8 ok: /api/peers does zero external geo calls (cache empty -> nulls, no crash)")

# ---- 8. geo_loop() is the only thing that may call the external API, and it
#         batches + caches exactly as expected ----
def fake_lookup_batch(ips):
    assert ips == ["203.0.113.55"], ips
    return {"203.0.113.55": {"country": "Testland", "city": "Testville", "lat": 12.34, "lon": 56.78}}
main_mod.geo.lookup_batch = fake_lookup_batch

import asyncio as _asyncio
async def _one_geo_tick():
    # run geo_loop's body once by racing it against a short timeout
    task = _asyncio.ensure_future(main_mod.geo_loop())
    await _asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except _asyncio.CancelledError:
        pass

_asyncio.run(_one_geo_tick())

main_mod.geo.lookup_batch = boom  # must not be called again on the next read
r = client.get("/api/peers", headers=H)
row = r.json()[0]
assert row["source_country"] == "Testland", row
assert row["source_lat"] == 12.34 and row["source_lon"] == 56.78, row
print("CHECK 9 ok: geo_loop populated the cache; /api/peers now reads it with zero API calls")

# ---- 10. startup sync: enabled+valid peers are (re)added live; disabled/expired are not ----
import app.database as _db
added = []
main_mod.awg.add_peer_live = lambda pub, psk, ip, ipv6=None: added.append(pub)
with _db.cursor() as cur:
    now_ = _db.now()
    for i, (name, enabled, exp) in enumerate([("s-ok", 1, None), ("s-disabled", 0, None), ("s-expired", 1, now_ - 50)]):
        cur.execute(
            "INSERT INTO peers (name, public_key, private_key, preshared_key, ip_address, enabled, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (name, "SYNCPUB_" + name, "priv", "psk", f"10.29.29.{200+i}", enabled, now_, exp),
        )
        cur.execute("INSERT INTO peer_stats (peer_id) VALUES (?)", (cur.lastrowid,))
main_mod.sync_live_peers()
assert "SYNCPUB_s-ok" in added, added
assert "SYNCPUB_s-disabled" not in added and "SYNCPUB_s-expired" not in added, added
print("CHECK 10 ok: startup sync re-adds only enabled, non-expired users ->", len(added), "peers")

print("\nALL CHECKS PASSED")

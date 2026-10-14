import asyncio
import os
import signal
import subprocess
import threading
import ipaddress
import io
import time
import psutil
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Body, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List

from . import config, database, awg, auth, geo

app = FastAPI(title="AmneziaWG Panel")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# brute-force protection for login endpoints (in-memory, per-IP)
# ============================================================
_login_attempts: dict = {}
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300    # 5 minutes to accumulate failures
LOGIN_BLOCK_SECONDS = 900     # 15 minute lockout once tripped


def _client_key(prefix: str, request: Request) -> str:
    ip = request.client.host if request.client else "unknown"
    return f"{prefix}:{ip}"


def _check_rate_limit(key: str):
    now = time.time()
    entry = _login_attempts.get(key)
    if entry and entry.get("blocked_until", 0) > now:
        retry_after = int(entry["blocked_until"] - now)
        raise HTTPException(status_code=429, detail=f"Too many attempts. Try again in {retry_after}s.")


def _record_failure(key: str):
    now = time.time()
    entry = _login_attempts.get(key)
    if not entry or now - entry["first"] > LOGIN_WINDOW_SECONDS:
        entry = {"count": 0, "first": now}
    entry["count"] += 1
    if entry["count"] >= LOGIN_MAX_ATTEMPTS:
        entry["blocked_until"] = now + LOGIN_BLOCK_SECONDS
    _login_attempts[key] = entry


def _record_success(key: str):
    _login_attempts.pop(key, None)


def _cleanup_rate_limits():
    now = time.time()
    stale = [
        k
        for k, v in _login_attempts.items()
        if now - v["first"] > LOGIN_WINDOW_SECONDS and v.get("blocked_until", 0) < now
    ]
    for k in stale:
        _login_attempts.pop(k, None)


# ============================================================
# schemas
# ============================================================
class LoginRequest(BaseModel):
    username: str
    password: str


class PortalLoginRequest(BaseModel):
    username: str
    password: str


class CreatePeerRequest(BaseModel):
    name: str
    note: Optional[str] = ""
    expires_at: Optional[int] = None       # unix timestamp, or None
    data_limit_gb: Optional[float] = None  # None = unlimited
    duration_days: Optional[int] = None    # used by "reset" to recompute expiry


class UpdatePeerRequest(BaseModel):
    enabled: Optional[bool] = None
    note: Optional[str] = None
    expires_at: Optional[int] = None
    data_limit_gb: Optional[float] = None
    name: Optional[str] = None  # rename: only a display label; keys/IP untouched, so the client stays connected
    unlimited: Optional[bool] = None  # explicit True clears the data cap (None alone is ambiguous with "unset")


class PortalCredentialsRequest(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None



class ServerSettingsRequest(BaseModel):
    endpoint: str


class AdjustPeerRequest(BaseModel):
    add_gb: Optional[float] = None   # can be negative to subtract
    add_days: Optional[int] = None   # can be negative to subtract


class AdminCredentialsRequest(BaseModel):
    current_password: str
    new_username: Optional[str] = None
    new_password: Optional[str] = None


# ============================================================
# startup
# ============================================================
@app.on_event("startup")
async def startup():
    database.init_db()
    # fewer worker threads = less memory (each thread keeps its own malloc arena/stack).
    # 8 is plenty for one admin + a few portal users.
    try:
        import anyio.to_thread
        anyio.to_thread.current_default_thread_limiter().total_tokens = 8
    except Exception:
        pass
    asyncio.create_task(asyncio.to_thread(reconcile_peers))
    asyncio.create_task(stats_loop())
    asyncio.create_task(enforcement_loop())
    asyncio.create_task(geo_loop())


_reconcile_lock = threading.Lock()


def reconcile_peers():
    """Single source of truth for 'who may be on the tunnel'.

    Computes the set of ACTIVE users (enabled, not expired, within quota) from the DB,
    then (a) adds the missing ones to the live interface, (b) removes inactive ones
    that are still on it, and (c) rewrites the on-disk .conf peer list atomically so a
    later reboot/restart matches. It only ever issues per-peer `awg set` commands, so
    sessions of users who are NOT being changed are never interrupted, and the tunnel
    itself is never restarted."""
    with _reconcile_lock:
        now = database.now()
        with database.cursor() as cur:
            cur.execute(
                """SELECT p.*, COALESCE(s.cumulative_rx,0)+COALESCE(s.cumulative_tx,0) AS used
                   FROM peers p LEFT JOIN peer_stats s ON s.peer_id = p.id ORDER BY p.id"""
            )
            rows = [dict(r) for r in cur.fetchall()]
        live = awg.dump()
        active = []
        for r in rows:
            expired = r["expires_at"] and now > r["expires_at"]
            over = r["data_limit_bytes"] and r["used"] > r["data_limit_bytes"]
            if r["enabled"] and not expired and not over:
                active.append(r)
                if r["public_key"] not in live:
                    try:
                        awg.add_peer_live(r["public_key"], r["preshared_key"], r["ip_address"], r["ipv6_address"])
                    except Exception as e:
                        print(f"[reconcile] could not add {r['name']}: {e}")
            elif r["public_key"] in live:
                awg.remove_peer_live(r["public_key"])
        try:
            awg.write_conf_peers(active)
        except Exception as e:
            print(f"[reconcile] could not write conf: {e}")


def sync_live_peers():
    """Makes sure every enabled, non-expired, within-quota peer in the panel
    DB is actually present on the live tunnel. `awg set ... peer` is
    idempotent and doesn't reset an existing session, so this is safe to run
    on every panel start. It heals the case where the tunnel was restarted
    from a .conf that doesn't list some users."""
    now = database.now()
    with database.cursor() as cur:
        cur.execute(
            """SELECT p.*, COALESCE(s.cumulative_rx,0)+COALESCE(s.cumulative_tx,0) AS used
               FROM peers p LEFT JOIN peer_stats s ON s.peer_id = p.id
               WHERE p.enabled = 1"""
        )
        rows = cur.fetchall()

    synced = 0
    for row in rows:
        if row["expires_at"] and now > row["expires_at"]:
            continue
        if row["data_limit_bytes"] and row["used"] > row["data_limit_bytes"]:
            continue
        try:
            awg.add_peer_live(row["public_key"], row["preshared_key"], row["ip_address"], row["ipv6_address"])
            synced += 1
        except Exception as e:
            print(f"[sync_live_peers] could not add {row['name']}: {e}")
    print(f"[sync_live_peers] {synced} enabled user(s) ensured on the live tunnel")


def _subnet_base() -> str:
    net = ipaddress.ip_network(config.SERVER_SUBNET, strict=False)
    parts = str(net.network_address).split(".")
    return ".".join(parts[:3])


def _derive_ipv6(ipv4_address: str) -> Optional[str]:
    """Derives a matching IPv6 address in the server's ULA range from the
    IPv4 host octet, e.g. 10.29.29.42 -> fd42:29:29::2a. Returns None if
    IPv6 wasn't enabled at install time (no native uplink detected)."""
    if not config.ENABLE_IPV6:
        return None
    host_octet = int(ipv4_address.split(".")[-1])
    base_net = config.SERVER_SUBNET6.split("/")[0]
    return f"{base_net}{format(host_octet, 'x')}"


def _get_admin_row():
    with database.cursor() as cur:
        cur.execute("SELECT * FROM admin WHERE id=1")
        return cur.fetchone()


# A source IP's city/country essentially never changes faster than this, and
# keeping the TTL long is what keeps call volume low - see geo_loop() below
# for why this is deliberately decoupled from the panel's 3s live poll.
GEO_CACHE_TTL_SECONDS = 7 * 24 * 3600
GEO_LOOKUP_INTERVAL_SECONDS = 600  # how often the background loop checks for new/stale IPs


def _enrich_with_geo(peers: list):
    """Attaches source_country/source_city/source_lat/source_lon to each peer
    dict in place, read-only - a plain SQLite SELECT against the cache table,
    no network I/O. This runs on every /api/peers call (the panel polls every
    few seconds), so it must never itself reach out to the internet: that job
    belongs to geo_loop(), which runs on its own slow, rate-limited schedule."""
    ips = sorted({p["source_ip"] for p in peers if p.get("source_ip")})
    if not ips:
        return

    with database.cursor() as cur:
        cur.execute(
            f"SELECT ip, country, city, lat, lon FROM ip_geo_cache WHERE ip IN ({','.join('?' * len(ips))})",
            ips,
        )
        cached = {r["ip"]: dict(r) for r in cur.fetchall()}

    for p in peers:
        entry = cached.get(p.get("source_ip"))
        p["source_country"] = entry["country"] if entry else None
        p["source_city"] = entry["city"] if entry else None
        p["source_lat"] = entry["lat"] if entry else None
        p["source_lon"] = entry["lon"] if entry else None


async def geo_loop():
    """Resolves source IPs to city/country in the background, well off the
    request path. Runs every GEO_LOOKUP_INTERVAL_SECONDS (10 min by default),
    looks at only the IPs currently live on the interface, skips anything
    already cached within GEO_CACHE_TTL_SECONDS (7 days), and sends the rest
    as a single batched request - capping how often (and how much) this ever
    calls the external geo service, so it can't get the VPS's IP rate-limited
    or flagged, and never adds load to the admin panel's live polling."""
    while True:
        try:
            live = awg.dump()
            ips = set()
            for info in live.values():
                endpoint = info.get("endpoint") or ""
                if endpoint and endpoint != "(none)":
                    ips.add(endpoint.rsplit(":", 1)[0].strip("[]"))

            if ips:
                now = database.now()
                with database.cursor() as cur:
                    cur.execute(
                        f"SELECT ip, updated_at FROM ip_geo_cache WHERE ip IN ({','.join('?' * len(ips))})",
                        list(ips),
                    )
                    cached_at = {r["ip"]: r["updated_at"] for r in cur.fetchall()}

                stale = [ip for ip in ips if ip not in cached_at or (now - cached_at[ip]) > GEO_CACHE_TTL_SECONDS]
                if stale:
                    fresh = geo.lookup_batch(stale)
                    if fresh:
                        with database.cursor() as cur:
                            for ip, info in fresh.items():
                                cur.execute(
                                    """INSERT INTO ip_geo_cache (ip, country, city, lat, lon, updated_at)
                                       VALUES (?, ?, ?, ?, ?, ?)
                                       ON CONFLICT(ip) DO UPDATE SET country=excluded.country, city=excluded.city,
                                           lat=excluded.lat, lon=excluded.lon, updated_at=excluded.updated_at""",
                                    (ip, info.get("country"), info.get("city"), info.get("lat"), info.get("lon"), now),
                                )
                    else:
                        print(f"[geo] no results for {len(stale)} IP(s) - lookup may be blocked from this server")
        except Exception as e:
            print(f"[geo_loop] error: {e}")
        await asyncio.sleep(GEO_LOOKUP_INTERVAL_SECONDS)


# ============================================================
# admin auth
# ============================================================
@app.post("/api/login")
def login(body: LoginRequest, request: Request):
    key = _client_key("admin", request)
    _check_rate_limit(key)
    row = _get_admin_row()
    if not row or body.username != row["username"] or not auth.verify_password(
        body.password, row["password_hash"]
    ):
        _record_failure(key)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    _record_success(key)
    return {"token": auth.create_admin_token(row["username"])}


@app.put("/api/admin/credentials")
def update_admin_credentials(body: AdminCredentialsRequest, _=Depends(auth.require_admin)):
    row = _get_admin_row()
    if not row or not auth.verify_password(body.current_password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    new_username = body.new_username.strip() if body.new_username else row["username"]
    new_hash = auth.hash_password(body.new_password) if body.new_password else row["password_hash"]

    with database.cursor() as cur:
        cur.execute(
            "UPDATE admin SET username=?, password_hash=? WHERE id=1",
            (new_username, new_hash),
        )
    return {"ok": True, "username": new_username}


@app.get("/api/admin/server-settings")
def get_server_settings(_=Depends(auth.require_admin)):
    return {
        "endpoint": config.SERVER_ENDPOINT,
        "port": config.SERVER_PORT,
        "ipv6_enabled": config.ENABLE_IPV6,
    }


@app.put("/api/admin/server-settings")
def update_server_settings(body: ServerSettingsRequest, _=Depends(auth.require_admin)):
    """Lets the admin point clients at a domain (or a new IP) without
    reinstalling. Takes effect immediately for every config/QR generated
    from now on - existing connected clients keep working off their old
    config until they re-fetch a new one (e.g. from /portal)."""
    new_endpoint = body.endpoint.strip()
    if not new_endpoint:
        raise HTTPException(400, "Endpoint cannot be empty")
    with database.cursor() as cur:
        cur.execute(
            """INSERT INTO server_settings (id, endpoint) VALUES (1, ?)
               ON CONFLICT(id) DO UPDATE SET endpoint=excluded.endpoint""",
            (new_endpoint,),
        )
    config.SERVER_ENDPOINT = new_endpoint
    return {"ok": True, "endpoint": new_endpoint}


# ============================================================
# peers (admin)
# ============================================================
@app.get("/api/peers")
def list_peers(
    search: Optional[str] = None,
    sort: Optional[str] = None,
    _=Depends(auth.require_admin),
):
    """sort: usage_desc, usage_asc, days_desc, days_asc, name_asc, created_desc (default)"""
    live = awg.dump()
    now = database.now()
    result = []
    with database.cursor() as cur:
        cur.execute(
            """SELECT p.*, s.cumulative_rx, s.cumulative_tx, s.last_handshake
               FROM peers p LEFT JOIN peer_stats s ON s.peer_id = p.id
               ORDER BY p.created_at DESC"""
        )
        for row in cur.fetchall():
            live_info = live.get(row["public_key"], {})
            last_handshake = live_info.get("latest_handshake") or row["last_handshake"] or 0
            online = bool(last_handshake) and (now - last_handshake) < config.ONLINE_THRESHOLD_SECONDS
            used_bytes = (row["cumulative_rx"] or 0) + (row["cumulative_tx"] or 0)
            days_left = (
                (row["expires_at"] - now) / 86400 if row["expires_at"] else None
            )

            # the kernel's own view of who's actually talking to this peer right now -
            # the real source address/port it last saw a packet from
            raw_endpoint = live_info.get("endpoint") or ""
            source_ip = None
            if raw_endpoint and raw_endpoint != "(none)":
                source_ip = raw_endpoint.rsplit(":", 1)[0].strip("[]")

            offline_seconds = None
            if not online and last_handshake:
                offline_seconds = max(0, now - last_handshake)

            result.append(
                {
                    "id": row["id"],
                    "account_number": row["account_number"],
                    "name": row["name"],
                    "ip_address": row["ip_address"],
                    "ipv6_address": row["ipv6_address"],
                    "note": row["note"],
                    "enabled": bool(row["enabled"]),
                    "created_at": row["created_at"],
                    "expires_at": row["expires_at"],
                    "days_left": days_left,
                    "duration_days": row["duration_days"],
                    "data_limit_bytes": row["data_limit_bytes"],
                    "used_bytes": used_bytes,
                    "rx_bytes": row["cumulative_rx"] or 0,
                    "tx_bytes": row["cumulative_tx"] or 0,
                    "last_handshake": last_handshake,
                    "online": online,
                    "offline_seconds": offline_seconds,
                    "source_ip": source_ip,
                    "portal_username": row["portal_username"],
                }
            )

    _enrich_with_geo(result)

    if search:
        q = search.strip().lower()
        result = [
            p
            for p in result
            if q in (p["name"] or "").lower()
            or q in (p["note"] or "").lower()
            or q in (p["ip_address"] or "").lower()
            or q in (p["ipv6_address"] or "").lower()
            or q in str(p["account_number"] or "")
            or q in (p["portal_username"] or "").lower()
        ]

    sort_key_map = {
        "usage_desc": (lambda p: p["used_bytes"], True),
        "usage_asc": (lambda p: p["used_bytes"], False),
        # peers with no expiry sort last regardless of direction
        "days_desc": (lambda p: (p["days_left"] is None, -(p["days_left"] or 0)), False),
        "days_asc": (lambda p: (p["days_left"] is None, p["days_left"] or 0), False),
        "name_asc": (lambda p: (p["name"] or "").lower(), False),
    }
    if sort in sort_key_map:
        key_fn, reverse = sort_key_map[sort]
        result.sort(key=key_fn, reverse=reverse)

    return result


@app.post("/api/peers")
def create_peer(body: CreatePeerRequest, _=Depends(auth.require_admin)):
    with database.cursor() as cur:
        cur.execute("SELECT ip_address FROM peers")
        used_ips = {r["ip_address"] for r in cur.fetchall()}

    ip_address = database.next_free_ip(_subnet_base(), used_ips)
    ipv6_address = _derive_ipv6(ip_address)
    account_number = database.next_account_number()
    private_key = awg.genkey()
    public_key = awg.pubkey(private_key)
    preshared_key = awg.genpsk()
    data_limit_bytes = int(body.data_limit_gb * 1024**3) if body.data_limit_gb else None

    portal_username = auth.random_username()
    portal_password = auth.random_password()
    portal_password_hash = auth.hash_password(portal_password)

    with database.cursor() as cur:
        cur.execute(
            """INSERT INTO peers
               (name, public_key, private_key, preshared_key, ip_address, ipv6_address, note,
                enabled, created_at, expires_at, data_limit_bytes, duration_days,
                portal_username, portal_password_hash, account_number)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
            (
                body.name,
                public_key,
                private_key,
                preshared_key,
                ip_address,
                ipv6_address,
                body.note or "",
                database.now(),
                body.expires_at,
                data_limit_bytes,
                body.duration_days,
                portal_username,
                portal_password_hash,
                account_number,
            ),
        )
        peer_id = cur.lastrowid
        cur.execute("INSERT INTO peer_stats (peer_id) VALUES (?)", (peer_id,))

    awg.add_peer_live(public_key, preshared_key, ip_address, ipv6_address)
    reconcile_peers()  # also persists the peer into the .conf (atomic)

    client_conf = awg.build_client_config(private_key, ip_address, preshared_key, ipv6_address)
    return {
        "id": peer_id,
        "account_number": account_number,
        "ip_address": ip_address,
        "ipv6_address": ipv6_address,
        "config": client_conf,
        "portal_username": portal_username,
        "portal_password": portal_password,  # shown once, plaintext
    }


@app.put("/api/peers/{peer_id}")
def update_peer(peer_id: int, body: UpdatePeerRequest, _=Depends(auth.require_admin)):
    with database.cursor() as cur:
        cur.execute("SELECT * FROM peers WHERE id=?", (peer_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "User not found")

        new_enabled = row["enabled"] if body.enabled is None else int(body.enabled)
        new_note = row["note"] if body.note is None else body.note
        new_name = row["name"]
        if body.name is not None:
            new_name = body.name.strip()
            if not new_name:
                raise HTTPException(400, "Name cannot be empty")
        new_expires = row["expires_at"] if body.expires_at is None else body.expires_at

        # `data_limit_gb: null` alone is indistinguishable from "field omitted" once it
        # crosses JSON, so switching a user to unlimited needs its own explicit flag.
        if body.unlimited is True:
            new_limit = None
        elif body.data_limit_gb is not None:
            new_limit = int(body.data_limit_gb * 1024**3)
        else:
            new_limit = row["data_limit_bytes"]

        cur.execute(
            """UPDATE peers SET enabled=?, note=?, expires_at=?, data_limit_bytes=?, name=? WHERE id=?""",
            (new_enabled, new_note, new_expires, new_limit, new_name, peer_id),
        )

    reconcile_peers()
    return {"ok": True}


@app.post("/api/peers/{peer_id}/reset")
def reset_peer(peer_id: int, _=Depends(auth.require_admin)):
    """Resets usage counters to zero and, if a duration_days was set, restarts the expiry period."""
    live = awg.dump()
    with database.cursor() as cur:
        cur.execute("SELECT * FROM peers WHERE id=?", (peer_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "User not found")

        live_info = live.get(row["public_key"], {})
        new_expires = row["expires_at"]
        if row["duration_days"]:
            new_expires = database.now() + row["duration_days"] * 86400

        cur.execute(
            """UPDATE peer_stats SET cumulative_rx=0, cumulative_tx=0, last_rx=?, last_tx=?
               WHERE peer_id=?""",
            (live_info.get("rx", 0), live_info.get("tx", 0), peer_id),
        )
        cur.execute("UPDATE peers SET enabled=1, expires_at=? WHERE id=?", (new_expires, peer_id))

    reconcile_peers()
    return {"ok": True, "expires_at": new_expires}


@app.post("/api/peers/{peer_id}/adjust")
def adjust_peer(peer_id: int, body: AdjustPeerRequest, _=Depends(auth.require_admin)):
    """Adds or subtracts GB / days from a user's plan (negative values subtract)."""
    with database.cursor() as cur:
        cur.execute("SELECT * FROM peers WHERE id=?", (peer_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "User not found")

        new_limit = row["data_limit_bytes"]
        if body.add_gb is not None:
            base = new_limit or 0
            new_limit = max(0, base + int(body.add_gb * 1024**3))

        new_expires = row["expires_at"]
        if body.add_days is not None:
            base = new_expires or database.now()
            new_expires = base + body.add_days * 86400

        cur.execute(
            "UPDATE peers SET data_limit_bytes=?, expires_at=? WHERE id=?",
            (new_limit, new_expires, peer_id),
        )

        cur.execute(
            """SELECT p.*, s.cumulative_rx, s.cumulative_tx FROM peers p
               JOIN peer_stats s ON s.peer_id = p.id WHERE p.id=?""",
            (peer_id,),
        )
        row2 = cur.fetchone()

        now = database.now()
        used = (row2["cumulative_rx"] or 0) + (row2["cumulative_tx"] or 0)
        still_expired = row2["expires_at"] and now > row2["expires_at"]
        still_over = row2["data_limit_bytes"] and used > row2["data_limit_bytes"]
        needs_reactivate = not row2["enabled"] and not still_expired and not still_over
        if needs_reactivate:
            cur.execute("UPDATE peers SET enabled=1 WHERE id=?", (peer_id,))

    reconcile_peers()  # adds the user back if the new quota/time allows it, removes if now exceeded

    return {"ok": True, "data_limit_bytes": new_limit, "expires_at": new_expires}


@app.post("/api/peers/{peer_id}/portal-credentials/regenerate")
def regenerate_portal_credentials(
    peer_id: int, body: PortalCredentialsRequest = None, _=Depends(auth.require_admin)
):
    """Regenerates the user's portal login. Pass a custom username/password to
    set them explicitly, or omit either to auto-generate a random one."""
    body = body or PortalCredentialsRequest()
    with database.cursor() as cur:
        cur.execute("SELECT id FROM peers WHERE id=?", (peer_id,))
        if not cur.fetchone():
            raise HTTPException(404, "User not found")

        if body.username:
            new_username = body.username.strip()
            cur.execute(
                "SELECT id FROM peers WHERE portal_username=? AND id<>?", (new_username, peer_id)
            )
            if cur.fetchone():
                raise HTTPException(409, "That username is already taken by another user")
        else:
            new_username = auth.random_username()

        new_password = body.password if body.password else auth.random_password()

        cur.execute(
            "UPDATE peers SET portal_username=?, portal_password_hash=? WHERE id=?",
            (new_username, auth.hash_password(new_password), peer_id),
        )
    return {"portal_username": new_username, "portal_password": new_password}


@app.delete("/api/peers/{peer_id}")
def delete_peer(peer_id: int, _=Depends(auth.require_admin)):
    with database.cursor() as cur:
        cur.execute("SELECT * FROM peers WHERE id=?", (peer_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        cur.execute("DELETE FROM peers WHERE id=?", (peer_id,))
        cur.execute("DELETE FROM peer_stats WHERE peer_id=?", (peer_id,))

    awg.remove_peer_live(row["public_key"])
    reconcile_peers()
    return {"ok": True}


@app.get("/api/peers/{peer_id}/config", response_class=PlainTextResponse)
def get_peer_config(peer_id: int, _=Depends(auth.require_admin)):
    with database.cursor() as cur:
        cur.execute("SELECT * FROM peers WHERE id=?", (peer_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "User not found")
    return awg.build_client_config(row["private_key"], row["ip_address"], row["preshared_key"], row["ipv6_address"])


@app.get("/api/peers/{peer_id}/qr")
def get_peer_qr(peer_id: int, _=Depends(auth.require_admin)):
    with database.cursor() as cur:
        cur.execute("SELECT * FROM peers WHERE id=?", (peer_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "User not found")
    conf = awg.build_client_config(row["private_key"], row["ip_address"], row["preshared_key"], row["ipv6_address"])
    import qrcode  # imported lazily: Pillow is only loaded when a QR is really requested
    img = qrcode.make(conf)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.get("/api/system")
def system_info(_=Depends(auth.require_admin)):
    with database.cursor() as cur:
        cur.execute("SELECT COUNT(*) c FROM peers")
        total = cur.fetchone()["c"]
        cur.execute("SELECT COALESCE(SUM(cumulative_rx+cumulative_tx),0) t FROM peer_stats")
        total_traffic = cur.fetchone()["t"]

    live = awg.dump()
    now = database.now()
    online = sum(
        1
        for v in live.values()
        if v.get("latest_handshake") and (now - v["latest_handshake"]) < config.ONLINE_THRESHOLD_SECONDS
    )

    mem = psutil.virtual_memory()
    cpu_percent = psutil.cpu_percent(interval=0.2)

    return {
        "endpoint": config.SERVER_ENDPOINT,
        "ipv6_enabled": config.ENABLE_IPV6,
        "port": config.SERVER_PORT,
        "interface": config.INTERFACE,
        "total_peers": total,
        "online_peers": online,
        "total_traffic_bytes": total_traffic,
        "cpu_percent": cpu_percent,
        "ram_percent": mem.percent,
        "ram_used_bytes": mem.used,
        "ram_total_bytes": mem.total,
    }


@app.get("/api/health")
def health():
    """Unauthenticated liveness probe used by the local watchdog timer. Reveals nothing."""
    return {"ok": True}


# ============================================================
# server monitor (RAM / CPU / disk / processes) - admin only
# ============================================================
PROTECTED_UNITS = {"awg-panel": "Panel (this web UI)"}


def _read_meminfo() -> dict:
    out = {}
    try:
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            out[k] = int(v.strip().split()[0]) * 1024
    except OSError:
        pass
    return out


@app.get("/api/server/overview")
def server_overview(_=Depends(auth.require_admin)):
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    mi = _read_meminfo()
    disk = psutil.disk_usage("/")
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
    net = psutil.net_io_counters()
    me = psutil.Process(os.getpid())
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "cpu_cores": psutil.cpu_count() or 1,
        "load": [round(x, 2) for x in load],
        "uptime_seconds": int(time.time() - psutil.boot_time()),
        "mem": {
            "total": vm.total,
            "used": vm.total - vm.available,      # what programs really hold
            "available": vm.available,
            "cache": mi.get("Cached", 0) + mi.get("Buffers", 0),   # reclaimable, not a problem
            "percent": round((vm.total - vm.available) * 100 / vm.total, 1),
        },
        "swap": {"total": sw.total, "used": sw.used, "percent": sw.percent},
        "disk": {"total": disk.total, "used": disk.used, "percent": disk.percent},
        "net": {"sent": net.bytes_sent, "recv": net.bytes_recv},
        "panel_rss": me.memory_info().rss,
        "panel_pid": me.pid,
    }


@app.get("/api/server/processes")
def server_processes(sort: str = "mem", limit: int = 15, _=Depends(auth.require_admin)):
    """Top processes by memory (default) or CPU. Same-name processes (e.g. many
    workers) are shown individually so the real culprit is visible."""
    limit = max(1, min(limit, 50))
    procs = []
    for p in psutil.process_iter(["pid", "name", "memory_info", "cmdline", "username"]):
        try:
            p.cpu_percent(None)  # prime the counter
            procs.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    time.sleep(0.4)
    rows = []
    total = psutil.virtual_memory().total
    for p in procs:
        try:
            rss = p.info["memory_info"].rss if p.info.get("memory_info") else 0
            cmd = " ".join(p.info.get("cmdline") or [])[:90] or p.info["name"]
            rows.append({
                "pid": p.pid,
                "name": p.info["name"],
                "cmd": cmd,
                "user": p.info.get("username") or "",
                "rss": rss,
                "mem_percent": round(rss * 100 / total, 1),
                "cpu_percent": round(p.cpu_percent(None), 1),
                "protected": p.pid in (1, os.getpid()),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    rows.sort(key=lambda r: r["cpu_percent"] if sort == "cpu" else r["rss"], reverse=True)
    return rows[:limit]


class KillRequest(BaseModel):
    force: bool = False


@app.post("/api/server/processes/{pid}/kill")
def server_kill_process(pid: int, body: KillRequest, _=Depends(auth.require_admin)):
    if pid in (1, os.getpid()) or pid == os.getppid():
        raise HTTPException(400, "This process is protected (it would take down the server or this panel)")
    try:
        p = psutil.Process(pid)
        name = p.name()
        if name in ("awg", "sshd") or name.startswith("systemd"):
            raise HTTPException(400, f"'{name}' is protected")
        p.send_signal(signal.SIGKILL if body.force else signal.SIGTERM)
    except psutil.NoSuchProcess:
        raise HTTPException(404, "Process no longer exists")
    except psutil.AccessDenied:
        raise HTTPException(403, "Not allowed")
    return {"ok": True, "killed": name}


@app.post("/api/server/free-memory")
def server_free_memory(_=Depends(auth.require_admin)):
    """Drops the kernel's reclaimable page cache. Safe (nothing is lost, the kernel
    refills it as needed); it only makes the 'used' number honest again."""
    before = psutil.virtual_memory().available
    try:
        os.sync()
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("3")
    except OSError as e:
        raise HTTPException(500, f"Could not drop caches: {e}")
    after = psutil.virtual_memory().available
    return {"ok": True, "freed_bytes": max(0, after - before)}


@app.post("/api/server/restart-panel")
def server_restart_panel(_=Depends(auth.require_admin)):
    """Restarts only the web panel (never the VPN tunnel - connected users are untouched)."""
    subprocess.Popen(["bash", "-c", "sleep 1; systemctl restart awg-panel"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return {"ok": True}


@app.get("/api/usage/summary")
def usage_summary(_=Depends(auth.require_admin)):
    """Server-wide traffic totals for today / this month / all time, for the
    circular usage charts at the top of the panel."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    month_prefix = today[:7]  # YYYY-MM

    with database.cursor() as cur:
        cur.execute("SELECT rx, tx FROM daily_usage WHERE date=?", (today,))
        row = cur.fetchone()
        today_bytes = (row["rx"] + row["tx"]) if row else 0

        cur.execute(
            "SELECT COALESCE(SUM(rx),0) r, COALESCE(SUM(tx),0) t FROM daily_usage WHERE date LIKE ?",
            (month_prefix + "%",),
        )
        mrow = cur.fetchone()
        month_bytes = (mrow["r"] or 0) + (mrow["t"] or 0)

        cur.execute("SELECT COALESCE(SUM(cumulative_rx+cumulative_tx),0) t FROM peer_stats")
        all_time_bytes = cur.fetchone()["t"]

    return {
        "today_bytes": today_bytes,
        "month_bytes": max(month_bytes, today_bytes),  # guard against clock/rollover edge cases
        "all_time_bytes": all_time_bytes,
    }


# ============================================================
# backup / restore
# ============================================================
@app.get("/api/backup")
def create_backup(_=Depends(auth.require_admin)):
    with database.cursor() as cur:
        cur.execute("SELECT * FROM peers")
        peers = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM peer_stats")
        stats_by_id = {r["peer_id"]: dict(r) for r in cur.fetchall()}

    admin_row = _get_admin_row()

    for p in peers:
        s = stats_by_id.get(p["id"], {})
        p["cumulative_rx"] = s.get("cumulative_rx", 0)
        p["cumulative_tx"] = s.get("cumulative_tx", 0)

    return {
        "version": 1,
        "exported_at": database.now(),
        "admin": (
            {"username": admin_row["username"], "password_hash": admin_row["password_hash"]}
            if admin_row
            else None
        ),
        "peers": peers,
    }


@app.post("/api/backup/restore")
def restore_backup(
    payload: dict = Body(...),
    restore_admin: bool = False,
    _=Depends(auth.require_admin),
):
    peers = payload.get("peers", [])
    imported, skipped = 0, 0
    next_account_no = database.next_account_number()  # called outside the lock below

    with database.cursor() as cur:
        cur.execute("SELECT public_key FROM peers")
        existing_keys = {r["public_key"] for r in cur.fetchall()}
        cur.execute("SELECT ip_address FROM peers")
        used_ips = {r["ip_address"] for r in cur.fetchall()}
        cur.execute("SELECT portal_username FROM peers WHERE portal_username IS NOT NULL")
        used_portal_usernames = {r["portal_username"] for r in cur.fetchall()}

        for p in peers:
            pub = p.get("public_key")
            if not pub or pub in existing_keys:
                skipped += 1
                continue

            ip_address = p.get("ip_address")
            if not ip_address or ip_address in used_ips:
                ip_address = database.next_free_ip(_subnet_base(), used_ips)
            used_ips.add(ip_address)
            existing_keys.add(pub)
            # recompute IPv6 from this server's own subnet rather than trusting
            # the source server's value (it may run a different IPv6 range,
            # or not have IPv6 enabled at all)
            ipv6_address = _derive_ipv6(ip_address)

            portal_username = p.get("portal_username")
            if not portal_username or portal_username in used_portal_usernames:
                portal_username = auth.random_username()
                while portal_username in used_portal_usernames:
                    portal_username = auth.random_username()
            used_portal_usernames.add(portal_username)

            cur.execute(
                """INSERT INTO peers
                   (name, public_key, private_key, preshared_key, ip_address, ipv6_address, note,
                    enabled, created_at, expires_at, data_limit_bytes, duration_days,
                    portal_username, portal_password_hash, account_number)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    p.get("name", "restored"),
                    pub,
                    p.get("private_key"),
                    p.get("preshared_key"),
                    ip_address,
                    ipv6_address,
                    p.get("note", ""),
                    1 if p.get("enabled", True) else 0,
                    p.get("created_at", database.now()),
                    p.get("expires_at"),
                    p.get("data_limit_bytes"),
                    p.get("duration_days"),
                    portal_username,
                    p.get("portal_password_hash"),
                    next_account_no,
                ),
            )
            next_account_no += 1
            new_id = cur.lastrowid
            cur.execute(
                "INSERT INTO peer_stats (peer_id, cumulative_rx, cumulative_tx) VALUES (?, ?, ?)",
                (new_id, p.get("cumulative_rx", 0), p.get("cumulative_tx", 0)),
            )
            imported += 1

        if restore_admin and payload.get("admin"):
            a = payload["admin"]
            cur.execute(
                "UPDATE admin SET username=?, password_hash=? WHERE id=1",
                (a["username"], a["password_hash"]),
            )

    reconcile_peers()  # live-add + persist all restored users
    return {"ok": True, "imported": imported, "skipped": skipped}


# ============================================================
# self-service portal (peer-scoped, separate from admin auth)
# ============================================================
@app.post("/api/portal/login")
def portal_login(body: PortalLoginRequest, request: Request):
    key = _client_key("portal", request)
    _check_rate_limit(key)
    with database.cursor() as cur:
        cur.execute("SELECT * FROM peers WHERE portal_username=?", (body.username,))
        row = cur.fetchone()
    if not row or not auth.verify_password(body.password, row["portal_password_hash"] or ""):
        _record_failure(key)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    _record_success(key)
    return {"token": auth.create_peer_token(row["id"])}


@app.get("/api/portal/status")
def portal_status(peer_id: int = Depends(auth.require_peer)):
    live = awg.dump()
    with database.cursor() as cur:
        cur.execute(
            """SELECT p.*, s.cumulative_rx, s.cumulative_tx, s.last_handshake
               FROM peers p LEFT JOIN peer_stats s ON s.peer_id = p.id WHERE p.id=?""",
            (peer_id,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Not found")

    now = database.now()
    live_info = live.get(row["public_key"], {})
    last_handshake = live_info.get("latest_handshake") or row["last_handshake"] or 0
    online = bool(last_handshake) and (now - last_handshake) < config.ONLINE_THRESHOLD_SECONDS
    used = (row["cumulative_rx"] or 0) + (row["cumulative_tx"] or 0)
    remaining_bytes = (row["data_limit_bytes"] - used) if row["data_limit_bytes"] else None
    remaining_days = (row["expires_at"] - now) / 86400 if row["expires_at"] else None

    return {
        "name": row["name"],
        "ip_address": row["ip_address"],
        "enabled": bool(row["enabled"]),
        "online": online,
        "used_bytes": used,
        "data_limit_bytes": row["data_limit_bytes"],
        "remaining_bytes": remaining_bytes,
        "expires_at": row["expires_at"],
        "remaining_days": remaining_days,
        "endpoint": f"{config.SERVER_ENDPOINT}:{config.SERVER_PORT}",
    }


@app.get("/api/portal/config", response_class=PlainTextResponse)
def portal_config(peer_id: int = Depends(auth.require_peer)):
    with database.cursor() as cur:
        cur.execute("SELECT * FROM peers WHERE id=?", (peer_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    return awg.build_client_config(row["private_key"], row["ip_address"], row["preshared_key"], row["ipv6_address"])


@app.get("/api/portal/qr")
def portal_qr(peer_id: int = Depends(auth.require_peer)):
    with database.cursor() as cur:
        cur.execute("SELECT * FROM peers WHERE id=?", (peer_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    conf = awg.build_client_config(row["private_key"], row["ip_address"], row["preshared_key"], row["ipv6_address"])
    import qrcode  # imported lazily: Pillow is only loaded when a QR is really requested
    img = qrcode.make(conf)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


# ============================================================
# background loops
# ============================================================
def _stats_tick():
    if True:
        try:
            live = awg.dump()
            day_str = time.strftime("%Y-%m-%d", time.gmtime())
            day_rx_total = 0
            day_tx_total = 0
            with database.cursor() as cur:
                cur.execute("SELECT id, public_key FROM peers")
                peer_map = {r["public_key"]: r["id"] for r in cur.fetchall()}

                for pubkey, info in live.items():
                    peer_id = peer_map.get(pubkey)
                    if not peer_id:
                        continue
                    cur.execute("SELECT * FROM peer_stats WHERE peer_id=?", (peer_id,))
                    stat = cur.fetchone()
                    if not stat:
                        continue

                    new_rx, new_tx = info["rx"], info["tx"]
                    delta_rx = new_rx if new_rx < stat["last_rx"] else new_rx - stat["last_rx"]
                    delta_tx = new_tx if new_tx < stat["last_tx"] else new_tx - stat["last_tx"]
                    delta_rx = max(delta_rx, 0)
                    delta_tx = max(delta_tx, 0)
                    day_rx_total += delta_rx
                    day_tx_total += delta_tx

                    cur.execute(
                        """UPDATE peer_stats SET
                           cumulative_rx = cumulative_rx + ?,
                           cumulative_tx = cumulative_tx + ?,
                           last_rx = ?, last_tx = ?,
                           last_handshake = ?, updated_at = ?
                           WHERE peer_id = ?""",
                        (
                            delta_rx,
                            delta_tx,
                            new_rx,
                            new_tx,
                            info["latest_handshake"],
                            database.now(),
                            peer_id,
                        ),
                    )

                if day_rx_total or day_tx_total:
                    cur.execute(
                        """INSERT INTO daily_usage (date, rx, tx) VALUES (?, ?, ?)
                           ON CONFLICT(date) DO UPDATE SET
                               rx = rx + excluded.rx, tx = tx + excluded.tx""",
                        (day_str, day_rx_total, day_tx_total),
                    )
        except Exception as e:
            print(f"[stats_loop] error: {e}")


async def stats_loop():
    while True:
        # blocking work (subprocess + sqlite) runs in a worker thread so a slow `awg`
        # call can never freeze the web server's event loop
        await asyncio.to_thread(_stats_tick)
        await asyncio.sleep(config.STATS_POLL_INTERVAL)


def _enforcement_tick():
    changed = False
    now = database.now()
    with database.cursor() as cur:
        cur.execute(
            """SELECT p.*, s.cumulative_rx, s.cumulative_tx
               FROM peers p JOIN peer_stats s ON s.peer_id = p.id
               WHERE p.enabled = 1"""
        )
        for row in cur.fetchall():
            expired = row["expires_at"] and now > row["expires_at"]
            used = (row["cumulative_rx"] or 0) + (row["cumulative_tx"] or 0)
            over_limit = row["data_limit_bytes"] and used > row["data_limit_bytes"]
            if expired or over_limit:
                cur.execute("UPDATE peers SET enabled=0 WHERE id=?", (row["id"],))
                changed = True
    if changed:
        reconcile_peers()  # removes them live AND from the .conf, so a reboot can't resurrect them
    _cleanup_rate_limits()


async def enforcement_loop():
    """Cuts off peers that expired or exceeded their quota."""
    while True:
        try:
            await asyncio.to_thread(_enforcement_tick)
        except Exception as e:
            print(f"[enforcement_loop] error: {e}")
        await asyncio.sleep(30)


# ============================================================
# static frontend (admin panel + self-service portal)
# ============================================================
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

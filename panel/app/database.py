import sqlite3
import time
import threading
from pathlib import Path
from contextlib import contextmanager
from . import config

_lock = threading.Lock()


def get_conn():
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


_conn = get_conn()


def _column_exists(table: str, column: str) -> bool:
    cur = _conn.execute(f"PRAGMA table_info({table})")
    return any(row["name"] == column for row in cur.fetchall())


def init_db():
    with _lock:
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS peers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                public_key TEXT UNIQUE NOT NULL,
                private_key TEXT NOT NULL,
                preshared_key TEXT NOT NULL,
                ip_address TEXT UNIQUE NOT NULL,
                note TEXT DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                expires_at INTEGER,
                data_limit_bytes INTEGER
            );

            CREATE TABLE IF NOT EXISTS peer_stats (
                peer_id INTEGER PRIMARY KEY REFERENCES peers(id) ON DELETE CASCADE,
                cumulative_rx INTEGER NOT NULL DEFAULT 0,
                cumulative_tx INTEGER NOT NULL DEFAULT 0,
                last_rx INTEGER NOT NULL DEFAULT 0,
                last_tx INTEGER NOT NULL DEFAULT 0,
                last_handshake INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS admin (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                username TEXT NOT NULL,
                password_hash TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS server_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                endpoint TEXT
            );

            CREATE TABLE IF NOT EXISTS daily_usage (
                date TEXT PRIMARY KEY,
                rx INTEGER NOT NULL DEFAULT 0,
                tx INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS ip_geo_cache (
                ip TEXT PRIMARY KEY,
                country TEXT,
                city TEXT,
                lat REAL,
                lon REAL,
                updated_at INTEGER NOT NULL
            );
            """
        )

        # --- migrations for older installs ---
        migrations = [
            ("peers", "duration_days", "INTEGER"),
            ("peers", "portal_username", "TEXT"),
            ("peers", "portal_password_hash", "TEXT"),
            ("peers", "ipv6_address", "TEXT"),
            ("peers", "account_number", "INTEGER"),
            ("peers", "bandwidth_limit_kbps", "INTEGER"),
            ("ip_geo_cache", "lat", "REAL"),
            ("ip_geo_cache", "lon", "REAL"),
        ]
        for table, col, col_type in migrations:
            if not _column_exists(table, col):
                _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")

        _conn.commit()

        # backfill account_number for peers created before this field existed
        rows = _conn.execute(
            "SELECT id FROM peers WHERE account_number IS NULL ORDER BY created_at ASC"
        ).fetchall()
        if rows:
            next_num = _next_account_number_locked()
            for r in rows:
                _conn.execute("UPDATE peers SET account_number=? WHERE id=?", (next_num, r["id"]))
                next_num += 1
            _conn.commit()

        # seed admin row from panel.env on first run
        row = _conn.execute("SELECT * FROM admin WHERE id=1").fetchone()
        if not row and config.ADMIN_PASSWORD_HASH:
            _conn.execute(
                "INSERT INTO admin (id, username, password_hash) VALUES (1, ?, ?)",
                (config.ADMIN_USERNAME, config.ADMIN_PASSWORD_HASH),
            )
            _conn.commit()

        # load a saved endpoint override (set from the panel UI) on top of panel.env
        settings_row = _conn.execute("SELECT * FROM server_settings WHERE id=1").fetchone()
        if settings_row and settings_row["endpoint"]:
            config.SERVER_ENDPOINT = settings_row["endpoint"]


def _next_account_number_locked() -> int:
    """Caller must already hold _lock. Starts the sequence at 100."""
    row = _conn.execute("SELECT MAX(account_number) AS m FROM peers").fetchone()
    if row and row["m"] is not None:
        return max(100, row["m"] + 1)
    return 100


def next_account_number() -> int:
    with _lock:
        return _next_account_number_locked()


@contextmanager
def cursor():
    with _lock:
        cur = _conn.cursor()
        try:
            yield cur
            _conn.commit()
        finally:
            cur.close()


def next_free_ip(subnet_base: str, used_ips: set) -> str:
    """subnet_base like 10.29.29 - starts at .2 (.1 is the server)"""
    for i in range(2, 255):
        candidate = f"{subnet_base}.{i}"
        if candidate not in used_ips:
            return candidate
    raise RuntimeError("No free IP addresses left in subnet")


def now() -> int:
    return int(time.time())

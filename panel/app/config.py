"""
تنظیمات پنل - همه چیز از فایل /etc/awg-panel/panel.env خونده می‌شه
(این فایل توسط اسکریپت نصب ساخته می‌شه)
"""
import os
from pathlib import Path

ENV_FILE = Path("/etc/awg-panel/panel.env")


def _load_env_file(path: Path) -> dict:
    data = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip().strip('"').strip("'")
    return data


_env = {**_load_env_file(ENV_FILE), **os.environ}


def get(key: str, default=None):
    return _env.get(key, default)


# --- مسیرها و تنظیمات اصلی ---
INTERFACE = get("AWG_INTERFACE", "awg0")
WG_CONF_PATH = Path(get("AWG_CONF_PATH", f"/etc/amnezia/amneziawg/{INTERFACE}.conf"))
SERVER_SUBNET = get("AWG_SUBNET", "10.29.29.0/24")
SERVER_ADDRESS = get("AWG_ADDRESS", "10.29.29.1/24")
SERVER_ENDPOINT = get("AWG_ENDPOINT", "")  # IP یا دامنه سرور
SERVER_PORT = int(get("AWG_PORT", "51820"))
SERVER_PUBLIC_KEY = get("AWG_SERVER_PUBLIC_KEY", "")
DNS_SERVERS = get("AWG_DNS", "1.1.1.1, 8.8.8.8")

# پارامترهای مبهم‌سازی AmneziaWG برای عبور از DPI
AWG_JC = get("AWG_JC", "4")
AWG_JMIN = get("AWG_JMIN", "40")
AWG_JMAX = get("AWG_JMAX", "70")
AWG_S1 = get("AWG_S1", "0")
AWG_S2 = get("AWG_S2", "0")
AWG_H1 = get("AWG_H1", "1")
AWG_H2 = get("AWG_H2", "2")
AWG_H3 = get("AWG_H3", "3")
AWG_H4 = get("AWG_H4", "4")
CLIENT_MTU = get("AWG_CLIENT_MTU", "1280")

# panel (admin credentials are now stored in the DB so they can be changed live
# from the panel UI; these are only used to seed the DB on first run)
DB_PATH = get("PANEL_DB_PATH", "/etc/awg-panel/panel.db")
JWT_SECRET = get("PANEL_JWT_SECRET", "change-me")
PANEL_PORT = int(get("PANEL_PORT", "8787"))
ADMIN_USERNAME = get("PANEL_ADMIN_USER", "admin")
ADMIN_PASSWORD_HASH = get("PANEL_ADMIN_PASSWORD_HASH", "")
STATS_POLL_INTERVAL = float(get("PANEL_STATS_INTERVAL", "4"))
ONLINE_THRESHOLD_SECONDS = int(get("PANEL_ONLINE_THRESHOLD", "150"))

# IPv6 (dual-stack) - only meaningful if the server has a real IPv6 uplink;
# install.sh auto-detects this and sets AWG_ENABLE_IPV6 accordingly.
ENABLE_IPV6 = get("AWG_ENABLE_IPV6", "0") == "1"
SERVER_SUBNET6 = get("AWG_SUBNET6", "fd42:29:29::/64")
SERVER_ADDRESS6 = get("AWG_ADDRESS6", "fd42:29:29::1/64")

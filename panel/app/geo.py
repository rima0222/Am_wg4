"""
Best-effort IP geolocation for the admin panel's "source IP" column.

Uses ip-api.com's free batch endpoint (no key required; http only on the
free tier - https needs a paid plan). Results are cached in the
ip_geo_cache DB table so this only ever runs for IPs we haven't resolved
recently. The admin panel polls /api/peers every few seconds, so this must
never block that loop on a flaky third-party API - every failure mode here
is swallowed and just means "no location shown", never a broken panel.
"""
import json
import urllib.request

BATCH_URL = "http://ip-api.com/batch?fields=status,country,city,lat,lon,query"
MAX_BATCH = 100
TIMEOUT = 3


def lookup_batch(ips: list) -> dict:
    """Returns {ip: {"country": ..., "city": ..., "lat": ..., "lon": ...}} for
    IPs it could resolve. lat/lon let the UI link straight to a map instead
    of a third-party IP-lookup page, which some networks block outright."""
    if not ips:
        return {}
    results = {}
    try:
        for i in range(0, len(ips), MAX_BATCH):
            chunk = ips[i : i + MAX_BATCH]
            payload = json.dumps([{"query": ip} for ip in chunk]).encode()
            req = urllib.request.Request(
                BATCH_URL, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode())
            for row in data:
                if row.get("status") == "success" and row.get("query"):
                    results[row["query"]] = {
                        "country": row.get("country") or None,
                        "city": row.get("city") or None,
                        "lat": row.get("lat"),
                        "lon": row.get("lon"),
                    }
    except Exception as e:
        print(f"[geo] lookup failed: {e}")
    return results

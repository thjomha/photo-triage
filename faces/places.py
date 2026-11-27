"""Turn GPS positions into place names ("Jasper, Alberta, Canada") using OpenStreetMap's Nominatim.

  python -m faces.places

Optional. It sends rounded coordinates (about 1 km) to nominatim.openstreetmap.org, one request per
second as their usage policy asks, and remembers every answer so each area is only looked up once.
Photos without GPS can still get a place: set one for a whole folder in the app (Search page).
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request

from . import db, search

URL = "https://nominatim.openstreetmap.org/reverse"
UA = "photo-triage/1.0 (personal photo organiser)"


def place_name(address: dict) -> str:
    town = next((address[k] for k in ("city", "town", "village", "municipality", "hamlet", "suburb", "county")
                 if address.get(k)), None)
    parts = [town, address.get("state") or address.get("region"), address.get("country")]
    return ", ".join(p for p in parts if p)


def fetch_nominatim(lat: float, lon: float) -> str:
    q = urllib.parse.urlencode({"format": "jsonv2", "lat": f"{lat:.4f}", "lon": f"{lon:.4f}",
                                "zoom": 10, "accept-language": "en"})
    req = urllib.request.Request(f"{URL}?{q}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    return place_name(data.get("address") or {}) or (data.get("display_name") or "")


def run(conn, fetch=fetch_nominatim, delay: float = 1.1, limit: int = 0) -> int:
    cells = conn.execute(
        "SELECT round(lat, 2) AS la, round(lon, 2) AS lo, COUNT(*) AS n FROM photos "
        "WHERE lat IS NOT NULL AND place IS NULL GROUP BY la, lo ORDER BY n DESC").fetchall()
    if limit:
        cells = cells[:limit]
    updated = 0
    for i, c in enumerate(cells, 1):
        key = f"{c['la']:.2f},{c['lo']:.2f}"
        row = conn.execute("SELECT place FROM geocache WHERE key = ?", (key,)).fetchone()
        if row is None:
            try:
                place = fetch(c["la"], c["lo"])
            except Exception as e:
                print(f"Lookup failed ({e}); stopping. Run again later to continue.", file=sys.stderr)
                break
            conn.execute("INSERT OR REPLACE INTO geocache(key, place) VALUES (?, ?)", (key, place))
            conn.commit()
            time.sleep(delay)
        else:
            place = row["place"]
        if place:
            paths = [r[0] for r in conn.execute(
                "SELECT path FROM photos WHERE round(lat, 2) = ? AND round(lon, 2) = ? AND place IS NULL",
                (c["la"], c["lo"]))]
            conn.execute("UPDATE photos SET place = ? WHERE round(lat, 2) = ? AND round(lon, 2) = ? AND place IS NULL",
                         (place, c["la"], c["lo"]))
            conn.commit()
            search.rebuild(conn, paths)
            updated += len(paths)
        print(f"[{i}/{len(cells)}] {key} -> {place or '(unknown)'}", flush=True)
    return updated


def main() -> int:
    conn = db.connect(init=True)
    n = run(conn)
    print(f"Added a place to {n} photos.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
protected_areas.py
------------------
Is the fire inside/next to a protected area? (OpenStreetMap via Overpass — free without a key)

Uses the `around` filter not a full square, so the request is light and fast (seconds), and cached for a full day
because protected area boundaries don't change.

Usage:
    from protected_areas import lookup
    lookup(27.72, 34.25)   # → {'name': 'Ras Muhammad National Park', 'designation': ..., 'distance_km': 2.4}
"""
from __future__ import annotations

import json
import math
import os
import time
import urllib.parse
import urllib.request
from typing import Optional

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.osm.jp/api/interpreter",
]
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "protected_areas_cache.json")
HEADERS = {"User-Agent": "FireLink-NASA-SpaceApps-2026"}   # Without it Overpass rejects (406)
CACHE: dict = {}
TTL_SECONDS = 86400
QUERY = """[out:json][timeout:{timeout}];
(
  way(around:{radius},{lat},{lon})["boundary"~"protected_area|national_park"];
  relation(around:{radius},{lat},{lon})["boundary"~"protected_area|national_park"];
  way(around:{radius},{lat},{lon})["leisure"="nature_reserve"];
  relation(around:{radius},{lat},{lon})["leisure"="nature_reserve"];
);
out tags center 8;"""


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 1)


def _post(query: str, timeout: float = 12.0) -> dict:
    """POST to the first working mirror. Overpass rejects rapid repeated requests, so we sleep a bit between attempts."""
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last = None
    for attempt, url in enumerate(OVERPASS_MIRRORS):
        if attempt:
            time.sleep(min(6, 2 ** (attempt - 1)))
        try:
            req = urllib.request.Request(url, data=data, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return json.loads(res.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - mirror down/request limit
            last = e
    raise last if last else RuntimeError("Overpass unavailable")


def _load_disk_cache() -> dict:
    try:
        with open(CACHE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_disk_cache() -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump({f"{k[0]},{k[1]},{k[2]}": v[1] for k, v in CACHE.items()}, fh, ensure_ascii=False)
    except OSError:
        pass


def _describe(tags: dict) -> str:
    """Short description: 'national park' / 'nature reserve' / 'protected area'."""
    return (tags.get("protect_class_label") or tags.get("designation") or tags.get("protection_title")
            or ("nature reserve" if tags.get("leisure") == "nature_reserve" else "protected area"))


def lookup(lat: float, lon: float, radius_m: int = 8000,
           post=None, ttl: int = TTL_SECONDS) -> Optional[dict]:
    """
    Nearest reserve within `radius_m`. Returns dict or None (no reserve/service not available).
    """
    cache_key = (round(lat, 2), round(lon, 2), radius_m)
    hit = CACHE.get(cache_key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    disk = _load_disk_cache()
    disk_key = f"{cache_key[0]},{cache_key[1]},{cache_key[2]}"
    if disk_key in disk:                      # Cached result from previous run (remains valid — bounds don't change)
        CACHE[cache_key] = (time.time(), disk[disk_key])
        return disk[disk_key]

    query = QUERY.format(timeout=int(min(20, max(5, radius_m / 2000))), radius=radius_m,
                         lat=round(lat, 4), lon=round(lon, 4))
    try:
        data = (post or _post)(query)
    except Exception:  # noqa: BLE001
        return None

    best = None
    for element in (data or {}).get("elements", []):
        tags = element.get("tags") or {}
        name = tags.get("name") or tags.get("name:en")
        if not name:
            continue
        center = element.get("center") or {}
        if center.get("lat") is None:
            continue
        distance = haversine_km(lat, lon, center["lat"], center["lon"])
        if best is None or distance < best["distance_km"]:
            best = {"name": name, "designation": _describe(tags),
                    "distance_km": distance, "osm": f"{element.get('type')}/{element.get('id')}"}
    CACHE[cache_key] = (time.time(), best)
    if best:
        _save_disk_cache()
    return best


def clear_cache() -> None:
    CACHE.clear()

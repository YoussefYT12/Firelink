""" 
weather.py
----------
Wind direction and speed at a fire location (OpenWeather) so we can say where the fire is moving.

The key (free): https://openweathermap.org/api → put in .env:
    OPENWEATHER_API_KEY=xxxxxxxx

Without a key, the project runs normally — it just returns `None`, and alerts appear without the wind line.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Optional

from config import _read_env_file

API = "https://api.openweathermap.org/data/2.5/weather"
CACHE: dict = {}
TTL_SECONDS = 1800          # Half an hour — the wind doesn't change every minute
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def api_key(explicit: Optional[str] = None) -> str:
    value = explicit or os.environ.get("OPENWEATHER_API_KEY") or _read_env_file().get("OPENWEATHER_API_KEY") or ""
    return str(value).strip()


def compass(deg: float) -> str:
    """Degree → direction in letters (16 directions)."""
    return COMPASS[int((float(deg) % 360) / 22.5 + 0.5) % 16]


def _fetch(lat: float, lon: float, key: str, timeout: float = 6.0) -> dict:
    query = urllib.parse.urlencode({"lat": round(lat, 3), "lon": round(lon, 3),
                                    "appid": key, "units": "metric"})
    req = urllib.request.Request(f"{API}?{query}", headers={"User-Agent": "FireLink"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def clear_cache() -> None:
    CACHE.clear()


def get_wind(lat: float, lon: float, key: Optional[str] = None,
             fetch=None, ttl: int = TTL_SECONDS) -> Optional[dict]:
    """
    Returns {from_deg, from, toward_deg, toward, speed_kmh, place} or None.
    `toward` = the direction the fire is moving toward (opposite the wind source direction).
    """
    key = api_key(key)
    if not key:
        return None
    cache_key = (round(lat, 2), round(lon, 2))
    hit = CACHE.get(cache_key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        data = (fetch or _fetch)(lat, lon, key)
    except Exception:  # noqa: BLE001 - network/wrong key/rate limit
        return None
    wind = data.get("wind") or {}
    deg = wind.get("deg")
    if deg is None:
        return None
    speed = wind.get("speed") or 0.0
    toward = (float(deg) + 180.0) % 360.0
    result = {
        "from_deg": round(float(deg), 1), "from": compass(deg),
        "toward_deg": round(toward, 1), "toward": compass(toward),
        "speed_kmh": round(float(speed) * 3.6, 1),
        "place": data.get("name") or None,
    }
    CACHE[cache_key] = (time.time(), result)
    return result


def drift_sentence(wind: Optional[dict]) -> Optional[str]:
    """Ready-to-display phrase: 'wind 18 km/h from N — fire likely spreads towards S'."""
    if not wind:
        return None
    return (f"wind {wind['speed_kmh']:g} km/h from {wind['from']} — "
            f"fire likely spreads towards {wind['toward']}")

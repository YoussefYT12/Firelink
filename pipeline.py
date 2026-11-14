"""
pipeline.py
-----------
The one place that builds \"payload\" (payload) that the map displays:
  - fire points (GeoJSON) after reconciliation between VIIRS and MODIS
  - full counts for each classification
  - alerts list: each hazard cluster (High/Critical) with region/country name and reason

Used by: the local server (/api/query), demo.py, and run_all.py.
"""

import csv
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date, datetime, timedelta, timezone


def _utc_today():
    return datetime.now(timezone.utc).date()


def _date_of(text):
    try:
        return _date.fromisoformat(str(text)[:10])
    except (TypeError, ValueError):
        return None


_PUBLISHED = {"at": 0.0, "day": None}


def published_through(ttl: int = 1800):
    """
    The most recent day NASA actually published in NRT sources (a single small query to data_availability).
    Returns None if the service is unavailable, in which case we fall back to the old method (last 3 days).
    """
    now = time.time()
    if _PUBLISHED["day"] and now - _PUBLISHED["at"] < ttl:
        return _PUBLISHED["day"]
    try:
        import history as history_mod
        from firms_client import get_map_key

        avail = history_mod.get_availability(get_map_key())
        days = [hi for name, (_lo, hi) in avail.items() if name.endswith("_NRT")]
        day = max(days) if days else None
    except Exception:  # noqa: BLE001  (network/key/service down)
        day = None
    _PUBLISHED.update(at=now, day=day)
    return day
from typing import List, Optional

from firms_client import fetch_viirs_and_modis_auto
from reconciliation import reconcile, FireStatus
from ai_risk import assess_risk, classify_fire_type
from landcover import query_land_cover_points
from clustering import cluster_confirmed_fires, reverse_geocode

MAX_GEOJSON_FEATURES = 60000   # above this number we export only Confirmed + Likely
MAX_ALERTS = 10                # maximum number of alerts in the list
_LEVEL_RANK = {"High": 1, "Critical": 2}

_CACHE = {}                    # (bbox, date, days) -> (save time, payload)
_CACHE_MAX = 8
_LIVE_TTL_SECONDS = 600        # live data updates at most every 10 minutes


def results_to_geojson(results: list, max_features: int = MAX_GEOJSON_FEATURES) -> dict:
    """Converts the reconciliation results to GeoJSON, along with the full counts for each classification."""
    counts = {"confirmed": 0, "likely": 0, "single": 0, "weak": 0}
    for r in results:
        if r.status == FireStatus.CONFIRMED:
            counts["confirmed"] += 1
        elif r.status == FireStatus.LIKELY:
            counts["likely"] += 1
        elif r.status == FireStatus.SINGLE_SOURCE:
            counts["single"] += 1
        else:
            counts["weak"] += 1

    truncated = len(results) > max_features
    if truncated:
        results = [r for r in results if r.status in (FireStatus.CONFIRMED, FireStatus.LIKELY)]

    features = []
    for r in results:
        detection = r.viirs_detection or r.modis_detection
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [round(r.longitude, 4), round(r.latitude, 4)],
                },
                "properties": {
                    "status": r.status.value,
                    "confidence_score": r.confidence_score,
                    "distance_km": r.distance_km,
                    "time_diff_hours": r.time_diff_hours,
                    "timestamp": detection.timestamp.isoformat() if detection else None,
                    "frp_mw": round(detection.frp, 2) if detection else None,
                    "viirs": r.viirs_detection is not None,
                    "brightness": round(detection.brightness, 1) if detection and detection.brightness else None,
                    "modis": r.modis_detection is not None,
                },
            }
        )

    return {
        "type": "FeatureCollection",
        "counts": counts,
        "truncated": truncated,
        "features": features,
    }



def _enrich_alerts(alerts: list, enabled: bool = True) -> None:
    """
    Adds to each alert: wind direction (OpenWeather) and nearest nature reserve (OpenStreetMap).
    Everything is optional and returns gracefully if the key is missing or the service is down —
    so the map never stops because of an external service.
    """
    if not enabled or not alerts or os.environ.get("FIRELINK_ENRICH", "1") == "0":
        return

    def add_wind(alert):
        try:
            from weather import get_wind, drift_sentence

            wind = get_wind(alert["latitude"], alert["longitude"])
            if wind:
                alert["wind"] = wind
                alert["wind_text"] = drift_sentence(wind)
        except Exception:  # noqa: BLE001
            pass

    def add_protected(alert):
        try:
            from protected_areas import lookup

            area = lookup(alert["latitude"], alert["longitude"])
            if area:
                alert["protected_area"] = area
        except Exception:  # noqa: BLE001
            pass

    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(add_wind, alerts))
        # Reserves: only return 5 alerts (Overpass doesn't like many requests)
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(add_protected, alerts[:5]))
    except Exception:  # noqa: BLE001
        pass


def compute_alerts(results: list, max_alerts: int = MAX_ALERTS, geocode: bool = True):
    """
    Aggregates confirmed/possible fires into geographic clusters, and evaluates each cluster with the local
    expert system (ai_risk.py), and returns the most dangerous clusters (High/Critical) by
    region/country. Returns (list of alerts, total number of dangerous clusters).
    """
    flagged = []
    for c in cluster_confirmed_fires(results):
        assessment = assess_risk(
            {
                "confirmed_count": c.confirmed_count,
                "likely_count": c.likely_count,
                "total_detections": c.total_count,
                "average_frp": c.avg_frp,
                "max_frp": c.max_frp,
                "confirmed_growth": None,
            }
        )
        if assessment.risk_level in _LEVEL_RANK:
            flagged.append((c, assessment))

    flagged.sort(
        key=lambda x: (_LEVEL_RANK[x[1].risk_level], x[0].confirmed_count, x[0].max_frp),
        reverse=True,
    )

    top = flagged[:max_alerts]

    def _land_cover_for(cluster):
        try:
            return query_land_cover_points(cluster.hotspots or [(cluster.latitude, cluster.longitude)])
        except Exception:  # Any error in the land source shouldn't stop alerts
            return None

    # Land type at the strongest fire points: in parallel so 10 alerts aren't slow
    if geocode and top:
        with ThreadPoolExecutor(max_workers=6) as pool:
            land_results = list(pool.map(_land_cover_for, [c for c, _ in top]))
    else:
        land_results = [None] * len(top)

    alerts = []
    for (c, a), land_cover_result in zip(top, land_results):
        place = reverse_geocode(c.latitude, c.longitude) if geocode else None

        if land_cover_result:
            fire_type, fire_type_source = land_cover_result
        else:
            fire_type = classify_fire_type(c.avg_frp, c.max_frp)
            fire_type_source = "estimated from fire intensity (land-cover lookup unavailable)"

        alerts.append(
            {
                "level": a.risk_level,
                "fire_type": fire_type,
                "fire_type_source": fire_type_source,
                "place": place or f"{c.latitude}, {c.longitude}",
                "latitude": c.latitude,
                "longitude": c.longitude,
                "confirmed": c.confirmed_count,
                "likely": c.likely_count,
                "avg_frp": c.avg_frp,
                "max_frp": c.max_frp,
                "summary": a.summary,
                "reasoning": a.reasoning,
                "action": a.recommended_action,
            }
        )
    _enrich_alerts(alerts, enabled=geocode)
    return alerts, len(flagged)


def build_payload(results: list, meta: Optional[dict] = None) -> dict:
    payload = results_to_geojson(results)
    alerts, total = compute_alerts(results)
    payload["alerts"] = alerts
    payload["alerts_total"] = total
    info = dict(meta or {})
    info["generated_at"] = datetime.now(timezone.utc).isoformat()
    payload["meta"] = info
    return payload


def write_payload(payload: dict, path: str = "firelink_output.geojson"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))


CSV_COLUMNS = ["latitude", "longitude", "status", "confidence_score", "distance_km",
               "time_diff_hours", "timestamp_utc", "frp_mw", "brightness_k",
               "detected_by_viirs", "detected_by_modis"]


def payload_to_csv_rows(payload: dict):
    """CSV rows for discovered points (same points that are in the GeoJSON). Returns (header, rows)."""
    rows = []
    for f in payload.get("features", []):
        lon, lat = f["geometry"]["coordinates"]
        p = f["properties"]
        rows.append([lat, lon, p.get("status"), p.get("confidence_score"), p.get("distance_km"),
                     p.get("time_diff_hours"), p.get("timestamp"), p.get("frp_mw"), p.get("brightness"),
                     p.get("viirs"), p.get("modis")])
    return CSV_COLUMNS, rows


def write_csv(payload: dict, path: str = "firelink_output.csv"):
    """Writes discovered points CSV (UTF-8 with BOM so Excel opens it correctly)."""
    header, rows = payload_to_csv_rows(payload)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def print_alerts(alerts: List[dict], total: Optional[int] = None):
    """Prints alerts to the terminal clearly (country/region + reason + recommendation)."""
    if not alerts:
        print("\nNo High/Critical alerts for this period.")
        return
    total = total if total is not None else len(alerts)
    print(f"\n{'=' * 64}")
    print(f"  {total} LOCATION(S) FLAGGED - showing top {len(alerts)}")
    print(f"{'=' * 64}")
    for a in alerts:
        print(f"\n*** [{a['level'].upper()}] {a['place']} — {a['fire_type']} ({a['fire_type_source']})")
        print(f"    Coordinates: {a['latitude']}, {a['longitude']}")
        print(f"    {a['summary']}")
        print(f"    Why:    {a['reasoning']}")
        print(f"    Action: {a['action']}")
    print(f"\n{'=' * 64}\n")


def run_pipeline(bbox: str = "world", date: Optional[str] = None, days: int = 1, use_cache: bool = True) -> dict:
    """Fetches data from NASA, reconciles, and returns the full package (points + alerts)."""
    requested_date = date
    # Live mode: ask NASA for the last day it actually published (small, fast request) instead of assuming \"today\"
    if date is None:
        published = published_through()
        if published:
            date = published.isoformat()

    key = (bbox, date, days)
    now = time.time()
    if use_cache and key in _CACHE:
        saved_at, payload = _CACHE[key]
        if date is not None or now - saved_at < _LIVE_TTL_SECONDS:
            return payload

    viirs, modis = fetch_viirs_and_modis_auto(bbox, date=date, day_range=days)
    note, effective_date, lag_days = None, date, None

    # NASA doesn't publish data "today" immediately: the latest available day is often yesterday or earlier.
    # Without this step the map says "no fires" when in fact "no data".
    recent = date is None or (_date_of(date) or _utc_today()) >= _utc_today() - timedelta(days=10)
    if recent and not viirs and not modis:
        # The requested day is still not published: try the previous day (one quick request),
        # and then the last 3 days if still empty.
        base = _date_of(date) or _utc_today()
        prev = (base - timedelta(days=1)).isoformat()
        viirs, modis = fetch_viirs_and_modis_auto(bbox, date=prev, day_range=1)
        if viirs or modis:
            effective_date = prev
        else:
            viirs, modis = [], []
            fb_viirs, fb_modis = fetch_viirs_and_modis_auto(bbox, date=None, day_range=3)
            newest = max((d.acq_date for d in fb_viirs + fb_modis), default=None)
            if newest:
                viirs = [d for d in fb_viirs if d.acq_date == newest]
                modis = [d for d in fb_modis if d.acq_date == newest]
                effective_date = newest

    # If the data returned is from a day older than requested, state it plainly (publication delay, not "no fires").
    newest = max((d.acq_date for d in viirs + modis), default=None)
    if newest:
        effective_date = newest
        lag_days = (_utc_today() - _date_of(newest)).days
        if lag_days > 0 and effective_date != requested_date:
            note = (f"NASA has not published {requested_date or 'today'} yet - "
                    f"showing the newest day available: {effective_date}.")

    today_iso = _utc_today().isoformat()
    latest_complete = (_utc_today() - timedelta(days=1)).isoformat()
    results = reconcile(viirs, modis)
    payload = build_payload(
        results,
        {"bbox": bbox, "date": effective_date, "requested_date": date, "days": days,
         "viirs": len(viirs), "modis": len(modis),
         "data_lag_days": lag_days, "note": note,
         # The current day is built up gradually with each satellite pass, so its count is always incomplete
         "partial_day": effective_date == today_iso,
         "latest_complete_day": latest_complete},
    )

    if len(_CACHE) >= _CACHE_MAX:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)
    _CACHE[key] = (now, payload)
    return payload

"""
calendar_activity.py
--------------------
Fire activity calendar for a specific area (this is what the challenge prompt asks for): we fetch
VIIRS and MODIS data for each day in the period, reconcile them, and compute for each day:
  - number of events (distinct fires), and how many are confirmed/possible/single-source/weak match
  - total FRP (heat intensity)

Then we flag:
  - "unusual" days: a day whose activity is much higher than usual for the same area
  - "critical periods": two or more consecutive days of elevated activity

Detection method (transparent, not a black box): compare each day to the other days in the same period
using the median (median) and the median absolute deviation (MAD) - they resist outliers better than the plain mean. So "unusual" means "unusual relative to
the rest of the period you chose", so the longer the period the more accurate the comparison.
"""

import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from firms_client import fetch_viirs_and_modis_auto
from reconciliation import reconcile, FireStatus
import burned_area

CHUNK_DAYS = 5            # NASA limit for archive sources (SP) = 5 days per request
MAX_AREA_SQ_DEG = 300.0   # Maximum area (~17x17 degrees): beyond that the data becomes huge
MAX_ROWS_TOTAL = 2_000_000
MIN_DAYS, MAX_DAYS = 14, 90
MIN_DAYS_FOR_BASELINE = 14

# detection thresholds
ELEVATED_Z, ELEVATED_RATIO = 2.0, 1.5     # high: z >= 2 and >= 1.5x the median
UNUSUAL_Z, UNUSUAL_RATIO = 3.5, 2.0       # Unusual: z >= 3.5 and >= 2x median
MIN_EVENTS_TO_FLAG = 10                   # Don't train on days with a very small number of fires

_CACHE = {}
_CACHE_MAX = 6
_LIVE_TTL_SECONDS = 1800


def bbox_area_sq_deg(bbox: str) -> float:
    west, south, east, north = (float(x) for x in bbox.split(","))
    return max(0.0, east - west) * max(0.0, north - south)


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


# ----------------------------------------------------------------- analysis (pure)
def _robust_z(values: list):
    """Returns (median, scale_info, z function). MAD first, and if zero use mean absolute deviation."""
    median = statistics.median(values)
    deviations = [abs(v - median) for v in values]
    mad = statistics.median(deviations)
    if mad > 0:
        return median, mad, lambda x: 0.6745 * (x - median) / mad
    mean_ad = sum(deviations) / len(deviations)
    if mean_ad > 0:
        return median, mean_ad, lambda x: (x - median) / (1.253314 * mean_ad)
    return median, 0.0, lambda x: 0.0


def analyze_days(daily: dict, partial_dates=()):
    """
    daily: {"2025-11-05": {"events":..,"confirmed":..,...} or None if the day has no data}
    Returns (days_list, periods, baseline, notes).
    """
    notes = []
    dates = sorted(daily)
    partial = set(partial_dates)

    usable = [daily[d]["events"] for d in dates if daily[d] is not None and d not in partial]
    baseline = None
    z_of = None
    if len(usable) >= MIN_DAYS_FOR_BASELINE:
        median, scale, z_of = _robust_z(usable)
        baseline = {"median": median, "spread": round(scale, 2), "days_used": len(usable)}
    else:
        notes.append(
            f"Only {len(usable)} day(s) with data - need at least {MIN_DAYS_FOR_BASELINE} "
            "to judge what is unusual, so no days were flagged."
        )

    days_list = []
    for d in dates:
        s = daily[d]
        if s is None:
            days_list.append({"date": d, "status": "nodata", "events": None, "burned_km2": None})
            continue
        entry = dict(s)
        entry["date"] = d
        entry["frp_sum"] = round(s.get("frp_sum", 0.0), 1)
        entry["partial"] = d in partial
        entry["score"] = None
        entry["status"] = "partial" if d in partial else "normal"
        if z_of is not None and d not in partial:
            x = s["events"]
            z = z_of(x)
            entry["score"] = round(z, 2)
            floor = max(baseline["median"], 1)
            if x >= MIN_EVENTS_TO_FLAG and z >= UNUSUAL_Z and x >= UNUSUAL_RATIO * floor:
                entry["status"] = "unusual"
            elif x >= MIN_EVENTS_TO_FLAG and z >= ELEVATED_Z and x >= ELEVATED_RATIO * floor:
                entry["status"] = "elevated"
        days_list.append(entry)

    # Critical periods: two consecutive days (actually consecutive dates) or more with elevated/unusual activity
    periods, run = [], []

    def close_run():
        if len(run) >= 2:
            peak = max(run, key=lambda e: e["events"])
            periods.append(
                {
                    "start": run[0]["date"],
                    "end": run[-1]["date"],
                    "days": len(run),
                    "peak_date": peak["date"],
                    "peak_events": peak["events"],
                    "total_events": sum(e["events"] for e in run),
                    "unusual_days": sum(1 for e in run if e["status"] == "unusual"),
                    "burned_km2": round(sum(e.get("burned_km2") or 0 for e in run), 1),
                }
            )

    prev_date = None
    for e in days_list:
        hot = e["status"] in ("elevated", "unusual")
        consecutive = prev_date is not None and (
            date.fromisoformat(e["date"]) - date.fromisoformat(prev_date) == timedelta(days=1)
        )
        if hot and (not run or consecutive):
            run.append(e)
        else:
            close_run()
            run = [e] if hot else []
        prev_date = e["date"]
    close_run()

    return days_list, periods, baseline, notes


# ----------------------------------------------------------------- data collection
def _aggregate_chunk(results) -> dict:
    per_day = {}
    for r in results:
        det = r.viirs_detection or r.modis_detection
        s = per_day.setdefault(
            det.acq_date,
            {"events": 0, "confirmed": 0, "likely": 0, "single": 0, "weak": 0, "frp_sum": 0.0,
             "_cells": {}},
        )
        s["events"] += 1
        kind = {
            FireStatus.CONFIRMED: "confirmed",
            FireStatus.LIKELY: "likely",
            FireStatus.SINGLE_SOURCE: "single",
        }.get(r.status, "weak")
        s[kind] += 1
        s["frp_sum"] += det.frp or 0.0
        # Burned area estimate: cell ≈1 km per observation, once per day
        # If both satellites saw the same fire, take the larger (MODIS 1 km²) so we don't underestimate
        burned_area.add(s["_cells"], "MODIS" if r.modis_detection else "VIIRS",
                        getattr(det, "latitude", None), getattr(det, "longitude", None))
    for s in per_day.values():                    # Cells are not stored, we convert them to numbers
        s.update(burned_area.finalize(s.pop("_cells")))
    return per_day


def _fetch_chunk(bbox: str, chunk_start: str, n_days: int):
    """Gets and reconciles a slice of days (with two attempts). Returns (per_day, rows)."""
    last_error = None
    for _ in range(2):
        try:
            viirs, modis = fetch_viirs_and_modis_auto(bbox, date=chunk_start, day_range=n_days)
            # Matching is only within the slice (a pair on the boundary of two slices is very rare and negligible)
            return _aggregate_chunk(reconcile(viirs, modis)), len(viirs) + len(modis)
        except Exception as e:  # noqa: BLE001
            last_error = e
    raise last_error


def _check_bbox(bbox: str):
    """Ensures the box west,south,east,north has logical coordinates before calling NASA."""
    try:
        w, s, e, n = (float(x) for x in bbox.split(","))
    except (ValueError, AttributeError):
        raise ValueError("Invalid area. Expected west,south,east,north.")
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise ValueError(
            "Invalid area: NASA expects west,south,east,north with "
            "-180<=west<east<=180 and -90<=south<north<=90."
        )


def build_calendar(bbox: str, end_date=None, days: int = 30) -> dict:
    if bbox == "world":
        raise ValueError("Draw an area on the map first - the calendar is built for one region.")
    _check_bbox(bbox)
    area = bbox_area_sq_deg(bbox)
    if area <= 0:
        raise ValueError("Invalid area.")
    if area > MAX_AREA_SQ_DEG:
        raise ValueError(
            f"This area is too large for the calendar ({area:.0f} sq degrees, max {MAX_AREA_SQ_DEG:.0f}, "
            "about 17x17 degrees). Draw a smaller box."
        )
    days = max(MIN_DAYS, min(MAX_DAYS, int(days)))

    today = _today_utc()
    end = date.fromisoformat(end_date) if end_date else today
    if end > today:
        end = today
    start = end - timedelta(days=days - 1)

    cache_key = (bbox, end.isoformat(), days)
    now = time.time()
    if cache_key in _CACHE:
        saved_at, payload = _CACHE[cache_key]
        if end < today - timedelta(days=1) or now - saved_at < _LIVE_TTL_SECONDS:
            return payload

    chunks = []
    cursor = start
    while cursor <= end:
        n = min(CHUNK_DAYS, (end - cursor).days + 1)
        chunks.append((cursor.isoformat(), n))
        cursor += timedelta(days=n)

    per_day, failed, errors, total_rows = {}, [], [], 0
    empty_old_chunks = []
    pool = ThreadPoolExecutor(max_workers=3)  # Be gentle with NASA rate limits
    try:
        futures = [(c, pool.submit(_fetch_chunk, bbox, c[0], c[1])) for c in chunks]
        for (c_start, n), fut in futures:
            try:
                chunk_days, rows = fut.result()
            except Exception as e:  # noqa: BLE001
                failed.append((c_start, n))
                errors.append(str(e))
                continue
            total_rows += rows
            if total_rows > MAX_ROWS_TOTAL:
                pool.shutdown(wait=False, cancel_futures=True)
                raise ValueError(
                    "This area is too busy (millions of detections). Draw a smaller box or a shorter period."
                )
            if rows == 0 and (today - date.fromisoformat(c_start)).days > 60:
                empty_old_chunks.append((c_start, n))
            for d, s in chunk_days.items():
                per_day[d] = s
    finally:
        pool.shutdown(wait=False)

    if len(failed) == len(chunks):
        raise RuntimeError(f"Could not download any data from NASA FIRMS: {errors[0] if errors else 'unknown error'}")

    failed_dates = set()
    for c_start, n in failed:
        for i in range(n):
            failed_dates.add((date.fromisoformat(c_start) + timedelta(days=i)).isoformat())

    daily = {}
    cursor = start
    while cursor <= end:
        d = cursor.isoformat()
        if d in failed_dates:
            daily[d] = None
        else:
            daily[d] = per_day.get(
                d, {"events": 0, "confirmed": 0, "likely": 0, "single": 0, "weak": 0, "frp_sum": 0.0,
                    "burned_km2": 0.0, "burned_km2_low": 0.0, "burned_km2_high": 0.0, "burned_pixels": 0}
            )
        cursor += timedelta(days=1)

    partial_dates = {today.isoformat()} if end == today else set()
    days_list, periods, baseline, notes = analyze_days(daily, partial_dates)

    if failed_dates:
        notes.append(f"{len(failed_dates)} day(s) could not be downloaded (shown hatched): {errors[0]}")
    if partial_dates:
        notes.append("Today is still in progress, so it is shown faded and is not flagged.")
    if empty_old_chunks and total_rows > 0:
        notes.append(
            "Some older stretches returned no detections at all - NASA's final archive for the most "
            "recent months may not be processed yet."
        )

    payload = {
        "meta": {
            "bbox": bbox,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": days,
            "area_sq_deg": round(area, 1),
            "total_rows": total_rows,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "days": days_list,
        "periods": periods,
        "baseline": baseline,
        "notes": notes,
        "summary": {
            "unusual_days": sum(1 for e in days_list if e["status"] == "unusual"),
            "elevated_days": sum(1 for e in days_list if e["status"] == "elevated"),
            "critical_periods": len(periods),
            # Estimate burned area for the whole period (days without data are excluded)
            "burned_km2": round(sum(e.get("burned_km2") or 0 for e in days_list if e["status"] != "nodata"), 1),
            "burned_km2_low": round(sum(e.get("burned_km2_low") or 0 for e in days_list if e["status"] != "nodata"), 1),
            "burned_km2_high": round(sum(e.get("burned_km2_high") or 0 for e in days_list if e["status"] != "nodata"), 1),
            "burned_pixels": sum(e.get("burned_pixels") or 0 for e in days_list if e["status"] != "nodata"),
            "burned_method": burned_area.method_note(),
        },
    }

    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(min(_CACHE, key=lambda k: _CACHE[k][0]), None)
    _CACHE[cache_key] = (now, payload)
    return payload

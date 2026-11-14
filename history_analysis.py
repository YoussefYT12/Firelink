"""
history_analysis.py
-------------------
Converts the loaded historical data (history.py) into UI-ready responses for any square the user draws:

  - a unified monthly series MODIS+VIIRS since 2003 (after calibration in harmonize.py)
  - comparison of any month (default: current month) with the average of the same month in previous years
  - the most active months and years, and the most active month in the year (climatology)

Unit: cell-day = "1 km × 1 km cell that recorded a fire on that day", aggregated by month.
The incomplete month (current, or with days not yet uploaded) is not included in averages or ranking.
"""

import calendar
import os
import statistics
from datetime import date, datetime, timedelta, timezone

import harmonize
import history

MIN_REFERENCE_YEARS = 3          # Minimum number of previous years to compare a month to its historical average
MIN_DAYS_FOR_PROJECTION = 3      # Current month: don't forecast for another month with fewer than 3 days
STALE_DAYS = 45                  # Data older than that = warn the user to update it

_STORE_CACHE = {}                # path -> (mtime, store)
_COVERAGE_CACHE = {}


class HistoryError(ValueError):
    """User-facing error (area outside data, no data...)."""


class AreaNotDownloaded(HistoryError):
    """The box selected by the user is not inside any loaded area (the UI displays a download button)."""


# ----------------------------------------------------------------- helpers
def parse_bbox(bbox: str):
    try:
        w, s, e, n = (float(x) for x in bbox.split(","))
    except (ValueError, AttributeError):
        raise HistoryError("Invalid area. Expected west,south,east,north.")
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise HistoryError("Invalid area: need -180<=west<east<=180 and -90<=south<north<=90.")
    return w, s, e, n


def _contains(outer, inner, tol=1e-6):
    return (outer[0] - tol <= inner[0] and outer[1] - tol <= inner[1]
            and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)


# Areas are stored as approximate 0.01°, and the user draws a box freely.
# So we allow a difference up to the grid step so the drawn box matches the stored area.
_MATCH_TOL = 0.011


def _area(b):
    return (b[2] - b[0]) * (b[3] - b[1])


def _month_days(month: str):
    y, m = int(month[:4]), int(month[5:7])
    return calendar.monthrange(y, m)[1]


def _months_between(first: str, last: str):
    y, m = int(first[:4]), int(first[5:7])
    ly, lm = int(last[:4]), int(last[5:7])
    out = []
    while (y, m) <= (ly, lm):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


# ----------------------------------------------------------------- stores
def load_store_cached(name: str):
    path = history.store_path(name)
    if not os.path.exists(path):
        return None
    mtime = os.path.getmtime(path)
    hit = _STORE_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    store = history.load_store(name)
    _STORE_CACHE.clear()          # We keep only one file in memory (files can grow)
    _COVERAGE_CACHE.clear()
    _STORE_CACHE[path] = (mtime, store)
    return store


def list_regions():
    """Loaded areas: [{name, bbox, updated, synthetic}] (read from cache if the file hasn't changed)."""
    if not os.path.isdir(history.DATA_DIR):
        return []
    out = []
    for f in sorted(os.listdir(history.DATA_DIR)):
        if not f.endswith(".json.gz"):
            continue
        name = f[:-len(".json.gz")]
        try:
            store = load_store_cached(name)
        except Exception:  # noqa: BLE001  (corrupt file)
            continue
        if store:
            meta = store["meta"]
            out.append({"name": name, "bbox": meta["bbox"], "updated": meta.get("updated"),
                        "synthetic": bool(meta.get("synthetic"))})
    return out


def find_store(bbox, name=None):
    """Finds a loaded area that fully covers the box (smallest area if more than one)."""
    regions = list_regions()
    if not regions:
        raise AreaNotDownloaded(
            "No historical data downloaded yet. Draw a box on the map and press "
            "\"⬇ Download history for this box\" (or run build_history.py in the terminal). "
            "To just try the screen: python build_history.py --demo"
        )
    q = parse_bbox(bbox) if bbox else None
    candidates = []
    for r in regions:
        if name and r["name"] != name:
            continue
        rb = parse_bbox(r["bbox"])
        if q is None or _contains(rb, q, tol=_MATCH_TOL):
            candidates.append((_area(rb), r))
    if not candidates:
        have = ", ".join(f"{r['name']} ({r['bbox']})" for r in regions)
        raise AreaNotDownloaded(
            "This area is not inside any downloaded historical region. Downloaded: " + have +
            ". Draw the box on the map and press \"⬇ Download history for this box\" to add it."
        )
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]["name"]


# ----------------------------------------------------------------- coverage (which days were really downloaded)
def covered_days(store, family):
    days = set()
    for c_start in store["done"][family]:
        n = store["nrt"][family].get(c_start, {}).get("n", history.CHUNK_DAYS)
        d0 = date.fromisoformat(c_start).toordinal()
        days.update(range(d0, d0 + n))
    return days


def month_status(covered, month):
    """complete | partial (current month) | partial_start (first missing month) | gaps (gaps) | absent."""
    if not covered:
        return "absent"
    y, m = int(month[:4]), int(month[5:7])
    d0 = date(y, m, 1).toordinal()
    month_set = set(range(d0, d0 + _month_days(month)))
    have = month_set & covered
    if not have:
        return "absent"
    missing = month_set - covered
    if not missing:
        return "complete"
    first, last = min(covered), max(covered)
    if min(missing) > last:
        return "partial"
    if max(missing) < first:
        return "partial_start"
    return "gaps"


# ----------------------------------------------------------------- selection of tiles inside the user's box
def select_series(store, family, box):
    """Aggregates monthly cell-days for 0.1° cells whose centers are inside the user's box → ({month: cell_days}, number of cells)."""
    w, s, e, n = box
    tile = store["meta"].get("tile_deg", 0.1)
    rows = history.monthly_rows(store, family)
    series, tiles = {}, set()
    for (month, tx, ty), (cd, _dets, _frp) in rows.items():
        cx, cy = (tx + 0.5) * tile, (ty + 0.5) * tile
        if w <= cx <= e and s <= cy <= n:
            series[month] = series.get(month, 0) + cd
            tiles.add((tx, ty))
    return series, tiles


def _calibration_pairs(modis, viirs, st_m, st_v):
    months = sorted(set(modis) | set(viirs))
    return [(m, modis.get(m, 0), viirs.get(m, 0)) for m in months
            if st_m.get(m) == "complete" and st_v.get(m) == "complete"]


# ----------------------------------------------------------------- comparison / ranking
def _label(ratio):
    if ratio is None:
        return "no_reference"
    if ratio >= 1.5:
        return "well_above"
    if ratio >= 1.15:
        return "above"
    if ratio <= 0.67:
        return "well_below"
    if ratio <= 0.87:
        return "below"
    return "typical"


def compare_month(rows_by_month, target, today=None):
    """Compares a month to the average of the same month in previous years (full months only)."""
    row = rows_by_month.get(target)
    if row is None or row["unified"] is None:
        return {"month": target, "available": False, "reason": "No data for this month in the downloaded history."}
    cal_m, year = int(target[5:7]), int(target[:4])
    ref = sorted(
        (r["unified"], r["month"]) for m, r in rows_by_month.items()
        if int(m[5:7]) == cal_m and int(m[:4]) < year and not r["partial"] and r["unified"] is not None
        and r["source"] != "missing"
    )
    value = row["unified"]
    out = {"month": target, "available": True, "value": value, "partial": row["partial"],
           "source": row["source"], "years_compared": len(ref)}
    if row["partial"]:
        today = today or datetime.now(timezone.utc).date()
        dim = _month_days(target)
        elapsed = row.get("days_covered") or (today.day - 1 if f"{today:%Y-%m}" == target else dim)
        out["days_elapsed"], out["days_in_month"] = elapsed, dim
        out["projected"] = round(value * dim / elapsed, 1) if elapsed >= MIN_DAYS_FOR_PROJECTION else None
        compare_value = out["projected"]
    else:
        compare_value = value
    if len(ref) < MIN_REFERENCE_YEARS or compare_value is None:
        out["label"] = "no_reference"
        out["reason"] = (f"Only {len(ref)} earlier year(s) for this calendar month - need at least "
                         f"{MIN_REFERENCE_YEARS} to compare.")
        return out
    vals = [v for v, _ in ref]
    mean = statistics.mean(vals)
    ratio = (compare_value / mean) if mean > 0 else None
    higher = sum(1 for v in vals if v > compare_value)
    out.update({
        "historical_mean": round(mean, 1),
        "historical_median": round(statistics.median(vals), 1),
        "historical_min": {"value": vals[0], "month": ref[0][1]},
        "historical_max": {"value": vals[-1], "month": ref[-1][1]},
        "compared_value": round(compare_value, 1),
        "ratio": None if ratio is None else round(ratio, 2),
        "percentile": round(100 * sum(1 for v in vals if v <= compare_value) / len(vals)),
        "rank": higher + 1, "of": len(vals) + 1,
        "label": _label(ratio),
    })
    return out


def _yearly(rows):
    by_year = {}
    for r in rows:
        if r["unified"] is None:
            continue
        y = by_year.setdefault(int(r["month"][:4]), {"year": int(r["month"][:4]), "unified": 0.0, "months": 0,
                                                     "complete": True})
        y["unified"] += r["unified"]
        y["months"] += 1
        if r["partial"]:
            y["complete"] = False
    out = []
    for y in sorted(by_year):
        rec = by_year[y]
        rec["complete"] = rec["complete"] and rec["months"] == 12
        rec["unified"] = round(rec["unified"], 1)
        out.append(rec)
    return out


def _climatology(rows):
    by_cal = {c: [] for c in range(1, 13)}
    for r in rows:
        if r["unified"] is not None and not r["partial"]:
            by_cal[int(r["month"][5:7])].append(r["unified"])
    out = []
    for c in range(1, 13):
        v = by_cal[c]
        out.append({
            "month_num": c, "n_years": len(v),
            "mean": round(statistics.mean(v), 1) if v else None,
            "median": round(statistics.median(v), 1) if v else None,
            "min": min(v) if v else None, "max": max(v) if v else None,
        })
    return out


# ----------------------------------------------------------------- the main entry point
def analyze(bbox=None, region=None, month=None, today=None):
    """
    Returns the bundle that /api/history sends to the map. bbox empty = the entire loaded area.
    month='YYYY-MM' (optional) = the month we compare (default: current month).
    """
    today = today or datetime.now(timezone.utc).date()
    name = find_store(bbox, region)
    store = load_store_cached(name)
    store_box = parse_bbox(store["meta"]["bbox"])
    box = parse_bbox(bbox) if bbox else store_box

    modis, tiles_m = select_series(store, "MODIS", box)
    viirs, tiles_v = select_series(store, "VIIRS", box)

    cov = {f: covered_days(store, f) for f in history.FAMILIES}
    if not cov["MODIS"] and not cov["VIIRS"]:
        raise HistoryError(f"Region '{name}' has no downloaded days yet. Run build_history.py again.")
    first_day = date.fromordinal(min(min(c) for c in cov.values() if c))
    last_day = date.fromordinal(max(max(c) for c in cov.values() if c))
    months = _months_between(f"{first_day:%Y-%m}", f"{last_day:%Y-%m}")
    st_m = {m: month_status(cov["MODIS"], m) for m in months}
    st_v = {m: month_status(cov["VIIRS"], m) for m in months}

    notes = []
    cal = harmonize.calibrate(_calibration_pairs(modis, viirs, st_m, st_v))
    scope = "area"
    if cal is None:
        # This box: calibrate on the entire loaded area (the ratio between the two sensors is more a sensor characteristic than location)
        full_m, _ = select_series(store, "MODIS", store_box)
        full_v, _ = select_series(store, "VIIRS", store_box)
        cal = harmonize.calibrate(_calibration_pairs(full_m, full_v, st_m, st_v))
        scope = "whole_region"
        if cal:
            notes.append("This box has too few fires to calibrate MODIS against VIIRS on its own, so the "
                         "calibration of the whole downloaded region was used.")
        else:
            scope = "none"
            notes.append("Not enough overlapping MODIS+VIIRS months to calibrate. Months before VIIRS (2012) show raw "
                         "MODIS counts and are NOT comparable with later months.")

    rows = harmonize.harmonize_months(months, modis, viirs, st_m, st_v, cal)
    for r in rows:   # Days of the month that actually loaded (for forecasting in the current month)
        if r["partial"]:
            fam = "VIIRS" if r["source"] == "viirs" else "MODIS"
            y, mth = int(r["month"][:4]), int(r["month"][5:7])
            d0 = date(y, mth, 1).toordinal()
            r["days_covered"] = len(set(range(d0, d0 + _month_days(r["month"]))) & cov[fam])
    by_month = {r["month"]: r for r in rows}

    gap_months = [m for m in months if by_month[m]["source"] == "missing" and "gaps" in (st_m[m], st_v[m])]
    if gap_months:
        notes.append(f"{len(gap_months)} month(s) have missing days (failed downloads) and are left out of the chart, "
                     f"averages and rankings: {', '.join(gap_months[:6])}{'…' if len(gap_months) > 6 else ''}. "
                     "Run build_history.py again to fill them.")
    age = (today - last_day).days
    if age > STALE_DAYS:
        notes.append(f"Downloaded data ends on {last_day} ({age} days ago). Run build_history.py again to bring it up to date.")
    if store["meta"].get("synthetic"):
        notes.append("DEMO DATA: generated by a simulator, NOT real fire detections.")
    if not (tiles_m or tiles_v):
        notes.append("No fires were recorded inside this box in the downloaded history.")

    target = month or f"{today:%Y-%m}"
    if len(target) != 7 or target[4] != "-":
        raise HistoryError("Invalid month, expected YYYY-MM.")
    comparison = compare_month(by_month, target, today)
    if not comparison["available"] and not month:
        target = rows[-1]["month"] if rows else target
        comparison = compare_month(by_month, target, today)

    complete_rows = [r for r in rows if r["unified"] is not None and not r["partial"]]
    top_months = sorted(complete_rows, key=lambda r: r["unified"], reverse=True)[:10]
    yearly = _yearly(rows)
    top_years = sorted((y for y in yearly if y["complete"]), key=lambda y: y["unified"], reverse=True)[:5]
    clim = _climatology(rows)
    peak_cal = max((c for c in clim if c["mean"] is not None), key=lambda c: c["mean"], default=None)

    return {
        "meta": {
            "region": name, "region_bbox": store["meta"]["bbox"], "query_bbox": ",".join(f"{x:g}" for x in box),
            "tiles": len(tiles_m | tiles_v), "first_month": months[0], "last_month": months[-1],
            "data_through": last_day.isoformat(), "synthetic": bool(store["meta"].get("synthetic")),
            "unit": "cell-days: distinct 1 km cells with a fire on a day, summed per month",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "harmonization": {"calibrated": cal is not None, "scope": scope, **(cal or {})},
        "months": [{k: v for k, v in r.items() if k != "days_covered"} for r in rows],
        "yearly": yearly,
        "climatology": clim,
        "comparison": comparison,
        "top_months": [{"month": r["month"], "unified": r["unified"], "source": r["source"]} for r in top_months],
        "top_years": top_years,
        "peak_calendar_month": peak_cal,
        "notes": notes,
    }

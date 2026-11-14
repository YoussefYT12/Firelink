"""history.py
----------
Data layer for the unified historical record (2003 → today).

Idea: we download the MODIS record (from 2003) and the VIIRS S-NPP record (from 2012) for an area, and convert each
observation to a common unit called **cell-day**: "a 1 km × 1 km cell that recorded a fire on that day".
This unit removes differences in pixel resolution between the sensors (VIIRS 375 m and MODIS 1 km): if VIIRS
observed 6 pixels inside the same cell on the same day, it is counted exactly once like MODIS.

Remaining differences (sensor sensitivity and overpass timing) are handled by statistical calibration in
a later stage (harmonize.py) from the overlap period 2012 → today.

We store a monthly summary for each 0.1° (~11 km) tile instead of every observation, so the file is small, and any tile drawn by
the user inside the area can be aggregated later (the cell-days aggregate to separate regions without double-counting).

Downloading takes time (thousands of requests), so it is **resumable**: if you stop it or the internet cuts out, run
the same command again and it continues from where it left off.
"""

import csv
import gzip
import io
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import requests

from config import get_map_key
from firms_client import FIRMS_BASE_URL

CELL_DEG = 0.01        # ~1 km
TILE_CELLS = 10        # 10 cells = square 0.1° (~11 km)
CHUNK_DAYS = 5         # NASA limit for archive sources per request
MAX_AREA_SQ_DEG = 150.0
SP_LAG_DAYS = 150      # final archive (SP) is delayed about 5 months behind real-time
AVAILABILITY_URL = "https://firms.modaps.eosdis.nasa.gov/api/data_availability/csv/{key}/ALL"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history_data")

# each family: (final archive source SP, near-real-time source NRT, default first day)
FAMILIES = {
    "MODIS": {"sources": ("MODIS_SP", "MODIS_NRT"), "default_start": date(2003, 1, 1)},
    "VIIRS": {"sources": ("VIIRS_SNPP_SP", "VIIRS_SNPP_NRT"), "default_start": date(2012, 1, 20)},
}

# ready regions (west,south,east,north)
NAMED_REGIONS = {
    "egypt_delta": "29.5,29.9,32.8,31.7",      # Nile Delta (rice straw burning in autumn)
    "egypt": "24.7,22.0,36.9,31.7",            # all of Egypt
}


class FirmsError(Exception):
    pass


# ----------------------------------------------------------------- download
def _download(url: str, attempts: int = 4) -> str:
    """
    Loads the response text. Actual input errors (e.g., wrong region) are returned immediately, but temporary
    rejection from NASA (rate limits, or "Invalid MAP_KEY" that it sometimes returns under load)
    and network errors and 5xx errors are retried — because they used to lose whole tiles in long downloads.
    """
    last = None
    for i in range(attempts):
        try:
            r = requests.get(url, timeout=90)
        except Exception as e:  # noqa: BLE001  (network outage / timeout)
            last = e
            time.sleep(5 * (3 ** i))
            continue

        text = r.text
        body = text[:300].lower()
        looks_like_csv = text.lstrip().lower().startswith(("latitude", "data_id"))
        temporary = r.status_code == 429 or (r.status_code in (400, 403) and "map_key" in body)
        if temporary and not looks_like_csv:
            last = FirmsError(f"temporary rejection (HTTP {r.status_code}): {text[:120].strip()}")
            time.sleep(min(60, 15 * (2 ** i)))
            continue
        if r.status_code >= 500:
            last = FirmsError(f"HTTP {r.status_code}: {text[:120].strip()}")
            time.sleep(5 * (3 ** i))
            continue
        if not r.ok:
            raise FirmsError(f"HTTP {r.status_code}: {text[:200].strip()}")
        return text
    raise FirmsError(f"failed after {attempts} attempts: {last}")


def check_key(key: str, bbox: str = "30.5,30.0,32.0,31.5", attempts: int = 3) -> None:
    """
    Test the key before a long download: if NASA rejects the key itself, it's better to stop immediately with a
    clear message instead of cycling through thousands of tiles all failing.

    Note: NASA sometimes returns "Invalid MAP_KEY" which only throttles requests, so we retry
    several times with a delay before deciding the key is truly invalid.
    """
    for i in range(max(1, attempts)):
        try:
            _download(f"{FIRMS_BASE_URL}/{key}/MODIS_NRT/{bbox}/1", attempts=1)
            return None
        except FirmsError as e:
            if "map_key" not in str(e).lower():
                return None          # grid/edge: not evidence of a wrong key
            if i + 1 < attempts:
                time.sleep(20)
    raise ValueError(
        "NASA rejected the FIRMS key (Invalid MAP_KEY). Check FIRMS_MAP_KEY in the .env file "
        "next to run_all.py - you can copy a working key from https://firms.modaps.eosdis.nasa.gov/api/area/"
    )


def _published_through(family: str, avail: dict):
    """The last day NASA says it published for this family (or None if unknown)."""
    if not avail:
        return None
    hi = [avail[s][1] for s in FAMILIES[family]["sources"] if s in avail]
    return max(hi) if hi else None


def get_availability(key: str) -> dict:
    """Returns {source_name: (first_date, last_date)} from NASA. If it fails returns {} (and the code uses an estimate)."""
    try:
        text = _download(AVAILABILITY_URL.format(key=key), attempts=2)
        out = {}
        for row in csv.DictReader(io.StringIO(text)):
            did = (row.get("data_id") or "").strip()
            lo, hi = (row.get("min_date") or "")[:10], (row.get("max_date") or "")[:10]
            if did and lo and hi:
                out[did] = (date.fromisoformat(lo), date.fromisoformat(hi))
        return out
    except Exception:  # noqa: BLE001
        return {}


def choose_sources(sp: str, nrt: str, start: date, end: date, avail: dict, today: date):
    """\n    Arranges the sources we try for a slice of days. Returns (the_list, confirmed?).\n    confirmed = NASA explicitly said this source covers the slice, so an empty response means \"no fires\" not \"no data\".\n    """
    sp_range, nrt_range = avail.get(sp), avail.get(nrt)
    if sp_range and sp_range[0] <= start and end <= sp_range[1]:
        return [sp, nrt], True
    if nrt_range and nrt_range[0] <= start and end <= nrt_range[1]:
        return [nrt, sp], True
    # No confirmed information: age-based estimate
    if (today - start).days > SP_LAG_DAYS:
        return [sp, nrt], False
    return [nrt, sp], False


# ----------------------------------------------------------------- aggregation
def keep_row(row: dict) -> bool:
    """We exclude static sources (gas flares/volcanoes) and low-confidence detections (a common criterion in FIRMS)."""
    t = (row.get("type") or "").strip()
    if t not in ("", "0"):
        return False
    conf = (row.get("confidence") or "").strip().lower()
    if conf.isdigit():            # MODIS: 0-100
        return int(conf) >= 30
    return conf != "l"            # VIIRS: l / n / h


def parse_rows(text: str):
    if not text.lstrip().lower().startswith("latitude"):
        raise FirmsError("unexpected response: " + text[:120].replace("\n", " "))
    return csv.DictReader(io.StringIO(text))


def aggregate_rows(rows):
    """\n    Returns ({(month, tile_x, tile_y): [cell_days, number_of_observations, total_FRP]}, kept, dropped).\n    cell-day = (day, 1 km cell) unique. Inside the slice days are not repeated between slices, so the count is accurate.\n    """
    seen, out = set(), {}
    kept = dropped = 0
    for row in rows:
        if not keep_row(row):
            dropped += 1
            continue
        try:
            lat, lon = float(row["latitude"]), float(row["longitude"])
            day = row["acq_date"]
            frp = float(row.get("frp") or 0.0)
        except (KeyError, ValueError):
            dropped += 1
            continue
        ix, iy = math.floor(lon / CELL_DEG), math.floor(lat / CELL_DEG)
        key = (day[:7], ix // TILE_CELLS, iy // TILE_CELLS)
        rec = out.get(key)
        if rec is None:
            rec = out[key] = [0, 0, 0.0]
        rec[1] += 1
        rec[2] += frp
        cell_day = (day, ix, iy)
        if cell_day not in seen:
            seen.add(cell_day)
            rec[0] += 1
        kept += 1
    return out, kept, dropped


def _merge(target: dict, rows: dict):
    for k, v in rows.items():
        rec = target.get(k)
        if rec is None:
            target[k] = list(v)
        else:
            rec[0] += v[0]
            rec[1] += v[1]
            rec[2] += v[2]


# ----------------------------------------------------------------- chunks
def plan_chunks(start: date, end: date):
    chunks, cur = [], start
    while cur <= end:
        n = min(CHUNK_DAYS, (end - cur).days + 1)
        chunks.append((cur.isoformat(), n))
        cur += timedelta(days=n)
    return chunks


def fetch_chunk(family: str, bbox: str, chunk_start: str, n_days: int, key: str, avail: dict, today: date,
                published_through: date = None):
    """\n    Returns (rows_dict, kept, used_source). Tries the second source if the first failed or returned empty without confirmation.\n\n    If the slice hasn't yet been published by NASA (after the last day in the availability list) and the response is empty, we consider it\n    \"not yet available\" and return it as a failure instead of recording it as zero fires - that phantom zero was causing the last\n    two weeks to show a sudden drop in the chart.\n    """
    sp, nrt = FAMILIES[family]["sources"]
    start = date.fromisoformat(chunk_start)
    end = start + timedelta(days=n_days - 1)
    order, certain = choose_sources(sp, nrt, start, end, avail, today)
    last_error, fallback = None, None
    for src in order:
        try:
            text = _download(f"{FIRMS_BASE_URL}/{key}/{src}/{bbox}/{n_days}/{chunk_start}")
            rows, kept, _ = aggregate_rows(parse_rows(text))
        except FirmsError as e:
            last_error = e
            continue
        if kept > 0 or certain:
            return rows, kept, src
        if fallback is None:
            fallback = (rows, kept, src)       # Empty and unsure: try the second before accepting \"zero\"
    if fallback is not None:
        if published_through is not None and end > published_through:
            raise FirmsError(
                f"{family} data is not published yet for {chunk_start} "
                f"(NASA has data through {published_through.isoformat()})"
            )
        return fallback
    raise last_error or FirmsError("no source answered")


# ----------------------------------------------------------------- store (gzip json, resumable)
def store_path(name: str) -> str:
    return os.path.join(DATA_DIR, f"{name}.json.gz")


def _enc(d: dict):
    return [[k[0], k[1], k[2], v[0], v[1], round(v[2], 2)] for k, v in sorted(d.items())]


def _dec(rows: list) -> dict:
    return {(r[0], r[1], r[2]): [r[3], r[4], r[5]] for r in rows}


def new_store(name: str, bbox: str) -> dict:
    return {
        "meta": {
            "name": name, "bbox": bbox, "version": 1, "cell_deg": CELL_DEG, "tile_deg": CELL_DEG * TILE_CELLS,
            "unit": "cell-day: distinct 1 km cell with >=1 detection on a UTC day",
            "filters": "static sources (type!=0) removed; MODIS confidence<30 and VIIRS 'low' removed",
            "created": datetime.now(timezone.utc).isoformat(),
        },
        "acc": {"MODIS": {}, "VIIRS": {}},     # Final archive slices (SP) aggregated
        "nrt": {"MODIS": {}, "VIIRS": {}},     # Near-real-time (NRT) slices separated so they update when SP is ready
        "done": {"MODIS": {}, "VIIRS": {}},    # {first day in slice: source used}
        "failed": {"MODIS": {}, "VIIRS": {}},
    }


def save_store(name: str, store: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = store_path(name)
    tmp = path + ".tmp"
    store["meta"]["updated"] = datetime.now(timezone.utc).isoformat()
    out = {
        "meta": store["meta"], "done": store["done"], "failed": store["failed"],
        "acc": {f: _enc(d) for f, d in store["acc"].items()},
        "nrt": {f: {c: {"n": v["n"], "rows": _enc(v["rows"])} for c, v in d.items()} for f, d in store["nrt"].items()},
    }
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))
    os.replace(tmp, path)       # Atomic write: file won't be corrupted if the process stops in the middle


def load_store(name: str):
    path = store_path(name)
    if not os.path.exists(path):
        return None
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        raw = json.load(fh)
    return {
        "meta": raw["meta"], "done": raw["done"], "failed": raw.get("failed", {"MODIS": {}, "VIIRS": {}}),
        "acc": {f: _dec(rows) for f, rows in raw["acc"].items()},
        "nrt": {f: {c: {"n": v["n"], "rows": _dec(v["rows"])} for c, v in d.items()} for f, d in raw["nrt"].items()},
    }


def monthly_rows(store: dict, family: str) -> dict:
    """All slices (SP + NRT) aggregated: {(month, tx, ty): [cell_days, detections, frp]}."""
    merged = {}
    _merge(merged, store["acc"][family])
    for chunk in store["nrt"][family].values():
        _merge(merged, chunk["rows"])
    return merged


# ----------------------------------------------------------------- the main job
def bbox_area(bbox: str) -> float:
    w, s, e, n = (float(x) for x in bbox.split(","))
    return max(0.0, e - w) * max(0.0, n - s)


def same_bbox(a: str, b: str, tol: float = 1e-6) -> bool:
    """Same cell numerically (textual form may differ: 29.5 vs 29.50)."""
    try:
        return all(abs(float(x) - float(y)) < tol for x, y in zip(a.split(","), b.split(",")))
    except (AttributeError, ValueError):
        return False


def build_history(name, bbox, start=None, end=None, workers=3, key=None, log=print,
                  retry_failed=True, refresh_nrt=False, force_area=False,
                  progress=None, should_stop=None):
    """
    Downloads (or resumes) an area record. Resumable and stoppable at any time.

    progress: optional function called with the download status (dict) so the UI can show a progress bar:
              phase (planning/downloading/done/cancelled), total, done, failed, percent,
              elapsed, eta_seconds, message.
    should_stop: optional function returning True if the user requested stop (the downloaded part is saved).
    """
    def emit(phase, **kw):
        if progress is None:
            return
        try:
            progress(dict(phase=phase, **kw))
        except Exception:  # noqa: BLE001  (display error won't stop the download)
            pass

    def stopped() -> bool:
        try:
            return bool(should_stop and should_stop())
        except Exception:  # noqa: BLE001
            return False

    key = get_map_key(key)
    area = bbox_area(bbox)
    if area <= 0:
        raise ValueError("Invalid bbox. Use: west,south,east,north")
    if area > MAX_AREA_SQ_DEG and not force_area:
        raise ValueError(f"Area is {area:.0f} sq degrees (max {MAX_AREA_SQ_DEG:.0f}). Use a smaller box or --force-area.")

    today = datetime.now(timezone.utc).date()
    end = end or (today - timedelta(days=1))          # Excluding today (still missing)
    start = start or FAMILIES["MODIS"]["default_start"]

    store = load_store(name)
    if store is None:
        store = new_store(name, bbox)
    elif not same_bbox(store["meta"]["bbox"], bbox):
        raise ValueError(f"'{name}' already exists for a different box ({store['meta']['bbox']}). Pick another --name.")

    emit("planning", total=0, done=0, failed=0, percent=0,
         message="Asking NASA what data it has for this area…")
    avail = get_availability(key)
    log(f"availability info from NASA: {'ok (' + str(len(avail)) + ' sources)' if avail else 'not available - using date-based guess'}")
    published = {fam: _published_through(fam, avail) for fam in FAMILIES}

    if refresh_nrt:       # NRT slices became older than the final archive delay: re-download them (likely converted to SP)
        for fam in FAMILIES:
            for c in [c for c, src in store["done"][fam].items()
                      if src.endswith("_NRT") and (today - date.fromisoformat(c)).days > SP_LAG_DAYS]:
                store["done"][fam].pop(c, None)
                store["nrt"][fam].pop(c, None)

    pending, planned = [], {}
    for fam, spec in FAMILIES.items():
        fam_start = max(start, spec["default_start"])
        fam_end = min(end, published[fam]) if published.get(fam) else end
        planned[fam] = {c for c, _ in plan_chunks(fam_start, fam_end)} if fam_end >= fam_start else set()
        if fam_end < fam_start:
            continue                      # NASA hasn't published any day in the requested period for this family yet
        for c_start, n in plan_chunks(fam_start, fam_end):
            if c_start in store["done"][fam]:
                # Missing slice (last days at load time, n < 5) is re-uploaded when its days are complete.
                # Missing slices are always stored in nrt (replace rather than accumulate) so there's no duplicate counting.
                have = store["nrt"][fam].get(c_start, {}).get("n", CHUNK_DAYS)
                if have >= n:
                    continue
            if c_start in store["failed"][fam] and not retry_failed:
                continue
            pending.append((fam, c_start, n))

    # Slices that failed previously and are out of the schedule (e.g. days NASA hadn't published yet and then the period was postponed)
    # Not needed for counting, so we remove it so the number shown to the user is correct.
    for fam, starts in planned.items():
        for c in [c for c in store["failed"][fam] if c not in starts]:
            store["failed"][fam].pop(c, None)

    total_chunks = 0
    for fam, spec in FAMILIES.items():
        fam_start = max(start, spec["default_start"])
        fam_end = min(end, published[fam]) if published.get(fam) else end
        if fam_end >= fam_start:
            total_chunks += len(plan_chunks(fam_start, fam_end))
    already = total_chunks - len(pending)
    log(f"'{name}' box {bbox} ({area:.1f} sq deg): {total_chunks} chunks in total, {len(pending)} to download "
        f"(~{len(pending) * 1.5 / max(workers, 1) / 60:.0f}+ minutes)")
    if not pending:
        log("nothing to do - everything is already downloaded.")
        emit("done", total=0, done=0, failed=0, already_done=already, percent=100,
             message="Everything was already downloaded.")
        return store

    if not avail and len(pending) >= 10:
        # Long download without confirming NASA can see the key: verify first to avoid wasting hours.
        emit("planning", total=len(pending), done=0, failed=0, percent=0,
             message="Checking the NASA key…")
        check_key(key)

    started = time.time()
    done_now = failed_now = 0
    batch_size = max(workers * 8, 8)
    pool = ThreadPoolExecutor(max_workers=workers)
    cancelled = False
    emit("downloading", total=len(pending), done=0, failed=0, already_done=already, percent=0,
         message="Downloading from NASA…")
    try:
        for i in range(0, len(pending), batch_size):
            if stopped():
                cancelled = True
                break
            batch = pending[i:i + batch_size]
            futures = [(fam, c, n, pool.submit(fetch_chunk, fam, bbox, c, n, key, avail, today, published.get(fam)))
                       for fam, c, n in batch]
            for fam, c, n, fut in futures:
                try:
                    rows, _, src = fut.result()
                except FirmsError as e:
                    store["failed"][fam][c] = str(e)[:200]
                    failed_now += 1
                    continue
                store["failed"][fam].pop(c, None)
                if src.endswith("_NRT") or n < CHUNK_DAYS:
                    store["nrt"][fam][c] = {"n": n, "rows": rows}
                else:
                    store["nrt"][fam].pop(c, None)     # Old missing slice replaced by full archive data.
                    _merge(store["acc"][fam], rows)
                store["done"][fam][c] = src
                done_now += 1
            save_store(name, store)
            elapsed = time.time() - started
            fraction = (done_now + failed_now) / len(pending)
            eta = elapsed / fraction * (1 - fraction) if fraction > 0 else None
            log(f"  {done_now + failed_now}/{len(pending)} chunks ({fraction:.0%}) | failed {failed_now} | "
                f"elapsed {elapsed / 60:.1f} min | ETA ~{(eta or 0) / 60:.0f} min")
            emit("downloading", total=len(pending), done=done_now, failed=failed_now, already_done=already,
                 percent=round(100 * fraction, 1), elapsed=round(elapsed, 1),
                 eta_seconds=None if eta is None else round(eta),
                 message="Downloading from NASA…")
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        save_store(name, store)
        log("stopped by you - progress saved. Run the same command again to continue.")
        emit("cancelled", total=len(pending), done=done_now, failed=failed_now, already_done=already,
             percent=round(100 * (done_now + failed_now) / len(pending), 1),
             message="Stopped. What was downloaded is saved.")
        return store
    finally:
        pool.shutdown(wait=False)

    save_store(name, store)
    if cancelled:
        log(f"stopped - {done_now} chunk(s) saved. Run again to continue.")
        emit("cancelled", total=len(pending), done=done_now, failed=failed_now, already_done=already,
             percent=round(100 * (done_now + failed_now) / len(pending), 1),
             message="Stopped. What was downloaded is saved.")
        return store

    if failed_now:
        log(f"{failed_now} chunk(s) failed (network or NASA errors). Run the same command again to retry them.")
    else:
        log("finished with no failures.")
    emit("done", total=len(pending), done=done_now, failed=failed_now, already_done=already,
         percent=100, elapsed=round(time.time() - started, 1), eta_seconds=0,
         message="Download finished.")
    return store


def summarize_store(store: dict) -> str:
    lines = [f"region '{store['meta']['name']}'  box {store['meta']['bbox']}"]
    for fam in FAMILIES:
        rows = monthly_rows(store, fam)
        months = sorted({k[0] for k in rows})
        lines.append(
            f"  {fam}: {len(store['done'][fam])} chunks done, {len(store['failed'][fam])} failed, "
            f"{len(store['nrt'][fam])} recent (NRT) | months with fires: {len(months)}"
            + (f" ({months[0]} to {months[-1]})" if months else "")
            + f" | cell-days: {sum(v[0] for v in rows.values()):,}"
        )
    return "\n".join(lines)
    

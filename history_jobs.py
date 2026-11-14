"""
history_jobs.py
---------------
Runs the historical record download (history.py) **in the background** inside the local server, so
the user can trigger the download from the map itself instead of opening the terminal and typing a command.

Its jobs:
  - start(bbox, start=…)   begins downloading an area, and returns the job state immediately (doesn't wait)
  - status()               progress: how many tiles completed, failed, percent, ETA
  - cancel()               stops after the last batch (what's already downloaded is saved, resumes later)

Only one job at a time: downloads use the same NASA key, so running two together
increases temporary rejections (rate limit) and loses tiles.
"""

import math
import re
import threading
from datetime import date, datetime, timedelta, timezone

import history
from config import MissingKeyError

MAX_AREA_SQ_DEG = history.MAX_AREA_SQ_DEG     # 150 square degrees (about 12x12)
MAX_WORKERS = 4
MAX_LOG_LINES = 6
MIN_BOX_DEG = 0.02                            # smallest acceptable square (~2 km)
GRID = 2                                      # truncate coordinates to two decimals: same area = same file

_BBOX_RE = re.compile(r"^-?\d+(?:\.\d+)?(?:,-?\d+(?:\.\d+)?){3}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,59}$")

_lock = threading.Lock()
_job = None
_seq = 0


class BuildError(ValueError):
    """Inputs rejected (wrong/too large area, wrong date...)."""


class AlreadyRunning(RuntimeError):
    """A download is already running."""


# ----------------------------------------------------------------- validation
def parse_bbox(bbox: str) -> str:
    """
    Verifies the square and returns it as a normalized string (two decimals) or raises BuildError.

    Rounding **outwards** (west/south up, east/north down) not to the nearest: this way the stored square
    always **contains** the square the user drew, so the UI can read its history later
    without going outside the area by 0.004 degrees.
    """
    bbox = (bbox or "").strip()
    if bbox == "world":
        raise BuildError("The whole world is too much to download. Draw a box around the area you care about.")
    if not _BBOX_RE.match(bbox):
        raise BuildError("Invalid area. Expected four numbers: west,south,east,north.")
    w, s, e, n = (float(x) for x in bbox.split(","))
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise BuildError("Invalid area: need -180 <= west < east <= 180 and -90 <= south < north <= 90.")
    if (e - w) < MIN_BOX_DEG or (n - s) < MIN_BOX_DEG:
        raise BuildError("That box is too small. Draw a bigger area (at least ~2 km across).")
    step = 10 ** GRID
    w, s = math.floor(w * step) / step, math.floor(s * step) / step
    e, n = math.ceil(e * step) / step, math.ceil(n * step) / step
    return f"{w:.{GRID}f},{s:.{GRID}f},{e:.{GRID}f},{n:.{GRID}f}"


def area_sq_deg(bbox: str) -> float:
    w, s, e, n = (float(x) for x in bbox.split(","))
    return max(0.0, e - w) * max(0.0, n - s)


def region_name(bbox: str) -> str:
    """Filename from the square itself: area_29.50_29.90_32.80_31.70 (same square = same file = resume)."""
    return "area_" + "_".join(bbox.split(","))


def existing_region_for(bbox: str):
    """
    If the same tile was previously downloaded under a different name (e.g. egypt_delta), we use that same name so
    the button can resume and repair missing tiles instead of starting a new download from scratch.
    """
    found = matching_region(bbox)
    return found["name"] if found else None


def matching_region(bbox: str):
    """
    Returns the loaded region that matches the same tile (within a grid step difference of 0.01°) or None.
    The small difference is possible because the tile is snapped before storage, and the user draws freely.
    """
    try:
        import history_analysis
        regions = history_analysis.list_regions()
    except Exception:  # noqa: BLE001  (corrupt file/folder missing)
        return None
    w, s, e, n = (float(x) for x in bbox.split(","))
    tol = 0.011
    for r in regions:
        try:
            rw, rs, re_, rn = (float(x) for x in r["bbox"].split(","))
        except (KeyError, ValueError, AttributeError):
            continue
        if (abs(rw - w) <= tol and abs(rs - s) <= tol
                and abs(re_ - e) <= tol and abs(rn - n) <= tol):
            return r
    return None


def resolve_start(value, today=None):
    """'2003' | '2012' | '12m' | 'YYYY' | 'YYYY-MM-DD' | None → start date (None = 2003)."""
    today = today or datetime.now(timezone.utc).date()
    if value in (None, "", "2003", "full"):
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if text == "2012":
        return date(2012, 1, 20)          # first day with VIIRS
    if text == "12m":
        return today - timedelta(days=365)
    try:
        if re.match(r"^\d{4}$", text):
            return date(int(text), 1, 1)
        return date.fromisoformat(text)
    except ValueError:
        raise BuildError("Invalid start date. Use 2003, 2012, 12m, or YYYY-MM-DD.")


def resolve_end(value, today=None):
    today = today or datetime.now(timezone.utc).date()
    if value in (None, ""):
        return today - timedelta(days=1)      # today still missing
    if isinstance(value, date):
        return value
    try:
        d = date.fromisoformat(str(value).strip())
    except ValueError:
        raise BuildError("Invalid end date. Use YYYY-MM-DD.")
    return min(d, today)


# ----------------------------------------------------------------- the job
def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _public(job):
    if job is None:
        return None
    return {k: v for k, v in job.items() if not k.startswith("_")}


def _log(job, message):
    line = str(message)[:240]
    with _lock:
        job["log"].append(line)
        del job["log"][:-MAX_LOG_LINES]


def _on_progress(job, p):
    with _lock:
        job["progress"] = dict(p)
        msg = p.get("message")
        if msg and (not job["log"] or job["log"][-1] != msg):
            job["log"].append(str(msg)[:240])
            del job["log"][:-MAX_LOG_LINES]


def summarize(store, region):
    """A short summary for the UI after the download finishes (how many tiles, how many cell-days, from when to when)."""
    out = {"region": region, "bbox": store["meta"]["bbox"], "chunks_done": 0, "chunks_failed": 0,
           "cell_days": 0, "months": [None, None], "families": {}, "failed_examples": []}
    months = set()
    examples = []
    for fam in history.FAMILIES:
        rows = history.monthly_rows(store, fam)
        months |= {k[0] for k in rows}
        cell_days = sum(v[0] for v in rows.values())
        out["families"][fam] = {"chunks_done": len(store["done"][fam]),
                                "chunks_failed": len(store["failed"][fam]),
                                "cell_days": cell_days}
        out["chunks_done"] += len(store["done"][fam])
        out["chunks_failed"] += len(store["failed"][fam])
        out["cell_days"] += cell_days
        examples += [str(v)[:90] for v in store["failed"][fam].values()]
    if months:
        out["months"] = [min(months), max(months)]
    out["failed_examples"] = sorted(set(examples))[:3]
    out["summary_text"] = history.summarize_store(store)
    return out


def _fail(job, message):
    with _lock:
        job["state"] = "error"
        job["error"] = str(message)
        job["finished_at"] = _now()
        job["log"].append(str(message)[:240])
        del job["log"][:-MAX_LOG_LINES]


def _run(job, region, bbox, start_date, end_date, workers):
    def log(message):
        _log(job, message)

    try:
        store = history.build_history(
            region, bbox, start=start_date, end=end_date,
            workers=max(1, min(int(workers or 3), MAX_WORKERS)),
            log=log, progress=lambda p: _on_progress(job, p),
            should_stop=lambda: bool(job.get("_cancel")),
        )
        result = summarize(store, region)
        with _lock:
            job["result"] = result
            job["state"] = "cancelled" if job.get("_cancel") else "done"
            job["finished_at"] = _now()
    except MissingKeyError as e:
        _fail(job, str(e))
    except ValueError as e:                 # invalid input/area, or wrong key from check_key
        _fail(job, str(e))
    except Exception as e:                  # noqa: BLE001  (grid, corrupt file...)
        _fail(job, f"{type(e).__name__}: {e}")


def start(bbox, start=None, end=None, workers=3, name=None):
    """Starts a background download and returns job status. Raises BuildError / AlreadyRunning."""
    global _job, _seq
    box = parse_bbox(bbox)
    area = area_sq_deg(box)
    if area > MAX_AREA_SQ_DEG:
        raise BuildError(
            f"This area is {area:.0f} square degrees - too big for one download "
            f"(max {MAX_AREA_SQ_DEG:.0f}, about 12x12 degrees). Draw a smaller box."
        )
    today = datetime.now(timezone.utc).date()
    start_date = resolve_start(start, today)
    end_date = resolve_end(end, today)
    if start_date and start_date >= end_date:
        raise BuildError("The start date must be before the end date.")

    # If the same area was loaded before, continue on the same stored tile (same tiling scheme)
    # Instead of starting a new download because of an approximate difference of 0.005 degrees.
    existing = None if name else matching_region(box)
    region = (name or (existing or {}).get("name") or region_name(box)).strip()
    if existing:
        box = existing["bbox"]
    if not _NAME_RE.match(region) or ".." in region:
        raise BuildError("Invalid region name (letters, digits, dot, dash and underscore only).")

    with _lock:
        if _job is not None and _job["state"] == "running":
            raise AlreadyRunning("A download is already running. Wait for it to finish, or press Stop first.")
        _seq += 1
        job = {
            "id": _seq, "region": region, "bbox": box,
            "start": start_date.isoformat() if start_date else history.FAMILIES["MODIS"]["default_start"].isoformat(),
            "end": end_date.isoformat(), "workers": max(1, min(int(workers or 3), MAX_WORKERS)),
            "state": "running", "started_at": _now(), "finished_at": None,
            "progress": {"phase": "queued", "total": 0, "done": 0, "failed": 0, "percent": 0,
                         "message": "Starting…"},
            "log": [], "error": None, "result": None, "_cancel": False,
        }
        _job = job

    threading.Thread(target=_run, args=(job, region, box, start_date, end_date, workers),
                     name=f"history-build-{job['id']}", daemon=True).start()
    return _public(job)


def status():
    """{"job": {...}} — the last job (running or finished) or None if nothing was created from the map."""
    with _lock:
        return {"job": _public(_job)}


def cancel():
    """Requests cancellation. Returns {"cancelled": True/False, "job": ...}."""
    with _lock:
        if _job is None or _job["state"] != "running":
            return {"cancelled": False, "job": _public(_job)}
        _job["_cancel"] = True
        _job["progress"] = dict(_job["progress"], message="Stopping after the current batch…")
        return {"cancelled": True, "job": _public(_job)}

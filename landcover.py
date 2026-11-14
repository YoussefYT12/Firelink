"""
landcover.py
------------
Land type classification at the fire (agricultural / forest / grassland) from real data.

Primary source: ESA WorldCover 2021 - global land map at 10 meter resolution (11 classes),
free and available on the public AWS S3, and covers the whole world (unlike OpenStreetMap which
has gaps). We read a small window (~40x40 pixels) around each fire point only
(a few kilobytes) without downloading the whole file (each file is hundreds of megabytes).
This requires the rasterio library:   pip install rasterio

Fallback: OpenStreetMap via Overpass API (if rasterio is not installed or
S3 is not available). And if both fail, the main code (pipeline.py) falls back to FRP estimation.

We measure at the top 3 fire points in the cluster, not at the center, and aggregate the results.
"""

import math
import os
import threading
import time
from collections import Counter

import requests

# ---------------------------------------------------------------- WorldCover
WORLDCOVER_URL = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
    "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
)
WINDOW_HALF_PIXELS = 20  # Window 41x41 pixels (~400 meters), close to VIIRS pixel size (375 m)

WC_NAMES = {
    10: "Tree cover", 20: "Shrubland", 30: "Grassland", 40: "Cropland", 50: "Built-up",
    60: "Bare/sparse", 70: "Snow/ice", 80: "Water", 90: "Wetland", 95: "Mangrove", 100: "Moss/lichen",
}
MIN_SHARE = 0.25  # Minimum fraction for a land type to count as the classification

# ---------------------------------------------------------------- OpenStreetMap (fallback)
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
HEADERS = {"User-Agent": "FireLink-NASA-SpaceApps-2026"}  # Otherwise Overpass rejects (406)
OVERPASS_TIMEOUT_S = 12

AGRI_TAGS = {"farmland", "farmyard", "orchard", "vineyard", "plant_nursery", "allotments", "meadow"}
FOREST_LANDUSE_TAGS = {"forest"}
FOREST_NATURAL_TAGS = {"wood"}
GRASSLAND_TAGS = {"grassland", "scrub", "heath", "fell", "grass"}
URBAN_TAGS = {"residential", "commercial", "industrial", "retail"}

LABEL_AGRI = "Likely agricultural/crop burning"
LABEL_FOREST = "Likely wildfire/forest fire"
LABEL_GRASS = "Likely grassland/bushfire"
LABEL_URBAN = "Unusual: near urban/built-up area - verify"
LABEL_LOWVEG = "Low-vegetation area (bare ground/water/ice) - verify"


class _Breaker:
    """Temporarily pauses attempts if the source failed several times in a row (so alerts don't get delayed)."""

    def __init__(self, max_failures=3, pause_seconds=600):
        self.max_failures, self.pause_seconds = max_failures, pause_seconds
        self.failures, self.paused_until = 0, 0.0

    def allowed(self):
        return time.time() >= self.paused_until

    def success(self):
        self.failures = 0

    def failure(self):
        self.failures += 1
        if self.failures >= self.max_failures:
            self.paused_until = time.time() + self.pause_seconds
            self.failures = 0


_wc_breaker = _Breaker()
_osm_breaker = _Breaker()
_osm_lock = threading.Lock()  # Overpass: one request at a time (fair use)
_wc_cache = {}
_osm_cache = {}
_hint_shown = False


# ================================================================ WorldCover
def tile_name(lat: float, lon: float) -> str:
    """WorldCover filename (3x3 degree tile, named by the southwest corner) e.g. N51E003."""
    lat0 = math.floor(lat / 3) * 3
    lon0 = math.floor(lon / 3) * 3
    ns = "N" if lat0 >= 0 else "S"
    ew = "E" if lon0 >= 0 else "W"
    return f"{ns}{abs(lat0):02d}{ew}{abs(lon0):03d}"


def classify_counts(counts: Counter, hotspots: int = 1):
    """Converts WorldCover class distribution to a fire classification + source note. None if no data."""
    total = sum(n for code, n in counts.items() if code)
    if total == 0:
        return None

    def share(codes):
        return sum(counts.get(c, 0) for c in codes) / total

    top = ", ".join(
        f"{WC_NAMES.get(code, code)} {n / total:.0%}"
        for code, n in counts.most_common(3) if code
    )
    spots = f", {hotspots} hotspots" if hotspots > 1 else ""
    note = f"ESA WorldCover 2021{spots}: {top}"

    groups = {LABEL_AGRI: share([40]), LABEL_FOREST: share([10, 95]), LABEL_GRASS: share([20, 30, 90])}
    best = max(groups, key=groups.get)
    built = share([50])
    # If buildings are the largest (and above the minimum) the area is urban, even if it contains trees/grass (parks)
    if built >= MIN_SHARE and built >= groups[best]:
        return LABEL_URBAN, note
    if groups[best] >= MIN_SHARE:
        return best, note
    return LABEL_LOWVEG, note


def _read_counts(lat: float, lon: float):
    """
    Reads a small window from a WorldCover file via HTTP Range and returns a Counter of classes.
    None = unavailable/failed (rasterio not installed or network), empty Counter = no data here (ocean).
    """
    global _hint_shown
    try:
        import rasterio
        from rasterio.windows import Window
    except ImportError:
        if not _hint_shown:
            _hint_shown = True
            print("[landcover] tip: run 'pip install rasterio' to enable real ESA WorldCover land cover.")
        return None

    url = WORLDCOVER_URL.format(tile=tile_name(lat, lon))
    env = dict(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
        GDAL_HTTP_TIMEOUT=20,
        GDAL_HTTP_MAX_RETRY=1,
    )
    try:
        with rasterio.Env(**env):
            with rasterio.open(url) as src:
                row, col = src.index(lon, lat)
                half = WINDOW_HALF_PIXELS
                row0, col0 = max(0, row - half), max(0, col - half)
                row1, col1 = min(src.height, row + half + 1), min(src.width, col + half + 1)
                if row1 <= row0 or col1 <= col0:
                    return Counter()
                data = src.read(1, window=Window(col0, row0, col1 - col0, row1 - row0))
        return Counter(int(v) for v in data.ravel().tolist())
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if "does not exist" in msg or "404" in msg:
            return Counter()  # No file for this tile = open ocean
        return None


def _worldcover_counts(lat: float, lon: float):
    key = (round(lat, 3), round(lon, 3))
    if key in _wc_cache:
        return _wc_cache[key]
    if not _wc_breaker.allowed():
        return None
    counts = _read_counts(lat, lon)
    if counts is None:
        # rasterio not installed is not a "network failure" - we don't count it
        try:
            import rasterio  # noqa: F401
            _wc_breaker.failure()
        except ImportError:
            pass
        return None
    _wc_breaker.success()
    _wc_cache[key] = counts
    return counts


# ================================================================ OpenStreetMap
def _categorize(tag_key: str, tag_value: str):
    if tag_value in AGRI_TAGS:
        return LABEL_AGRI, f"OpenStreetMap: {tag_value}"
    if tag_key == "landuse" and tag_value in FOREST_LANDUSE_TAGS:
        return LABEL_FOREST, f"OpenStreetMap: {tag_value}"
    if tag_key == "natural" and tag_value in FOREST_NATURAL_TAGS:
        return LABEL_FOREST, f"OpenStreetMap: {tag_value}"
    if tag_value in GRASSLAND_TAGS:
        return LABEL_GRASS, f"OpenStreetMap: {tag_value}"
    if tag_value in URBAN_TAGS:
        return LABEL_URBAN, f"OpenStreetMap: {tag_value}"
    return None


_OSM_PRIORITY = {LABEL_AGRI: 0, LABEL_FOREST: 0, LABEL_GRASS: 1, LABEL_URBAN: 2}


def _pick_best(elements: list):
    best = None
    for el in elements:
        tags = el.get("tags", {})
        for key_name in ("landuse", "natural"):
            if key_name in tags:
                cat = _categorize(key_name, tags[key_name])
                if cat and (best is None or _OSM_PRIORITY[cat[0]] < _OSM_PRIORITY[best[0]]):
                    best = cat
    return best


def _overpass(query: str):
    for url in OVERPASS_MIRRORS:
        try:
            r = requests.post(url, data={"data": query}, headers=HEADERS, timeout=OVERPASS_TIMEOUT_S)
            if r.ok:
                return r.json().get("elements", [])
        except Exception:  # noqa: BLE001
            continue
    return None


def _query_osm(lat: float, lon: float):
    """Fallback: (label, note) or None. One request is executed at a time."""
    key = (round(lat, 3), round(lon, 3))
    if key in _osm_cache:
        return _osm_cache[key]
    if not _osm_breaker.allowed():
        return None
    with _osm_lock:
        inside = f"""[out:json][timeout:10];
        is_in({lat},{lon})->.a;
        (area.a["landuse"]; area.a["natural"];);
        out tags;"""
        elements = _overpass(inside)
        if elements is None:
            _osm_breaker.failure()
            return None
        _osm_breaker.success()
        result = _pick_best(elements)
        if result is None:
            near = f"""[out:json][timeout:10];
            (way(around:300,{lat},{lon})["landuse"]; way(around:300,{lat},{lon})["natural"];);
            out tags 10;"""
            near_elements = _overpass(near)
            if near_elements is None:
                return None
            result = _pick_best(near_elements)
        _osm_cache[key] = result
        time.sleep(1.0)
        return result


# ================================================================ public API
def query_land_cover_points(points):
    """
    points: list of (lat, lon) for the strongest fire points in the cluster.
    Returns (fire_type_label, source_note) or None if no source is available.
    """
    if os.environ.get("FIRELINK_NO_GEOCODE"):  # Same variable to disable internet requests during tests
        return None
    points = list(points or [])
    if not points:
        return None

    total, answered = Counter(), 0
    for lat, lon in points:
        counts = _worldcover_counts(lat, lon)
        if counts is not None:
            total.update(counts)
            answered += 1
    if answered:
        return classify_counts(total, hotspots=answered)

    # WorldCover not available: try OpenStreetMap at the strongest point
    return _query_osm(*points[0])


def query_land_cover(lat: float, lon: float):
    """One point (for compatibility with old scripts)."""
    return query_land_cover_points([(lat, lon)])
"""
check_landcover_debug.py
------------------------
Separate diagnostic (doesn't modify any file in the project). Answers 3 questions in order:
  1) Can your machine reach the ESA WorldCover files on AWS? (without any additional library)
  2) Is rasterio installed? And can it read a small window from the file?
  3) (just for information) does the backup Overpass server respond?

  python tools/check_landcover_debug.py
"""

import time

import requests

TILE_URL = ("https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
            "ESA_WorldCover_10m_2021_v200_N51E003_Map.tif")  # square containing the Netherlands
LAT, LON = 42.0, -92.5   # central Iowa, USA (Corn Belt) - expected: Cropland

print("=== 1) Can this computer reach ESA WorldCover on AWS S3? ===")
started = time.time()
try:
    r = requests.get(TILE_URL, headers={"Range": "bytes=0-16383"}, timeout=25)
    head = r.content[:4]
    is_tiff = head[:2] in (b"II", b"MM")
    print(f"   HTTP {r.status_code} in {time.time() - started:.1f}s, got {len(r.content)} bytes, "
          f"TIFF header: {'YES' if is_tiff else 'NO'}")
    if r.status_code == 206 and is_tiff:
        print("   -> OK: the land-cover file is reachable (partial download works).")
    else:
        print("   -> Unexpected reply:", r.text[:200].replace("\n", " "))
except requests.exceptions.Timeout:
    print(f"   TIMEOUT after {time.time() - started:.1f}s -> network/ISP is blocking or slowing AWS S3.")
except Exception as e:
    print(f"   ERROR ({type(e).__name__}): {e}")

print("\n=== 2) rasterio + reading a small window ===")
try:
    import rasterio  # noqa: F401
    print(f"   rasterio is installed (version {rasterio.__version__}).")
    from landcover import classify_counts, _read_counts
    started = time.time()
    counts = _read_counts(LAT, LON)
    took = time.time() - started
    if counts is None:
        print(f"   FAILED after {took:.1f}s (network error). See check 1 above.")
    elif not counts:
        print("   Tile has no data here (open sea).")
    else:
        print(f"   Read a 41x41 pixel window in {took:.1f}s.")
        print("   Result for the Iowa farm point:", classify_counts(counts))
        print("   -> EXPECTED: 'Likely agricultural/crop burning' with Cropland as the main class.")
        print("      (one point can land on a farmhouse/road - test_landcover.py checks 9 points per area)")
except ImportError:
    print("   rasterio is NOT installed.  Run:   pip install rasterio")

print("\n=== 3) (info only) Overpass / OpenStreetMap fallback ===")
q = f'[out:json][timeout:8];is_in({LAT},{LON})->.a;(area.a["landuse"];);out tags;'
for url in ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"):
    started = time.time()
    try:
        r = requests.post(url, data={"data": q}, headers={"User-Agent": "FireLink-NASA-SpaceApps-2026"}, timeout=12)
        print(f"   {url.split('/')[2]}: HTTP {r.status_code} in {time.time() - started:.1f}s")
    except requests.exceptions.Timeout:
        print(f"   {url.split('/')[2]}: TIMEOUT")
    except Exception as e:
        print(f"   {url.split('/')[2]}: {type(e).__name__}")
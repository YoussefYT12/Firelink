"""
check_landcover.py
------------------
Test land cover type on 4 known areas (doesn't modify any file in the project):

  python tools/check_landcover.py

Each area is tested at 9 points (3x3 grid, distance between points ~5 km), because a single point may fall on a house or road or small town. The actual result is the majority.
Uses the same project code (landcover.py). Requires rasterio + internet.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # to find landcover.py

from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from landcover import query_land_cover

STEP = 0.05  # Step (~5 km)

REGIONS = [
    ("Farmland - Iowa, USA (corn belt)", 42.0, -92.5, "agricultural"),
    ("Farmland - Punjab, India", 30.55, 75.45, "agricultural"),
    ("Forest - Black Forest, Germany", 48.30, 8.20, "wildfire/forest"),
    ("Desert - Sahara, Algeria", 23.0, 10.0, "low-vegetation"),
]

for name, lat, lon, expected in REGIONS:
    points = [(lat + dy * STEP, lon + dx * STEP) for dy in (-1, 0, 1) for dx in (-1, 0, 1)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda p: query_land_cover(*p), points))

    labels = Counter(r[0] if r else "no result" for r in results)
    winner, wins = labels.most_common(1)[0]
    print(f"-> {name}   (expected: {expected})")
    for label, n in labels.most_common():
        print(f"     {n}/9  {label}")
    first = next((r for r in results if r), None)
    if first:
        print(f"     example source: {first[1]}")
    print(f"   MAJORITY: {winner} ({wins}/9)\n")
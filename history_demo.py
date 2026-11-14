"""
history_demo.py
---------------
Generates a historical **demo (synthetic)** area in the same format as build_history.py files, so you can try the history screen
and the chart and the calibration immediately without waiting hours of downloads from NASA and without a key.

These data are simulated, not real fires: the file sets meta.synthetic=true and the UI shows a clear warning.
The tests (tests/) also use it to verify that calibration returns the true parameter we set.

  python build_history.py --demo
"""

import math
import random
from datetime import date, timedelta

import history

DEMO_NAME = "demo_synthetic"
DEMO_BBOX = "30.0,30.0,31.0,31.0"
TRUE_RATIO = 2.3        # VIIRS sees 2.3x of MODIS in the simulation (calibration must find it)


def _poisson(rng, lam):
    if lam <= 0:
        return 0
    if lam > 60:   # Normal approximation for large values
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def make_demo_store(name=DEMO_NAME, bbox=DEMO_BBOX, end=None, seed=7, true_ratio=TRUE_RATIO, tiles=12):
    """Full store (same format as history.new_store) from 2003 to end, with a peak season in October-November."""
    rng = random.Random(seed)
    end = end or (date.today() - timedelta(days=1))
    store = history.new_store(name, bbox)
    store["meta"]["synthetic"] = True
    w, s, e, n = (float(x) for x in bbox.split(","))
    tile_ids = [(int((w + rng.random() * (e - w - 0.1)) / 0.1), int((s + rng.random() * (n - s - 0.1)) / 0.1))
                for _ in range(tiles)]
    weights = [0.4 + 1.6 * rng.random() for _ in tile_ids]

    seasonal = {1: .4, 2: .3, 3: .4, 4: .6, 5: 1.0, 6: .9, 7: .5, 8: .6, 9: 1.2, 10: 3.2, 11: 2.6, 12: .8}
    year_boost = {2010: 1.6, 2016: 1.9, 2021: 1.4}     # Years with above-normal activity

    modis_start, viirs_start = date(2003, 1, 1), date(2012, 1, 20)
    months = []
    y, m = 2003, 1
    while date(y, m, 1) <= end:
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    for fam, start, ratio in (("MODIS", modis_start, 1.0), ("VIIRS", viirs_start, true_ratio)):
        for (y, m) in months:
            first = date(y, m, 1)
            nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
            lo, hi = max(first, start), min(nxt - timedelta(days=1), end)
            if hi < lo:
                continue
            frac = ((hi - lo).days + 1) / (nxt - first).days      # First/last months incomplete = fewer days
            trend = 1.0 + 0.02 * (y - 2003)
            for (tx, ty), wgt in zip(tile_ids, weights):
                lam = 14 * wgt * seasonal[m] * trend * year_boost.get(y, 1.0) * ratio
                lam *= frac * math.exp(rng.gauss(0, 0.18))          # monthly noise
                cd = _poisson(rng, lam)
                if cd:
                    store["acc"][fam][(f"{y:04d}-{m:02d}", tx, ty)] = [cd, cd * 2, round(cd * 11.5, 2)]
        d = start
        while d <= end:
            n_days = min(history.CHUNK_DAYS, (end - d).days + 1)
            store["done"][fam][d.isoformat()] = f"{fam}_SP"
            if n_days < history.CHUNK_DAYS:     # Last missing tile: stored in nrt field as history.py does
                store["nrt"][fam][d.isoformat()] = {"n": n_days, "rows": {}}
            d += timedelta(days=history.CHUNK_DAYS)
    return store

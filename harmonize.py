"""harmonize.py
------------
The statistical calibration between MODIS (since 2003) and VIIRS (since 2012) to produce **a single time series**.

The problem: VIIRS (375 m pixels) detects smaller fires than MODIS (1 km pixels), so if we merge the sensors
together it will look like "fires increased" in 2012 when only the sensor changed. The solution:

  1) Overlap period (2012 → present): both sensors see the same area in the same months. For each month
     we compute the ratio  VIIRS ÷ MODIS  (unit: cell-day, see history.py).
  2) The calibration factor k = median of these ratios. The median resists outlier months better than the mean.
     If there is enough overlap (4 years or more) we compute a factor for each month of the year (k_m) because the difference between sensors
     changes with season (fire type, clouds and view angle), with shrinkage towards the global k
     to prevent odd values for months with few samples.
  3) Before 2012: the unified value = k_m × MODIS, with an uncertainty range (from p10 and p90 of the ratios).
     From 2012: the value = VIIRS itself (no need to adjust the more accurate sensor).
  4) We measure calibration quality with leave-one-year-out: remove one year, calibrate on the rest, predict VIIRS for that year
     from MODIS and compute the mean relative error (cv_mape). This tells the user how much to trust the old numbers,
     not just a number that looks precise.

Constraints (we state them clearly in the UI): the calibration assumes the relationship between sensors is approximately
stable between 2003 and 2012, and that fire patterns in the overlap period are similar to those before it. Areas with low activity (less than
~3 cell-days per month) are not calibrated on their own.
"""

import math
import statistics

MIN_OVERLAP_MONTHS = 24     # Minimum number of overlap months required to trust the calibration
MIN_MODIS_COUNT = 3         # A MODIS month with fewer than that is not used in the ratio calculation (the ratio becomes noise)
SHRINK = 3.0                # weight of global k versus single-month samples
SEASONAL_MIN_YEARS = 4      # minimum number of years to use a coefficient per month
SEASONAL_MIN_SAMPLES = 3    # minimum number of samples per month for its coefficient to be calculated separately


def _quantile(sorted_vals, q):
    if not sorted_vals:
        return None
    pos = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def usable_pairs(pairs):
    """pairs: [(month 'YYYY-MM', modis, viirs)] ← we filter months whose noise fraction is high."""
    return [(m, mo, vi) for m, mo, vi in pairs if mo >= MIN_MODIS_COUNT and vi > 0]


def _fit(usable):
    """returns (global k, {month number: k_m}, seasonal?) from valid months only."""
    ratios = [vi / mo for _, mo, vi in usable]
    k = statistics.median(ratios)
    years = {int(m[:4]) for m, _, _ in usable}
    by_cal = {}
    for m, mo, vi in usable:
        by_cal.setdefault(int(m[5:7]), []).append(vi / mo)
    seasonal = len(years) >= SEASONAL_MIN_YEARS
    k_by_month = {}
    for c in range(1, 13):
        samples = by_cal.get(c, [])
        if seasonal and len(samples) >= SEASONAL_MIN_SAMPLES:
            med = statistics.median(samples)
            k_by_month[c] = (len(samples) * med + SHRINK * k) / (len(samples) + SHRINK)
        else:
            k_by_month[c] = k
    return k, k_by_month, seasonal


def _mape(usable, k_by_month):
    errs = []
    for m, mo, vi in usable:
        pred = k_by_month[int(m[5:7])] * mo
        errs.append(abs(pred - vi) / vi)
    return statistics.mean(errs) if errs else None


def calibrate(pairs):
    """
    pairs: [(month, modis_cell_days, viirs_cell_days)] for months where both are complete.
    returns dict with calibration coefficients and their quality, or None if the overlap is not sufficient.
    """
    usable = usable_pairs(pairs)
    if len(usable) < MIN_OVERLAP_MONTHS:
        return None
    k, k_by_month, seasonal = _fit(usable)
    ratios = sorted(vi / mo for _, mo, vi in usable)

    # leave-one-year-out: predict each year from calibration on the other years
    cv_errs = []
    for year in sorted({int(m[:4]) for m, _, _ in usable}):
        train = [p for p in usable if int(p[0][:4]) != year]
        test = [p for p in usable if int(p[0][:4]) == year]
        if len(train) < MIN_OVERLAP_MONTHS // 2 or not test:
            continue
        _, kbm, _ = _fit(train)
        cv_errs.append(_mape(test, kbm))
    cv_errs = [e for e in cv_errs if e is not None]

    return {
        "k": round(k, 4),
        "k_by_month": {c: round(v, 4) for c, v in k_by_month.items()},
        "seasonal": seasonal,
        "ratio_p10": round(_quantile(ratios, 0.10), 4),
        "ratio_p90": round(_quantile(ratios, 0.90), 4),
        "overlap_months": len(usable),
        "from": usable[0][0],
        "to": usable[-1][0],
        "pearson_r": None if _pearson([p[1] for p in usable], [p[2] for p in usable]) is None
        else round(_pearson([p[1] for p in usable], [p[2] for p in usable]), 3),
        "mape_in_sample": round(_mape(usable, k_by_month), 3),
        "cv_mape": round(statistics.mean(cv_errs), 3) if cv_errs else None,
    }


def harmonize_months(months, modis, viirs, status_modis, status_viirs, cal):
    """
    Builds the unified series. months: sorted list 'YYYY-MM'. modis/viirs: {month: cell_days}.
    status_*: {month: complete|partial|partial_start|gaps|absent}. cal: result of calibrate() or None.
    Each item: month, modis, viirs, unified, low, high, source, partial.
    """
    out = []
    for m in months:
        sm, sv = status_modis.get(m, "absent"), status_viirs.get(m, "absent")
        mo, vi = modis.get(m, 0), viirs.get(m, 0)
        row = {
            "month": m,
            "modis": mo if sm != "absent" else None,
            "viirs": vi if sv != "absent" else None,
            "unified": None, "low": None, "high": None, "source": "missing", "partial": False,
        }

        def from_modis(row, partial):
            if cal:
                k = cal["k_by_month"][int(m[5:7])]
                row["unified"] = round(k * mo, 1)
                row["low"] = round(cal["ratio_p10"] * mo, 1)
                row["high"] = round(cal["ratio_p90"] * mo, 1)
                row["source"] = "modis_calibrated"
            else:
                row["unified"] = float(mo)
                row["source"] = "modis_uncalibrated"
            row["partial"] = partial

        if sv == "complete":
            row["unified"], row["source"] = float(vi), "viirs"
        elif sm == "complete":
            from_modis(row, False)
        elif sv == "partial":
            row["unified"], row["source"], row["partial"] = float(vi), "viirs", True
        elif sm == "partial":
            from_modis(row, True)
        out.append(row)
    return out

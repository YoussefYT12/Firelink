import random
import unittest

from tests import helpers  # noqa: F401
import harmonize


def synth_pairs(k, years=range(2012, 2024), noise=0.1, seed=3):
    rng = random.Random(seed)
    out = []
    for y in years:
        for m in range(1, 13):
            base = 40 + 30 * (1 if m in (10, 11) else 0) + rng.randint(0, 20)
            modis = base
            viirs = max(1, round(k * base * (1 + rng.gauss(0, noise))))
            out.append((f"{y}-{m:02d}", modis, viirs))
    return out


class CalibrationTests(unittest.TestCase):
    def test_recovers_the_known_ratio(self):
        cal = harmonize.calibrate(synth_pairs(2.3))
        self.assertIsNotNone(cal)
        self.assertAlmostEqual(cal["k"], 2.3, delta=0.15)
        self.assertGreater(cal["pearson_r"], 0.9)
        self.assertLess(cal["cv_mape"], 0.25)

    def test_too_little_overlap_returns_none(self):
        self.assertIsNone(harmonize.calibrate(synth_pairs(2.0, years=range(2012, 2013))))

    def test_quiet_months_are_not_used_for_the_ratio(self):
        pairs = synth_pairs(2.0) + [("2030-01", 1, 50), ("2030-02", 2, 90)]   # MODIS near zero = noise level
        usable = harmonize.usable_pairs(pairs)
        self.assertNotIn("2030-01", [m for m, _, _ in usable])
        self.assertAlmostEqual(harmonize.calibrate(pairs)["k"], 2.0, delta=0.15)

    def test_seasonal_factors_follow_seasonal_difference(self):
        rng = random.Random(1)
        pairs = []
        for y in range(2012, 2024):
            for m in range(1, 13):
                k = 3.0 if m in (10, 11) else 1.5            # the two devices differ in burn season
                modis = 50 + rng.randint(0, 10)
                pairs.append((f"{y}-{m:02d}", modis, round(k * modis)))
        cal = harmonize.calibrate(pairs)
        self.assertTrue(cal["seasonal"])
        self.assertGreater(cal["k_by_month"][10], 2.6)
        self.assertLess(cal["k_by_month"][4], 1.8)


class HarmonizeMonthsTests(unittest.TestCase):
    def setUp(self):
        self.cal = {"k_by_month": {m: 2.0 for m in range(1, 13)}, "ratio_p10": 1.5, "ratio_p90": 2.5}
        self.months = ["2011-06", "2012-01", "2012-02", "2026-11"]
        self.modis = {"2011-06": 10, "2012-01": 20, "2012-02": 30, "2026-11": 5}
        self.viirs = {"2012-01": 12, "2012-02": 70, "2026-11": 9}
        self.st_m = {m: "complete" for m in self.months}
        self.st_m["2026-11"] = "partial"
        self.st_v = {"2011-06": "absent", "2012-01": "partial_start", "2012-02": "complete", "2026-11": "partial"}

    def test_sources_per_month(self):
        rows = {r["month"]: r for r in harmonize.harmonize_months(self.months, self.modis, self.viirs, self.st_m, self.st_v, self.cal)}
        self.assertEqual((rows["2011-06"]["source"], rows["2011-06"]["unified"]), ("modis_calibrated", 20.0))
        self.assertEqual((rows["2011-06"]["low"], rows["2011-06"]["high"]), (15.0, 25.0))
        self.assertEqual(rows["2012-01"]["source"], "modis_calibrated")      # First month VIIRS missing → use calibrated MODIS
        self.assertEqual((rows["2012-02"]["source"], rows["2012-02"]["unified"]), ("viirs", 70.0))
        self.assertTrue(rows["2026-11"]["partial"])
        self.assertEqual(rows["2026-11"]["source"], "viirs")

    def test_without_calibration_raw_modis_is_labelled_uncalibrated(self):
        rows = harmonize.harmonize_months(["2011-06"], {"2011-06": 10}, {}, {"2011-06": "complete"}, {}, None)
        self.assertEqual((rows[0]["source"], rows[0]["unified"]), ("modis_uncalibrated", 10.0))

    def test_no_coverage_is_missing_not_zero(self):
        rows = harmonize.harmonize_months(["2001-01"], {}, {}, {}, {}, self.cal)
        self.assertEqual((rows[0]["unified"], rows[0]["source"]), (None, "missing"))


if __name__ == "__main__":
    unittest.main()

import unittest
from datetime import date

from tests import helpers
import history
import history_analysis as ha
import history_demo

TODAY = date(2026, 11, 13)


class AnalyzeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = helpers.TempHistoryDir()
        cls.ctx.__enter__()
        history.save_store("demo_synthetic", history_demo.make_demo_store(end=date(2026, 11, 12)))

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__()

    def test_series_spans_2003_to_now_and_is_calibrated(self):
        r = ha.analyze(today=TODAY)
        self.assertEqual((r["meta"]["first_month"], r["meta"]["last_month"]), ("2003-01", "2026-11"))
        self.assertTrue(r["harmonization"]["calibrated"])
        self.assertAlmostEqual(r["harmonization"]["k"], history_demo.TRUE_RATIO, delta=0.25)
        sources = {m["month"]: m["source"] for m in r["months"]}
        self.assertEqual(sources["2005-06"], "modis_calibrated")
        self.assertEqual(sources["2020-06"], "viirs")

    def test_no_jump_at_the_2012_boundary(self):
        """The whole point of calibration: without calibration, the first year of VIIRS would appear ~2.3 times higher."""
        r = ha.analyze(today=TODAY)
        by_year = {y["year"]: y["unified"] for y in r["yearly"]}
        self.assertLess(by_year[2013] / by_year[2011], 1.6)
        raw_2013 = sum(m["modis"] or 0 for m in r["months"] if m["month"].startswith("2013"))
        self.assertGreater(by_year[2013] / raw_2013, 1.8)   # i.e., the normalized value is not raw MODIS

    def test_current_month_is_partial_projected_and_compared_to_average(self):
        c = ha.analyze(today=TODAY)["comparison"]
        self.assertEqual(c["month"], "2026-11")
        self.assertTrue(c["partial"])
        self.assertEqual((c["days_elapsed"], c["days_in_month"]), (12, 30))
        self.assertAlmostEqual(c["projected"], c["value"] * 30 / 12, delta=0.2)
        self.assertEqual(c["years_compared"], 23)
        self.assertIn(c["label"], ha.__dict__.get("_LABELS", ("well_above", "above", "typical", "below", "well_below")))

    def test_comparison_excludes_the_month_itself_and_later_years(self):
        c = ha.analyze(month="2015-10", today=TODAY)["comparison"]
        self.assertFalse(c["partial"])
        self.assertEqual(c["years_compared"], 12)            # 2003..2014
        self.assertLessEqual(c["rank"], c["of"])

    def test_peak_month_and_top_years_match_simulated_truth(self):
        r = ha.analyze(today=TODAY)
        self.assertIn(r["peak_calendar_month"]["month_num"], (10, 11))      # Peak in October-November in the simulation
        self.assertIn(r["top_years"][0]["year"], (2016, 2010, 2021))        # Years whose activity we increased
        self.assertTrue(all(not y.get("partial") for y in r["top_years"]))
        self.assertNotIn("2026-11", [m["month"] for m in r["top_months"]])  # The missing month does not enter the ranking

    def test_sub_box_uses_only_tiles_inside_it(self):
        whole = ha.analyze(today=TODAY)
        part = ha.analyze(bbox="30.0,30.0,30.4,30.4", today=TODAY)
        self.assertLess(part["meta"]["tiles"], whole["meta"]["tiles"])
        self.assertLess(sum(m["unified"] or 0 for m in part["months"]), sum(m["unified"] or 0 for m in whole["months"]))

    def test_box_outside_downloaded_region_explains_what_to_do(self):
        with self.assertRaises(ha.HistoryError) as cm:
            ha.analyze(bbox="10,10,11,11", today=TODAY)
        self.assertIn("Download history for this box", str(cm.exception))

    def test_invalid_boxes_are_rejected(self):
        for bad in ("5,5,4,6", "a,b,c,d", "0,0,200,10", "0,50,10,40"):
            with self.assertRaises(ha.HistoryError):
                ha.parse_bbox(bad)

    def test_failed_chunks_leave_month_out_instead_of_understating_it(self):
        store = history.load_store("demo_synthetic")
        mid = lambda fam, month: sorted(k for k in store["done"][fam] if k.startswith(month))[2]
        store["done"]["MODIS"].pop(mid("MODIS", "2005-03"))      # A segment failed mid-month
        store["done"]["VIIRS"].pop(mid("VIIRS", "2015-03"))
        history.save_store("gap_test", store)
        r = ha.analyze(region="gap_test", today=TODAY)
        rows = {m["month"]: m for m in r["months"]}
        self.assertEqual(rows["2005-03"]["source"], "missing")
        self.assertIsNone(rows["2005-03"]["unified"])
        self.assertTrue(any("missing days" in n for n in r["notes"]))
        # VIIRS missing in 2015-03 but MODIS is complete → use calibrated MODIS instead of discarding the month
        self.assertEqual(rows["2015-03"]["source"], "modis_calibrated")

    def test_demo_data_is_flagged(self):
        r = ha.analyze(today=TODAY)
        self.assertTrue(r["meta"]["synthetic"])
        self.assertTrue(any("DEMO" in n for n in r["notes"]))


class ComparisonTests(unittest.TestCase):
    def _rows(self, values):
        return {f"{y}-10": {"month": f"{y}-10", "unified": v, "partial": False, "source": "viirs"} for y, v in values.items()}

    def test_labels(self):
        base = {y: 100 for y in range(2015, 2025)}
        for value, label in ((200, "well_above"), (125, "above"), (100, "typical"), (80, "below"), (40, "well_below")):
            rows = self._rows({**base, 2025: value})
            self.assertEqual(ha.compare_month(rows, "2025-10")["label"], label, value)

    def test_not_enough_history(self):
        rows = self._rows({2023: 10, 2024: 12, 2025: 50})
        c = ha.compare_month(rows, "2025-10")
        self.assertEqual(c["label"], "no_reference")
        self.assertIn("at least", c["reason"])

    def test_missing_month(self):
        self.assertFalse(ha.compare_month({}, "2025-10")["available"])


class MonthStatusTests(unittest.TestCase):
    def test_status_kinds(self):
        d = date(2024, 2, 1).toordinal()
        covered = set(range(d, d + 29 + 31 + 10))      # February 2024 (29) + March (31) + 10 days of April
        self.assertEqual(ha.month_status(covered, "2024-02"), "complete")
        self.assertEqual(ha.month_status(covered, "2024-04"), "partial")
        self.assertEqual(ha.month_status(covered, "2024-05"), "absent")
        covered.discard(date(2024, 2, 10).toordinal())
        self.assertEqual(ha.month_status(covered, "2024-02"), "gaps")
        late = set(range(date(2012, 1, 20).toordinal(), date(2012, 3, 1).toordinal()))
        self.assertEqual(ha.month_status(late, "2012-01"), "partial_start")


if __name__ == "__main__":
    unittest.main()

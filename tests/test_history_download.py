import unittest
from datetime import date

from tests import helpers  # noqa: F401
import history


def csv_text(rows):
    head = "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight,type\n"
    return head + "\n".join(rows) + "\n"


class FilterTests(unittest.TestCase):
    def test_keep_row_rules(self):
        self.assertTrue(history.keep_row({"type": "0", "confidence": "n"}))
        self.assertTrue(history.keep_row({"type": "", "confidence": "h"}))
        self.assertFalse(history.keep_row({"type": "2", "confidence": "h"}))       # Fixed source (gas flare)
        self.assertFalse(history.keep_row({"type": "0", "confidence": "l"}))       # VIIRS low confidence
        self.assertFalse(history.keep_row({"type": "0", "confidence": "29"}))      # MODIS confidence < 30
        self.assertTrue(history.keep_row({"type": "0", "confidence": "30"}))


class AggregationTests(unittest.TestCase):
    def test_cell_day_counts_each_cell_once_per_day(self):
        # 3 VIIRS observations inside the same 1 km cell on the same day = one cell-day, but 3 detections
        rows = [
            {"latitude": "30.0012", "longitude": "31.0013", "acq_date": "2020-05-03", "frp": "2", "confidence": "n", "type": "0"},
            {"latitude": "30.0051", "longitude": "31.0066", "acq_date": "2020-05-03", "frp": "3", "confidence": "n", "type": "0"},
            {"latitude": "30.0090", "longitude": "31.0091", "acq_date": "2020-05-03", "frp": "5", "confidence": "h", "type": "0"},
            {"latitude": "30.0012", "longitude": "31.0013", "acq_date": "2020-05-04", "frp": "1", "confidence": "n", "type": "0"},
        ]
        out, kept, dropped = history.aggregate_rows(rows)
        self.assertEqual((kept, dropped), (4, 0))
        (rec,) = out.values()
        self.assertEqual(rec[0], 2)          # Two different days, same cell
        self.assertEqual(rec[1], 4)
        self.assertAlmostEqual(rec[2], 11.0)

    def test_bad_rows_are_dropped_not_crashing(self):
        out, kept, dropped = history.aggregate_rows([{"latitude": "x", "longitude": "1", "acq_date": "2020-01-01", "type": "0"}])
        self.assertEqual((kept, dropped, out), (0, 1, {}))

    def test_parse_rows_rejects_error_pages(self):
        with self.assertRaises(history.FirmsError):
            history.parse_rows("Invalid MAP_KEY.")


class PlanningTests(unittest.TestCase):
    def test_plan_chunks_covers_every_day_exactly_once(self):
        chunks = history.plan_chunks(date(2020, 1, 1), date(2020, 1, 12))
        self.assertEqual(chunks, [("2020-01-01", 5), ("2020-01-06", 5), ("2020-01-11", 2)])

    def test_choose_sources_uses_availability_when_known(self):
        avail = {"MODIS_SP": (date(2000, 11, 1), date(2026, 4, 1)), "MODIS_NRT": (date(2026, 1, 1), date(2026, 11, 12))}
        order, certain = history.choose_sources("MODIS_SP", "MODIS_NRT", date(2020, 1, 1), date(2020, 1, 5), avail, date(2026, 11, 13))
        self.assertEqual((order[0], certain), ("MODIS_SP", True))
        order, certain = history.choose_sources("MODIS_SP", "MODIS_NRT", date(2026, 11, 1), date(2026, 11, 5), avail, date(2026, 11, 13))
        self.assertEqual((order[0], certain), ("MODIS_NRT", True))

    def test_choose_sources_guesses_by_age_without_availability(self):
        order, certain = history.choose_sources("A_SP", "A_NRT", date(2026, 11, 1), date(2026, 11, 5), {}, date(2026, 11, 13))
        self.assertEqual((order[0], certain), ("A_NRT", False))


class ResumeTests(unittest.TestCase):
    """the bug that was fixed: the last-period missing slice must be reloaded when its days are complete, without duplicate counting."""

    def _run(self, store_holder, end, calls):
        def fake_fetch(family, bbox, c_start, n, key, avail, today, published=None):
            calls.append((family, c_start, n))
            # Each day has one observation in a fixed cell → The slice gives n cell-days
            rows = {("2026-11", 300, 300): [n, n, 1.0 * n]}
            return rows, n, f"{family}_NRT"

        from tests.helpers import TempHistoryDir
        real = history.fetch_chunk
        history.fetch_chunk = fake_fetch
        history.get_availability = lambda key: {}
        try:
            return history.build_history("t", "30,30,31,31", start=date(2026, 11, 1), end=end, workers=1, key="k", log=lambda *_: None)
        finally:
            history.fetch_chunk = real

    def test_partial_tail_chunk_is_completed_without_double_counting(self):
        from tests.helpers import TempHistoryDir
        with TempHistoryDir():
            calls = []
            store = self._run(None, date(2026, 11, 7), calls)           # 7 days → slice 5 + slice 2 (missing)
            m = history.monthly_rows(store, "VIIRS")[("2026-11", 300, 300)]
            self.assertEqual(m[0], 7)
            calls.clear()
            store = self._run(None, date(2026, 11, 12), calls)          # After 5 days: the missing slice is fully reloaded
            viirs_calls = [c for c in calls if c[0] == "VIIRS"]
            self.assertIn(("VIIRS", "2026-11-06", 5), viirs_calls)
            self.assertNotIn(("VIIRS", "2026-11-01", 5), viirs_calls)    # the full slice is not reloaded again
            m = history.monthly_rows(store, "VIIRS")[("2026-11", 300, 300)]
            self.assertEqual(m[0], 12)                                   # 12 days exactly (not 14)


if __name__ == "__main__":
    unittest.main()

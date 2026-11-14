"""Tests for burned area estimation (km²) + linking it to the calendar + the color palette in the UI."""

import os
import unittest
from datetime import date
from types import SimpleNamespace

import burned_area
import calendar_activity
from reconciliation import FireStatus
from tests import helpers


def det(lat, lon, d="2026-11-05", frp=1.0):
    return SimpleNamespace(latitude=lat, longitude=lon, acq_date=d, frp=frp)


def rec(lat, lon, viirs=True, modis=False, status=FireStatus.SINGLE_SOURCE, d="2026-11-05"):
    return SimpleNamespace(
        viirs_detection=det(lat, lon, d) if viirs else None,
        modis_detection=det(lat, lon, d) if modis else None,
        status=status,
    )


class EstimateTests(unittest.TestCase):
    def test_one_cell_counts_once_even_with_many_detections(self):
        # 3 detections in the same cell (~1 km) = one pixel, not 3
        out = burned_area.estimate([("VIIRS", 30.001, 31.001), ("VIIRS", 30.004, 31.002), ("VIIRS", 30.000, 31.000)])
        self.assertEqual(out["burned_pixels"], 1)
        self.assertAlmostEqual(out["burned_km2"], 0.1, places=1)

    def test_coarser_sensor_wins_inside_one_cell(self):
        # VIIRS + MODIS at the same location: we count once using MODIS area (1 km²)
        out = burned_area.estimate([("VIIRS", 30.001, 31.001), ("MODIS", 30.002, 31.003)])
        self.assertEqual(out["burned_pixels"], 1)
        self.assertEqual(out["burned_km2"], 1.0)

    def test_separate_cells_add_up(self):
        out = burned_area.estimate([("VIIRS", 30.0, 31.0), ("VIIRS", 30.5, 31.5), ("MODIS", 31.0, 32.0)])
        self.assertEqual(out["burned_pixels"], 3)
        self.assertAlmostEqual(out["burned_km2"], 1.3, places=1)    # 0.14 + 0.14 + 1.0

    def test_range_is_reported_around_the_estimate(self):
        out = burned_area.estimate([("VIIRS", 30.0, 31.0)])
        self.assertLessEqual(out["burned_km2_low"], out["burned_km2"])
        self.assertLessEqual(out["burned_km2"], out["burned_km2_high"])

    def test_empty_and_missing_coordinates_are_safe(self):
        self.assertEqual(burned_area.estimate([])["burned_km2"], 0.0)
        cells = {}
        burned_area.add(cells, "VIIRS", None, None)
        self.assertEqual(burned_area.finalize(cells)["burned_km2"], 0.0)

    def test_unknown_sensor_falls_back_to_the_larger_pixel(self):
        out = burned_area.estimate([("SOMETHING", 30.0, 31.0)])
        self.assertEqual(out["burned_km2"], 1.0)


class CalendarWiringTests(unittest.TestCase):
    def test_day_totals_include_burned_area(self):
        per_day = calendar_activity._aggregate_chunk([
            rec(30.00, 31.00, viirs=True, modis=True, status=FireStatus.CONFIRMED),
            rec(30.201, 31.201, viirs=True),
            rec(30.204, 31.204, viirs=True),        # same cell as above (~300 m)
        ])
        day = per_day["2026-11-05"]
        self.assertEqual(day["events"], 3)
        self.assertNotIn("_cells", day)              # temporary cells were deleted before the JSON
        self.assertEqual(day["burned_pixels"], 2)
        self.assertAlmostEqual(day["burned_km2"], 1.1, places=1)    # MODIS 1.0 + VIIRS 0.14

    def test_build_calendar_reports_a_period_total(self):
        real = calendar_activity._fetch_chunk
        try:
            calendar_activity._fetch_chunk = lambda bbox, start, n: (
                {start: {"events": 3, "confirmed": 1, "likely": 1, "single": 1, "weak": 0, "frp_sum": 5.0,
                         "burned_km2": 1.2, "burned_km2_low": 0.4, "burned_km2_high": 3.0, "burned_pixels": 3}}, 3)
            payload = calendar_activity.build_calendar("30.0,30.0,31.0,31.0", end_date="2026-11-13", days=14)
        finally:
            calendar_activity._fetch_chunk = real
            calendar_activity._CACHE.clear()
        s = payload["summary"]
        days_total = round(sum(d["burned_km2"] or 0 for d in payload["days"]), 1)
        self.assertEqual(s["burned_km2"], days_total)   # Total period = sum of days
        self.assertEqual(s["burned_pixels"],
                         sum(d["burned_pixels"] or 0 for d in payload["days"]))
        self.assertIn("estimate", s["burned_method"].lower())
        self.assertTrue(all("burned_km2" in d for d in payload["days"]))


class AccessibilityTests(unittest.TestCase):
    def test_map_page_has_the_colour_blind_toggle_and_burned_area(self):
        html = open(os.path.join(helpers.ROOT, "map.html"), encoding="utf-8").read()
        for needle in ("cvd-toggle", "PALETTES", "firelink_palette", "burned_km2", "fmtKm2", "body.cvd"):
            self.assertIn(needle, html, msg=needle)
        # color-blindness palette must have 4 colors different from the default
        self.assertIn("#D55E00", html)
        self.assertIn("#56B4E9", html)


if __name__ == "__main__":
    unittest.main()

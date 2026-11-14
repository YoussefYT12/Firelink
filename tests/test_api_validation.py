"""Tests for bbox checking and sanitizing error messages before returning to the browser."""

import unittest

from run_all import check_bbox, sanitize_error


class TestCheckBbox(unittest.TestCase):
    def test_accepts_world_and_valid_box(self):
        self.assertIsNone(check_bbox("world"))
        self.assertIsNone(check_bbox(""))
        self.assertIsNone(check_bbox("25.0,22.0,35.0,32.0"))
        self.assertIsNone(check_bbox("-180,-90,180,90"))

    def test_rejects_impossible_latitudes(self):
        with self.assertRaises(ValueError) as ctx:
            check_bbox("25,22,35,132")
        self.assertIn("Latitude", str(ctx.exception))

    def test_rejects_impossible_longitudes(self):
        with self.assertRaises(ValueError) as ctx:
            check_bbox("999,29,36,32")
        self.assertIn("Longitude", str(ctx.exception))

    def test_rejects_flipped_box(self):
        with self.assertRaises(ValueError):
            check_bbox("35,22,25,32")        # west greater than east
        with self.assertRaises(ValueError):
            check_bbox("25,32,35,22")        # south greater than north

    def test_rejects_degenerate_box(self):
        with self.assertRaises(ValueError):
            check_bbox("30,30,30,30")


class TestSanitizeError(unittest.TestCase):
    def test_masks_nasa_key(self):
        msg = ("FIRMS API error (400) for source 'VIIRS_SNPP_NRT':\n"
               "URL: https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
               "b0537799fbb3482c72a6138420f1f3c8/VIIRS_SNPP_NRT/999,29,36,32/1/2026-11-14\n"
               "Response: Invalid MAP_KEY.")
        out = sanitize_error(msg)
        self.assertNotIn("b0537799fbb3482c72a6138420f1f3c8", out)
        self.assertNotIn("https://", out)
        self.assertIn("Invalid MAP_KEY", out)

    def test_keeps_plain_messages(self):
        self.assertEqual(sanitize_error(ValueError("Draw a box first.")), "Draw a box first.")

    def test_shortens_very_long_messages(self):
        self.assertLessEqual(len(sanitize_error("x" * 2000)), 400)

    def test_handles_collapsed_whitespace(self):
        self.assertEqual(sanitize_error("a\n\n b\t c"), "a b c")


if __name__ == "__main__":
    unittest.main()

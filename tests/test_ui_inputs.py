"""Tests that the input fields (coordinates + GeoJSON file) are visible, not hidden inside details."""

import os
import re
import unittest

MAP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "map.html")


def _html():
    with open(MAP, encoding="utf-8") as fh:
        return fh.read()


class TestCoordinatesVisible(unittest.TestCase):
    def test_card_exists(self):
        self.assertIn('id="coords-card"', _html())

    def test_four_inputs_are_outside_the_collapsed_part(self):
        html = _html()
        card = html.index('id="coords-card"')
        collapsed = html.index('<details id="coords-details"')
        for el in ('id="c-west"', 'id="c-east"', 'id="c-south"', 'id="c-north"'):
            self.assertGreater(html.index(el), card)
            self.assertLess(html.index(el), collapsed)   # visible before the collapsed section

    def test_labels_are_human_readable(self):
        html = _html()
        for label in ("West longitude", "East longitude", "South latitude", "North latitude"):
            self.assertIn(label, html)

    def test_button_runs_the_search(self):
        html = _html()
        block = html[html.index("$('coords-box-btn')"):html.index("$('coords-pt-btn')")]
        self.assertIn("runQuery(", block)

    def test_point_radius_still_available(self):
        html = _html()
        for el in ('id="p-lat"', 'id="p-lon"', 'id="p-km"', 'id="coords-pt-btn"'):
            self.assertIn(el, html)


class TestFileInputVisible(unittest.TestCase):
    def test_card_exists(self):
        self.assertIn('id="file-card"', _html())

    def test_input_not_inside_details(self):
        html = _html()
        start = html.index('id="file-card"')
        end = html.index('id="file-input"')
        self.assertLess(start, end)
        self.assertNotIn("<details", html[start:end])

    def test_shows_loaded_file_name(self):
        html = _html()
        self.assertIn('id="file-name"', html)
        block = html[html.index("$('file-input')"):]
        self.assertIn("Loaded ' + file.name", block)

    def test_accepts_geojson(self):
        html = _html()
        m = re.search(r'<input type="file" id="file-input"[^>]*accept="([^"]+)"', html)
        self.assertIsNotNone(m)
        self.assertIn(".geojson", m.group(1))


if __name__ == "__main__":
    unittest.main()

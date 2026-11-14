"""Tests for collapsed map toggle + sidebar hide button.

The issue we're guarding against: the toggle was always open in the corner, so it covered the map
and gets cut off at the bottom on short screens (1366x766 for example).
"""

import os
import unittest

MAP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "map.html")


def _html():
    with open(MAP, encoding="utf-8") as fh:
        return fh.read()


class TestLegendIsCollapsible(unittest.TestCase):
    def test_starts_collapsed(self):
        html = _html()
        self.assertIn("let legendOpen = false;", html)

    def test_looks_like_a_button(self):
        html = _html()
        self.assertIn(".map-legend.collapsed { padding: 0; border-color: var(--accent)", html)
        self.assertIn("click to open", html)      # The text tells the user that the button

    def test_has_a_toggle_header(self):
        html = _html()
        self.assertIn("class=\"lg-head\"", html)
        self.assertIn("setLegendOpen(!legendOpen)", html)

    def test_body_is_hidden_when_collapsed(self):
        self.assertIn(".map-legend.collapsed .lg-body { display: none; }", _html())

    def test_cannot_grow_past_the_viewport(self):
        html = _html()
        self.assertIn("max-height: calc(100vh - 96px)", html)
        self.assertIn("overflow-y: auto", html)

    def test_choice_is_remembered(self):
        html = _html()
        self.assertIn("localStorage.getItem('firelink_legend_open')", html)
        self.assertIn("localStorage.setItem('firelink_legend_open'", html)

    def test_rebuild_keeps_the_open_state(self):
        """fillLegend rebuilds the HTML when the palette changes, so it must restore the open state."""
        html = _html()
        block = html[html.index("function fillLegend"):html.index("function setPalette")]
        self.assertIn("setLegendOpen(legendOpen)", block)

    def test_imagery_controls_are_inside_the_body(self):
        html = _html()
        body = html.index('<div class="lg-body">')
        self.assertLess(body, html.index('id="gibs-select"'))
        self.assertLess(body, html.index('id="cvd-toggle"'))


class TestSidebarToggle(unittest.TestCase):
    def test_button_exists(self):
        self.assertIn("side-toggle-btn", _html())

    def test_hiding_removes_the_panel(self):
        self.assertIn("body.side-hidden #sidebar { display: none; }", _html())

    def test_map_resizes_after_toggle(self):
        html = _html()
        block = html[html.index("function toggleSidebar"):html.index("sideToggle.addTo(map);")]
        self.assertIn("map.invalidateSize()", block)

    def test_choice_survives_reload(self):
        html = _html()
        self.assertIn("localStorage.getItem('firelink_side_hidden')", html)
        self.assertIn("localStorage.setItem('firelink_side_hidden'", html)


if __name__ == "__main__":
    unittest.main()

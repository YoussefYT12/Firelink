"""Tests of NASA GIBS imagery layer and the share link (bbox + date in the URL)."""

import os
import re
import unittest

MAP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "map.html")


def _html():
    with open(MAP, encoding="utf-8") as fh:
        return fh.read()


class TestGibsLayer(unittest.TestCase):
    def test_uses_wmts_template_order(self):
        html = _html()
        block = html[html.index("function gibsUrl"):html.index("function updateGibs")]
        self.assertIn("gibs.earthdata.nasa.gov/wmts/epsg3857/best/", block)
        # GIBS ordering is z/y/x not z/x/y
        self.assertIn("/{z}/{y}/{x}.jpg", block)
        self.assertIn("GoogleMapsCompatible_Level9", block)

    def test_layer_names(self):
        html = _html()
        self.assertIn("VIIRS_SNPP_CorrectedReflectance_TrueColor", html)
        self.assertIn("MODIS_Terra_CorrectedReflectance_TrueColor", html)

    def test_control_exists_in_legend(self):
        html = _html()
        for el in ('id="gibs-select"', 'id="gibs-opacity"', 'id="gibs-note"'):
            self.assertIn(el, html)

    def test_layer_follows_the_data_date(self):
        html = _html()
        block = html[html.index("function gibsDay"):html.index("function gibsUrl")]
        self.assertIn("currentMeta.date", block)

    def test_missing_imagery_is_explained(self):
        html = _html()
        block = html[html.index("function updateGibs"):html.index("function stateUrl")]
        self.assertIn("tileerror", block)
        self.assertIn("publishes about a day behind", block)


class TestShareLink(unittest.TestCase):
    def test_button_exists(self):
        self.assertIn('id="share-btn"', _html())

    def test_url_contains_bbox_date_days(self):
        html = _html()
        block = html[html.index("function stateUrl"):html.index("$('share-btn')")]
        for param in ("bbox", "date", "days"):
            self.assertIn(f"p.set('{param}'", block)

    def test_restores_state_on_load(self):
        html = _html()
        block = html[html.index("function initFromUrl"):html.index("function initFromUrl") + 1200]
        self.assertIn("new URLSearchParams(location.search)", block)
        self.assertIn("fillBoxInputs", block)
        self.assertIn("applyTypedArea", block)
        self.assertIn("runQuery", block)

    def test_boot_prefers_url_over_default_file(self):
        self.assertIn("if (!initFromUrl()) loadFile(false);", _html())

    def test_url_is_updated_after_each_query(self):
        html = _html()
        self.assertIn("history.replaceState(null, '', stateUrl())", html)

    def test_validates_parameters(self):
        html = _html()
        self.assertIn("const DATE_RE = /^\\d{4}-\\d{2}-\\d{2}$/;", html)
        self.assertIn("const BBOX_RE = ", html)


class TestNotifyModuleDocs(unittest.TestCase):
    def test_env_example_documents_email_and_weather(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.example")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for name in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "ALERT_EMAIL_TO", "OPENWEATHER_API_KEY"):
            self.assertIn(name, text)
        self.assertNotIn("TELEGRAM", text)      # bot removed in favor of email


if __name__ == "__main__":
    unittest.main()

"""UI filter tests (satellite / intensity / brightness) and burned area report."""

import os
import re
import unittest

from pipeline import results_to_geojson, payload_to_csv_rows, CSV_COLUMNS
from reconciliation import reconcile
from firms_client import FireDetection


MAP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "map.html")


def _html():
    with open(MAP, encoding="utf-8") as fh:
        return fh.read()


def _det(lat, lon, source, frp, bright, minutes=0):
    hhmm = "%02d%02d" % (10 + minutes // 60, minutes % 60)
    return FireDetection(latitude=lat, longitude=lon, brightness=bright, confidence="n",
                         acq_date="2026-11-13", acq_time=hhmm, frp=frp, source=source)


class TestFilterControls(unittest.TestCase):
    def test_controls_exist(self):
        html = _html()
        for el in ('id="f-sensor"', 'id="f-frp"', 'id="f-bright"', 'id="f-reset"', 'id="f-count"'):
            self.assertIn(el, html)

    def test_sensor_options(self):
        html = _html()
        for value in ('value="viirs"', 'value="modis"', 'value="both"', 'value="all"'):
            self.assertIn(value, html)

    def test_filter_logic_uses_properties(self):
        html = _html()
        # filter must read viirs / modis / frp_mw / brightness from point properties
        block = html[html.index("function matchesFilters"):html.index("function repaint")]
        for key in ("p.viirs", "p.modis", "p.frp_mw", "p.brightness"):
            self.assertIn(key, block)

    def test_timeline_and_filters_combine(self):
        html = _html()
        block = html[html.index("function repaint"):html.index("function syncFilterUI")]
        self.assertIn("timeCutoff", block)
        self.assertIn("matchesFilters", block)

    def test_truncated_counts_skipped_when_filtering(self):
        html = _html()
        self.assertIn("fileCounts && !filtersActive()", html)


class TestBrightnessField(unittest.TestCase):
    def test_geojson_has_brightness(self):
        viirs = [_det(30.0, 31.0, "VIIRS", 12.5, 340.2)]
        modis = [_det(30.0, 31.0, "MODIS", 10.0, 320.0, minutes=3)]
        payload = results_to_geojson(reconcile(viirs, modis))
        props = payload["features"][0]["properties"]
        self.assertIn("brightness", props)
        self.assertAlmostEqual(props["brightness"], 340.2, places=1)

    def test_csv_has_brightness_column(self):
        viirs = [_det(30.0, 31.0, "VIIRS", 12.5, 340.2)]
        payload = results_to_geojson(reconcile(viirs, []))
        header, rows = payload_to_csv_rows(payload)
        self.assertEqual(header, CSV_COLUMNS)
        self.assertIn("brightness_k", header)
        self.assertEqual(len(rows[0]), len(header))
        self.assertEqual(rows[0][header.index("brightness_k")], 340.2)


class TestBurnedReport(unittest.TestCase):
    def test_report_buttons_exist(self):
        html = _html()
        self.assertIn('id="cal-report-csv"', html)
        self.assertIn('id="cal-report-pdf"', html)

    def test_buttons_shown_with_calendar(self):
        html = _html()
        block = html[html.index("function renderCalendar"):html.index("async function buildCalendar")]
        self.assertIn("cal-report-csv').hidden = false", block)
        self.assertIn("cal-report-pdf').hidden = false", block)

    def test_report_contains_totals_and_method(self):
        html = _html()
        block = html[html.index("function burnedReportHtml"):]
        for token in ("Burned (estimate)", "Range", "1 km fire cells", "Day by day", "Method:"):
            self.assertIn(token, block)

    def test_pdf_uses_print_dialog(self):
        html = _html()
        start = html.index("$('cal-report-pdf').addEventListener")
        block = html[start:start + 900]
        self.assertIn("window.open", block)
        self.assertIn("w.print()", block)

    def test_report_is_english_only(self):
        # report prints in English like the rest of the UI (Arabic files get garbled on some printers)
        html = _html()
        block = html[html.index("function burnedReportHtml"):]
        template = block[block.index("return '<!DOCTYPE html>"):block.index("</body></html>")]
        self.assertFalse(re.search(r"[\u0600-\u06FF]", template))


if __name__ == "__main__":
    unittest.main()

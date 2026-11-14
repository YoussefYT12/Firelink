import csv
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import date

from tests import helpers
import history
import history_demo
import run_all
from pipeline import payload_to_csv_rows, results_to_geojson, write_csv, CSV_COLUMNS
from firms_client import FireDetection
from reconciliation import reconcile, FireStatus


def det(lat, lon, t, src, frp=10.0):
    return FireDetection(lat, lon, 330.0, "n", "2026-11-12", t, frp, src)


class ReconciliationTests(unittest.TestCase):
    def test_close_pair_is_confirmed_and_far_point_is_single(self):
        viirs = [det(30.0, 31.0, "1030", "VIIRS_SNPP_NRT"), det(10.0, 10.0, "1030", "VIIRS_SNPP_NRT")]
        modis = [det(30.002, 31.002, "1035", "MODIS_NRT")]
        res = reconcile(viirs, modis)
        statuses = sorted(r.status.name for r in res)
        self.assertEqual(statuses, ["CONFIRMED", "SINGLE_SOURCE"])


class CsvExportTests(unittest.TestCase):
    def setUp(self):
        res = reconcile([det(30.0, 31.0, "1030", "VIIRS_SNPP_NRT", 12.5)], [det(30.001, 31.001, "1032", "MODIS_NRT", 9)])
        self.payload = results_to_geojson(res)

    def test_geojson_carries_the_fields_the_csv_needs(self):
        p = self.payload["features"][0]["properties"]
        self.assertEqual((p["frp_mw"], p["viirs"], p["modis"]), (12.5, True, True))

    def test_csv_rows_match_geojson(self):
        header, rows = payload_to_csv_rows(self.payload)
        self.assertEqual(header, CSV_COLUMNS)
        self.assertEqual(len(rows), len(self.payload["features"]))
        self.assertEqual((rows[0][0], rows[0][1]), (self.payload["features"][0]["geometry"]["coordinates"][1], self.payload["features"][0]["geometry"]["coordinates"][0]))

    def test_written_csv_is_readable_by_excel_and_python(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.csv")
            write_csv(self.payload, path)
            raw = open(path, "rb").read()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))                       # BOM
            parsed = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
            self.assertEqual(len(parsed), 1)
            self.assertIn("Confirmed", parsed[0]["status"])
            self.assertEqual(parsed[0]["detected_by_viirs"], "True")


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hist = helpers.TempHistoryDir()
        cls.hist.__enter__()
        history.save_store("demo_synthetic", history_demo.make_demo_store(end=date(2026, 11, 12)))
        cls.httpd, cls.port = run_all.start_web_server()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.hist.__exit__()

    def get(self, path):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(f"http://127.0.0.1:{self.port}{path}") as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_history_endpoint(self):
        status, body = self.get("/api/history?bbox=30.0,30.0,30.6,30.6&month=2020-10")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["comparison"]["month"], "2020-10")
        self.assertTrue(data["months"])

    def test_history_regions_endpoint(self):
        status, body = self.get("/api/history/regions")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["regions"][0]["name"], "demo_synthetic")

    def test_history_bad_input_is_400_not_500(self):
        for q in ("bbox=abc", "bbox=1,2,3&month=2020-10", "month=2020-13", "region=../../etc", "bbox=10,10,11,11"):
            status, body = self.get("/api/history?" + q)
            self.assertEqual(status, 400, q)
            self.assertIn("error", json.loads(body))

    def test_secrets_and_source_are_not_served(self):
        for path in ("/.env", "/config.py", "/firms_client.py", "/history_data/demo_synthetic.json.gz", "/tests/helpers.py", "/../config.py"):
            status, _ = self.get(path)
            self.assertEqual(status, 404, path)

    def test_map_is_served_and_root_redirects(self):
        status, body = self.get("/map.html")
        self.assertEqual(status, 200)
        self.assertIn(b"hist-chart1", body)
        self.assertEqual(self.get("/")[0], 302)

    def test_map_page_has_csv_history_and_coordinate_controls(self):
        html = open(os.path.join(helpers.ROOT, "map.html"), encoding="utf-8").read()
        for needle in ("csv-btn", "cal-csv-btn", "hist-csv-btn", "coords-box-btn", "coords-pt-btn", "/api/history"):
            self.assertIn(needle, html)


class FirmsErrorPageTests(unittest.TestCase):
    """NASA responds 200 and with it an error text instead of CSV: we must treat it as an error, not "zero fires"."""

    class _Resp:
        def __init__(self, text, status=200):
            self.text, self.status_code, self.ok = text, status, status == 200

    def _fetch(self, text):
        import firms_client
        real = firms_client.requests.get
        firms_client.requests.get = lambda *a, **k: self._Resp(text)
        try:
            return firms_client.fetch_fire_data("30,30,31,31", "VIIRS_SNPP_NRT", 1, map_key="k")
        finally:
            firms_client.requests.get = real

    def test_error_text_is_an_error_not_zero_fires(self):
        with self.assertRaises(ValueError):
            self._fetch("Invalid MAP_KEY.")
        with self.assertRaises(ValueError):
            self._fetch("No data available for the requested area")

    def test_real_csv_is_parsed(self):
        rows = self._fetch(
            "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight,type\n"
            "30.1,31.2,330.1,0.4,0.4,2026-11-12,1030,N,VIIRS,2.0NRT,290.2,3.5,D,0\n"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].acq_date, "2026-11-12")

    def test_empty_body_is_zero_fires(self):
        self.assertEqual(self._fetch(""), [])


if __name__ == "__main__":
    unittest.main()

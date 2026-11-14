"""History download tests from inside the map: the API that runs the download in the background + progress percentage."""

import json
import os
import time
import unittest
import urllib.error
import urllib.request
from datetime import date

from tests import helpers
import history
import history_analysis
import history_demo
import history_jobs
import run_all

BOX = "30.0,30.0,31.0,31.0"
NEW_REGION = "area_30.00_30.00_31.00_31.00"


def http_get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def http_post(port, path, payload=None, raw=None):
    data = raw if raw is not None else json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def wait_for_state(port, states, timeout=8.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        _, data = http_get(port, "/api/history/build/status")
        last = data.get("job")
        if last and last["state"] in states:
            return last
        time.sleep(0.05)
    raise AssertionError(f"job never reached {states}, last={last}")


class BboxTests(unittest.TestCase):
    def test_bbox_is_normalised_to_two_decimals_outwards(self):
        # Approximation outside: the stored square must contain the square the user drew
        self.assertEqual(history_jobs.parse_bbox(" 30.004,30.006,31.0,31.0 "), "30.00,30.00,31.00,31.00")
        self.assertEqual(history_jobs.parse_bbox("-122.2745,35.1327,-115.7701,38.2161"),
                         "-122.28,35.13,-115.77,38.22")

    def test_stored_box_always_contains_the_drawn_box(self):
        for box in ("-122.2745,35.1327,-115.7701,38.2161", "29.5123,30.4123,31.9876,31.0001"):
            stored = [float(x) for x in history_jobs.parse_bbox(box).split(",")]
            drawn = [float(x) for x in box.split(",")]
            self.assertTrue(history_analysis._contains(stored, drawn), msg=f"{box} -> {stored}")

    def test_drawn_box_matches_the_region_downloaded_for_it(self):
        # Same as happened with the user: square drawn with 4 decimal places, and the area was stored rounded
        drawn = "-122.2745,35.1327,-115.7701,38.2161"
        with helpers.TempHistoryDir():
            name = history_jobs.region_name(history_jobs.parse_bbox(drawn))
            history.save_store(name, history_demo.make_demo_store(
                name=name, bbox=history_jobs.parse_bbox(drawn), end=date(2026, 11, 12), tiles=4))
            self.assertEqual(history_analysis.find_store(drawn), name)
            # And an old area stored rounded to the nearest (like the user's) should also match
            history_analysis._STORE_CACHE.clear()
            history.save_store("old_area", history_demo.make_demo_store(
                name="old_area", bbox="-122.27,35.13,-115.77,38.22", end=date(2026, 11, 12), tiles=4))
            self.assertEqual(history_analysis.find_store(drawn), "old_area")

    def test_region_name_is_stable_for_the_same_box(self):
        box = history_jobs.parse_bbox("30.0,30.0,31.0,31.0")
        self.assertEqual(history_jobs.region_name(box), NEW_REGION)
        self.assertEqual(history_jobs.region_name(history_jobs.parse_bbox("30,30,31,31")), NEW_REGION)

    def test_bad_boxes_are_rejected(self):
        for bad in ("", "world", "abc", "1,2,3", "31,30,30,31", "0,0,0.001,0.001", "-181,0,1,1"):
            with self.assertRaises(history_jobs.BuildError, msg=bad):
                history_jobs.parse_bbox(bad)

    def test_start_presets(self):
        today = date(2026, 11, 14)
        self.assertIsNone(history_jobs.resolve_start(None, today))
        self.assertIsNone(history_jobs.resolve_start("2003", today))
        self.assertEqual(history_jobs.resolve_start("2012", today), date(2012, 1, 20))
        self.assertEqual(history_jobs.resolve_start("12m", today), date(2025, 11, 14))
        self.assertEqual(history_jobs.resolve_start("2020", today), date(2020, 1, 1))
        with self.assertRaises(history_jobs.BuildError):
            history_jobs.resolve_start("last year", today)

    def test_area_drawn_with_other_decimals_reuses_the_download(self):
        # The user drew California with four decimal places, and the area was stored rounded
        with helpers.TempHistoryDir():
            history.save_store("area_-122.27_35.13_-115.77_38.22", history_demo.make_demo_store(
                name="area_-122.27_35.13_-115.77_38.22", bbox="-122.27,35.13,-115.77,38.22",
                end=date(2026, 11, 12), tiles=4))
            hit = history_jobs.matching_region("-122.2745,35.1327,-115.7701,38.2161")
            self.assertIsNotNone(hit)
            self.assertEqual(hit["name"], "area_-122.27_35.13_-115.77_38.22")
            # And a distant square does not match
            self.assertIsNone(history_jobs.matching_region("10.0,10.0,11.0,11.0"))

    def test_same_bbox_in_another_format_is_the_same_box(self):
        self.assertTrue(history.same_bbox("29.5,29.9,32.8,31.7", "29.50,29.90,32.80,31.70"))
        self.assertFalse(history.same_bbox("29.5,29.9,32.8,31.7", "29.5,29.9,32.8,31.8"))
        self.assertFalse(history.same_bbox("bad", "29.5,29.9,32.8,31.7"))

    def test_same_box_reuses_the_existing_region_name(self):
        with helpers.TempHistoryDir():
            history.save_store("egypt_delta", history_demo.make_demo_store(
                name="egypt_delta", bbox="29.5,29.9,32.8,31.7", end=date(2026, 11, 12), tiles=4))
            box = history_jobs.parse_bbox("29.5,29.9,32.8,31.7")
            self.assertEqual(history_jobs.existing_region_for(box), "egypt_delta")
            self.assertIsNone(history_jobs.existing_region_for("10.0,10.0,11.0,11.0"))


class BuildProgressTests(unittest.TestCase):
    """build_history itself: must report total and percent and stop on user request."""

    def setUp(self):
        self._fetch, self._avail, self._check = history.fetch_chunk, history.get_availability, history.check_key
        history.fetch_chunk = lambda fam, bbox, c, n, key, avail, today, published=None: ({}, 0, f"{fam}_NRT")
        history.get_availability = lambda key: {}
        history.check_key = lambda *a, **k: None
        self.dir = helpers.TempHistoryDir()
        self.dir.__enter__()

    def tearDown(self):
        history.fetch_chunk, history.get_availability, history.check_key = self._fetch, self._avail, self._check
        self.dir.__exit__()

    def test_progress_goes_from_planning_to_100_percent(self):
        events = []
        history.build_history("t", BOX, start=date(2026, 11, 1), end=date(2026, 11, 20), workers=1, key="k",
                              log=lambda *_: None, progress=events.append)
        self.assertEqual(events[0]["phase"], "planning")
        downloading = [e for e in events if e["phase"] == "downloading"]
        self.assertEqual(downloading[0]["total"], 8)          # 20 days × 2 months ÷ 5 days = 8 slices
        self.assertEqual(downloading[-1]["percent"], 100)
        self.assertEqual(events[-1]["phase"], "done")
        percents = [e["percent"] for e in downloading]
        self.assertEqual(percents, sorted(percents))          # Does not go back

    def test_should_stop_saves_and_reports_cancelled(self):
        events = []
        store = history.build_history("t", BOX, start=date(2026, 1, 1), end=date(2026, 11, 13), workers=1, key="k",
                                      log=lambda *_: None, progress=events.append, should_stop=lambda: True)
        self.assertEqual(events[-1]["phase"], "cancelled")
        self.assertIsNotNone(store)
        self.assertTrue(os.path.exists(history.store_path("t")))   # What was uploaded was saved



class TailChunkTests(unittest.TestCase):
    """Latest days that NASA still hasn't published: failure (retry) not zero fires."""

    def test_published_tail_is_not_recorded_as_zero(self):
        # Tile not yet published by NASA and empty response ⇒ failure, not \"zero fires\"
        empty = "latitude,longitude,acq_date,acq_time,confidence,frp,type\n"
        real = history._download
        history._download = lambda url, attempts=4: empty
        try:
            with self.assertRaises(history.FirmsError):
                history.fetch_chunk("VIIRS", BOX, "2026-11-01", 5, "k", {}, date(2026, 11, 14), date(2026, 10, 31))
            # And the same tile if NASA did publish it: real zero fires, not a failure
            rows, kept, src = history.fetch_chunk("VIIRS", BOX, "2026-11-01", 5, "k", {}, date(2026, 11, 14), date(2026, 11, 30))
            self.assertEqual(kept, 0)
            self.assertTrue(src.endswith("_NRT"))
        finally:
            history._download = real


class BuildApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hist = helpers.TempHistoryDir()
        cls.hist.__enter__()
        cls.httpd, cls.port = run_all.start_web_server()
        cls._real_build = history.build_history
        history.build_history = cls.fake_build

    @classmethod
    def tearDownClass(cls):
        history.build_history = cls._real_build
        history_jobs._job = None
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.hist.__exit__()

    @staticmethod
    def fake_build(name, bbox, start=None, end=None, workers=3, key=None, log=print,
                   progress=None, should_stop=None, **kw):
        """Quick alternative to history.build_history: increments progress and saves a placeholder area."""
        def report(done):
            if progress:
                progress({"phase": "downloading", "total": 4, "done": done, "failed": 0,
                          "percent": round(100 * done / 4, 1), "message": "Downloading from NASA…"})
        report(0)
        store = history_demo.make_demo_store(name=name, bbox=bbox, end=date(2026, 11, 12), tiles=6)
        for i in range(1, 5):
            time.sleep(0.05)
            if should_stop and should_stop():
                history.save_store(name, store)
                if progress:
                    progress({"phase": "cancelled", "total": 4, "done": i - 1, "failed": 0,
                              "percent": round(100 * (i - 1) / 4, 1), "message": "Stopped."})
                return store
            report(i)
        history.save_store(name, store)
        if progress:
            progress({"phase": "done", "total": 4, "done": 4, "failed": 0, "percent": 100,
                      "message": "Download finished."})
        return store

    def test_download_from_the_map_creates_the_region(self):
        status, data = http_post(self.port, "/api/history/build", {"bbox": BOX, "start": "2012"})
        self.assertEqual(status, 202, data)
        self.assertEqual(data["job"]["state"], "running")
        self.assertEqual(data["job"]["region"], NEW_REGION)

        job = wait_for_state(self.port, {"done"})
        self.assertEqual(job["progress"]["percent"], 100)
        self.assertGreater(job["result"]["chunks_done"], 0)
        self.assertEqual(job["result"]["region"], NEW_REGION)

        # The new area is now readable in /api/history and also in the area list
        status, hist = http_get(self.port, "/api/history?bbox=30.1,30.1,30.5,30.5")
        self.assertEqual(status, 200, hist)
        self.assertEqual(hist["meta"]["region"], NEW_REGION)
        _, regions = http_get(self.port, "/api/history/regions")
        self.assertIn(NEW_REGION, [r["name"] for r in regions["regions"]])

    def test_second_download_is_rejected_while_one_runs(self):
        history_jobs._job = None
        self.assertEqual(http_post(self.port, "/api/history/build", {"bbox": "31.0,31.0,32.0,32.0"})[0], 202)
        status, data = http_post(self.port, "/api/history/build", {"bbox": "33.0,33.0,34.0,34.0"})
        self.assertEqual(status, 409, data)
        self.assertIn("already running", data["error"].lower())
        wait_for_state(self.port, {"done", "cancelled", "error"})

    def test_cancel_stops_the_running_download(self):
        history_jobs._job = None
        http_post(self.port, "/api/history/build", {"bbox": "35.0,35.0,36.0,36.0"})
        status, data = http_post(self.port, "/api/history/build/cancel")
        self.assertEqual(status, 200)
        self.assertTrue(data["cancelled"])
        job = wait_for_state(self.port, {"cancelled", "done"})
        self.assertEqual(job["state"], "cancelled")

    def test_bad_requests_are_400(self):
        history_jobs._job = None
        cases = [
            {},                                                     # No box
            {"bbox": "world"},
            {"bbox": "abc"},
            {"bbox": "-100,-40,100,40"},                            # Area larger than the limit
            {"bbox": BOX, "start": "tomorrow"},
            {"bbox": BOX, "start": "2026-11-01", "end": "2026-01-01"},
        ]
        for body in cases:
            status, data = http_post(self.port, "/api/history/build", body)
            self.assertEqual(status, 400, body)
            self.assertIn("error", data)
        status, data = http_post(self.port, "/api/history/build", raw=b"not json")
        self.assertEqual(status, 400)

    def test_uncovered_area_asks_for_a_download(self):
        status, data = http_get(self.port, "/api/history?bbox=-60,-20,-50,-10")
        self.assertEqual(status, 400)
        self.assertTrue(data.get("needs_download"))
        self.assertIn("Download history for this box", data["error"])

    def test_map_page_has_the_download_control(self):
        html = open(os.path.join(helpers.ROOT, "map.html"), encoding="utf-8").read()
        for needle in ("dl-btn", "dl-cancel", "dl-fill", "/api/history/build", "dl-range"):
            self.assertIn(needle, html)


if __name__ == "__main__":
    unittest.main()

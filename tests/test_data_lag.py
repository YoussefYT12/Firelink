"""Tests for NASA publishing delay: if the requested day is not yet published, we must show the latest available day and state it explicitly."""

import unittest
from datetime import datetime, timedelta, timezone

import pipeline
from firms_client import FireDetection

TODAY = datetime.now(timezone.utc).date()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()


def det(day):
    return FireDetection(latitude=30.05, longitude=31.25, brightness=330.0, confidence="n",
                         acq_date=day, acq_time="1200", frp=3.0, source="VIIRS_SNPP_NRT")


class DataLagTests(unittest.TestCase):
    def setUp(self):
        self.real = pipeline.fetch_viirs_and_modis_auto
        self.real_published = pipeline.published_through
        pipeline.published_through = lambda *a, **k: None   # tests without network
        pipeline._CACHE.clear()

    def tearDown(self):
        pipeline.fetch_viirs_and_modis_auto = self.real
        pipeline.published_through = self.real_published
        pipeline._CACHE.clear()

    def test_today_not_published_falls_back_to_the_newest_day(self):
        calls = []

        def fake(bbox, date=None, day_range=1):
            calls.append((date, day_range))
            if date is None:                      # mode \"live\": latest 3 days
                return [det(YESTERDAY)], []
            return [], []                         # the requested day is empty (not published)

        pipeline.fetch_viirs_and_modis_auto = fake
        payload = pipeline.run_pipeline("29,22,36,32", date=TODAY.isoformat(), days=1, use_cache=False)
        meta = payload["meta"]

        self.assertEqual(meta["date"], YESTERDAY)
        self.assertEqual(meta["requested_date"], TODAY.isoformat())
        self.assertEqual(meta["data_lag_days"], 1)
        self.assertIn("newest day available", meta["note"])
        self.assertEqual(len(payload["features"]), 1)      # point appeared instead of empty map
        self.assertEqual(calls[0][0], TODAY.isoformat())    # try the requested date first

    def test_live_view_also_reports_the_real_day(self):
        pipeline.fetch_viirs_and_modis_auto = lambda bbox, date=None, day_range=1: (
            [det(YESTERDAY)], [] if date is None else ([], []))
        payload = pipeline.run_pipeline("world", date=None, days=1, use_cache=False)
        self.assertEqual(payload["meta"]["date"], YESTERDAY)
        self.assertEqual(payload["meta"]["data_lag_days"], 1)

    def test_old_date_with_no_fires_is_left_alone(self):
        # old empty date = no fires actually, not a publishing delay: do not skip to the latest day
        old = "2019-06-01"
        calls = []

        def fake(bbox, date=None, day_range=1):
            calls.append(date)
            return [], []

        pipeline.fetch_viirs_and_modis_auto = fake
        payload = pipeline.run_pipeline("29,22,36,32", date=old, days=1, use_cache=False)
        self.assertEqual(payload["meta"]["date"], old)
        self.assertIsNone(payload["meta"]["note"])
        self.assertEqual(calls, [old])             # no extra request to NASA

    def test_when_the_day_has_data_nothing_changes(self):
        pipeline.fetch_viirs_and_modis_auto = lambda bbox, date=None, day_range=1: ([det(TODAY.isoformat())], [])
        payload = pipeline.run_pipeline("29,22,36,32", date=TODAY.isoformat(), days=1, use_cache=False)
        self.assertEqual(payload["meta"]["date"], TODAY.isoformat())
        self.assertIsNone(payload["meta"]["note"])
        self.assertEqual(payload["meta"]["data_lag_days"], 0)   # zero = no delay


if __name__ == "__main__":
    unittest.main()

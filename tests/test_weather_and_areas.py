"""Tests wind direction (OpenWeather) and nearest protected area (OSM) — without grid."""

import unittest
from unittest import mock

import protected_areas
import weather


class TestCompass(unittest.TestCase):
    def test_cardinal_points(self):
        self.assertEqual(weather.compass(0), "N")
        self.assertEqual(weather.compass(90), "E")
        self.assertEqual(weather.compass(180), "S")
        self.assertEqual(weather.compass(270), "W")

    def test_in_between_points(self):
        self.assertEqual(weather.compass(45), "NE")
        self.assertEqual(weather.compass(225), "SW")
        self.assertEqual(weather.compass(359), "N")     # rotates around north


class TestWind(unittest.TestCase):
    def setUp(self):
        weather.clear_cache()

    def test_without_key_returns_none(self):
        with mock.patch.object(weather, "api_key", return_value=""):
            self.assertIsNone(weather.get_wind(30, 31))

    def test_direction_and_speed(self):
        fake = lambda lat, lon, key: {"wind": {"speed": 5.0, "deg": 90}, "name": "Cairo"}
        wind = weather.get_wind(30.05, 31.25, key="k", fetch=fake)
        self.assertEqual(wind["from"], "E")
        self.assertEqual(wind["toward"], "W")          # fire moves opposite to wind source
        self.assertEqual(wind["toward_deg"], 270.0)
        self.assertAlmostEqual(wind["speed_kmh"], 18.0, places=1)

    def test_missing_deg_returns_none(self):
        self.assertIsNone(weather.get_wind(30, 31, key="k", fetch=lambda *a: {"wind": {"speed": 3}}))

    def test_network_error_returns_none(self):
        def boom(*a):
            raise OSError("no network")
        self.assertIsNone(weather.get_wind(30, 31, key="k", fetch=boom))

    def test_cache_avoids_second_call(self):
        calls = []
        def fake(lat, lon, key):
            calls.append(1)
            return {"wind": {"speed": 2.0, "deg": 10}}
        weather.get_wind(30.0, 31.0, key="k", fetch=fake)
        weather.get_wind(30.0, 31.0, key="k", fetch=fake)
        self.assertEqual(len(calls), 1)

    def test_sentence(self):
        text = weather.drift_sentence({"speed_kmh": 18.0, "from": "N", "toward": "S"})
        self.assertIn("18 km/h from N", text)
        self.assertIn("towards S", text)
        self.assertIsNone(weather.drift_sentence(None))


class TestProtectedAreas(unittest.TestCase):
    def setUp(self):
        protected_areas.clear_cache()

    def test_haversine(self):
        # Cairo ← Alexandria ≈ 180 km
        self.assertAlmostEqual(protected_areas.haversine_km(30.04, 31.24, 31.20, 29.92), 180, delta=12)

    def test_picks_nearest_named_area(self):
        def fake_post(query):
            return {"elements": [
                {"type": "relation", "id": 1, "tags": {"name": "Far Park", "boundary": "national_park"},
                 "center": {"lat": 30.6, "lon": 31.6}},
                {"type": "way", "id": 2, "tags": {"name": "Near Reserve", "leisure": "nature_reserve"},
                 "center": {"lat": 30.1, "lon": 31.3}},
                {"type": "way", "id": 3, "tags": {"boundary": "protected_area"},
                 "center": {"lat": 30.05, "lon": 31.25}},   # No name → ignore
            ]}
        area = protected_areas.lookup(30.05, 31.25, post=fake_post)
        self.assertEqual(area["name"], "Near Reserve")
        self.assertEqual(area["designation"], "nature reserve")
        self.assertLess(area["distance_km"], 10)

    def test_none_when_nothing_found(self):
        self.assertIsNone(protected_areas.lookup(0, 0, post=lambda q: {"elements": []}))

    def test_service_failure_is_quiet(self):
        def boom(query):
            raise OSError("overpass down")
        self.assertIsNone(protected_areas.lookup(12.3, 45.6, post=boom))

    def test_cache_avoids_second_query(self):
        calls = []
        def fake_post(query):
            calls.append(1)
            return {"elements": [{"type": "way", "id": 9, "tags": {"name": "P", "boundary": "protected_area"},
                                  "center": {"lat": 10.0, "lon": 20.0}}]}
        protected_areas.lookup(10.0, 20.0, post=fake_post)
        protected_areas.lookup(10.0, 20.0, post=fake_post)
        self.assertEqual(len(calls), 1)


class TestAlertEnrichment(unittest.TestCase):
    def test_enrichment_is_optional_and_never_raises(self):
        import pipeline
        alerts = [{"latitude": 30.0, "longitude": 31.0, "place": "x"}]
        with mock.patch.dict("os.environ", {"FIRELINK_ENRICH": "0"}):
            pipeline._enrich_alerts(alerts)
        self.assertNotIn("wind", alerts[0])

        alerts = [{"latitude": 30.0, "longitude": 31.0, "place": "x"}]
        with mock.patch("weather.get_wind", side_effect=RuntimeError("boom")), \
                mock.patch("protected_areas.lookup", side_effect=RuntimeError("boom")):
            pipeline._enrich_alerts(alerts)          # don't throw exception
        self.assertEqual(alerts[0]["place"], "x")


if __name__ == "__main__":
    unittest.main()

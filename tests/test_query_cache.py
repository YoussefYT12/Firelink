"""Tests for results cache /api/query + port support from environment variable PORT.

Cache is important at presentation time: the same region and the same date return instantly, so flipping the map
in front of the judges doesn't have to wait 10 seconds each time, and the NASA key quota isn't spent twice on the same query.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_all  # noqa: E402


class TestQueryCache(unittest.TestCase):
    def setUp(self):
        run_all._QUERY_CACHE.clear()
        run_all.QUERY_CACHE_TTL = 600

    def test_returns_what_was_stored(self):
        run_all.cache_put(("world", "", 1), {"features": [1, 2, 3]})
        self.assertEqual(run_all.cache_get(("world", "", 1)), {"features": [1, 2, 3]})

    def test_different_area_is_a_different_entry(self):
        run_all.cache_put(("world", "", 1), {"a": 1})
        self.assertIsNone(run_all.cache_get(("10,10,20,20", "", 1)))

    def test_expired_entry_is_dropped(self):
        run_all.QUERY_CACHE_TTL = 0.01
        run_all.cache_put(("world", "", 1), {"a": 1})
        time.sleep(0.05)
        self.assertIsNone(run_all.cache_get(("world", "", 1)))

    def test_cache_is_capped(self):
        for i in range(run_all.QUERY_CACHE_MAX + 5):
            run_all.cache_put((f"box{i}", "", 1), {"i": i})
        self.assertLessEqual(len(run_all._QUERY_CACHE), run_all.QUERY_CACHE_MAX)


class TestPortOption(unittest.TestCase):
    def test_start_web_server_accepts_explicit_port(self):
        import inspect
        sig = inspect.signature(run_all.start_web_server)
        self.assertIn("port", sig.parameters)

    def test_main_has_a_port_flag(self):
        src = inspect_source()
        self.assertIn('"--port"', src)

    def test_env_port_is_read(self):
        self.assertIn('os.environ.get("PORT"', inspect_source())


def inspect_source():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run_all.py")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


if __name__ == "__main__":
    unittest.main()

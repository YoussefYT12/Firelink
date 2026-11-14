"""Email alert tests (without any network or real SMTP connection)."""

import json
import os
import tempfile
import unittest
from unittest import mock

import notify


def fake_payload(frps, date="2026-11-13", note=None, partial=False):
    features = []
    for i, frp in enumerate(frps):
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [31.0 + i * 0.1, 30.0 + i * 0.1]},
            "properties": {"status": "Confirmed (both)", "frp_mw": frp, "brightness": 340.0,
                           "viirs": True, "modis": True},
        })
    return {"features": features, "meta": {"date": date, "note": note, "partial_day": partial},
            "counts": {"confirmed": len(features), "likely": 0, "single": 0, "weak": 0}}


class FakeSMTP:
    """Alternative to smtplib.SMTP — logs what happened instead of opening a real connection."""
    instances = []

    def __init__(self, host, port, timeout=20, context=None):
        self.host, self.port = host, port
        self.started_tls = False
        self.logged_in = None
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


SETTINGS = {"host": "smtp.example.com", "port": 587, "security": "starttls",
            "user": "bot@example.com", "password": "secret",
            "to": "a@example.com, b@example.com", "sender": "FireLink <bot@example.com>"}


class TestSummarize(unittest.TestCase):
    def test_counts_only_detections_above_threshold(self):
        res = notify.summarize("25,22,35,32", min_frp=30, pipeline=lambda *a, **k: fake_payload([10, 31, 55, 29.9]))
        self.assertEqual(res["total"], 4)
        self.assertEqual(res["strong"], 2)
        self.assertEqual(res["max_frp"], 55)
        self.assertTrue(res["triggered"])

    def test_not_triggered_below_min_count(self):
        res = notify.summarize("25,22,35,32", min_frp=30, min_count=3,
                               pipeline=lambda *a, **k: fake_payload([40, 50]))
        self.assertFalse(res["triggered"])

    def test_missing_frp_is_ignored(self):
        res = notify.summarize("25,22,35,32", min_frp=30, pipeline=lambda *a, **k: fake_payload([None, 40]))
        self.assertEqual(res["strong"], 1)


class TestMessage(unittest.TestCase):
    def test_subject_and_body_mention_area_day_and_peak(self):
        res = notify.summarize("25,22,35,32", min_frp=30, pipeline=lambda *a, **k: fake_payload([40, 70]))
        subject, text, html = notify.build_message(res)
        self.assertIn("25,22,35,32", subject)
        self.assertIn("2026-11-13", subject)
        self.assertIn("70 MW", text)
        self.assertIn("<table", html)
        self.assertIn("2 detection(s)", html)

    def test_wind_and_protected_area_appear_in_the_email(self):
        res = notify.summarize("25,22,35,32", min_frp=30, pipeline=lambda *a, **k: fake_payload([70]))
        notify.enrich_fire(
            res["fires"][0],
            wind_lookup=lambda lat, lon: {"from": "N", "toward": "S", "speed_kmh": 18, "toward_deg": 180},
            area_lookup=lambda lat, lon: {"name": "Test Park", "designation": "national park", "distance_km": 4.2},
        )
        _, text, html = notify.build_message(res)
        self.assertIn("spreads towards S", text)
        self.assertIn("Test Park", text)
        self.assertIn("Test Park", html)

    def test_partial_day_is_explained(self):
        res = notify.summarize("world", min_frp=30, pipeline=lambda *a, **k: fake_payload([40], partial=True))
        _, text, _ = notify.build_message(res)
        self.assertIn("still being published", text)

    def test_quiet_message_when_nothing_passes(self):
        res = notify.summarize("world", min_frp=30, pipeline=lambda *a, **k: fake_payload([5]))
        subject, text, _ = notify.build_message(res)
        self.assertIn("no detections above 30 MW", subject)
        self.assertIn("0 detection(s)", text)


class TestSending(unittest.TestCase):
    def setUp(self):
        FakeSMTP.instances = []

    def test_missing_host_raises_clear_error(self):
        with self.assertRaises(notify.NotifyError) as ctx:
            notify.send_email("s", "t", settings={"to": "a@b.c", "sender": "x@y.z"}, smtp_factory=FakeSMTP)
        self.assertIn("SMTP_HOST", str(ctx.exception))

    def test_missing_recipient_raises_clear_error(self):
        with self.assertRaises(notify.NotifyError) as ctx:
            notify.send_email("s", "t", settings={"host": "h", "to": "", "sender": "x@y.z"}, smtp_factory=FakeSMTP)
        self.assertIn("ALERT_EMAIL_TO", str(ctx.exception))

    def test_sends_html_and_text_with_starttls(self):
        notify.send_email("FireLink alert", "plain body", "<b>html body</b>",
                          settings=SETTINGS, smtp_factory=FakeSMTP)
        s = FakeSMTP.instances[0]
        self.assertEqual((s.host, s.port), ("smtp.example.com", 587))
        self.assertTrue(s.started_tls)
        self.assertEqual(s.logged_in, ("bot@example.com", "secret"))
        msg = s.sent[0]
        self.assertEqual(msg["Subject"], "FireLink alert")
        self.assertEqual(msg["To"], "a@example.com, b@example.com")
        self.assertEqual(msg.get_body(("plain",)).get_content().strip(), "plain body")
        self.assertIn("html body", msg.get_body(("html",)).get_content())

    def test_ssl_mode_does_not_starttls(self):
        notify.send_email("s", "t", settings={**SETTINGS, "security": "ssl"}, smtp_factory=FakeSMTP)
        self.assertFalse(FakeSMTP.instances[0].started_tls)


class TestDedupe(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_same_area_and_day_alerts_once(self):
        self.assertFalse(notify.already_alerted("25,22,35,32", 30, "2026-11-13", self.path))
        notify.mark_alerted("25,22,35,32", 30, "2026-11-13", self.path)
        self.assertTrue(notify.already_alerted("25,22,35,32", 30, "2026-11-13", self.path))

    def test_different_day_or_threshold_is_not_blocked(self):
        notify.mark_alerted("25,22,35,32", 30, "2026-11-13", self.path)
        self.assertFalse(notify.already_alerted("25,22,35,32", 30, "2026-11-14", self.path))
        self.assertFalse(notify.already_alerted("25,22,35,32", 50, "2026-11-13", self.path))

    def test_state_file_is_readable_json(self):
        notify.mark_alerted("world", 80, "2026-11-13", self.path)
        with open(self.path, encoding="utf-8") as fh:
            self.assertIn("world|80", json.load(fh))


class TestCli(unittest.TestCase):
    def test_dry_run_prints_and_sends_nothing(self):
        sent = []
        result = notify.summarize("world", min_frp=1, pipeline=lambda *x, **y: fake_payload([50]))
        with mock.patch.object(notify, "summarize", lambda *a, **k: result), \
                mock.patch.object(notify, "send_email", lambda *a, **k: sent.append(1)):
            code = notify.main(["--bbox", "world", "--dry-run", "--force", "--no-enrich"])
        self.assertEqual(code, 0)
        self.assertEqual(sent, [])

    def test_quiet_exit_code_when_below_threshold(self):
        result = notify.summarize("world", min_frp=30, pipeline=lambda *x, **y: fake_payload([1]))
        with mock.patch.object(notify, "summarize", lambda *a, **k: result):
            code = notify.main(["--bbox", "world", "--no-enrich"])
        self.assertEqual(code, 2)

    def test_missing_config_returns_error_code(self):
        result = notify.summarize("world", min_frp=1, pipeline=lambda *x, **y: fake_payload([50]))
        with mock.patch.object(notify, "summarize", lambda *a, **k: result), \
                mock.patch.object(notify, "_setting", return_value=""), \
                mock.patch.object(notify, "already_alerted", return_value=False):
            code = notify.main(["--bbox", "world", "--force", "--no-enrich"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()

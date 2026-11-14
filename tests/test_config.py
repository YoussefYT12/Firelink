import os
import tempfile
import unittest
from unittest import mock

from tests import helpers  # noqa: F401
import config


class KeyLoadingTests(unittest.TestCase):
    def test_env_var_wins_over_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            open(path, "w").write("FIRMS_MAP_KEY=from_file\n")
            with mock.patch.object(config, "_ENV_FILE", path), mock.patch.dict(os.environ, {"FIRMS_MAP_KEY": "from_env"}):
                self.assertEqual(config.get_map_key(), "from_env")

    def test_reads_env_file_with_quotes_and_comments(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            open(path, "w").write('# comment\n\nOTHER=1\nFIRMS_MAP_KEY="abc123"\n')
            with mock.patch.object(config, "_ENV_FILE", path), mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(config.get_map_key(), "abc123")

    def test_missing_key_gives_clear_error(self):
        with mock.patch.object(config, "_ENV_FILE", "/nonexistent/.env"), mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(config.MissingKeyError) as cm:
                config.get_map_key()
            self.assertIn(".env", str(cm.exception))

    def test_placeholder_from_example_file_is_rejected(self):
        with mock.patch.object(config, "_ENV_FILE", "/nonexistent/.env"), mock.patch.dict(os.environ, {"FIRMS_MAP_KEY": "your_key_here"}):
            with self.assertRaises(config.MissingKeyError):
                config.get_map_key()

    def test_no_key_is_hardcoded_in_source(self):
        """Any text of length 32 hex characters inside Python files = leaked key."""
        import re
        for name in os.listdir(helpers.ROOT):
            if name.endswith((".py", ".html", ".bat", ".txt", ".md")):
                text = open(os.path.join(helpers.ROOT, name), encoding="utf-8").read()
                self.assertIsNone(re.search(r"\b[0-9a-f]{32}\b", text), f"possible API key in {name}")


if __name__ == "__main__":
    unittest.main()

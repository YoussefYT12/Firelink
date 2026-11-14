"""Shared tools for tests: puts the project folder in sys.path and creates a temporary history_data folder."""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import history  # noqa: E402
import history_analysis  # noqa: E402


class TempHistoryDir:
    """with TempHistoryDir(): ... → history.DATA_DIR points to a temporary folder, and it is restored."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = history.DATA_DIR
        history.DATA_DIR = self._tmp.name
        history_analysis._STORE_CACHE.clear()
        return self._tmp.name

    def __exit__(self, *exc):
        history.DATA_DIR = self._old
        history_analysis._STORE_CACHE.clear()
        self._tmp.cleanup()

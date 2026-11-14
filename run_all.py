"""
run_all.py
----------
One command runs the entire project:
  1. fetches today's data (or a date you specify), analyzes it and produces alerts
  2. runs a local server with an API: /api/query (any date/area), /api/calendar (calendar of days),
     /api/history (the unified time series since 2003 + comparison to the mean) - the map uses this
     to fetch all of that from inside the map itself without commands
  3. opens your browser to the map automatically
  4. (optional --monitor) will monitor and update the data periodically

Examples:
  python run_all.py --bbox world --monitor     # the whole world + continuous monitoring (this is what start_firelink.bat does)
  python run_all.py --region sinai             # a single region
  python run_all.py --date 2020-08-19          # an old day
"""

import argparse
import http.server
import json
import os
import re
import socketserver
import threading
import time
import urllib.parse
import webbrowser

from firms_client import fetch_viirs_and_modis_auto
from reconciliation import reconcile, summarize
from config import MissingKeyError
from pipeline import run_pipeline, build_payload, write_payload, write_csv, print_alerts
from calendar_activity import build_calendar
import history_analysis
import history_jobs
from history_analysis import HistoryError
from demo import REGIONS, CASE_STUDY, generate_mock_data

OUTPUT_FILE = "firelink_output.geojson"
OUTPUT_CSV = "firelink_output.csv"
FIRST_PORT = 8000
HOST = "127.0.0.1"       # This machine only. (If you want to open it from another machine on the same network: --host 0.0.0.0)

_BBOX_RE = re.compile(r"^(world|-?\d+(\.\d+)?(,-?\d+(\.\d+)?){3})$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_MAP_KEY_RE = re.compile(r"[0-9a-f]{32}")


def sanitize_error(msg) -> str:
    """Removes the NASA key and any URL from the error message before returning it to the browser."""
    text = _MAP_KEY_RE.sub("***", str(msg))
    text = re.sub(r"(?im)^\s*URL:\s*\S+.*$", "", text)
    return " ".join(text.split())[:400]


def check_bbox(bbox: str) -> None:
    """Raises ValueError with a meaningful message if the box is not valid (out of range or inverted)."""
    if bbox in ("", "world"):
        return
    w, s_, e, n = (float(x) for x in bbox.split(","))
    if not (-180 <= w <= 180 and -180 <= e <= 180):
        raise ValueError("Longitude must be between -180 and 180.")
    if not (-90 <= s_ <= 90 and -90 <= n <= 90):
        raise ValueError("Latitude must be between -90 and 90.")
    if w >= e:
        raise ValueError("West must be smaller than East (longitude).")
    if s_ >= n:
        raise ValueError("South must be smaller than North (latitude).")

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,59}$")
MAX_BODY_BYTES = 4096


class FireLinkHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the project files + the /api/query interface the map talks to."""

    # The server serves only the frontend files. Without this list anyone could open /.env or /firms_client.py
    # or history_data and read them (and the .env would reveal the key).
    PUBLIC_FILES = {"/map.html", "/firelink_output.geojson", "/firelink_output.csv"}

    def send_head(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":     # Toggle display of folder files list
            self.send_response(302)
            self.send_header("Location", "/map.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if path not in self.PUBLIC_FILES:
            self.send_error(404, "Not found")
            return None
        return super().send_head()

    def log_message(self, fmt, *args):
        pass  # Mute log for each request so the terminal stays clean

    def end_headers(self):
        # No cache: any update to files appears immediately without Ctrl+Shift+R
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_calendar(self, parsed):
        q = urllib.parse.parse_qs(parsed.query)
        bbox = q.get("bbox", [""])[0]
        end = q.get("end", [""])[0] or None
        try:
            days = int(q.get("days", ["30"])[0])
        except ValueError:
            days = 30
        if not _BBOX_RE.match(bbox):
            return self._send_json(400, {"error": "Draw an area on the map first."})
        try:
            check_bbox(bbox)
        except ValueError as e:
            return self._send_json(400, {"error": str(e)})
        if end is not None and not _DATE_RE.match(end):
            return self._send_json(400, {"error": "Invalid date, expected YYYY-MM-DD."})
        try:
            print(f"[calendar] area={bbox} end={end or 'today'} days={days} ...")
            payload = build_calendar(bbox, end, days)
            print(f"[calendar] done: {payload['summary']}")
            return self._send_json(200, payload)
        except ValueError as e:  # Rejected inputs (area too large/invalid...)
            return self._send_json(400, {"error": sanitize_error(e)})
        except Exception as e:  # noqa: BLE001
            print(f"[calendar] failed: {e}")
            return self._send_json(500, {"error": sanitize_error(e)})

    def _handle_history(self, parsed):
        q = urllib.parse.parse_qs(parsed.query)
        bbox = q.get("bbox", [""])[0] or None
        region = q.get("region", [""])[0] or None
        month = q.get("month", [""])[0] or None
        if bbox is not None and not _BBOX_RE.match(bbox):
            return self._send_json(400, {"error": "Invalid area (bbox)."})
        if bbox == "world":
            bbox = None
        if month is not None and not _MONTH_RE.match(month):
            return self._send_json(400, {"error": "Invalid month, expected YYYY-MM."})
        if region is not None and not _NAME_RE.match(region):
            return self._send_json(400, {"error": "Invalid region name."})
        try:
            return self._send_json(200, history_analysis.analyze(bbox, region, month))
        except history_analysis.AreaNotDownloaded as e:
            # The UI uses this flag to show the download button immediately
            return self._send_json(400, {"error": str(e), "needs_download": True, "bbox": bbox})
        except HistoryError as e:
            return self._send_json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            print(f"[history] failed: {e}")
            return self._send_json(500, {"error": str(e)})

    # ---------------------------------------------------------- download history
    def _read_json_body(self):
        """Request body (small JSON) or {} if empty. Raises ValueError if invalid/too large."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("Request body too large.")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Invalid JSON body.")
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    def _handle_build_start(self):
        try:
            body = self._read_json_body()
            print(f"[history/build] start bbox={body.get('bbox')} start={body.get('start')}")
            job = history_jobs.start(
                bbox=body.get("bbox", ""),
                start=body.get("start"),
                end=body.get("end"),
                workers=body.get("workers", 3),
            )
            print(f"[history/build] job {job['id']} for '{job['region']}' ({job['bbox']}) started")
            return self._send_json(202, {"job": job})
        except history_jobs.AlreadyRunning as e:
            return self._send_json(409, {"error": str(e), "job": history_jobs.status()["job"]})
        except (ValueError, TypeError) as e:      # BuildError and invalid input
            return self._send_json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            print(f"[history/build] failed: {e}")
            return self._send_json(500, {"error": str(e)})

    def _handle_build_cancel(self):
        try:
            result = history_jobs.cancel()
            return self._send_json(200, result)
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": str(e)})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/history/regions":
            return self._send_json(200, {"regions": history_analysis.list_regions()})
        if parsed.path == "/api/history/build/status":
            return self._send_json(200, history_jobs.status())
        if parsed.path == "/api/history":
            return self._handle_history(parsed)
        if parsed.path == "/api/calendar":
            return self._handle_calendar(parsed)
        if parsed.path != "/api/query":
            return super().do_GET()

        q = urllib.parse.parse_qs(parsed.query)
        bbox = q.get("bbox", ["world"])[0]
        date = q.get("date", [""])[0] or None
        try:
            days = max(1, min(5, int(q.get("days", ["1"])[0])))
        except ValueError:
            days = 1

        if not _BBOX_RE.match(bbox):
            return self._send_json(400, {"error": "Invalid area (bbox)."})
        try:
            check_bbox(bbox)
        except ValueError as e:
            return self._send_json(400, {"error": str(e)})
        if date is not None and not _DATE_RE.match(date):
            return self._send_json(400, {"error": "Invalid date, expected YYYY-MM-DD."})

        cache_key = (bbox, date or "", days)
        cached = cache_get(cache_key)
        if cached is not None:
            print(f"[query] cache hit area={bbox} date={date or 'live'}")
            return self._send_json(200, cached)

        try:
            print(f"[query] area={bbox} date={date or 'live'} ...")
            payload = run_pipeline(bbox, date, days)
            print(f"[query] done: {len(payload['features'])} features, {len(payload['alerts'])} alerts")
            cache_put(cache_key, payload)
            return self._send_json(200, payload)
        except Exception as e:  # Return the error to the map instead of letting the server crash
            print(f"[query] failed: {e}")
            return self._send_json(500, {"error": sanitize_error(e)})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/history/build":
            return self._handle_build_start()
        if parsed.path == "/api/history/build/cancel":
            return self._handle_build_cancel()
        return self._send_json(404, {"error": "Not found"})


# Small cache for /api/query results: same area + same date return immediately instead of going to NASA again.
# This makes map panning fast (and very useful during presentations to judges) and reduces key quota usage.
_QUERY_CACHE = {}
_QUERY_CACHE_LOCK = threading.Lock()
QUERY_CACHE_TTL = float(os.environ.get("FIRELINK_QUERY_TTL", "600"))   # 10 minutes
QUERY_CACHE_MAX = 64


def cache_get(key):
    with _QUERY_CACHE_LOCK:
        item = _QUERY_CACHE.get(key)
        if not item:
            return None
        saved_at, payload = item
        if time.time() - saved_at > QUERY_CACHE_TTL:
            _QUERY_CACHE.pop(key, None)
            return None
        return payload


def cache_put(key, payload):
    with _QUERY_CACHE_LOCK:
        if len(_QUERY_CACHE) >= QUERY_CACHE_MAX:
            _QUERY_CACHE.clear()
        _QUERY_CACHE[key] = (time.time(), payload)


class FireLinkServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # On Windows this prevents running two servers on the same port by mistake


def start_web_server(host=None, port=None):
    """Opens the server on the first free port (8000, if occupied 8001...).

    If a port is specified (via --port or the environment variable PORT) we use that exact one —
    that's what hosting platforms like Render and Railway require.
    """
    candidates = [port] if port else list(range(FIRST_PORT, FIRST_PORT + 10))
    for port in candidates:
        try:
            httpd = FireLinkServer((host or HOST, port), FireLinkHandler)
        except OSError:
            continue
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        print(f"\nServing FireLink at http://localhost:{port}/map.html", flush=True)
        return httpd, port
    if candidates and candidates[0]:
        raise RuntimeError(f"Port {candidates[0]} is already in use.")
    raise RuntimeError("No free port found between 8000 and 8009.")


def fetch_and_export(bbox, date, days, mock=False):
    if mock:
        print("Mock Mode - quick test without an API Key\n")
        viirs, modis = generate_mock_data()
        results = reconcile(viirs, modis)
        payload = build_payload(
            results,
            {"bbox": bbox, "date": date, "days": days, "viirs": len(viirs), "modis": len(modis)},
        )
    else:
        label = f"around {date}" if date else "live/current"
        print(f"Fetching {label} data for {bbox} ({days} day(s))\n")
        # Exactly the same API path: meaning the same handling of NASA publish delay (actual date + alert)
        payload = run_pipeline(bbox, date, days, use_cache=False)
        meta = payload["meta"]
        print(f"Day shown: {meta.get('date')}   VIIRS: {meta.get('viirs')}   MODIS: {meta.get('modis')}")
        if meta.get("note"):
            print(f"Note: {meta['note']}")
        results = None

    if results is not None:
        print(f"VIIRS detections: {len(viirs)}")
        print(f"MODIS detections: {len(modis)}\n")
        print("Reconciliation Summary:")
        for status, count in summarize(results).items():
            print(f"   {status}: {count}")
    write_payload(payload, OUTPUT_FILE)
    write_csv(payload, OUTPUT_CSV)
    print_alerts(payload["alerts"], payload["alerts_total"])
    return payload


def monitor_loop(bbox, interval):
    """Periodically: fetches new data, updates the file (the map updates itself), and prints alerts."""
    while True:
        time.sleep(interval)
        print(f"\n--- Re-checking {bbox} ---")
        try:
            payload = run_pipeline(bbox, None, 1, use_cache=False)
            write_payload(payload, OUTPUT_FILE)
            write_csv(payload, OUTPUT_CSV)
            print_alerts(payload["alerts"], payload["alerts_total"])
        except Exception as e:
            print(f"Re-check failed (will retry next round): {e}")


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))  # Files are always from the project folder

    parser = argparse.ArgumentParser(description="FireLink - run everything with one command")
    parser.add_argument("--region", choices=REGIONS.keys(), default=None)
    parser.add_argument("--bbox", type=str, default=None)
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD, omit for live data")
    parser.add_argument("--days", type=int, default=1, help="Days to fetch (default 1)")
    parser.add_argument("--case-study", action="store_true")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--monitor", action="store_true", help="Keep monitoring after the first check")
    parser.add_argument("--interval", type=int, default=900)
    parser.add_argument("--host", default=HOST, help="address to listen on (default 127.0.0.1 = this computer only)")
    parser.add_argument("--port", type=int, default=None,
                        help="port to listen on (default: first free port in 8000-8009, or $PORT when hosted)")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser")
    parser.add_argument("--no-fetch", action="store_true",
                        help="Open the map immediately with the last saved file and skip the startup download")
    args = parser.parse_args()

    # Hosting platforms (Render/Railway/Fly) set the port in the PORT environment variable and require 0.0.0.0
    port_arg = args.port or (int(os.environ["PORT"]) if os.environ.get("PORT", "").isdigit() else None)
    host_arg = args.host
    if port_arg and host_arg == HOST:
        host_arg = "0.0.0.0"

    days = args.days
    if args.case_study:
        bbox, date, days = CASE_STUDY["bbox"], CASE_STUDY["date"], 5
    else:
        bbox = args.bbox or REGIONS[args.region or "world"]
        date = args.date

    # The first server (fractions of a second) so the browser opens immediately, and loading follows:
    # The whole thing takes ~3 minutes, and before that the screen stayed black and the user thought it had frozen.
    _, port = start_web_server(host_arg, port_arg)
    if not args.no_browser:
        webbrowser.open(f"http://localhost:{port}/map.html")

    if not args.no_fetch:
        try:
            fetch_and_export(bbox, date, days, mock=args.mock)
            print("\nData file updated - refresh the map (F5) to see the newest day.")
        except MissingKeyError as e:       # The server is running, so the map runs from the saved file
            print(f"\n{e}\nThe map is open with the last saved file. Add a key to fetch live data "
                  f"(or try: python run_all.py --mock).")
        except Exception as e:  # noqa: BLE001 - network/NASA down
            print(f"\nCould not fetch new data: {e}\nThe map still works with the last saved file.")

    try:
        if args.monitor:
            print(f"Continuous monitoring every {args.interval}s (Ctrl+C to stop)...")
            monitor_loop(bbox, args.interval)
        else:
            print("Map is open. Press Ctrl+C here to stop.")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

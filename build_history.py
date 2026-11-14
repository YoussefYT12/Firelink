"""
build_history.py
----------------
Historical data download command (MODIS from 2003 + VIIRS from 2012) for an area, to build the unified calendar.

Steps:
  1) Quick check for access to NASA data (about a minute):
        python build_history.py --check

  2) Download an area (takes time, leave it running; if you stop it, restart and it will resume):
        python build_history.py --name egypt_delta
        python build_history.py --name my_area --bbox "west,south,east,north"

  2b) Quick demo run without downloading or a key (synthetic "DEMO" data):
        python build_history.py --demo

  3) View what was downloaded:
        python build_history.py --info
        python build_history.py --info --name egypt_delta

Files are saved in the history_data folder next to the program.
"""

import argparse
import os
import time
from datetime import date, datetime, timedelta, timezone

import history
import history_demo
from config import MissingKeyError, get_map_key
from firms_client import FIRMS_BASE_URL

TEST_BOX = "30.5,30.0,32.0,31.5"   # Part of the Nile Delta - small and fast


def _try_source(src: str, d: date, n: int, key: str) -> str:
    started = time.time()
    try:
        text = history._download(f"{FIRMS_BASE_URL}/{key}/{src}/{TEST_BOX}/{n}/{d.isoformat()}", attempts=1)
        _, kept, dropped = history.aggregate_rows(history.parse_rows(text))
        return f"OK     {kept} detections kept ({dropped} filtered out) in {time.time() - started:.1f}s"
    except Exception as e:  # noqa: BLE001
        return f"FAILED {str(e)[:110]}"


def check(key: str):
    today = datetime.now(timezone.utc).date()
    print("== 1) What NASA says it has (data availability)")
    avail = history.get_availability(key)
    if not avail:
        print("   could not read the list - the tool will guess by date instead")
    for src in sorted(avail):
        if src.startswith(("MODIS", "VIIRS_SNPP", "VIIRS_NOAA20")):
            lo, hi = avail[src]
            print(f"   {src:20} {lo}  ->  {hi}")

    print("\n== 2) Real test requests (3 days, small box in the Nile Delta)")
    probes = [
        ("old, final archive", date(2019, 3, 1), ["MODIS_SP", "VIIRS_SNPP_SP"]),
        ("recent (10 days ago)", today - timedelta(days=10), ["MODIS_NRT", "VIIRS_SNPP_NRT"]),
        ("in-between (100 days ago) - the risky gap", today - timedelta(days=100),
         ["MODIS_SP", "MODIS_NRT", "VIIRS_SNPP_SP", "VIIRS_SNPP_NRT"]),
    ]
    for label, d, sources in probes:
        print(f"   {label}  [{d}]")
        for src in sources:
            print(f"      {src:16} {_try_source(src, d, 3, key)}")
    print("\nSend me this whole screen. The in-between result tells me which source to use for the last months.")


def info(name):
    if name:
        store = history.load_store(name)
        print(history.summarize_store(store) if store else f"no data yet for '{name}'")
        return
    if not os.path.isdir(history.DATA_DIR):
        print("no history_data folder yet.")
        return
    files = sorted(f for f in os.listdir(history.DATA_DIR) if f.endswith(".json.gz"))
    print("downloaded regions:" if files else "no regions downloaded yet.")
    for f in files:
        store = history.load_store(f[:-len(".json.gz")])
        print("\n" + history.summarize_store(store))


def main(argv=None):
    p = argparse.ArgumentParser(description="Download MODIS + VIIRS history for one region (resumable).")
    p.add_argument("--demo", action="store_true", help="create a SIMULATED region (no download, no key) to try the history screen")
    p.add_argument("--check", action="store_true", help="test access to NASA data and exit")
    p.add_argument("--info", action="store_true", help="show what is already downloaded")
    p.add_argument("--name", help="short name for the region (e.g. egypt_delta)")
    p.add_argument("--bbox", help='"west,south,east,north" (not needed for built-in names)')
    p.add_argument("--start", help="YYYY-MM-DD (default 2003-01-01 for MODIS, 2012-01-20 for VIIRS)")
    p.add_argument("--end", help="YYYY-MM-DD (default yesterday)")
    p.add_argument("--workers", type=int, default=3, help="parallel downloads (default 3, max 4)")
    p.add_argument("--force-area", action="store_true", help="allow a box bigger than 150 sq degrees")
    p.add_argument("--refresh-nrt", action="store_true", help="re-download old 'recent' chunks that NASA has since finalized")
    p.add_argument("--no-retry-failed", action="store_true", help="skip chunks that failed before")
    args = p.parse_args(argv)

    if args.info:
        return info(args.name)
    if args.demo:
        store = history_demo.make_demo_store()
        history.save_store(history_demo.DEMO_NAME, store)
        print(f"Created SIMULATED region '{history_demo.DEMO_NAME}' ({history_demo.DEMO_BBOX}) - not real fires.")
        print("Open the map, draw a box inside that area, and use the History section.")
        return
    try:
        key = get_map_key()
    except MissingKeyError as e:
        p.error(str(e))
    if args.check:
        return check(key)

    if not args.name:
        p.error("give --name (built-in: " + ", ".join(history.NAMED_REGIONS) + ") or use --check / --info")
    bbox = args.bbox or history.NAMED_REGIONS.get(args.name)
    if not bbox:
        p.error(f"'{args.name}' is not a built-in region; add --bbox \"west,south,east,north\"")

    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    try:
        store = history.build_history(
            args.name, bbox, start=start, end=end, workers=max(1, min(args.workers, 4)), key=key,
            retry_failed=not args.no_retry_failed, refresh_nrt=args.refresh_nrt, force_area=args.force_area,
        )
    except ValueError as e:
        p.error(str(e))
    print("\n" + history.summarize_store(store))


if __name__ == "__main__":
    main()
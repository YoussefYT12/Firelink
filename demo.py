"""
demo.py
-------
A demo script showing the whole project working: fetching data (or mock data for demo)
+ running the matching engine + printing a summary + exporting GeoJSON for the map.

How to run:
  1. Quick trial without internet or API Key (mock data):
     python demo.py --mock

  2. Real run on Sinai (requires API Key, see firms_client.py):
     # Put the key in .env (FIRMS_MAP_KEY=...) or: export FIRMS_MAP_KEY=\"...\"
     python demo.py --region sinai

  3. Real run on a globally known fire-prone region (California, for example):
     python demo.py --region california
"""

import argparse
import json
import random
from datetime import datetime, timedelta

from firms_client import FireDetection, fetch_viirs_and_modis_auto
from reconciliation import reconcile, summarize, FireStatus
from ai_risk import assess_risk, build_stats_from_results
from clustering import cluster_confirmed_fires, annotate_top_clusters_with_location
from pipeline import build_payload, write_payload

# Predefined areas to test (bbox in format west,south,east,north)
REGIONS = {
    "world": "world",                         # The whole globe
    "sinai": "32.5,28.5,34.5,31.0",           # Sinai - Egypt
    "california": "-121.5,36.0,-119.0,38.5",  # California - a globally known fire region
    "amazon": "-63.0,-10.0,-58.0,-5.0",       # Amazon rainforest - a globally known fire region
}

# Case Study ready as a quick example: Palisades and Eaton fires, Los Angeles, January 2025
# ignited: 7 January 2025 | first VIIRS detection of the Eaton fire: 8 January at 1:30 AM (NASA documentation)
CASE_STUDY = {
    "bbox": "-118.7,33.95,-117.95,34.3",
    "date": "2025-01-07",
}


def generate_mock_data(n_pairs: int = 15, n_unmatched: int = 5):
    """
    Generates realistic mock data so you can test the engine immediately
    without waiting for an API Key or internet connection.
    """
    viirs, modis = [], []
    base_date = datetime(2026, 9, 15, 12, 0)
    base_lat, base_lon = 30.0, 33.0

    for i in range(n_pairs):
        lat = base_lat + random.uniform(-0.5, 0.5)
        lon = base_lon + random.uniform(-0.5, 0.5)
        v_time = base_date + timedelta(minutes=random.randint(0, 600))
        # MODIS detects roughly the same fire with slight differences in location and timing
        m_time = v_time + timedelta(minutes=random.randint(-90, 90))
        offset_lat = random.uniform(-0.01, 0.01)
        offset_lon = random.uniform(-0.01, 0.01)

        viirs.append(_make_detection(lat, lon, v_time, "VIIRS_SNPP_NRT"))
        modis.append(_make_detection(lat + offset_lat, lon + offset_lon, m_time, "MODIS_NRT"))

    # Single source point only (to see that SINGLE_SOURCE classification is working)
    for i in range(n_unmatched):
        lat = base_lat + random.uniform(-1.0, 1.0)
        lon = base_lon + random.uniform(-1.0, 1.0)
        t = base_date + timedelta(minutes=random.randint(0, 600))
        viirs.append(_make_detection(lat, lon, t, "VIIRS_SNPP_NRT"))

    return viirs, modis


def _make_detection(lat, lon, timestamp: datetime, source: str) -> FireDetection:
    return FireDetection(
        latitude=lat,
        longitude=lon,
        brightness=random.uniform(300, 400),
        confidence="nominal",
        acq_date=timestamp.strftime("%Y-%m-%d"),
        acq_time=timestamp.strftime("%H%M"),
        frp=random.uniform(5, 80),
        source=source,
    )


def export_geojson(results, path="firelink_output.geojson", meta=None):
    """Exports the results (points + full numbers + alerts with place names) to a GeoJSON file for the map."""
    payload = build_payload(results, meta or {})
    write_payload(payload, path)
    note = " (only Confirmed+Likely exported - dataset is huge)" if payload["truncated"] else ""
    print(f"\nExported file: {path} - {len(payload['features'])} features{note}")


def main():
    parser = argparse.ArgumentParser(description="FireLink - VIIRS/MODIS Reconciliation Demo")
    parser.add_argument("--mock", action="store_true", help="Use mock data, no API Key needed")
    parser.add_argument("--region", choices=REGIONS.keys(), default=None, help="A built-in region")
    parser.add_argument("--bbox", type=str, default=None,
                         help='Custom area "west,south,east,north" (e.g. from map.html\'s draw tool)')
    parser.add_argument("--date", type=str, default=None,
                         help="Date to investigate, YYYY-MM-DD (omit for current/live data)")
    parser.add_argument("--days", type=int, default=1, help="Number of days to fetch (default 1 = today only)")
    parser.add_argument(
        "--case-study",
        action="store_true",
        help="Shortcut: run the built-in LA Palisades/Eaton Fires (Jan 2025) example",
    )
    args = parser.parse_args()

    if args.mock:
        print("Mock Mode - quick test without an API Key\n")
        viirs, modis = generate_mock_data()
        bbox = "mock"
        date = None
        days_used = 1
    else:
        if args.case_study:
            bbox = CASE_STUDY["bbox"]
            date = CASE_STUDY["date"]
        else:
            bbox = args.bbox or REGIONS[args.region or "sinai"]
            date = args.date

        if date:
            print(f"Investigating {bbox} around {date} (auto-selecting NRT or archival data)\n")
        else:
            print(f"Fetching current/live data for {bbox}\n")

        days_used = 5 if args.case_study else args.days  # The case study needs 5 days (archive limit)
        viirs, modis = fetch_viirs_and_modis_auto(bbox, date=date, day_range=days_used)

    print(f"VIIRS detections: {len(viirs)}")
    print(f"MODIS detections: {len(modis)}\n")

    results = reconcile(viirs, modis)
    summary = summarize(results)

    print("Reconciliation Summary:")
    for status, count in summary.items():
        print(f"   {status}: {count}")

    print("\n--- AI Risk Assessment ---")
    if bbox == "world":
        clusters = cluster_confirmed_fires(results)
        alerts = []
        for c in clusters:
            stats = {
                "confirmed_count": c.confirmed_count,
                "likely_count": c.likely_count,
                "total_detections": c.total_count,
                "average_frp": c.avg_frp,
                "max_frp": c.max_frp,
                "confirmed_growth": None,
            }
            assessment = assess_risk(stats)
            if assessment.risk_level in ("High", "Critical"):
                alerts.append((c, assessment))

        print(f"Found {len(clusters)} fire clusters worldwide, {len(alerts)} flagged High/Critical.\n")
        if alerts:
            top_alerts = sorted(alerts, key=lambda a: a[0].confirmed_count, reverse=True)[:5]
            located = annotate_top_clusters_with_location([c for c, _ in top_alerts], top_n=5)
            for (cluster, place), (_, assessment) in zip(located, top_alerts):
                location_str = place or f"lat {cluster.latitude}, lon {cluster.longitude}"
                print(f"*** ALERT [{assessment.risk_level}] near: {location_str}")
                print(f"    Coordinates: {cluster.latitude}, {cluster.longitude}")
                print(f"    {assessment.summary}")
                print(f"    Why: {assessment.reasoning}")
                print(f"    Action: {assessment.recommended_action}\n")
    else:
        stats = build_stats_from_results(results)
        assessment = assess_risk(stats)
        print(f"Risk Level: {assessment.risk_level}")
        print(f"Summary:    {assessment.summary}")
        print(f"Reasoning:  {assessment.reasoning}")
        print(f"Action:     {assessment.recommended_action}")
        if assessment.risk_level in ("High", "Critical"):
            print("\n*** ALERT: elevated fire risk detected in this area ***")

    if args.case_study:
        detections_with_time = [r for r in results if (r.viirs_detection or r.modis_detection)]
        if detections_with_time:
            earliest = min(
                detections_with_time,
                key=lambda r: (r.viirs_detection or r.modis_detection).timestamp,
            )
            earliest_time = (earliest.viirs_detection or earliest.modis_detection).timestamp

            confirmed_only = [r for r in detections_with_time if r.status == FireStatus.CONFIRMED]
            earliest_confirmed_time = None
            if confirmed_only:
                earliest_confirmed = min(
                    confirmed_only,
                    key=lambda r: (r.viirs_detection or r.modis_detection).timestamp,
                )
                earliest_confirmed_time = (
                    earliest_confirmed.viirs_detection or earliest_confirmed.modis_detection
                ).timestamp

            print("\n--- Case Study Validation ---")
            print(f"Earliest detection (any source):        {earliest_time} UTC")
            if earliest_confirmed_time:
                print(f"Earliest CONFIRMED detection (both sources): {earliest_confirmed_time} UTC")
            print("Documented ignition (public record):    2025-01-07 (Palisades & Eaton)")
            print("Documented first VIIRS detection (NASA): 2025-01-08 01:30 UTC (Eaton Fire)")
            print(
                "\nNote: a single-source detection earlier than the documented ignition "
                "time is worth flagging as a possible false positive (sun glint, industrial "
                "heat) rather than treated as confirmed fire activity - this is exactly why "
                "the multi-source Confirmed category matters more than any single detection."
            )

    export_geojson(
        results,
        meta={"bbox": bbox, "date": date, "days": days_used, "viirs": len(viirs), "modis": len(modis)},
    )


if __name__ == "__main__":
    main()
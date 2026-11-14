"""
monitor.py
----------
Live Monitoring. The default mode now: monitors
**the whole world** without specifying a region, and groups nearby points into \"clusters\"
geographical (clustering.py), and any cluster with high risk gets an approximate place name
and appears as a clear alert.

How to run (default mode - whole world):
  # put the key in .env (FIRMS_MAP_KEY=...) or: export FIRMS_MAP_KEY=\"...\"
  python monitor.py

If you want to monitor a single region instead of the whole world (like before):
  python monitor.py --region sinai
  python monitor.py --bbox \"32.5,28.5,34.5,31.0\"

--interval in seconds (900 = every 15 minutes by default).

Note: The risk scoring layer (ai_risk.py) runs entirely locally - there is no
paid external API or key required. Place name lookup (reverse_geocode in
clustering.py) uses free OpenStreetMap, and is only activated for truly
dangerous clusters (not everything) to stay fast.
"""

import argparse
import time
from datetime import datetime, timezone

from firms_client import fetch_viirs_and_modis
from reconciliation import reconcile
from ai_risk import assess_risk
from clustering import cluster_confirmed_fires, annotate_top_clusters_with_location

REGIONS = {
    "world": "world",  # the whole globe - this value is interpreted directly by the NASA FIRMS API
    "sinai": "32.5,28.5,34.5,31.0",
    "california": "-121.5,36.0,-119.0,38.5",
    "amazon": "-63.0,-10.0,-58.0,-5.0",
}

TOP_CLUSTERS_TO_GEOCODE = 5  # get a place name only for the top 5 most dangerous clusters, to save time


def log(message: str):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{timestamp}] {message}")


def run_once(bbox: str, previous_cluster_counts: dict):
    viirs, modis = fetch_viirs_and_modis(bbox, day_range=1)
    results = reconcile(viirs, modis)
    clusters = cluster_confirmed_fires(results)

    log(
        f"Poll complete: {len(viirs)} VIIRS, {len(modis)} MODIS raw detections "
        f"-> {len(clusters)} fire clusters found"
    )

    new_cluster_counts = {}
    alerts = []

    for cluster in clusters:
        prev = previous_cluster_counts.get(cluster.grid_key, 0)
        growth = cluster.confirmed_count - prev
        new_cluster_counts[cluster.grid_key] = cluster.confirmed_count

        stats = {
            "confirmed_count": cluster.confirmed_count,
            "likely_count": cluster.likely_count,
            "total_detections": cluster.total_count,
            "average_frp": cluster.avg_frp,
            "max_frp": cluster.max_frp,
            "confirmed_growth": growth if previous_cluster_counts else None,
        }
        assessment = assess_risk(stats)

        if assessment.risk_level in ("High", "Critical"):
            alerts.append((cluster, assessment))

    if alerts:
        # Place name only for most dangerous clusters, not everything
        top_alerts = sorted(alerts, key=lambda a: a[0].confirmed_count, reverse=True)
        located = annotate_top_clusters_with_location(
            [c for c, _ in top_alerts], top_n=TOP_CLUSTERS_TO_GEOCODE
        )

        print(f"\n{'=' * 60}")
        print(f"  {len(alerts)} LOCATION(S) FLAGGED - HIGH/CRITICAL RISK")
        print(f"{'=' * 60}")
        for (cluster, place), (_, assessment) in zip(located, top_alerts):
            location_str = place or f"lat {cluster.latitude}, lon {cluster.longitude}"
            print(f"\n*** ALERT [{assessment.risk_level}] near: {location_str}")
            print(f"    Coordinates: {cluster.latitude}, {cluster.longitude}")
            print(f"    {assessment.summary}")
            print(f"    Why: {assessment.reasoning}")
            print(f"    Action: {assessment.recommended_action}")
        print(f"{'=' * 60}\n")
    else:
        log("No High/Critical clusters this round.")

    return new_cluster_counts


def main():
    parser = argparse.ArgumentParser(description="FireLink live monitoring loop")
    parser.add_argument("--region", choices=REGIONS.keys(), default=None,
                         help="Use one of the built-in regions (default: world)")
    parser.add_argument("--bbox", type=str, default=None,
                         help='Custom area as "west,south,east,north" (get this from map.html\'s draw tool)')
    parser.add_argument("--interval", type=int, default=900, help="Seconds between polls (default 900 = 15 min)")
    parser.add_argument("--once", action="store_true", help="Run a single check and exit (no loop)")
    args = parser.parse_args()

    if args.bbox:
        bbox = args.bbox
        region_label = f"custom area {bbox}"
    else:
        region_key = args.region or "world"
        bbox = REGIONS[region_key]
        region_label = region_key

    log(f"Starting FireLink monitor for '{region_label}' (interval: {args.interval}s)")
    if region_label == "world":
        log("Monitoring the entire planet - this pulls a lot of data, first poll may take a moment.")

    previous_cluster_counts = {}
    try:
        while True:
            previous_cluster_counts = run_once(bbox, previous_cluster_counts)
            if args.once:
                break
            log(f"Sleeping {args.interval}s until next poll...\n")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("Monitor stopped by user.")


if __name__ == "__main__":
    main()
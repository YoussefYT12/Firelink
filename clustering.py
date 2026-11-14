"""
clustering.py
--------------
When monitoring the whole world, it's not reasonable to issue an alert for every single point - there may be hundreds of nearby points representing the same fire. The solution: group them into "clusters" (Clusters) geographically, and evaluate each cluster separately.

Method: simple Grid Binning - we divide the globe into small squares (according to
GRID_SIZE_DEG), and any two points that fall into the same cell (or a neighboring cell) are counted as one cluster. This method is much faster than comparing every point to every point (especially with thousands of points worldwide).
"""

import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

import requests

from reconciliation import ReconciledFire, FireStatus

GRID_SIZE_DEG = 0.5  # ~55 km at the equator - reasonable size to group "same fire"


@dataclass
class FireCluster:
    latitude: float
    longitude: float
    confirmed_count: int
    likely_count: int
    total_count: int
    avg_frp: float
    max_frp: float
    grid_key: tuple  # Stable cluster key - used to track growth between monitoring cycles
    hotspots: list = field(default_factory=list)  # Top 3 fire points (lat, lon) - to assess land cover at those locations


MAX_CLUSTER_SPAN_CELLS = 6  # Maximum cluster span: 6 cells × 0.5° ≈ 330 km


def _merge_adjacent_cells(buckets: dict) -> dict:
    """
    Merges adjacent cells into a single cluster (Flood Fill / BFS), but **with a maximum geographic cluster span** (MAX_CLUSTER_SPAN_CELLS). Without this limit, a seasonal burning belt spanning an entire continent (like agricultural burning in Central Africa in January) would be grouped as a single fake "fire" of tens of thousands of points - a completely unrealistic number. This limit allows merging a real extended regional fire (like local Congo fires or the Australian fire complex), but prevents combining geographically distant areas into one cluster.
    """
    visited = set()
    groups = []

    for start in buckets:
        if start in visited:
            continue

        visited.add(start)
        component = [start]
        stack = [start]
        min_r = max_r = start[0]
        min_c = max_c = start[1]

        while stack:
            row, col = stack.pop()
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    neighbor = (row + dr, col + dc)
                    if neighbor not in buckets or neighbor in visited:
                        continue

                    new_min_r, new_max_r = min(min_r, neighbor[0]), max(max_r, neighbor[0])
                    new_min_c, new_max_c = min(min_c, neighbor[1]), max(max_c, neighbor[1])
                    if (new_max_r - new_min_r > MAX_CLUSTER_SPAN_CELLS
                            or new_max_c - new_min_c > MAX_CLUSTER_SPAN_CELLS):
                        continue  # This would make the cluster wider than allowed - ignore it here

                    visited.add(neighbor)
                    component.append(neighbor)
                    stack.append(neighbor)
                    min_r, max_r, min_c, max_c = new_min_r, new_max_r, new_min_c, new_max_c

        groups.append(component)

    merged = {}
    for i, keys in enumerate(groups):
        merged[i] = []
        for k in keys:
            merged[i].extend(buckets[k])
    return merged


def cluster_confirmed_fires(results: List[ReconciledFire]) -> List[FireCluster]:
    """Groups CONFIRMED and LIKELY points (actually significant) into geographic clusters."""
    buckets = {}

    for r in results:
        if r.status not in (FireStatus.CONFIRMED, FireStatus.LIKELY):
            continue

        key = (round(r.latitude / GRID_SIZE_DEG), round(r.longitude / GRID_SIZE_DEG))
        buckets.setdefault(key, []).append(r)

    buckets = _merge_adjacent_cells(buckets)

    clusters = []
    for key, members in buckets.items():
        lats = [m.latitude for m in members]
        lons = [m.longitude for m in members]
        confirmed_n = sum(1 for m in members if m.status == FireStatus.CONFIRMED)
        likely_n = sum(1 for m in members if m.status == FireStatus.LIKELY)

        # Top 3 fire points (by FRP) from different locations - these are where we measure land type,
        # Because the cluster centroid may fall on a lake or land different from the fires themselves
        ranked = sorted(
            members,
            key=lambda m: ((m.viirs_detection or m.modis_detection).frp or 0),
            reverse=True,
        )
        hotspots, seen = [], set()
        for m in ranked:
            spot = (round(m.latitude, 2), round(m.longitude, 2))
            if spot in seen:
                continue
            seen.add(spot)
            hotspots.append((m.latitude, m.longitude))
            if len(hotspots) == 3:
                break

        frps = []
        for m in members:
            det = m.viirs_detection or m.modis_detection
            if det and det.frp:
                frps.append(det.frp)

        clusters.append(
            FireCluster(
                latitude=round(sum(lats) / len(lats), 4),
                longitude=round(sum(lons) / len(lons), 4),
                confirmed_count=confirmed_n,
                likely_count=likely_n,
                total_count=len(members),
                avg_frp=round(sum(frps) / len(frps), 2) if frps else 0.0,
                max_frp=round(max(frps), 2) if frps else 0.0,
                grid_key=key,
                hotspots=hotspots,
            )
        )

    # Most dangerous first
    clusters.sort(key=lambda c: (c.confirmed_count, c.max_frp), reverse=True)
    return clusters


_GEOCODE_CACHE = {}


def reverse_geocode(lat: float, lon: float) -> Optional[str]:
    """
    Retrieves a short place name (region, country) from coordinates using OpenStreetMap
    Nominatim (free, without an API Key). Caches results so it doesn't query
    the same place twice. If it fails for any reason it returns None without stopping the program.
    """
    if os.environ.get("FIRELINK_NO_GEOCODE"):
        return None

    key = (round(lat, 1), round(lon, 1))
    if key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[key]

    label = None
    got_response = False
    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={
                "format": "jsonv2", "lat": lat, "lon": lon, "zoom": 5,
                "addressdetails": 1, "accept-language": "en",
            },
            headers={"User-Agent": "FireLink-NASA-SpaceApps-2026"},
            timeout=8,
        )
        if response.ok:
            got_response = True
            data = response.json()
            addr = data.get("address") or {}
            country = addr.get("country")
            region = (addr.get("state") or addr.get("region")
                      or addr.get("province") or addr.get("county"))
            parts = [p for p in (region, country) if p]
            label = ", ".join(parts) if parts else data.get("display_name")
    except Exception:
        label = None

    time.sleep(1.0)  # Nominatim policy: at most one request per second
    if got_response:
        _GEOCODE_CACHE[key] = label
    return label


def annotate_top_clusters_with_location(clusters: List[FireCluster], top_n: int = 5):
    """Only provides place names for the top_n clusters (to respect Nominatim usage limit)."""
    annotated = []
    for i, c in enumerate(clusters):
        place = reverse_geocode(c.latitude, c.longitude) if i < top_n else None
        annotated.append((c, place))
    return annotated
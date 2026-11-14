"""
reconciliation.py
------------------
This is the heart of the FireLink project, and the part that will set you apart from any other team.

The problem it solves:
  VIIRS and MODIS detect the same area but with different spatial and temporal resolutions.
  Question: a point detected by VIIRS and a point detected by MODIS in roughly the same area -
  are they "the same fire" really or two different nearby fires?

The solution:
  Reconciliation engine (Reconciliation Engine) calculates for each pair (VIIRS, MODIS):
    1. the geographic distance between them (Haversine formula)
    2. the time difference between the detection times
    3. "Confidence Score" composed of the two (0.0 - 1.0)
  Then it classifies each fire:
    - CONFIRMED: observed by two sources with high confidence (almost certainly the same fire)
    - LIKELY: observed by two sources but with medium confidence
    - SINGLE_SOURCE: observed by only one source
    - CONFLICTING: points very close in space but very far in time (hard to consider them the same event)
"""

import math
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from firms_client import FireDetection

# Thresholds adjustable according to experience and desired accuracy
MAX_DISTANCE_KM = 2.0       # Maximum distance after which we consider the two points "completely distant"
MAX_TIME_DIFF_HOURS = 6.0   # Maximum time difference after which we consider the two detections "unrelated"


class FireStatus(Enum):
    CONFIRMED = "Confirmed by both sources (high confidence)"
    LIKELY = "Likely match (medium confidence)"
    SINGLE_SOURCE = "Single source only"
    WEAK_MATCH = "Weak match (low confidence, same event uncertain)"


@dataclass
class ReconciledFire:
    """A fire reconciled between sources, ready for display on the map."""
    latitude: float
    longitude: float
    status: FireStatus
    confidence_score: float          # 0.0 - 1.0
    viirs_detection: Optional[FireDetection]
    modis_detection: Optional[FireDetection]
    distance_km: Optional[float] = None
    time_diff_hours: Optional[float] = None


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates the distance in kilometers between two points on the Earth's surface."""
    R = 6371.0  # Earth's radius in kilometers
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def compute_confidence_score(distance_km: float, time_diff_hours: float) -> float:
    """
    Converts distance and time difference to a single confidence score (0.0 - 1.0).
    The smaller the distance and time difference, the higher the confidence.
    """
    distance_score = max(0.0, 1.0 - (distance_km / MAX_DISTANCE_KM))
    time_score = max(0.0, 1.0 - (time_diff_hours / MAX_TIME_DIFF_HOURS))
    # Give distance a slightly higher weight, because it's the most important indicator that it's \"the same fire\"
    return round((distance_score * 0.6) + (time_score * 0.4), 3)


def classify_confidence(score: float) -> FireStatus:
    if score >= 0.75:
        return FireStatus.CONFIRMED
    elif score >= 0.4:
        return FireStatus.LIKELY
    else:
        return FireStatus.WEAK_MATCH


def reconcile(
    viirs_points: List[FireDetection], modis_points: List[FireDetection]
) -> List[ReconciledFire]:
    """
    Main function: takes all VIIRS and MODIS points and returns a unified list
    after reconciling them.

    Performance note: instead of comparing every VIIRS point with every MODIS point (O(n×m) -
    very slow at global scale, could be billions of comparisons), we build
    a geographic index (Spatial Grid) for MODIS points first: we divide the map into
    small squares roughly the size of MAX_DISTANCE_KM, and each VIIRS point only searches in
    its own square and the eight surrounding squares, not in all points.
    This turns the runtime from O(n×m) to about O(n) in practice.
    """
    # Grid cell size in degrees: convert MAX_DISTANCE_KM to degrees approximately (111 km per degree)
    cell_size = max(MAX_DISTANCE_KM / 111.0, 0.001)

    def grid_key(lat: float, lon: float):
        return (int(lat // cell_size), int(lon // cell_size))

    # Index MODIS points by their geographic grid cell
    modis_grid = {}
    for idx, m in enumerate(modis_points):
        modis_grid.setdefault(grid_key(m.latitude, m.longitude), []).append(idx)

    results: List[ReconciledFire] = []
    matched_modis_indices = set()

    for v in viirs_points:
        best_match = None
        best_score = -1.0
        best_distance = None
        best_time_diff = None

        v_row, v_col = grid_key(v.latitude, v.longitude)
        # Search only in the point's cell and the 8 surrounding ones (3x3)
        candidate_indices = []
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                candidate_indices.extend(modis_grid.get((v_row + dr, v_col + dc), []))

        for idx in candidate_indices:
            if idx in matched_modis_indices:
                continue  # Each MODIS point matches only once

            m = modis_points[idx]
            distance = haversine_distance_km(v.latitude, v.longitude, m.latitude, m.longitude)
            if distance > MAX_DISTANCE_KM:
                continue

            time_diff = abs((v.timestamp - m.timestamp).total_seconds()) / 3600.0
            if time_diff > MAX_TIME_DIFF_HOURS:
                continue

            score = compute_confidence_score(distance, time_diff)
            if score > best_score:
                best_score = score
                best_match = (idx, m)
                best_distance = distance
                best_time_diff = time_diff

        if best_match:
            idx, m = best_match
            matched_modis_indices.add(idx)
            results.append(
                ReconciledFire(
                    latitude=v.latitude,
                    longitude=v.longitude,
                    status=classify_confidence(best_score),
                    confidence_score=best_score,
                    viirs_detection=v,
                    modis_detection=m,
                    distance_km=round(best_distance, 3),
                    time_diff_hours=round(best_time_diff, 2),
                )
            )
        else:
            # No MODIS observation is close enough - treat as a single source
            results.append(
                ReconciledFire(
                    latitude=v.latitude,
                    longitude=v.longitude,
                    status=FireStatus.SINGLE_SOURCE,
                    confidence_score=0.0,
                    viirs_detection=v,
                    modis_detection=None,
                )
            )

    # Any MODIS point without a VIIRS pair is also added as a single source
    for idx, m in enumerate(modis_points):
        if idx not in matched_modis_indices:
            results.append(
                ReconciledFire(
                    latitude=m.latitude,
                    longitude=m.longitude,
                    status=FireStatus.SINGLE_SOURCE,
                    confidence_score=0.0,
                    viirs_detection=None,
                    modis_detection=m,
                )
            )

    return results


def summarize(results: List[ReconciledFire]) -> dict:
    """A quick statistical summary, useful for display in the Dashboard and the Pitch."""
    summary = {status: 0 for status in FireStatus}
    for r in results:
        summary[r.status] += 1
    return {status.value: count for status, count in summary.items()}
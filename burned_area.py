"""
burned_area.py
--------------
Estimate of burned area (km²) from NASA FIRMS detections.

Idea (transparent and declared in the interface, not a black box):
  - Each thermal detection is a **single pixel** from the satellite image, and its area is approximately known:
        VIIRS (I-band, 375 m resolution)  →  0.375 × 0.375 = 0.1406 km²
        MODIS (1 km resolution)           →  1 × 1       = 1.0    km²
  - Many detections fall in the same place on the same day (a single heat plume detected
    multiple times). So we count each ≈1 km cell **once** per day, not every detection individually.
  - If a cell has both VIIRS and MODIS detections, we take the larger (MODIS 1 km²) so
    we don't count the same place twice.

The result is an **estimate** not a measurement: a pixel covers an area that may include fire + smoke + unburned ground,
and some small fires are completely below the sensor resolution and are not detected at all. That's why we also return a range
(low/high) based on sensor resolution.

Units: all distances in kilometers, coordinates in decimal degrees.
"""

# Pixel area of each satellite in square kilometers (source: official sensor resolution)
PIXEL_KM2 = {"VIIRS": 0.1406, "MODIS": 1.0}

CELL_DEG = 0.01          # ≈1.1 km at the equator: closer than that we consider it the same pixel
FALLBACK_KM2 = 1.0       # If the satellite is unknown, assume the worst (MODIS) so we don't underestimate


def cell_of(lat: float, lon: float):
    """Cell key (~1 km) that the observation falls in."""
    return (round(lat / CELL_DEG), round(lon / CELL_DEG))


def add(cells: dict, family: str, lat: float, lon: float):
    """Adds an observation to today's cells: keeps the largest pixel area for each cell."""
    if lat is None or lon is None:
        return
    key = cell_of(lat, lon)
    area = PIXEL_KM2.get((family or "").upper(), FALLBACK_KM2)
    if key not in cells or area > cells[key]:
        cells[key] = area


def finalize(cells: dict) -> dict:
    """From today's cells → estimated area (km²) + lower/upper range + number of pixels."""
    if not cells:
        return {"burned_km2": 0.0, "burned_km2_low": 0.0, "burned_km2_high": 0.0, "burned_pixels": 0}
    n = len(cells)
    best = sum(cells.values())
    return {
        "burned_km2": round(best, 1),
        "burned_km2_low": round(n * PIXEL_KM2["VIIRS"], 1),
        "burned_km2_high": round(n * PIXEL_KM2["MODIS"], 1),
        "burned_pixels": n,
    }


def estimate(points) -> dict:
    """Shortcut: points = [(family, lat, lon), ...] → same outputs as finalize."""
    cells = {}
    for family, lat, lon in points:
        add(cells, family, lat, lon)
    return finalize(cells)


def method_note() -> str:
    """Explanation of the method displayed to the user next to the number."""
    return (
        "Estimated burned area: one 1 km pixel per fire cell per day "
        "(VIIRS 375 m = 0.14 km², MODIS 1 km = 1 km²). It is an estimate from "
        "hotspots, not a burned-scar measurement."
    )

"""
firms_client.py
----------------
A simple client to fetch fire data (Fire Hotspots) from the NASA FIRMS API
for different sources: VIIRS and MODIS, so we can compare them later.

To run this code you must:
1. register for free and get MAP_KEY from here:
   https://firms.modaps.eosdis.nasa.gov/api/area/
2. put the MAP_KEY in a .env file (copy .env.example) or in an environment variable named FIRMS_MAP_KEY,
   or pass it directly to the function. (see config.py) - the key is not written in the code.
"""

import csv
import io
import os
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

import requests

from config import get_map_key

FIRMS_BASE_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"

# Main available data sources in FIRMS (SOURCE names)
SOURCE_VIIRS_SNPP = "VIIRS_SNPP_NRT"     # VIIRS - Suomi NPP satellite (near-real-time data NRT)
SOURCE_VIIRS_NOAA20 = "VIIRS_NOAA20_NRT"  # VIIRS - NOAA-20 satellite (near-real-time data NRT)
SOURCE_MODIS = "MODIS_NRT"                # MODIS - Terra/Aqua (near-real-time data NRT)

# Historical data sources (Standard Processing) - for any event older than how many months
# Its accuracy is scientifically higher than NRT, but it lags 2-5 months behind the actual event time
SOURCE_VIIRS_SNPP_SP = "VIIRS_SNPP_SP"
SOURCE_VIIRS_NOAA20_SP = "VIIRS_NOAA20_SP"
SOURCE_MODIS_SP = "MODIS_SP"


@dataclass
class FireDetection:
    """A single fire observation point, standardized in format regardless of its source."""
    latitude: float
    longitude: float
    brightness: float       # Brightness/temperature (Kelvin approximately)
    confidence: str         # Detection confidence from the satellite itself (low/nominal/high or number)
    acq_date: str           # Observation date (YYYY-MM-DD)
    acq_time: str           # Observation time (HHMM UTC)
    frp: float              # Fire Radiative Power - thermal radiative power (indicator of fire intensity)
    source: str             # VIIRS_SNPP_NRT / VIIRS_NOAA20_NRT / MODIS_NRT

    @property
    def timestamp(self) -> datetime:
        """Converts the textual date and time to a comparable datetime object."""
        time_str = self.acq_time.zfill(4)  # For example "930" becomes "0930"
        dt_str = f"{self.acq_date} {time_str[:2]}:{time_str[2:]}"
        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M")


def fetch_fire_data(
    bbox: str,
    source: str,
    day_range: int = 1,
    date: Optional[str] = None,
    map_key: Optional[str] = None,
) -> List[FireDetection]:
    """
    Fetches fire data from the FIRMS API for a given area and source.

    bbox: coordinates in the format "west,south,east,north"
          example for Sinai approximately: "32.5,28.5,34.5,31.0"
    source: SOURCE_VIIRS_SNPP or SOURCE_MODIS ...etc
    day_range: number of days requested (1 for the most recent available day)
    date: specific date in YYYY-MM-DD format (if empty, it takes the most recent available data)
    map_key: your API Key; if empty it will take it from FIRMS_MAP_KEY (environment variable or .env file)
    """
    key = get_map_key(map_key)   # Raises MissingKeyError with a clear message if there is no key

    url = f"{FIRMS_BASE_URL}/{key}/{source}/{bbox}/{day_range}"
    if date:
        url += f"/{date}"

    response = requests.get(url, timeout=60)
    if not response.ok:
        raise ValueError(
            f"FIRMS API error ({response.status_code}) for source '{source}':\n"
            f"URL: {url}\n"
            f"Response: {response.text.strip()}"
        )

    # NASA sometimes responds 200 OK with a textual error message (for example "Invalid MAP_KEY.") instead of CSV.
    # Without this check the code used to read it as "zero fires" and show the user that there is no fire.
    body = response.text.lstrip()
    if body and not body.lower().startswith(("latitude", "country_id", "data_id")):
        raise ValueError(
            f"Unexpected answer from FIRMS for source '{source}' (not a CSV table):\n"
            f"{body.strip()[:300]}"
        )

    detections = []
    reader = csv.DictReader(io.StringIO(response.text))
    for row in reader:
        try:
            detections.append(
                FireDetection(
                    latitude=float(row["latitude"]),
                    longitude=float(row["longitude"]),
                    brightness=float(row.get("bright_ti4") or row.get("brightness") or 0),
                    confidence=row.get("confidence", ""),
                    acq_date=row["acq_date"],
                    acq_time=row["acq_time"],
                    frp=float(row.get("frp") or 0),
                    source=source,
                )
            )
        except (KeyError, ValueError):
            # Ignore any row with missing or corrupted data
            continue

    return detections


def fetch_viirs_and_modis(bbox: str, day_range: int = 1, date: Optional[str] = None):
    """Helper function that retrieves VIIRS (both sources) and MODIS data together for the same area (near-real-time NRT data)."""
    viirs_snpp = fetch_fire_data(bbox, SOURCE_VIIRS_SNPP, day_range, date)
    viirs_noaa20 = fetch_fire_data(bbox, SOURCE_VIIRS_NOAA20, day_range, date)
    modis = fetch_fire_data(bbox, SOURCE_MODIS, day_range, date)

    all_viirs = viirs_snpp + viirs_noaa20
    return all_viirs, modis


def fetch_viirs_and_modis_historical(bbox: str, date: str, day_range: int = 7):
    """
    Same idea but for historical data (older than a few months) using Standard
    Processing sources, and this is what we use in the Case Study (documented real fires).
    Note: SP sources accept a maximum day_range = 5 days per request.
    """
    viirs_snpp = fetch_fire_data(bbox, SOURCE_VIIRS_SNPP_SP, day_range, date)
    viirs_noaa20 = fetch_fire_data(bbox, SOURCE_VIIRS_NOAA20_SP, day_range, date)
    modis = fetch_fire_data(bbox, SOURCE_MODIS_SP, day_range, date)

    all_viirs = viirs_snpp + viirs_noaa20
    return all_viirs, modis


def fetch_viirs_and_modis_auto(bbox: str, date: Optional[str] = None, day_range: int = 3):
    """
    The "smart" function that decides by itself how to fetch the data:
      - Without a date (date=None) -> near-real-time NRT data (latest day/days)
      - A date newer than ~60 days -> also NRT (the near archive is available there)
      - A date older than that -> Standard Processing (SP), with automatic restriction of day_range
        to a maximum of 5 days because that's NASA's limit for these sources

    This way you can try any old fire (from 2012 until roughly now) without
    thinking yourself about the source type - the code picks it correctly by itself.
    """
    if date is None:
        return fetch_viirs_and_modis(bbox, day_range=day_range)

    days_ago = (datetime.utcnow().date() - datetime.strptime(date, "%Y-%m-%d").date()).days

    if days_ago <= 60:
        return fetch_viirs_and_modis(bbox, day_range=day_range, date=date)
    else:
        capped_range = min(day_range, 5)  # NASA limit for SP sources
        return fetch_viirs_and_modis_historical(bbox, date, capped_range)


if __name__ == "__main__":
    # Demo example: approximate Sinai region
    SINAI_BBOX = "32.5,28.5,34.5,31.0"
    viirs, modis = fetch_viirs_and_modis(SINAI_BBOX, day_range=3)
    print(f"VIIRS detections: {len(viirs)}")
    print(f"MODIS detections: {len(modis)}")

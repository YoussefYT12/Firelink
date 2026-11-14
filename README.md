# FireLink

Two-satellite fire detection (**VIIRS** + **MODIS** from NASA FIRMS) reconciled into one map,
with a burned-day calendar, a unified history since 2003, burned-area estimates and email alerts.

## Quick start

```bash
pip install requests            # the only package the core app needs
python run_all.py               # the map opens immediately, data loads behind it
# Windows: double-click start_firelink.bat (it installs requests for you)
# Fast start on the last saved file:  python run_all.py --no-fetch
```

The shipped build includes a ready `.env` with a NASA key, so nothing has to be configured first.
A free key is available at <https://firms.modaps.eosdis.nasa.gov/api/area/>.
The key is never hard-coded: it is read from the `FIRMS_MAP_KEY` environment variable or from `.env`
(git-ignored). The local server listens on `127.0.0.1` and serves three files only
(`map.html` plus the generated GeoJSON/CSV), so `.env` and the source cannot be read through it.

To try the interface without a key or a network: `python run_all.py --mock` (mock fires) and
`python build_history.py --demo` (mock history, labelled DEMO).

## What the interface does

| Feature | Where |
|---|---|
| Fires for any day since 2012, worldwide or for one area, classified Confirmed / Likely / Single / Weak | Search |
| **Area selection**: draw a box, **type coordinates** (west/east/south/north), or a point + radius | Search -> Enter coordinates |
| **Download the detected points as CSV** (and GeoJSON) | Under the counters: Download CSV |
| Daily activity calendar (14-90 days) with unusual days and critical periods, plus CSV export | Activity calendar |
| **Unified history 2003 -> today** (monthly chart) | History since 2003 |
| **Compare any month with the multi-year mean for that month**: ratio, rank, end-of-month projection | History -> comparison card |
| Busiest months and years, and the typical fire season | History |
| **Download the history of the box you drew, from inside the map** with progress, ETA, stop and resume | History -> Download history for this box |
| `firelink_output.csv` and `.geojson`, rewritten on every update | project folder |

CSV columns: `latitude, longitude, status, confidence_score, distance_km, time_diff_hours,
timestamp_utc, frp_mw, brightness_k, detected_by_viirs, detected_by_modis`.
At world scale the file keeps Confirmed + Likely only, for speed; draw a smaller area for every point.

### Screen controls
- **Map key** (bottom-right) starts **collapsed** as an obvious button ("MAP KEY - CLICK TO OPEN")
  so it never hides the fires. One click opens the fire classes, the colour-blind-safe palette and the
  NASA GIBS imagery layer with its opacity slider. The choice is remembered and the panel scrolls on
  short screens.
- **The < / > button** under the zoom controls hides or shows the side panel so the map can take the
  whole screen.

### For judges (publish a link that opens in a browser)
- **Render, free:** `render.yaml` is included - push to GitHub, choose New Blueprint, add
  `FIRMS_MAP_KEY` under Environment. Full steps in [DEPLOY.md](DEPLOY.md).
- Any Python host: `PORT=8080 python run_all.py --no-fetch --host 0.0.0.0` (the app reads `PORT`).
- `/api/query` results are cached for 10 minutes, so browsing stays fast and the NASA key quota is
  never spent twice on the same question.

### Imagery, share links, wind and protected areas
- **NASA GIBS** in the map key: VIIRS or MODIS Terra true colour for the day on the map, with an
  opacity slider; a clear message appears when that day is not published yet.
- **Share link**: `map.html?bbox=25,22,35,32&date=2026-11-13` reopens the same box and day; the URL
  updates itself after every search.
- **Wind** (`weather.py`, OpenWeather, optional key): "wind 18 km/h from N - fire likely spreads
  towards S". **Protected areas** (`protected_areas.py`, OpenStreetMap): nearest reserve within 8 km,
  cached on disk. **Land cover** comes from ESA WorldCover. Set `FIRELINK_ENRICH=0` to skip all of it.

### Email alerts

```bash
python notify.py --test                                        # test email
python notify.py --bbox 25,22,35,32 --min-frp 30               # alert above 30 MW
python notify.py --bbox 25,22,35,32 --min-frp 30 --dry-run     # print, do not send
python notify.py --bbox world --min-frp 80 --min-count 5
```

Configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY` (starttls|ssl|none), `SMTP_USER`,
`SMTP_PASSWORD`, `ALERT_EMAIL_TO`, `ALERT_EMAIL_FROM` in `.env` (see `.env.example`).
Gmail needs an App Password. One alert per (area + threshold + day) via `alert_state.json`;
`--force` overrides it. Exit codes: `0` sent, `2` nothing to report, `1` error.

## History data (2003 -> today)

From inside the map: draw a box, open **History since 2003**, pick "How far back" and press
**Download history for this box**. The button estimates the number of NASA requests before it starts,
then shows progress, ETA and a Stop button; the download runs in the local server, so refreshing the
page does not lose it and it resumes where it stopped.

From the terminal (for large areas or scripts):

```bash
python build_history.py --check                       # quick reachability test
python build_history.py --name egypt_delta            # a ready-made area
python build_history.py --name my_area --bbox "west,south,east,north"   # max 150 square degrees
python build_history.py --info                        # what is already downloaded
```

Files live in `history_data/`. A box drawn on the map must sit inside a downloaded area.
Days NASA has not published are recorded as failures and retried later, never as zero fires.

**Unit**: a `cell-day` is one 1 km cell with a detection on that day, summed per month, which removes
the sensor resolution difference (VIIRS 375 m vs MODIS 1 km).
**Harmonisation** (`harmonize.py`): VIIRS sees smaller fires, so simply adding both sensors creates a
fake jump in 2012. The calibration factor is the median of monthly VIIRS/MODIS ratios during the
overlap years (per calendar month when four or more years exist), applied to MODIS before 2012, with a
p10-p90 uncertainty band and leave-one-year-out quality checking. Months with missing days are
excluded from means and ranks; the current month is compared with its end-of-month projection.

## Tests

```bash
python -m unittest discover -s tests -t .
```

170 tests cover publication lag, burned-area maths, filters and reports, share links and imagery,
the email notifier, wind and protected areas, the query cache and the UI contracts. They need no
network and no key.

## Project layout

```
run_all.py            local server + API: /api/query /api/calendar /api/history /api/history/build
map.html              the whole interface (Leaflet, canvas charts, no extra libraries)
firms_client.py       live FIRMS download          config.py       key handling
reconciliation.py     VIIRS <-> MODIS matching     clustering.py / ai_risk.py / landcover.py
calendar_activity.py  daily activity calendar      pipeline.py     payload + GeoJSON + CSV
history.py            resumable archive download   build_history.py  command line
harmonize.py          MODIS -> VIIRS calibration   history_analysis.py  series, comparison, ranks
history_jobs.py       background download + progress (used by the map button)
```

> Security note: `--host 0.0.0.0` exposes the server (and the history download button) to your whole
> network. Keep the default `127.0.0.1` unless you need it.

## Data
- NASA FIRMS area API - VIIRS S-NPP and MODIS active fire detections.
- NASA GIBS - VIIRS/MODIS true-colour imagery tiles.
- ESA WorldCover 2021 - land-cover context (non-NASA partner dataset).

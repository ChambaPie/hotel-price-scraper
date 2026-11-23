# Hotel price scraper (public)

Public data collector for hotels and B&Bs within a 10-hour drive of Trieste: a catalog of priceable properties,
their landscape, and their prices. **Deal identification and alerts are not here**; a private repository reads the
files below.

## What it collects (GitHub Actions, free for public repositories)

| Workflow | Schedule (UTC) | Output |
|---|---|---|
| Refresh catalog (daily) | 03:41 | `data/catalog.csv` (TripAdvisor properties via Xotelo, drive times, official stars), `data/watchlist.csv` |
| Refresh official stars and landscape (weekly) | Mon 02:11 | `data/osm_catalog.csv`, `data/landscape.csv` (elevation, sea, beach, lake, ski area, city) |
| Place details (daily) | 10:29 | `data/locations.csv` (checked position of each watched hotel: OpenStreetMap object, Photon search, or the listed one; reused by every run, `config/location_fixes.csv` for fixes by hand), `data/places.csv` (type of place around each watched property: mountain, ski, sea, beach, lake, old town, major city, city, vineyards, thermal, countryside), `data/google_ratings.csv` (Google rating, number of reviews, class, amenities) |
| Scan (TripAdvisor heatmap) | 05:17, 17:17 | `data/heatmap.csv`, `data/heatmap_changes.csv` |
| Booking.com collection | hourly check, a full pass every 4 h | `data/booking_ids.csv`, `data/last_minute_cards.csv`; every night's price for 24 months (months 13-24 refreshed daily) in `booking_calendars.csv` on the `snapshots` branch; 4 parallel jobs |

## Data contract (read by the private repo)
- `main/data/*.csv` as above; `snapshots/booking_calendars.csv`: one row per property, `nights` = one token per night
  from `first_date` (`""` none, `"184"` price, `"184/3"` price with a 3-night minimum stay), joined by `;`.
- Raw URLs: `https://raw.githubusercontent.com/<owner>/<repo>/<branch>/<path>`.

## Settings
Everything is configurable in `config/settings.yaml` and `config/selection.yaml` (which properties are collected).

## Run locally
```bash
pip install -r requirements-dev.txt && python -m pytest -q
```

Personal, low-volume collection. Sources: OpenStreetMap (ODbL), Open Topo Data, OSRM demo server (FOSSGIS),
Xotelo, Booking.com (via a headless browser; its terms don't allow automated access).

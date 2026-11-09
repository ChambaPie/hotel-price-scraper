# Hotel price scraper (public)

Public data collector for hotels and B&Bs within a 10-hour drive of Trieste: a catalog of priceable properties,
their landscape, and their prices. **Deal identification and alerts are not here**; a private repository reads the
files below.

## What it collects (GitHub Actions, free for public repositories)

| Workflow | Schedule (UTC) | Output |
|---|---|---|
| Refresh catalog (daily) | 03:41 | `data/catalog.csv` (TripAdvisor properties via Xotelo, drive times, official stars), `data/watchlist.csv` |
| Refresh official stars and landscape (weekly) | Mon 02:11 | `data/osm_catalog.csv`, `data/landscape.csv` (elevation, sea, beach, lake, ski area, city) |
| Scan (TripAdvisor heatmap) | 05:17, 17:17 | `data/heatmap.csv`, `data/heatmap_changes.csv` |
| Booking.com collection | 11:47, 23:47 | `data/booking_ids.csv`, `data/last_minute_cards.csv`; every night's price for 12 months in `booking_calendars.csv` on the `snapshots` branch |

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

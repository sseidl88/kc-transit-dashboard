# kc-transit-dashboard

Daily-refreshed scheduled-service dashboard for KCATA/RideKC bus and streetcar routes.

**Live site:** GitHub Pages serving from `docs/` on `main`

## What it shows

Built from KCATA's public GTFS static feed (routes, stops, shapes, schedules), pulled
daily by GitHub Actions:

- A map of every weekday route, colored by scheduled frequency, with a "frequent
  network only" filter and a layer of transfer hubs (stops served by 3+ routes)
- Trip frequency (headway), span of service, stop count, and route length per route
- What changed since the last detected schedule revision (KCATA updates schedules a
  few times a year, not daily — this is usually empty, and that's expected)
- A one-time **2020 → today** network comparison (routes added/dropped, frequency
  changes), backfilled from the one historical GTFS snapshot that's freely available
- A weekday-vs-weekend service equity view — routes with strong weekday service but
  little or no weekend coverage
- A side-by-side comparison tool for any two routes, plus a search/sort table of all routes

## Why "scheduled service" and not "on-time performance" / "ridership"

KCATA doesn't publish those as an open, machine-readable feed:

- Real-time vehicle positions/trip updates exist (KCATA uses Swiftly) but the public
  endpoint requires an authenticated API key.
- Official ridership and on-time-performance numbers are published as monthly PDF
  reports (system-wide aggregates), not a per-route daily API.

So this dashboard computes what's actually derivable from the open static feed —
frequency, coverage, and service span — rather than faking numbers that aren't
really available. See [CLAUDE.md](CLAUDE.md) for the full rationale and architecture.

## Running locally

```
pip install requests
python kc_transit_update.py
```

Writes JSON/GeoJSON into `docs/data/`. Open `docs/index.html` with any static file
server to view.

KCATA's own feed server isn't reachable from cloud/CI networks, so GitHub Actions
pulls via a free [Transitland](https://www.transit.land/) API key instead (repo
secret `TRANSITLAND_API_KEY`). Running from a normal residential network, the
script falls back to KCATA's direct feed with no key needed. See
[CLAUDE.md](CLAUDE.md) for details.

## Tests

```
pip install requests pytest
python -m pytest tests/ -v
```

Runs on every push/PR via `.github/workflows/ci.yml` (separate from the daily
data-pull workflow).

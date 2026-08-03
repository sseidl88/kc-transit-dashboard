# kc-transit-dashboard

Daily-refreshed scheduled-service dashboard for KCATA/RideKC bus and streetcar routes.

**Live site:** GitHub Pages serving from `docs/` on `main`

## What it shows

Built from KCATA's public GTFS static feed (routes, stops, shapes, schedules), pulled
daily by GitHub Actions:

- A map of every weekday route, colored by scheduled frequency
- Trip frequency (headway), span of service, stop count, and route length per route
- What changed since the last detected schedule revision (KCATA updates schedules a
  few times a year, not daily — this is usually empty, and that's expected)
- A side-by-side comparison tool for any two routes

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

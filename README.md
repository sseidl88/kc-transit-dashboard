# kc-transit-dashboard

Daily-refreshed scheduled-service dashboard covering two Kansas City-area
transit agencies: KCATA and RideKC Johnson County Transit — built around one
question: now that riding costs money again, is service actually reaching
the people who need it most?

**Live site:** GitHub Pages serving from `docs/` on `main`

## What it shows

Built from each agency's public GTFS static feed (routes, stops, shapes, schedules),
pulled daily by GitHub Actions:

- A map of every weekday route across both agencies, colored by scheduled frequency
  (opens showing just the frequent network by default — full network is one click
  away), with toggles for transfer hubs (stops served by 3+ routes), a frequent-network
  walkshed (¼-mile buffer around frequent stops), overall stop density, and TOD
  opportunity zones (real KC parcels zoned single-family-only despite sitting within
  ¼ mile of frequent transit — a small, honestly-reported finding: 7 parcels, not
  hundreds)
- **How much longer does the bus actually take?** — click two points on the map to
  compare estimated transit time (walk + wait + ride, using each route's own
  scheduled speed) against driving (OSRM). Single-route only — if no direct route
  serves both points, it says so instead of guessing at a transfer
- Frequent-network access by Kansas City, MO council district — a genuine coverage/
  equity view, not just a map
- Trip frequency (headway), span of service, stop count, and route length per route
- What changed since the last detected schedule revision (agencies update schedules a
  few times a year, not daily — this is usually empty, and that's expected)
- A one-time **2020 → today** network comparison for KCATA (routes added/dropped,
  frequency changes), backfilled from the one historical GTFS snapshot that's freely
  available
- Potential future extensions currently under study (North Kansas City, 18th & Vine,
  and an East-West corridor) — real, sourced study-area boundaries with Census
  population figures and a rough cost estimate (illustrative length × KC's own last
  two extension costs per mile — Riverfront ~$87M/mi, Main Street ~$100M/mi), plus an
  illustrative route line through each study's own named streets, snapped to real
  roads via OSRM (same router the design tool below uses) rather than drawn as
  straight segments. Clearly marked as not-a-confirmed-alignment — see
  [CLAUDE.md](CLAUDE.md) for exactly what's real data versus this dashboard's own
  approximation
- **How would this actually get paid for?** — real funding facts for the two built
  extensions (a federal Section 5309 grant + a voter-approved local taxing district),
  not a funding plan for the proposed ones (none has a confirmed source yet)
- **Design your own extension** — click points on the map to sketch a route; it snaps
  to real streets via [OSRM](https://project-osrm.org/) (free, no key) and estimates
  length, cost, and population served live in the browser, using the same benchmarks
  and a pre-baked Census dataset (no live API calls after the one-time build). Also:
  a side-by-side comparison against the 3 real study corridors, the actual street
  names your route follows, a warning if it crosses the Missouri River (cost is
  almost certainly higher there), how much of it is new coverage vs. already served
  by the existing frequent network, and a shareable link
- **KC's original 1948 streetcar network** — 10 lines digitized from a scan of an
  October 1948 Kansas City Public Service Co. system map, toggleable on the map in
  sepia. Confidence is marked per route (high/medium/low) depending on how legible
  that part of the map actually was — see [CLAUDE.md](CLAUDE.md) for specifics.
  One unplanned finding: the old Country Club line ran almost the exact corridor of
  today's Main Street Extension. A "Compare 1948 ↔ Today" button turns this into a
  draggable before/after slider right on the map instead of a plain on/off toggle
- **Where should KC invest next?** — with fares back as of June 2026, a plain,
  explainable "priority zone" rule (above-median car-free households + far from the
  frequent network) built from real Census data, compared against the 3 corridors
  above to see whether current planning actually matches where the need is highest.
  It doesn't, for 2 of 3 — reported honestly either way
- A weekday-vs-weekend service equity view — routes with strong weekday service but
  little or no weekend coverage
- A side-by-side comparison tool for any two routes (shareable via URL), plus a
  search/sort table of all routes

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
python kc_transit_update.py --agency kcata
python kc_transit_update.py --agency jocounty
```

Writes JSON/GeoJSON into `docs/data/` (kcata) and `docs/data/jocounty/`. Open
`docs/index.html` with any static file server to view.

KCATA's own feed server isn't reachable from cloud/CI networks, so GitHub Actions
pulls both agencies via a free [Transitland](https://www.transit.land/) API key
instead (repo secret `TRANSITLAND_API_KEY`). Running from a normal residential
network, the script falls back to each agency's direct feed with no key needed.
See [CLAUDE.md](CLAUDE.md) for details, including why Unified Government Transit
and IndeBus aren't included.

## Tests

```
pip install requests pytest
python -m pytest tests/ -v
```

Runs on every push/PR via `.github/workflows/ci.yml` (separate from the daily
data-pull workflow).

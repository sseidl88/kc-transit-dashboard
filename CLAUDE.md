# kc-transit-dashboard

GitHub Pages dashboard covering KCATA/RideKC scheduled transit service (Kansas City).

**Repo:** sseidl88/kc-transit-dashboard
**Live site:** GitHub Pages serving from `docs/` on `main` branch

---

## Key rules

- **Scope is scheduled service only — not real-time, not ridership, not official
  on-time performance.** KCATA doesn't publish those as an open API:
  - GTFS-Realtime (vehicle positions, trip updates) exists via Swiftly
    (`api.goswift.ly/real-time/kcata/...`) but returns `401 Unauthorized` —
    requires a Bearer token KCATA/Swiftly would have to issue.
  - Ridership and on-time-performance figures are published as monthly PDF
    "Key Performance Indicator" reports at `ridekc.org/planning/dashboard` —
    system-wide aggregates, not a per-route daily API, and not worth scraping
    for a "daily" dashboard since they update monthly.
  - If real-time access is ever granted, that becomes an additive module —
    don't retrofit the existing metrics to fake precision they don't have.
- Data source is **KCATA's public static GTFS feed** — freely downloadable, no
  auth, no rate limit observed... from a residential/normal network. It is
  **not reachable from cloud/datacenter IPs** — confirmed with two full runs
  of hard connect-timeouts from GitHub Actions' own runners (5 retries each,
  ~90s per attempt, 100% failure, no variance — a dropped-packet block, not
  an overloaded server) and from this project's dev sandbox. Pulling directly
  from `kc-metro.com` in CI does not work; don't revert to it without solving
  that first.
- **Actual source in CI: Transitland's mirror**, via
  `TRANSITLAND_API_KEY` (repo secret) →
  `https://transit.land/api/v2/rest/feeds/f-9yu-kcata/download_latest_feed_version?apikey=...`
  (302-redirects to a CDN-hosted copy of the same feed KCATA publishes).
  Transitland's free tier only serves the *latest* feed version, which is
  exactly what this pipeline needs — no historical archive access required.
  Get a free key at transit.land (Interline account → free plan → API key
  under "Subscriptions and API keys"). `download_gtfs()` falls back to the
  direct `kc-metro.com` URL when `TRANSITLAND_API_KEY` isn't set (e.g. bare
  local dev), which will work outside cloud CI/sandboxes but not inside them.
- Retries with backoff (5 attempts) before giving up either way; if a run
  still fails, it fails loudly in the Actions log rather than silently
  producing empty data — check the Actions tab if a day's update is missing.
  Tomorrow's cron just tries again; nothing is lost, only delayed.
- Always `git pull --rebase origin main` before `git push` in CI, same reason as
  any daily-cron-writes-to-main setup: avoid races between scheduled and manual runs.

---

## Architecture

```
KCATA GTFS static zip → kc_transit_update.py (GitHub Actions daily cron)
                       → JSON/GeoJSON committed to docs/data/
                       → GitHub Pages serves docs/ as a static site
                       → docs/index.html reads data via fetch()
```

- No build step — vanilla JS inline in `docs/index.html`, Leaflet loaded via CDN
  for the map, styles in `docs/style.css`.
- JSON files in the repo are the database — same pattern as the nba-visual project.
- Workflow: `.github/workflows/daily-update.yml` — cron `0 11 * * *` + `workflow_dispatch`.

---

## Data pipeline (`kc_transit_update.py`)

GTFS feed: `http://www.kc-metro.com/gtf/google_transit.zip` (KCATA's official feed).

**What it computes per route**, from `routes.txt` / `trips.txt` / `stop_times.txt` /
`calendar.txt` / `calendar_dates.txt` / `shapes.txt`:

- Representative weekday/Saturday/Sunday dates are picked as the nearest upcoming
  Wed/Sat/Sun with actual scheduled service (`pick_representative_dates`) — avoids
  picking a date GTFS calendar_dates has zeroed out.
- `trips_weekday/saturday/sunday` — trip counts by service day type.
- `headway_minutes` — median gap between weekday trip start times, computed only
  on the route's dominant `direction_id` (avoids double-counting both directions).
- `span_start`/`span_end` — first/last scheduled time, formatted 12-hour, with a
  `(+1)` suffix for GTFS's after-midnight `25:xx:xx`-style times.
- `stop_count` — distinct stops served on weekday trips.
- `route_length_miles` — haversine sum along the route's most-common `shape_id`.
- `frequency_tier` / map line `color` — bucketed from `headway_minutes` using the
  dataviz skill's sequential blue ramp (ordinal steps 550/400/300/250 — see
  `docs/style.css` header comment and `FREQUENCY_TIERS` in the script). The map
  itself doesn't need a dark-mode variant of these colors since OSM tiles are
  always light regardless of page theme.

**History / trending** (`docs/data/history.json`, `docs/data/trending.json`):
Because GTFS static changes a handful of times a year (KCATA "sign-ups"), not
daily, the pipeline only appends a new history entry when a route's
`(trips_weekday, headway_minutes)` signature actually differs from the last
recorded entry — most days produce zero new history rows and empty trending
lists, which is the correct/expected state, not a bug.

**Gotcha already hit once:** `route_signature()` must return **lists**, not
tuples — the signature gets JSON round-tripped through `history.json`, and
`json.loads` never reconstructs tuples, so comparing a fresh tuple against a
disk-loaded list is always `!=` even when nothing changed. Caused every run to
falsely detect a "change." Covered by the local fixture test.

---

## Testing without live network access

The `kc-metro.com` feed host was unreachable from the sandboxed dev environment
during initial build (works fine from GitHub Actions' runners). `--local-zip PATH`
lets you point the script at any GTFS-shaped zip — a synthetic fixture was used to
validate parsing logic (frequency tiers, headway math, trending detection,
idempotency) before the first real Actions run. Re-generate a quick fixture if you
need to test again: a `routes.txt`/`calendar.txt`/`trips.txt`/`stop_times.txt`/
`shapes.txt` set with a handful of routes at different headways is enough.

---

## Frontend (`docs/index.html`)

- Single fetch of `data/routes.json` (metrics + meta), `data/routes.geojson` (map
  lines), `data/trending.json` — all with graceful fallback if a file 404s (e.g.
  before the first Actions run has ever committed data).
- Map: Leaflet + OpenStreetMap tiles (no API key), routes colored by frequency
  tier, popup per route on click, hover to highlight.
- Route comparison mirrors the nba-visual "player comparison" pattern — two
  `<select>`s, thin comparison bars per metric. Headway is inverted when sizing
  its bar (lower minutes = more frequent = should read as the "bigger" bar).
- Table is client-side sortable by clicking any header (`sortState` in the script).

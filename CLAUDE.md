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

**Two real bugs hit while building this, both now covered by `tests/test_pipeline.py`:**
1. `route_signature()` must return **lists**, not tuples — the signature gets
   JSON round-tripped through `history.json`, and `json.loads` never
   reconstructs tuples, so comparing a fresh tuple against a disk-loaded list
   is always `!=` even when nothing changed. Caused every run to falsely
   detect a "change."
2. `pick_representative_dates()` must **clamp its search into the feed's own
   date range**, not always search forward from `datetime.now()`. Surfaced
   while backfilling the 2020 archive: "today" (2026) falls outside every
   `calendar.txt`/`calendar_dates.txt` entry in a 2020 feed, so
   `active_service_ids()` came back empty for every candidate date and every
   route silently got zero trips (`0 routes, 0 weekday trips` — no error, just
   wrong). Fixed via `feed_date_bounds()` clamping the anchor date into
   `[feed_min, feed_max - 14d]` before searching.

**Route matching uses `route_short_name`, not `route_id`** (see `route_key()`),
because `route_id` isn't guaranteed stable across feed republishes, and
`compute_trending()` needs a stable join key to diff two snapshots — daily
consecutive ones or, for the historical backfill, snapshots years apart.
On top of that, `compute_trending()` reconciles apparent "added + discontinued"
pairs that share an exact `route_long_name` (see the long comment above the
`discontinued_by_name` block) — needed because KC Streetcar's public route
code changed **STRC → STCR** between the 2020 archive and today's feed. Without
that reconciliation pass, KC's flagship transit line would show up as
simultaneously a brand-new route and a discontinued one. The same reconciliation
logic is duplicated in `docs/index.html` (`computeHistoricalDiff()`) for the
frontend's then-vs-now section — keep both in sync if this logic changes.

---

## Historical backfill (`docs/data/baseline_2020.json`)

KCATA's own feed archive isn't freely accessible for anything but the *latest*
version — Transitland's free tier only serves current feeds; historical
`feed_versions` downloads 401 without a paid or Interline
Hobbyist/Academic-credits plan (a form-based application, not automatic). The
only free historical data available was a **single** Wayback Machine snapshot
of `kc-metro.com/gtf/google_transit.zip` from **2020-11-20**
(`web.archive.org/cdx/search/cdx?url=kc-metro.com/gtf/google_transit.zip` —
checked broader domain queries too, this is genuinely the only one). So the
"Network change" section is a one-time **then-vs-now** comparison, not a
continuous timeline — framed that way in the UI copy, not oversold as more
than it is.

`baseline_2020.json` was generated once, locally, and is committed as static
data — **the daily workflow never regenerates it** (`main()` only writes it
when `--baseline-out` is passed, which the workflow doesn't pass). Regenerate
command, if ever needed:
```
python kc_transit_update.py --local-zip <2020 zip> --as-of-date 2020-12-01 \
  --baseline-out docs/data/baseline_2020.json --baseline-label "November 2020"
```
If someone gets Transitland's Hobbyist/Academic credits (or a paid plan)
later, this could become a real multi-point timeline instead — worth
revisiting, not worth blocking on.

---

## Testing

`tests/test_pipeline.py` (pytest, run via `.github/workflows/ci.yml` on every
push/PR) covers the time/distance helpers, calendar exception handling, both
bugs above as explicit regression tests, `compute_trending()`'s up/down/added/
discontinued/reconciliation logic, and an end-to-end `build_dataset()` pass
against a small synthetic GTFS zip built in-memory (`_make_fixture_zip()`).

Separately: the `kc-metro.com` feed host was unreachable from the sandboxed dev
environment during initial build (works fine from GitHub Actions' runners).
`--local-zip PATH` points the script at any GTFS-shaped zip for manual
end-to-end runs outside the test suite.

---

## Frontend (`docs/index.html`)

- Fetches `data/routes.json` (metrics + meta), `data/routes.geojson` (route
  lines), `data/hubs.geojson` (transfer-hub points), `data/trending.json`, and
  `data/baseline_2020.json` — all with graceful fallback if a file 404s (e.g.
  before the first Actions run has ever committed data, or `baseline_2020.json`
  if it's ever removed).
- Map: Leaflet + OpenStreetMap tiles (no API key), routes colored by frequency
  tier, popup per route on click, hover to highlight. Two toggles:
  "Frequent network only" (rebuilds the route layer filtered to
  `headway_minutes <= 15`, see `FREQUENT_MAX_HEADWAY`) and transfer hubs
  (stops served by 3+ weekday routes, computed server-side in
  `build_dataset()`, shown/hidden without rebuilding).
- "Network change" section: 2020-vs-today comparison computed client-side by
  `computeHistoricalDiff()` — deliberately mirrors `compute_trending()` in
  Python rather than shipping a precomputed diff file, so it always reflects
  whatever `routes.json` currently has without needing a backend regeneration
  step.
- "Weekday vs. weekend" equity section: routes ranked by
  `(trips_saturday + trips_sunday) / 2 / trips_weekday`, purely client-side
  from data already in `routes.json` — no backend change needed for this one.
- Route comparison mirrors the nba-visual "player comparison" pattern — two
  `<select>`s, thin comparison bars per metric. Headway is inverted when sizing
  its bar (lower minutes = more frequent = should read as the "bigger" bar).
- Table is client-side sortable by clicking any header (`sortState`) and
  filterable via the search box (`searchQuery`) — both re-run `renderTable()`.
- Theme toggle cycles system/light/dark via `data-theme` on `<html>`, persisted
  in `localStorage`; stale-data banner fires if `meta.generated_at` is >3 days old.

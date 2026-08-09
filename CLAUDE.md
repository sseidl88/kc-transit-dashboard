# kc-transit-dashboard

GitHub Pages dashboard covering scheduled transit service for two RideKC-branded
agencies: KCATA and RideKC Johnson County Transit (Kansas City metro).

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
Agency GTFS static zips → kc_transit_update.py --agency {kcata,jocounty} (daily cron, once per agency)
                         → JSON/GeoJSON committed to docs/data/ (kcata) and docs/data/jocounty/ (JCT)
                         → GitHub Pages serves docs/ as a static site
                         → docs/index.html fetches both agencies' data and merges client-side
```

- No build step — vanilla JS inline in `docs/index.html`, Leaflet loaded via CDN
  for the map, styles in `docs/style.css`.
- JSON files in the repo are the database — same pattern as the nba-visual project.
- Workflow: `.github/workflows/daily-update.yml` — cron `0 11 * * *` + `workflow_dispatch`,
  runs the pipeline once per agency in `AGENCIES` (currently 2 steps).

---

## Multi-agency (`AGENCIES` in `kc_transit_update.py`)

Two RideKC-branded agencies actually have an open, reachable GTFS feed:

- **kcata** → `docs/data/` (the original, already-live location — kept there for
  backward compatibility with existing fetches/caches, not moved to `docs/data/kcata/`)
- **jocounty** (RideKC Johnson County Transit / "The JO") → `docs/data/jocounty/`,
  feed hosted on `data.trilliumtransit.com` (modern, reachable directly — unlike
  KCATA's legacy host — but still routed through Transitland by default for
  consistency; onestop_id `f-9yum-thejo`)

**Unified Government Transit and IndeBus are not included** — checked every
transit operator Transitland has indexed within 40km of downtown KC
(`GET /api/v2/rest/operators?lat=...&lon=...&radius=40000`) and neither
publishes a discoverable GTFS feed there. Not a bug, just genuinely unavailable
data — same category of finding as the real-time/ridership gaps above. If either
ever publishes one, add it to `AGENCIES` the same way `jocounty` was added.

Each agency gets its own `history.json`/`trending.json` (a real Johnson County
Transit service change and a KCATA one are unrelated events, shouldn't be
compared against each other). `docs/index.html` fetches every agency in
`AGENCIES` (client-side `loadAgencyBundle()`), tags every route/feature with
`agency`/`agency_id`, and merges for the map/table/compare/equity views. Missing
per-agency files (e.g. before `jocounty`'s first Actions run) are caught and
skipped per-agency, not fatal to the whole page.

**The 2020 historical backfill stays KCATA-only, deliberately** — the Wayback
Machine snapshot never covered Johnson County Transit, so `renderHistorical()`
diffs the baseline against `kcataRoutesData` specifically, not the merged
`routesData` — otherwise every JCT route would wrongly show up as "added since
2020."

Route IDs aren't unique across agencies (independent numbering schemes could
coincidentally collide), so anywhere routes from different agencies might sit
in the same list — the compare-tool `<select>`s — the JS uses a composite
`agency_id:route_id` key (`routeUid()`), not bare `route_id`.

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
- `docs/data/stops.geojson` — every weekday-served stop, each flagged
  `"frequent": true/false` (reachable by at least one ≤15-min route or not).
  Powers the frontend's walkshed/density layers and the council-district
  coverage table — deliberately a superset of `hubs.geojson` (which only keeps
  3+-route stops) rather than a separate computation.

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

## Potential future extensions (`docs/data/streetcar_studies.geojson`)

Shows the 3 corridors KCATA/KC Streetcar Authority are actively studying —
**North Kansas City Extension**, **18th & Vine Jazz District Extension**, and
the **East-West Transit Study** (39th St + Linwood Blvd, Rainbow Blvd to Van
Brunt Blvd — mode not confirmed as streetcar; KCATA and KCSA are evaluating
multiple modes). None of the three has a chosen alignment, funding, or
(for East-West) even a confirmed mode. Checked first, same as everywhere
else in this project — Main Street Extension and Riverfront Extension are
**not** in this file because they're already built and already reflected in
the live GTFS feed (that's why STCR's trip count/length jumped between the
2020 baseline and today).

**None of this is GTFS** — these lines don't exist yet, so there's no feed to
pull. `streetcar_studies.geojson` was built **once, manually, offline** (not
by the daily pipeline) and is committed as static reference data, same
treatment as `baseline_2020.json` and `kcmo_council_districts.geojson`:

- **Study-area polygons** (North KC, East-West) use the real bounding
  streets/landmarks each study's own materials describe (e.g. East-West:
  "bounded by Rainbow Blvd west, Van Brunt Blvd east, 31st St north, 43rd St
  south" from ridekc.org/planning/eastwesttransit) — not guessed.
- **Route lines are this dashboard's own illustrative approximation**, not an
  official alignment — none of the three studies has published one in any
  machine-readable (or even map-image) form as of this writing. Built by
  geocoding the streets each study's materials actually name (e.g. East-West's
  Nov 2023 update: "39th Street and Linwood Boulevard, between Rainbow
  Boulevard and Van Brunt Boulevard") via Nominatim (free, no key) and
  connecting the points. 18th & Vine has no published street-level detail at
  all beyond "push the line east into the 18th and Vine corridor," so its line
  is this dashboard's best plausible guess (18th St to Vine St) — flagged as
  such in its card, more so than the other two.
- **Population figures are real Census data, not a ridership forecast** —
  neither study has published one. `CENTLAT`/`CENTLON` block-group centroids
  from the Census Bureau's TIGERweb REST service (free, no key) for Jackson
  Co MO, Clay Co MO, and Wyandotte Co KS, tested against each study-area
  polygon with the same point-in-polygon function as the council-district
  section; population per block group from the Census ACS 2022 5-year API
  (`B01003_001E`), which **does** require a free key — a one-time key, used
  once to build this static file, not stored as a recurring secret (unlike
  `TRANSITLAND_API_KEY`) since population doesn't change day to day. 18th &
  Vine has no published study-area boundary at all (unlike the other two), so
  its population figure uses a 0.5-mile buffer around the illustrative line
  instead of a polygon — noted in its own `population_method` field.
- **North Kansas City's number is a small sample** — only 2 block groups
  matched (the corridor is a narrow, mostly industrial river crossing), so
  treat ~2,058 as rougher than the other two even by this feature's own
  already-loose standards.
- A ring-ordering bug surfaced while building this: `burlington_32nd`'s
  address-based geocode (`"3200 Burlington Street"`) landed south of two
  points that should have bounded it to the north, breaking the polygon into
  a near-degenerate sliver (1 block group instead of a plausible handful).
  Street address numbers don't reliably correspond to KC's numbered-avenue
  cross streets outside the systematic MO-side grid — dropped that point and
  reordered the ring as an actual perimeter walk instead of trusting an
  arbitrary point sequence to self-order correctly.

Frontend renders study areas dashed/violet (`--study-color`, a categorical
slot not used anywhere else on the map) specifically so it can never be
mistaken for real service at a glance, gated behind its own "Show proposed
extensions" checkbox (default off — this is speculative content, shouldn't
be part of the default view). Regenerating this file (if a study publishes a
real alignment, or a new one starts) means rerunning the same manual
geocode → TIGERweb → ACS pipeline by hand; there's no `--rebuild-studies`
flag, this isn't meant to be a repeatable pipeline step.

---

## Testing

`tests/test_pipeline.py` (pytest, run via `.github/workflows/ci.yml` on every
push/PR) covers the time/distance helpers, calendar exception handling, both
bugs above as explicit regression tests, `compute_trending()`'s up/down/added/
discontinued/reconciliation logic, the stops/hubs layers (frequent-flag
correctness, the 3-route hub threshold), `AGENCIES` config sanity, and an
end-to-end `build_dataset()` pass against small synthetic GTFS zips built
in-memory. The frontend's point-in-polygon logic isn't covered here (it's
pure JS with no Python equivalent) — it was validated ad hoc against the real
`stops.geojson`/`kcmo_council_districts.geojson` output via Node before
shipping, not via an automated test.

Separately: the `kc-metro.com` feed host was unreachable from the sandboxed dev
environment during initial build (works fine from GitHub Actions' runners).
`--local-zip PATH` points the script at any GTFS-shaped zip for manual
end-to-end runs outside the test suite.

---

## Frontend (`docs/index.html`)

- Per agency in `AGENCIES`, fetches `routes.json`/`routes.geojson`/`hubs.geojson`/
  `stops.geojson`/`trending.json` (`loadAgencyBundle()`), plus once each:
  `data/baseline_2020.json` and `data/kcmo_council_districts.geojson` — all with
  graceful fallback if a file 404s (e.g. before an agency's first Actions run,
  or if `baseline_2020.json`/the districts file is ever removed).
- Map: Leaflet + OpenStreetMap tiles (no API key), routes colored by frequency
  tier **and weighted by it** (`weightForHeadway()` — thicker line for a
  frequent route, thinner for an infrequent one), popup per route on click,
  hover to highlight (weight +3 over its base, not a fixed value, so the
  hierarchy survives the hover state too). Controls:
  - Route filter (`initRouteFilter()`) — a dropdown checklist grouped by
    agency, with a search box and "All" / "None" / "Streetcar only" quick
    actions, for picking specific routes (e.g. "just the streetcar"). State
    round-trips through `?routes=` (comma-separated `agency_id:route_id`
    keys, only written when not "all" — the common case stays out of the URL).
    `selectedRouteKeys` is `null` for "all" rather than a populated Set, so
    the default path stays cheap.
  - "Frequent network only" — rebuilds the route layer filtered to
    `headway_minutes <= 15` (`FREQUENT_MAX_HEADWAY`); state syncs to `?freq=1`.
    Composes with the route filter (both apply as AND in `buildRouteLayer()`'s
    `filter`), not a separate mechanism.
  - A single "Overlay" `<select>` — Transfer hubs / Frequent-network walkshed
    (¼-mile `L.circle` per `stops.geojson` feature flagged `frequent` —
    deliberately not a real street-network isochrone or a GIS-library
    buffer/intersection, just Leaflet circles, enough to make coverage gaps
    visually obvious without adding a geometry dependency like shapely to the
    pipeline) / Stop density (every stop as a small low-opacity dot) / None
    (default). Was three independent checkboxes that could all be on
    simultaneously alongside the route lines — collapsed to one mutually
    exclusive picker specifically because that stacking was what made the map
    busy, not any single layer on its own.
- "Frequent-network access by council district" section: pure client-side
  point-in-polygon (ray casting, `pointInRing()`/`pointInGeometry()`) against
  `data/kcmo_council_districts.geojson` (Kansas City, MO's 6 council districts,
  downloaded once from Open Data KC's Socrata API — `data.kcmo.org/resource/
  5qar-bf4m.geojson` — and committed as static reference data, same treatment
  as `baseline_2020.json`; regenerate only if KCMO redistricts). No GIS library;
  ignores interior rings (holes), an acceptable simplification since none of
  KCMO's 6 districts are donut-shaped. Johnson County Transit's stops mostly
  fall outside every KCMO district and are naturally excluded by that geometry
  test alone — no explicit agency filter needed. Table hides itself
  (`display:none` by default) if nothing matches, so an empty result reads as
  "no data" rather than a broken section.
- "Network change" section: 2020-vs-today comparison computed client-side by
  `computeHistoricalDiff()` — deliberately mirrors `compute_trending()` in
  Python rather than shipping a precomputed diff file, so it always reflects
  whatever `routes.json` currently has without needing a backend regeneration
  step. **KCATA-only** — see "Multi-agency" above for why.
- "Weekday vs. weekend" equity section: routes ranked by
  `(trips_saturday + trips_sunday) / 2 / trips_weekday`, purely client-side
  from data already in `routes.json` (merged across both agencies — a genuine
  regional equity view, unlike the KCATA-only historical section).
- Route comparison mirrors the nba-visual "player comparison" pattern — two
  `<select>`s, thin comparison bars per metric. Headway is inverted when sizing
  its bar (lower minutes = more frequent = should read as the "bigger" bar).
  Selection state round-trips through `?routeA=`/`?routeB=` (composite
  `agency_id:route_id` values, `routeUid()`) via `setUrlParam()` — same
  shareable-link pattern nba-visual uses for `?league=&team=`.
- Table is client-side sortable by clicking any header (`sortState`) and
  filterable via the search box (`searchQuery`) — both re-run `renderTable()`.
- Theme toggle cycles system/light/dark via `data-theme` on `<html>`, persisted
  in `localStorage`; stale-data banner fires if any agency's `generated_at` is
  >3 days old.

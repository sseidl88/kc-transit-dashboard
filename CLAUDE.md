# kc-transit-dashboard

GitHub Pages dashboard covering scheduled transit service for two RideKC-branded
agencies: KCATA and RideKC Johnson County Transit (Kansas City metro).

**Repo:** sseidl88/kc-transit-dashboard
**Live site:** GitHub Pages serving from `docs/` on `main` branch

---

## Key rules

- **Scope is scheduled service only for every metric except the live streetcar
  tracker — not ridership, not official on-time performance.** KCATA doesn't
  publish those as an open API:
  - GTFS-Realtime (vehicle positions, trip updates) exists via Swiftly
    (`api.goswift.ly/real-time/kcata/...`) and returns `401 Unauthorized`
    hit directly — but **Transitland already holds an authorized key** and
    re-serves it through a cached REST pass-through, confirmed working with
    the existing `TRANSITLAND_API_KEY`. This became the live streetcar
    tracker, kept as its own additive module (`kc_streetcar_realtime.py`,
    `streetcar-live-section` — see its own section below) rather than
    retrofitted into the scheduled-service metrics elsewhere on this page,
    per the plan when this was still hypothetical.
  - Ridership and on-time-performance figures are published as monthly PDF
    "Key Performance Indicator" reports at `ridekc.org/planning/dashboard` —
    system-wide aggregates, not a per-route daily API, and not worth scraping
    for a "daily" dashboard since they update monthly. The live tracker's own
    delay estimate is explicitly not this — see its section for the distinction.
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

KCATA real-time feed (via Transitland pass-through) → kc_streetcar_realtime.py (~every 15 min cron)
                         → streetcar_live.json / streetcar_delays.json committed to docs/data/
                         → docs/index.html polls those every 60s — see "Live streetcar tracker" below
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
- `route_length_miles` — haversine sum along the route's most-common `shape_id`,
  computed from the **full-precision** shape points, before any simplification.
- `docs/data/routes.geojson` line geometry is simplified (`simplify_polyline()`,
  Douglas-Peucker, ~100ft/0.02mi tolerance, no shapely dependency — same
  "no GIS library" pattern as the rest of the pipeline) before being written
  out. GTFS shapes.txt typically samples a point every 10-30 feet, which is
  far more detail than a metro-wide map needs and was a real contributor to
  the map feeling busy with ~39 routes drawn at once — cuts a feed's ~44,500
  raw shape points down to roughly 800 rendered points across all routes.
  `route_length_miles` is computed *before* simplifying, so this is purely a
  rendering optimization, not a change to any reported statistic.
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
  Boulevard and Van Brunt Boulevard") via Nominatim (free, no key), then
  **snapped to real streets via OSRM** (`scripts` in the note below) instead
  of left as straight segments between the geocoded points — same router the
  design tool uses, so the whole page is visually consistent.
  - **East-West's line runs 39th St east to Main St, then north on Main to
    Linwood Blvd, then east on Linwood** — not a straight run along 39th the
    whole way. Main St is where this illustrative line meets the existing
    KC Streetcar alignment, so routing the connection through Main (rather
    than jogging east on 39th to Troost) reflects how a real extension would
    actually tie into the built network. Finding the Main St/Linwood Blvd
    waypoint took an extra step: Nominatim's `&`-style intersection queries
    return empty for this pair, and house-number-style fallback queries on
    each street landed at inconsistent points rather than the real crossing.
    Resolved instead by pulling both streets' full way geometry from the
    **Overpass API** (`overpass-api.de`, free, no key) and finding their
    literal shared OSM node — exact by construction, no geocoding-text
    guesswork. (Also had to search under "Linwood Boulevard" *and* "East
    Linwood Boulevard" — OSM splits the street into direction-prefixed way
    names, so a name-exact query for just "Linwood Boulevard" alone missed
    the segment that actually crosses Main St.)
  - **18th & Vine's line ends at The Paseo** — 18th St east from the existing
    line to The Paseo, its only published terminus detail. Shorter than a
    line pushed all the way to Vine St/Prospect Ave (which was this
    dashboard's earlier, unpublished-detail guess before the terminus was
    confirmed) — The Paseo sits west of Vine St, so correcting the endpoint
    shortened the line, not lengthened it.
- **Snapping gotcha**: North KC's route originally used the ASB Bridge as its
  river-crossing waypoint (it's the real study's own stated west boundary),
  but that bridge is rail/pedestrian-only — not part of OSRM's drivable
  network. Feeding it in as a waypoint made OSRM detour badly (zigzagging
  north/south, 3.02mi → 7.29mi, a fake-looking mess, not "cleaner"). Fixed by
  swapping in the Heart of America Bridge (a real drivable crossing a few
  hundred feet away) — re-snapped cleanly to 4.99mi with zero direction
  reversals. General lesson: a waypoint being real and well-sourced doesn't
  guarantee it's *drivable* — sanity-check the snapped result (point count,
  total length, whether the path backtracks on itself) before trusting it,
  same as checking any other geocode. `historic_streetcar_1952.geojson`'s
  route 7 (Armourdale–Troost Ave., which crosses the Kansas/Missouri state
  line) also came back with a much longer snap (5.77mi → 8.46mi) since a
  real driving path has to go via an actual bridge instead of a straight
  line across the river — kept as-is since it came back with zero direction
  reversals, the same "is it a real path or a fake mess" check used
  throughout this section.
- Both files were snapped with a **one-time Python script** (not part of the
  daily pipeline — these are static reference data, same as everywhere else
  in this section) that calls the same public OSRM endpoint the frontend
  design tool uses, `overview=full` for maximum path detail since this runs
  offline once rather than live in a user's browser.
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

## Transit-need priority analysis (`docs/data/transit_need.json`)

The dashboard's thesis section: bus fares returned June 1, 2026 after ~6
years free (RideKC/KCATA — see their news release), so **where the service
actually reaches now carries more weight than it did when riding was free**.
This answers "where should KC invest next?" with an explainable rule, not an
opaque score: a block group is **priority** if it's both above the
three-county median share of car-free households (ACS `B25044`, owner +
renter occupied units reporting zero vehicles available ÷ total occupied
units) **and** more than 0.5 miles from the nearest stop on a ≤15-min route
(haversine distance to `stops.geojson`/`jocounty/stops.geojson` points
flagged `frequent`, both agencies). Same one-time-key Census pipeline as the
study corridors — no new secret, no daily automation.

**The payoff comparison**: for each of the 3 corridors in
`streetcar_studies.geojson`, what share of *its own* block groups are
priority zones, versus the citywide rate (41%). The real result, computed
honestly rather than steered toward a conclusion: **East-West (23%) and
18th & Vine (20%) are below the citywide priority rate; North Kansas City
(100%, but only 2 block groups — a small-sample caveat repeated from the
studies section) is above it.** (18th & Vine's rate moved from an earlier
14%/7-block-group figure to 20%/5 block groups after its illustrative line's
terminus was corrected to The Paseo — a shorter line, fewer block groups in
its 0.5-mile buffer, same below-citywide conclusion either way.) Two of three
currently-studied corridors
don't rank as especially high-need by this metric — stated plainly in the
section copy alongside the honest caveat that ridership, cost, economic
development, and connectivity are all legitimate reasons a corridor gets
studied that this metric doesn't capture. The point isn't "KCATA is wrong,"
it's "here's one more input worth weighing."

**A real bug caught before shipping this**: the first version of the
corridor comparison came back as *zero* priority block groups inside either
study area, which was suspicious enough to check by hand — turned out to be
a lat/lon tuple-order swap when re-reading `streetcar_studies.geojson`'s
rings back out of GeoJSON's `[lon, lat]` order (`(lat, lon) for lon, lat in
coordinates` instead of `(lon, lat) for lon, lat in coordinates`), which
silently fed every ring point into `point_in_ring()` on the wrong axis.
Fixed by keeping the ring in GeoJSON's own `[lon, lat]` order throughout,
matching the convention `build_studies.py` and the frontend's own
`pointInGeometry()` already use — same lesson as the North KC ring-ordering
bug one section up: verify a surprising geographic result against a second,
independent check before trusting it.

**No new map layer** — deliberately. This ships as stat tiles + two ranked
tables in `docs/index.html` (`renderNeedAnalysis()`), not another Leaflet
overlay, specifically because the map had just been decluttered (route
filter + collapsing three overlay checkboxes into one `<select>`) and
piling a fifth layer back on would undo that.

Like the study corridors, this is a **one-time manual build**, not a daily
pipeline step — rerun the geocode → TIGERweb → ACS → reverse-geocode chain
by hand if the underlying GTFS or study corridors change enough to warrant it.

---

## Cost estimates and the "design your own extension" tool

**Cost benchmark**: none of the 3 studies has published a cost estimate (North
KC's own materials only say "likely multi-hundred-million-dollar"), so costs
here are illustrative length × **$87–100M/mile** — KC's own two most recent
streetcar extension costs (Riverfront $61.1M / 0.7mi ≈ $87M/mi; Main Street
$351.71M / ~3.5mi ≈ $100M/mi), not a generic national average. Chose these
over national figures (Tampa ~$59M/mi, Santa Ana >$150M/mi) specifically
because same-agency/same-city/same-era costs account for local labor,
utility relocation, and procurement conditions that a generic figure can't.
Baked into `streetcar_studies.geojson` as `cost_estimate_low/high_millions` —
North KC's `cost_method` carries an extra caveat that its estimate is
probably a floor, since neither KC benchmark involved a river crossing and
this one does (bridge/utility work costs more).

**The design tool** (`docs/index.html`, `initDesignTool()` and friends): click
points on the existing route map (no second Leaflet instance — reuses
`leafletMap` via `onMapClickForDesign`, gated behind a `designMode` flag so
normal map interaction is unaffected when it's off) to sketch a hypothetical
route. `design-section` sits immediately below `map-section` in the page
(moved there from further down, past the trip-time and live-tracker
sections) specifically because its copy says "click the map above" — putting
something else's own map in between made that instruction ambiguous. Each click re-requests the full point sequence from
**OSRM's free public router** (`router.project-osrm.org`, no key, `Access-
Control-Allow-Origin: *` confirmed before building around it) with
`overview=simplified` — deliberately not `full`: a Douglas-Peucker-simplified
polyline is still street-accurate but cuts the point count from
hundreds/thousands to tens, which matters because the population buffer
check below does a distance test against every returned point for all ~974
block groups. Length, cost, and population/car-free-household figures reuse
the exact same math and $/mile benchmark as the study corridors above, just
computed live in the browser against a pre-baked static file
(`docs/data/block_group_population.json` — the same 974 block-group
population + no-vehicle-household dataset built for the need analysis,
trimmed to just `lat`/`lon`/`population`/`no_vehicle_households`) since a
static GitHub Pages site can't make authenticated live Census calls without
exposing a key client-side. OSRM's `driving` profile is a proxy, not a
transit router — the section copy says so, since it may occasionally route
via a highway segment a real streetcar alignment never would.

**Five upgrades to the design tool, all client-side, no new data pipeline:**
- **Comparison table** (`renderDesignComparison()`) — your design's length/cost/
  population next to the 3 real study corridors, plus a cost-per-person figure
  for both, with the section copy explicit that lower cost-per-person isn't
  automatically "better" (doesn't account for existing coverage or who
  specifically lives there).
- **Street names** — OSRM's route request now passes `steps=true`; consecutive
  duplicate step names are collapsed into a "Follows: X → Y → Z" line.
- **Bridge/river-crossing warning** — proximity (0.2mi) to `KNOWN_RIVER_CROSSINGS`,
  4 real Missouri River bridges near downtown KC, individually geocoded via
  Nominatim. **Not** a name-contains-"bridge" check on OSRM's step names — tried
  that first, and it false-negatived on an actual test crossing (Broadway
  Boulevard's bridge segment is named "Broadway Boulevard" in OSM, not
  "Broadway Bridge"; the `bridge=yes` tag isn't surfaced in route step names at
  all). Verified against a real crossing before shipping, same as everywhere
  else in this project.
- **New vs. redundant coverage** — each snapped segment's midpoint is tested
  against `designFrequentStopCoords` (both agencies' `frequent`-flagged stops)
  at the same 0.25mi radius as the frequent-network walkshed elsewhere;
  segment length is summed into "new" or "redundant" accordingly. Verified
  against a real test line drawn along Main St (where the streetcar already
  runs) — correctly comes back 100% redundant, 0% new.
- **Shareable link** — `designPoints` round-trips through `?design=` (`lat,lon`
  pairs, `;`-joined, 5 decimal places), same `setUrlParam()` pattern as every
  other shareable state on this page. A "Copy link" button writes
  `location.href` to the clipboard. Loading a `?design=` URL renders the
  route and stats immediately without requiring the user to click "Start
  designing" first.

---

## Historic streetcar network (`docs/data/historic_streetcar_1952.geojson`)

16 streetcar lines digitized from a user-provided scan of an **April 1952
Kansas City Public Service Co. system map** — the actual original KC
streetcar network, well before it was dismantled. Not GTFS, not a study:
a one-time, by-hand digitization, same treatment as `baseline_2020.json`
and `streetcar_studies.geojson`.

**Replaces an earlier October 1948 digitization** (`historic_streetcar_1948.geojson`,
10 routes, numbered 50s–60s — deleted, not kept alongside this one). The
1948 map's route names were the only routing information it gave beyond
downtown, so most of its routes were "medium" confidence at best. The 1952
map uses a completely different numbering scheme (Street Car Lines 1–17)
and — critically — has its own "STREET CAR LINES" panel giving explicit
street-by-street routing (a "VIA ..." street list) for every numbered line,
which is what actually made a higher-confidence, 16-line replacement
possible rather than just a rescan of the same 10 routes.

**Downtown intersections were pinned via the Overpass API** (`overpass-api.de`,
free, no key) rather than Nominatim geocoding — the same technique the
East-West corridor's Main St/Linwood Blvd waypoint above uses: pull the full
OSM way geometry for each named street and find the literal shared node (or,
where streets don't actually cross today, the closest-approach point,
flagged with the gap distance). This is categorically more reliable than
free-text geocoding for a street-by-street routing list like this map
provides — most of the ~19 downtown intersections needed came back as exact
shared nodes. Named places beyond downtown with no cross-street given
(Armourdale, Muncie, Fairmount) were still geocoded via Nominatim, same as
before.

**A real inconsistency found in the source map itself, not just this
dashboard's reading of it**: the map's own quick-reference index (top
right) and its detailed "STREET CAR LINES" routing panel (center) disagree
with each other for routes 9 and 10 — the index calls them "Broadway–64th
St." and "Broadway–65th St.–Woodland Ave." respectively, but the detail
panel's own via-street text for those same two numbers describes "55th St.
& Woodland Ave." and "North Troost Ave.–64th St." instead (i.e., the
endpoints the index assigns to 9 and 10 line up with what the detail panel
calls 10 and 9). Caught by cross-checking the two panels against each other
after transcribing them independently twice and getting the same mismatch
both times — not a transcription slip on this dashboard's part. Resolved by
using the detail panel (the more specific, operationally meaningful source)
as authoritative for the actual routing, both routes marked `low`
confidence, and the discrepancy stated plainly in each route's `note` field
rather than silently picking one panel's numbering as "correct."

**Route 17 ("N. Kansas City Line") was dropped entirely** — the map gives
it no via-street detail at all, and no name distinct from route 16 ("North
Kansas City Line"). Building a second geometry for it would mean fabricating
a route with zero actual basis in the source; same "don't guess, say so"
standard already applied elsewhere in this project (e.g. not inventing a
feed for Unified Government Transit or IndeBus).

**Confidence is explicit and varies per route**, stored as
`properties.confidence` (`high`/`medium`/`low`) and reflected in the map's
dash pattern (`HISTORIC_CONFIDENCE_DASH`):
- **High** (route 8, The Paseo–55th St. & Woodland Ave.): via-streets and
  terminus agree between the map's index and detail panel, and every
  intersection needed came back as an exact OSM node.
- **Medium** (most routes): via-streets are clear from the detail panel,
  but either the far terminus is a named place without a specific
  cross-street (Muncie, Fairmount, Armourdale), or a couple of the via
  streets listed don't literally cross on the modern street grid (State
  Line Rd. and Independence Ave., for route 14) and were reconciled as
  separate segments the route touches rather than a literal turn-by-turn
  sequence.
- **Low** (routes 9, 10, 15): 9 and 10 for the index/detail-panel mismatch
  described above; 15 because its named terminus ("So. Zone Point") was a
  historic fare-zone boundary term, not a place that exists on any modern
  map — its location couldn't be identified, so the line shown stops short
  of the route's actual endpoint.

Map styling: sepia/brown (`#8b5a2b`), never confused with the frequency-tier
blue, the "under study" violet, or the user-design magenta. Gated behind its
own "Show 1952 streetcar network" toggle, default off — same reasoning as
every other opt-in overlay on this map.

**All 16 routes were snapped to real streets via OSRM** (same one-time
script and endpoint used for the study corridors above), replacing the
original straight-segment-between-waypoints lines — the whole point being
that dashed straight lines between a handful of waypoints looked obviously
synthetic next to the real route shapes. Sanity-checked the same way as the
North KC bridge fix (point count, length change, zero direction reversals)
before trusting the result — all 16 came back clean, no zigzagging.

---

## How would this actually get paid for (`funding-section`)

Pure content, no new data pipeline. Real, sourced facts about how the two
*built* extensions were funded: **Section 5309 "New Starts"** (federal,
~49.5% of Main Street's $351.71M — competitive, not a guarantee, and a
project needs ridership projections + environmental review + engineering
work done just to apply) and the **Main Street Rail TDD** (local — a
voter-approved special taxing district ⅓ mile either side of the route,
property tax surcharge + 1% sales tax, which only pencils out because that
corridor is "the most densely developed and highly valued property in the
city"). Deliberately not a funding *plan* for the 3 proposed corridors —
none has a confirmed funding source. The one real, sourced detail that ties
this back to the need-priority section: KC's own East-West Transit Study
says a TDD there likely wouldn't raise enough revenue (less commercially
developed corridor), and that's also the corridor ranking below the
citywide priority rate — flagged as a plausible shared cause, explicitly
**not** claimed as proven causation.

---

## TOD opportunity zones (`docs/data/tod_opportunity.geojson`)

A parcel counts as a TOD opportunity zone if it's zoned **single-family-only**
under KC's own zoning code *and* sits within ¼ mile of a frequent (≤15 min)
stop — real parcel geometry from Open Data KC (`data.kcmo.org`, dataset
`mreg-j9sj`, 2,619 zoning polygons citywide, fetched once via
`.geojson?$limit=3000` — zoning changes rarely enough this isn't a daily
pipeline step). "Single-family-only" = KC's own `R-80`/`R-20`/`R-10`/`R-7.5`/
`R-6`/`AG-R` codes — per kcmo.gov's own zoning code, the number suffix is
roughly the minimum lot size in thousands of sqft, so a *bigger* number means
a *bigger* required lot and lower allowed density; `R-5` and smaller permit
duplexes/multifamily and aren't flagged. Combined classifications like
`R-2.5/ICO` are matched on the base code before the `/`.

**The honest result: only 7 parcels (320 acres) qualify**, out of 445
exclusionary-zoned parcels (123,177 acres) citywide — sanity-checked by
reverse-geocoding a few flagged parcels (real KC neighborhoods: Munsell
Acres, Blue Hills, Independence Plaza, not an error) and by comparing
against a looser bounding-box count before trusting the number. Framed in
the UI as a small, specific finding, not a sweeping one — most of KC's
low-density zoning simply isn't near the frequent network at all, which is
its own kind of answer. The section copy also flags that this is zoning
*classification*, not current land use — a flagged parcel could already be
a park, church, or school rather than redevelopable land; a real TOD
analysis would need parcel-level land-use data this dataset doesn't have.
Uses aqua (`#1baf7a`) on the map — the one remaining unused categorical
slot (blue=routes, violet=studies, magenta=user design, sepia=historic).

---

## 1952-vs-today comparison slider

A draggable divider over the map, not another checkbox — `enterSliderMode()`/
`exitSliderMode()` in `docs/index.html`. Leaflet has no built-in way to split
a single vector layer's rendering by an arbitrary line, and the map's
`preferCanvas:true` setting means most layers already share one canvas
element, which can't be CSS-clipped per-layer. Worked around it with two
**dedicated Leaflet panes** (`sliderTodayPane`/`sliderHistoricPane`), each
with its own `L.canvas()` renderer — separate DOM elements, so each can get
its own `clip-path: inset(...)` driven by the divider's drag position.
Entering slider mode clears every other map layer (route lines, studies,
historic, whichever overlay was selected) so the two comparison layers don't
compete with a 5th thing on screen; exiting rebuilds them by re-reading the
current state of the normal controls (checkboxes, overlay select) rather
than trying to remember what was on before. The "today" side is drawn as a
single flat color, not the usual frequency-tier ramp — this comparison is
about *where* lines exist, not how frequent they are, and reusing the
multi-color ramp here would fight the slider's own left/right visual split.

**A real bug hit after shipping this**: the slider divider dragged fine but
the clip never visibly took effect — both layers stayed fully visible
regardless of divider position. Root cause: Leaflet panes are
`position:absolute` with no explicit size of their own (their canvas child is
also absolutely positioned, for panning), so a freshly created pane can have
an effective 0×0 box. `clip-path: inset()` percentages resolve against the
*clipped element's own* box, so every inset on a 0×0 pane resolves to 0
regardless of the percentage requested — clipping silently no-ops. Switching
to `width:100%; height:100%` isn't a safe fix either: the pane's own
containing block (Leaflet's internal `_mapPane`) likely has no defined size
either, so percentage sizing is just as unreliable one level up. Fixed with
`applySliderPaneSize()` — sets each slider pane's width/height to
**explicit pixel values** from `leafletMap.getSize()`, called once on
entering slider mode and re-applied on window `resize` while it's active
(cleaned up in `exitSliderMode()`). General lesson, same shape as the
North KC bridge-snap bug above: a CSS mechanism that depends on an element's
own box (percentage sizing, percentage clip-path) needs that box to actually
be defined by something — don't assume a `position:absolute` element has a
size just because it has content.

---

## Trip time: transit vs. driving

**`avg_speed_mph`** (`kc_transit_update.py`) is a new per-route field:
median one-way trip duration (first-to-last stop, dominant direction only,
same restriction as `headway_minutes`) converted to hours, divided into
`route_length_miles`. Median, not mean, for the same outlier-resistance
reason `headway_minutes` already uses it. Covered by
`test_build_dataset_computes_expected_metrics`, which checks the fixture's
known 5-minute stop-to-stop trip time against the computed speed.

**The tool itself is deliberately single-route-only.** Modeling transfers
well needs a real trip-planning graph search with transfer penalties —
out of scope for what's meant to be a napkin-math comparison, not a trip
planner. `findBestDirectTransitOption()` walks every route's geometry,
finds the nearest point on each to both the clicked origin and destination
(`nearestPointOnLine()`, tracking cumulative arc-length so the "distance
along the route between two points" is a simple subtraction), keeps only
routes where *both* points are within a half-mile, and estimates
`walk-to + wait (half the headway) + ride (arc distance / avg_speed_mph) +
walk-from`. When nothing qualifies, it says so explicitly rather than
guessing at a transfer. Driving time is OSRM's own `duration` field from the
same free public router already used elsewhere (`overview=false` here —
this call only needs the total duration, not the geometry, so skipping
geometry keeps the response small). Mutually exclusive with the design
tool via each toggle's click handler calling the other's — both hijack map
clicks, so only one can be active at a time.

Verified end-to-end against real data before shipping: fed the tool a
route's own first/last shape points as origin/destination (guaranteed to
land exactly on that route), and confirmed the output's ride time matched
`route_length_miles / avg_speed_mph` by hand.

---

## Live streetcar tracker (`kc_streetcar_realtime.py`, `streetcar-live-section`)

**Real-time data turned out to be available after all** — the "Key rules"
section above's real-time gap (401 from `api.goswift.ly` without a bearer
token) is only true of hitting Swiftly *directly*. Transitland already holds
an authorized key for KCATA's real-time feed and re-serves it through its own
cached REST pass-through
(`transit.land/api/v2/rest/feeds/f-kcata~rt/download_latest_rt/{type}.json`),
fetched and cached once per minute. Confirmed working with the project's
existing `TRANSITLAND_API_KEY` — no new secret, no agency contact needed.
Scoped to the KC Streetcar specifically (route_id `601` in KCATA's feed, GTFS
route_type `0`), not the full bus fleet, per what was actually asked for.

**Why this can't just run in the browser**: GitHub Pages is static-only, and
embedding the API key in client-side JS would expose it to anyone who views
source. So `kc_streetcar_realtime.py` runs server-side via a **separate**
GitHub Actions workflow (`.github/workflows/streetcar-realtime.yml`,
`workflow_dispatch` + a schedule — deliberately not folded into
`daily-update.yml`, since this is a completely different cadence and kind of
data) and writes small static JSON snapshots into `docs/data/`, which the
frontend polls every 60s. Same "JSON files are the database" pattern as
everything else in this project. This means "live" here means *as fresh as
the last completed run*, not sub-second — stated plainly in the section
copy, not oversold.

**The cron interval doesn't mean what it says, and the copy/threshold now
account for that.** Originally shipped as `*/5 * * * *` (5-minute cadence,
chosen with the user as a freshness/commit-volume tradeoff — ~288 commits/
day). After a week live, checking real run timestamps showed GitHub wasn't
honoring that at all: consecutive successful runs landed 25–45+ minutes
apart, never close to 5. This is a known GitHub Actions limitation, not a
bug in the workflow — GitHub doesn't guarantee scheduled-workflow timing
under roughly 15 minutes, especially on lower-traffic public repos, and
silently spaces runs out further rather than erroring. Fixed by being
honest about it in three places instead of chasing a precision GitHub won't
actually deliver: the cron is now `*/15 * * * *` (a granularity GitHub
tends to respect more consistently, though still not exactly), the
section copy says "targeting every 15 minutes" rather than promising a
number, and the frontend's staleness warning threshold moved from 20 to 90
minutes so normal scheduler jitter doesn't trip a false "this looks
broken" message — 90 min comfortably clears the 25–45 min gaps actually
observed while still catching a genuinely stopped workflow (which shows
up as many hours, not tens of minutes). Public repos get unlimited GitHub
Actions minutes either way, so none of this was ever a cost concern —
purely a scheduling-reliability one.

**`docs/data/streetcar_live.json`** — current position, speed, bearing,
current status (`STOPPED_AT`/`IN_TRANSIT_TO`), current stop, and live
occupancy (status + percentage) for every streetcar vehicle, overwritten
each run. Occupancy wasn't asked for — it's just part of the same feed
payload Swiftly already provides, so it's shown for free rather than
discarded. Rendered on a small dedicated Leaflet map (its own instance, not
layered onto the main map, per the same "each tool gets its own section"
pattern the design tool and trip-time tool already use) with a pulsing
green `--live-color` marker style — green chosen specifically because it's
distinct from every other categorical color already on this page (frequency
blue, study violet, design magenta, historic sepia, TOD aqua, hub/walkshed
gold, stop density red).

**Vehicle markers are a small inline-SVG streetcar silhouette**
(`streetcarDivIcon()`), not a plain dot — a rounded body plus a triangular
nose pointing "up" by default, rotated per-vehicle by the feed's own
`bearing` field (already present in the payload, previously unused) via a
CSS `transform: rotate()` on an inner wrapper div. The rotation has to land
on an *inner* div, not the outer div Leaflet itself positions the marker
with — rotating Leaflet's own positioning element would break placement,
not just orientation. `L.divIcon` inherits Leaflet's default white
background/border styling meant for icon-image markers, which would show as
a visible box around the SVG's transparent areas — reset with `.streetcar-
live-icon { background: transparent !important; border: none !important; }`
in `style.css`. A `null` bearing (not every GTFS-RT ping includes one) falls
back to pointing north rather than omitting the icon.

**`docs/data/streetcar_schedule.json`** — a small addition to the *daily*
pipeline (`kc_transit_update.py`, gated behind `streetcar_route_id` /
`STREETCAR_ROUTE_ID = "601"`, only passed for the `kcata` agency): for every
streetcar trip running **today** (not the representative weekday date used
for the rest of the metrics — see the bug below), the full stop-by-stop
schedule (`stop_sequence`, `stop_id`, `scheduled_seconds`). This is what lets
the live tracker compute delay without re-parsing the whole GTFS zip every
5 minutes.

**A real, load-bearing bug caught before this shipped**: the daily pipeline's
`pick_representative_dates()` deliberately anchors the regular weekday/
Saturday/Sunday metrics to the *nearest* date of each type (e.g. the nearest
upcoming Wednesday) rather than literally today — correct for those, since
it keeps metrics stable and comparable regardless of what day the pipeline
happens to run on. That's the wrong anchor for the streetcar schedule,
though: real-time GTFS-RT trip_ids are tied to *today's* actual service_id.
First version of `streetcar_schedule.json` used the representative
Wednesday's trips, and testing against the real live feed on an actual
Friday showed **zero overlap at all** between the schedule's trip_id range
(391333–391806) and the live feed's active trip_ids (391147–391271) — not a
near-miss, a completely different range, meaning delay computation could
never find a matching trip no matter how long it ran. Fixed by giving the
streetcar schedule its own, separately-anchored `active_service_ids(...,
today)` lookup, independent of the representative-date selection used
everywhere else in `build_dataset()`. Covered by
`test_streetcar_schedule_uses_todays_date_not_representative_weekday`, a
fixture with two same-route trips on two different service patterns
(Wednesday-only vs. Friday-only) specifically built to catch a regression
back to the representative-date anchor.

**`docs/data/streetcar_delays.json`** — a rolling log of *observed* schedule
adherence, appended to each run and trimmed to the last 3 days (the frontend
only ever shows today; the buffer just avoids an empty view right after
midnight). Deliberately built from actual observed stop arrivals — a vehicle
position entity showing `currentStatus == STOPPED_AT` at a stop, compared
against that trip's scheduled time — not from GTFS-RT's `trip_updates`
arrival *predictions* for stops not yet reached, which shift run to run and
would make for a moving, not-quite-honest "how did it do" record. Deduped by
`(service_date, trip_id, stop_id)` so the same stop isn't recorded twice
across consecutive polls.

This is genuinely a **sample**, not a complete record, and the section copy
says so plainly: a streetcar usually dwells at a stop for well under a
minute or two, so most stops on most trips are never caught mid-dwell by a
poll that (realistically) lands every 15-45 minutes. And there's no way to
know *why* a trip was early or late —
GTFS-RT doesn't carry a reason code, so the tracker doesn't pretend to.
This is this dashboard's own estimate against the *published static*
schedule, explicitly **not** official KCATA on-time performance (which is
only available as monthly system-wide PDF aggregates, same distinction
"Key rules" draws at the top of this file).

Delay severity in the table reuses the dataviz palette's existing
`--good`/`--warning`/`--serious`/`--critical` tokens (defined in
`style.css` but, until this feature, not actually applied anywhere else on
the page) — within 2 min = good, 2–5 = warning, 5–10 = serious, 10+ =
critical.

**Direction labels verified against real data, not assumed**: `direction_id`
0 vs. 1 isn't self-explanatory from the feed alone, so before hardcoding
"River Market-bound" / "Union Station-bound" labels, the actual relationship
was checked against a live snapshot — direction 0's `stop_sequence`
increases together with latitude (Union Station-area start, River Market-area
end), direction 1 is the reverse. Got this backwards on the first pass
(labels initially swapped) and caught it by checking the real data before
shipping, same standard applied to every other surprising or unverified
claim in this project.

**Delay trends** (below the daily table, same section): aggregates *all*
retained history, not just today, into a by-direction comparison and two
hand-rolled diverging SVG bar charts (by hour of day, by day) — no charting
library, same zero-dependency approach as the rest of the page. This is
what actually required bumping `DELAY_RETENTION_DAYS` from 3 to 180 in
`kc_streetcar_realtime.py`: the original 3-day trim was silently deleting
the exact history this view needs before it could ever accumulate. Bar
*position* (above/below a zero baseline) encodes early-vs-late; bar *color*
reuses the same good/warning/serious/critical severity classes as the daily
table rather than introducing a second, competing color language for the
same kind of data on the same page — position carries sign, color carries
"how bad." Hover tooltips are native SVG `<title>` elements (zero extra
JS/CSS), matching the project's general preference for the simplest thing
that actually works over building custom UI machinery for a secondary view.
Hour-of-day is parsed directly out of the `observed_time_local` ISO string's
own characters (`.slice(11, 13)`), not via `new Date(...).getHours()` —
the latter reads in the *viewer's* local timezone, which would silently
mislabel every bar for anyone checking this page from outside Central time,
same class of bug as the `chicagoTodayServiceDate()` timezone handling
above. With only a few days of real data so far, both charts and the note
above them say so plainly (a growing dataset that sharpens over time) rather
than presenting a 4-point chart as if it were a settled pattern.

---

## Testing

`tests/test_pipeline.py` (pytest, run via `.github/workflows/ci.yml` on every
push/PR) covers the time/distance helpers, calendar exception handling, both
bugs above as explicit regression tests, `compute_trending()`'s up/down/added/
discontinued/reconciliation logic, the stops/hubs layers (frequent-flag
correctness, the 3-route hub threshold), `simplify_polyline()` (collapses
collinear points, keeps real corners, leaves short lines untouched),
`avg_speed_mph` (checked against the fixture's known stop-to-stop trip
time), `AGENCIES` config sanity, `streetcar_schedule.json` extraction
(including the representative-date regression test described above), and an
end-to-end `build_dataset()` pass against small synthetic GTFS zips built
in-memory. The frontend's point-in-polygon logic and the live tracker's
render functions (`renderStreetcarLiveMap`, `renderStreetcarDelayStats`,
etc.) aren't covered here (pure JS, no Python equivalent) — validated ad hoc
against real captured data via a small mock-DOM Node harness before shipping,
not via an automated test. `kc_streetcar_realtime.py` itself was run
end-to-end locally against the real live feed (using a user-supplied
Transitland key) before shipping, including verifying delay computation
actually produces sane, correctly-signed values against real vehicle data.

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
  hierarchy survives the hover state too). Base line opacity is 0.6 (was
  0.85) — with up to ~39 routes drawn simultaneously, full-strength lines at
  rest were a real source of busyness; hover still jumps to opacity 1, so
  the contrast on interaction actually got sharper, not weaker. Route
  geometry is also simplified server-side before it ever reaches the
  frontend — see `simplify_polyline()` in `kc_transit_update.py` below.
  Controls:
  - Route filter (`initRouteFilter()`) — a dropdown checklist grouped by
    agency, with a search box and "All" / "None" / "Streetcar only" quick
    actions, for picking specific routes (e.g. "just the streetcar"). State
    round-trips through `?routes=` (comma-separated `agency_id:route_id`
    keys, only written when not "all" — the common case stays out of the URL).
    `selectedRouteKeys` is `null` for "all" rather than a populated Set, so
    the default path stays cheap.
  - "Frequent network only" — rebuilds the route layer filtered to
    `headway_minutes <= 15` (`FREQUENT_MAX_HEADWAY`). **Defaults to ON** —
    opening with all ~39 routes at once was the single biggest source of map
    clutter, more than any individual layer. `?freq=0` opts back into the
    full network; the common case (checked) stays out of the URL, same
    "only write the uncommon state" pattern as the route filter above.
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

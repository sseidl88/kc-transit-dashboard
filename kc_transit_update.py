#!/usr/bin/env python3
"""KC Transit Dashboard data pipeline.

KCATA (RideKC) publishes an open GTFS static feed but does not publish an
open real-time (GTFS-RT) feed or per-route ridership/on-time-performance
data (those are Swiftly-authenticated / monthly PDF-only — see CLAUDE.md).
So this pipeline derives *scheduled service* metrics from the static feed:
trip frequency, headway, span of service, stop coverage, and route length.
It snapshots those metrics on every run and only records a new history
entry when something actually changed, since KCATA revises schedules a
few times a year, not daily.
"""

import argparse
import csv
import io
import json
import math
import os
import statistics
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# KCATA's own feed host (a legacy IIS box) refuses connections from
# cloud/datacenter IP ranges — confirmed with hard connect-timeouts from both
# GitHub Actions runners and a cloud dev sandbox, consistently, not a fluke.
# Transitland mirrors the same feed on infrastructure that's actually
# reachable, so that's the default when an API key is available. See
# CLAUDE.md for how to get a free key.
GTFS_URL = "http://www.kc-metro.com/gtf/google_transit.zip"
TRANSITLAND_URL = "https://transit.land/api/v2/rest/feeds/f-9yu-kcata/download_latest_feed_version"
ROOT = Path(__file__).parent
DATA_DIR = ROOT / "docs" / "data"
HISTORY_PATH = DATA_DIR / "history.json"
HISTORY_MAX_ENTRIES = 60

ROUTE_TYPE_NAMES = {
    "0": "Streetcar",
    "1": "Subway",
    "2": "Commuter Rail",
    "3": "Bus",
    "5": "Cable Car",
    "11": "Trolleybus",
    "12": "Monorail",
}

# Sequential blue ramp (dataviz palette, ordinal steps 550/400/300/250) —
# darker = more frequent service. Lightest step held at 250, the palette's
# ordinal floor for the light surface (lighter steps drop below 2:1 contrast).
FREQUENCY_TIERS = [
    (15, "Frequent (≤15 min)", "#1c5cab"),
    (30, "Standard (16–30 min)", "#3987e5"),
    (60, "Infrequent (31–60 min)", "#6da7ec"),
    (float("inf"), "Limited (60+ min / peak-only)", "#86b6ef"),
]

WEEKDAY_FIELDS = ["monday", "tuesday", "wednesday", "thursday", "friday"]
DOW_FIELDS = WEEKDAY_FIELDS + ["saturday", "sunday"]


def log(msg):
    print(f"[kc-transit] {msg}", flush=True)


# ---------------------------------------------------------------------------
# GTFS download + parsing
# ---------------------------------------------------------------------------

def download_gtfs(url, local_zip=None, attempts=5, backoff_seconds=30):
    if local_zip:
        log(f"Using local GTFS zip: {local_zip}")
        data = Path(local_zip).read_bytes()
        return data, None

    transitland_key = os.environ.get("TRANSITLAND_API_KEY")
    if transitland_key:
        url = TRANSITLAND_URL
        params = {"apikey": transitland_key}
        log("Using Transitland mirror (TRANSITLAND_API_KEY set)")
    else:
        params = None
        log("No TRANSITLAND_API_KEY set — falling back to KCATA's direct feed URL "
            "(likely unreachable from cloud CI; see CLAUDE.md)")

    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            log(f"Downloading GTFS feed (attempt {attempt}/{attempts})")
            resp = requests.get(url, params=params, timeout=90)
            resp.raise_for_status()
            log(f"Downloaded {len(resp.content):,} bytes")
            return resp.content, resp.headers.get("Last-Modified")
        except requests.exceptions.RequestException as exc:
            last_error = exc
            log(f"  attempt {attempt} failed: {exc}")
            if attempt < attempts:
                time.sleep(backoff_seconds * attempt)
    raise RuntimeError(f"Could not download GTFS feed after {attempts} attempts") from last_error


def read_csv(zf, filename):
    if filename not in zf.namelist():
        log(f"  (missing {filename}, skipping)")
        return []
    with zf.open(filename) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig")
        return list(csv.DictReader(text))


def stream_csv(zf, filename):
    if filename not in zf.namelist():
        return
    with zf.open(filename) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig")
        yield from csv.DictReader(text)


# ---------------------------------------------------------------------------
# Calendar handling
# ---------------------------------------------------------------------------

def active_service_ids(calendar_rows, calendar_dates_rows, target_date):
    dow_field = DOW_FIELDS[target_date.weekday()]
    active = set()
    for row in calendar_rows:
        try:
            start = datetime.strptime(row["start_date"], "%Y%m%d").date()
            end = datetime.strptime(row["end_date"], "%Y%m%d").date()
        except (KeyError, ValueError):
            continue
        if start <= target_date <= end and row.get(dow_field) == "1":
            active.add(row["service_id"])
    for row in calendar_dates_rows:
        try:
            d = datetime.strptime(row["date"], "%Y%m%d").date()
        except (KeyError, ValueError):
            continue
        if d != target_date:
            continue
        if row.get("exception_type") == "1":
            active.add(row["service_id"])
        elif row.get("exception_type") == "2":
            active.discard(row["service_id"])
    return active


def next_weekday_date(start, target_weekday, horizon_days=14):
    """First date >= start (searching forward) whose weekday matches target_weekday."""
    for offset in range(horizon_days):
        d = start + timedelta(days=offset)
        if d.weekday() == target_weekday:
            return d
    return start


def pick_representative_dates(calendar_rows, calendar_dates_rows, today):
    """Pick the soonest Wed/Sat/Sun that actually has scheduled service."""
    targets = {"weekday": 2, "saturday": 5, "sunday": 6}  # Wed, Sat, Sun
    picked = {}
    for label, dow in targets.items():
        candidate = next_weekday_date(today, dow)
        for _ in range(6):  # try a few consecutive weeks if service is empty
            ids = active_service_ids(calendar_rows, calendar_dates_rows, candidate)
            if ids:
                break
            candidate += timedelta(days=7)
        picked[label] = (candidate, active_service_ids(calendar_rows, calendar_dates_rows, candidate))
    return picked


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def gtfs_time_to_seconds(value):
    try:
        h, m, s = (int(p) for p in value.strip().split(":"))
        return h * 3600 + m * 60 + s
    except (ValueError, AttributeError):
        return None


def format_clock(seconds_total):
    if seconds_total is None:
        return None
    h, rem = divmod(seconds_total, 3600)
    m = rem // 60
    next_day = h >= 24
    h = h % 24
    period = "AM" if h < 12 else "PM"
    h12 = h % 12
    if h12 == 0:
        h12 = 12
    label = f"{h12}:{m:02d} {period}"
    return label + " (+1)" if next_day else label


def haversine_miles(lat1, lon1, lat2, lon2):
    r = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def frequency_tier(headway_minutes):
    if headway_minutes is None:
        return FREQUENCY_TIERS[-1][1], FREQUENCY_TIERS[-1][2]
    for threshold, label, color in FREQUENCY_TIERS:
        if headway_minutes <= threshold:
            return label, color
    return FREQUENCY_TIERS[-1][1], FREQUENCY_TIERS[-1][2]


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def build_dataset(zf, today):
    routes_rows = read_csv(zf, "routes.txt")
    trips_rows = read_csv(zf, "trips.txt")
    calendar_rows = read_csv(zf, "calendar.txt")
    calendar_dates_rows = read_csv(zf, "calendar_dates.txt")
    shapes_rows = read_csv(zf, "shapes.txt")
    feed_info_rows = read_csv(zf, "feed_info.txt")

    log(f"routes={len(routes_rows)} trips={len(trips_rows)} "
        f"calendar={len(calendar_rows)} calendar_dates={len(calendar_dates_rows)} "
        f"shapes_points={len(shapes_rows)}")

    dates = pick_representative_dates(calendar_rows, calendar_dates_rows, today)
    weekday_ids = dates["weekday"][1]
    saturday_ids = dates["saturday"][1]
    sunday_ids = dates["sunday"][1]

    # trip_id -> (route_id, direction_id, shape_id) for weekday service only
    trip_info = {}
    trips_weekday_count = Counter()
    trips_saturday_count = Counter()
    trips_sunday_count = Counter()
    route_shape_votes = defaultdict(Counter)
    route_direction_votes = defaultdict(Counter)

    for row in trips_rows:
        route_id = row.get("route_id")
        service_id = row.get("service_id")
        if not route_id or not service_id:
            continue
        if service_id in weekday_ids:
            trips_weekday_count[route_id] += 1
            trip_info[row.get("trip_id")] = {
                "route_id": route_id,
                "direction_id": row.get("direction_id") or "0",
                "shape_id": row.get("shape_id"),
            }
            route_direction_votes[route_id][row.get("direction_id") or "0"] += 1
            if row.get("shape_id"):
                route_shape_votes[route_id][row.get("shape_id")] += 1
        elif service_id in saturday_ids:
            trips_saturday_count[route_id] += 1
        elif service_id in sunday_ids:
            trips_sunday_count[route_id] += 1

    # Stream stop_times.txt once, only tracking weekday trips.
    trip_bounds = {}  # trip_id -> [min_seq, min_time, max_seq, max_time]
    route_stops = defaultdict(set)
    for row in stream_csv(zf, "stop_times.txt"):
        trip_id = row.get("trip_id")
        info = trip_info.get(trip_id)
        if info is None:
            continue
        try:
            seq = int(row.get("stop_sequence", ""))
        except ValueError:
            continue
        t = gtfs_time_to_seconds(row.get("departure_time") or row.get("arrival_time"))
        stop_id = row.get("stop_id")
        if stop_id:
            route_stops[info["route_id"]].add(stop_id)
        bounds = trip_bounds.get(trip_id)
        if bounds is None:
            trip_bounds[trip_id] = [seq, t, seq, t]
        else:
            if seq < bounds[0]:
                bounds[0], bounds[1] = seq, t
            if seq > bounds[2]:
                bounds[2], bounds[3] = seq, t

    # Aggregate start times per route, restricted to each route's dominant direction.
    route_start_times = defaultdict(list)
    route_span = {}
    for trip_id, bounds in trip_bounds.items():
        info = trip_info[trip_id]
        start_t, end_t = bounds[1], bounds[3]
        route_id = info["route_id"]
        span = route_span.setdefault(route_id, [None, None])
        if start_t is not None and (span[0] is None or start_t < span[0]):
            span[0] = start_t
        if end_t is not None and (span[1] is None or end_t > span[1]):
            span[1] = end_t
        dominant_direction = route_direction_votes[route_id].most_common(1)[0][0]
        if info["direction_id"] == dominant_direction and start_t is not None:
            route_start_times[route_id].append(start_t)

    # Shape geometry lookup.
    shape_points = defaultdict(list)
    for row in shapes_rows:
        try:
            seq = int(row["shape_pt_sequence"])
            lat = float(row["shape_pt_lat"])
            lon = float(row["shape_pt_lon"])
        except (KeyError, ValueError):
            continue
        shape_points[row["shape_id"]].append((seq, lat, lon))
    for shape_id in shape_points:
        shape_points[shape_id].sort(key=lambda p: p[0])

    routes_out = []
    features = []
    for row in routes_rows:
        route_id = row.get("route_id")
        weekday_trips = trips_weekday_count.get(route_id, 0)
        if weekday_trips == 0:
            continue  # no weekday service in this feed cycle (e.g. seasonal/demand-response)

        start_times = sorted(route_start_times.get(route_id, []))
        headway_minutes = None
        if len(start_times) >= 2:
            diffs = [(b - a) / 60 for a, b in zip(start_times, start_times[1:]) if b > a]
            if diffs:
                headway_minutes = round(statistics.median(diffs), 1)

        span = route_span.get(route_id, [None, None])
        tier_label, tier_color = frequency_tier(headway_minutes)

        shape_id = None
        votes = route_shape_votes.get(route_id)
        if votes:
            shape_id = votes.most_common(1)[0][0]
        route_length_miles = None
        points = shape_points.get(shape_id, []) if shape_id else []
        if len(points) >= 2:
            length = sum(
                haversine_miles(points[i][1], points[i][2], points[i + 1][1], points[i + 1][2])
                for i in range(len(points) - 1)
            )
            route_length_miles = round(length, 1)
            features.append({
                "type": "Feature",
                "properties": {
                    "route_id": route_id,
                    "route_short_name": row.get("route_short_name") or "",
                    "route_long_name": row.get("route_long_name") or "",
                    "route_type": ROUTE_TYPE_NAMES.get(row.get("route_type"), "Bus"),
                    "headway_minutes": headway_minutes,
                    "frequency_tier": tier_label,
                    "color": tier_color,
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[lon, lat] for _, lat, lon in points],
                },
            })

        routes_out.append({
            "route_id": route_id,
            "route_short_name": row.get("route_short_name") or "",
            "route_long_name": row.get("route_long_name") or "",
            "route_type": ROUTE_TYPE_NAMES.get(row.get("route_type"), "Bus"),
            "trips_weekday": weekday_trips,
            "trips_saturday": trips_saturday_count.get(route_id, 0),
            "trips_sunday": trips_sunday_count.get(route_id, 0),
            "headway_minutes": headway_minutes,
            "frequency_tier": tier_label,
            "span_start": format_clock(span[0]),
            "span_end": format_clock(span[1]),
            "stop_count": len(route_stops.get(route_id, ())),
            "route_length_miles": route_length_miles,
            "has_geometry": route_length_miles is not None,
        })

    routes_out.sort(key=lambda r: r["trips_weekday"], reverse=True)

    feed_version = None
    if feed_info_rows:
        feed_version = feed_info_rows[0].get("feed_version")

    meta = {
        "weekday_date_checked": dates["weekday"][0].isoformat(),
        "saturday_date_checked": dates["saturday"][0].isoformat(),
        "sunday_date_checked": dates["sunday"][0].isoformat(),
        "route_count": len(routes_out),
        "total_weekday_trips": sum(r["trips_weekday"] for r in routes_out),
        "feed_version": feed_version,
    }

    geojson = {"type": "FeatureCollection", "features": features}
    return routes_out, geojson, meta


# ---------------------------------------------------------------------------
# History / trending
# ---------------------------------------------------------------------------

def route_signature(routes):
    # Lists, not tuples: this gets JSON round-tripped through history.json,
    # and json.loads never reconstructs tuples, so a tuple-vs-list comparison
    # would always be unequal even when nothing actually changed.
    return [[r["route_id"], r["trips_weekday"], r["headway_minutes"]] for r in routes]


def compute_trending(previous_routes, current_routes):
    prev_by_id = {r["route_id"]: r for r in previous_routes}
    up, down = [], []
    for r in current_routes:
        prev = prev_by_id.get(r["route_id"])
        if prev is None:
            continue
        before, after = prev["trips_weekday"], r["trips_weekday"]
        if before == 0 or after == before:
            continue
        pct = round((after - before) / before * 100, 1)
        entry = {
            "route_id": r["route_id"],
            "route_short_name": r["route_short_name"],
            "route_long_name": r["route_long_name"],
            "metric": "trips_weekday",
            "before": before,
            "after": after,
            "pct_change": pct,
        }
        (up if after > before else down).append(entry)
    up.sort(key=lambda e: e["pct_change"], reverse=True)
    down.sort(key=lambda e: e["pct_change"])
    return up, down


def update_history(routes_out, today_iso, feed_last_modified):
    history = []
    if HISTORY_PATH.exists():
        history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))

    trending_up, trending_down = [], []
    new_signature = route_signature(routes_out)
    last_entry = history[-1] if history else None

    if last_entry is None or last_entry["signature"] != new_signature:
        if last_entry is not None:
            trending_up, trending_down = compute_trending(last_entry["routes"], routes_out)
        history.append({
            "date": today_iso,
            "feed_last_modified": feed_last_modified,
            "signature": new_signature,
            "routes": [
                {"route_id": r["route_id"], "route_short_name": r["route_short_name"],
                 "trips_weekday": r["trips_weekday"], "headway_minutes": r["headway_minutes"]}
                for r in routes_out
            ],
        })
        history = history[-HISTORY_MAX_ENTRIES:]
        log(f"Schedule change detected — recorded new history entry "
            f"({len(trending_up)} up, {len(trending_down)} down)")
    else:
        log("No schedule change since last recorded snapshot")

    return history, trending_up, trending_down


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gtfs-url", default=GTFS_URL)
    parser.add_argument("--local-zip", default=None, help="Use a local GTFS zip instead of downloading")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    content, last_modified = download_gtfs(args.gtfs_url, args.local_zip)
    today = datetime.now(timezone.utc).date()

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        routes_out, geojson, meta = build_dataset(zf, today)

    now_iso = datetime.now(timezone.utc).isoformat()
    meta["generated_at"] = now_iso
    meta["feed_last_modified"] = last_modified

    (DATA_DIR / "routes.json").write_text(
        json.dumps({"meta": meta, "routes": routes_out}, indent=2), encoding="utf-8")
    (DATA_DIR / "routes.geojson").write_text(json.dumps(geojson), encoding="utf-8")

    history, trending_up, trending_down = update_history(routes_out, today.isoformat(), last_modified)
    HISTORY_PATH.write_text(json.dumps(history, indent=2), encoding="utf-8")

    (DATA_DIR / "trending.json").write_text(json.dumps({
        "as_of": now_iso,
        "up": trending_up,
        "down": trending_down,
    }, indent=2), encoding="utf-8")

    (DATA_DIR / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    log(f"Done. {meta['route_count']} routes, {meta['total_weekday_trips']} weekday trips.")


if __name__ == "__main__":
    main()

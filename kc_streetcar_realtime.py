#!/usr/bin/env python3
"""KC Streetcar live-position tracker.

Runs every 5 minutes via .github/workflows/streetcar-realtime.yml (separate
from the once-daily kc_transit_update.py pipeline — this is a different kind
of data with a different refresh cadence). Pulls KCATA's real-time GTFS-RT
vehicle-positions feed through Transitland's cached REST pass-through
(https://transit.land/api/v2/rest/feeds/f-kcata~rt/download_latest_rt/...),
using the same TRANSITLAND_API_KEY secret the static pipeline already has —
no new secret needed. Hitting Swiftly's api.goswift.ly endpoint directly
returns 401 without a bearer token KCATA/Swiftly would have to issue; the
Transitland pass-through sidesteps that entirely since Transitland already
holds an authorized key for this feed (confirmed working — see CLAUDE.md).

GitHub Pages is static-only, so this can't just run in the browser: the
API key would be exposed to anyone who views source. Instead this script
writes small static JSON snapshots into docs/data/, and the frontend polls
*those* — same "JSON files are the database" pattern as the rest of this
project, just refreshed far more often than the daily pipeline.

Writes two files:
  - streetcar_live.json — current positions of every KC Streetcar vehicle
    (route_id 601 in KCATA's feed), overwritten each run.
  - streetcar_delays.json — a rolling log of *observed* schedule adherence,
    appended to each run, trimmed to the last few days. Deliberately built
    from actual observed stop arrivals (vehicle position entities showing
    currentStatus == STOPPED_AT), not shifting GTFS-RT arrival predictions
    for stops not yet reached — a real "how did it do" record, not a moving
    guess. That also means it's a SAMPLE: a streetcar typically dwells at a
    stop for well under 5 minutes, so most stops on most trips are never
    caught mid-dwell by a 5-minute poll. Documented plainly in the frontend
    copy, not papered over.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "docs" / "data"

RT_URL_TEMPLATE = "https://transit.land/api/v2/rest/feeds/f-kcata~rt/download_latest_rt/vehicle_positions.json"
STREETCAR_ROUTE_ID = "601"
LOCAL_TZ = ZoneInfo("America/Chicago")
# 180 days, not the original 3 -- the frontend's "today" panel only ever
# needed a few days of buffer, but the delay-trends view (by hour of day,
# by direction, by day) needs real history to accumulate into, and the
# original 3-day trim was silently deleting the exact data that view needs
# before it could ever build up. ~110 records/day observed in practice, so
# 180 days is roughly 20k records (a few MB) -- generous without being
# unbounded.
DELAY_RETENTION_DAYS = 180
UA = "kc-transit-dashboard/1.0 (streetcar live tracker, github.com/sseidl88/kc-transit-dashboard)"


def log(msg):
    print(f"[kc-streetcar-rt] {msg}", flush=True)


def fetch_vehicle_positions(api_key, attempts=3, backoff_seconds=10):
    url = f"{RT_URL_TEMPLATE}?apikey={api_key}"
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last_error = exc
            log(f"  attempt {attempt}/{attempts} failed: {exc}")
            if attempt < attempts:
                time.sleep(backoff_seconds * attempt)
    raise RuntimeError(f"Could not fetch vehicle positions after {attempts} attempts") from last_error


def scheduled_epoch(service_date, scheduled_seconds):
    """service_date: 'YYYYMMDD' string (GTFS-RT's startDate). scheduled_seconds
    is seconds since midnight of that service date in local (America/Chicago)
    wall-clock time -- can exceed 86400 for after-midnight trips, same
    >24:00:00 convention GTFS static uses, and timedelta rolls that into the
    next calendar day correctly."""
    midnight = datetime.strptime(service_date, "%Y%m%d")
    local_naive = midnight + timedelta(seconds=scheduled_seconds)
    local_dt = local_naive.replace(tzinfo=LOCAL_TZ)
    return local_dt.timestamp()


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log(f"  warning: couldn't parse {path.name}, starting fresh")
        return default


def main():
    api_key = os.environ.get("TRANSITLAND_API_KEY")
    if not api_key:
        log("No TRANSITLAND_API_KEY set -- this script has no fallback (unlike the daily "
            "static pipeline), since there's no direct-feed alternative for real-time data. Exiting.")
        sys.exit(1)

    log("Fetching KCATA vehicle positions via Transitland pass-through")
    data = fetch_vehicle_positions(api_key)
    entities = data.get("entity", [])
    log(f"Fetched {len(entities)} total vehicle positions")

    streetcar_entities = [
        e for e in entities
        if e.get("vehicle", {}).get("trip", {}).get("routeId") == STREETCAR_ROUTE_ID
    ]
    log(f"{len(streetcar_entities)} are KC Streetcar (route {STREETCAR_ROUTE_ID})")

    now_iso = datetime.now(timezone.utc).isoformat()
    vehicles_out = []
    for e in streetcar_entities:
        v = e["vehicle"]
        trip = v.get("trip", {})
        pos = v.get("position", {})
        speed_ms = pos.get("speed")
        vehicles_out.append({
            "vehicle_id": v.get("vehicle", {}).get("id") or e.get("id"),
            "trip_id": trip.get("tripId"),
            "direction_id": trip.get("directionId"),
            "start_time": trip.get("startTime"),
            "start_date": trip.get("startDate"),
            "lat": pos.get("latitude"),
            "lon": pos.get("longitude"),
            "bearing": pos.get("bearing"),
            "speed_mph": round(speed_ms * 2.23694, 1) if speed_ms is not None else None,
            "current_status": v.get("currentStatus"),
            "stop_id": v.get("stopId"),
            "stop_sequence": v.get("currentStopSequence"),
            "occupancy_status": v.get("occupancyStatus"),
            "occupancy_percentage": v.get("occupancyPercentage"),
            "timestamp": v.get("timestamp"),
        })

    (DATA_DIR / "streetcar_live.json").write_text(json.dumps({
        "generated_at": now_iso,
        "route_id": STREETCAR_ROUTE_ID,
        "vehicles": vehicles_out,
    }, indent=2), encoding="utf-8")
    log(f"Wrote streetcar_live.json ({len(vehicles_out)} vehicles)")

    # --- Delay tracking: only for vehicles actually STOPPED_AT a stop right
    # now -- an observed event, not a shifting prediction. ---
    schedule = load_json(DATA_DIR / "streetcar_schedule.json", {"trips": {}})["trips"]
    if not schedule:
        log("No streetcar_schedule.json found (or it's empty) -- skipping delay computation "
            "this run; the daily pipeline writes it, so this should resolve after its next run.")

    delays = load_json(DATA_DIR / "streetcar_delays.json", [])
    seen_keys = {(d["service_date"], d["trip_id"], d["stop_id"]) for d in delays}

    new_records = 0
    for e in streetcar_entities:
        v = e["vehicle"]
        if v.get("currentStatus") != "STOPPED_AT":
            continue
        trip = v.get("trip", {})
        trip_id = trip.get("tripId")
        service_date = trip.get("startDate")
        stop_id = v.get("stopId")
        stop_seq = v.get("currentStopSequence")
        if not (trip_id and service_date and stop_id):
            continue
        key = (service_date, trip_id, stop_id)
        if key in seen_keys:
            continue  # already recorded this stop for this trip today

        trip_schedule = schedule.get(trip_id)
        if not trip_schedule:
            continue  # trip not in today's streetcar schedule (shouldn't normally happen)
        stop_entry = next((s for s in trip_schedule["stops"] if s["stop_sequence"] == stop_seq), None)
        if stop_entry is None:
            continue

        observed_ts = v.get("timestamp")
        if observed_ts is None:
            continue
        sched_epoch = scheduled_epoch(service_date, stop_entry["scheduled_seconds"])
        observed_epoch = int(observed_ts)
        delay_seconds = round(observed_epoch - sched_epoch)

        delays.append({
            "service_date": service_date,
            "trip_id": trip_id,
            "direction_id": trip.get("directionId"),
            "stop_id": stop_id,
            "stop_sequence": stop_seq,
            "scheduled_time_local": datetime.fromtimestamp(sched_epoch, LOCAL_TZ).isoformat(),
            "observed_time_local": datetime.fromtimestamp(observed_epoch, LOCAL_TZ).isoformat(),
            "delay_minutes": round(delay_seconds / 60, 1),
        })
        seen_keys.add(key)
        new_records += 1

    # Trim to DELAY_RETENTION_DAYS -- bounds file growth while keeping enough
    # history for the trends view (by hour of day, by direction, by day).
    cutoff = (datetime.now(LOCAL_TZ) - timedelta(days=DELAY_RETENTION_DAYS)).strftime("%Y%m%d")
    delays = [d for d in delays if d["service_date"] >= cutoff]

    (DATA_DIR / "streetcar_delays.json").write_text(json.dumps(delays, indent=2), encoding="utf-8")
    log(f"Wrote streetcar_delays.json ({new_records} new observations this run, "
        f"{len(delays)} total retained)")


if __name__ == "__main__":
    main()

"""Tests for kc_transit_update.py's metrics math and diffing logic.

Two of these (test_route_signature_survives_json_roundtrip and
test_streetcar_rename_reconciles_instead_of_add_plus_drop) are regression
tests for real bugs caught while building this: a tuple-vs-list JSON
round-trip mismatch that made every run falsely detect a "schedule change",
and a route-matching bug that made a merely-renumbered route look like it
was simultaneously added and discontinued.
"""
import io
import json
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import kc_transit_update as kctu


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def test_gtfs_time_to_seconds():
    assert kctu.gtfs_time_to_seconds("06:05:00") == 6 * 3600 + 5 * 60
    assert kctu.gtfs_time_to_seconds("25:10:00") == 25 * 3600 + 10 * 60  # after-midnight
    assert kctu.gtfs_time_to_seconds("") is None
    assert kctu.gtfs_time_to_seconds(None) is None


def test_format_clock_marks_next_day():
    assert kctu.format_clock(6 * 3600 + 5 * 60) == "6:05 AM"
    assert kctu.format_clock(13 * 3600) == "1:00 PM"
    assert kctu.format_clock(25 * 3600) == "1:00 AM (+1)"
    assert kctu.format_clock(None) is None


def test_haversine_miles_known_distance():
    # Downtown KC to the airport, roughly 15-16 miles as the crow flies.
    d = kctu.haversine_miles(39.0997, -94.5786, 39.2976, -94.7139)
    assert 14 < d < 17


def test_frequency_tier_buckets():
    assert kctu.frequency_tier(10)[0].startswith("Frequent")
    assert kctu.frequency_tier(20)[0].startswith("Standard")
    assert kctu.frequency_tier(45)[0].startswith("Infrequent")
    assert kctu.frequency_tier(90)[0].startswith("Limited")
    assert kctu.frequency_tier(None)[0].startswith("Limited")


# ---------------------------------------------------------------------------
# Calendar / representative-date logic
# ---------------------------------------------------------------------------

def test_active_service_ids_respects_calendar_dates_exceptions():
    calendar = [{"service_id": "WD", "monday": "1", "tuesday": "1", "wednesday": "1",
                 "thursday": "1", "friday": "1", "saturday": "0", "sunday": "0",
                 "start_date": "20260101", "end_date": "20261231"}]
    # A holiday that cancels normal Wednesday service, plus a one-off extra service.
    calendar_dates = [
        {"service_id": "WD", "date": "20260107", "exception_type": "2"},
        {"service_id": "HOLIDAY", "date": "20260107", "exception_type": "1"},
    ]
    normal_wed = date(2026, 1, 14)
    holiday_wed = date(2026, 1, 7)
    assert kctu.active_service_ids(calendar, calendar_dates, normal_wed) == {"WD"}
    assert kctu.active_service_ids(calendar, calendar_dates, holiday_wed) == {"HOLIDAY"}


def test_pick_representative_dates_clamps_into_archived_feed_range():
    """Regression test: pick_representative_dates() used to always search
    forward from `today`. For a feed archived years in the past, "today"
    falls outside every calendar/calendar_dates entry, active_service_ids()
    comes back empty for every candidate date, and every route silently
    ends up with zero trips. It must clamp into the feed's own date range.
    """
    calendar = []
    calendar_dates = [
        {"service_id": "WD", "date": "20201202", "exception_type": "1"},  # a Wednesday
        {"service_id": "SA", "date": "20201205", "exception_type": "1"},
        {"service_id": "SU", "date": "20201206", "exception_type": "1"},
    ]
    picked = kctu.pick_representative_dates(calendar, calendar_dates, date(2026, 8, 4))
    assert picked["weekday"][1] == {"WD"}
    assert picked["saturday"][1] == {"SA"}
    assert picked["sunday"][1] == {"SU"}


def test_pick_representative_dates_live_feed_uses_today():
    today = date(2026, 8, 5)  # a Wednesday
    calendar = [{"service_id": "WD", "monday": "1", "tuesday": "1", "wednesday": "1",
                 "thursday": "1", "friday": "1", "saturday": "0", "sunday": "0",
                 "start_date": "20260101", "end_date": "20261231"}]
    picked = kctu.pick_representative_dates(calendar, [], today)
    assert picked["weekday"][0] == today
    assert picked["weekday"][1] == {"WD"}


# ---------------------------------------------------------------------------
# route_signature / history round-trip
# ---------------------------------------------------------------------------

def test_route_signature_survives_json_roundtrip():
    """Regression test: route_signature() must return lists, not tuples.
    history.json round-trips through json.dumps/json.loads, and json.loads
    never reconstructs tuples — comparing a freshly computed tuple against a
    disk-loaded list is always unequal even when nothing changed, which
    made every single run falsely detect a "schedule change".
    """
    routes = [{"route_id": "R1", "route_short_name": "1", "trips_weekday": 50, "headway_minutes": 15.0}]
    sig = kctu.route_signature(routes)
    roundtripped = json.loads(json.dumps(sig))
    assert sig == roundtripped


# ---------------------------------------------------------------------------
# compute_trending
# ---------------------------------------------------------------------------

def _route(route_id, short, long_name, trips, headway=15.0):
    return {"route_id": route_id, "route_short_name": short, "route_long_name": long_name,
            "trips_weekday": trips, "headway_minutes": headway}


def test_compute_trending_detects_up_down_added_discontinued():
    previous = [_route("A", "1", "First St", 40), _route("B", "2", "Second St", 30)]
    current = [_route("A", "1", "First St", 60), _route("C", "3", "Third St", 20)]
    up, down, added, discontinued = kctu.compute_trending(previous, current)
    assert [e["route_short_name"] for e in up] == ["1"]
    assert up[0]["before"] == 40 and up[0]["after"] == 60
    assert down == []
    assert [r["route_short_name"] for r in added] == ["3"]
    assert [r["route_short_name"] for r in discontinued] == ["2"]


def test_streetcar_rename_reconciles_instead_of_add_plus_drop():
    """Regression test: KC Streetcar's route code changed from STRC (2020
    archive) to STCR (current feed) — same route, same long name, just a
    relabel. Matching on route_short_name alone made it show up as both
    newly "added" (STCR) and "discontinued" (STRC), which is a false and
    fairly embarrassing claim about KC's flagship transit line. It must
    reconcile via matching route_long_name instead.
    """
    previous = [_route("R1", "STRC", "KC Streetcar", 137)]
    current = [_route("R2", "STCR", "KC Streetcar", 230)]
    up, down, added, discontinued = kctu.compute_trending(previous, current)
    assert added == []
    assert discontinued == []
    assert len(up) == 1
    assert up[0]["before"] == 137 and up[0]["after"] == 230
    assert "STRC" in up[0]["route_short_name"] and "STCR" in up[0]["route_short_name"]


def test_compute_trending_ignores_unchanged_routes():
    previous = [_route("A", "1", "First St", 40)]
    current = [_route("A", "1", "First St", 40)]
    up, down, added, discontinued = kctu.compute_trending(previous, current)
    assert up == down == added == discontinued == []


# ---------------------------------------------------------------------------
# build_dataset — small synthetic GTFS fixture (integration-style)
# ---------------------------------------------------------------------------

def _make_fixture_zip():
    files = {
        "routes.txt": "route_id,route_short_name,route_long_name,route_type\n"
                      "R1,10,Tenth Street,3\n",
        "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                        "WD,1,1,1,1,1,0,0,20260101,20261231\n",
        "calendar_dates.txt": "service_id,date,exception_type\n",
        "shapes.txt": "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
                      "S1,39.05,-94.58,1\nS1,39.03,-94.57,2\nS1,39.00,-94.56,3\n",
    }
    trips = ["route_id,service_id,trip_id,direction_id,shape_id"]
    stop_times = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence"]
    # Three weekday trips, 20-minute headway, 2 stops each.
    for i, start_min in enumerate([6 * 60, 6 * 60 + 20, 6 * 60 + 40]):
        trip_id = f"T{i}"
        trips.append(f"R1,WD,{trip_id},0,S1")
        for seq, stop_id in enumerate(["STOPA", "STOPB"], start=1):
            t = start_min + seq * 5
            h, m = divmod(t, 60)
            stop_times.append(f"{trip_id},{h:02d}:{m:02d}:00,{h:02d}:{m:02d}:00,{stop_id},{seq}")
    files["trips.txt"] = "\n".join(trips) + "\n"
    files["stop_times.txt"] = "\n".join(stop_times) + "\n"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    buf.seek(0)
    return zipfile.ZipFile(buf)


def test_build_dataset_computes_expected_metrics():
    zf = _make_fixture_zip()
    routes_out, geojson, hubs_geojson, meta = kctu.build_dataset(zf, date(2026, 8, 5))  # a Wednesday

    assert len(routes_out) == 1
    r = routes_out[0]
    assert r["trips_weekday"] == 3
    assert r["headway_minutes"] == 20.0
    assert r["stop_count"] == 2
    assert r["span_start"] == "6:05 AM"
    assert r["route_length_miles"] is not None and r["route_length_miles"] > 0
    assert r["has_geometry"] is True

    assert len(geojson["features"]) == 1
    assert meta["route_count"] == 1
    assert meta["total_weekday_trips"] == 3


def test_build_dataset_skips_routes_with_no_weekday_service():
    """A route that only runs on Saturdays (e.g. a demand-response/seasonal
    route) shouldn't appear in routes_out, which is built around weekday
    service — it would otherwise show up with every weekday metric null."""
    files = {
        "routes.txt": "route_id,route_short_name,route_long_name,route_type\n"
                      "R1,10,Tenth Street,3\nR2,SAT,Saturday Only,3\n",
        "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                        "WD,1,1,1,1,1,0,0,20260101,20261231\n"
                        "SA,0,0,0,0,0,1,0,20260101,20261231\n",
        "calendar_dates.txt": "service_id,date,exception_type\n",
        "shapes.txt": "",
        "trips.txt": "route_id,service_id,trip_id,direction_id,shape_id\n"
                     "R1,WD,T0,0,\nR2,SA,T1,0,\n",
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                          "T0,06:00:00,06:00:00,STOPA,1\nT0,06:10:00,06:10:00,STOPB,2\n"
                          "T1,09:00:00,09:00:00,STOPA,1\nT1,09:10:00,09:10:00,STOPB,2\n",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf_write:
        for name, content in files.items():
            zf_write.writestr(name, content)
    buf.seek(0)
    zf = zipfile.ZipFile(buf)

    routes_out, _, _, meta = kctu.build_dataset(zf, date(2026, 8, 5))  # a Wednesday
    assert [r["route_id"] for r in routes_out] == ["R1"]
    assert meta["route_count"] == 1

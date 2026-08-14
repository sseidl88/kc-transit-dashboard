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


def test_simplify_polyline_collapses_collinear_points():
    # A dead-straight line: GTFS-style dense sampling should collapse to endpoints.
    points = [(39.0, -94.5 + i * 0.001) for i in range(50)]
    simplified = kctu.simplify_polyline(points)
    assert simplified == [points[0], points[-1]]


def test_simplify_polyline_keeps_real_corners():
    # An L-shaped path (continuous, no gap at the bend) should keep the
    # corner as its own point rather than simplifying straight through it.
    points = [(39.0, -94.5 + i * 0.001) for i in range(25)]
    points += [(39.0 + i * 0.001, -94.476) for i in range(25)]
    simplified = kctu.simplify_polyline(points)
    assert len(simplified) == 3
    assert simplified[0] == points[0]
    assert simplified[-1] == points[-1]


def test_simplify_polyline_leaves_short_lines_alone():
    points = [(39.0, -94.5), (39.01, -94.49)]
    assert kctu.simplify_polyline(points) == points


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
    routes_out, geojson, hubs_geojson, stops_geojson, meta, _ = kctu.build_dataset(zf, date(2026, 8, 5))  # a Wednesday

    assert len(routes_out) == 1
    r = routes_out[0]
    assert r["trips_weekday"] == 3
    assert r["headway_minutes"] == 20.0
    assert r["stop_count"] == 2
    assert r["span_start"] == "6:05 AM"
    assert r["route_length_miles"] is not None and r["route_length_miles"] > 0
    assert r["has_geometry"] is True

    # Each fixture trip's two stops are 5 scheduled minutes apart -> avg
    # speed should be route_length_miles / (5/60) hours, within rounding.
    assert r["avg_speed_mph"] is not None and r["avg_speed_mph"] > 0
    expected_speed = r["route_length_miles"] / (5 / 60)
    assert abs(r["avg_speed_mph"] - expected_speed) < 0.5

    assert len(geojson["features"]) == 1
    assert meta["route_count"] == 1
    assert meta["total_weekday_trips"] == 3


def test_build_dataset_streetcar_schedule_extraction():
    """streetcar_route_id opts a route into the extra per-trip stop schedule
    used by kc_streetcar_realtime.py to compute delay against live GTFS-RT --
    omitted entirely (empty dict) when no streetcar_route_id is passed, since
    that's the normal case for every route on every ordinary daily run."""
    zf = _make_fixture_zip()
    *_, streetcar_schedule = kctu.build_dataset(zf, date(2026, 8, 5), streetcar_route_id="R1")

    assert set(streetcar_schedule.keys()) == {"T0", "T1", "T2"}
    t0 = streetcar_schedule["T0"]
    assert t0["direction_id"] == "0"
    assert [s["stop_id"] for s in t0["stops"]] == ["STOPA", "STOPB"]
    assert [s["stop_sequence"] for s in t0["stops"]] == [1, 2]
    # Fixture's first trip starts at 6:00 AM, stop A at +5min, stop B at +10min.
    assert t0["stops"][0]["scheduled_seconds"] == 6 * 3600 + 5 * 60
    assert t0["stops"][1]["scheduled_seconds"] == 6 * 3600 + 10 * 60

    zf2 = _make_fixture_zip()
    *_, no_streetcar = kctu.build_dataset(zf2, date(2026, 8, 5))
    assert no_streetcar == {}


def test_streetcar_schedule_uses_todays_date_not_representative_weekday():
    """Regression test for a real bug hit while building the live-tracking
    feature: pick_representative_dates() anchors the normal weekday/Saturday/
    Sunday metrics to the *nearest* date of each type (e.g. the nearest
    upcoming Wednesday), not literally today -- correct for those stable,
    comparable metrics, but wrong for the streetcar schedule, since real-time
    GTFS-RT trip_ids are tied to *today's* actual service_id. Using the
    representative Wednesday's trip_ids instead of today's meant the live
    tracker's delay computation could never find a matching trip_id at all
    (verified against the real feed: today's live trip_ids and the
    representative Wednesday's static trip_ids didn't overlap even slightly).
    This fixture has two same-route trips on two different service patterns
    (Wednesday-only vs Friday-only) specifically to catch a regression back
    to anchoring on the representative date instead of the literal one passed in.
    """
    files = {
        "routes.txt": "route_id,route_short_name,route_long_name,route_type\n"
                      "R1,STCR,Streetcar,0\n",
        "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                        "WED,0,0,1,0,0,0,0,20260101,20261231\n"
                        "FRI,0,0,0,0,1,0,0,20260101,20261231\n",
        "calendar_dates.txt": "service_id,date,exception_type\n",
        "shapes.txt": "",
        "trips.txt": "route_id,service_id,trip_id,direction_id,shape_id\n"
                     "R1,WED,TWED,0,\n"
                     "R1,FRI,TFRI,0,\n",
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                          "TWED,06:00:00,06:00:00,STOPA,1\n"
                          "TWED,06:10:00,06:10:00,STOPB,2\n"
                          "TFRI,07:00:00,07:00:00,STOPA,1\n"
                          "TFRI,07:10:00,07:10:00,STOPB,2\n",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf_write:
        for name, content in files.items():
            zf_write.writestr(name, content)
    buf.seek(0)
    zf = zipfile.ZipFile(buf)

    friday = date(2026, 8, 14)  # a Friday; nearest representative Wednesday is 2026-08-19
    assert friday.strftime("%A") == "Friday"
    *_, streetcar_schedule = kctu.build_dataset(zf, friday, streetcar_route_id="R1")

    assert set(streetcar_schedule.keys()) == {"TFRI"}, (
        "should capture today's (Friday's) actual trip, not the representative Wednesday's")
    assert streetcar_schedule["TFRI"]["stops"][0]["scheduled_seconds"] == 7 * 3600


def test_build_dataset_stops_and_hubs_layers():
    """A stop served only by an infrequent route shouldn't be flagged
    'frequent'; a stop served by 3+ routes should show up as a transfer hub;
    a stop served by just 2 routes (common, not very meaningful) shouldn't."""
    files = {
        "routes.txt": "route_id,route_short_name,route_long_name,route_type\n"
                      "R1,F,Frequent Route,3\nR2,I,Infrequent Route,3\nR3,I2,Infrequent Route 2,3\n",
        "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                        "WD,1,1,1,1,1,0,0,20260101,20261231\n",
        "calendar_dates.txt": "service_id,date,exception_type\n",
        "shapes.txt": "",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n"
                     "HUB,Hub Stop,39.05,-94.58\nLONELY,Lonely Stop,39.10,-94.60\n",
        # R1 is frequent (10-min headway) and stops at HUB only.
        # R2 and R3 are infrequent (60-min headway) and both also stop at HUB
        # (making it a 3-route hub) plus LONELY (a 2-route, non-hub stop).
        "trips.txt": "route_id,service_id,trip_id,direction_id,shape_id\n"
                     "R1,WD,T0,0,\nR1,WD,T1,0,\nR1,WD,T2,0,\n"
                     "R2,WD,T3,0,\nR2,WD,T4,0,\n"
                     "R3,WD,T5,0,\nR3,WD,T6,0,\n",
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                          "T0,06:00:00,06:00:00,HUB,1\nT1,06:10:00,06:10:00,HUB,1\nT2,06:20:00,06:20:00,HUB,1\n"
                          "T3,07:00:00,07:00:00,HUB,1\nT3,07:05:00,07:05:00,LONELY,2\n"
                          "T4,08:00:00,08:00:00,HUB,1\nT4,08:05:00,08:05:00,LONELY,2\n"
                          "T5,07:00:00,07:00:00,HUB,1\nT5,07:05:00,07:05:00,LONELY,2\n"
                          "T6,08:00:00,08:00:00,HUB,1\nT6,08:05:00,08:05:00,LONELY,2\n",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf_write:
        for name, content in files.items():
            zf_write.writestr(name, content)
    buf.seek(0)
    zf = zipfile.ZipFile(buf)

    _, _, hubs_geojson, stops_geojson, _, _ = kctu.build_dataset(zf, date(2026, 8, 5))  # a Wednesday

    stops_by_id = {f["properties"]["stop_id"]: f["properties"] for f in stops_geojson["features"]}
    assert stops_by_id["HUB"]["frequent"] is True   # served by R1 (10-min headway) among others
    assert stops_by_id["LONELY"]["frequent"] is False  # only served by infrequent R2/R3

    hub_ids = {f["properties"]["stop_id"] for f in hubs_geojson["features"]}
    assert hub_ids == {"HUB"}  # 3 routes converge here; LONELY only has 2


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

    routes_out, _, _, _, meta, _ = kctu.build_dataset(zf, date(2026, 8, 5))  # a Wednesday
    assert [r["route_id"] for r in routes_out] == ["R1"]
    assert meta["route_count"] == 1


# ---------------------------------------------------------------------------
# Multi-agency config
# ---------------------------------------------------------------------------

def test_agencies_config_well_formed():
    assert "kcata" in kctu.AGENCIES
    for agency_id, agency in kctu.AGENCIES.items():
        assert agency["name"]
        assert agency["direct_url"].startswith("http")
        assert agency["transitland_onestop_id"].startswith("f-")
        assert agency["out_dir"] is not None
    # kcata must stay at the original root data dir — it's the already-live
    # location docs/index.html's default fetches (and GitHub Pages caches) expect.
    assert kctu.AGENCIES["kcata"]["out_dir"] == kctu.DATA_DIR
    assert kctu.AGENCIES["jocounty"]["out_dir"] != kctu.DATA_DIR

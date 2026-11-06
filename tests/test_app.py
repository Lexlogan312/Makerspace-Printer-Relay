"""The main loop, run against fake Supabase / discovery / printer connections."""

import dataclasses

from relay.app import STARTUP_GRACE_S, STUCK_RECONNECT_S, connection_problem
from relay.database import DatabaseError
from relay.printer import Snapshot
from tests.fixtures import PRINTER, PRINTING, FakeConnection, make_relay, state

PRINTING_AMS = PRINTING["print"]["ams"]["ams"]

OTHER_SERIAL = "03900D5C2000916"


def test_connects_to_discovered_ip_and_records_what_discovery_learned():
    relay, db, disc, clock = make_relay([dataclasses.replace(PRINTER, model="Unknown")])
    disc.announce(PRINTER.serial, "10.42.0.23")
    disc.announce(OTHER_SERIAL, "10.42.0.40")  # not registered
    relay.tick()

    (conn,) = FakeConnection.created
    assert conn.host == "10.42.0.23" and conn.started  # discovery beats the stored 172.20.10.7
    assert (PRINTER.id, {"host": "10.42.0.23"}) in db.written("update_connection")
    assert (PRINTER.id, {"model": "A1"}) in db.written("update_printer")
    assert (PRINTER.id, {"firmware_version": "01.08.00.00"}) in db.written("update_printer")
    assert db.written("delete_discovered") == [([PRINTER.serial],)]
    (rows,) = db.written("upsert_discovered")[0]
    assert [r["serial"] for r in rows] == [OTHER_SERIAL]

    # Nothing changed, so the next pass writes none of that again
    before = len(db.calls)
    clock.advance(2)
    relay.tick()
    assert [c[0] for c in db.calls[before:]] == []


def test_pushes_status_only_when_it_changes_and_measures_latency():
    relay, db, _disc, clock = make_relay()
    relay.tick()
    conn = FakeConnection.created[0]
    conn.report(state("RUNNING"), clock.wall.timestamp())
    clock.advance(2)
    relay.tick()
    (records,) = db.written("upsert_status")[-1]
    assert records[0]["status"] == "printing"
    assert relay.latencies == [2.0]  # report arrived 2 s before the write

    pushes = len(db.written("upsert_status"))
    clock.advance(2)
    relay.tick()
    assert len(db.written("upsert_status")) == pushes  # unchanged

    clock.advance(31)
    relay.tick()
    assert len(db.written("upsert_status")) == pushes  # still within the minute

    clock.advance(30)  # status refresh, once a minute
    relay.tick()
    assert len(db.written("upsert_status")) == pushes + 1


def test_temperature_and_layer_wobble_alone_do_not_trigger_a_push():
    relay, db, _disc, clock = make_relay()
    relay.tick()
    conn = FakeConnection.created[0]
    conn.report(state("RUNNING", nozzle_temper=220.0, bed_temper=65.0, layer_num=10), clock.wall.timestamp())
    clock.advance(2)
    relay.tick()
    pushes = len(db.written("upsert_status"))
    conn.report(state("RUNNING", nozzle_temper=221.3, bed_temper=64.6, layer_num=11), clock.wall.timestamp())
    clock.advance(2)
    relay.tick()
    assert len(db.written("upsert_status")) == pushes          # nothing a student sees changed
    clock.advance(60)
    relay.tick()
    (records,) = db.written("upsert_status")[-1]
    assert records[0]["nozzle_temp"] == 221 and records[0]["layer_current"] == 11  # sent with the refresh


def test_refreshes_of_unchanged_printers_ride_along_in_one_request():
    other = dataclasses.replace(PRINTER, id="22222222-2222-2222-2222-222222222222", serial=OTHER_SERIAL, label="Flint")
    relay, db, disc, clock = make_relay([PRINTER, other])
    disc.announce(PRINTER.serial, "10.42.0.23")
    disc.announce(OTHER_SERIAL, "10.42.0.24")
    relay.tick()
    a, b = FakeConnection.created
    a.report(state("IDLE"), clock.wall.timestamp())
    clock.advance(2)
    relay.tick()                       # first push: a
    b.report(state("IDLE"), clock.wall.timestamp())
    clock.advance(40)
    relay.tick()                       # b's first push; a is 40 s old (past halfway), so it rides along
    (records,) = db.written("upsert_status")[-1]
    assert {r["printer_id"] for r in records} == {PRINTER.id, other.id}


def test_loaded_filament_is_written_in_one_update_only_when_it_changes():
    relay, db, _disc, clock = make_relay()
    relay.tick()
    conn = FakeConnection.created[0]
    conn.report(state("RUNNING"), clock.wall.timestamp())   # PETG, #FF6A13 (fixtures)
    clock.advance(2)
    relay.tick()
    assert db.written("update_printer") == [(PRINTER.id, {
        "filament_color": "#FF6A13", "filament_color_name": "Orange", "filament_type": "PETG"})]

    conn.report(state("RUNNING", mc_percent=50), clock.wall.timestamp())   # same filament
    clock.advance(2)
    relay.tick()
    assert len(db.written("update_printer")) == 1

    swapped = {"tray_now": "0", "ams": PRINTING_AMS}  # switch to the white PLA slot
    conn.report(state("RUNNING", ams=swapped), clock.wall.timestamp())
    clock.advance(2)
    relay.tick()
    assert db.written("update_printer")[-1] == (PRINTER.id, {
        "filament_color": "#FFFFFF", "filament_color_name": "White", "filament_type": "PLA"})


def test_no_offline_flash_during_startup_then_reports_problem():
    relay, db, _disc, clock = make_relay()
    relay.tick()  # connection created but never connects
    assert db.written("upsert_status") == []
    assert db.written("update_connection") == []

    clock.advance(STARTUP_GRACE_S + 1)
    relay.tick()
    (records,) = db.written("upsert_status")[-1]
    assert records[0]["online"] is False
    # Discovery is running but hasn't heard this printer
    assert (PRINTER.id, {"last_error": "Not seen on the network. Check that it's powered on and joined "
                                       "to the printer hotspot."}) in db.written("update_connection")


def test_ip_change_reconnects():
    relay, db, disc, clock = make_relay()
    disc.announce(PRINTER.serial, "10.42.0.23")
    relay.tick()
    disc.announce(PRINTER.serial, "10.42.0.77")
    clock.advance(2)
    relay.tick()
    first, second = FakeConnection.created
    assert first.stopped and second.host == "10.42.0.77"
    assert (PRINTER.id, {"host": "10.42.0.77"}) in db.written("update_connection")


def test_admin_changes_take_effect_on_next_sync():
    relay, db, _disc, clock = make_relay()
    relay.tick()
    db.printers = [dataclasses.replace(PRINTER, label="Fred", access_code="87654321")]
    clock.advance(31)
    relay.tick()
    first, second = FakeConnection.created
    assert first.stopped and second.printer.access_code == "87654321"


def test_retiring_closes_open_job():
    relay, db, _disc, clock = make_relay()
    relay.tick()
    FakeConnection.created[0].report(state("RUNNING"), clock.wall.timestamp())
    clock.advance(STARTUP_GRACE_S)
    relay.tick()
    db.printers = [dataclasses.replace(PRINTER, maintenance_status="offline_permanent")]
    clock.advance(31)
    relay.tick()
    assert FakeConnection.created[0].stopped and relay.printers == {}
    (rows,) = db.written("upsert_jobs")[-1]
    assert rows[0]["outcome"] == "unknown" and rows[0]["ended_at"]


def test_failed_writes_are_retried_and_rejected_rows_dropped():
    relay, db, _disc, clock = make_relay()
    relay.tick()
    FakeConnection.created[0].report(state("RUNNING"), clock.wall.timestamp())
    db.fail["upsert_status"] = DatabaseError("can't reach Supabase")
    db.fail["insert_events"] = DatabaseError("can't reach Supabase")
    clock.advance(2)
    relay.tick()
    assert relay.pending_events  # kept for later

    del db.fail["upsert_status"], db.fail["insert_events"]
    clock.advance(2)
    relay.tick()
    assert db.written("upsert_status") and relay.pending_events == []

    # A row Supabase rejects (e.g. printer deleted a moment ago) is dropped, not retried forever
    relay.pending_events.append({"printer_id": "gone"})
    db.fail["insert_events"] = DatabaseError("violates foreign key", status=409)
    clock.advance(2)
    relay.tick()
    assert relay.pending_events == []


def test_heartbeat_once_per_minute_in_second_half():
    relay, db, _disc, clock = make_relay()   # clock starts at 14:00:40
    relay.tick()
    clock.advance(2)
    relay.tick()
    assert len(db.written("insert_heartbeat")) == 1
    assert db.written("insert_heartbeat")[0][0]["printers_online"] is None  # still starting up
    clock.advance(20)   # 14:01:02, first half of the next minute
    relay.tick()
    assert len(db.written("insert_heartbeat")) == 1
    clock.advance(30)   # 14:01:32
    relay.tick()
    assert len(db.written("insert_heartbeat")) == 2
    assert db.written("insert_heartbeat")[1][0]["printers_online"] == 0  # past start-up: a real count


def test_connection_problem_messages():
    up = Snapshot(connected=True)
    down = Snapshot(connected=False, error="Access code rejected.")
    assert connection_problem(up, True, True) is None
    assert connection_problem(down, True, False).startswith("Not seen on the network")
    assert connection_problem(down, True, True) == "Access code rejected."
    assert connection_problem(Snapshot(), False, None).startswith("No IP address yet")
    assert connection_problem(Snapshot(), True, None) == "Connecting…"


def test_photo_time_rides_along_unless_someone_is_watching():
    relay, db, _disc, clock = make_relay()
    relay.tick()
    conn = FakeConnection.created[0]
    conn.report(state("RUNNING"), clock.wall.timestamp())
    clock.advance(2)
    relay.tick()
    pushes = len(db.written("upsert_status"))

    # A routine photo: no request of its own, it goes out with the next refresh.
    relay._photo_taken(PRINTER.id, "2026-10-02T14:00:45+00:00")
    clock.advance(2)
    relay.tick()
    assert len(db.written("upsert_status")) == pushes
    clock.advance(60)
    relay.tick()
    (records,) = db.written("upsert_status")[-1]
    assert records[0]["snapshot_at"] == "2026-10-02T14:00:45+00:00"

    # Someone has the printer open: each new photo is sent on the next pass.
    relay.camera = type("Camera", (), {"watching": {PRINTER.id}})()
    pushes = len(db.written("upsert_status"))
    relay._photo_taken(PRINTER.id, "2026-10-02T14:01:50+00:00")
    clock.advance(2)
    relay.tick()
    assert len(db.written("upsert_status")) == pushes + 1
    (records,) = db.written("upsert_status")[-1]
    assert records[0]["snapshot_at"] == "2026-10-02T14:01:50+00:00"
    clock.advance(2)
    relay.tick()
    assert len(db.written("upsert_status")) == pushes + 1  # same photo: nothing more to send


def test_progress_and_time_left_ride_along_but_a_new_estimate_goes_out_at_once():
    relay, db, _disc, clock = make_relay()
    relay.tick()
    conn = FakeConnection.created[0]
    conn.report(state("RUNNING", mc_percent=10, mc_remaining_time=None), clock.wall.timestamp())
    clock.advance(2)
    relay.tick()
    pushes = len(db.written("upsert_status"))

    conn.report(state("RUNNING", mc_percent=10, mc_remaining_time=90), clock.wall.timestamp())
    clock.advance(2)
    relay.tick()
    assert len(db.written("upsert_status")) == pushes + 1      # the first estimate: sent now

    conn.report(state("RUNNING", mc_percent=11, mc_remaining_time=89), clock.wall.timestamp())
    clock.advance(2)
    relay.tick()
    assert len(db.written("upsert_status")) == pushes + 1      # a minute ticking down: waits
    clock.advance(60)
    relay.tick()
    (records,) = db.written("upsert_status")[-1]
    assert records[0]["remaining_minutes"] == 89 and records[0]["progress_percent"] == 11

    conn.report(state("PAUSE", mc_percent=11, mc_remaining_time=89), clock.wall.timestamp())
    clock.advance(2)
    relay.tick()
    assert db.written("upsert_status")[-1][0][0]["status"] == "paused"  # a state change: sent now


def test_a_connection_stuck_on_connecting_is_started_again_while_the_printer_is_on_the_network():
    relay, _db, disc, clock = make_relay()
    disc.announce(PRINTER.serial, "10.42.0.23")
    relay.tick()
    first = FakeConnection.created[-1]
    clock.advance(STUCK_RECONNECT_S - 1)
    relay.tick()
    assert FakeConnection.created[-1] is first                 # still giving it time
    clock.advance(2)
    relay.tick()
    second = FakeConnection.created[-1]
    assert second is not first and first.stopped and second.started

    # Once it's connected, nothing more happens.
    second.report(state("IDLE"), clock.wall.timestamp())
    clock.advance(STUCK_RECONNECT_S * 3)
    relay.tick()
    assert FakeConnection.created[-1] is second


def test_no_fresh_connection_for_a_printer_off_the_network_or_with_a_rejected_code():
    relay, _db, disc, clock = make_relay()
    disc.announce(PRINTER.serial, PRINTER.host)   # its stored address, so dropping off the network doesn't change it
    relay.tick()
    conn = FakeConnection.created[-1]
    conn.snap = Snapshot(connected=False, error="Access code rejected. Check the access code shown on the printer's screen.")
    clock.advance(STUCK_RECONNECT_S * 2)
    relay.tick()
    assert FakeConnection.created[-1] is conn                  # a new one would be refused too

    conn.snap = Snapshot(connected=False)
    disc.announcements.clear()                                 # powered off: not on the network
    clock.advance(STUCK_RECONNECT_S * 2)
    relay.tick()
    assert FakeConnection.created[-1] is conn

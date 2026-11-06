"""Shared test data, and fakes for running the relay without Supabase, discovery or printers."""

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from relay.app import Relay
from relay.config import Printer, RelayConfig
from relay.database import DatabaseError
from relay.discovery import Announcement
from relay.printer import Snapshot
from relay.status import build_status

NOW = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)

PRINTER = Printer(
    id="11111111-1111-1111-1111-111111111111", serial="03900D5C2526895", label="FredPrintstone",
    model="A1", maintenance_status="operational", access_code="12345678", host="172.20.10.7",
)

PRINTING = {
    "print": {
        "gcode_state": "RUNNING",
        "mc_percent": 42,
        "mc_remaining_time": 75,
        "subtask_name": "bracket_v2",
        "layer_num": 30,
        "total_layer_num": 120,
        "nozzle_temper": 219.8,
        "nozzle_target_temper": 220,
        "bed_temper": 64.6,
        "bed_target_temper": 65,
        "print_error": 0,
        "hms": [],
        "wifi_signal": "-49dBm",
        "ams": {
            "tray_now": "1",
            "ams": [{"id": "0", "tray": [
                {"id": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF"},
                {"id": "1", "tray_type": "PETG", "tray_color": "FF6A13FF"},
            ]}],
        },
        "vt_tray": {"tray_type": "", "tray_color": "00000000"},
    }
}


def state(gcode_state: str, **fields) -> dict:
    """PRINTING with a different gcode_state and/or fields."""
    return {"print": {**PRINTING["print"], "gcode_state": gcode_state, **fields}}


def record(gcode_state: str = "RUNNING", online: bool = True, at: datetime = NOW, **fields) -> dict:
    return build_status(PRINTER, state(gcode_state, **fields), online, at.timestamp(), now=at)


class FakeDB:
    def __init__(self, printers):
        self.printers = printers
        self.calls = []
        self.fail: dict[str, DatabaseError] = {}   # method name -> error to raise

    def _call(self, name, *args):
        if name in self.fail:
            raise self.fail[name]
        self.calls.append((name, *args))

    def written(self, name):
        return [c[1:] for c in self.calls if c[0] == name]

    def load_printers(self):
        self._call("load_printers")
        return list(self.printers)

    def load_last_status(self, ids):
        return {}

    def load_open_jobs(self, ids):
        return {}

    def upsert_status(self, records): self._call("upsert_status", records)
    def update_printer(self, pid, fields): self._call("update_printer", pid, fields)
    def update_connection(self, pid, fields): self._call("update_connection", pid, fields)
    def insert_events(self, rows): self._call("insert_events", rows)
    def upsert_jobs(self, rows): self._call("upsert_jobs", rows)
    def insert_heartbeat(self, row): self._call("insert_heartbeat", row)
    def upsert_discovered(self, rows): self._call("upsert_discovered", rows)
    def delete_discovered(self, serials): self._call("delete_discovered", serials)
    def close(self): pass


class FakeDiscovery:
    available = True

    def __init__(self):
        self.announcements: dict[str, Announcement] = {}

    def announce(self, serial, ip, model_code="N2S", firmware="01.08.00.00"):
        self.announcements[serial] = Announcement(serial, ip, model_code, "3DP-039-895", firmware, seen_at=0)

    def get(self, serial): return self.announcements.get(serial)
    def recent(self): return list(self.announcements.values())
    def start(self): pass
    def stop(self): pass


class FakeConnection:
    created: ClassVar[list["FakeConnection"]] = []   # every connection made, newest last

    def __init__(self, printer, host, request_full_status, dump_dir):
        self.printer, self.host = printer, host
        self.snap = Snapshot()
        self.started = self.stopped = False
        FakeConnection.created.append(self)

    @property
    def is_connected(self): return self.snap.connected

    @property
    def error(self): return self.snap.error

    def report(self, st, at):
        self.snap = Snapshot(state=st, connected=True, last_message_at=at, first_unread_at=at)

    def snapshot(self):
        snap, self.snap = self.snap, dataclasses.replace(self.snap, first_unread_at=None)
        return snap

    def start(self): self.started = True
    def stop(self): self.stopped = True
    def request_full_status(self): pass


class Clock:
    def __init__(self):
        self.mono = 1000.0
        self.wall = datetime(2026, 10, 2, 14, 0, 40, tzinfo=UTC)

    def advance(self, seconds):
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)


def make_relay(printers=(PRINTER,), connection_factory=FakeConnection, light_state_path=None, **config):
    """A Relay wired to fakes. `config` sets RelayConfig fields, e.g. camera_light="auto"."""
    FakeConnection.created = []
    db, disc, clock = FakeDB(list(printers)), FakeDiscovery(), Clock()
    relay = Relay(RelayConfig(supabase_url="x", supabase_key="x", **config), db, disc,
                  connection_factory=connection_factory, clock=lambda: clock.mono, wall_clock=lambda: clock.wall,
                  light_state_path=light_state_path)
    return relay, db, disc, clock

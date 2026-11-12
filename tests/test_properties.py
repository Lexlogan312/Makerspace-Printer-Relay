"""Property-based tests (Hypothesis): rules that must hold for any input, checked against hundreds
of generated cases instead of a few hand-picked ones. When one fails, Hypothesis prints the
smallest input it found that breaks the rule."""

import re
import tempfile
from datetime import timedelta
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from relay.config import load_config
from relay.discovery import parse_announcement
from relay.history import CANCELED_ERROR, PrinterHistory, format_error
from relay.printer import deep_merge
from relay.status import (
    FILAMENT_COLORS,
    GCODE_STATE_TO_STATUS,
    HUE_FAMILIES,
    PROGRESS_FIELDS,
    VOLATILE_FIELDS,
    build_status,
    color_family,
    color_name,
    fingerprint,
)
from tests.fixtures import NOW, PRINTER

GCODE_STATES = [*GCODE_STATE_TO_STATUS, "UNKNOWN_STATE", None]
STATUSES = {*GCODE_STATE_TO_STATUS.values(), "unknown", "offline"}
FAMILIES = {family for _, family in HUE_FAMILIES} | {"black", "white", "gray", "beige", "brown", "pink"}



def hex_digits(n: int):
    """n hex digits, either case. (Built from characters rather than a regex: regex strategies
    are slow to set up, which trips Hypothesis's speed check on slow hardware like a Pi 3B+.)"""
    return st.text("0123456789abcdefABCDEF", min_size=n, max_size=n)


# ───────────── Raw printer reports ─────────────
# Printers report numbers as ints, floats or strings, and sometimes leave fields out or send junk.
numberish = st.one_of(st.none(), st.integers(0, 100_000), st.floats(-50, 1_000, allow_nan=False),
                      st.integers(0, 100_000).map(str), st.text(max_size=5))
tray_color = st.one_of(st.none(), st.text(max_size=10),
                       hex_digits(8), hex_digits(6))
tray = st.fixed_dictionaries({"id": st.one_of(st.integers(0, 3).map(str), st.none(), st.text(max_size=2))},
                             optional={"tray_type": st.sampled_from(["PLA", "PETG", "", None]), "tray_color": tray_color})
ams = st.fixed_dictionaries({}, optional={
    "tray_now": st.one_of(st.none(), st.sampled_from(["0", "1", "5", "254", "255"]), st.text(max_size=3)),
    "ams": st.lists(st.fixed_dictionaries({"id": st.sampled_from(["0", "1"])},
                                          optional={"tray": st.lists(tray, max_size=4)}), max_size=2),
})
print_report = st.fixed_dictionaries({}, optional={
    "gcode_state": st.sampled_from(GCODE_STATES),
    "mc_percent": numberish, "mc_remaining_time": numberish, "layer_num": numberish,
    "total_layer_num": numberish, "nozzle_temper": numberish, "bed_temper": numberish,
    "print_error": st.one_of(st.none(), st.integers(0, 2**32 - 1)),
    "subtask_name": st.one_of(st.none(), st.text(max_size=20)),
    "wifi_signal": st.one_of(st.none(), st.text(max_size=8)),
    "ams": ams,
    "vt_tray": tray,
})


@given(report=print_report, connected=st.booleans(),
       last_message_at=st.one_of(st.none(), st.floats(1e9, 2e9)))
def test_build_status_handles_any_report(report, connected, last_message_at):
    r = build_status(PRINTER, {"print": report}, connected, last_message_at, now=NOW)

    assert r["status"] in STATUSES
    assert (r["status"] == "offline") == (not connected)
    if r["status"] not in ("printing", "paused"):
        assert r["progress_percent"] is None and r["remaining_minutes"] is None
    if r["status"] != "printing":
        assert r["estimated_completion"] is None
    assert isinstance(r["print_error"], int)
    color = r["filament_color"]
    assert color is None or re.fullmatch(r"#[0-9A-F]{6}", color)
    assert (r["filament_color_name"] is None) == (color is None)  # every real color gets a name


# ───────────── Filament colors ─────────────
hex_color = hex_digits(6).map(lambda h: "#" + h)


@given(hex_color)
def test_every_color_gets_a_known_name(color):
    name = color_name(color)
    assert name in FILAMENT_COLORS or name.lower() in FAMILIES


@given(st.sampled_from(sorted(FILAMENT_COLORS.items())), st.booleans())
def test_an_exact_elegoo_color_keeps_its_own_name(item, lower):
    name, color = item
    assert color_name(color.lower() if lower else color) == name


@given(st.tuples(st.integers(0, 255), st.integers(0, 255), st.integers(0, 255)))
def test_every_rgb_has_a_color_family(rgb):
    assert color_family(rgb) in FAMILIES


# ───────────── Change detection ─────────────
record_value = st.one_of(st.none(), st.integers(0, 1000), st.text(max_size=6), st.booleans())
record = st.dictionaries(st.sampled_from(sorted(VOLATILE_FIELDS | PROGRESS_FIELDS | {"status", "job_name", "online"})),
                         record_value)


@given(record, st.sampled_from(sorted(VOLATILE_FIELDS)), record_value)
def test_volatile_fields_never_count_as_a_change(r, key, value):
    assert fingerprint({**r, key: value}) == fingerprint(r)


@given(record, st.sampled_from(sorted(PROGRESS_FIELDS)), st.integers(0, 1000), st.integers(0, 1000))
def test_progress_changing_alone_is_not_a_change(r, key, a, b):
    assert fingerprint({**r, key: a}) == fingerprint({**r, key: b})


@given(record, st.sampled_from(["status", "job_name", "online"]), record_value, record_value)
def test_a_visible_field_changing_is_a_change(r, key, a, b):
    if repr(a) != repr(b):
        assert fingerprint({**r, key: a}) != fingerprint({**r, key: b})


# ───────────── Merging partial reports ─────────────
json_value = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=5)),
    lambda children: st.one_of(st.lists(children, max_size=3), st.dictionaries(st.text(max_size=3), children, max_size=3)),
    max_leaves=10,
)
json_object = st.dictionaries(st.text(max_size=3), json_value, max_size=4)


@given(json_object, json_object)
def test_merging_a_report_is_idempotent(base, update):
    once = deep_merge(deep_merge({}, base), update)
    twice = deep_merge(deep_merge(deep_merge({}, base), update), update)
    assert once == twice


@given(json_object, json_object)
def test_merging_keeps_untouched_fields_and_applies_new_ones(base, update):
    merged = deep_merge(deep_merge({}, base), update)
    for key, value in base.items():
        if key not in update:
            assert merged[key] == value
    for key, value in update.items():
        if not (isinstance(value, dict) and isinstance(base.get(key), dict)):
            assert merged[key] == value


@given(json_object, json_object)
def test_merged_state_doesnt_share_data_with_the_report(base, update):
    merged = deep_merge(deep_merge({}, base), update)
    snapshot = repr(merged)
    for value in update.values():
        if isinstance(value, list):
            value.append("changed later")
        elif isinstance(value, dict):
            value["changed later"] = True
    assert repr(merged) == snapshot


# ───────────── Print history ─────────────
observation = st.fixed_dictionaries({
    "online": st.booleans(),
    "gcode_state": st.sampled_from(GCODE_STATES),
    "print_error": st.sampled_from([0, 0, CANCELED_ERROR, 0x05008051]),
    "job_name": st.sampled_from([None, "", "bracket.3mf", "gear.3mf"]),
    "minutes_later": st.integers(1, 120),
})


@settings(max_examples=300)
@given(st.lists(observation, max_size=40))
def test_history_never_breaks_its_rules(observations):
    h = PrinterHistory(PRINTER.id)
    at = NOW
    open_job, closed, last_state = None, set(), None
    for o in observations:
        at += timedelta(minutes=o["minutes_later"])
        rec = {"online": o["online"], "gcode_state": o["gcode_state"], "print_error": o["print_error"],
               "job_name": o["job_name"] or None, "label": PRINTER.label, "filament_type": "PLA",
               "filament_color": "#FFFFFF", "filament_color_name": "White"}
        events, jobs = h.observe(rec, at)

        for e in events:
            assert e["kind"] in ("connection", "state", "error")
            if e["kind"] == "state":  # state changes chain: each starts where the last one ended
                assert e["from_value"] == last_state and e["to_value"] != e["from_value"]
                last_state = e["to_value"]

        for job in jobs:
            assert job["id"] not in closed  # a finished print is never reopened or rewritten
            if job["ended_at"] is None:
                assert open_job in (None, job["id"])  # never two prints open at once
                open_job = job["id"]
            else:
                assert job["outcome"] in ("finished", "failed", "canceled", "unknown")
                assert job["ended_at"] >= job["started_at"]
                if job["outcome"] == "canceled":
                    assert rec["gcode_state"] == "FAILED" and rec["print_error"] == CANCELED_ERROR
                closed.add(job["id"])
                open_job = None if open_job == job["id"] else open_job


@given(st.integers(0, 2**32 - 1))
def test_error_codes_format_and_read_back(code):
    text = format_error(code)
    assert len(text) == 9 and text[4] == "-"
    assert int(text.replace("-", ""), 16) == code


# ───────────── Network announcements ─────────────
@given(st.binary(max_size=300), st.ip_addresses(v=4).map(str))
def test_any_packet_is_safe_to_parse(data, sender):
    a = parse_announcement(data, sender, 0.0)
    assert a is None or (a.ip == sender and a.serial)


header_text = st.text(st.characters(min_codepoint=33, max_codepoint=126), min_size=1, max_size=15)


@given(header_text, header_text, header_text, st.ip_addresses(v=4).map(str))
def test_a_real_announcement_reads_back(serial, model, name, ip):
    packet = (f"NOTIFY * HTTP/1.1\r\nLocation: {ip}\r\nNT: urn:bambulab-com:device:3dprinter:1\r\n"
              f"USN: {serial}\r\nDevModel.bambu.com: {model}\r\nDevName.bambu.com: {name}\r\n\r\n").encode()
    a = parse_announcement(packet, ip, 1.0)
    assert (a.serial, a.ip, a.model_code, a.dev_name) == (serial, ip, model, name)


@given(header_text, st.ip_addresses(v=4).map(str), st.ip_addresses(v=4).map(str))
def test_an_announcement_cant_point_at_another_address(serial, claimed, sender):
    packet = f"NOTIFY * HTTP/1.1\r\nLocation: {claimed}\r\nNT: bambulab\r\nUSN: {serial}\r\n\r\n".encode()
    a = parse_announcement(packet, sender, 1.0)
    assert (a is not None) == (claimed == sender)


# ───────────── Settings ─────────────
@settings(max_examples=50)
@given(heartbeat=st.floats(-100, 1000, allow_nan=False), interval=st.floats(-100, 1000, allow_nan=False),
       idle=st.floats(-100, 5000, allow_nan=False), watch=st.floats(-100, 100, allow_nan=False),
       watch_max=st.integers(-50, 100))
def test_settings_below_their_minimums_are_raised(heartbeat, interval, idle, watch, watch_max):
    toml = (f'[supabase]\nurl = "https://x.supabase.co"\nkey = "sb_secret_test"\n'
            f"[relay]\nstatus_heartbeat_s = {heartbeat}\n"
            f"[camera]\ninterval_s = {interval}\nidle_interval_s = {idle}\n"
            f"watch_interval_s = {watch}\nwatch_max = {watch_max}\n")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "relay.toml"
        path.write_text(toml)
        c = load_config(path)
    assert c.status_heartbeat_s >= 30 and c.camera_interval_s >= 15
    assert c.camera_idle_interval_s >= 60 and c.camera_watch_interval_s >= 3
    assert 1 <= c.camera_watch_max <= 20

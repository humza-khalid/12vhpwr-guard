"""v1.3: shutdown-reason notice, flight recorder, pause reminder, plus regressions
for the two review fixes (single flight dump on same-tick engage+shutdown, no false
marker-file log line when shutdown fires without engagement).

Suite A unit-tests the pieces directly. Suite B drives the real monitor_loop with a
fake sensor and stubbed shutdown. Nothing here can shut the machine down or touch
the GPU.
"""

import glob
import json
import os
import time

import pytest

from helpers import CountingLogger, FakeBackend, FakeMitigator, run_loop, running_loop, wait_until


@pytest.fixture
def loop(loop_env):
    """Suite B here needs the note that the real shutdown_windows leaves behind."""
    loop_env.note_on_shutdown = True
    return loop_env


# =========================================================================
# Suite A: the pieces on their own
# =========================================================================
def test_a1_shutdown_note_round_trip(guard, monkeypatch):
    """A1: shutdown note round trip."""
    g, toasts = guard.g, guard.toasts
    log = CountingLogger()

    # The one call to the real shutdown_windows in the whole suite. subprocess.run
    # is stubbed out first, so the shutdown.exe it builds is never launched.
    monkeypatch.setattr(g.subprocess, "run", lambda *a, **k: None)
    g.shutdown_windows(log, "Tier 1: test reason 11.2A")

    assert os.path.exists(g.LAST_SHUTDOWN_PATH), "note file written by shutdown"
    with open(g.LAST_SHUTDOWN_PATH, encoding="utf-8") as f:
        data = json.load(f)
    assert "test reason" in data.get("reason", ""), f"note carries the reason: {data}"
    assert bool(data.get("time_human")), "note carries a human time"

    toasts.clear()
    log2 = CountingLogger()
    g.announce_last_shutdown(log2)
    assert any("emergency stop" in w for w in log2.records["warning"]), \
        "announce warns in the log"
    assert any("test reason" in w for w in log2.records["warning"]), \
        "announce includes the reason"
    assert len(toasts) == 1 and "read this" in toasts[0][0], "announce toasts"
    assert not os.path.exists(g.LAST_SHUTDOWN_PATH), "note deleted after announce"

    toasts.clear()
    g.announce_last_shutdown(log2)
    assert not toasts, "no note -> silent"

    with open(g.LAST_SHUTDOWN_PATH, "w", encoding="utf-8") as f:
        f.write("{broken json")
    toasts.clear()
    log3 = CountingLogger()
    g.announce_last_shutdown(log3)
    assert len(toasts) == 1, "corrupt note still announces"
    assert any("sustained overcurrent" in w for w in log3.records["warning"]), \
        "corrupt note uses generic wording"
    assert not os.path.exists(g.LAST_SHUTDOWN_PATH), "corrupt note cleaned up"


def test_a2_flight_recorder(guard):
    """A2: flight recorder."""
    g, tmp = guard.g, str(guard.tmp)
    fr = g.FlightRecorder(maxlen=120)
    log4 = CountingLogger()

    assert fr.dump(log4, "engaged") is None, "empty buffer -> no file"

    pins = [(f"GPU 12VHPWR Pin{i} Current", 0.5 + i * 0.01, "A") for i in range(1, 7)]
    for _n in range(200):
        fr.record(pins)
    assert len(fr._buf) == 120, "ring buffer caps at maxlen"

    path = fr.dump(log4, "engaged")
    assert path is not None and os.path.exists(path), f"dump returns a path: {path}"
    lines = open(path, encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 121, f"csv has header + 120 rows: {len(lines)}"
    assert lines[0] == "time,pin1,pin2,pin3,pin4,pin5,pin6", f"header names six pins: {lines[0]}"
    assert lines[1].count(",") == 6, f"rows carry six values: {lines[1]}"
    assert any("Flight recorder" in m for m in log4.records["info"]), "dump logged"

    for i in range(15):
        time.sleep(0.01)
        fr.dump(log4, f"e{i}")
    flights = glob.glob(os.path.join(tmp, "flight_*.csv"))
    assert len(flights) <= g.FLIGHT_KEEP_FILES, \
        f"retention keeps at most FLIGHT_KEEP_FILES: {len(flights)}"


# =========================================================================
# Suite B: the real monitor_loop, on compressed timings
# =========================================================================
def test_b1_engagement_dumps_a_flight_file(loop):
    """B1: engagement dumps a flight file."""
    tmp, shutdowns = str(loop.tmp), loop.shutdowns
    FakeBackend.amps = 10.0

    def until_engaged():
        if glob.glob(os.path.join(tmp, "flight_*_engaged.csv")):
            FakeBackend.amps = 1.0
            return True
        return False

    run_loop(15.0, until=until_engaged)

    engaged_files = glob.glob(os.path.join(tmp, "flight_*_engaged.csv"))
    assert len(engaged_files) == 1, f"flight_*_engaged.csv written: {engaged_files}"
    body = open(engaged_files[0], encoding="utf-8").read()
    assert "10.000" in body, "dump captured the overcurrent"
    assert not shutdowns, f"no shutdown in B1: {shutdowns}"


def test_b2_b3_shutdown_note_written_then_announced(loop):
    """B2: shutdown dumps a flight file and leaves the note.
    B3: next start announces and clears the note.

    One test because B3 is the next start after B2: the note it announces is the
    one B2's shutdown left behind, which is the whole round trip.
    """
    g, tmp = loop.g, str(loop.tmp)
    toasts, shutdowns = loop.toasts, loop.shutdowns

    g.set_response_mode(g.RESPONSE_SHUTDOWN_ONLY)
    FakeBackend.amps = 13.5
    run_loop(15.0, until=lambda: bool(shutdowns))

    assert len(shutdowns) == 1, f"B2 shutdown fired: {shutdowns}"
    assert len(glob.glob(os.path.join(tmp, "flight_*_shutdown.csv"))) == 1, \
        "B2 flight_*_shutdown.csv written"
    assert os.path.exists(g.LAST_SHUTDOWN_PATH), "B2 shutdown note written"

    toasts.clear()
    FakeBackend.amps = 0.5
    run_loop(6.0, until=lambda: any("read this" in t[0] for t in toasts))

    assert any("read this" in t[0] for t in toasts), \
        f"B3 startup announced the shutdown: {toasts}"
    assert any("Tier 2" in t[1] for t in toasts), "B3 announcement names the tier reason"
    assert not os.path.exists(g.LAST_SHUTDOWN_PATH), "B3 note cleared"


def test_b4a_tier3_same_tick_dumps_exactly_one_flight_file(loop):
    """B4a: tier 3 same tick -> exactly one flight file."""
    g, tmp, shutdowns = loop.g, str(loop.tmp), loop.shutdowns
    g.set_response_mode(g.RESPONSE_TIERED)
    FakeBackend.amps = 17.0

    run_loop(15.0, until=lambda: bool(shutdowns))

    assert len(shutdowns) == 1 and "Tier 3" in shutdowns[0], \
        f"tier 3 shutdown fired: {shutdowns}"
    assert len(glob.glob(os.path.join(tmp, "flight_*_shutdown.csv"))) == 1, \
        "exactly one shutdown dump"
    assert len(glob.glob(os.path.join(tmp, "flight_*_engaged.csv"))) == 0, \
        "no duplicate engaged dump in the same tick"


def test_b4b_not_engaged_shutdown_never_logs_the_marker_claim(loop):
    """B4b: not-engaged shutdown never logs the marker claim."""
    shutdowns = loop.shutdowns
    FakeMitigator.AVAILABLE = False
    FakeBackend.amps = 13.5
    try:
        log = run_loop(15.0, until=lambda: bool(shutdowns))
    finally:
        FakeMitigator.AVAILABLE = True

    assert len(shutdowns) == 1, f"shutdown fired without engagement: {shutdowns}"
    assert not any("Leaving the GPU limited" in m for m in log.records["info"]), \
        ("no false 'Leaving the GPU limited' log line: "
         f"{[m for m in log.records['info'] if 'Leaving' in m]}")


def test_b4c_engaged_shutdown_still_logs_the_marker_claim(loop):
    """B4c: engaged shutdown still logs the marker claim."""
    shutdowns = loop.shutdowns
    FakeBackend.amps = 10.0

    log = run_loop(18.0, until=lambda: bool(shutdowns))

    assert len(shutdowns) == 1 and "Tier 1" in shutdowns[0], \
        f"tier 1 engaged then shut down: {shutdowns}"
    assert any("Leaving the GPU limited" in m for m in log.records["info"]), \
        f"engaged-through-shutdown logs the marker line: {log.records['info'][-3:]}"


def test_b5_pause_reminder_fires_and_resume_logs(loop):
    """B5: pause reminder fires and resume logs."""
    g, toasts = loop.g, loop.toasts
    g.set_response_mode(g.RESPONSE_TIERED)
    g.PAUSE_REMINDER_EVERY_SEC = 0.3
    toasts.clear()
    g.set_state(paused=True)

    # The loop thread appends to both lists while the test reads them, so every
    # read takes a copy first.
    def reminders():
        return [x for x in list(toasts) if "still paused" in x[0]]

    with running_loop() as logger:
        # The paused branch ticks every 0.5s, so two reminders take about a second
        # here. The deadline is only there to fail rather than hang.
        got_two = wait_until(lambda: len(reminders()) >= 2, 15.0)
        assert got_two, f"reminder toasts while paused: {len(reminders())}"
        assert any("unprotected" in w for w in list(logger.records["warning"])), \
            "reminder warns in log"

        g.set_state(paused=False)
        resumed = wait_until(
            lambda: any("resumed after" in m for m in list(logger.records["info"])), 10.0)

    assert resumed, "resume logged with duration"

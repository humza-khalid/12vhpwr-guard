"""F3: the guard restarts itself after a long loss of the direct sensor (B-003).

The PR #1 experiment bounced the NVIDIA driver under a running guard. The NVAPI
session the direct backend holds died and never came back inside that process: 131
reconnect attempts in a row, all of them failed, and the card was unprotected until
someone restarted the scheduled task by hand. The installed task already restarts a
failed run (999 times, a minute apart), so all the guard has to do is stop being a
successful one.

Suite A covers the exit plumbing on its own, suite B the config key, and suite C runs
the real monitor_loop against a fake sensor that dies on cue.

Nothing here can end the test process. request_exit only records a code and stops a
stub icon; sys.exit lives in main(), which no test calls.
"""

import contextlib
import threading
import time
import types

import pytest

from helpers import JOIN_TIMEOUT, CountingLogger, FakeBackend, run_loop, wait_until

import hwinfo_12vhpwr_guard as g

# The event id test needs the real win32con constant, which only exists when pywin32
# imported. HAVE_EVENTLOG is forced False by the guard fixture, so the module
# attribute is what says whether the constant is reachable.
HAVE_WIN32CON = getattr(g, "win32con", None) is not None

# Compressed windows. The reconnect retry comes down with them: it is the cadence the
# restart clock gets looked at, so a negative test is only worth something if the loop
# had plenty of chances to fire during it.
WINDOW = 0.5
SLOW_WINDOW = 1.0   # where a test has to tell one clock from another
RETRY = 0.05


class FakeIcon:
    """Stands in for the pystray icon that request_exit has to stop."""

    def __init__(self, raises=False):
        self.stops = 0
        self._raises = raises

    def stop(self):
        self.stops += 1
        if self._raises:
            raise RuntimeError("icon already gone")


def compress(monkeypatch, window=WINDOW, retry=RETRY):
    """Squeeze the restart window and the reconnect cadence into a test's lifetime."""
    monkeypatch.setattr(g, "SENSOR_LOSS_RESTART_SEC", window)
    monkeypatch.setattr(g, "RECONNECT_RETRY_SEC", retry)


def connected():
    return bool(g.get_state().get("source"))


def blind():
    return not g.get_state().get("source")


def exit_code():
    return g.get_state().get("exit_code")


def lose_the_sensor():
    """The driver takes the session away and refuses every reconnect after it."""
    FakeBackend.gone = True
    FakeBackend.connect_ok = False


def give_it_back():
    FakeBackend.gone = False
    FakeBackend.connect_ok = True


@contextlib.contextmanager
def loop_thread():
    """The real monitor_loop in a thread, with its stop event visible.

    helpers.run_loop keeps its stop event to itself, and half of F3 is about who sets
    that event, so this suite runs the loop itself.
    """
    stop = threading.Event()
    logger = CountingLogger()
    t = threading.Thread(target=g.monitor_loop, args=(stop, logger), daemon=True)
    t.start()
    try:
        yield types.SimpleNamespace(stop=stop, log=logger, thread=t)
    finally:
        stop.set()
        t.join(timeout=JOIN_TIMEOUT)


# =========================================================================
# Suite A: the exit plumbing
# =========================================================================
def test_a1_request_exit_without_a_tray_icon(guard, monkeypatch):
    """A1: the loop can ask to exit before the tray exists, and under tests."""
    monkeypatch.setattr(g, "TRAY_ICON", None)
    stop = threading.Event()

    g.request_exit(1, stop)

    assert g.get_state()["exit_code"] == 1, "the code is recorded for main()"
    assert stop.is_set(), "and the monitor loop is told to stop"
    assert g.pending_exit_code() == 1


def test_a2_request_exit_stops_the_tray_icon(guard, monkeypatch):
    """A2: stopping the icon is what returns main() from icon.run()."""
    icon = FakeIcon()
    monkeypatch.setattr(g, "TRAY_ICON", icon)
    stop = threading.Event()

    g.request_exit(1, stop)

    assert icon.stops == 1
    assert stop.is_set()
    assert g.pending_exit_code() == 1


def test_a3_a_failing_icon_does_not_take_the_exit_with_it(guard, monkeypatch):
    """A3: the exit code still stands if the icon is already gone."""
    icon = FakeIcon(raises=True)
    monkeypatch.setattr(g, "TRAY_ICON", icon)

    g.request_exit(1)

    assert icon.stops == 1
    assert g.pending_exit_code() == 1


def test_a4_pending_exit_code_defaults_to_zero(guard):
    """A4: nothing asked for a code, so the process ends cleanly."""
    assert g.pending_exit_code() == 0

    g.set_state(exit_code=None)
    assert g.pending_exit_code() == 0, "and junk cannot break the exit"
    g.set_state(exit_code="nonsense")
    assert g.pending_exit_code() == 0


def test_a5_a_user_exit_stays_at_zero(guard, monkeypatch):
    """A5: Exit from the tray must not look like a failure to Task Scheduler."""
    monkeypatch.setattr(g, "TRAY_ICON", None)
    icon = FakeIcon()
    stop = threading.Event()

    g.on_exit(icon, None, stop)

    assert stop.is_set() and icon.stops == 1
    assert g.pending_exit_code() == 0, "0 means the scheduler leaves it alone"


@pytest.mark.parametrize("seconds,text", [
    (300.0, "5 minutes"),
    (600.0, "10 minutes"),
    (120.0, "2 minutes"),
    (60.0, "1 minute"),
    (30.0, "30 seconds"),
    (0.5, "0.5 seconds"),
])
def test_a6_the_toast_says_the_window_in_words(seconds, text):
    """A6: the toast quotes the configured window, not a hardcoded five minutes."""
    assert g._span_text(seconds) == text


# =========================================================================
# Suite B: the config key
# =========================================================================
def write_config(tmp, body):
    path = tmp / "config.ini"
    path.write_text("[Settings]\n" + body, encoding="utf-8")
    return path


def test_b1_the_key_defaults_to_five_minutes(guard):
    """B1: no file, no key, five minutes."""
    assert g.DEFAULT_SENSOR_LOSS_RESTART_SECONDS == 300.0
    assert g.load_config()["sensor_loss_restart_seconds"] == 300.0


def test_b2_the_key_is_read_from_the_file(guard):
    write_config(guard.tmp, "sensor_loss_restart_seconds = 120\n")
    assert g.load_config()["sensor_loss_restart_seconds"] == 120.0


def test_b3_zero_survives_the_loader(guard):
    """B3: 0 is the documented off switch, so it cannot be treated as a bad value."""
    write_config(guard.tmp, "sensor_loss_restart_seconds = 0\n")
    assert g.load_config()["sensor_loss_restart_seconds"] == 0.0


def test_b4_a_negative_falls_back_to_the_default(guard):
    write_config(guard.tmp, "sensor_loss_restart_seconds = -5\n")
    assert g.load_config()["sensor_loss_restart_seconds"] == 300.0


def test_b5_junk_falls_back_to_the_default(guard):
    write_config(guard.tmp, "sensor_loss_restart_seconds = soon\n")
    assert g.load_config()["sensor_loss_restart_seconds"] == 300.0


# =========================================================================
# Suite C: the real monitor_loop
# =========================================================================
def test_c1_the_direct_sensor_staying_gone_restarts_the_process(loop_env, monkeypatch):
    """C1: connected on the direct sensor, then blind for the whole window."""
    gm = loop_env.g
    icon = FakeIcon()
    compress(monkeypatch)
    monkeypatch.setattr(gm, "TRAY_ICON", icon)
    FakeBackend.key = "astral"

    with loop_thread() as run:
        assert wait_until(connected, 5.0), "the direct sensor connected first"
        lose_the_sensor()

        assert wait_until(lambda: exit_code() == 1, WINDOW * 20), \
            f"it asked for exit code 1: {gm.get_state()}"
        assert run.stop.is_set(), "and stopped the monitor loop"
        run.thread.join(timeout=JOIN_TIMEOUT)

    assert icon.stops == 1, "the tray icon was stopped so main() can return"

    errors = [e for e in run.log.records["error"] if "Restarting to recover" in e]
    assert errors, f"the restart is an ERROR line: {run.log.records['error']}"
    assert "after the direct sensor was lost" in errors[0], errors[0]

    toasts = [t for t in loop_env.toasts if "restarting" in t[0]]
    assert toasts, f"and a toast: {loop_env.toasts}"
    assert "restarting to recover it" in toasts[0][1], toasts[0]

    assert any("12VHPWR Guard stopped." in i for i in run.log.records["info"]), \
        f"the normal stop path still ran: {run.log.records['info']}"
    assert not loop_env.shutdowns, "nothing here shuts the machine down"
    assert all(not m.engaged for m in loop_env.made), "and nothing is left limited"


@pytest.mark.skipif(not HAVE_WIN32CON, reason="pywin32 not installed")
def test_c2_the_restart_writes_event_9002(loop_env, monkeypatch):
    """C2: 9002, error type, so a restart is visible in Event Viewer."""
    gm = loop_env.g
    events = []
    compress(monkeypatch)
    monkeypatch.setattr(gm, "TRAY_ICON", FakeIcon())
    monkeypatch.setattr(gm, "HAVE_EVENTLOG", True)
    monkeypatch.setattr(gm, "eventlog_register_source", lambda logger: None)
    monkeypatch.setattr(
        gm, "eventlog_write",
        lambda event_type, event_id, message: events.append((event_type, event_id, message)),
    )
    FakeBackend.key = "astral"

    with loop_thread() as run:
        assert wait_until(connected, 5.0)
        lose_the_sensor()
        assert wait_until(lambda: exit_code() == 1, WINDOW * 20)
        run.thread.join(timeout=JOIN_TIMEOUT)

    nines = [e for e in events if e[1] == 9002]
    assert nines, f"one 9002 for the restart: {[e[1] for e in events]}"
    assert nines[0][0] == gm.win32con.EVENTLOG_ERROR_TYPE, "an error entry"
    assert "Restarting to recover the sensor path" in nines[0][2], nines[0][2]
    assert any(e[1] == 1003 for e in events), "and the normal stopped entry still lands"


def test_c3_an_hwinfo_only_session_never_restarts(loop_env, monkeypatch):
    """C3: closing HWiNFO must never turn into a restart loop."""
    gm = loop_env.g
    compress(monkeypatch)
    monkeypatch.setattr(gm, "TRAY_ICON", FakeIcon())
    FakeBackend.key = "hwinfo"

    with loop_thread() as run:
        assert wait_until(connected, 5.0)
        lose_the_sensor()
        assert wait_until(blind, 5.0), "the loss was noticed"

        time.sleep(WINDOW * 3)
        assert exit_code() == 0, "it waits purple, exactly as it did before"
        assert not run.stop.is_set()
        assert run.thread.is_alive()

    assert any("Sensor source lost" in w for w in run.log.records["warning"]), \
        f"the sensor really did go: {run.log.records['warning']}"


def test_c4_a_session_that_never_connected_never_restarts(loop_env, monkeypatch):
    """C4: an unsupported card has no direct session to recover."""
    gm = loop_env.g
    compress(monkeypatch)
    monkeypatch.setattr(gm, "TRAY_ICON", FakeIcon())
    FakeBackend.key = "astral"
    FakeBackend.connect_ok = False

    with loop_thread() as run:
        assert wait_until(lambda: gm.get_state()["status"] == gm.Status.ERROR, 5.0)

        time.sleep(WINDOW * 3)
        assert exit_code() == 0, "restarting would only fail again"
        assert run.thread.is_alive()

    assert any("Waiting for a pin-current source" in i for i in run.log.records["info"]), \
        f"it really did try to connect: {run.log.records['info']}"
    assert gm.get_state()["last_message"] == "Waiting for pin sensors...", \
        "and it is sitting purple, not restarting"


def test_c5_a_reconnect_starts_the_clock_again(loop_env, monkeypatch):
    """C5: lost, back, lost again. The second outage gets a full window of its own."""
    gm = loop_env.g
    compress(monkeypatch, window=SLOW_WINDOW)
    monkeypatch.setattr(gm, "TRAY_ICON", FakeIcon())
    FakeBackend.key = "astral"

    with loop_thread() as run:
        assert wait_until(connected, 5.0)

        lose_the_sensor()
        assert wait_until(blind, 5.0)
        time.sleep(SLOW_WINDOW * 0.5)
        assert exit_code() == 0, "half a window is not a window"

        give_it_back()
        assert wait_until(connected, 5.0), "it reconnected"
        assert exit_code() == 0

        lose_the_sensor()
        assert wait_until(blind, 5.0)
        second_loss = time.time()
        assert wait_until(lambda: exit_code() == 1, SLOW_WINDOW * 10), \
            f"the second outage restarts too: {gm.get_state()}"
        blind_for = time.time() - second_loss

    assert blind_for >= SLOW_WINDOW * 0.6, (
        f"it waited a full window from the second loss, not the leftover of the "
        f"first: {blind_for:.2f}s")


def test_c6_a_fallback_to_hwinfo_counts_as_a_source(loop_env, monkeypatch):
    """C6: direct, then HWiNFO, then nothing at all.

    The fallback connection stops the clock (the card is protected again), and losing
    that too restarts, because the session has no source and the direct path is the
    one a fresh process can fix.
    """
    gm = loop_env.g
    compress(monkeypatch, window=SLOW_WINDOW)
    monkeypatch.setattr(gm, "TRAY_ICON", FakeIcon())
    FakeBackend.key = "astral"

    with loop_thread() as run:
        assert wait_until(connected, 5.0), "on the direct sensor"

        lose_the_sensor()
        assert wait_until(blind, 5.0)
        FakeBackend.key = "hwinfo"
        give_it_back()
        assert wait_until(connected, 5.0), "now on the fallback"
        assert exit_code() == 0, "a fallback source is a source"

        lose_the_sensor()
        assert wait_until(lambda: exit_code() == 1, SLOW_WINDOW * 10), \
            f"no source at all, and a direct session was lost earlier: {gm.get_state()}"
        run.thread.join(timeout=JOIN_TIMEOUT)

    assert any("Restarting to recover" in e for e in run.log.records["error"])


def test_c7_a_paused_guard_does_not_restart(loop_env, monkeypatch):
    """C7: pause hands the decision back to the user, and resume starts the clock over."""
    gm = loop_env.g
    compress(monkeypatch, window=SLOW_WINDOW)
    monkeypatch.setattr(gm, "TRAY_ICON", FakeIcon())
    FakeBackend.key = "astral"

    with loop_thread() as run:
        assert wait_until(connected, 5.0)
        lose_the_sensor()
        assert wait_until(blind, 5.0)

        gm.set_state(paused=True)
        assert wait_until(lambda: gm.get_state()["status"] == gm.Status.PAUSED, 5.0)
        time.sleep(SLOW_WINDOW * 3)
        assert exit_code() == 0, "paused time is not blind time"

        gm.set_state(paused=False)
        resumed = time.time()
        assert wait_until(lambda: exit_code() == 1, SLOW_WINDOW * 10), \
            f"and it restarts once it is watching again: {gm.get_state()}"
        after_resume = time.time() - resumed

    assert after_resume >= SLOW_WINDOW * 0.8, (
        f"the clock started over at the resume rather than carrying the pause: "
        f"{after_resume:.2f}s")
    assert any("Monitoring resumed" in i for i in run.log.records["info"])


def test_c8_zero_turns_the_whole_thing_off(loop_env, monkeypatch):
    """C8: sensor_loss_restart_seconds = 0 waits forever, the way v1.3 did."""
    gm = loop_env.g
    compress(monkeypatch, window=0.0)
    monkeypatch.setattr(gm, "TRAY_ICON", FakeIcon())
    FakeBackend.key = "astral"

    with loop_thread() as run:
        assert wait_until(connected, 5.0)
        lose_the_sensor()
        assert wait_until(blind, 5.0)

        time.sleep(WINDOW * 3)
        assert exit_code() == 0, "switched off means it keeps waiting"
        assert run.thread.is_alive()

    assert any("Self-restart after direct sensor loss: off" in i
               for i in run.log.records["info"]), \
        f"the startup line says so: {run.log.records['info']}"


def test_c9_the_startup_line_names_the_window(loop_env, monkeypatch):
    """C9: five minutes, in the log, before anything has gone wrong."""
    gm = loop_env.g
    monkeypatch.setattr(gm, "SENSOR_LOSS_RESTART_SEC", 300.0)
    FakeBackend.key = "astral"

    log = run_loop(10.0, until=connected)

    assert any("Self-restart after direct sensor loss: 300 s" in i
               for i in log.records["info"]), f"{log.records['info']}"

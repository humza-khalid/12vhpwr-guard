"""guard_core: the platform-neutral decision logic (v1.5 stage S1).

Four suites:

A. guard_core imports with every Windows-only module blocked, which is the proof it
   would import on Linux.
B. ThresholdTracker, the tier 1 timer that used to live inline in monitor_loop,
   driven directly with a hand-made clock.
C. guard_core's TieredResponse, PinAdvisor and FlightRecorder used without the
   Windows module's bindings: plain getters and a list for a notifier, and nothing
   platform specific touched.
D. The Windows bindings in hwinfo_12vhpwr_guard: the notifier's level mapping, its
   call-time lookups, and the constants the subclasses read from that module.
"""

import os
import subprocess
import sys
import types

import pytest

import guard_core as core
import hwinfo_12vhpwr_guard as g

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Same check the other suites use: the Event Viewer constants only exist when pywin32
# imported, and the fixture forces HAVE_EVENTLOG off, so the module attribute is
# what says whether they are reachable.
HAVE_WIN32CON = getattr(g, "win32con", None) is not None

LABEL = "GPU 12VHPWR Pin3 Current"

# Something like time.time(), so the progress line fires on the first over tick the
# way it does in the real loop (the "last notice" starts at 0.0).
T0 = 1000.0


class SequenceLog:
    """A logger and a notifier writing into one list, so order can be asserted."""

    def __init__(self):
        self.seq = []

    def _log(self, level, msg):
        self.seq.append(("log", level, str(msg)))

    def info(self, m): self._log("info", m)
    def warning(self, m): self._log("warning", m)
    def error(self, m): self._log("error", m)
    def critical(self, m): self._log("critical", m)
    def debug(self, m): self._log("debug", m)

    def notify(self, notice):
        self.seq.append(("notice", notice))

    def logs(self, level=None):
        return [e[2] for e in self.seq if e[0] == "log" and (level is None or e[1] == level)]

    def notices(self):
        return [e[1] for e in self.seq if e[0] == "notice"]

    def clear(self):
        self.seq.clear()


class PlainMitigator:
    """The mitigator interface TieredResponse uses, and nothing else."""

    def __init__(self, restore_ok=True, restore_warning=None):
        self.is_available = True
        self.unavailable_reason = None
        self.last_restore_warning = restore_warning
        self._restore_ok = restore_ok
        self.calls = []

    def engage(self, reason=""):
        self.calls.append("engage")
        return True

    def release(self):
        self.calls.append("release")
        return self._restore_ok


@pytest.fixture
def platform_untouched(guard, monkeypatch):
    """Fails the test if anything reaches the Windows module's toast or Event Viewer."""
    touched = []
    monkeypatch.setattr(g, "HAVE_EVENTLOG", True)
    monkeypatch.setattr(g, "eventlog_write", lambda *a: touched.append(("eventlog", a)))
    monkeypatch.setattr(g, "toast", lambda *a: touched.append(("toast", a)))
    yield touched
    assert touched == [], f"guard_core reached the Windows module: {touched}"


# =========================================================================
# Suite A: imports with the Windows modules blocked
# =========================================================================
BLOCKED = ("ctypes.wintypes", "win32api", "win32con", "win32evtlogutil", "winotify",
           "pystray", "tkinter", "hwinfo_12vhpwr_guard")


def test_a1_imports_and_runs_with_every_windows_module_blocked():
    """A1: a fresh interpreter, the Windows modules set to None, then the import.

    A None entry in sys.modules makes any import of that name raise ImportError, so
    a guard_core that reached for one of them anywhere at import time fails here.
    The classes are then built and ticked once each, still inside that interpreter.
    """
    code = f"""
import sys
sys.path.insert(0, {REPO_ROOT!r})
for name in {BLOCKED!r}:
    sys.modules[name] = None
import guard_core as core

class Log:
    def __getattr__(self, name):
        return lambda *a, **k: None

notices = []
tracker = core.ThresholdTracker(Log(), notify=notices.append)
assert tracker.update(1000.0, "pin", 10.0, "A", 9.5, 15.0).branch == core.THRESHOLD_OVER
response = core.TieredResponse(lambda: None, Log(), notify=notices.append)
assert response.evaluate(0.0, 1.0, "pin", "A", 9.5, False) is None
advisor = core.PinAdvisor()
assert advisor.evaluate(0.0, [("pin", 1.0, "A")] * 6) == []
# 3001/3002 from the timer starting, 6004 because there is no mitigator to use.
assert [n.event_id for n in notices] == [3001, 3002, 6004], notices
for name in {BLOCKED!r}:
    assert sys.modules[name] is None, name + " was replaced by a real import"
assert "ctypes" not in sys.modules, "guard_core pulled in ctypes"
print("guard_core ok")
"""
    proc = subprocess.run([sys.executable, "-I", "-c", code],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "guard_core ok" in proc.stdout


def test_a2_the_windows_module_reexports_every_moved_name(guard):
    """A2: every constant in guard_core is reachable on g, as the same object.

    The tests and tools read these as g.<name>; the move must not have hidden one.
    The three classes with Windows bindings are subclasses under the same name.
    """
    constants = [n for n in dir(core) if n.isupper() and not n.startswith("_")]
    assert len(constants) > 30, constants
    for name in constants:
        assert hasattr(g, name), f"g.{name} missing"
        assert getattr(g, name) is getattr(core, name), f"g.{name} is a different object"
    for name in ("Notice", "Advisory", "ThresholdTick", "ThresholdTracker"):
        assert getattr(g, name) is getattr(core, name), name
    for name in ("TieredResponse", "PinAdvisor", "FlightRecorder"):
        assert issubclass(getattr(g, name), getattr(core, name)), name


# =========================================================================
# Suite B: ThresholdTracker with an injected clock
# =========================================================================
def over(tracker, t, amps=10.0, threshold=9.5, sustained=15.0):
    return tracker.update(t, LABEL, amps, "A", threshold, sustained)


@pytest.fixture
def tracked():
    log = SequenceLog()
    return core.ThresholdTracker(log, notify=log.notify), log


def test_b1_starting_the_timer_warns_with_3001_and_a_toast(tracked):
    """B1: the first over sample starts the timer, word for word as before."""
    tracker, log = tracked
    tick = over(tracker, T0)

    assert tick == core.ThresholdTick(core.THRESHOLD_OVER, False, "")
    start = (f"Threshold exceeded: {LABEL}=10.00A >= 9.50A. "
             f"Starting 15s sustained timer.")
    progress = f"Still over threshold for 0.0s: {LABEL}=10.00A (needs 15.0s)"
    assert log.seq == [
        ("log", "warning", start),
        ("notice", core.Notice("warning", 3001, start, ("12VHPWR Guard - Warning", start))),
        ("log", "warning", progress),
        ("notice", core.Notice("warning", 3002, progress)),
    ]
    assert tracker.over_start == T0


def test_b2_the_progress_line_keeps_its_cadence(tracked):
    """B2: 3002 at most every SUSTAINED_PROGRESS_LOG_EVERY_SEC while over."""
    tracker, log = tracked
    every = core.SUSTAINED_PROGRESS_LOG_EVERY_SEC
    assert every == 5.0
    over(tracker, T0)
    log.clear()

    t = T0
    while t < T0 + every - 0.5:
        t += 0.5
        over(tracker, t)
    assert log.seq == [], f"nothing between progress lines: {log.seq}"

    over(tracker, T0 + every)
    line = f"Still over threshold for 5.0s: {LABEL}=10.00A (needs 15.0s)"
    assert log.seq == [("log", "warning", line),
                       ("notice", core.Notice("warning", 3002, line))]


def test_b3_tier1_expires_only_after_the_window(tracked):
    """B3: tier1_expired once the sustained window has run, with the reason text."""
    tracker, _log = tracked
    for t in (T0, T0 + 5.0, T0 + 14.5):
        assert over(tracker, t).tier1_expired is False, t
    tick = over(tracker, T0 + 15.0)
    assert tick.tier1_expired is True
    assert tick.tier1_reason == f"sustained 15.0s >= 15.0s, {LABEL}=10.00A >= 9.50A"


def test_b4_tier1_waits_for_consecutive_samples(tracked, monkeypatch):
    """B4: a window that has run out still needs CONSECUTIVE_SAMPLES_REQUIRED in a row.

    The default is 1, which makes the rule invisible, so it is raised to 3 here. A
    sample in the near band breaks the run without stopping the timer.
    """
    tracker, _log = tracked
    monkeypatch.setattr(core, "CONSECUTIVE_SAMPLES_REQUIRED", 3)

    over(tracker, T0)
    near = over(tracker, T0 + 1.0, amps=9.4)
    assert near.branch == core.THRESHOLD_NEAR and tracker.over_start == T0
    assert over(tracker, T0 + 20.0).tier1_expired is False, "1 in a row"
    assert over(tracker, T0 + 20.5).tier1_expired is False, "2 in a row"
    assert over(tracker, T0 + 21.0).tier1_expired is True, "3 in a row"


def test_b5_cleared_notice_only_after_the_stable_window(tracked):
    """B5: 4001 once the load has been clear for CLEAR_STABLE_SECONDS, then reset."""
    tracker, log = tracked
    assert core.CLEAR_STABLE_SECONDS == 1.0
    over(tracker, T0)
    log.clear()

    assert over(tracker, T0 + 2.0, amps=5.0).branch == core.THRESHOLD_CLEARED
    over(tracker, T0 + 2.5, amps=5.0)
    assert log.seq == [], "not yet stable"
    over(tracker, T0 + 3.0, amps=5.0)
    line = "Load cleared below 9.30A for 1.0s. Timer reset."
    assert log.seq == [
        ("log", "info", line),
        ("notice", core.Notice("info", 4001, line, ("12VHPWR Guard", "Load cleared. Timer reset."))),
    ]
    assert tracker.over_start is None and tracker.last_over_notice == 0.0

    log.clear()
    over(tracker, T0 + 10.0, amps=5.0)
    assert log.seq == [], "said once per episode"


def test_b6_no_cleared_notice_without_a_running_timer(tracked):
    """B6: a load that was never over says nothing when it stays clear."""
    tracker, log = tracked
    for t in (T0, T0 + 1.0, T0 + 2.0, T0 + 5.0):
        assert over(tracker, t, amps=5.0).branch == core.THRESHOLD_CLEARED
    assert log.seq == []


def test_b7_the_near_band_restarts_the_clear_timer(tracked):
    """B7: a sample between threshold - hysteresis and threshold restarts the clear wait."""
    tracker, log = tracked
    over(tracker, T0)
    log.clear()

    over(tracker, T0 + 1.0, amps=5.0)                       # clear timer starts
    assert over(tracker, T0 + 1.5, amps=9.4).branch == core.THRESHOLD_NEAR
    assert tracker.clear_start is None and tracker.consecutive_over == 0
    over(tracker, T0 + 2.0, amps=5.0)                       # starts again here
    over(tracker, T0 + 2.9, amps=5.0)
    assert log.seq == [], "1.9 s after the first clear sample, but only 0.9 s since the band"
    over(tracker, T0 + 3.0, amps=5.0)
    assert [n.event_id for n in log.notices()] == [4001]


def test_b8_reset_forgets_everything(tracked):
    """B8: reset() is what a reconnect and a resume gap call; the next over starts fresh."""
    tracker, log = tracked
    over(tracker, T0)
    over(tracker, T0 + 1.0, amps=9.4)
    tracker.reset()
    assert (tracker.over_start, tracker.consecutive_over,
            tracker.clear_start, tracker.last_over_notice) == (None, 0, None, 0.0)

    log.clear()
    over(tracker, T0 + 2.0)
    assert [n.event_id for n in log.notices()] == [3001, 3002], "the timer starts over"


def test_b9_classify_matches_update(tracked):
    """B9: the loop sets its status from classify() before update(); they must agree."""
    tracker, _log = tracked
    for amps in (9.6, 9.5, 9.4999, 9.31, 9.3, 9.2999, 0.0):
        branch = core.ThresholdTracker.classify(amps, 9.5)
        assert over(tracker, T0, amps=amps).branch == branch, amps


# =========================================================================
# Suite C: the core classes without the Windows bindings
# =========================================================================
TIERS = dict(core.DEFAULT_TIER_VALUES)


def test_c1_tiered_response_engages_and_releases_with_plain_getters(platform_untouched):
    """C1: one tier 1 engage, hold and release cycle on guard_core alone."""
    log = SequenceLog()
    m = PlainMitigator()
    r = core.TieredResponse(lambda: m, log, get_tier_values=lambda: dict(TIERS),
                            get_response_mode=lambda: core.RESPONSE_TIERED,
                            notify=log.notify)

    assert r.evaluate(0.0, 10.0, LABEL, "A", 9.5, True, "sustained test") is None
    assert r.engaged and m.calls == ["engage"]
    engaged = log.notices()
    assert [(n.level, n.event_id) for n in engaged] == [("warning", 6002)]
    assert engaged[0].message.startswith("Tier 1: sustained test. GPU limited")
    assert engaged[0].toast[0] == "12VHPWR Guard - GPU limited"

    log.clear()
    assert r.evaluate(10.0, 5.0, LABEL, "A", 9.5, False) is None, "grace ran out, current down"
    assert [(n.level, n.event_id, n.toast) for n in log.notices()] == [("info", 6003, None)]

    log.clear()
    assert r.evaluate(130.0, 5.0, LABEL, "A", 9.5, False) is None
    assert not r.engaged and r.released_at == 130.0 and m.calls == ["engage", "release"]
    released = log.notices()
    assert [(n.level, n.event_id) for n in released] == [("info", 6005)]
    assert released[0].toast == ("12VHPWR Guard", "GPU restored to normal clocks and power limit.")
    assert log.logs("warning")[0].startswith("GPU limit released (current stayed below 9.30A")


def test_c2_an_unverified_restore_writes_6007_before_the_log_line(platform_untouched):
    """C2: the 6007 entry, the warning line and the 6005 entry, in that order."""
    log = SequenceLog()
    m = PlainMitigator(restore_ok=False, restore_warning="power limit 400 W, expected 600 W")
    r = core.TieredResponse(lambda: m, log, get_tier_values=lambda: dict(TIERS),
                            get_response_mode=lambda: core.RESPONSE_TIERED,
                            notify=log.notify)
    r.evaluate(0.0, 10.0, LABEL, "A", 9.5, True, "sustained test")
    log.clear()

    r.release(5.0, "monitoring paused", arm_retrigger=False)
    msg = ("GPU limit released (monitoring paused). WARNING: the card may not be fully "
           "restored, check the log above.")
    assert log.seq == [
        ("notice", core.Notice("warning", 6007, "power limit 400 W, expected 600 W")),
        ("log", "warning", msg),
        ("notice", core.Notice("info", 6005, msg, (
            "12VHPWR Guard",
            "GPU limit released, but the restore could not be verified. Check the log."))),
    ]


def test_c3_defaults_without_any_getters(platform_untouched):
    """C3: left out, the getters mean the defaults and the notifier says nothing."""
    log = SequenceLog()
    r = core.TieredResponse(lambda: PlainMitigator(), log)
    assert r.effective_mode() == core.RESPONSE_TIERED
    assert r.evaluate(0.0, 13.5, LABEL, "A", 9.5, False) is None
    assert log.logs("warning")[0].startswith("Critical current:"), "the log line still lands"
    out = r.evaluate(core.DEFAULT_CRITICAL_SECONDS, 13.5, LABEL, "A", 9.5, False)
    assert out is None and r.engaged, "tier 2 engages on the default 3 s window"


def test_c4_pin_advisor_flags_a_dead_pin_on_its_own(platform_untouched):
    """C4: guard_core.PinAdvisor with its own defaults raises the dead-pin advisory."""
    a = core.PinAdvisor()
    pins = [(f"GPU 12VHPWR Pin{i} Current", amps, "A")
            for i, amps in enumerate((0.1, 8.0, 8.0, 8.0, 8.0, 8.0), start=1)]
    assert a.evaluate(0.0, pins) == []
    out = a.evaluate(core.DEFAULT_ADVISORY_SUSTAIN_SECONDS, pins)
    assert len(out) == 1, out
    adv = out[0]
    assert (adv.kind, adv.pin, adv.first) == ("dead_pin", 1, True)
    assert adv.message.startswith("Pin advisory: pin 1 reads 0.10 A")


def test_c5_pin_advisor_reads_a_custom_constants_namespace(platform_untouched):
    """C5: the constants namespace is consulted, so a platform can supply its own."""
    consts = types.SimpleNamespace(**{n: getattr(core, n) for n in dir(core) if n.isupper()})
    consts.NO_CURRENT_SUSTAIN_SEC = 2.0
    a = core.PinAdvisor(constants=consts)
    zeros = [(f"GPU 12VHPWR Pin{i} Current", 0.0, "A") for i in range(1, 7)]
    assert a.evaluate(0.0, zeros) == []
    out = a.evaluate(2.0, zeros)
    assert [x.kind for x in out] == ["no_current"], out
    assert "for 2 s" in out[0].message, out[0].message


def test_c6_flight_recorder_writes_where_it_is_told(tmp_path, platform_untouched):
    """C6: guard_core.FlightRecorder takes its directory and retention as arguments."""
    log = SequenceLog()
    fr = core.FlightRecorder(str(tmp_path), maxlen=5, keep_files=1)
    fr.record([("p1", 1.0, "A"), ("p2", 2.0, "A")])
    first = fr.dump(log, "a")
    assert first is not None and os.path.dirname(first) == str(tmp_path)
    open(os.path.join(str(tmp_path), "flight_00000000_000000_old.csv"), "w").close()
    fr.dump(log, "b")
    left = sorted(os.listdir(str(tmp_path)))
    assert len(left) == 1, f"keep_files=1 leaves one dump: {left}"


# =========================================================================
# Suite D: the Windows bindings
# =========================================================================
@pytest.mark.skipif(not HAVE_WIN32CON, reason="pywin32 not installed")
def test_d1_notifier_maps_levels_to_event_types(guard, monkeypatch):
    """D1: info, warning and error land as the matching Event Viewer entry types."""
    events = []
    monkeypatch.setattr(g, "HAVE_EVENTLOG", True)
    monkeypatch.setattr(g, "eventlog_write", lambda t, i, m: events.append((t, i, m)))

    g.windows_notify(g.Notice("info", 1, "a"))
    g.windows_notify(g.Notice("warning", 2, "b"))
    g.windows_notify(g.Notice("error", 3, "c"))

    assert events == [
        (g.win32con.EVENTLOG_INFORMATION_TYPE, 1, "a"),
        (g.win32con.EVENTLOG_WARNING_TYPE, 2, "b"),
        (g.win32con.EVENTLOG_ERROR_TYPE, 3, "c"),
    ]
    assert guard.toasts == [], "no toast on a notice that carries none"


def test_d2_notifier_reads_have_eventlog_at_call_time(guard, monkeypatch):
    """D2: flipping HAVE_EVENTLOG after the notifier exists changes what it does."""
    events = []
    monkeypatch.setattr(g, "eventlog_write", lambda t, i, m: events.append(i))
    monkeypatch.setattr(g, "HAVE_EVENTLOG", False)
    g.windows_notify(g.Notice("warning", 3001, "x"))
    assert events == []
    if HAVE_WIN32CON:
        monkeypatch.setattr(g, "HAVE_EVENTLOG", True)
        g.windows_notify(g.Notice("warning", 3001, "x"))
        assert events == [3001]


def test_d3_notifier_toasts_only_when_the_notice_has_one(guard, monkeypatch):
    """D3: the toast goes out when present; a notice with no event id skips Event Viewer."""
    events = []
    monkeypatch.setattr(g, "HAVE_EVENTLOG", True)
    monkeypatch.setattr(g, "eventlog_write", lambda t, i, m: events.append(i))

    g.windows_notify(g.Notice("info", None, "m", ("Title", "Body")))
    assert guard.toasts == [("Title", "Body")]
    assert events == [], "no event id, no Event Viewer entry"


def test_d4_bindings_see_a_toast_swapped_after_they_were_built(guard, monkeypatch):
    """D4: g.toast is looked up per notice, not captured when TieredResponse is built."""
    m = PlainMitigator()
    m.is_available = False
    m.unavailable_reason = "not elevated"
    g.set_response_mode(g.RESPONSE_TIERED)
    r = g.TieredResponse(lambda: m, SequenceLog())
    late = []
    monkeypatch.setattr(g, "toast", lambda title, msg: late.append((title, msg)))

    assert r.effective_mode() == g.RESPONSE_SHUTDOWN_ONLY
    assert late == [("12VHPWR Guard", "GPU limiting unavailable (not elevated). Shutdown-only.")]
    assert guard.toasts == []


def test_d5_windows_pin_advisor_reads_patched_constants_per_tick(guard, monkeypatch):
    """D5: ADVISORY_CLEAR_SEC patched on g after the advisor exists still applies."""
    a = g.PinAdvisor(g.get_advisory_values())
    zeros = [(f"GPU 12VHPWR Pin{i} Current", 0.0, "A") for i in range(1, 7)]
    a.evaluate(0.0, zeros)
    assert [x.kind for x in a.evaluate(g.NO_CURRENT_SUSTAIN_SEC, zeros)] == ["no_current"]

    monkeypatch.setattr(g, "ADVISORY_CLEAR_SEC", 5.0)
    live = [(f"GPU 12VHPWR Pin{i} Current", 1.0, "A") for i in range(1, 7)]
    t = g.NO_CURRENT_SUSTAIN_SEC + 1.0
    assert a.evaluate(t, live) == []
    out = a.evaluate(t + 5.0, live)
    assert [x.kind for x in out] == ["cleared"], f"cleared on the patched 5 s: {out}"


def test_d6_windows_flight_recorder_follows_log_dir(guard, tmp_path, monkeypatch):
    """D6: g.LOG_DIR moved after the recorder was built still moves the dump."""
    fr = g.FlightRecorder(maxlen=10)
    fr.record([("p1", 1.0, "A")])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(g, "LOG_DIR", str(elsewhere))
    path = fr.dump(SequenceLog(), "moved")
    assert path is not None and os.path.dirname(path) == str(elsewhere)

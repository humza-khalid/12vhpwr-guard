"""F2: the sensor-path check, so the guard never shows green when it cannot see the
load (B-002).

A BTF card in a BTF motherboard is powered through the GC-HPWR slot, so the card-side
pin sensors carry no current at all: the guard reads zero on every pin and would sit
green while protecting nothing. A dead or unpowered sensor block reads the same way.
There is no protecting a load the sensors cannot see, so the whole feature is the
guard refusing to look green and saying why.

Suite A drives PinAdvisor directly on synthetic time (the rule has to work unchanged
in the planned Linux daemon, so it owns no clock, no toast and no NVML). Suite B runs
the real monitor_loop to prove the icon actually goes purple and comes back. Suite C
covers the optional NVML power reading in gpu_mitigation, with a fake driver.

Nothing here loads nvml.dll, touches the card, or can shut the machine down.
"""

import glob
import os

import pytest

from gpu_mitigation import MARKER_NAME, GpuMitigator, NvmlError, _Nvml
from helpers import CountingLogger, FakeBackend, FakeMitigator, run_loop

import hwinfo_12vhpwr_guard as g

# The event id test needs the real win32con constant, which only exists when pywin32
# imported. HAVE_EVENTLOG is forced False by the guard fixture, so the module
# attribute is what says whether the constant is reachable.
HAVE_WIN32CON = getattr(g, "win32con", None) is not None

# Reference patterns, all six pins in amps.
NO_CURRENT = (0.02, 0.02, 0.02, 0.02, 0.02, 0.02)   # 0.12 A total, a slot-powered card
DEAD_FLAT = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)          # a sensor block reading nothing
CABLED_IDLE = (0.5, 0.9, 0.6, 1.0, 0.7, 0.8)        # 4.5 A total, the user's own card
LOADED = (7.5, 7.4, 7.6, 7.5, 7.3, 7.5)             # 44.8 A total, healthy under load
# One dead pin under load. Half the pins near zero is a cable fault, not a sensor
# path that sees nothing, and the two must not be confused.
ONE_DEAD = (0.0, 5.0, 0.5, 5.0, 5.0, 5.0)
# 2.0 A total. Nothing like enough for the power the card reports, but every pin is
# well clear of the no-current floor, so only the power rule can reach it.
TRICKLE = (0.4, 0.3, 0.4, 0.3, 0.3, 0.3)

NO_CURRENT_SUSTAIN = 60.0   # NO_CURRENT_SUSTAIN_SEC
MISMATCH_SUSTAIN = 30.0     # MISMATCH_SUSTAIN_SEC
REPEAT = 600.0              # advisory_repeat_seconds default
CLEAR = 60.0                # ADVISORY_CLEAR_SEC


def pins(values):
    """The loop's [(label, amps, unit)] shape, from six numbers."""
    return [(f"GPU 12VHPWR Pin{i} Current", a, "A")
            for i, a in enumerate(values, start=1)]


def advisor(gm, **overrides):
    values = gm.get_advisory_values()
    values.update(overrides)
    return gm.PinAdvisor(values)


# =========================================================================
# Suite A: PinAdvisor on its own, synthetic time
# =========================================================================
def test_a1_zero_pins_warn_once_after_a_minute(guard):
    """A1: nothing for 60 s, then one first warning naming the sensor path."""
    a = advisor(guard.g)

    assert a.evaluate(0.0, pins(NO_CURRENT)) == [], "nothing on the first sighting"
    assert a.evaluate(59.9, pins(NO_CURRENT)) == [], "nothing a tenth short of a minute"

    out = a.evaluate(NO_CURRENT_SUSTAIN, pins(NO_CURRENT))
    assert len(out) == 1, f"exactly one advisory at the sustain: {out}"
    one = out[0]
    assert one.kind == "no_current", one.kind
    assert one.pin is None, "the whole connector, not one pin"
    assert one.first is True
    assert one.message.startswith("Sensor path:"), one.message
    assert "cannot protect the card" in one.message, one.message
    assert "BTF" in one.message and "README" in one.message, \
        f"it points at the one setup where this is expected: {one.message}"
    assert one.summary == ("The pin sensors see no current. This tool cannot protect "
                           "the card in this setup."), one.summary
    assert one.short == "no current on any pin, not protected", one.short

    assert a.evaluate(61.0, pins(NO_CURRENT)) == [], "one warning, not one per tick"


def test_a2_zero_pins_repeat_only_after_the_repeat_window(guard):
    """A2: quiet until the repeat window, then one repeat, then quiet again."""
    a = advisor(guard.g)
    assert a.evaluate(0.0, pins(NO_CURRENT)) == []
    assert len(a.evaluate(NO_CURRENT_SUSTAIN, pins(NO_CURRENT))) == 1

    for tick in (61.0, 300.0, NO_CURRENT_SUSTAIN + REPEAT - 1.0):
        assert a.evaluate(tick, pins(NO_CURRENT)) == [], f"quiet at t={tick}"

    out = a.evaluate(NO_CURRENT_SUSTAIN + REPEAT, pins(NO_CURRENT))
    assert len(out) == 1, f"one repeat at the repeat window: {out}"
    assert out[0].kind == "no_current" and out[0].first is False

    assert a.evaluate(NO_CURRENT_SUSTAIN + REPEAT + 1.0, pins(NO_CURRENT)) == []


def test_a3_one_cleared_line_a_minute_after_the_pins_come_back(guard):
    """A3: current on the pins again clears it, once, after the clear window."""
    a = advisor(guard.g)
    a.evaluate(0.0, pins(NO_CURRENT))
    assert len(a.evaluate(NO_CURRENT_SUSTAIN, pins(NO_CURRENT))) == 1

    back = 100.0
    assert a.evaluate(back, pins((1.0,) * 6)) == [], "no cleared line the moment it goes"
    assert a.evaluate(back + CLEAR - 1.0, pins((1.0,) * 6)) == [], "not a second early"

    out = a.evaluate(back + CLEAR, pins((1.0,) * 6))
    assert len(out) == 1, f"one cleared advisory: {out}"
    assert out[0].kind == "cleared" and out[0].first is False
    assert out[0].ended == "no_current", "it says which episode ended"
    assert out[0].message == "Sensor path cleared: the pins carry current again.", \
        out[0].message

    for tick in (back + CLEAR + 1.0, back + 5000.0):
        assert a.evaluate(tick, pins((1.0,) * 6)) == [], f"episode forgotten at t={tick}"


def test_a4_a_cabled_card_at_idle_never_trips_it(guard):
    """A4: 0.5 to 1.0 A per pin is what a cabled Astral idles at. An hour of it."""
    a = advisor(guard.g)

    assert min(CABLED_IDLE) >= g.NO_CURRENT_AMPS * 5, \
        "the quietest idle pin sits five times the floor, so the margin is real"
    for tick in range(0, 3600, 5):
        assert a.evaluate(float(tick), pins(CABLED_IDLE)) == [], \
            f"silent on a cabled card at t={tick}"


def test_a5_the_load_gate_does_not_suppress_it(guard):
    """A5: 0.12 A total is far under the 18 A advisory gate, and it fires anyway.

    The gate is computed from the very readings that are missing, so gating this
    rule would mean asking a blind sensor for permission to report itself.
    """
    gm = guard.g
    a = advisor(gm)

    assert sum(NO_CURRENT) < gm.DEFAULT_ADVISORY_LOAD_AMPS, "well under the gate"
    a.evaluate(0.0, pins(NO_CURRENT))
    out = a.evaluate(NO_CURRENT_SUSTAIN, pins(NO_CURRENT))
    assert len(out) == 1 and out[0].kind == "no_current", out


def test_a6_a_dead_pin_under_load_is_not_a_sensor_path_fault(guard):
    """A6: some pins at zero while the rest carry current is a cable fault."""
    a = advisor(guard.g)

    a.evaluate(0.0, pins(ONE_DEAD))
    out = a.evaluate(NO_CURRENT_SUSTAIN, pins(ONE_DEAD))
    assert len(out) == 1, f"one advisory: {out}"
    assert out[0].kind == "dead_pin", "the pins that are left prove the sensors work"
    assert out[0].pin == 1


def test_a7_the_power_rule_names_both_numbers(guard):
    """A7: 400 W against 2 A of pins, after 30 s, with the power wording."""
    a = advisor(guard.g)

    assert a.evaluate(0.0, pins(TRICKLE), board_power_w=400.0) == []
    assert a.evaluate(29.9, pins(TRICKLE), board_power_w=400.0) == [], \
        "the power rule waits 30 s, not none"

    out = a.evaluate(MISMATCH_SUSTAIN, pins(TRICKLE), board_power_w=400.0)
    assert len(out) == 1, f"one advisory at the power sustain: {out}"
    one = out[0]
    assert one.kind == "no_current", "same kind, so one episode and one clear"
    assert one.first is True
    assert "400 W" in one.message, one.message
    assert "2.0 A" in one.message, f"the pins' own total is in it: {one.message}"
    assert "33 A" in one.message, f"and what that power implies: {one.message}"
    assert "not on the load path" in one.message, one.message
    assert "400 W" in one.summary and "2.0 A" in one.summary, one.summary
    assert one.short.endswith("not protected"), one.short


def test_a8_pins_that_match_the_power_say_nothing(guard):
    """A8: 40 A of pins at 500 W is the connector doing its job."""
    a = advisor(guard.g)
    healthy = (6.7, 6.6, 6.7, 6.7, 6.6, 6.7)   # 40 A total

    assert abs(sum(healthy) - 40.0) < 0.1
    for tick in range(0, 3600, 10):
        assert a.evaluate(float(tick), pins(healthy), board_power_w=500.0) == [], \
            f"silent at t={tick}"


def test_a9_without_a_power_reading_the_power_rule_is_inert(guard):
    """A9: board_power_w None leaves only the all-zero rule, which 2 A does not meet."""
    a = advisor(guard.g)

    for tick in range(0, 3600, 10):
        assert a.evaluate(float(tick), pins(TRICKLE)) == [], f"silent at t={tick}"
        assert a.evaluate(float(tick), pins(TRICKLE), board_power_w=None) == [], \
            f"explicitly None is the same at t={tick}"


def test_a10_a_low_power_reading_cannot_start_the_power_rule(guard):
    """A10: under 150 W the slot alone could explain it, so the rule stays out."""
    a = advisor(guard.g)

    for tick in range(0, 600, 10):
        assert a.evaluate(float(tick), pins(TRICKLE), board_power_w=100.0) == [], \
            f"silent under the minimum watts at t={tick}"

    # The same pins at a power the slot cannot supply on its own do fire.
    b = advisor(guard.g)
    b.evaluate(0.0, pins(TRICKLE), board_power_w=400.0)
    assert len(b.evaluate(MISMATCH_SUSTAIN, pins(TRICKLE), board_power_w=400.0)) == 1


def test_a11_when_both_rules_are_true_the_power_wording_wins(guard):
    """A11: zeros plus a real power reading is the more specific thing to say."""
    a = advisor(guard.g)

    a.evaluate(0.0, pins(DEAD_FLAT), board_power_w=480.0)
    out = a.evaluate(MISMATCH_SUSTAIN, pins(DEAD_FLAT), board_power_w=480.0)
    assert len(out) == 1, f"and it fires on the shorter power sustain: {out}"
    assert out[0].kind == "no_current"
    assert "480 W" in out[0].message, out[0].message
    assert "all 6 pins" not in out[0].message, "the zero wording is the fallback"


def test_a12_the_two_rules_share_one_episode(guard):
    """A12: the same fault seen a second way is not a second warning."""
    a = advisor(guard.g)

    a.evaluate(0.0, pins(DEAD_FLAT))
    out = a.evaluate(NO_CURRENT_SUSTAIN, pins(DEAD_FLAT))
    assert len(out) == 1 and "all 6 pins" in out[0].message, "the zero wording first"

    # A power reading arrives (a game starts, NVML answers). Same episode, so no new
    # first warning and no cleared line in between.
    for tick in (61.0, 120.0, 300.0):
        assert a.evaluate(tick, pins(DEAD_FLAT), board_power_w=480.0) == [], \
            f"still the one episode at t={tick}"

    out = a.evaluate(NO_CURRENT_SUSTAIN + REPEAT, pins(DEAD_FLAT), board_power_w=480.0)
    assert len(out) == 1 and out[0].first is False, f"a repeat, not a new episode: {out}"
    assert "480 W" in out[0].message, "which now has the better numbers in it"


def test_a13_advisories_off_silences_the_cable_rules_only(guard):
    """A13: `advisories = off` is not a way to switch off the sensor-path check."""
    a = advisor(guard.g)

    # The two cable advisories, gated.
    for pattern in (ONE_DEAD, (4.0, 4.2, 4.1, 4.0, 4.3, 7.5)):
        b = advisor(guard.g)
        b.evaluate(0.0, pins(pattern), advisories_enabled=False)
        for tick in (10.0, 60.0, 600.0, 3600.0):
            assert b.evaluate(tick, pins(pattern), advisories_enabled=False) == [], \
                f"no cable advisory with the flag off at t={tick}"

    # The sensor-path check, not gated.
    a.evaluate(0.0, pins(NO_CURRENT), advisories_enabled=False)
    out = a.evaluate(NO_CURRENT_SUSTAIN, pins(NO_CURRENT), advisories_enabled=False)
    assert len(out) == 1 and out[0].kind == "no_current", \
        f"the guard still admits it cannot see the load: {out}"


def test_a14_reset_forgets_a_half_formed_sensor_path_fault(guard):
    """A14: a gap in the readings restarts the minute, it does not bank it."""
    a = advisor(guard.g)

    a.evaluate(0.0, pins(NO_CURRENT))
    a.evaluate(59.0, pins(NO_CURRENT))
    a.reset()

    assert a.evaluate(59.5, pins(NO_CURRENT)) == [], "the clock restarted"
    assert a.evaluate(119.4, pins(NO_CURRENT)) == [], "still short of a fresh minute"
    out = a.evaluate(119.5, pins(NO_CURRENT))
    assert len(out) == 1 and out[0].first is True, f"a new first warning: {out}"


def test_a15_the_constants_are_the_documented_numbers(guard):
    """A15: the numbers the README quotes are the numbers that ship."""
    gm = guard.g

    assert gm.NO_CURRENT_AMPS == 0.1
    assert gm.NO_CURRENT_SUSTAIN_SEC == 60.0
    assert gm.MISMATCH_MIN_WATTS == 150.0
    assert gm.MISMATCH_RATIO == 0.25
    assert gm.MISMATCH_SUSTAIN_SEC == 30.0
    assert gm.ADVISORY_EVENT_IDS == {
        "imbalance": 7001, "dead_pin": 7002, "no_current": 7003}

    # The margin argument: the PCIe slot supplies at most 75 W, so at the minimum
    # watts at least 6.25 A has to cross the connector, and the ratio asks for half
    # of that. A correctly sensed cable cannot get under it.
    must_cross = 75.0 / 12.0
    trips_below = gm.MISMATCH_RATIO * (gm.MISMATCH_MIN_WATTS / 12.0)
    assert trips_below < must_cross, (trips_below, must_cross)


# =========================================================================
# Suite B: the real monitor_loop
# =========================================================================
class PoweredMitigator(FakeMitigator):
    """A mitigator that answers the NVML power reading. The stock fake does not."""

    watts = 400.0

    def power_usage_mw(self):
        return int(PoweredMitigator.watts * 1000)


def compress(monkeypatch, sustain=0.3, clear=0.3, mismatch=0.3):
    """Squeeze the three sensor-path windows so a loop run takes about a second."""
    monkeypatch.setattr(g, "NO_CURRENT_SUSTAIN_SEC", sustain)
    monkeypatch.setattr(g, "MISMATCH_SUSTAIN_SEC", mismatch)
    monkeypatch.setattr(g, "ADVISORY_CLEAR_SEC", clear)


def is_purple():
    st = g.get_state()
    return st["status"] == g.Status.ERROR and st.get("sensor_path_fault") is True


def test_b1_zero_pins_turn_the_icon_purple_and_say_why(loop_env, monkeypatch):
    """B1: purple, a warning, a toast, a tooltip and the evidence file. Nothing else."""
    gm, tmp = loop_env.g, str(loop_env.tmp)
    compress(monkeypatch)
    FakeBackend.pins = DEAD_FLAT

    log = run_loop(15.0, until=is_purple)

    assert is_purple(), f"the icon is purple, not green: {gm.get_state()}"
    assert gm.get_state()["advisory"] == "no current on any pin, not protected"
    assert "NOT PROTECTED" in gm.tray_title(), gm.tray_title()
    assert "no current on any pin" in gm.tray_title(), gm.tray_title()

    warnings = [w for w in log.records["warning"] if "Sensor path" in w]
    assert warnings, f"the fault is a WARNING: {log.records['warning']}"
    assert "cannot protect the card" in warnings[0], warnings[0]

    toasts = [t for t in loop_env.toasts if "not protected" in t[0]]
    assert toasts, f"the first warning toasts: {loop_env.toasts}"
    assert "cannot protect" in toasts[0][1], toasts[0]

    flights = glob.glob(os.path.join(tmp, "flight_*_advisory.csv"))
    assert len(flights) == 1, f"one flight file as evidence: {flights}"

    assert any("Sensor-path check: pins and board power" in i
               for i in log.records["info"]), \
        f"the startup line names the tiered form: {log.records['info']}"
    assert not loop_env.shutdowns, f"it never shuts down: {loop_env.shutdowns}"
    assert all(not m.engaged for m in loop_env.made), "it never limits the GPU"
    assert not any("engage" in c for m in loop_env.made for c in m.calls), \
        "the mitigator was never asked to engage"


def test_b2_the_icon_goes_back_to_green_when_current_returns(loop_env, monkeypatch):
    """B2: pins carrying current again clears the fault and the status."""
    gm = loop_env.g
    compress(monkeypatch)
    FakeBackend.pins = DEAD_FLAT
    phase = {"seen_purple": False}

    def until():
        if not phase["seen_purple"]:
            if is_purple():
                phase["seen_purple"] = True
                FakeBackend.pins = (1.0,) * 6
            return False
        st = gm.get_state()
        return st["status"] == gm.Status.OK and not st.get("sensor_path_fault")

    log = run_loop(20.0, until=until)

    assert phase["seen_purple"], "the fault happened first"
    st = gm.get_state()
    assert st["status"] == gm.Status.OK, f"green again: {st}"
    assert st.get("sensor_path_fault") is False, st
    assert st["advisory"] is None, "and the tooltip suffix is gone"
    assert any("Sensor path cleared" in i for i in log.records["info"]), \
        f"the clear is an INFO line: {log.records['info']}"
    assert "OK" in gm.tray_title(), gm.tray_title()


def test_b3_advisories_off_does_not_silence_it(loop_env, monkeypatch):
    """B3: the guard still refuses to look green with the advisories switched off."""
    gm = loop_env.g
    compress(monkeypatch)
    gm.set_advisory_values(advisories_enabled=False)
    FakeBackend.pins = DEAD_FLAT

    log = run_loop(15.0, until=is_purple)

    assert is_purple(), f"still purple: {gm.get_state()}"
    assert any("Advisories: off" in i for i in log.records["info"]), \
        "the advisories really were off"
    assert [w for w in log.records["warning"] if "Sensor path" in w], \
        f"and it still said so: {log.records['warning']}"


def test_b4_shutdown_only_turns_purple_without_touching_nvml(loop_env, monkeypatch):
    """B4: the NVML-free half runs in shutdown-only mode, and binds nothing."""
    gm = loop_env.g
    compress(monkeypatch)
    gm.set_response_mode(gm.RESPONSE_SHUTDOWN_ONLY)
    FakeBackend.pins = DEAD_FLAT

    log = run_loop(15.0, until=is_purple)

    assert is_purple(), f"purple without a power reading: {gm.get_state()}"
    assert [w for w in log.records["warning"] if "Sensor path" in w], \
        f"the warning is the pins-only wording: {log.records['warning']}"
    assert any("Sensor-path check: pins only (no NVML in shutdown-only mode)" in i
               for i in log.records["info"]), \
        f"and the startup line says so: {log.records['info']}"

    # The loop builds exactly one mitigator whatever the mode, for startup recovery,
    # and that one never binds NVML unless a marker file from a previous run exists.
    # Nothing in shutdown-only mode connects to the card.
    assert all(m.recovery_ran for m in loop_env.made), \
        f"only the startup recovery mitigator: {[m.calls for m in loop_env.made]}"
    assert not any(c.startswith("connect") for m in loop_env.made for c in m.calls), \
        f"nothing bound NVML: {[m.calls for m in loop_env.made]}"


def test_b5_the_loop_passes_board_power_to_the_advisor(loop_env, monkeypatch):
    """B5: pins that carry a trickle while the card reports 400 W, in tiered mode."""
    gm = loop_env.g
    made = loop_env.made
    compress(monkeypatch)

    def factory(logger=None):
        m = PoweredMitigator()
        made.append(m)
        return m

    monkeypatch.setattr(gm, "GpuMitigator", factory)
    FakeBackend.pins = TRICKLE

    log = run_loop(15.0, until=is_purple)

    assert is_purple(), f"the power rule turned it purple: {gm.get_state()}"
    warnings = [w for w in log.records["warning"] if "Sensor path" in w]
    assert warnings, f"one warning: {log.records['warning']}"
    assert "400 W" in warnings[0] and "2.0 A" in warnings[0], \
        f"with both numbers in it: {warnings[0]}"
    assert min(TRICKLE) > gm.NO_CURRENT_AMPS, \
        "no pin is near zero, so only the power reading can explain this"
    assert not loop_env.shutdowns, f"it never shuts down: {loop_env.shutdowns}"


@pytest.mark.skipif(not HAVE_WIN32CON, reason="pywin32 not installed")
def test_b6_events_7003_and_7004(loop_env, monkeypatch):
    """B6: the fault writes 7003 (warning) and the clear writes 7004 (information)."""
    gm = loop_env.g
    events = []
    compress(monkeypatch)
    monkeypatch.setattr(gm, "HAVE_EVENTLOG", True)
    monkeypatch.setattr(gm, "eventlog_register_source", lambda logger: None)
    monkeypatch.setattr(
        gm, "eventlog_write",
        lambda event_type, event_id, message: events.append((event_type, event_id, message)),
    )
    FakeBackend.pins = DEAD_FLAT
    phase = {"seen_purple": False}

    def until():
        if not phase["seen_purple"]:
            if is_purple():
                phase["seen_purple"] = True
                FakeBackend.pins = (1.0,) * 6
            return False
        return any(e[1] == 7004 for e in events)

    run_loop(20.0, until=until)

    faults = [e for e in events if e[1] == 7003]
    clears = [e for e in events if e[1] == 7004]
    assert faults, f"one 7003 for the fault: {[e[1] for e in events]}"
    assert faults[0][0] == gm.win32con.EVENTLOG_WARNING_TYPE, "a warning"
    assert "Sensor path" in faults[0][2], faults[0][2]
    assert clears, f"one 7004 for the clear: {[e[1] for e in events]}"
    assert clears[0][0] == gm.win32con.EVENTLOG_INFORMATION_TYPE, "information"
    assert "cleared" in clears[0][2], clears[0][2]


# =========================================================================
# Suite C: the NVML power reading (gpu_mitigation)
# =========================================================================
SUB_ID = 0x89E31043
USAGE_MW = 480_000


class PowerNvml:
    """A fake driver with the one call F2 adds, plus the minimum to connect."""

    HANDLE = "handle-0"

    def __init__(self):
        self.usage_mw = USAGE_MW
        self.fail_reads = False
        self.reads = 0

    def init(self):
        pass

    def shutdown(self):
        pass

    def device_count(self):
        return 1

    def handle(self, index):
        return self.HANDLE

    def subsystem_id(self, handle):
        return SUB_ID

    def name(self, handle):
        return "NVIDIA GeForce RTX 5090"

    def power_limit_mw(self, handle):
        return 600_000

    def power_limit_constraints_mw(self, handle):
        return 400_000, 600_000

    def min_supported_graphics_clock_mhz(self, handle):
        return 180

    def power_usage_mw(self, handle):
        self.reads += 1
        if self.fail_reads:
            raise NvmlError("nvmlDeviceGetPowerUsage", 3)
        return self.usage_mw


def make_mitigator(tmp_path, fake):
    return GpuMitigator(
        marker_path=str(tmp_path / MARKER_NAME),
        nvml_factory=lambda: fake,
        elevation_check=lambda: True,
        logger=CountingLogger(),
    )


def test_c1_power_usage_is_none_before_connect(tmp_path):
    """C1: nothing to ask, so nothing is claimed."""
    fake = PowerNvml()
    m = make_mitigator(tmp_path, fake)

    assert m.power_usage_mw() is None
    assert fake.reads == 0, "and the driver was not touched"


def test_c2_power_usage_reads_the_card(tmp_path):
    """C2: connected, it returns what the driver says, in milliwatts."""
    fake = PowerNvml()
    m = make_mitigator(tmp_path, fake)
    assert m.connect(SUB_ID) is True, m.unavailable_reason

    assert m.power_usage_mw() == USAGE_MW
    assert fake.reads == 1


def test_c3_a_failing_read_is_none_not_an_exception(tmp_path):
    """C3: an NVML error is a missing detail, never a raise into the monitor loop."""
    fake = PowerNvml()
    m = make_mitigator(tmp_path, fake)
    assert m.connect(SUB_ID) is True
    fake.fail_reads = True

    assert m.power_usage_mw() is None


def test_c4_a_driver_without_the_call_is_none_too(tmp_path, monkeypatch):
    """C4: the binding is optional, so a driver that lacks it must cost nothing."""
    fake = PowerNvml()
    m = make_mitigator(tmp_path, fake)
    assert m.connect(SUB_ID) is True
    monkeypatch.delattr(PowerNvml, "power_usage_mw")

    assert m.power_usage_mw() is None
    assert m.is_available, "and the mitigation itself is unaffected"


def test_c5_an_unbound_call_raises_the_ordinary_not_supported_error():
    """C5: _Nvml with no bindings answers NOT_SUPPORTED rather than crashing.

    That is the path a driver too old to export nvmlDeviceGetPowerUsage takes, and
    it is why the binding is declared required=False: the guard loses a number and
    keeps its mitigation.
    """
    with pytest.raises(NvmlError):
        _Nvml()._call("nvmlDeviceGetPowerUsage", None, None)

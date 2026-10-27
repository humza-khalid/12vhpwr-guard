"""F1: the warn-only pin imbalance and dead-pin advisory (B-001).

Suite A drives PinAdvisor directly on synthetic time, which is the point of the
class: no threads, no clock of its own, nothing Windows-specific, so the planned
Linux daemon can import it as is. An hour of monitoring is a handful of calls.

Suite B runs the real monitor_loop once to prove the loop renders a dead pin as a
warning, a toast, a tooltip and a flight file, and that it does nothing else at all:
no shutdown, no mitigation.
"""

import glob
import os

import pytest

from helpers import FakeBackend, run_loop


# The reference patterns, all six pins in amps.
DEAD_PIN = (9.0, 9.0, 0.2, 9.0, 9.0, 9.0)          # 45.2 A total, pin 3 open
TWO_DEAD_PINS = (9.0, 0.3, 0.2, 9.0, 9.0, 9.0)     # 36.5 A total, pins 2 and 3
IMBALANCED = (4.0, 4.2, 4.1, 4.0, 4.3, 7.5)        # 28.1 A total, 3.5 A spread
HEALTHY = (7.5, 7.4, 7.6, 7.5, 7.3, 7.5)           # 44.8 A total, 0.3 A spread
IDLE = (0.5, 0.5, 0.5, 0.5, 0.5, 0.5)              # 3.0 A total, under the gate
# A dead pin in every respect except that the connector is carrying 17.1 A, under
# the 18 A gate. The only thing stopping this one is the gate.
UNDER_GATE = (3.4, 3.4, 0.1, 3.4, 3.4, 3.4)

SUSTAIN = 10.0      # advisory_sustain_seconds default
REPEAT = 600.0      # advisory_repeat_seconds default
CLEAR = 60.0        # ADVISORY_CLEAR_SEC


def pins(values):
    """The loop's [(label, amps, unit)] shape, from six numbers."""
    return [(f"GPU 12VHPWR Pin{i} Current", a, "A")
            for i, a in enumerate(values, start=1)]


def advisor(g, **overrides):
    values = g.get_advisory_values()
    values.update(overrides)
    return g.PinAdvisor(values)


def first_warning(a, values, start=0.0):
    """Drive an advisor to the first warning of an episode and return it."""
    assert a.evaluate(start, pins(values)) == [], "nothing on the first sighting"
    out = a.evaluate(start + SUSTAIN, pins(values))
    assert len(out) == 1 and out[0].first, f"one first warning at the sustain: {out}"
    return out[0]


# =========================================================================
# Suite A: PinAdvisor on its own, synthetic time
# =========================================================================
def test_a1_below_the_load_gate_nothing_ever_fires(guard):
    """A1: under the load gate no condition can start, however long it lasts."""
    g = guard.g
    a = advisor(g)

    assert sum(UNDER_GATE) < g.DEFAULT_ADVISORY_LOAD_AMPS, "pattern is under the gate"
    for tick in range(0, 3600, 5):
        assert a.evaluate(float(tick), pins(UNDER_GATE)) == [], \
            f"silent under the gate at t={tick}"

    # The same numbers scaled up past the gate do fire, so it really was the gate.
    b = advisor(g)
    assert first_warning(b, DEAD_PIN).kind == "dead_pin"


def test_a2_dead_pin_warns_once_after_the_sustain(guard):
    """A2: dead pin, first warning after 10 s, with the pin and the numbers in it."""
    g = guard.g
    a = advisor(g)

    assert a.evaluate(0.0, pins(DEAD_PIN)) == [], "nothing on the first sighting"
    assert a.evaluate(9.9, pins(DEAD_PIN)) == [], "nothing a tenth short of the sustain"

    out = a.evaluate(10.0, pins(DEAD_PIN))
    assert len(out) == 1, f"exactly one advisory at the sustain: {out}"
    one = out[0]
    assert one.kind == "dead_pin", one.kind
    assert one.pin == 3, one.pin
    assert one.first is True, "first warning of the episode"
    assert "pin 3" in one.message, one.message
    assert "0.20 A" in one.message, "the quiet pin's current is in the message"
    assert "9.00 A" in one.message, "the other five's average is in the message"
    assert one.short == "pin 3 near zero", one.short
    assert one.summary and len(one.summary) < len(one.message), "a shorter line to toast"

    assert a.evaluate(10.5, pins(DEAD_PIN)) == [], "one warning, not one per tick"


def test_a3_dead_pin_repeats_only_after_the_repeat_window(guard):
    """A3: no repeat before 600 s, one repeat after."""
    g = guard.g
    a = advisor(g)
    first_warning(a, DEAD_PIN)

    for tick in (11.0, 100.0, 300.0, 609.0):
        assert a.evaluate(tick, pins(DEAD_PIN)) == [], f"quiet at t={tick}"

    out = a.evaluate(SUSTAIN + REPEAT, pins(DEAD_PIN))
    assert len(out) == 1, f"one repeat at the repeat window: {out}"
    assert out[0].kind == "dead_pin" and out[0].pin == 3
    assert out[0].first is False, "a repeat is not the first warning"

    assert a.evaluate(SUSTAIN + REPEAT + 1.0, pins(DEAD_PIN)) == [], "then quiet again"


def test_a4_one_cleared_advisory_a_minute_after_the_condition_ends(guard):
    """A4: exactly one cleared advisory, 60 s after the fault goes, then silence."""
    g = guard.g
    a = advisor(g)
    first_warning(a, DEAD_PIN)

    gone = 100.0
    assert a.evaluate(gone, pins(HEALTHY)) == [], "no cleared line the moment it goes"
    assert a.evaluate(gone + CLEAR - 1.0, pins(HEALTHY)) == [], "not a second early"

    out = a.evaluate(gone + CLEAR, pins(HEALTHY))
    assert len(out) == 1, f"one cleared advisory: {out}"
    assert out[0].kind == "cleared", out[0].kind
    assert out[0].first is False, "a cleared line is not a warning"
    assert "cleared" in out[0].message.lower(), out[0].message
    assert "dead pin" in out[0].message, "it names the condition that ended"

    for tick in (gone + CLEAR + 1.0, gone + 500.0, gone + 5000.0):
        assert a.evaluate(tick, pins(HEALTHY)) == [], f"episode forgotten at t={tick}"


def test_a5_falling_under_the_load_gate_also_clears(guard):
    """A5: an active episode reads as absent below the gate, and clears there."""
    g = guard.g
    a = advisor(g)
    first_warning(a, DEAD_PIN)

    assert a.evaluate(50.0, pins(IDLE)) == [], "idle is absence, not a new condition"
    out = a.evaluate(50.0 + CLEAR, pins(IDLE))
    assert len(out) == 1 and out[0].kind == "cleared", f"cleared while idle: {out}"


def test_a6_imbalance_names_the_busiest_and_quietest_pins(guard):
    """A6: a 3.5 A spread with no dead pin, named after pin 6."""
    g = guard.g
    a = advisor(g)

    assert a.evaluate(0.0, pins(IMBALANCED)) == []
    out = a.evaluate(SUSTAIN, pins(IMBALANCED))
    assert len(out) == 1, f"one advisory at the sustain: {out}"
    one = out[0]
    assert one.kind == "imbalance", one.kind
    assert one.pin == 6, "the busiest pin is the one to name"
    assert one.first is True
    assert "pin 6" in one.message and "7.50 A" in one.message, one.message
    assert "pin 1" in one.message and "4.00 A" in one.message, one.message
    assert "3.50 A" in one.message, "the spread is in the message"
    assert "pin 6" in one.short, one.short

    # A healthy card has a spread too. It must not be enough.
    b = advisor(g)
    assert b.evaluate(0.0, pins(HEALTHY)) == []
    assert b.evaluate(SUSTAIN * 10, pins(HEALTHY)) == [], "0.3 A spread stays quiet"


def test_a7_dead_pin_takes_precedence_over_imbalance(guard):
    """A7: a dead pin is also a big spread. It is reported as the dead pin."""
    g = guard.g
    a = advisor(g)

    hi, lo = max(DEAD_PIN), min(DEAD_PIN)
    assert (hi - lo) >= g.DEFAULT_IMBALANCE_SPREAD_AMPS, "the pattern is both faults"

    one = first_warning(a, DEAD_PIN)
    assert one.kind == "dead_pin", "the more specific fault wins"


def test_a8_a_change_of_kind_starts_a_new_episode(guard):
    """A8: dead pin turning into an imbalance ends one episode and starts another."""
    g = guard.g
    a = advisor(g)
    first_warning(a, DEAD_PIN)

    out = a.evaluate(20.0, pins(IMBALANCED))
    assert len(out) == 1 and out[0].kind == "cleared", \
        f"the dead pin episode ends when the fault changes shape: {out}"

    assert a.evaluate(29.9, pins(IMBALANCED)) == [], "the new episode sustains first"
    out = a.evaluate(30.0, pins(IMBALANCED))
    assert len(out) == 1, f"the new episode warns on its own clock: {out}"
    assert out[0].kind == "imbalance" and out[0].first is True


def test_a9_reset_forgets_a_half_sustained_condition(guard):
    """A9: reset() drops a condition that had not yet earned a warning."""
    g = guard.g
    a = advisor(g)

    a.evaluate(0.0, pins(DEAD_PIN))
    a.evaluate(9.0, pins(DEAD_PIN))
    a.reset()

    assert a.evaluate(9.5, pins(DEAD_PIN)) == [], "the sustain clock restarted"
    assert a.evaluate(19.4, pins(DEAD_PIN)) == [], "still short of a fresh 10 s"
    out = a.evaluate(19.5, pins(DEAD_PIN))
    assert len(out) == 1 and out[0].first is True, f"a new first warning: {out}"


def test_a10_reset_forgets_a_reported_episode_too(guard):
    """A10: after reset() nothing is left to repeat or to clear."""
    g = guard.g
    a = advisor(g)
    first_warning(a, DEAD_PIN)
    a.reset()

    for tick in (100.0, 700.0, 5000.0):
        assert a.evaluate(tick, pins(HEALTHY)) == [], \
            f"no stale cleared line at t={tick}"


def test_a11_two_dead_pins_report_the_lowest_and_the_count(guard):
    """A11: more than one quiet pin names the quietest and says how many."""
    g = guard.g
    a = advisor(g)

    one = first_warning(a, TWO_DEAD_PINS)
    assert one.kind == "dead_pin"
    assert one.pin == 3, "pin 3 at 0.20 A is lower than pin 2 at 0.30 A"
    assert "2 pins" in one.message, one.message
    assert "0.20 A" in one.message, one.message


def test_a12_the_class_ignores_the_enabled_flag(guard):
    """A12: enabling is the caller's job. The class only classifies."""
    g = guard.g
    a = advisor(g, advisories_enabled=False)

    one = first_warning(a, DEAD_PIN)
    assert one.kind == "dead_pin", "PinAdvisor does not read the flag"


def test_a13_the_settings_are_the_documented_defaults(guard):
    """A13: the numbers the README quotes are the numbers that ship."""
    g = guard.g
    values = g.get_advisory_values()

    assert values["advisories_enabled"] is True
    assert values["advisory_load_amps"] == 18.0
    assert values["dead_pin_max_amps"] == 0.5
    assert values["imbalance_spread_amps"] == 3.0
    assert values["advisory_sustain_seconds"] == 10.0
    assert values["advisory_repeat_seconds"] == 600.0
    assert g.DEAD_PIN_OTHERS_MIN_AMPS == 3.0
    assert g.IMBALANCE_MIN_MAX_AMPS == 5.0
    assert g.ADVISORY_CLEAR_SEC == 60.0


def test_a14_the_on_off_flag_round_trips_through_config(guard):
    """A14: `advisories = off` in [Settings] turns them off, and back on."""
    g = guard.g

    for written, expected in (("off", False), ("on", True), ("false", False),
                              ("1", True), ("nonsense", True)):
        with open(g.CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write("[Settings]\nadvisories = %s\nimbalance_spread_amps = 2.5\n" % written)
        loaded = g.load_config()
        assert loaded["advisories_enabled"] is expected, \
            f"advisories = {written} -> {expected}"
        assert loaded["imbalance_spread_amps"] == 2.5, "the numbers load too"
        assert loaded["advisory_load_amps"] == 18.0, "an absent key keeps its default"


def test_a15_saving_a_setting_preserves_the_advisory_keys(guard):
    """A15: a tray threshold change must not eat a hand-edited advisory key."""
    g = guard.g
    with open(g.CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write("[Settings]\nadvisories = off\ndead_pin_max_amps = 0.75\n")

    assert g.save_config(9.5, 15.0), "config written"
    body = open(g.CONFIG_PATH, encoding="utf-8").read()
    assert "advisories = off" in body, body
    assert "dead_pin_max_amps = 0.75" in body, body


# =========================================================================
# Suite B: the real monitor_loop
# =========================================================================
def test_b1_the_loop_renders_a_dead_pin_advisory_and_nothing_else(loop_env):
    """B1: warning, toast, tooltip, flight file. No shutdown, no mitigation."""
    g, tmp = loop_env.g, str(loop_env.tmp)

    # The advisor is built at loop start, so the compressed sustain has to be in
    # place before the thread runs.
    g.set_advisory_values(advisory_sustain_seconds=0.05, advisory_repeat_seconds=600.0)
    FakeBackend.pins = DEAD_PIN

    log = run_loop(15.0, until=lambda: bool(
        glob.glob(os.path.join(tmp, "flight_*_advisory.csv"))))

    flights = glob.glob(os.path.join(tmp, "flight_*_advisory.csv"))
    assert len(flights) == 1, f"one flight_*_advisory.csv as evidence: {flights}"
    assert "0.200" in open(flights[0], encoding="utf-8").read(), \
        "the dump captured the quiet pin"

    warnings = [w for w in log.records["warning"] if "Pin advisory" in w]
    assert warnings, f"the advisory is a WARNING: {log.records['warning']}"
    assert "pin 3" in warnings[0], warnings[0]

    toasts = [t for t in loop_env.toasts if "advisory" in t[0]]
    assert toasts, f"the first warning toasts: {loop_env.toasts}"
    assert "Pin 3" in toasts[0][1], toasts[0]

    assert g.get_state()["advisory"] == "pin 3 near zero", g.get_state()["advisory"]
    assert "advisory: pin 3 near zero" in g.tray_title(), g.tray_title()

    assert not loop_env.shutdowns, f"an advisory never shuts down: {loop_env.shutdowns}"
    assert all(not m.engaged for m in loop_env.made), "an advisory never limits the GPU"
    assert not any("engage" in c for m in loop_env.made for c in m.calls), \
        "the mitigator was never asked to engage"


def test_b2_the_loop_honours_the_off_switch(loop_env):
    """B2: `advisories = off` and the same fault produces nothing at all."""
    g, tmp = loop_env.g, str(loop_env.tmp)

    g.set_advisory_values(advisories_enabled=False, advisory_sustain_seconds=0.05)
    FakeBackend.pins = DEAD_PIN

    log = run_loop(1.5)

    assert any("Advisories: off" in i for i in log.records["info"]), \
        f"the startup line says so: {log.records['info']}"
    assert not glob.glob(os.path.join(tmp, "flight_*_advisory.csv")), "no flight file"
    assert not [w for w in log.records["warning"] if "Pin advisory" in w], \
        f"no advisory warning: {log.records['warning']}"
    assert not [t for t in loop_env.toasts if "advisory" in t[0]], "no advisory toast"
    assert g.get_state()["advisory"] is None, "no tooltip suffix"


def test_b3_the_startup_line_names_the_settings(loop_env):
    """B3: with advisories on, the log says what they are set to."""
    g = loop_env.g
    g.set_advisory_values(advisory_sustain_seconds=0.05)
    FakeBackend.pins = HEALTHY

    log = run_loop(2.0)

    line = [i for i in log.records["info"] if i.startswith("Advisories (warn only)")]
    assert line, f"one startup line: {log.records['info']}"
    assert "load gate=18.0A" in line[0], line[0]
    assert "dead pin<=0.50A" in line[0], line[0]
    assert "spread>=3.00A" in line[0], line[0]
    assert not [w for w in log.records["warning"] if "Pin advisory" in w], \
        "a healthy card stays quiet"

"""The v1.1 tiered response.

Suite A drives TieredResponse.evaluate() directly with an injected clock, so every
window (grace, hold, clear, re-trigger) is exact rather than raced.
Suite B runs the real monitor_loop with a fake sensor and a stubbed mitigator on
compressed timings, to prove the wiring around it.

shutdown_windows is stubbed everywhere. Nothing here can power the machine off.
"""

import astral_i2c

from helpers import CountingLogger, FakeBackend, FakeMitigator, run_loop, running_loop, wait_until


def fresh(g, mitigator=None, mode=None, **tiers):
    """A TieredResponse with compressed, explicit windows."""
    g.set_config_values(9.5, 15.0)
    g.set_tier_values(
        critical_amps=13.0, critical_seconds=3.0, catastrophic_amps=16.0,
        mitigation_grace_seconds=10.0, mitigation_clear_seconds=60.0,
        mitigation_min_hold_seconds=120.0, retrigger_window_seconds=600.0,
    )
    if tiers:
        g.set_tier_values(**tiers)
    g.set_response_mode(g.RESPONSE_TIERED if mode is None else mode)
    log = CountingLogger()
    m = mitigator if mitigator is not None else FakeMitigator()
    return g.TieredResponse(lambda: m, log), m, log


def tick(r, t, amps, tier1=False, threshold=9.5):
    return r.evaluate(now=t, max_val=amps, max_label="GPU 12VHPWR Pin3 Current",
                      max_unit="A", threshold=threshold, tier1_expired=tier1,
                      tier1_reason=f"sustained test, {amps:.2f}A")


# =========================================================================
# Suite A: the ladder, on an injected clock
# =========================================================================
def test_a1_tier1_mitigation_works(guard):
    """A1: tier 1, mitigation works."""
    g = guard.g
    r, m, _log = fresh(g)

    out = tick(r, 0.0, 10.0, tier1=True)
    assert out is None and r.engaged, "tier 1 engages instead of shutting down"
    assert m.count("engage") == 1 and "tier 1" in m.calls[0], f"engage names the tier: {m.calls}"

    assert tick(r, 1.0, 1.0) is None, "no shutdown while current is falling"
    assert tick(r, 10.0, 1.0) is None, "still no shutdown at the grace deadline"
    assert r.grace_deadline is None, "grace cleared once current came down"
    assert tick(r, 61.0, 1.0) is None and r.engaged, "holds through the clear window"
    assert tick(r, 119.0, 1.0) is None and r.engaged, "holds until the minimum hold expires"

    tick(r, 120.0, 1.0)
    assert not r.engaged and m.count("release") == 1, "releases after hold + clear"
    assert r.released_at == 120.0, "release arms the re-trigger window"


def test_a2_tier1_mitigation_does_not_help(guard):
    """A2: tier 1, mitigation does not help."""
    g = guard.g
    r, _m, _log = fresh(g)

    tick(r, 0.0, 10.0, tier1=True)
    assert tick(r, 9.0, 10.0) is None, "no shutdown before the grace window ends"
    out = tick(r, 10.0, 10.0, tier1=True)
    assert out is not None, "shuts down when the current stays high"
    assert "Tier 1" in out, f"reason names tier 1: {out}"
    assert "did not fix it" in out, f"reason says limiting did not work: {out}"


def test_a3_tier2_critical(guard):
    """A3: tier 2 critical."""
    g = guard.g
    r, m, _log = fresh(g)

    assert tick(r, 0.0, 13.5) is None and not r.engaged, "13.5A alone does not act"
    assert tick(r, 2.0, 13.5) is None and not r.engaged, "still waiting at 2s"
    out = tick(r, 3.0, 13.5)
    assert out is None and r.engaged, "engages at 3s, long before tier 1's 15s"
    assert "tier 2" in m.calls[-1], f"engage names tier 2: {m.calls}"
    assert tick(r, 4.0, 1.0) is None, "no shutdown once the current drops"

    r2, m2, _ = fresh(g)
    tick(r2, 0.0, 13.5)
    tick(r2, 1.0, 5.0)
    assert r2.critical_since is None, "dropping below critical restarts tier 2's window"
    assert m2.count("engage") == 0, f"no engage after a brief spike: {m2.calls}"


def test_a4_tier3_catastrophic(guard):
    """A4: tier 3 catastrophic."""
    g = guard.g
    r, m, _log = fresh(g)

    assert tick(r, 0.0, 17.0) is None, "one sample at 17A is not enough"
    out = tick(r, 0.5, 17.0)
    assert out is not None, "two consecutive samples shut down immediately"
    assert "Tier 3" in out, f"reason names tier 3: {out}"
    assert m.count("engage") == 1 and r.engaged, f"the GPU is limited on the way down: {m.calls}"

    r, m, _log = fresh(g)
    tick(r, 0.0, 17.0)
    tick(r, 0.5, 2.0)
    assert r.catastrophic_samples == 0, "a single spike between samples resets the count"
    assert tick(r, 1.0, 17.0) is None, "no shutdown from a lone spike"


def test_a4b_garbage_filter_upstream_of_the_tiers(guard):
    """A4b: the garbage filter upstream of the tiers."""
    assert not astral_i2c.is_plausible(12.0, 31.0), "31A is rejected as implausible"
    assert not astral_i2c.is_plausible(12.0, 65.535), "0xFFFF garbage is rejected"
    assert astral_i2c.is_plausible(12.0, 16.5), "a real 16.5A catastrophic reading passes"
    assert astral_i2c.is_plausible(12.0, 15.0), "a real 15A reading passes"


def test_a5_retrigger_after_a_release(guard):
    """A5: re-trigger after a release."""
    g = guard.g
    r, _m, _log = fresh(g)

    tick(r, 0.0, 10.0, tier1=True)
    tick(r, 1.0, 1.0)
    tick(r, 130.0, 1.0)
    assert not r.engaged and r.released_at == 130.0, "released after the hold"
    out = tick(r, 200.0, 10.0, tier1=True)
    assert out is not None, "a fault inside the window shuts down"
    assert "re-triggered" in out, f"reason says re-triggered: {out}"
    assert "Persistent fault" in out, f"reason says persistent: {out}"

    r, _m, _log = fresh(g)
    tick(r, 0.0, 10.0, tier1=True)
    tick(r, 1.0, 1.0)
    tick(r, 130.0, 1.0)
    out = tick(r, 900.0, 10.0, tier1=True)
    assert out is None and r.engaged, "a fault after the window mitigates again"


def test_a6_shutdown_only_mode(guard):
    """A6: shutdown-only mode."""
    g = guard.g

    def explode():
        raise AssertionError("shutdown_only must never touch the mitigator")

    g.set_config_values(9.5, 15.0)
    g.set_tier_values(critical_amps=13.0, critical_seconds=3.0, catastrophic_amps=16.0)
    g.set_response_mode(g.RESPONSE_SHUTDOWN_ONLY)
    r = g.TieredResponse(explode, CountingLogger())

    assert tick(r, 0.0, 13.5) is None, "13.5A waits out tier 2's window"
    out = tick(r, 3.0, 13.5)
    assert out is not None, "shuts down at 3s, not 15s"
    assert "Tier 2" in out, f"reason names tier 2: {out}"
    assert "No mitigation attempted" in out, f"reason says no mitigation was tried: {out}"
    assert "shutdown-only" in out, f"reason names the mode: {out}"

    r2 = g.TieredResponse(explode, CountingLogger())
    out = tick(r2, 0.0, 10.0, tier1=True)
    assert out is not None and "Tier 1" in out, "tier 1 still shuts down in shutdown-only"


def test_a7_switching_modes_at_runtime(guard):
    """A7: switching modes at runtime."""
    g = guard.g
    r, m, _log = fresh(g)

    tick(r, 0.0, 10.0, tier1=True)
    assert r.engaged, "mitigated before the switch"

    g.set_response_mode(g.RESPONSE_SHUTDOWN_ONLY)
    out = tick(r, 1.0, 1.0)
    assert not r.engaged and m.count("release") == 1, \
        "switching to shutdown-only gives the GPU back"
    assert out is None, "no shutdown just from switching"
    assert r.released_at is None, "a user-driven release does not arm the re-trigger window"
    out = tick(r, 2.0, 10.0, tier1=True)
    assert out is not None and "re-triggered" not in out, \
        f"the next fault shuts down, not re-trigger: {out}"

    g.set_response_mode(g.RESPONSE_TIERED)
    r, _m, _log = fresh(g)
    assert tick(r, 0.0, 10.0, tier1=True) is None and r.engaged, \
        "switching back to tiered mitigates again"


def test_a8_mitigation_unavailable(guard):
    """A8: mitigation unavailable."""
    g = guard.g
    m = FakeMitigator(available=False, reason="not elevated")
    r, _m, log = fresh(g, mitigator=m)

    assert r.effective_mode() == g.RESPONSE_SHUTDOWN_ONLY, "tiered downgrades to shutdown-only"
    out = tick(r, 0.0, 10.0, tier1=True)
    assert out is not None, "tier 1 shuts down"
    assert "not elevated" in out, f"reason explains why there was no mitigation: {out}"
    assert m.count("engage") == 0, f"nothing was engaged: {m.calls}"

    for _ in range(5):
        r.effective_mode()
    warned = [w for w in log.records["warning"] if "shutdown-only" in w]
    assert len(warned) == 1, f"the warning is logged once, not every tick: {warned}"


def test_a9_engage_itself_failing(guard):
    """A9: engage itself failing."""
    g = guard.g
    m = FakeMitigator(available=True, engage_ok=False)
    r, _m, _log = fresh(g, mitigator=m)

    out = tick(r, 0.0, 10.0, tier1=True)
    assert out is not None, "a failed engage falls through to shutdown"
    assert "failed" in out, f"reason says the attempt failed: {out}"
    assert not r.engaged, "state is not left claiming to be mitigated"


def test_a10_pause_and_resume_releases(guard):
    """A10: pause and resume releases."""
    g = guard.g
    r, m, _log = fresh(g)

    tick(r, 0.0, 10.0, tier1=True)
    r.release(5.0, "monitoring paused", arm_retrigger=False)
    assert not r.engaged and m.count("release") == 1, "pause releases the GPU"
    assert r.released_at is None, "pause does not arm the re-trigger window"

    r, _m, _log = fresh(g)
    tick(r, 0.0, 10.0, tier1=True)
    r.release(5.0, "suspend/resume gap", arm_retrigger=False)
    r.reset_timers()
    assert r.released_at is None and r.critical_since is None and r.catastrophic_samples == 0, \
        "a resume gap clears every timer"


# =========================================================================
# Suite B: the real monitor_loop, on compressed timings
# =========================================================================
def test_b1_tier1_through_the_real_loop(loop_env):
    """B1: tier 1 through the real loop."""
    made, shutdowns = loop_env.made, loop_env.shutdowns
    FakeBackend.amps = 10.0

    def flip_when_engaged():
        # Once the guard limits the card, pretend the current actually fell.
        if made and any(m.engaged for m in made):
            FakeBackend.amps = 1.0
            return True
        return False

    run_loop(12.0, until=flip_when_engaged)

    engaged_any = [m for m in made if m.count("engage")]
    assert any(m.recovery_ran for m in made), f"startup recovery ran: {[m.calls for m in made]}"
    assert len(engaged_any) >= 1, f"the loop engaged the mitigator: {[m.calls for m in made]}"
    assert shutdowns == [], f"no shutdown when limiting worked: {shutdowns}"
    assert any(m.count("close") for m in made), \
        f"the mitigator was closed on exit: {[m.calls for m in made]}"


def test_b2_tier1_where_limiting_does_not_help(loop_env):
    """B2: tier 1 where limiting does not help."""
    made, shutdowns = loop_env.made, loop_env.shutdowns
    FakeBackend.amps = 10.0

    log = run_loop(12.0, until=lambda: bool(shutdowns))

    assert len(shutdowns) == 1, f"shutdown fired: {shutdowns}"
    assert "Tier 1" in shutdowns[0], f"reason names tier 1: {shutdowns}"
    assert any(m.count("engage") for m in made), \
        f"the GPU was limited first: {[m.calls for m in made]}"
    assert any(m.engaged for m in made), \
        f"mitigation is left engaged through shutdown: {[m.calls for m in made]}"
    assert "marker file" in log.all_text(), \
        f"the log explains the marker will clean up: {log.records['info'][-3:]}"


def test_b3_status_and_tray_while_limited(loop_env):
    """B3: status and tray while limited."""
    g = loop_env.g
    FakeBackend.amps = 10.0
    seen = {}

    def capture_when_engaged():
        st = g.get_state()
        if st.get("status") == g.Status.MITIGATED:
            seen.update(st)
            seen["title"] = g.tray_title()
            FakeBackend.amps = 1.0
            return True
        return False

    run_loop(12.0, until=capture_when_engaged)

    assert seen.get("status") == g.Status.MITIGATED, f"tray status becomes MITIGATED: {seen}"
    assert "LIMITED" in seen.get("title", ""), f"tooltip says LIMITED: {seen.get('title')}"
    assert "Pin3" in seen.get("title", "") and "fake" in seen.get("title", ""), \
        f"tooltip keeps the pin and source: {seen.get('title')}"
    assert g.get_state().get("mitigation") == "ready", "mitigation state is reported ready"


def test_b4_shutdown_only_through_the_real_loop(loop_env):
    """B4: shutdown-only through the real loop."""
    g = loop_env.g
    made, shutdowns = loop_env.made, loop_env.shutdowns
    g.set_response_mode(g.RESPONSE_SHUTDOWN_ONLY)
    FakeBackend.amps = 13.5

    run_loop(12.0, until=lambda: bool(shutdowns))

    assert len(shutdowns) == 1, f"shutdown fired: {shutdowns}"
    assert "Tier 2" in shutdowns[0], \
        f"it was tier 2's 0.15s window, not tier 1's 0.3s: {shutdowns}"
    # The startup marker check still runs (rule 2: a crash while mitigated must be
    # cleaned up whatever mode we are in now), but with no marker it binds nothing.
    assert all(m.count("connect") == 0 for m in made), \
        f"NVML was never connected: {[m.calls for m in made]}"
    assert all(m.count("engage") == 0 and m.count("release") == 0 for m in made), \
        f"nothing was ever engaged or released: {[m.calls for m in made]}"


def test_b5_pause_releases_the_gpu(loop_env):
    """B5: pause releases the GPU."""
    g = loop_env.g
    made, shutdowns = loop_env.made, loop_env.shutdowns
    FakeBackend.amps = 10.0

    with running_loop():
        engaged_before = wait_until(lambda: any(m.engaged for m in made), 9.0)
        g.set_state(paused=True)
        # The paused branch ticks every 0.5s, so the release lands within one of
        # those rather than immediately.
        released = wait_until(
            lambda: bool(made) and not any(m.engaged for m in made), 6.0)
    g.set_state(paused=False)

    assert engaged_before, f"the guard had limited the GPU: {[m.calls for m in made]}"
    assert released, f"pausing released it: {[m.calls for m in made]}"
    assert shutdowns == [], f"no shutdown from pausing: {shutdowns}"


def test_b6_tooltip_when_mitigation_is_unavailable(guard):
    """B6: tooltip when mitigation is unavailable.

    Tray state only, so this one needs no loop wiring.
    """
    g = guard.g
    # The mode suffix only shows on the healthy tooltip; a missing sensor or an
    # error has more urgent things to say. The original suite inherited an OK-ish
    # status from the scenario before it, this says so out loud.
    g.set_state(status=g.Status.OK)
    g.set_state(mitigation="not elevated")
    g.set_response_mode(g.RESPONSE_TIERED)

    assert g.effective_shutdown_only(), "effective mode is shutdown-only"
    assert "shutdown-only" in g.tray_title(), f"tooltip warns: {g.tray_title()}"
    assert "unavailable - not elevated" in g.tiered_menu_label(), \
        f"the menu label says why: {g.tiered_menu_label()}"

    g.set_state(mitigation="ready")
    assert not g.effective_shutdown_only(), "and goes quiet once it is available"
    assert "shutdown-only" not in g.tray_title(), "normal tooltip has no mode suffix"

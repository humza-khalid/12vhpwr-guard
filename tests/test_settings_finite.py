"""nan and inf in config.ini must never switch a protection tier off (B-018).

`nan <= 0` is False and `inf > 0` is True, so before this fix `threshold_amps = nan` was
accepted and tier 1 silently never fired, and `catastrophic_amps = inf` did the same to tier 3.
Linux-safe: only guard_core is imported. The Windows loader is held to the same results by
tests/test_settings_parity.py.
"""

import pytest

import guard_core


def load(tmp_path, line):
    path = tmp_path / "config.ini"
    path.write_text(f"[Settings]\n{line}\n", encoding="utf-8")
    return guard_core.load_settings(str(path))


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "NaN", "Infinity"])
@pytest.mark.parametrize("key", ["threshold_amps", "sustained_seconds_required"])
def test_non_finite_tier1_values_fall_back_to_the_defaults(tmp_path, key, value):
    loaded = load(tmp_path, f"{key} = {value}")
    defaults = guard_core.default_settings()
    assert loaded["threshold_amps"] == defaults["threshold_amps"]
    assert loaded["sustained_seconds_required"] == defaults["sustained_seconds_required"]


@pytest.mark.parametrize("value", ["nan", "inf"])
@pytest.mark.parametrize("key", sorted(guard_core.DEFAULT_TIER_VALUES))
def test_non_finite_tier_values_fall_back_one_by_one(tmp_path, key, value):
    loaded = load(tmp_path, f"{key} = {value}")
    assert loaded[key] == guard_core.DEFAULT_TIER_VALUES[key]


@pytest.mark.parametrize("value", ["nan", "inf"])
@pytest.mark.parametrize("key", sorted(k for k in guard_core.DEFAULT_ADVISORY_VALUES
                                       if k != "advisories_enabled"))
def test_non_finite_advisory_values_fall_back(tmp_path, key, value):
    loaded = load(tmp_path, f"{key} = {value}")
    assert loaded[key] == guard_core.DEFAULT_ADVISORY_VALUES[key]


@pytest.mark.parametrize("value", ["nan", "inf"])
def test_non_finite_restart_window_falls_back(tmp_path, value):
    loaded = load(tmp_path, f"sensor_loss_restart_seconds = {value}")
    assert loaded["sensor_loss_restart_seconds"] == \
        guard_core.default_settings()["sensor_loss_restart_seconds"]


def test_zero_restart_window_still_means_off(tmp_path):
    assert load(tmp_path, "sensor_loss_restart_seconds = 0")["sensor_loss_restart_seconds"] == 0

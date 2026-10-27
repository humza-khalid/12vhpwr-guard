"""guard_core.load_settings against the Windows guard's load_config, file for file.

Both platforms read the same config.ini keys, and the Linux daemon reads them through
guard_core.load_settings while the Windows guard keeps its own load_config. This feeds the
same files through both and requires the same answer for every key, quirks included (a bad
number anywhere throws the whole file away; a non-positive tier value falls back alone; 0
is a real value for the self-restart window).

It imports the Windows module, so it is Windows only.
"""

import math
import sys

import pytest

if sys.platform != "win32":
    pytest.skip("imports the Windows guard module", allow_module_level=True)

import guard_core  # noqa: E402
import hwinfo_12vhpwr_guard as g  # noqa: E402

ALL_KEYS = (
    "threshold_amps = 10.5\n"
    "sustained_seconds_required = 20\n"
    "sensor_backend = Astral\n"
    "response_mode = SHUTDOWN_ONLY\n"
    "critical_amps = 12.5\n"
    "critical_seconds = 4\n"
    "catastrophic_amps = 17\n"
    "mitigation_grace_seconds = 8\n"
    "mitigation_clear_seconds = 45\n"
    "mitigation_min_hold_seconds = 90\n"
    "retrigger_window_seconds = 300\n"
    "advisories = off\n"
    "advisory_load_amps = 20\n"
    "dead_pin_max_amps = 0.4\n"
    "imbalance_spread_amps = 2.5\n"
    "advisory_sustain_seconds = 12\n"
    "advisory_repeat_seconds = 900\n"
    "sensor_loss_restart_seconds = 120\n"
)

CASES = {
    "empty file": "",
    "no settings section": "[Other]\nthreshold_amps = 3\n",
    "empty section": "[Settings]\n",
    "every key set": "[Settings]\n" + ALL_KEYS,
    "negative threshold throws the file away": "[Settings]\nthreshold_amps = -1\ncritical_amps = 12\n",
    "zero sustained throws the file away": "[Settings]\nsustained_seconds_required = 0\n",
    "garbage threshold throws the file away": "[Settings]\nthreshold_amps = lots\nadvisories = off\n",
    "garbage tier value throws the file away": "[Settings]\ncritical_amps = twelve\n",
    "garbage advisory number throws the file away": "[Settings]\nadvisory_load_amps = x\n",
    "garbage restart throws the file away": "[Settings]\nsensor_loss_restart_seconds = soon\n",
    "non-positive tier values fall back alone": (
        "[Settings]\nthreshold_amps = 9\ncritical_amps = 0\ncritical_seconds = -2\n"
        "catastrophic_amps = 18\nmitigation_grace_seconds = 0\n"),
    "non-positive advisory values fall back alone": (
        "[Settings]\nadvisory_load_amps = 0\ndead_pin_max_amps = -1\n"
        "imbalance_spread_amps = 4\nadvisory_repeat_seconds = 0\n"),
    "unknown backend and mode fall back alone": (
        "[Settings]\nsensor_backend = nvapi\nresponse_mode = panic\nthreshold_amps = 8\n"),
    "advisories off": "[Settings]\nadvisories = off\n",
    "advisories no": "[Settings]\nadvisories = No\n",
    "advisories 0": "[Settings]\nadvisories = 0\n",
    "advisories false": "[Settings]\nadvisories = FALSE\n",
    "advisories on": "[Settings]\nadvisories = on\n",
    "advisories garbage keeps the default": "[Settings]\nadvisories = maybe\n",
    "restart zero is off": "[Settings]\nsensor_loss_restart_seconds = 0\n",
    "restart negative falls back": "[Settings]\nsensor_loss_restart_seconds = -5\n",
    "whitespace and case": "[Settings]\nsensor_backend =   HWiNFO  \nresponse_mode =  Tiered \n",
    "interpolation syntax error": "[Settings]\nthreshold_amps = 9%\n",
    "duplicate key": "[Settings]\nthreshold_amps = 9\nthreshold_amps = 10\n",
    "not an ini file": "threshold_amps = 9\n",
    "nan and inf pass straight through": (
        "[Settings]\nthreshold_amps = nan\nsustained_seconds_required = inf\n"
        "critical_amps = nan\nsensor_loss_restart_seconds = nan\n"),
}


def same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return type(a) is type(b) and a == b


def assert_parity(windows: dict, shared: dict) -> None:
    assert set(windows) == set(shared)
    for key in windows:
        assert same(windows[key], shared[key]), (key, windows[key], shared[key])


@pytest.mark.parametrize("name", sorted(CASES))
def test_same_answer_for_every_file(name, tmp_path, monkeypatch):
    path = tmp_path / "config.ini"
    path.write_text(CASES[name], encoding="utf-8")
    monkeypatch.setattr(g, "CONFIG_PATH", str(path))
    assert_parity(g.load_config(), guard_core.load_settings(str(path)))


def test_missing_file_is_the_defaults(tmp_path, monkeypatch):
    path = tmp_path / "absent.ini"
    monkeypatch.setattr(g, "CONFIG_PATH", str(path))
    windows = g.load_config()
    assert_parity(windows, guard_core.load_settings(str(path)))
    assert_parity(windows, guard_core.load_settings(None))
    assert_parity(windows, guard_core.default_settings())


def test_undecodable_file(tmp_path, monkeypatch):
    path = tmp_path / "config.ini"
    path.write_bytes(b"[Settings]\nthreshold_amps = 9\n\xff\xfe\x00junk\n")
    monkeypatch.setattr(g, "CONFIG_PATH", str(path))
    assert_parity(g.load_config(), guard_core.load_settings(str(path)))


def test_every_key_set_is_actually_read(tmp_path):
    """Guards the parity cases above against both sides silently returning defaults."""
    path = tmp_path / "config.ini"
    path.write_text("[Settings]\n" + ALL_KEYS, encoding="utf-8")
    loaded = guard_core.load_settings(str(path))
    assert loaded["threshold_amps"] == 10.5
    assert loaded["sensor_backend"] == "astral"
    assert loaded["response_mode"] == guard_core.RESPONSE_SHUTDOWN_ONLY
    assert loaded["mitigation_min_hold_seconds"] == 90.0
    assert loaded["advisories_enabled"] is False
    assert loaded["advisory_repeat_seconds"] == 900.0
    assert loaded["sensor_loss_restart_seconds"] == 120.0

"""Test fixtures for 12VHPWR Guard.

The guard is a single module of globals, so the fixtures here exist to make each
test independent of every other one: paths point at a tmp_path, side effects
(toast, Event Viewer) are stubbed out, and every global a test can reach is
snapshotted on the way in and put back on the way out.

Redirecting CONFIG_PATH, LOG_DIR and LAST_SHUTDOWN_PATH is not optional hygiene.
Without it a run writes flight recorder CSVs into the repo's logs/ directory and
can delete a real last_shutdown.json before the user has seen it.

These tests import the guard, which imports ctypes.wintypes, so they are Windows
only.
"""

import os
import sys
import types

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
# The repo root so "import hwinfo_12vhpwr_guard" works from any cwd, and this
# directory so "import helpers" works whatever import mode pytest is run in.
for _path in (TESTS_DIR, REPO_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import hwinfo_12vhpwr_guard as g  # noqa: E402
from helpers import FakeBackend, FakeMitigator  # noqa: E402

# Module globals a test is allowed to change. Snapshotted before the test and
# written back afterwards, so a test can run alone, in any order, or in any
# subset. On a clean tree the restored values are the documented defaults:
# tiered response, PAUSE_REMINDER_EVERY_SEC 1800.0, the DEFAULT_TIER_VALUES.
RESTORED_GLOBALS = (
    "LOGGER",
    "CONFIG_PATH",
    "LOG_DIR",
    "LAST_SHUTDOWN_PATH",
    "POLL_INTERVAL_SEC",
    "PAUSE_REMINDER_EVERY_SEC",
    "THRESHOLD_AMPS",
    "SUSTAINED_SECONDS_REQUIRED",
    "RESPONSE_MODE",
    "SENSOR_BACKEND",
    "HAVE_EVENTLOG",
    "HAVE_MITIGATION",
    "GpuMitigator",
    "candidate_backends",
    "shutdown_windows",
    "toast",
)


@pytest.fixture
def guard(tmp_path):
    """The guard module with every side effect pointed somewhere harmless.

    Returns a namespace: .g is the module, .toasts is every toast raised during
    the test as (title, msg), .tmp is the directory standing in for both the log
    directory and the install directory.
    """
    saved = {name: getattr(g, name) for name in RESTORED_GLOBALS}
    saved_tiers = dict(g.TIER_VALUES)
    saved_advisories = dict(g.ADVISORY_VALUES)
    saved_shared = dict(g.shared)

    toasts = []

    # Redirected before the test body runs, so no setter, dump or shutdown note
    # can reach the repo. CONFIG_PATH is included even though only the tray
    # handlers write it, because a test is one call away from doing so.
    g.CONFIG_PATH = str(tmp_path / "config.ini")
    g.LOG_DIR = str(tmp_path)
    g.LAST_SHUTDOWN_PATH = str(tmp_path / "last_shutdown.json")
    g.HAVE_EVENTLOG = False
    g.toast = lambda title, msg="": toasts.append((title, msg))

    yield types.SimpleNamespace(g=g, toasts=toasts, tmp=tmp_path)

    for name, value in saved.items():
        setattr(g, name, value)
    # These are mutated in place by set_tier_values(), set_advisory_values() and
    # set_state(), so putting the object back is not enough.
    g.TIER_VALUES.clear()
    g.TIER_VALUES.update(saved_tiers)
    g.ADVISORY_VALUES.clear()
    g.ADVISORY_VALUES.update(saved_advisories)
    g.shared.clear()
    g.shared.update(saved_shared)
    FakeMitigator.AVAILABLE = True
    FakeBackend.amps = 0.5
    FakeBackend.pins = None


@pytest.fixture
def loop_env(guard):
    """A guard wired up to drive the real monitor_loop with fakes.

    Everything is compressed so a full engage, hold and release cycle takes about
    a second. shutdown_windows is stubbed, so nothing here can power the machine
    off; .shutdowns collects the reasons it would have used and .made collects
    every mitigator the loop built.
    """
    env = guard
    shutdowns = []
    made = []

    # The v1.3 suite needs the note the real shutdown_windows leaves behind; the
    # v1.1 suite does not, and would announce it back on a second run in the same
    # test. Off by default, switched on where it is the thing under test.
    env.note_on_shutdown = False
    env.shutdowns = shutdowns
    env.made = made

    def stub_shutdown(logger, reason):
        shutdowns.append(reason)
        if env.note_on_shutdown:
            g.note_shutdown_reason(reason)

    def fake_factory(logger=None):
        m = FakeMitigator()
        made.append(m)
        return m

    g.shutdown_windows = stub_shutdown
    g.candidate_backends = lambda logger: [FakeBackend]
    g.GpuMitigator = fake_factory
    g.HAVE_MITIGATION = True

    g.POLL_INTERVAL_SEC = 0.02
    g.set_config_values(9.5, 0.3)
    g.set_tier_values(
        critical_amps=13.0, critical_seconds=0.15, catastrophic_amps=16.0,
        mitigation_grace_seconds=0.3, mitigation_clear_seconds=0.2,
        mitigation_min_hold_seconds=0.4, retrigger_window_seconds=5.0,
    )
    g.set_response_mode(g.RESPONSE_TIERED)
    return env

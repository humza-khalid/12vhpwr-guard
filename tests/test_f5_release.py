"""F5 (B-005): the power limit is read back after every restore.

These drive the real GpuMitigator with a fake NVML injected through nvml_factory, so
nothing here loads nvml.dll, binds a handle or touches the card. The marker file lives
in tmp_path, never next to config.ini.

The knobs on FakeNvml are the three ways a restore goes wrong in the field: the driver
accepts a set and drops it, the card refuses to leave its floor, and the read itself
fails. Each is flipped after engage(), because that is when a real one would start.
"""

import pytest

from gpu_mitigation import MARKER_NAME, GpuMitigator, NvmlError
from helpers import CountingLogger

SUB_ID = 0x89E31043
GPU_NAME = "NVIDIA GeForce RTX 5090"
USER_LIMIT_MW = 600_000
MIN_MW = 400_000
MAX_MW = 600_000
IDLE_CLOCK_MHZ = 2610
FLOOR_CLOCK_MHZ = 180


class FakeNvml:
    """Stands in for _Nvml: one card, and knobs for the ways a restore can fail.

    Mirrors the real signatures (every call takes the opaque handle first) and records
    every power limit set so a test can count the ones a release issued.
    """

    HANDLE = "handle-0"

    def __init__(self, limit_mw=USER_LIMIT_MW):
        self.limit_mw = limit_mw
        self.set_calls = []
        self.locked_clocks = None
        self.reset_clock_calls = 0
        self.initialized = False

        # Knobs.
        self.ignore_sets = 0      # silently drop this many set_power_limit_mw calls
        self.stuck_at = None      # the limit always reads back as this value
        self.fail_reads = False   # power_limit_mw raises NvmlError

    # -- lifecycle
    def init(self):
        self.initialized = True

    def shutdown(self):
        self.initialized = False

    # -- enumeration
    def device_count(self):
        return 1

    def handle(self, index):
        if index != 0:
            raise NvmlError("nvmlDeviceGetHandleByIndex_v2", 2)
        return self.HANDLE

    def subsystem_id(self, handle):
        return SUB_ID

    def name(self, handle):
        return GPU_NAME

    # -- power
    def power_limit_mw(self, handle):
        if self.fail_reads:
            raise NvmlError("nvmlDeviceGetPowerManagementLimit", 3)
        return self.limit_mw if self.stuck_at is None else self.stuck_at

    def power_limit_constraints_mw(self, handle):
        return MIN_MW, MAX_MW

    def set_power_limit_mw(self, handle, milliwatts):
        self.set_calls.append(int(milliwatts))
        if self.ignore_sets > 0:
            # Accepted and quietly dropped, which is what the read-back exists to catch.
            self.ignore_sets -= 1
            return
        self.limit_mw = int(milliwatts)

    # -- clocks
    def set_locked_clocks(self, handle, min_mhz, max_mhz):
        self.locked_clocks = (int(min_mhz), int(max_mhz))

    def reset_locked_clocks(self, handle):
        self.reset_clock_calls += 1
        self.locked_clocks = None

    def graphics_clock_mhz(self, handle):
        return FLOOR_CLOCK_MHZ if self.locked_clocks else IDLE_CLOCK_MHZ

    def min_supported_graphics_clock_mhz(self, handle):
        return FLOOR_CLOCK_MHZ


@pytest.fixture
def fake():
    return FakeNvml()


@pytest.fixture
def marker(tmp_path):
    return tmp_path / MARKER_NAME


def make_mitigator(tmp_path, fake, logger=None):
    """A real GpuMitigator wired to the fake card, elevated, marker in tmp_path."""
    return GpuMitigator(
        marker_path=str(tmp_path / MARKER_NAME),
        nvml_factory=lambda: fake,
        elevation_check=lambda: True,
        logger=logger or CountingLogger(),
    )


def engage_one(tmp_path, fake, logger=None):
    """Connect and engage, the way the tier engine does. Returns the mitigator."""
    m = make_mitigator(tmp_path, fake, logger)
    assert m.connect(SUB_ID) is True, m.unavailable_reason
    assert m.engage("test") is True
    return m


# =========================================================================
# engage, unchanged by F5
# =========================================================================
def test_engage_still_drops_the_limit_to_the_minimum(tmp_path, fake, marker):
    m = engage_one(tmp_path, fake)

    assert m.engaged is True
    assert fake.limit_mw == MIN_MW
    assert fake.locked_clocks == (FLOOR_CLOCK_MHZ, FLOOR_CLOCK_MHZ)
    assert marker.exists(), "engage writes the crash recovery marker"
    assert m.last_restore_warning is None


# =========================================================================
# release
# =========================================================================
def test_release_verified_first_try(tmp_path, fake, marker):
    """The happy path: one set, one read-back that agrees, marker gone."""
    log = CountingLogger()
    m = engage_one(tmp_path, fake, log)
    sets_before = len(fake.set_calls)
    warnings_before = len(log.records["warning"])

    assert m.release() is True
    assert not marker.exists()
    assert fake.limit_mw == USER_LIMIT_MW
    assert m.last_restore_warning is None
    assert fake.set_calls[sets_before:] == [USER_LIMIT_MW], \
        "exactly one set to the saved limit, no retry when the first one took"
    assert len(log.records["warning"]) == warnings_before, "nothing new to warn about"


def test_release_retries_once_when_the_first_set_is_dropped(tmp_path, fake, marker):
    """ignore_sets=1: the driver eats the first request, the retry lands."""
    log = CountingLogger()
    m = engage_one(tmp_path, fake, log)
    sets_before = len(fake.set_calls)
    warnings_before = len(log.records["warning"])
    fake.ignore_sets = 1

    assert m.release() is True
    assert not marker.exists()
    assert fake.limit_mw == USER_LIMIT_MW
    assert m.last_restore_warning is None
    assert fake.set_calls[sets_before:] == [USER_LIMIT_MW, USER_LIMIT_MW], \
        "the set is issued twice, the second one after the read-back disagreed"
    assert len(log.records["warning"]) == warnings_before, \
        f"a retry that worked is not worth a warning: {log.records['warning']}"


def test_release_warns_and_keeps_the_marker_when_the_card_is_stuck(tmp_path, fake, marker):
    """stuck_at: the card reports its floor whatever we set, so the release is not done."""
    log = CountingLogger()
    m = engage_one(tmp_path, fake, log)
    warnings_before = len(log.records["warning"])
    fake.stuck_at = MIN_MW

    assert m.release() is False
    assert marker.exists(), \
        "a limit that verifiably did not restore keeps its marker for the next start"

    warned = log.records["warning"][warnings_before:]
    assert len(warned) == 1, f"one warning, not one per attempt: {warned}"
    assert "400000" in warned[0] and "600000" in warned[0]

    assert m.last_restore_warning is not None
    assert "400000" in m.last_restore_warning, "what the card reports"
    assert "600000" in m.last_restore_warning, "what the user had"


def test_release_is_idempotent_without_dropping_the_kept_marker(tmp_path, fake, marker):
    """close() calls release() a second time; that must not delete the evidence."""
    m = engage_one(tmp_path, fake)
    fake.stuck_at = MIN_MW
    assert m.release() is False

    assert m.release() is False, "still not restored, so still not a success"
    assert marker.exists()

    m.close()
    assert marker.exists(), "close() releases again and must leave the marker alone"


def test_release_treats_a_failed_read_back_as_unverified(tmp_path, fake, marker):
    """fail_reads: the set went out but nothing can confirm it, so do not assume."""
    log = CountingLogger()
    m = engage_one(tmp_path, fake, log)
    warnings_before = len(log.records["warning"])
    fake.fail_reads = True

    assert m.release() is False
    assert marker.exists()

    warned = log.records["warning"][warnings_before:]
    assert len(warned) == 1, f"one warning: {warned}"
    assert "nvmlDeviceGetPowerManagementLimit" in warned[0], \
        f"the NVML error is named: {warned[0]}"
    assert "600000" in warned[0], "and the value we were trying to restore"
    assert m.last_restore_warning is not None
    assert "nvmlDeviceGetPowerManagementLimit" in m.last_restore_warning


def test_release_when_never_engaged_is_still_a_clean_true(tmp_path, fake, marker):
    m = make_mitigator(tmp_path, fake)
    assert m.connect(SUB_ID) is True

    assert m.release() is True
    assert m.last_restore_warning is None
    assert not marker.exists()


# =========================================================================
# startup_recovery, the crash path
# =========================================================================
def test_startup_recovery_restores_and_clears_the_marker(tmp_path, fake, marker):
    """A fresh instance picks up the marker a crashed run left behind."""
    engage_one(tmp_path, fake)  # dies here, still mitigated
    assert marker.exists()
    assert fake.limit_mw == MIN_MW

    fresh = make_mitigator(tmp_path, fake)
    msg = fresh.startup_recovery()

    assert msg is not None and "restored" in msg, msg
    assert fake.limit_mw == USER_LIMIT_MW
    assert fresh.last_restore_warning is None
    assert not marker.exists()


def test_startup_recovery_reports_both_numbers_and_keeps_the_marker(tmp_path, fake, marker):
    """The stuck card again, this time on the recovery path the user actually reads."""
    engage_one(tmp_path, fake)
    fake.stuck_at = MIN_MW

    fresh = make_mitigator(tmp_path, fake)
    msg = fresh.startup_recovery()

    assert msg is not None
    assert "400000" in msg and "600000" in msg, \
        f"the message the guard logs and toasts carries both numbers: {msg}"
    assert marker.exists(), "kept, so the next start tries again"
    assert fresh.last_restore_warning is not None
    assert "400000" in fresh.last_restore_warning


def test_startup_recovery_retries_a_dropped_set(tmp_path, fake, marker):
    engage_one(tmp_path, fake)
    fake.ignore_sets = 1
    sets_before = len(fake.set_calls)

    fresh = make_mitigator(tmp_path, fake)
    msg = fresh.startup_recovery()

    assert "restored" in msg, msg
    assert fake.limit_mw == USER_LIMIT_MW
    assert fake.set_calls[sets_before:] == [USER_LIMIT_MW, USER_LIMIT_MW]
    assert not marker.exists()
    assert fresh.last_restore_warning is None


def test_startup_recovery_without_a_marker_does_nothing(tmp_path, fake, marker):
    fresh = make_mitigator(tmp_path, fake)

    assert fresh.startup_recovery() is None
    assert fake.initialized is False, "no marker means NVML is never even loaded"

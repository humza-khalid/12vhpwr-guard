"""Shared stand-ins for the guard's test suites.

Nothing here can shut the machine down or touch the GPU. The mitigator is a
recording fake, the sensor backend is a fake, and every test that drives the real
monitor_loop stubs g.shutdown_windows before the loop starts.

conftest.py puts the repo root on sys.path, so importing this module from a test
is enough to get the guard module with it.
"""

import contextlib
import threading
import time

import hwinfo_12vhpwr_guard as g

# How long to wait for the monitor thread to notice its stop event. The loop
# checks it every poll interval (0.5s at worst, in the paused branch), so this is
# a hang detector rather than a schedule.
JOIN_TIMEOUT = 10.0

# The test thread polls its exit condition much faster than the loop ticks, so a
# state that only exists for one grace window (engaged but not yet escalated) is
# not missed on a busy CI runner.
POLL_SEC = 0.005


class CountingLogger:
    """Logger stand-in that keeps every record so a test can assert on it."""

    def __init__(self):
        self.records = {"info": [], "warning": [], "error": [], "critical": [], "debug": []}

    def _add(self, level, msg):
        self.records[level].append(str(msg))

    def info(self, m): self._add("info", m)
    def warning(self, m): self._add("warning", m)
    def error(self, m): self._add("error", m)
    def critical(self, m): self._add("critical", m)
    def debug(self, m): self._add("debug", m)

    def all_text(self):
        return "\n".join(sum(self.records.values(), []))


class FakeMitigator:
    """Stands in for GpuMitigator. Records every call the guard makes.

    available=None (the default) defers to the class attribute AVAILABLE, which is
    how a test flips every mitigator the loop builds to unavailable mid-run without
    reaching into instances it never sees.
    """

    AVAILABLE = True

    def __init__(self, available=None, engage_ok=True, reason=None, logger=None):
        self._available = available
        self._engage_ok = engage_ok
        self._reason = reason
        self.calls = []
        self.engaged = False
        self.recovery_ran = False

    @property
    def is_available(self):
        return FakeMitigator.AVAILABLE if self._available is None else self._available

    @property
    def unavailable_reason(self):
        if self.is_available:
            return None
        return self._reason or "not elevated"

    def connect(self, subsystem_id=None):
        self.calls.append(f"connect({subsystem_id})")
        return self.is_available

    def engage(self, reason=""):
        self.calls.append(f"engage({reason})")
        if not self._engage_ok or not self.is_available:
            return False
        self.engaged = True
        return True

    def release(self):
        self.calls.append("release")
        self.engaged = False
        return True

    def startup_recovery(self):
        self.recovery_ran = True
        return None

    def close(self):
        self.calls.append("close")

    def count(self, prefix):
        return sum(1 for c in self.calls if c.startswith(prefix))


class FakeBackend:
    """Sensor stand-in. The test writes amps, the guard reads pins."""

    key = "fake"
    tag = "fake"
    amps = 0.5
    # Set to six values to drive every pin at once. The advisory tests need a whole
    # pattern rather than one hot pin; None keeps the amps behaviour above.
    pins = None
    subsystem_id = 0x89E31043
    # The F3 tests take the sensor away and keep it away. gone makes every read raise
    # the way a dead driver session does, connect_ok False makes every reconnect fail,
    # and key is set to "astral" or "hwinfo" to say which backend is being stood in
    # for, because the self-restart only applies to the direct one.
    gone = False
    connect_ok = True

    def connect(self):
        if not FakeBackend.connect_ok:
            raise RuntimeError("fake backend cannot connect")
        return "fake sensor"

    @property
    def degraded(self):
        return None

    def read_pins(self):
        if FakeBackend.gone:
            raise g.SensorGoneError("fake sensor gone")
        if FakeBackend.pins is not None:
            return [(f"GPU 12VHPWR Pin{i} Current", float(a), "A")
                    for i, a in enumerate(FakeBackend.pins, start=1)]
        return [(f"GPU 12VHPWR Pin{i} Current",
                 FakeBackend.amps if i == 3 else 0.5, "A") for i in range(1, 7)]

    def close(self):
        pass


def wait_until(cond, timeout, interval=POLL_SEC):
    """Poll cond() until it is true or timeout expires. Returns the final result.

    Deadlines passed in here are safety nets, not schedules: a fast machine leaves
    as soon as cond() is true, so they are set well above what the scenario needs.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return bool(cond())


@contextlib.contextmanager
def running_loop():
    """The real monitor_loop in a thread for the body of the with block."""
    stop = threading.Event()
    logger = CountingLogger()
    t = threading.Thread(target=g.monitor_loop, args=(stop, logger), daemon=True)
    t.start()
    try:
        yield logger
    finally:
        stop.set()
        t.join(timeout=JOIN_TIMEOUT)


def run_loop(seconds_max=8.0, until=None):
    """Run the real monitor_loop in a thread until until() or a timeout.

    until() is what normally ends the run, so seconds_max only bites when the
    scenario never happens. It is deliberately generous for slow CI runners.
    """
    with running_loop() as logger:
        deadline = time.time() + seconds_max
        while time.time() < deadline:
            if until is not None and until():
                break
            time.sleep(POLL_SEC)
    return logger

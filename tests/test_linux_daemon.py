"""Tests for linux/guardd.py, the Linux daemon (B-012, S4a).

They drive the real Daemon loop with a fake sensor backend, a fake mitigator and recorders for
everything the daemon does to the outside world: the power-off commands, desktop notifications,
wall messages, sd_notify and the single-instance lock. Nothing here touches I2C, NVML or
systemd, and nothing can power anything off: the shutdown sequence runs through a recording
runner and a sleep that returns at once.

Self-contained on purpose: no hwinfo_12vhpwr_guard and no tests/helpers.py, because this file
has to run on the Linux CI job too, where the Windows module cannot even be imported. Timings
are compressed (10 ms polls, sub-second tier windows) so the whole file runs in seconds.
"""

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LINUX_DIR = REPO_ROOT / "linux"
for _p in (str(REPO_ROOT), str(LINUX_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import guard_core  # noqa: E402
import guardd  # noqa: E402
from astral_i2c import PIN_LABEL_FMT, SensorGoneError, TransientReadError  # noqa: E402

ASTRAL_5090_OC = 0x89E31043
HEALTHY = [1.0] * 6
OVER = [5.0, 5.0, 5.0, 5.0, 5.0, 10.0]          # tier 1 only (9.5 A)
CATASTROPHIC = [5.0, 5.0, 5.0, 5.0, 5.0, 17.0]  # tier 3 (16 A)

# The Windows loop tests' compressed tiers (tests/conftest.py loop_env), plus a short
# advisory sustain.
COMPRESSED = dict(
    threshold_amps=9.5, sustained_seconds_required=0.3,
    critical_amps=13.0, critical_seconds=0.15, catastrophic_amps=16.0,
    mitigation_grace_seconds=0.3, mitigation_clear_seconds=0.2,
    mitigation_min_hold_seconds=0.4, retrigger_window_seconds=5.0,
    advisory_sustain_seconds=0.05,
)

STATE_KEYS = {"version", "pid", "updated", "status", "message", "max_label", "max_val",
              "max_unit", "source", "mode", "mitigation", "advisory", "sensor_path_fault"}

POWEROFF = ["systemctl", "poweroff"]
POWEROFF_FORCE = ["systemctl", "poweroff", "--force"]


# ------------------------------------------------------------------- fakes
class Rig:
    """What the fake card is doing. Changed by the test while the loop runs."""

    def __init__(self):
        self.amps = list(HEALTHY)
        self.key = "astral"
        self.connect_ok = True
        self.connect_error = "no supported ASUS ROG Astral GPU found (no NVIDIA display adapter)"
        self.gone = False
        self.transient = 0
        self.connects = 0
        self.reads = 0
        self.on_read = None


class FakeBackend:
    tag = "direct"
    degraded = None

    def __init__(self, rig: Rig):
        self.rig = rig
        self.key = rig.key
        self.closed = False

    @property
    def subsystem_id(self):
        return ASTRAL_5090_OC

    def connect(self) -> str:
        self.rig.connects += 1
        if not self.rig.connect_ok:
            raise RuntimeError(self.rig.connect_error)
        return "Fake RTX 5090 [ROG Astral RTX 5090 OC] on /dev/i2c-4"

    def read_pins(self):
        rig = self.rig
        rig.reads += 1
        if rig.on_read is not None:
            rig.on_read(rig)
        if rig.gone:
            raise SensorGoneError("3 consecutive failed I2C reads (last: EIO)")
        if rig.transient > 0:
            rig.transient -= 1
            raise TransientReadError("implausible sample: all zero")
        return [(PIN_LABEL_FMT.format(i + 1), float(a), "A") for i, a in enumerate(rig.amps)]

    def close(self) -> None:
        self.closed = True


class FakeMitigator:
    """GpuMitigator's shape: what TieredResponse, the daemon and startup recovery use."""

    def __init__(self, env, marker_path):
        self.env = env
        self.marker_path = marker_path
        self.is_available = False
        self.unavailable_reason = "not connected"
        self.last_restore_warning = None
        self.engaged = False
        self.connected_with = "never"
        self.engage_reasons = []
        self.release_calls = 0
        self.closed = False
        self.recovered = False
        self.power_mw = None

    def connect(self, subsystem_id=None):
        self.connected_with = subsystem_id
        if self.env.on_mitigator_connect is not None:
            self.env.on_mitigator_connect()
        if self.env.mitigation_available:
            self.is_available = True
            self.unavailable_reason = None
        else:
            self.unavailable_reason = "not root"
        return self.is_available

    def engage(self, reason=""):
        if not self.is_available:
            return False
        if not self.engaged:
            self.engaged = True
            self.engage_reasons.append(reason)
            Path(self.marker_path).write_text("{}", encoding="utf-8")
            if self.env.on_engage is not None:
                self.env.on_engage()
        return True

    def release(self):
        if self.engaged:
            self.release_calls += 1
        self.engaged = False
        try:
            os.remove(self.marker_path)
        except OSError:
            pass
        return True

    def close(self):
        # Like the real one: release, then drop NVML.
        self.closed = True
        self.release()
        self.is_available = False
        self.unavailable_reason = "not connected"

    def power_usage_mw(self):
        return self.power_mw

    def startup_recovery(self):
        self.recovered = True
        try:
            os.remove(self.marker_path)
        except OSError:
            pass
        return ("Previous run ended while the GPU was limited (tier 1: test) - clocks and "
                "power limit restored")


class ListHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__(logging.DEBUG)
        self.sink = sink

    def emit(self, record):
        self.sink.append((record.levelname, record.getMessage()))


class RecordingDaemon(guardd.Daemon):
    """The real daemon, with every state.json payload it writes kept in order."""

    def __init__(self, *args, history=None, **kwargs):
        self.history = history if history is not None else []
        super().__init__(*args, **kwargs)

    def _write_state(self, payload):
        self.history.append(dict(payload))
        return super()._write_state(payload)


class Env:
    """One daemon under test and everything it did."""

    _count = 0

    def __init__(self, tmp_path, **settings):
        Env._count += 1
        self.tmp = tmp_path
        self.run_dir = tmp_path / "run"
        self.var_dir = tmp_path / "var"
        self.log_dir = tmp_path / "log"
        self.rig = Rig()
        self.settings = dict(COMPRESSED)
        self.settings.update(settings)
        self.made = []
        self.mitigation_available = True
        self.on_engage = None
        self.on_mitigator_connect = None
        self.events = []        # ("sleep", seconds) and ("run", argv), in order
        self.runner_hook = None
        self.desktop = []       # (title, body, critical, sessions returned)
        self.desktop_delay = 0.0
        self.sessions = 1
        self.walls = []
        self.sd = []
        self.records = []
        self.clock_offset = 0.0
        self.lock_held = False
        self.lock_paths = []
        self.lock_releases = 0
        self.history = []
        self.code = None
        self.thread = None
        self.daemon = None
        self.logger = logging.getLogger(f"test_linux_daemon.{Env._count}")
        self.logger.handlers[:] = [ListHandler(self.records)]
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False

    # -- seams ----------------------------------------------------------------
    def _make_mitigator(self, marker_path, logger):
        m = FakeMitigator(self, marker_path)
        self.made.append(m)
        return m

    def _sleep(self, seconds):
        self.events.append(("sleep", seconds))

    def _run(self, argv):
        self.events.append(("run", list(argv)))
        if self.runner_hook is not None:
            self.runner_hook(list(argv))

    def _desktop(self, title, body, critical=False):
        if self.desktop_delay:
            time.sleep(self.desktop_delay)
        sessions = self.sessions
        self.desktop.append((title, body, critical, sessions))
        return sessions

    def _wall(self, message):
        self.walls.append(message)
        return True

    def _sd(self, message):
        self.sd.append(message)
        return True

    def _lock(self, path):
        self.lock_paths.append(path)
        if self.lock_held:
            return None

        def release():
            self.lock_releases += 1

        return release

    # -- driving --------------------------------------------------------------
    def build(self, **overrides):
        kwargs = dict(
            settings=self.settings, settings_path=None,
            state_dir=str(self.run_dir), var_dir=str(self.var_dir), log_dir=str(self.log_dir),
            logger=self.logger,
            backend_factory=lambda: FakeBackend(self.rig),
            mitigator_factory=self._make_mitigator,
            clock=lambda: time.time() + self.clock_offset,
            sleep=self._sleep, runner=self._run,
            desktop_notify=self._desktop, wall=self._wall,
            sd_notify_fn=self._sd, env={}, locker=self._lock,
            poll_interval=0.01, reconnect_retry=0.02, pins_missing_retry=0.02,
        )
        kwargs.update(overrides)
        self.daemon = RecordingDaemon(history=self.history, **kwargs)
        return self.daemon

    def start(self, **overrides):
        self.build(**overrides)

        def body():
            self.code = self.daemon.run()

        self.thread = threading.Thread(target=body, daemon=True)
        self.thread.start()
        return self.daemon

    def join(self, timeout=5.0):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "daemon did not finish"
        return self.code

    def stop(self, signum=signal.SIGTERM, timeout=5.0):
        self.daemon.request_stop(signum)
        return self.join(timeout)

    def wait_for(self, condition, timeout=5.0, what="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.005)
        raise AssertionError(f"timed out waiting for {what}; log tail: {self.records[-8:]}")

    # -- reading back ---------------------------------------------------------
    def logs(self, level=None):
        return [m for lvl, m in list(self.records) if level is None or lvl == level]

    def has_log(self, fragment, level=None):
        return any(fragment in m for m in self.logs(level))

    def state(self):
        """The state file as a reader sees it, {} before the first write. Retried because on
        Windows (where these tests also run) os.replace fails while a reader has the file
        open, and the daemon simply writes it again on the next tick."""
        for _attempt in range(50):
            try:
                with open(self.run_dir / "state.json", encoding="utf-8") as f:
                    return json.load(f)
            except FileNotFoundError:
                return {}
            except (PermissionError, ValueError):
                time.sleep(0.005)
        raise AssertionError("state.json unreadable")

    def statuses(self):
        return [p["status"] for p in list(self.history)]

    def titles(self):
        return [d[0] for d in list(self.desktop)]

    def flights(self, event):
        return sorted(self.log_dir.glob(f"flight_*_{event}.csv"))


def phases(events):
    """Collapse the 1 s sleep steps between commands into totals."""
    out, acc = [], 0.0
    for kind, value in events:
        if kind == "sleep":
            acc += value
        else:
            out.append(("sleep", round(acc, 3)))
            out.append(("run", value))
            acc = 0.0
    if acc:
        out.append(("sleep", round(acc, 3)))
    return out


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    if e.thread is not None and e.thread.is_alive():
        e.daemon.request_stop(signal.SIGTERM)
        e.thread.join(5)


# ======================================================================== startup
@pytest.mark.parametrize("mode", [guard_core.RESPONSE_TIERED, guard_core.RESPONSE_SHUTDOWN_ONLY])
def test_startup_lines_ready_and_clean_stop(env, mode):
    env.settings["response_mode"] = mode
    env.start()
    env.wait_for(lambda: "OK" in env.statuses(), what="status OK")
    assert env.stop() == 0

    logs = env.logs()
    assert logs[0] == "Starting 12VHPWR Guard v1.5.0 (daemon)..."
    assert any(m.startswith("Threshold=9.50A | Poll=0.01s | Sustained=0.3s") for m in logs)
    assert any(m.startswith(f"Response mode={mode} | Tier 2 critical=13.00A/0s | "
                            f"Tier 3 catastrophic=16.00A") for m in logs)
    assert any(m.startswith("Advisories (warn only): load gate=18.0A") for m in logs)
    expected_path = ("Sensor-path check: pins and board power" if mode == "tiered"
                     else "Sensor-path check: pins only (no NVML in shutdown-only mode)")
    assert expected_path in logs
    assert "Self-restart after direct sensor loss: 300 s" in logs
    assert "Sensor backend: setting=auto, trying [astral]" in logs
    assert "Connected: Fake RTX 5090 [ROG Astral RTX 5090 OC] on /dev/i2c-4 [direct]" in logs
    assert logs[-1] == "12VHPWR Guard stopped."

    assert env.sd[0] == "READY=1" and env.sd.count("READY=1") == 1
    assert env.sd[-1] == "STOPPING=1"
    assert ("12VHPWR Guard", "Monitoring started.", False, 1) in env.desktop
    assert ("12VHPWR Guard", "Monitoring active via direct.", False, 1) in env.desktop
    assert env.state()["status"] == "STOPPED"
    assert env.lock_paths == [str(env.run_dir / "guardd.lock")] and env.lock_releases == 1
    for folder in (env.run_dir, env.var_dir, env.log_dir):
        assert folder.is_dir()


def test_advisories_off_and_self_restart_off_lines(env):
    env.settings.update(advisories_enabled=False, sensor_loss_restart_seconds=0.0)
    env.start()
    env.wait_for(lambda: env.has_log("Connected:"), what="connect")
    env.stop()
    assert "Advisories: off" in env.logs()
    assert "Self-restart after direct sensor loss: off" in env.logs()


def test_already_running_exits_0_without_touching_anything(env):
    env.lock_held = True
    env.build()
    assert env.daemon.run() == 0
    assert env.has_log("Already running")
    assert env.sd == [] and env.made == [] and env.rig.connects == 0
    assert not (env.run_dir / "state.json").exists()


def test_already_running_skips_startup_recovery(env):
    """A second daemon must never undo the first one's live GPU limit."""
    env.run_dir.mkdir(parents=True)
    (env.run_dir / "mitigation.active").write_text("{}", encoding="utf-8")
    env.lock_held = True
    env.build()
    assert env.daemon.run() == 0
    assert env.made == []
    assert (env.run_dir / "mitigation.active").exists()


def test_lock_error_is_fatal(env):
    def broken(path):
        raise PermissionError("read-only file system")

    env.build(locker=broken)
    assert env.daemon.run() == 1
    assert env.has_log("Cannot take the single-instance lock", "CRITICAL")


@pytest.mark.skipif(not hasattr(os, "fork") or sys.platform == "win32",
                    reason="flock needs a POSIX system")
def test_real_flock_refuses_a_second_holder(tmp_path):
    path = str(tmp_path / "guardd.lock")
    first = guardd.flock_exclusive(path)
    assert first is not None
    try:
        assert guardd.flock_exclusive(path) is None
    finally:
        first()
    again = guardd.flock_exclusive(path)
    assert again is not None
    again()


# ======================================================================== tiers
def test_tier1_engage_then_release_through_the_loop(env):
    # A real limit makes the current fall, so the fake does the same.
    env.on_engage = lambda: setattr(env.rig, "amps", list(HEALTHY))
    env.rig.amps = list(OVER)
    env.start()
    env.wait_for(lambda: env.made and env.made[0].release_calls == 1, what="release")
    assert env.stop() == 0

    m = env.made[0]
    assert m.connected_with == ASTRAL_5090_OC
    assert len(m.engage_reasons) == 1 and m.engage_reasons[0].startswith("tier 1: sustained")
    assert env.has_log("Threshold exceeded: GPU 12VHPWR Pin6 Current=10.00A >= 9.50A", "WARNING")
    assert env.has_log("GPU limit brought the current down to 1.00A")
    assert env.has_log("GPU limit released (current stayed below 9.30A for 0s). Another "
                       "overcurrent within 5s will be treated as a persistent fault")
    statuses = env.statuses()
    assert "MITIGATED" in statuses
    assert statuses.index("OK", statuses.index("MITIGATED")) > statuses.index("MITIGATED")
    limited = [d for d in env.desktop if d[0] == "12VHPWR Guard - GPU limited"]
    assert len(limited) == 1 and limited[0][2] is True
    warn = [d for d in env.desktop if d[0] == "12VHPWR Guard - Warning"]
    assert warn and warn[0][2] is True
    restored = [d for d in env.desktop if d[1] == "GPU restored to normal clocks and power limit."]
    assert restored and restored[0][2] is False
    assert len(env.flights("engaged")) == 1
    assert [e for e in env.events if e[0] == "run"] == []
    assert m.closed and m.release_calls == 1


def test_shutdown_only_never_builds_a_mitigator_and_powers_off(env):
    env.settings["response_mode"] = guard_core.RESPONSE_SHUTDOWN_ONLY
    env.rig.amps = list(OVER)

    def runner_hook(argv):
        if argv == POWEROFF:
            # What systemd does as soon as the orderly poweroff starts stopping units.
            env.daemon.request_stop(signal.SIGTERM)

    env.runner_hook = runner_hook
    env.start()
    assert env.join() == 0

    assert env.made == []
    assert phases(env.events) == [("sleep", 5.0), ("run", POWEROFF),
                                  ("sleep", 20.0), ("run", POWEROFF_FORCE)]

    note = json.loads((env.var_dir / "last_shutdown.json").read_text(encoding="utf-8"))
    assert set(note) == {"time", "time_human", "reason", "version"}
    assert note["version"] == "1.5.0"
    reason = note["reason"]
    assert reason.startswith("Tier 1: sustained")
    assert reason.endswith("No mitigation attempted, response mode is shutdown-only.")
    assert env.has_log(f"SHUTDOWN TRIGGERED: {reason}", "CRITICAL")
    assert env.has_log("SIGTERM received during an emergency shutdown; finishing the shutdown "
                       "sequence first.", "WARNING")
    assert ("12VHPWR Guard - Shutdown", reason, True, 1) in env.desktop
    assert env.walls == [f"12VHPWR Guard: emergency shutdown in 5 seconds. {reason}"]
    assert env.state()["status"] == "SHUTDOWN"
    assert "STOPPED" not in env.statuses()
    assert len(env.flights("shutdown")) == 1
    assert not env.has_log("Leaving the GPU limited")


def test_tier3_limits_and_shuts_down_leaving_the_gpu_limited(env):
    env.rig.amps = list(CATASTROPHIC)
    env.runner_hook = lambda argv: (env.daemon.request_stop(signal.SIGTERM)
                                    if argv == POWEROFF_FORCE else None)
    env.start()
    assert env.join() == 0

    m = env.made[0]
    assert m.engaged and m.release_calls == 0 and not m.closed
    assert (env.run_dir / "mitigation.active").exists()
    reason = json.loads((env.var_dir / "last_shutdown.json").read_text("utf-8"))["reason"]
    assert reason.startswith("Tier 3 (catastrophic): GPU 12VHPWR Pin6 Current=17.00A >= 16.00A")
    assert reason.endswith("GPU limited and shutting down immediately.")
    assert env.has_log("Leaving the GPU limited through shutdown; the marker file will "
                       "trigger a restore on the next start.")
    assert [e[1] for e in env.events if e[0] == "run"] == [POWEROFF, POWEROFF_FORCE]
    assert env.flights("engaged") == [] and len(env.flights("shutdown")) == 1
    assert env.state()["status"] == "SHUTDOWN"
    assert env.state()["mitigation"] == "engaged"


def test_a_limited_gpu_lets_the_orderly_poweroff_finish_without_forcing(env):
    """systemd's SIGTERM during the orderly poweroff, with the card at its floor: the daemon
    steps aside instead of holding its stop for 20 s and forcing every shutdown."""
    env.rig.amps = list(CATASTROPHIC)

    def runner_hook(argv):
        if argv == POWEROFF:
            env.daemon.request_stop(signal.SIGTERM)

    env.runner_hook = runner_hook
    env.start()
    assert env.join() == 0

    m = env.made[0]
    assert m.engaged and m.release_calls == 0 and not m.closed, "the limit stays through shutdown"
    assert (env.run_dir / "mitigation.active").exists()
    assert [e[1] for e in env.events if e[0] == "run"] == [POWEROFF], "no forced poweroff"
    assert env.has_log("Orderly poweroff under way and the GPU is limited; leaving the rest of "
                       "the shutdown to the system.")
    assert env.state()["status"] == "SHUTDOWN"


def test_grace_window_failure_shuts_down(env):
    """Limiting did not bring the current down: the Windows wording, then the power-off."""
    env.rig.amps = list(OVER)
    env.runner_hook = lambda argv: (env.daemon.request_stop()
                                    if argv == POWEROFF_FORCE else None)
    env.start()
    assert env.join() == 0
    reason = json.loads((env.var_dir / "last_shutdown.json").read_text("utf-8"))["reason"]
    assert reason.endswith("Limiting the card did not fix it.")
    assert env.made[0].engaged


def test_mitigation_unavailable_runs_shutdown_only(env):
    env.mitigation_available = False
    env.rig.amps = list(OVER)
    env.runner_hook = lambda argv: (env.daemon.request_stop()
                                    if argv == POWEROFF_FORCE else None)
    env.start()
    assert env.join() == 0
    reason = json.loads((env.var_dir / "last_shutdown.json").read_text("utf-8"))["reason"]
    assert reason.endswith("No mitigation attempted, GPU limiting unavailable (not root).")
    assert ("12VHPWR Guard", "GPU limiting unavailable (not root). Shutdown-only.", True, 1) \
        in env.desktop
    assert any(p["mode"] == "shutdown_only" and p["mitigation"] == "unavailable: not root"
               for p in env.history)


# ======================================================================== advisories
def test_dead_pin_advisory_notifies_and_dumps(env):
    env.rig.amps = [0.2, 5.0, 5.0, 5.0, 5.0, 5.0]
    env.start()
    env.wait_for(lambda: "12VHPWR Guard - advisory" in env.titles(), what="advisory")
    env.wait_for(lambda: env.state().get("advisory") == "pin 1 near zero", what="state advisory")
    assert env.stop() == 0

    advisory = [d for d in env.desktop if d[0] == "12VHPWR Guard - advisory"][0]
    assert advisory[1].startswith("Pin 1 reads 0.20 A while the others average 5.00 A")
    assert advisory[2] is False
    assert env.has_log("Pin advisory: pin 1 reads 0.20 A while the other 5 average 5.00 A",
                       "WARNING")
    assert len(env.flights("advisory")) == 1
    # Warn only: the advisory never touches the GPU or the machine.
    assert all(not m.engage_reasons for m in env.made)
    assert [e for e in env.events if e[0] == "run"] == []
    assert "ERROR" not in env.statuses()


def test_sensor_path_fault_holds_error_and_clears(env, monkeypatch):
    monkeypatch.setattr(guard_core, "NO_CURRENT_SUSTAIN_SEC", 0.05)
    monkeypatch.setattr(guard_core, "ADVISORY_CLEAR_SEC", 0.05)
    env.rig.amps = [0.05] * 6
    env.start()
    env.wait_for(lambda: env.state().get("sensor_path_fault") is True, what="fault")
    state = env.state()
    assert state["status"] == "ERROR"
    assert state["message"] == "Not protected: the pin sensors see no current"
    assert state["advisory"] == "no current on any pin, not protected"
    env.wait_for(lambda: "12VHPWR Guard - not protected" in env.titles(), what="notification")
    note = [d for d in env.desktop if d[0] == "12VHPWR Guard - not protected"][0]
    assert note[1].startswith("The pin sensors see no current.") and note[2] is True

    env.rig.amps = list(HEALTHY)
    env.wait_for(lambda: env.state().get("status") == "OK", what="OK again")
    assert env.state().get("sensor_path_fault") is False
    assert env.state()["advisory"] is None
    assert env.has_log("Sensor path cleared: the pins carry current again.")
    assert env.stop() == 0


# ======================================================================== sensor errors
def test_transient_reads_are_skipped(env):
    env.rig.transient = 20
    env.start()
    env.wait_for(lambda: env.rig.reads > 25 and "OK" in env.statuses(), what="reads")
    assert env.stop() == 0
    assert len([m for m in env.logs("DEBUG") if m.startswith("Transient read error:")]) == 20
    assert env.rig.connects == 1
    assert not env.has_log("Sensor source lost")
    assert "ERROR" not in env.statuses()


def test_pins_missing_waits_and_recovers(env):
    env.rig.amps = []
    env.start()
    env.wait_for(lambda: env.has_log("Pin sensors not found"), what="missing")
    env.wait_for(lambda: any(p["message"] == "Sensors not found" for p in env.history),
                 what="state")
    env.rig.amps = list(HEALTHY)
    env.wait_for(lambda: env.has_log("Pin sensors found again. Monitoring resumed."),
                 what="found again")
    assert env.stop() == 0
    assert len([m for m in env.logs() if m.startswith("Pin sensors not found")]) == 1


def test_sensor_lost_keeps_the_limit_then_reconnect_releases(env):
    def lose_sensor():
        env.rig.amps = list(HEALTHY)
        env.rig.gone = True
        env.rig.connect_ok = False

    env.on_engage = lose_sensor
    env.rig.amps = list(OVER)
    env.start()
    env.wait_for(lambda: env.has_log("GPU stays limited while the sensor is gone"), what="lost")
    env.wait_for(lambda: "12VHPWR Guard - not protected" in env.titles(), what="not protected")
    m = env.made[0]
    assert m.engaged and m.release_calls == 0
    assert env.has_log("Sensor source lost: 3 consecutive failed I2C reads", "WARNING")
    assert env.has_log("Sustained over-threshold timer reset by sensor disconnect.")
    assert ("12VHPWR Guard", "Sensor source lost. Waiting to reconnect...", True, 1) \
        in env.desktop
    lost = [p for p in env.history if p["message"] == "Sensor source lost"]
    assert lost and lost[0]["status"] == "ERROR" and lost[0]["source"] == ""
    assert lost[0]["mitigation"] == "engaged"
    env.wait_for(lambda: env.state().get("status") == "WAITING", what="reconnecting")
    assert env.state().get("message") == "Sensor source lost. Waiting to reconnect..."

    # Back, and clean: the limit comes off the normal way.
    env.rig.gone = False
    env.rig.connect_ok = True
    env.wait_for(lambda: m.release_calls == 1, what="release after reconnect")
    assert env.stop() == 0
    assert env.rig.connects >= 2
    assert len([m_ for m_ in env.logs() if m_.startswith("Connected:")]) == 2
    assert env.desktop.count(("12VHPWR Guard", "Monitoring active via direct.", False, 1)) == 2
    assert env.daemon._tracker.over_start is None


def test_reconnect_resets_the_tier1_timer(env):
    """Over threshold, lost, back: the sustained window starts again from scratch."""
    env.settings["sustained_seconds_required"] = 0.5
    env.rig.amps = list(OVER)
    def on_read(rig):
        if rig.reads == 20:
            rig.gone = True             # this read fails: the sensor is lost
        elif rig.reads == 21:
            rig.gone = False            # the first read after the reconnect is clean
            rig.amps = list(HEALTHY)

    env.rig.on_read = on_read
    env.start()
    env.wait_for(lambda: env.rig.connects == 2 and env.state().get("status") == "OK",
                 what="reconnect")
    assert env.has_log("Sustained over-threshold timer reset by sensor disconnect.")
    assert env.daemon._tracker.over_start is None
    assert env.stop() == 0
    assert all(not m.engage_reasons for m in env.made)


def test_resume_gap_releases_without_arming_retrigger(env):
    def suspend_right_after_engage():
        env.rig.amps = list(HEALTHY)
        env.clock_offset += 10.0

    env.on_engage = suspend_right_after_engage
    env.rig.amps = list(OVER)
    env.start()
    env.wait_for(lambda: env.made and env.made[0].release_calls == 1, what="release")
    assert env.stop() == 0
    assert env.has_log("detected (suspend/resume?) - sustained timer reset.", "WARNING")
    released = [m for m in env.logs() if m.startswith("GPU limit released (suspend/resume gap)")]
    assert released == ["GPU limit released (suspend/resume gap)."]
    assert env.daemon._response.released_at is None
    assert [e for e in env.events if e[0] == "run"] == []


def test_slow_nvml_bind_at_boot_is_not_a_resume_gap(env):
    # Mint S5: the driver's first init took 5 s at boot and the next tick logged a
    # suspend/resume gap. The bind now happens before the first sample.
    reads_at_bind = []

    def slow_first_init():
        reads_at_bind.append(env.rig.reads)
        env.clock_offset += 6.0

    env.on_mitigator_connect = slow_first_init
    env.start()
    env.wait_for(lambda: env.rig.reads >= 5, what="a few samples")
    assert env.stop() == 0
    assert reads_at_bind == [0]
    assert not env.has_log("detected (suspend/resume?)")


# ======================================================================== F3 self-restart
def test_self_restart_exits_75_after_the_direct_sensor_was_lost(env):
    env.settings["sensor_loss_restart_seconds"] = 0.2

    def on_read(rig):
        if rig.reads == 3:
            rig.gone = True
            rig.connect_ok = False

    env.rig.on_read = on_read
    env.start()
    assert env.join() == 75
    msg = ("Restarting to recover the sensor path: no pin-current source for 0.2 s after "
           "the direct sensor was lost")
    assert env.has_log(msg, "ERROR")
    assert ("12VHPWR Guard - restarting", "No sensor for 0.2 seconds, restarting to recover it.",
            True, 1) in env.desktop
    state = env.state()
    assert state["status"] == "ERROR"
    assert state["message"] == "Restarting to recover the sensor path"


@pytest.mark.parametrize("case", ["never_connected", "not_direct"])
def test_self_restart_needs_the_direct_sensor_to_have_been_seen(env, case):
    env.settings["sensor_loss_restart_seconds"] = 0.1
    if case == "never_connected":
        env.rig.connect_ok = False
    else:
        env.rig.key = "fake"

        def on_read(rig):
            if rig.reads == 3:
                rig.gone = True
                rig.connect_ok = False

        env.rig.on_read = on_read
    env.start()
    env.wait_for(lambda: env.rig.connects >= 5, what="several reconnect attempts")
    time.sleep(0.3)
    assert env.thread.is_alive()
    assert env.stop() == 0
    assert not env.has_log("Restarting to recover the sensor path")


def test_waiting_for_a_sensor_notifies_not_protected_once(env):
    env.rig.connect_ok = False
    env.start()
    env.wait_for(lambda: env.rig.connects >= 5, what="attempts")
    env.daemon.flush_notifications()
    assert env.stop() == 0
    not_protected = [d for d in env.desktop if d[0] == "12VHPWR Guard - not protected"]
    assert len(not_protected) == 1
    assert not_protected[0][1] == (
        "No pin sensor source, so the connector is not being watched. astral: no supported "
        "ASUS ROG Astral GPU found (no NVIDIA display adapter)")
    assert not_protected[0][2] is True
    warnings = [m for m in env.logs("WARNING") if m.startswith("No sensor source available")]
    assert warnings == ["No sensor source available: astral: no supported ASUS ROG Astral GPU "
                        "found (no NVIDIA display adapter)"]
    assert all(p["status"] == "WAITING" for p in env.history if p["status"] != "STOPPED")


def test_not_protected_is_repeated_until_a_session_sees_it(env):
    env.rig.connect_ok = False
    env.sessions = 0
    env.start(sticky_retry=0.02)
    env.wait_for(lambda: env.titles().count("12VHPWR Guard - not protected") >= 3,
                 what="retries")
    env.sessions = 1
    env.wait_for(lambda: any(d[3] == 1 for d in env.desktop if
                             d[0] == "12VHPWR Guard - not protected"), what="delivered")
    time.sleep(0.15)
    env.daemon.flush_notifications()
    reached = [d for d in env.desktop if d[0] == "12VHPWR Guard - not protected" and d[3] == 1]
    assert len(reached) == 1
    assert env.stop() == 0


# ======================================================================== per-start rate limit
def write_stamps(env, age):
    env.run_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (env.run_dir / "notify_stamps.json").write_text(json.dumps(
        {key: now - age for key in ("started", "connected", "not_protected")}), "utf-8")


def test_per_start_notifications_are_stamped_when_they_reach_a_desktop(env):
    env.start()
    env.wait_for(lambda: "OK" in env.statuses(), what="OK")
    env.daemon.flush_notifications()
    assert env.stop() == 0
    stamps = json.loads((env.run_dir / "notify_stamps.json").read_text("utf-8"))
    assert set(stamps) == {"started", "connected"}


@pytest.mark.parametrize("connects", [True, False])
def test_a_restart_loop_does_not_repeat_per_start_notifications(env, connects):
    write_stamps(env, age=30.0)
    env.rig.connect_ok = connects
    env.start()
    env.wait_for(lambda: env.rig.connects >= 3 or "OK" in env.statuses(), what="running")
    env.daemon.flush_notifications()
    assert env.stop() == 0
    titles = env.titles()
    bodies = [d[1] for d in env.desktop]
    assert "Monitoring started." not in bodies
    assert "Monitoring active via direct." not in bodies
    assert "12VHPWR Guard - not protected" not in titles
    assert env.has_log("Not repeating the 'started' notification")


def test_per_start_notifications_return_after_the_window(env):
    write_stamps(env, age=700.0)
    env.rig.connect_ok = False
    env.start()
    env.wait_for(lambda: "12VHPWR Guard - not protected" in env.titles(), what="not protected")
    assert env.stop() == 0
    assert ("12VHPWR Guard", "Monitoring started.", False, 1) in env.desktop


def test_overcurrent_and_shutdown_are_never_rate_limited(env):
    write_stamps(env, age=1.0)
    env.settings["response_mode"] = guard_core.RESPONSE_SHUTDOWN_ONLY
    env.rig.amps = list(OVER)
    env.runner_hook = lambda argv: (env.daemon.request_stop()
                                    if argv == POWEROFF_FORCE else None)
    env.start()
    assert env.join() == 0
    titles = env.titles()
    assert "12VHPWR Guard - Warning" in titles
    assert "12VHPWR Guard - Shutdown" in titles
    assert env.walls


# ======================================================================== startup notes
def test_startup_recovery_runs_when_the_marker_exists(env):
    env.run_dir.mkdir(parents=True)
    (env.run_dir / "mitigation.active").write_text('{"reason": "tier 1: test"}', "utf-8")
    env.start()
    env.wait_for(lambda: "OK" in env.statuses(), what="OK")
    assert env.stop() == 0
    recovery = env.made[0]
    assert recovery.recovered and recovery.closed
    msg = ("Previous run ended while the GPU was limited (tier 1: test) - clocks and power "
           "limit restored")
    assert env.has_log(msg, "WARNING")
    assert ("12VHPWR Guard", msg, False, 1) in env.desktop
    assert not (env.run_dir / "mitigation.active").exists()
    # The loop binds a mitigator of its own, separately from the recovery one.
    assert len(env.made) == 2 and not env.made[1].recovered


def test_no_marker_no_recovery(env):
    env.start()
    env.wait_for(lambda: "OK" in env.statuses(), what="OK")
    assert env.stop() == 0
    assert all(not m.recovered for m in env.made)


@pytest.mark.parametrize("corrupt", [False, True])
def test_leftover_note_is_announced_and_deleted(env, corrupt):
    env.var_dir.mkdir(parents=True)
    note = env.var_dir / "last_shutdown.json"
    if corrupt:
        note.write_text("{not json", encoding="utf-8")
        expected = ("The last shutdown was an emergency stop by 12VHPWR Guard (an earlier "
                    "session): sustained overcurrent on a 12VHPWR pin Check the connector "
                    "and cable seating before the next heavy load.")
    else:
        note.write_text(json.dumps({"time": 1.0, "time_human": "2026-10-28 10:00:00",
                                    "reason": "Tier 1: test.", "version": "1.5.0"}), "utf-8")
        expected = ("The last shutdown was an emergency stop by 12VHPWR Guard "
                    "(2026-10-28 10:00:00): Tier 1: test. Check the connector and cable "
                    "seating before the next heavy load.")
    (env.var_dir / "last_shutdown.json.tmp").write_text("x", encoding="utf-8")
    # Booting: nobody is logged in yet, so the first attempts reach no session.
    env.sessions = 0
    env.start(sticky_retry=0.02)
    env.wait_for(lambda: env.titles().count("12VHPWR Guard - read this") >= 2, what="retry")
    assert not note.exists() and not (env.var_dir / "last_shutdown.json.tmp").exists()
    env.sessions = 1
    env.wait_for(lambda: any(d[3] == 1 for d in env.desktop
                             if d[0] == "12VHPWR Guard - read this"), what="delivered")
    time.sleep(0.15)
    env.daemon.flush_notifications()
    assert env.stop() == 0
    assert env.has_log(expected, "WARNING")
    announced = [d for d in env.desktop if d[0] == "12VHPWR Guard - read this"]
    assert all(d[1] == expected and d[2] is True for d in announced)
    assert len([d for d in announced if d[3] == 1]) == 1


# ======================================================================== signals
def test_sigterm_releases_an_engaged_limit_and_writes_stopped(env):
    env.settings["mitigation_grace_seconds"] = 30.0
    env.rig.amps = list(OVER)
    env.start()
    env.wait_for(lambda: env.made and env.made[0].engaged, what="engaged")
    env.wait_for(lambda: env.state().get("status") == "MITIGATED", what="state MITIGATED")
    assert env.stop(signal.SIGTERM) == 0

    m = env.made[0]
    assert m.closed and not m.engaged and m.release_calls == 1
    assert not (env.run_dir / "mitigation.active").exists()
    assert env.has_log("SIGTERM received, stopping.")
    assert env.has_log("Stopping: releasing the GPU limit before exit.", "WARNING")
    state = env.state()
    assert state["status"] == "STOPPED"
    assert state["mitigation"] == "unavailable: not connected"
    assert env.sd[-1] == "STOPPING=1"
    assert env.lock_releases == 1
    assert [e for e in env.events if e[0] == "run"] == []


def test_sigint_stops_too(env):
    env.start()
    env.wait_for(lambda: "OK" in env.statuses(), what="OK")
    assert env.stop(signal.SIGINT) == 0
    assert env.has_log("SIGINT received, stopping.")


def test_stop_finishes_promptly_while_waiting_for_a_sensor(env):
    env.rig.connect_ok = False
    env.start(reconnect_retry=3.0)
    env.wait_for(lambda: env.rig.connects >= 1, what="first attempt")
    started = time.monotonic()
    assert env.stop() == 0
    assert time.monotonic() - started < 2.0


# ======================================================================== state file
def test_state_json_schema_and_atomic_writes(env, monkeypatch):
    replaces = []
    real_replace = os.replace

    def recording_replace(src, dst):
        replaces.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(guardd.os, "replace", recording_replace)
    env.start()
    env.wait_for(lambda: env.state().get("status") == "OK", what="OK")
    state = env.state()
    assert set(state) == STATE_KEYS
    assert state["version"] == "1.5.0"
    assert state["pid"] == os.getpid()
    assert isinstance(state["updated"], float)
    assert state["message"] == "OK (max 1.00A)"
    assert state["max_label"] == "GPU 12VHPWR Pin1 Current" and state["max_val"] == 1.0
    assert state["max_unit"] == "A"
    assert state["source"] == "Fake RTX 5090 [ROG Astral RTX 5090 OC] on /dev/i2c-4 [direct]"
    assert state["mode"] == "tiered" and state["mitigation"] == "ready"
    assert state["advisory"] is None and state["sensor_path_fault"] is False

    # Steady state: no more than one write a second, however fast the loop polls.
    writes_before = len(env.history)
    reads_before = env.rig.reads
    time.sleep(0.5)
    assert env.rig.reads - reads_before > 10
    assert len(env.history) - writes_before <= 1
    assert env.stop() == 0

    state_path = str(env.run_dir / "state.json")
    ours = [r for r in replaces if r[1] == state_path]
    assert ours and all(src == state_path + ".tmp" for src, _dst in ours)
    assert not (env.run_dir / "state.json.tmp").exists()
    assert env.state()["status"] == "STOPPED"


def test_state_before_the_first_tick_and_in_shutdown_only(env):
    env.settings["response_mode"] = guard_core.RESPONSE_SHUTDOWN_ONLY
    env.rig.connect_ok = False
    env.start(state_write_every=0.05)
    env.wait_for(lambda: (env.run_dir / "state.json").exists() and
                 env.state().get("message") == "Waiting for pin sensors...", what="waiting")
    state = env.state()
    assert state["status"] == "WAITING" and state["source"] == ""
    assert state["max_label"] is None and state["max_val"] is None
    assert state["mode"] == "shutdown_only"
    assert state["mitigation"] == "unavailable: response mode is shutdown-only"
    assert env.history[0]["message"] == "Starting..."
    assert env.stop() == 0


# ======================================================================== systemd
class FakeSocket:
    sent = []
    made = []

    def __init__(self, family, kind):
        FakeSocket.made.append((family, kind))
        self.closed = False

    def sendto(self, data, address):
        FakeSocket.sent.append((data, address))

    def close(self):
        self.closed = True


@pytest.fixture
def fake_socket(monkeypatch):
    FakeSocket.sent = []
    FakeSocket.made = []
    if not hasattr(socket, "AF_UNIX"):
        monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)
    return FakeSocket


def test_sd_notify_sends_one_datagram(fake_socket):
    env = {"NOTIFY_SOCKET": "/run/systemd/notify"}
    assert guardd.sd_notify("READY=1", env, socket_factory=fake_socket) is True
    assert fake_socket.made == [(socket.AF_UNIX, socket.SOCK_DGRAM)]
    assert fake_socket.sent == [(b"READY=1", "/run/systemd/notify")]


def test_sd_notify_abstract_namespace(fake_socket):
    env = {"NOTIFY_SOCKET": "@/org/freedesktop/systemd1/notify/123"}
    assert guardd.sd_notify("WATCHDOG=1", env, socket_factory=fake_socket) is True
    assert fake_socket.sent == [(b"WATCHDOG=1", "\0/org/freedesktop/systemd1/notify/123")]


@pytest.mark.parametrize("value", [None, "", "vsock:2:1234"])
def test_sd_notify_is_a_no_op_without_a_usable_socket(fake_socket, value):
    env = {} if value is None else {"NOTIFY_SOCKET": value}
    assert guardd.sd_notify("READY=1", env, socket_factory=fake_socket) is False
    assert fake_socket.made == []


def test_sd_notify_never_raises(monkeypatch):
    if not hasattr(socket, "AF_UNIX"):
        monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)

    class Broken(FakeSocket):
        def sendto(self, data, address):
            raise ConnectionRefusedError("no listener")

    assert guardd.sd_notify("READY=1", {"NOTIFY_SOCKET": "/x"}, socket_factory=Broken) is False


@pytest.mark.skipif(sys.platform == "win32" or not hasattr(socket, "AF_UNIX"),
                    reason="needs AF_UNIX datagram sockets")
def test_sd_notify_over_a_real_socket(tmp_path):
    path = str(tmp_path / "notify.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        listener.bind(path)
        listener.settimeout(2)
        assert guardd.sd_notify("READY=1", {"NOTIFY_SOCKET": path}) is True
        assert listener.recv(64) == b"READY=1"
    finally:
        listener.close()


@pytest.mark.parametrize("env_vars, expected", [
    ({}, None),
    ({"WATCHDOG_USEC": "30000000"}, 5.0),
    ({"WATCHDOG_USEC": "4000000"}, 2.0),
    ({"WATCHDOG_USEC": "30000000", "WATCHDOG_PID": str(os.getpid())}, 5.0),
    ({"WATCHDOG_USEC": "30000000", "WATCHDOG_PID": "1"}, None),
    ({"WATCHDOG_USEC": "soon"}, None),
    ({"WATCHDOG_USEC": "0"}, None),
])
def test_watchdog_interval(env_vars, expected):
    assert guardd.watchdog_interval(env_vars) == expected


def test_daemon_pings_the_watchdog_only_when_asked(env):
    env.start(env={"WATCHDOG_USEC": "30000000"})
    env.wait_for(lambda: env.rig.reads > 20, what="reads")
    assert env.stop() == 0
    # Once on the first pass, then not again for 5 s.
    assert env.sd.count("WATCHDOG=1") == 1
    assert env.sd[0] == "READY=1" and env.sd[-1] == "STOPPING=1"


def test_daemon_watchdog_cadence_follows_the_interval(env):
    env.start(env={"WATCHDOG_USEC": "30000000"}, watchdog_every=0.0)
    env.wait_for(lambda: env.sd.count("WATCHDOG=1") > 10, what="pings")
    assert env.stop() == 0


def test_no_watchdog_without_watchdog_usec(env):
    env.start()
    env.wait_for(lambda: env.rig.reads > 10, what="reads")
    assert env.stop() == 0
    assert "WATCHDOG=1" not in env.sd


def test_watchdog_is_pinged_through_the_shutdown_waits(env):
    env.settings["response_mode"] = guard_core.RESPONSE_SHUTDOWN_ONLY
    env.rig.amps = list(OVER)
    env.runner_hook = lambda argv: (env.daemon.request_stop()
                                    if argv == POWEROFF_FORCE else None)
    env.start(env={"WATCHDOG_USEC": "30000000"}, watchdog_every=0.0)
    assert env.join() == 0
    # 25 one-second steps, each followed by a ping, on top of the loop's own.
    assert env.sd.count("WATCHDOG=1") >= 25


# ======================================================================== notifications
def test_missing_notify_module_degrades_to_logging_only(env, monkeypatch):
    monkeypatch.setitem(sys.modules, "notify", None)
    env.var_dir.mkdir(parents=True)
    (env.var_dir / "last_shutdown.json").write_text('{"reason": "Tier 1: test."}', "utf-8")
    kwargs = dict(
        settings=env.settings, settings_path=None, state_dir=str(env.run_dir),
        var_dir=str(env.var_dir), log_dir=str(env.log_dir), logger=env.logger,
        backend_factory=lambda: FakeBackend(env.rig), mitigator_factory=env._make_mitigator,
        sleep=env._sleep, runner=env._run, sd_notify_fn=env._sd, env={}, locker=env._lock,
        poll_interval=0.01, reconnect_retry=0.02,
    )
    env.daemon = RecordingDaemon(history=env.history, **kwargs)
    env.thread = threading.Thread(target=lambda: setattr(env, "code", env.daemon.run()),
                                  daemon=True)
    env.thread.start()
    env.wait_for(lambda: "OK" in env.statuses(), what="OK")
    assert env.stop() == 0
    warnings = [m for m in env.logs("WARNING") if m.startswith("Desktop notifications "
                                                                "unavailable")]
    assert len(warnings) == 1
    assert env.has_log("The last shutdown was an emergency stop by 12VHPWR Guard", "WARNING")
    assert not (env.var_dir / "last_shutdown.json").exists()


def test_a_slow_notification_never_stalls_the_loop(env):
    env.desktop_delay = 0.5
    env.start()
    env.wait_for(lambda: env.rig.reads >= 1, what="first read")
    reads = env.rig.reads
    time.sleep(0.3)
    # "Monitoring started." is still being delivered, and the loop has carried on anyway.
    assert env.rig.reads - reads > 5
    assert env.stop() == 0


def test_notification_failure_is_logged_not_raised(env):
    def broken(title, body, critical=False):
        raise RuntimeError("runuser missing")

    env.start(desktop_notify=broken)
    env.wait_for(lambda: env.has_log("Notification failed: runuser missing"), what="failure")
    assert env.thread.is_alive()
    assert env.stop() == 0


# ======================================================================== settings, import
def test_load_settings_reads_the_file_and_falls_back(tmp_path):
    path = tmp_path / "config.ini"
    assert guard_core.load_settings(str(path)) == guard_core.default_settings()
    path.write_text("[Settings]\nthreshold_amps = 10\nresponse_mode = Shutdown_Only\n"
                    "critical_amps = 0\nadvisories = off\nsensor_loss_restart_seconds = 0\n",
                    encoding="utf-8")
    loaded = guard_core.load_settings(str(path))
    assert loaded["threshold_amps"] == 10.0
    assert loaded["response_mode"] == "shutdown_only"
    assert loaded["critical_amps"] == guard_core.DEFAULT_CRITICAL_AMPS
    assert loaded["advisories_enabled"] is False
    assert loaded["sensor_loss_restart_seconds"] == 0.0
    path.write_text("[Settings]\nthreshold_amps = -1\nadvisories = off\n", encoding="utf-8")
    assert guard_core.load_settings(str(path)) == guard_core.default_settings()


def test_daemon_reads_its_config_file(tmp_path):
    config = tmp_path / "config.ini"
    config.write_text("[Settings]\nresponse_mode = shutdown_only\nthreshold_amps = 8.5\n",
                      encoding="utf-8")
    daemon = guardd.Daemon(settings_path=str(config), state_dir=str(tmp_path / "run"),
                           var_dir=str(tmp_path / "var"), log_dir=str(tmp_path / "log"),
                           desktop_notify=None, wall=None)
    assert daemon.settings["response_mode"] == "shutdown_only"
    assert daemon.settings["threshold_amps"] == 8.5


def test_parse_args_defaults_and_overrides():
    args = guardd.parse_args([])
    assert args.config == "/etc/12vhpwr-guard/config.ini"
    assert args.state_dir == "/run/12vhpwr-guard"
    assert args.var_dir == "/var/lib/12vhpwr-guard"
    assert args.log_dir == "/var/log/12vhpwr-guard"
    args = guardd.parse_args(["--config", "c.ini", "--state-dir", "s", "--var-dir", "v",
                              "--log-dir", "l"])
    assert (args.config, args.state_dir, args.var_dir, args.log_dir) == ("c.ini", "s", "v", "l")


def test_journal_formatter_prefixes_priority_only_for_the_journal():
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "pin hot", None, None)
    assert guardd._JournalFormatter(journal=False).format(record) == "WARNING | pin hot"
    assert guardd._JournalFormatter(journal=True).format(record) == "<4>WARNING | pin hot"


BLOCKED = ("ctypes.wintypes", "win32api", "win32con", "win32evtlogutil", "winotify",
           "pystray", "tkinter")


def test_imports_without_any_windows_module():
    code = f"""
import sys
for name in {BLOCKED!r}:
    sys.modules[name] = None
sys.path[:0] = [{str(REPO_ROOT)!r}, {str(LINUX_DIR)!r}]
import guardd
assert guardd.LINUX_APP_VERSION == "1.5.0"
for name in {BLOCKED!r}:
    assert sys.modules[name] is None, name + " was replaced by a real import"
assert "hwinfo_12vhpwr_guard" not in sys.modules
print("guardd ok")
"""
    proc = subprocess.run([sys.executable, "-I", "-c", code],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "guardd ok" in proc.stdout


def test_source_has_no_em_dash():
    for path in (LINUX_DIR / "guardd.py", Path(__file__)):
        assert chr(0x2014) not in path.read_text(encoding="utf-8"), path

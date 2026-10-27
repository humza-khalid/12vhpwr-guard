#!/usr/bin/env python3
"""
12VHPWR Guard for Linux: the root daemon that watches an ASUS ROG Astral's 16-pin connector.

Same protection as the Windows guard, from the same engine: guard_core decides (the tier 1
sustained timer, the tier ladder, the pin advisories and the sensor-path check), gpu_mitigation
limits the card through NVML, and linux/astral_linux.py reads the six pins over i2c-dev. This
file is only the Linux shell around that engine, the counterpart of monitor_loop and its
helpers in hwinfo_12vhpwr_guard.py: connect, read, decide, say it, act. Wherever a message is
not about Windows itself, it is the Windows message word for word.

Where it says things:
  - the journal (stdout, "LEVEL | message", captured by systemd) gets every log line;
  - desktop notifications (linux/notify.py) stand in for toasts, critical for warnings,
    errors, shutdown and "not protected";
  - a state file (/run/12vhpwr-guard/state.json) stands in for the tray icon and tooltip,
    read by the `12vhpwr-guard status` command;
  - a wall message goes out with an emergency shutdown, for console and SSH sessions.

systemd runs it (Type=notify, WatchdogSec=30, Restart=always). READY=1 goes out once startup
is done, WATCHDOG=1 from the loop, so a hung loop is restarted rather than left blind, and a
planned self-restart is simply exit code 75.

Every outside effect goes through a seam on the Daemon class (backend, mitigator, clock, sleep,
the command runner that powers the machine off, notifications, sd_notify, the lock, paths), so
tests/test_linux_daemon.py drives the real loop with fakes on any OS and nothing in it can
power anything off. Stdlib only, plus the repo's own modules.
"""

import argparse
import errno
import json
import logging
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

# The installed layout mirrors the repo (/opt/12vhpwr-guard/linux/guardd.py next to
# /opt/12vhpwr-guard/guard_core.py), so this directory and its parent are all the path needs.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
for _path in (_REPO_ROOT, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import guard_core  # noqa: E402
from guard_core import (  # noqa: E402
    ADVISORY_EVENT_IDS,
    NOTICE_ERROR,
    NOTICE_WARNING,
    RESPONSE_SHUTDOWN_ONLY,
    RESPONSE_TIERED,
    THRESHOLD_CLEARED,
    THRESHOLD_OVER,
    Advisory,
    FlightRecorder,
    Notice,
    PinAdvisor,
    ThresholdTracker,
    TieredResponse,
)
from astral_i2c import SensorGoneError, TransientReadError  # noqa: E402
from astral_linux import AstralLinuxBackend  # noqa: E402

# Optional, as on Windows: without it the ladder runs shutdown-only and says why.
try:
    from gpu_mitigation import GpuMitigator
except Exception:  # pragma: no cover - only on a broken install
    GpuMitigator = None

# There is no APP_VERSION shared between the platforms yet.
LINUX_APP_VERSION = "1.5.0"

# =========================
# Paths (each one overridable on the command line, for the tests and the Mint e2e)
# =========================
DEFAULT_CONFIG_PATH = "/etc/12vhpwr-guard/config.ini"
# tmpfs: cleared on reboot together with the GPU settings the marker describes.
DEFAULT_STATE_DIR = "/run/12vhpwr-guard"
# Must survive the reboot, because the shutdown note is read at the next start.
DEFAULT_VAR_DIR = "/var/lib/12vhpwr-guard"
DEFAULT_LOG_DIR = "/var/log/12vhpwr-guard"

STATE_FILE_NAME = "state.json"
LOCK_FILE_NAME = "guardd.lock"
NOTE_FILE_NAME = "last_shutdown.json"
# When each per-start notification last reached a desktop, so a crash loop cannot spam one.
# In the state dir, which systemd keeps across restarts (RuntimeDirectoryPreserve=restart).
NOTIFY_STAMPS_FILE_NAME = "notify_stamps.json"
# gpu_mitigation.MARKER_NAME, spelled out so a failed import of that module cannot also
# take the path away from startup recovery's existence check.
MARKER_FILE_NAME = "mitigation.active"

# =========================
# Timing, the Windows guard's numbers
# =========================
POLL_INTERVAL_SEC = 0.5
RECONNECT_RETRY_SEC = 3.0
RESUME_GRACE_SEC = 5.0
PINS_MISSING_RETRY_SEC = 2.0
# Windows passes /t 5 to shutdown.exe; here the daemon waits the same 5 s itself.
SHUTDOWN_DELAY_SEC = 5.0
# A unit that hangs the orderly stop must not be able to keep a faulty connector loaded.
SHUTDOWN_FORCE_AFTER_SEC = 20.0

# =========================
# Linux only
# =========================
# The state file is for humans and the CLI, so once a second is plenty. Status changes go
# out at once regardless.
STATE_WRITE_EVERY_SEC = 1.0
# WatchdogSec=30 in the unit, so a ping every 5 s leaves plenty of margin.
WATCHDOG_EVERY_SEC = 5.0
# A root daemon starts before anyone logs in, so a notification sent at boot reaches nobody.
# The few that describe a lasting problem are sent again at this cadence until one desktop
# session has actually received them (see Daemon._sticky_set).
STICKY_RETRY_SEC = 30.0
# The unit restarts the daemon forever, every 2 s (StartLimitIntervalSec=0). The
# notifications every start sends ("Monitoring started.", the first "Monitoring active",
# the first "not protected") go out at most once per this window across restarts. Only the
# first of each in a process is limited; later ones answer a real event in that process
# (a lost sensor coming back, say). Overcurrent, shutdown and everything else never are.
PER_START_NOTIFY_EVERY_SEC = 600.0
PER_START_KEYS = ("started", "connected", "not_protected")

# EX_TEMPFAIL, the same planned-restart code as Windows. systemd restarts on it.
RESTART_EXIT_CODE = 75
EXIT_OK = 0
EXIT_FATAL = 1

# The status values in state.json (the shared contract with guard_cli.py).
STATUS_OK = "OK"
STATUS_OVER = "OVER"
STATUS_MITIGATED = "MITIGATED"
# No sensor connected yet, or reconnecting. Windows shows this purple too.
STATUS_WAITING = "WAITING"
# A sensor-path fault (the pins see no current) or a lost sensor.
STATUS_ERROR = "ERROR"
STATUS_SHUTDOWN = "SHUTDOWN"
STATUS_STOPPED = "STOPPED"

# Commands for the emergency power-off, run through the injectable runner.
POWEROFF_COMMAND = ["systemctl", "poweroff"]
POWEROFF_FORCE_COMMAND = ["systemctl", "poweroff", "--force"]

_UNSET = object()


def _sessions(reached) -> int:
    """send_desktop_notification's answer as a count; anything odd counts as nobody."""
    try:
        return int(reached or 0)
    except (TypeError, ValueError):
        return 0


def _span_text(seconds: float) -> str:
    """A window in seconds as something a notification can say out loud (as on Windows)."""
    if seconds >= 120:
        return f"{seconds / 60:.0f} minutes"
    if seconds >= 60:
        return "1 minute"
    return f"{seconds:g} seconds"


# =========================
# systemd
# =========================
def sd_notify(message: str, env: Optional[Dict[str, str]] = None,
              socket_factory: Optional[Callable] = None) -> bool:
    """Send one sd_notify datagram to $NOTIFY_SOCKET. True when it went out.

    A no-op (False) when the variable is unset, which is every run outside systemd. The
    protocol is one AF_UNIX datagram holding "KEY=VALUE" lines; an address starting with "@"
    is in the abstract namespace, which the socket API spells with a leading NUL. Never
    raises: a lost READY costs a start timeout, a lost WATCHDOG costs a restart, and neither
    is worth crashing the loop over.
    """
    env = os.environ if env is None else env
    address = env.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    elif not address.startswith("/"):
        # vsock: and friends, for VM supervisors. Not something this daemon runs under.
        return False
    family = getattr(socket, "AF_UNIX", None)
    if family is None:
        return False
    factory = socket_factory or socket.socket
    try:
        sock = factory(family, socket.SOCK_DGRAM)
    except OSError:
        return False
    try:
        sock.sendto(message.encode("utf-8"), address)
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def watchdog_interval(env: Optional[Dict[str, str]] = None) -> Optional[float]:
    """Seconds between WATCHDOG=1 pings, or None when systemd is not watching this process.

    WATCHDOG_USEC is set by systemd when the unit has WatchdogSec; WATCHDOG_PID, when present,
    names the process it applies to. Every 5 s, or half the watchdog period if someone set it
    shorter than 10 s.
    """
    env = os.environ if env is None else env
    raw = env.get("WATCHDOG_USEC")
    if not raw:
        return None
    pid = env.get("WATCHDOG_PID")
    if pid and pid.strip() != str(os.getpid()):
        return None
    try:
        usec = int(raw)
    except ValueError:
        return None
    if usec <= 0:
        return None
    return min(WATCHDOG_EVERY_SEC, usec / 1_000_000 / 2)


# =========================
# Single instance
# =========================
def flock_exclusive(path: str) -> Optional[Callable[[], None]]:
    """Take an exclusive, non-blocking flock on path. Returns a release callable, or None
    when another process already holds it.

    Two daemons would mean two loops limiting and restoring the same card, and two startup
    recoveries undoing each other's limit, so the lock is taken before anything touches the
    state directory or the GPU. flock dies with the process, so a crash never leaves a stale
    lock behind. fcntl is imported here so the module still imports on Windows (tests).
    """
    import fcntl
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as ex:
        os.close(fd)
        if ex.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return None
        raise
    try:
        # Informational only: the lock is the flock, not this number.
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    except OSError:
        pass

    def release() -> None:
        try:
            os.close(fd)
        except OSError:
            pass

    return release


# =========================
# The emergency power-off
# =========================
def run_command(argv: List[str]) -> None:
    """The real runner: run one command, bounded, and log nothing (the caller does).

    The binary is looked up in the system directories before PATH, for the same reason the
    Windows guard calls shutdown.exe by absolute path: the one safety-critical action must not
    depend on the environment it happens to inherit.
    """
    cmd = list(argv)
    if cmd and "/" not in cmd[0]:
        for folder in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
            candidate = os.path.join(folder, cmd[0])
            if os.path.exists(candidate):
                cmd[0] = candidate
                break
        else:
            found = shutil.which(cmd[0])
            if found:
                cmd[0] = found
    subprocess.run(cmd, check=False, timeout=30,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)


# =========================
# Notifications
# =========================
class NotificationQueue:
    """Runs notification jobs on one background thread, in order.

    A desktop notification means a runuser + notify-send per logged-in session, which can take
    a while, and the loop must never wait on that: tier 2 has a 3 second window and a stalled
    tick would also starve the watchdog. So the loop only enqueues. Bounded, and a job that
    cannot be queued is dropped with one warning; the journal has the text regardless.
    """

    def __init__(self, logger: logging.Logger, maxsize: int = 64):
        self._log = logger
        self._q: "queue.Queue" = queue.Queue(maxsize)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._dropped_warned = False

    def submit(self, fn: Callable, *args, **kwargs) -> None:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._worker,
                                                name="guardd-notify", daemon=True)
                self._thread.start()
        try:
            self._q.put_nowait((fn, args, kwargs))
        except queue.Full:
            if not self._dropped_warned:
                self._dropped_warned = True
                self._log.warning("Notification queue full; dropping notifications until "
                                  "it drains (the journal still has every message).")

    def _worker(self) -> None:
        while True:
            job = self._q.get()
            try:
                if job is None:
                    return
                fn, args, kwargs = job
                try:
                    fn(*args, **kwargs)
                except Exception as ex:
                    self._log.warning(f"Notification failed: {ex}")
            finally:
                self._q.task_done()

    def flush(self, timeout: float) -> bool:
        """Wait up to timeout for every queued job to finish. True when the queue drained."""
        deadline = time.monotonic() + timeout
        with self._q.all_tasks_done:
            while self._q.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._q.all_tasks_done.wait(remaining)
        return True

    def close(self, timeout: float) -> None:
        if self._thread is None:
            return
        self.flush(timeout)
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass


# =========================
# The daemon
# =========================
class Daemon:
    """One guard process: startup, the monitor loop, and a clean or emergency end.

    run() does all of it in the calling thread and returns the exit code. main() runs it on a
    worker thread so the signal handlers (which Python always runs on the main thread) never
    interrupt the loop while it holds a lock.

    Seams, all optional (the defaults are the real system):
      settings / settings_path   a settings dict (merged over the defaults), or the file
      state_dir, var_dir, log_dir
      logger
      backend_factory()          -> a sensor backend (connect/read_pins/close/key/tag)
      mitigator_factory(marker_path, logger) -> a GpuMitigator-shaped object, or None
      clock()                    wall clock for every decision (time.time)
      monotonic()                for the watchdog and sticky cadence (time.monotonic)
      sleep(seconds)             only for the shutdown sequence (time.sleep)
      runner(argv)               runs the power-off commands (run_command)
      desktop_notify(title, body, critical=False) -> sessions reached
      wall(message) -> bool      both default to linux/notify.py, imported lazily
      sd_notify(message) -> bool
      env                        where WATCHDOG_USEC is read (os.environ)
      locker(path)               -> release callable, or None when already running
    """

    def __init__(self, settings: Optional[dict] = None,
                 settings_path: Optional[str] = DEFAULT_CONFIG_PATH,
                 state_dir: str = DEFAULT_STATE_DIR,
                 var_dir: str = DEFAULT_VAR_DIR,
                 log_dir: str = DEFAULT_LOG_DIR,
                 logger: Optional[logging.Logger] = None,
                 backend_factory: Optional[Callable] = None,
                 mitigator_factory=_UNSET,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 runner: Optional[Callable[[List[str]], None]] = None,
                 desktop_notify=_UNSET,
                 wall=_UNSET,
                 sd_notify_fn: Optional[Callable[[str], bool]] = None,
                 env: Optional[Dict[str, str]] = None,
                 locker: Optional[Callable] = None,
                 poll_interval: float = POLL_INTERVAL_SEC,
                 reconnect_retry: float = RECONNECT_RETRY_SEC,
                 resume_grace: float = RESUME_GRACE_SEC,
                 pins_missing_retry: float = PINS_MISSING_RETRY_SEC,
                 shutdown_delay: float = SHUTDOWN_DELAY_SEC,
                 force_after: float = SHUTDOWN_FORCE_AFTER_SEC,
                 state_write_every: float = STATE_WRITE_EVERY_SEC,
                 sticky_retry: float = STICKY_RETRY_SEC,
                 per_start_every: float = PER_START_NOTIFY_EVERY_SEC,
                 watchdog_every: Optional[float] = None):
        self.log = logger or logging.getLogger("12vhpwr_guard")

        self.settings_path = settings_path
        loaded = guard_core.load_settings(settings_path) if settings is None \
            else guard_core.default_settings()
        if settings is not None:
            loaded.update(settings)
        self.settings = loaded

        self.state_dir = state_dir
        self.var_dir = var_dir
        self.log_dir = log_dir
        self.state_path = os.path.join(state_dir, STATE_FILE_NAME)
        self.lock_path = os.path.join(state_dir, LOCK_FILE_NAME)
        self.marker_path = os.path.join(state_dir, MARKER_FILE_NAME)
        self.note_path = os.path.join(var_dir, NOTE_FILE_NAME)
        self.stamps_path = os.path.join(state_dir, NOTIFY_STAMPS_FILE_NAME)

        self._backend_factory = backend_factory or AstralLinuxBackend
        if mitigator_factory is _UNSET:
            mitigator_factory = _default_mitigator_factory
        self._mitigator_factory = mitigator_factory
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._runner = runner or run_command
        self._sd_notify = sd_notify_fn or (lambda message: sd_notify(message, env))
        self._env = os.environ if env is None else env
        self._locker = locker or flock_exclusive

        self.poll_interval = poll_interval
        self.reconnect_retry = reconnect_retry
        self.resume_grace = resume_grace
        self.pins_missing_retry = pins_missing_retry
        self.shutdown_delay = shutdown_delay
        self.force_after = force_after
        self.state_write_every = state_write_every
        self.sticky_retry = sticky_retry
        self.per_start_every = per_start_every
        interval = watchdog_interval(self._env)
        if interval is not None and watchdog_every is not None:
            interval = watchdog_every
        self._watchdog_every = interval

        self._queue = NotificationQueue(self.log)
        self._desktop_fn, self._wall_fn = self._resolve_notifiers(desktop_notify, wall)

        # Set from any thread (the signal handler) and waited on by the loop.
        self._stop = threading.Event()

        # -- engine ----------------------------------------------------------
        self._tracker = ThresholdTracker(self.log, notify=self._render_notice)
        self._advisor = PinAdvisor(self._advisory_values())
        self._flight = FlightRecorder(log_dir)
        self._response = TieredResponse(
            self._ensure_mitigator, self.log,
            get_tier_values=self._tier_values,
            get_response_mode=lambda: self.settings["response_mode"],
            notify=self._render_notice,
        )

        # -- loop state, the Windows monitor_loop's locals -------------------
        self._backend = None
        self._subsystem_id: Optional[int] = None
        self._mitigator = None
        self._mitigation_tried = False
        self._shutting_down = False
        self._restarting = False
        self._pins_missing = False
        self._last_sample: Optional[float] = None
        self._direct_seen = False
        self._source_lost_at: Optional[float] = None
        self._source_missing_notified = False
        self._source_connected_notified = False
        self._source_missing_logged = False
        self._last_missing_detail: Optional[str] = None

        # -- what state.json says --------------------------------------------
        self._status = STATUS_WAITING
        self._message = "Starting..."
        self._max: Tuple[Optional[str], Optional[float]] = (None, None)
        self._source = ""
        self._advisory: Optional[str] = None
        self._sensor_path_fault = False
        self._last_written_key = None
        self._last_written_at: Optional[float] = None

        # -- sticky notifications and the watchdog ---------------------------
        self._sticky: Dict[str, Tuple[str, str]] = {}
        self._sticky_lock = threading.Lock()
        self._sticky_last = 0.0
        self._watchdog_last: Optional[float] = None
        # Per-start notifications already used once by this process.
        self._per_start_used = set()
        self._stamps_lock = threading.Lock()

    # ------------------------------------------------------------------ settings
    def _tier_values(self) -> dict:
        return {key: self.settings[key] for key in guard_core.DEFAULT_TIER_VALUES}

    def _advisory_values(self) -> dict:
        return {key: self.settings[key] for key in guard_core.DEFAULT_ADVISORY_VALUES}

    # ------------------------------------------------------------- notifications
    def _resolve_notifiers(self, desktop, wall):
        """The injected functions, or linux/notify.py's, imported here and only here.

        A missing or broken notify module costs the desktop notifications and nothing else:
        one warning, and every message still reaches the journal.
        """
        if desktop is not _UNSET and wall is not _UNSET:
            return desktop, wall
        try:
            import notify as notify_module
            real_desktop = notify_module.send_desktop_notification
            real_wall = notify_module.send_wall
        except Exception as ex:
            self.log.warning(f"Desktop notifications unavailable (notify module: {ex}); "
                             f"every message still goes to the journal.")
            real_desktop = real_wall = None
        return (real_desktop if desktop is _UNSET else desktop,
                real_wall if wall is _UNSET else wall)

    def _desktop(self, title: str, body: str, critical: bool = False,
                 stamp_key: Optional[str] = None) -> None:
        if self._desktop_fn is None:
            return
        if stamp_key is None:
            self._queue.submit(self._desktop_fn, title, body, critical=critical)
        else:
            self._queue.submit(self._deliver_stamped, stamp_key, title, body, critical)

    def _deliver_stamped(self, key: str, title: str, body: str, critical: bool) -> None:
        if _sessions(self._desktop_fn(title, body, critical=critical)) > 0:
            self._write_stamp(key)

    # -- per-start rate limit --------------------------------------------------
    def _read_stamps(self) -> Dict[str, float]:
        try:
            with open(self.stamps_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {k: float(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _write_stamp(self, key: str) -> None:
        with self._stamps_lock:
            stamps = self._read_stamps()
            stamps[key] = float(self._clock())
            tmp = self.stamps_path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(stamps, f)
                os.replace(tmp, self.stamps_path)
            except OSError as ex:
                self.log.debug(f"Could not write {self.stamps_path}: {ex}")

    def _per_start_allowed(self, key: str) -> bool:
        """False when this is the process's first notification of this kind and an earlier
        process already got one to a desktop inside PER_START_NOTIFY_EVERY_SEC."""
        if key in self._per_start_used:
            return True
        self._per_start_used.add(key)
        with self._stamps_lock:
            stamp = self._read_stamps().get(key)
        if stamp is None:
            return True
        age = float(self._clock()) - stamp
        if 0 <= age < self.per_start_every:
            self.log.info(f"Not repeating the '{key}' notification: one reached a desktop "
                          f"{age:.0f} s ago, before a restart.")
            return False
        return True

    def _send_wall(self, message: str) -> None:
        if self._wall_fn is None:
            return
        self._queue.submit(self._wall_fn, message)

    def _render_notice(self, notice: Notice) -> None:
        """guard_core's notifier on Linux. The core has already written the log line; a
        Notice with a toast becomes a desktop notification, critical for warnings and errors.
        Windows-only parts (the Event Viewer id) have no Linux counterpart."""
        if notice.toast is None:
            return
        title, body = notice.toast
        self._desktop(title, body, critical=notice.level in (NOTICE_WARNING, NOTICE_ERROR))

    def _sticky_set(self, key: str, title: str, body: str) -> None:
        """A critical notification about a lasting problem, sent now and again every
        STICKY_RETRY_SEC until at least one desktop session has received it.

        This is what makes "not protected" and the last-shutdown note reach a person on
        Linux: the daemon is up long before anyone logs in, and a notification sent to no
        session at all is not a notification. Once one session has it, it is not repeated,
        so a logged-in user sees it exactly once, as on Windows.
        """
        if self._desktop_fn is None:
            return
        with self._sticky_lock:
            self._sticky[key] = (title, body)
        self._sticky_last = self._monotonic()
        self._queue.submit(self._deliver_sticky, key, title, body)

    def _sticky_clear(self, key: str) -> None:
        with self._sticky_lock:
            self._sticky.pop(key, None)

    def _deliver_sticky(self, key: str, title: str, body: str) -> None:
        with self._sticky_lock:
            if self._sticky.get(key) != (title, body):
                return      # cleared or replaced while it waited in the queue
        if _sessions(self._desktop_fn(title, body, critical=True)) > 0:
            with self._sticky_lock:
                if self._sticky.get(key) == (title, body):
                    del self._sticky[key]
            if key in PER_START_KEYS:
                self._write_stamp(key)

    def _retry_sticky(self) -> None:
        with self._sticky_lock:
            pending = list(self._sticky.items())
        if not pending:
            return
        now = self._monotonic()
        if (now - self._sticky_last) < self.sticky_retry:
            return
        self._sticky_last = now
        for key, (title, body) in pending:
            self._queue.submit(self._deliver_sticky, key, title, body)

    def flush_notifications(self, timeout: float = 2.0) -> bool:
        return self._queue.flush(timeout)

    # -------------------------------------------------------------------- systemd
    def _notify_systemd(self, message: str) -> None:
        try:
            self._sd_notify(message)
        except Exception:
            pass

    def _watchdog(self) -> None:
        """WATCHDOG=1, at most every _watchdog_every seconds, only when systemd asked."""
        if self._watchdog_every is None:
            return
        now = self._monotonic()
        if self._watchdog_last is not None and (now - self._watchdog_last) < self._watchdog_every:
            return
        self._watchdog_last = now
        self._notify_systemd("WATCHDOG=1")

    # ---------------------------------------------------------------- state file
    def _set_status(self, status: str, message: str) -> None:
        self._status = status
        self._message = message

    def _set_ok_status(self, message: str) -> None:
        """Windows set_ok_status: OK, unless the guard cannot see the load at all.

        OVER, MITIGATED and SHUTDOWN are set elsewhere and still win; this is only about the
        guard's right to say OK.
        """
        if self._sensor_path_fault:
            self._set_status(STATUS_ERROR, "Not protected: the pin sensors see no current")
        else:
            self._set_status(STATUS_OK, message)

    def _quiet_effective_mode(self) -> str:
        """What will actually happen, without TieredResponse.effective_mode()'s side effects
        (it warns once and would bind NVML before the sensor is even connected)."""
        mode = self.settings["response_mode"]
        if mode != RESPONSE_TIERED:
            return mode
        m = self._mitigator
        if self._mitigation_tried and (m is None or not m.is_available):
            return RESPONSE_SHUTDOWN_ONLY
        return RESPONSE_TIERED

    def _mitigation_text(self) -> str:
        m = self._mitigator
        # The mitigator's own flag wins when it has one: a stop releases through close(),
        # behind the response ladder's back, exactly as Exit does on Windows.
        if self._response.engaged and (m is None or getattr(m, "engaged", True)):
            return "engaged"
        if m is None:
            if self.settings["response_mode"] != RESPONSE_TIERED:
                return "unavailable: response mode is shutdown-only"
            if self._mitigation_tried:
                return "unavailable: mitigation module unavailable"
            return "unavailable: not connected yet"
        if m.is_available:
            return "ready"
        return f"unavailable: {m.unavailable_reason or 'unknown'}"

    def state_payload(self) -> dict:
        label, value = self._max
        return {
            "version": LINUX_APP_VERSION,
            "pid": os.getpid(),
            "updated": float(self._clock()),
            "status": self._status,
            "message": self._message,
            "max_label": label,
            "max_val": None if value is None else round(float(value), 3),
            "max_unit": "A",
            "source": self._source,
            "mode": self._quiet_effective_mode(),
            "mitigation": self._mitigation_text(),
            "advisory": self._advisory,
            "sensor_path_fault": bool(self._sensor_path_fault),
        }

    def _publish(self, force: bool = False) -> None:
        """Write state.json at once when the status (or anything else a reader acts on)
        changed, otherwise at most once a second. The message and the amps alone change on
        nearly every tick, so they ride along with the once-a-second write."""
        payload = self.state_payload()
        key = (payload["status"], payload["source"], payload["mode"], payload["mitigation"],
               payload["advisory"], payload["sensor_path_fault"])
        now = payload["updated"]
        due = (self._last_written_at is None
               or now < self._last_written_at
               or (now - self._last_written_at) >= self.state_write_every)
        if not force and key == self._last_written_key and not due:
            return
        if self._write_state(payload):
            self._last_written_key = key
            self._last_written_at = now

    def _write_state(self, payload: dict) -> bool:
        """tmp + os.replace, so a reader sees the old file or the new one, never half of one."""
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            try:
                os.chmod(tmp, 0o644)    # the CLI runs as an ordinary user
            except OSError:
                pass
            os.replace(tmp, self.state_path)
            return True
        except OSError as ex:
            self.log.debug(f"Could not write {self.state_path}: {ex}")
            return False

    # ---------------------------------------------------------------- mitigator
    def _ensure_mitigator(self):
        """Windows ensure_mitigator: bind NVML the first time tiered mode actually needs it.

        Shutdown-only never gets here, which is the point: that mode is the rollback if
        anything about the GPU limiting misbehaves, so it must not touch NVML at all. Tried
        once per process, bound to the card the sensor is reading (subsystem id).
        """
        if self._mitigator_factory is None:
            return None
        if self.settings["response_mode"] != RESPONSE_TIERED:
            return self._mitigator
        if self._mitigator is None and not self._mitigation_tried:
            self._mitigation_tried = True
            try:
                candidate = self._mitigator_factory(self.marker_path, self.log)
            except Exception as ex:
                self.log.error(f"Mitigation: could not be set up ({ex})")
                return None
            if candidate is None:
                return None
            try:
                candidate.connect(self._subsystem_id)
            except Exception as ex:
                self.log.error(f"Mitigation: connect failed ({ex})")
            self._mitigator = candidate
        return self._mitigator

    def _board_power_watts(self) -> Optional[float]:
        """What the card says it is drawing, or None. Tiered mode only, as on Windows: the
        mode is checked here so shutdown-only never asks NVML anything."""
        if self.settings["response_mode"] != RESPONSE_TIERED:
            return None
        m = self._ensure_mitigator()
        if m is None or not m.is_available:
            return None
        try:
            milliwatts = m.power_usage_mw()
        except Exception:
            return None
        return None if milliwatts is None else float(milliwatts) / 1000.0

    # ------------------------------------------------------------------ startup
    def _prepare_dirs(self) -> None:
        # 0755: the CLI reads state.json as an ordinary user.
        for folder in (self.state_dir, self.var_dir, self.log_dir):
            os.makedirs(folder, mode=0o755, exist_ok=True)

    def _log_startup(self) -> None:
        s = self.settings
        tiers = self._tier_values()
        self.log.info(f"Starting 12VHPWR Guard v{LINUX_APP_VERSION} (daemon)...")
        path = self.settings_path
        if path:
            self.log.info(f"Config: {path}" + ("" if os.path.exists(path)
                                               else " (not found, using defaults)"))
        self.log.info(f"Flight recordings: {self.log_dir}")
        self.log.info(
            f"Threshold={s['threshold_amps']:.2f}A | Poll={self.poll_interval}s | "
            f"Sustained={s['sustained_seconds_required']}s"
        )
        self.log.info(
            f"Response mode={s['response_mode']} | "
            f"Tier 2 critical={tiers['critical_amps']:.2f}A/{tiers['critical_seconds']:.0f}s | "
            f"Tier 3 catastrophic={tiers['catastrophic_amps']:.2f}A"
        )
        if s["advisories_enabled"]:
            self.log.info(
                f"Advisories (warn only): load gate={s['advisory_load_amps']:.1f}A | "
                f"dead pin<={s['dead_pin_max_amps']:.2f}A | "
                f"spread>={s['imbalance_spread_amps']:.2f}A | "
                f"sustain={s['advisory_sustain_seconds']:.0f}s | "
                f"repeat={s['advisory_repeat_seconds']:.0f}s"
            )
        else:
            self.log.info("Advisories: off")
        if s["response_mode"] == RESPONSE_TIERED:
            self.log.info("Sensor-path check: pins and board power")
        else:
            self.log.info("Sensor-path check: pins only (no NVML in shutdown-only mode)")
        restart_after = s["sensor_loss_restart_seconds"]
        self.log.info("Self-restart after direct sensor loss: "
                      + (f"{restart_after:g} s" if restart_after > 0 else "off"))
        if s["sensor_backend"] == "hwinfo":
            self.log.warning("sensor_backend = hwinfo has no meaning on Linux (there is no "
                             "HWiNFO here); using the card's own sensor.")
        key = getattr(self._backend_factory, "key", AstralLinuxBackend.key)
        self.log.info(f"Sensor backend: setting={s['sensor_backend']}, trying [{key}]")

    def _run_startup_recovery(self) -> None:
        """Windows run_startup_recovery: undo a GPU limit left behind by a previous run that
        died while mitigating (a crash, kill -9, or a watchdog restart).

        Only when the marker is actually there, so a daemon that never mitigates never loads
        NVML. Runs after the lock is held: a second daemon must never undo the first one's
        live limit. The recovery mitigator is closed afterwards; the loop binds its own.
        """
        if not os.path.exists(self.marker_path) or self._mitigator_factory is None:
            return
        try:
            m = self._mitigator_factory(self.marker_path, self.log)
        except Exception as ex:
            self.log.error(f"Startup recovery failed: {ex}")
            return
        if m is None:
            return
        try:
            msg = m.startup_recovery()
            if msg:
                self.log.warning(msg)
                restored = msg.endswith("clocks and power limit restored")
                self._desktop("12VHPWR Guard", msg, critical=not restored)
        except Exception as ex:
            self.log.error(f"Startup recovery failed: {ex}")
        finally:
            try:
                m.close()
            except Exception:
                pass

    def _announce_last_shutdown(self) -> None:
        """Windows announce_last_shutdown: if the guard ended the last session, say so loudly.

        A corrupt note still announces (generic wording). The file is removed either way; the
        notification itself is sticky, because at boot nobody is logged in to see it yet.
        """
        if not os.path.exists(self.note_path):
            return
        try:
            try:
                with open(self.note_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
            when = data.get("time_human", "an earlier session")
            reason = data.get("reason", "sustained overcurrent on a 12VHPWR pin")
            msg = (f"The last shutdown was an emergency stop by 12VHPWR Guard ({when}): "
                   f"{reason} Check the connector and cable seating before the next "
                   f"heavy load.")
            self.log.warning(msg)
            self._sticky_set("announce", "12VHPWR Guard - read this", msg)
        finally:
            for path in (self.note_path + ".tmp", self.note_path):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass

    def _note_shutdown_reason(self, reason: str) -> None:
        """Windows note_shutdown_reason, into the var dir so it survives the reboot."""
        payload = {
            "time": time.time(),
            "time_human": time.strftime("%Y-%m-%d %H:%M:%S"),
            "reason": reason,
            "version": LINUX_APP_VERSION,
        }
        tmp = self.note_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.note_path)
        except Exception as ex:
            self.log.warning(f"Could not write the shutdown note {self.note_path}: {ex}")

    # ---------------------------------------------------------------- run
    def request_stop(self, signum: Optional[int] = None, _frame=None) -> None:
        """SIGTERM/SIGINT handler, also callable directly. Safe from any thread.

        During an emergency shutdown the stop is noted but the shutdown sequence still runs
        to its end: `systemctl poweroff` itself sends this SIGTERM, and the forced power-off
        20 s later only happens if this process is still there to issue it.
        """
        try:
            name = signal.Signals(signum).name if signum else "stop request"
        except (ValueError, TypeError):
            name = f"signal {signum}"
        if self._shutting_down:
            self.log.warning(f"{name} received during an emergency shutdown; finishing the "
                             f"shutdown sequence first.")
        else:
            self.log.info(f"{name} received, stopping.")
        self._stop.set()

    def run(self) -> int:
        """Everything from the lock to the last state write. Returns the exit code."""
        try:
            self._prepare_dirs()
        except OSError as ex:
            self.log.critical(f"Cannot create the daemon's directories: {ex}")
            return EXIT_FATAL
        try:
            release = self._locker(self.lock_path)
        except Exception as ex:
            self.log.critical(f"Cannot take the single-instance lock {self.lock_path}: {ex}")
            return EXIT_FATAL
        if release is None:
            self.log.info(f"Already running (another guardd holds {self.lock_path}); "
                          f"exiting.")
            return EXIT_OK
        try:
            return self._run_locked()
        finally:
            try:
                release()
            except Exception:
                pass

    def _run_locked(self) -> int:
        code = EXIT_FATAL
        try:
            self._publish(force=True)
            self._log_startup()
            self._run_startup_recovery()
            self._announce_last_shutdown()
            if self._per_start_allowed("started"):
                self._desktop("12VHPWR Guard", "Monitoring started.", stamp_key="started")
            self._notify_systemd("READY=1")
            code = self._loop()
        except Exception as ex:
            # A bug, not a sensor problem (those are handled inside the loop). Exit non-zero
            # and let systemd start a fresh process; the cleanup below still runs first.
            self.log.critical(f"Daemon error: {ex}", exc_info=True)
            code = EXIT_FATAL
        finally:
            self._cleanup(code)
        return code

    def _cleanup(self, code: int) -> None:
        self._notify_systemd("STOPPING=1")
        if self._backend is not None:
            try:
                self._backend.close()
            except Exception:
                pass
            self._backend = None

        if self._mitigator is not None:
            if self._shutting_down:
                # Logged by the shutdown sequence already. The card stays limited until the
                # power goes; the marker restores it at the next start if the reboot did not.
                pass
            else:
                if self._response.engaged:
                    self.log.warning("Stopping: releasing the GPU limit before exit.")
                # Windows Exit does the same: close() releases and verifies, then drops NVML.
                try:
                    self._mitigator.close()
                except Exception:
                    pass

        self.log.info("12VHPWR Guard stopped.")
        if self._shutting_down:
            pass    # SHUTDOWN stays on the state file until the power goes
        elif self._restarting:
            self._set_status(STATUS_ERROR, "Restarting to recover the sensor path")
            self._publish(force=True)
        elif code == EXIT_OK:
            self._set_status(STATUS_STOPPED, "Stopped")
            self._publish(force=True)
        else:
            self._set_status(STATUS_ERROR, "Daemon error, restarting")
            self._publish(force=True)
        self._queue.close(timeout=2.0)

    # -------------------------------------------------------------------- loop
    def _loop(self) -> int:
        while not self._stop.is_set():
            self._watchdog()
            self._retry_sticky()

            # (Re)connect whenever there is no live data source.
            if self._backend is None:
                if not self._try_connect():
                    if self._maybe_restart_for_sensor_loss():
                        return RESTART_EXIT_CODE
                    self._publish()
                    self._stop.wait(self.reconnect_retry)
                    continue
                self._on_connected()

            try:
                code = self._tick()
                if code is not None:
                    return code
            except TransientReadError as ex:
                # One bad sample. Skip the tick without disturbing any timer; the backend
                # escalates to SensorGoneError if it keeps happening.
                self.log.debug(f"Transient read error: {ex}")
                self._publish()
                self._stop.wait(self.poll_interval)
            except SensorGoneError as ex:
                self._on_sensor_lost(ex)
            except Exception as ex:
                self.log.error(f"Loop error: {ex}")
                self._set_status(STATUS_ERROR, f"Error: {ex}")
                self._publish()
                self._stop.wait(1.0)
        return EXIT_OK

    def _try_connect(self) -> bool:
        """Windows try_connect, for the one Linux backend."""
        self._set_status(
            STATUS_WAITING,
            "Sensor source lost. Waiting to reconnect..." if self._source_lost_at is not None
            else "Waiting for pin sensors...")

        if not self._source_missing_logged:
            self.log.info("Waiting for a pin-current source...")
            self._source_missing_logged = True

        candidate = None
        key = getattr(self._backend_factory, "key", AstralLinuxBackend.key)
        try:
            candidate = self._backend_factory()
            key = getattr(candidate, "key", key)
            description = candidate.connect()
        except Exception as ex:
            detail = f"{key}: {ex}"
            if candidate is not None:
                try:
                    candidate.close()
                except Exception:
                    pass
            if not self._source_missing_notified:
                self._source_missing_notified = True
                self._source_connected_notified = False
                if self._per_start_allowed("not_protected"):
                    self._sticky_set(
                        "not_protected", "12VHPWR Guard - not protected",
                        f"No pin sensor source, so the connector is not being watched. "
                        f"{detail}")
            # Once per missing period, and again only if the reason changes (i2c-dev
            # loaded, then a kernel driver found on the chip, say). The reason is what a
            # user needs to fix it, so it is logged, not only shown in a notification.
            if detail != self._last_missing_detail:
                self._last_missing_detail = detail
                self.log.warning(f"No sensor source available: {detail}")
            return False

        self._backend = candidate
        tag = getattr(candidate, "tag", key)
        self._source = f"{description} [{tag}]"
        self.log.info(f"Connected: {description} [{tag}]")
        if not self._source_connected_notified:
            if self._per_start_allowed("connected"):
                self._desktop("12VHPWR Guard", f"Monitoring active via {tag}.",
                              stamp_key="connected")
            self._source_connected_notified = True
        self._source_missing_notified = False
        self._source_missing_logged = False
        self._last_missing_detail = None
        degraded = getattr(candidate, "degraded", None)
        if degraded:
            self.log.warning(degraded)
        return True

    def _on_connected(self) -> None:
        """A new connection starts its timers from scratch (Windows, after try_connect)."""
        self._tracker.reset()
        self._pins_missing = False
        self._last_sample = None
        self._source_lost_at = None
        self._subsystem_id = getattr(self._backend, "subsystem_id", None)
        if getattr(self._backend, "key", "") == AstralLinuxBackend.key:
            self._direct_seen = True
        # The advisor and the state it drives are always cleared together, so a purple hold
        # can never outlive the episode that asked for it.
        self._advisor.reset()
        self._advisory = None
        self._sensor_path_fault = False
        self._sticky_clear("not_protected")
        self._sticky_clear("sensor_path")

    def _maybe_restart_for_sensor_loss(self) -> bool:
        """Windows maybe_restart_for_sensor_loss (F3): a fresh process gets a fresh driver
        session, so past the window the daemon exits 75 and systemd starts it again.

        Only for a process where the direct sensor actually connected at some point, so a
        machine that never had one keeps waiting rather than restart-looping.
        """
        restart_after = self.settings["sensor_loss_restart_seconds"]
        if restart_after <= 0 or not self._direct_seen or self._source_lost_at is None:
            return False
        if (self._clock() - self._source_lost_at) < restart_after:
            return False
        msg = (f"Restarting to recover the sensor path: no pin-current source for "
               f"{restart_after:g} s after the direct sensor was lost")
        self.log.error(msg)
        self._desktop("12VHPWR Guard - restarting",
                      f"No sensor for {_span_text(restart_after)}, restarting to recover it.",
                      critical=True)
        self._restarting = True
        return True

    def _on_sensor_lost(self, ex: Exception) -> None:
        """Windows SensorGoneError handling: ERROR, keep an engaged limit, close, and start
        the self-restart clock."""
        self.log.warning(f"Sensor source lost: {ex}")
        self._desktop("12VHPWR Guard", "Sensor source lost. Waiting to reconnect...",
                      critical=True)
        if self._tracker.over_start is not None:
            self.log.warning("Sustained over-threshold timer reset by sensor disconnect.")
        # The advisory timers go with it: a condition seen before a blind spell was not
        # observed across it.
        self._advisor.reset()
        self._advisory = None
        self._sensor_path_fault = False
        self._sticky_clear("sensor_path")
        if self._response.engaged:
            # Deliberately stays engaged. We limited the card because the current was
            # dangerous, and losing the ability to measure is not evidence that it stopped
            # being dangerous. It releases normally once readings come back and stay clean.
            self.log.warning("GPU stays limited while the sensor is gone; it will be "
                             "released once clean readings return.")
        self._set_status(STATUS_ERROR, "Sensor source lost")
        try:
            self._backend.close()
        except Exception:
            pass
        self._backend = None
        self._last_sample = None
        self._source = ""
        self._publish(force=True)
        # The guard is blind as of now.
        self._source_lost_at = self._clock()
        # Re-arm the one-shot notifications for the next outage/recovery.
        self._source_missing_notified = False
        self._source_missing_logged = False
        self._source_connected_notified = False
        self._last_missing_detail = None

    def _render_advisory(self, adv: Advisory) -> None:
        """Windows render_advisory: say one advisory. Nothing here touches the GPU."""
        if adv.kind == "cleared":
            self.log.info(adv.message)
            if adv.ended == "no_current":
                # The pins carry current again, so the readings mean something again and
                # the status may go back to OK on the next tick.
                self._sensor_path_fault = False
                self._sticky_clear("sensor_path")
            self._advisory = None
            return

        self.log.warning(adv.message)
        if adv.kind == "no_current":
            # Not a warning about the cable but about this tool: it is watching a wire
            # that carries nothing. The flag holds the status at ERROR until it clears.
            self._advisory = adv.short
            self._sensor_path_fault = True
            self._sticky_set("sensor_path", "12VHPWR Guard - not protected", adv.summary)
        else:
            self._advisory = adv.short
            self._desktop("12VHPWR Guard - advisory", adv.summary)
        if adv.first:
            # The minute before the first warning is the evidence: it is what an RMA claim
            # or a bug report needs, and the fault may not repeat on demand.
            self._flight.dump(self.log, "advisory")

    def _tick(self) -> Optional[int]:
        """One sample. Returns an exit code when the loop should end, else None."""
        pins = self._backend.read_pins()
        now = self._clock()

        # A long gap means the machine slept or badly stalled, not that current was high
        # the whole time. Never carry a sustained timer, or a limit decided on readings from
        # before the gap, across it.
        if self._last_sample is not None and (now - self._last_sample) > self.resume_grace:
            gap = now - self._last_sample
            self.log.warning(
                f"Sample gap {gap:.1f}s detected (suspend/resume?) - sustained timer reset."
            )
            self._tracker.reset()
            self._advisor.reset()
            self._advisory = None
            self._sensor_path_fault = False
            self._sticky_clear("sensor_path")
            self._source_lost_at = None
            if self._response.engaged:
                self._response.release(now, "suspend/resume gap", arm_retrigger=False)
            self._response.reset_timers()
        self._last_sample = now

        if not pins:
            if not self._pins_missing:
                self._pins_missing = True
                msg = "Pin sensors not found (name mismatch or sensors not active)."
                self.log.warning(msg)
                self._desktop("12VHPWR Guard", msg)
            self._set_status(STATUS_WAITING, "Sensors not found")
            self._publish()
            self._stop.wait(self.pins_missing_retry)
            return None
        if self._pins_missing:
            self._pins_missing = False
            msg = "Pin sensors found again. Monitoring resumed."
            self.log.info(msg)
            self._desktop("12VHPWR Guard", msg)

        self._flight.record(pins)

        # Warn only, and completely separate from the threshold logic below. The advisor
        # runs on every tick; the setting silences only the two cable advisories, never the
        # sensor-path check.
        for adv in self._advisor.evaluate(
            now, pins,
            board_power_w=self._board_power_watts(),
            advisories_enabled=self.settings["advisories_enabled"],
        ):
            self._render_advisory(adv)

        max_label, max_val, max_unit = max(pins, key=lambda x: x[1])
        self._max = (max_label, max_val)
        threshold = self.settings["threshold_amps"]
        sustained = self.settings["sustained_seconds_required"]

        branch = ThresholdTracker.classify(max_val, threshold)
        if branch == THRESHOLD_OVER:
            self._set_status(STATUS_OVER, f"Over: {max_label} {max_val:.2f}{max_unit}")
        elif branch == THRESHOLD_CLEARED:
            self._set_ok_status(f"OK (max {max_val:.2f}{max_unit})")
        else:
            self._set_ok_status(f"OK (near threshold: {max_val:.2f}{max_unit})")

        tick = self._tracker.update(now, max_label, max_val, max_unit, threshold, sustained)

        was_engaged = self._response.engaged
        shutdown_reason = self._response.evaluate(
            now=now, max_val=max_val, max_label=max_label, max_unit=max_unit,
            threshold=threshold, tier1_expired=tick.tier1_expired,
            tier1_reason=tick.tier1_reason,
        )

        if self._response.engaged:
            self._set_status(STATUS_MITIGATED, f"GPU limited: {max_label} {max_val:.2f}{max_unit}")
            if not was_engaged and not shutdown_reason:
                # The minute leading up to the engagement is the evidence. When engage and
                # shutdown land in the same tick, the shutdown dump has the same data.
                self._flight.dump(self.log, "engaged")

        if shutdown_reason:
            return self._emergency_shutdown(shutdown_reason)

        self._publish()
        self._stop.wait(self.poll_interval)
        return None

    # ---------------------------------------------------------- emergency shutdown
    def _hold(self, seconds: float) -> None:
        """Wait through the shutdown sequence in 1 s steps, still pinging the watchdog.

        Not the stop event: a SIGTERM must not cut these waits short (see request_stop). And
        5 + 20 s without a ping would be too close to WatchdogSec=30, and a watchdog kill
        here would restart the daemon, whose startup recovery would undo the limit.
        """
        remaining = float(seconds)
        while remaining > 0:
            step = min(1.0, remaining)
            self._sleep(step)
            remaining -= step
            self._watchdog()

    def _hold_for_force(self) -> bool:
        """The wait between the orderly and the forced poweroff. True means step aside.

        The orderly poweroff sends this process SIGTERM. If the GPU is already limited the
        card is safe however long the rest of the shutdown takes, so the daemon lets it run
        (returns True). If it is not limited (shutdown-only mode, or limiting failed) the
        full wait runs and the caller forces the poweroff: that is the case where the power
        has to go regardless of a unit that hangs the orderly stop.
        """
        remaining = float(self.force_after)
        while remaining > 0:
            if self._stop.is_set() and self._response.engaged:
                return True
            step = min(1.0, remaining)
            self._sleep(step)
            remaining -= step
            self._watchdog()
        return self._stop.is_set() and self._response.engaged

    def _run_poweroff(self, argv: List[str]) -> None:
        self.log.critical(f"Running: {' '.join(argv)}")
        try:
            self._runner(list(argv))
        except Exception as ex:
            self.log.error(f"{' '.join(argv)} failed: {ex}")

    def _emergency_shutdown(self, reason: str) -> int:
        """The Linux shutdown_windows, plus the force step Windows gets from /f.

        Note, log, notify (desktop and wall), leave the GPU limited, wait 5 s, orderly
        poweroff, and if this process is still alive 20 s later, a forced poweroff.
        """
        self._shutting_down = True
        try:
            self._set_status(STATUS_SHUTDOWN, "Shutting down...")
            self._publish(force=True)
            self._flight.dump(self.log, "shutdown")

            # The note first: it is the one thing that has to be on disk before the power
            # goes.
            self._note_shutdown_reason(reason)
            self.log.critical(f"SHUTDOWN TRIGGERED: {reason}")
            self._desktop("12VHPWR Guard - Shutdown", reason, critical=True)
            self._send_wall(f"12VHPWR Guard: emergency shutdown in "
                            f"{self.shutdown_delay:g} seconds. {reason}")
            if self._response.engaged:
                # Gated on engaged: a session that never engaged anything must not claim
                # a marker file that was never written.
                self.log.info("Leaving the GPU limited through shutdown; the marker file "
                              "will trigger a restore on the next start.")
        except Exception as ex:
            # Nothing before the power-off is allowed to stand in its way.
            self.log.error(f"Shutdown preparation failed ({ex}); powering off anyway.")

        self._hold(self.shutdown_delay)
        self._run_poweroff(POWEROFF_COMMAND)
        if self._hold_for_force():
            # The orderly poweroff is stopping this unit and the card is already at its
            # floor, so nothing is gained by holding the stop for the force step: staying
            # would only turn every guard-triggered shutdown into a forced one 20 s later.
            # The GPU stays limited (cleanup never releases while shutting down).
            self.log.info("Orderly poweroff under way and the GPU is limited; leaving the "
                          "rest of the shutdown to the system.")
            return EXIT_OK
        self.log.critical(f"Still running {self.force_after:g} s after an orderly poweroff "
                          f"was requested; forcing it.")
        self._run_poweroff(POWEROFF_FORCE_COMMAND)

        # Normally the power is gone before this line. If not, stay in the shutdown state
        # (the GPU stays limited, the marker stays) until stopped by hand, still answering
        # the watchdog: a restart here would run startup recovery and undo the limit.
        self.log.critical("The machine has not powered off. The guard stays in the shutdown "
                          "state; power the machine off by hand.")
        while not self._stop.wait(1.0):
            self._watchdog()
        return EXIT_OK


def _default_mitigator_factory(marker_path: str, logger: logging.Logger):
    """The real GpuMitigator with the platform default gpu_uv_config (the gpu-uv rule)."""
    if GpuMitigator is None:
        return None
    return GpuMitigator(marker_path=marker_path, logger=logger)


# =========================
# Entry point
# =========================
class _JournalFormatter(logging.Formatter):
    """'LEVEL | message', with a syslog priority prefix when stdout is the journal.

    systemd strips a leading <N> from each line it captures and files the line at that
    priority, so `journalctl -p warning` works. JOURNAL_STREAM is set exactly when stdout is
    connected to the journal; anywhere else the prefix would only be noise.
    """

    _PRIORITY = {logging.CRITICAL: 2, logging.ERROR: 3, logging.WARNING: 4,
                 logging.INFO: 6, logging.DEBUG: 7}

    def __init__(self, journal: bool):
        super().__init__("%(levelname)s | %(message)s")
        self._journal = journal

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if self._journal:
            text = f"<{self._PRIORITY.get(record.levelno, 6)}>{text}"
        return text


def setup_logging(stream=None) -> logging.Logger:
    logger = logging.getLogger("12vhpwr_guard")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler(stream or sys.stdout)
        handler.setFormatter(_JournalFormatter(bool(os.environ.get("JOURNAL_STREAM"))))
        logger.addHandler(handler)
    return logger


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="guardd", description="12VHPWR Guard daemon (run by systemd as root).")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    parser.add_argument("--var-dir", default=DEFAULT_VAR_DIR)
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    parser.add_argument("--version", action="version", version=f"%(prog)s {LINUX_APP_VERSION}")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logger = setup_logging()
    try:
        daemon = Daemon(settings_path=args.config, state_dir=args.state_dir,
                        var_dir=args.var_dir, log_dir=args.log_dir, logger=logger)
    except Exception as ex:
        logger.critical(f"Could not start: {ex}", exc_info=True)
        return EXIT_FATAL

    result: Dict[str, int] = {}

    def body() -> None:
        result["code"] = daemon.run()

    # The loop runs on a worker thread and the main thread only waits for it, so a signal
    # handler (always run on the main thread) can never interrupt the loop in the middle of
    # holding a lock that the handler then needs.
    worker = threading.Thread(target=body, name="guardd-loop", daemon=True)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, daemon.request_stop)
    worker.start()
    while worker.is_alive():
        worker.join(0.5)
    return result.get("code", EXIT_FATAL)


if __name__ == "__main__":
    sys.exit(main())

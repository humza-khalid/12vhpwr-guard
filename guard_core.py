"""The part of 12VHPWR Guard that decides, shared by every platform.

Everything here is pure Python and imports nothing platform specific, so the planned
Linux daemon can import it unchanged and make exactly the decisions the Windows guard
makes: same thresholds, same timings, same wording.

The rule that keeps it that way: nothing in this module toasts, writes to Event
Viewer, sleeps, or shuts anything down. Log lines still go to the logger the caller
hands in, because a log file exists on every platform. Everything else a decision
wants said goes out as a Notice through the notifier the caller hands in, and each
platform renders it its own way (Windows: Event Viewer, then a toast). Settings come
in through getters or values, never through another module's globals.

hwinfo_12vhpwr_guard.py re-exports every public name here, so `g.<name>` keeps
working for the tests and for anything else that reads it from there.
"""

import glob
import logging
import os
import sys
import time
from collections import deque
from typing import Callable, List, NamedTuple, Optional, Tuple

# =========================
# Defaults
# =========================
# Default values (used when config file doesn't exist or is invalid)
DEFAULT_THRESHOLD_AMPS = 9.5
DEFAULT_SUSTAINED_SECONDS_REQUIRED = 15.0

# =========================
# Tiered response (v1.1)
#
# Tier 1 is the pair above: elevated current, ridden out for a while before acting.
# Tiers 2 and 3 exist because some currents are too high to sit through a 15 second
# window. Spec load at 600 W is about 8.3 A per pin and ASUS's own warning fires at
# 9.2 A; documented slow-cook failures run 11-15 A, and the melted connector everyone
# has seen measured over 22 A on one wire.
# =========================
DEFAULT_CRITICAL_AMPS = 13.0
DEFAULT_CRITICAL_SECONDS = 3.0
DEFAULT_CATASTROPHIC_AMPS = 16.0

# Two samples rather than one: tier 3 skips every timer, so a single bad read must not
# be able to reach it. The plausibility filter rejects impossible values first.
CATASTROPHIC_SAMPLES_REQUIRED = 2

# How long the GPU slowdown gets to bring the current down before the guard stops
# waiting and shuts the machine off instead.
DEFAULT_MITIGATION_GRACE_SECONDS = 10.0
CRITICAL_GRACE_SECONDS = 5.0

# Currents fall the instant the clocks do, so releasing on current alone would flap.
# Mitigation holds for a minimum time and only lifts after a long clean stretch.
DEFAULT_MITIGATION_MIN_HOLD_SECONDS = 120.0
DEFAULT_MITIGATION_CLEAR_SECONDS = 60.0

# A fault that returns this soon after a release is not a transient, and riding it out
# a second time is not worth the risk.
DEFAULT_RETRIGGER_WINDOW_SECONDS = 600.0

# tiered: slow the GPU down first, shut down only if that does not fix it.
# shutdown_only: v1.0 behaviour, with the tier timings still applied.
RESPONSE_TIERED = "tiered"
RESPONSE_SHUTDOWN_ONLY = "shutdown_only"
VALID_RESPONSE_MODES = (RESPONSE_TIERED, RESPONSE_SHUTDOWN_ONLY)
DEFAULT_RESPONSE_MODE = RESPONSE_TIERED

DEFAULT_TIER_VALUES = {
    "critical_amps": DEFAULT_CRITICAL_AMPS,
    "critical_seconds": DEFAULT_CRITICAL_SECONDS,
    "catastrophic_amps": DEFAULT_CATASTROPHIC_AMPS,
    "mitigation_grace_seconds": DEFAULT_MITIGATION_GRACE_SECONDS,
    "mitigation_clear_seconds": DEFAULT_MITIGATION_CLEAR_SECONDS,
    "mitigation_min_hold_seconds": DEFAULT_MITIGATION_MIN_HOLD_SECONDS,
    "retrigger_window_seconds": DEFAULT_RETRIGGER_WINDOW_SECONDS,
}

# =========================
# Pin advisories (v1.4, warn only)
#
# The tiers above act on absolute per-pin current, which is what melts a connector.
# That leaves them blind to a fault which lowers every reading: at a 540 W limit one
# open pin puts 44.8 A through the surviving five, 8.96 A each, under the 9.5 A trip.
# Nothing is over threshold and the connector is in trouble. The advisories read the
# spread between the pins instead, and only ever talk: they never limit the GPU and
# never shut anything down.
# =========================
DEFAULT_ADVISORIES_ENABLED = True

# About 215 W through the connector. Below it the numbers are too small to tell a
# fault from sensor noise, so nothing can start.
DEFAULT_ADVISORY_LOAD_AMPS = 18.0
DEFAULT_DEAD_PIN_MAX_AMPS = 0.5
DEFAULT_IMBALANCE_SPREAD_AMPS = 3.0
DEFAULT_ADVISORY_SUSTAIN_SECONDS = 10.0
DEFAULT_ADVISORY_REPEAT_SECONDS = 600.0

# Not config keys: these qualify a rule rather than tune it. The others have to be
# carrying real current before one quiet pin means anything, a spread only matters
# once the busiest pin is working, and a condition that has been gone for a minute
# is over.
DEAD_PIN_OTHERS_MIN_AMPS = 3.0
IMBALANCE_MIN_MAX_AMPS = 5.0
ADVISORY_CLEAR_SEC = 60.0

# =========================
# Sensor-path check (v1.4)
#
# A BTF card in an actual BTF motherboard is powered through the GC-HPWR slot, so
# nothing at all flows through the 16-pin cable and the card-side pin sensors read
# zero however hard the GPU is working. Every rule above would then be measuring a
# wire that carries no current, and green would be a lie. A dead or unpowered sensor
# block produces the same readings and deserves the same answer, so the check is
# about what the sensors report, not about why.
#
# Not config keys either: a connector that is carrying nothing is not a tuning
# question, and there is no number here a user could usefully move.
# =========================
# Below this a pin is carrying nothing. A cabled Astral idles at roughly 0.5 to 1.0 A
# per pin, because the card takes nearly everything through the 16-pin and almost
# nothing from the slot, so a cabled card cannot look like this.
NO_CURRENT_AMPS = 0.1
NO_CURRENT_SUSTAIN_SEC = 60.0

# The optional half of the check (tiered mode only, where NVML is already bound): the
# card reports real board power while the pins do not add up to anything like it.
# The PCIe slot supplies at most 75 W, so at 150 W at least 75 W has to cross the
# connector, which is 6.25 A at 12 V, while 25 % of the current implied by 150 W is
# only 3.1 A. A correctly sensed cable cannot get under the ratio, so the margin is
# wide in the direction that matters (no false alarm on a healthy card).
MISMATCH_MIN_WATTS = 150.0
MISMATCH_RATIO = 0.25
MISMATCH_SUSTAIN_SEC = 30.0

# Event Viewer ids for the three kinds the advisor can raise. All three are warnings;
# the "cleared" line that ends any of them is 7004, information.
ADVISORY_EVENT_IDS = {"imbalance": 7001, "dead_pin": 7002, "no_current": 7003}

DEFAULT_ADVISORY_VALUES = {
    "advisories_enabled": DEFAULT_ADVISORIES_ENABLED,
    "advisory_load_amps": DEFAULT_ADVISORY_LOAD_AMPS,
    "dead_pin_max_amps": DEFAULT_DEAD_PIN_MAX_AMPS,
    "imbalance_spread_amps": DEFAULT_IMBALANCE_SPREAD_AMPS,
    "advisory_sustain_seconds": DEFAULT_ADVISORY_SUSTAIN_SECONDS,
    "advisory_repeat_seconds": DEFAULT_ADVISORY_REPEAT_SECONDS,
}

# =========================
# Tier 1 sustained timer
# =========================
CONSECUTIVE_SAMPLES_REQUIRED = 1

# While over threshold, log progress every N seconds (set 0 to disable)
SUSTAINED_PROGRESS_LOG_EVERY_SEC = 5.0

# Hysteresis / clear
CLEAR_HYSTERESIS_AMPS = 0.2
CLEAR_STABLE_SECONDS = 1.0

# =========================
# Flight recorder
# =========================
# Flight recorder: the last ~minute of per-pin samples, kept in memory only and
# written to the log directory when the guard acts. The dump is the evidence for a
# bug report or an RMA claim; normal operation never touches the disk for it.
FLIGHT_SAMPLES = 120  # 60 s at the 0.5 s poll interval
FLIGHT_KEEP_FILES = 10


# =========================
# Notices
# =========================
NOTICE_INFO = "info"
NOTICE_WARNING = "warning"
NOTICE_ERROR = "error"


class Notice(NamedTuple):
    """Something a decision wants said beyond the log line. The platform says it.

    The log line itself is not part of this: the core writes it straight to the
    logger it was given, before or after the notice exactly as the guard always has,
    so the order a reader sees in the log, Event Viewer and toasts does not change.
    """

    # "info", "warning" or "error". Windows maps these onto the three Event Viewer
    # entry types; another platform picks its own urgency from it.
    level: str
    # Windows Event Viewer id. None for a notice that has no event of its own.
    event_id: Optional[int]
    # The full text, as written to Event Viewer.
    message: str
    # (title, message) for a desktop notification, None when this one stays quiet.
    toast: Optional[Tuple[str, str]] = None


# A notifier takes one Notice and renders it. The default says nothing, which is what
# a caller that only wants the decisions (a test, a dry run) needs.
Notifier = Callable[[Notice], None]


def no_notify(_notice: Notice) -> None:
    """The notifier used when none is passed in."""


class FlightRecorder:
    """The last ~minute of per-pin samples, written to disk only when the guard acts.

    Memory only in normal operation, so the lightweight promise holds. The dump is
    what a bug report or an RMA claim needs: what every pin was doing in the sixty
    seconds before the guard limited the GPU or shut the machine down.

    log_dir is where dumps go and keep_files how many survive. Both are read through
    properties at dump time, so a platform binding can point them at a setting that
    may change after the recorder was built (the Windows guard's LOG_DIR, which the
    tests redirect).
    """

    def __init__(self, log_dir: str, maxlen: int = FLIGHT_SAMPLES,
                 keep_files: int = FLIGHT_KEEP_FILES):
        self._buf = deque(maxlen=maxlen)
        self._log_dir = log_dir
        self._keep_files = keep_files

    @property
    def log_dir(self) -> str:
        return self._log_dir

    @property
    def keep_files(self) -> int:
        return self._keep_files

    def record(self, pins) -> None:
        self._buf.append((time.time(), [round(a, 3) for _label, a, _unit in pins]))

    def dump(self, logger: logging.Logger, event: str) -> Optional[str]:
        """Write the buffer to <log_dir>/flight_<stamp>_<event>.csv. Returns the path."""
        if not self._buf:
            return None
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.log_dir, f"flight_{stamp}_{event}.csv")
        try:
            first_len = len(self._buf[0][1])
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write("time," + ",".join(f"pin{i + 1}" for i in range(first_len)) + "\n")
                for ts, vals in self._buf:
                    iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
                    f.write(f"{iso}.{int(ts % 1 * 1000):03d},"
                            + ",".join(f"{v:.3f}" for v in vals) + "\n")
            logger.info(f"Flight recorder: {len(self._buf)} samples written to {path}")
        except Exception as ex:
            logger.warning(f"Flight recorder: could not write dump ({ex})")
            return None
        self._prune()
        return path

    def _prune(self) -> None:
        """Keep the newest keep_files dumps; the stamp sorts chronologically."""
        try:
            flights = sorted(glob.glob(os.path.join(self.log_dir, "flight_*.csv")))
            for old in flights[:-self.keep_files]:
                os.remove(old)
        except Exception:
            pass


class Advisory(NamedTuple):
    """One thing the advisor wants said. The caller decides how to say it."""

    # "dead_pin", "imbalance", "no_current" or "cleared".
    kind: str
    # 1-based pin the advisory is about (the quiet one, or the busiest one). None
    # when the advisory is about all of them at once.
    pin: Optional[int]
    # Full line for a log file or Event Viewer: numbers and what to do about them.
    message: str
    # One short line, for a toast.
    summary: str
    # Tooltip fragment, a few words. Empty for "cleared".
    short: str
    # True for the first warning of an episode, False for a repeat. A "cleared"
    # advisory is not a warning, so it is never first.
    first: bool
    # On a "cleared" advisory, the kind of episode that just ended, so the caller can
    # undo exactly what it did for that kind. None on a warning.
    ended: Optional[str] = None


class PinAdvisor:
    """Warn-only reading of the six pins. It never mitigates and never stops.

    Two jobs, both of which the absolute per-pin tiers are blind to.

    The shape of the readings. An open pin moves its load to the pins that are left,
    and if the card is not near its limit no single pin has to go over threshold for
    the connector to be in trouble. This looks at the spread rather than the biggest
    reading. Those two kinds are advisories in the ordinary sense and the caller can
    switch them off.

    The sensor path itself. If every pin reads nothing, the guard is watching a wire
    that carries no current (slot power on a BTF board, or a dead sensor block) and
    nothing above this line means anything. That is not an opinion about the cable,
    it is the guard admitting it cannot see the load, so it is not something the
    advisories setting can silence.

    Deliberately platform neutral: pure logic, no toast, no Event Viewer, no shared
    state, no logger, no clock of its own (every decision uses the `now` handed in).
    The caller renders whatever evaluate() returns, which is what lets the planned
    Linux daemon import this class unchanged.

    constants is where the fixed numbers below (ADVISORY_CLEAR_SEC, the sensor-path
    thresholds and windows) are looked up, at call time on every tick. It defaults
    to this module. The Windows guard passes its own module, which re-exports the
    same names, so a value patched there (the tests compress the sensor-path windows
    that way) still applies the moment it is set, exactly as it did when this class
    lived in that file.
    """

    def __init__(self, values: Optional[dict] = None, constants=None):
        self._c = constants if constants is not None else sys.modules[__name__]
        merged = dict(DEFAULT_ADVISORY_VALUES)
        if values:
            merged.update(values)
        self.load_amps = float(merged["advisory_load_amps"])
        self.dead_pin_max_amps = float(merged["dead_pin_max_amps"])
        self.imbalance_spread_amps = float(merged["imbalance_spread_amps"])
        self.sustain_seconds = float(merged["advisory_sustain_seconds"])
        self.repeat_seconds = float(merged["advisory_repeat_seconds"])
        self.reset()

    def reset(self) -> None:
        """Forget everything. Called whenever the caller's own timers reset.

        A gap in the readings (sensor loss, suspend, a pause) means the half-formed
        condition on the other side of it was never observed for as long as it looks.
        """
        # The condition seen on the last tick, and when its unbroken run started.
        self._kind: Optional[str] = None
        self._since: Optional[float] = None
        # The episode already reported, if any, and when it was last reported.
        self._episode: Optional[str] = None
        self._episode_pin: Optional[int] = None
        self._last_report = 0.0
        # When the reported episode's condition went away, for the clear timer.
        self._absent_since: Optional[float] = None

    def evaluate(self, now: float, pins, board_power_w: Optional[float] = None,
                 advisories_enabled: bool = True) -> List[Advisory]:
        """One tick. Returns what should be said this time, usually nothing.

        pins is the loop's [(label, amps, unit)] list.

        board_power_w is what the card says it is drawing, when the caller can ask
        (NVML in tiered mode on Windows). None leaves the power half of the
        sensor-path check inert, and every other rule works exactly the same.

        advisories_enabled is the user's `advisories` setting, passed per call rather
        than read from the settings this class was built with, because it gates only
        the two cable advisories. The sensor-path check runs either way: a guard that
        cannot see the load has to say so whatever the user asked to be told about.
        """
        out: List[Advisory] = []
        kind, rule, pin, message, summary, short = self._classify(
            pins, board_power_w, advisories_enabled)

        if kind is None:
            self._kind = None
            self._since = None
            if self._episode is not None:
                if self._absent_since is None:
                    self._absent_since = now
                elif (now - self._absent_since) >= self._c.ADVISORY_CLEAR_SEC:
                    out.append(self._close_episode())
            return out

        # A different fault than the one being reported is a new episode. The old one
        # is over whether or not it had the full clear window to end quietly.
        if self._episode is not None and self._episode != kind:
            out.append(self._close_episode())

        if kind != self._kind:
            self._kind = kind
            self._since = now
        self._absent_since = None

        if self._episode is None:
            if (now - self._since) >= self._sustain_for(rule):
                self._episode = kind
                self._episode_pin = pin
                self._last_report = now
                out.append(Advisory(kind, pin, message, summary, short, True))
        elif self.repeat_seconds > 0 and (now - self._last_report) >= self.repeat_seconds:
            self._episode_pin = pin
            self._last_report = now
            out.append(Advisory(kind, pin, message, summary, short, False))
        return out

    # ---- internals ----
    def _sustain_for(self, rule: Optional[str]) -> float:
        """How long this particular condition has to hold before it is worth saying.

        The two cable advisories share the configured sustain. The sensor-path rules
        have their own: no current at all is a slower, quieter claim and gets a full
        minute, while a power reading that contradicts the pins is already specific
        and only needs half of that. Read from the constants namespace at call time so
        the numbers stay in one place.
        """
        return {
            "no_current_zero": self._c.NO_CURRENT_SUSTAIN_SEC,
            "no_current_power": self._c.MISMATCH_SUSTAIN_SEC,
        }.get(rule, self.sustain_seconds)

    def _close_episode(self) -> Advisory:
        """End the reported episode and produce the one line that says so."""
        ended = self._episode
        pin = self._episode_pin
        if ended == "no_current":
            message = "Sensor path cleared: the pins carry current again."
        else:
            name = "dead pin" if ended == "dead_pin" else "imbalance"
            where = f" on pin {pin}" if pin else ""
            message = f"Pin advisory cleared: the {name} condition{where} is gone."
        self._episode = None
        self._episode_pin = None
        self._last_report = 0.0
        self._absent_since = None
        return Advisory("cleared", pin, message, message, "", False, ended)

    def _classify(self, pins, board_power_w: Optional[float] = None,
                  advisories_enabled: bool = True):
        """(kind, rule, pin, message, summary, short), kind None if clean.

        `rule` names the exact condition inside the kind, because the two
        sensor-path rules share one kind (and so one episode, one clear) while
        waiting different lengths of time.
        """
        nothing = (None, None, None, "", "", "")
        try:
            amps = [float(a) for _label, a, _unit in pins]
        except Exception:
            return nothing
        if len(amps) < 2:
            return nothing

        # The sensor path first, and deliberately above the load gate: the gate is
        # computed from the very readings that are missing, so gating this rule would
        # be asking a broken sensor for permission to report itself. Zeros are the
        # whole signal here.
        sensor_path = self._classify_sensor_path(amps, board_power_w)
        if sensor_path is not None:
            return sensor_path

        # Everything below is an advisory about the cable, which the user can switch
        # off. Nothing above is.
        if not advisories_enabled:
            return nothing

        # Idle noise is not a fault. Nothing can start below the load gate, and an
        # episode that is running is treated as absent, which feeds its clear timer.
        if sum(amps) < self.load_amps:
            return nothing

        # Dead pin first: it is the more specific fault, and a dead pin usually
        # produces a large spread as well.
        dead = [i for i, a in enumerate(amps) if a <= self.dead_pin_max_amps]
        if dead:
            low = min(dead, key=lambda i: amps[i])
            others = [a for j, a in enumerate(amps) if j != low]
            mean_others = sum(others) / len(others)
            if mean_others >= self._c.DEAD_PIN_OTHERS_MIN_AMPS:
                pin = low + 1
                extra = f" ({len(dead)} pins read near zero)" if len(dead) > 1 else ""
                summary = (f"Pin {pin} reads {amps[low]:.2f} A while the others average "
                           f"{mean_others:.2f} A{extra}.")
                message = (f"Pin advisory: pin {pin} reads {amps[low]:.2f} A while the "
                           f"other {len(others)} average {mean_others:.2f} A{extra}. That "
                           f"pin or its contact looks open, so the load is going through "
                           f"the rest. Reseat the cable at both ends and check the "
                           f"connector.")
                return ("dead_pin", "dead_pin", pin, message, summary,
                        f"pin {pin} near zero")

        hi = max(range(len(amps)), key=lambda i: amps[i])
        lo = min(range(len(amps)), key=lambda i: amps[i])
        spread = amps[hi] - amps[lo]
        if (spread >= self.imbalance_spread_amps
                and amps[hi] >= self._c.IMBALANCE_MIN_MAX_AMPS):
            pin = hi + 1
            summary = (f"Pin {pin} carries {amps[hi]:.2f} A, pin {lo + 1} carries "
                       f"{amps[lo]:.2f} A (spread {spread:.2f} A).")
            message = (f"Pin advisory: pin {pin} carries {amps[hi]:.2f} A while pin "
                       f"{lo + 1} carries {amps[lo]:.2f} A, a spread of {spread:.2f} A "
                       f"across the connector. Uneven contact resistance puts the heat "
                       f"on the busiest pin. Reseat the cable at both ends and check the "
                       f"connector.")
            return ("imbalance", "imbalance", pin, message, summary,
                    f"pin {pin} high, spread {spread:.1f}A")

        return nothing

    def _classify_sensor_path(self, amps, board_power_w: Optional[float]):
        """The sensor-path condition for this sample, or None when the pins look real.

        Two ways to reach the same conclusion, so they share the "no_current" kind and
        with it one episode and one cleared line. When both are true the power wording
        wins, because naming the two numbers is a stronger thing to show someone than
        saying the pins read nothing.
        """
        total = sum(amps)

        # The power half. Inert without a reading, and only above a load where the
        # slot alone cannot be the explanation.
        if board_power_w is not None and board_power_w >= self._c.MISMATCH_MIN_WATTS:
            expected = board_power_w / 12.0
            if total < (self._c.MISMATCH_RATIO * expected):
                message = (f"Sensor path: the card draws {board_power_w:.0f} W but the "
                           f"pins add up to {total:.1f} A (expected about "
                           f"{expected:.0f} A). The pin sensors are not on the load "
                           f"path, so this tool cannot protect the card. On a BTF "
                           f"motherboard with slot (GC-HPWR) power this is expected: "
                           f"the load bypasses the card-side sensors. See the BTF note "
                           f"in the README.")
                summary = (f"The card draws {board_power_w:.0f} W but the pins add up to "
                           f"{total:.1f} A. This tool cannot protect the card in this "
                           f"setup.")
                short = f"pins carry {total:.1f}A of about {expected:.0f}A, not protected"
                return "no_current", "no_current_power", None, message, summary, short

        # The core half, which needs nothing but the pins and so runs in every
        # response mode, on every platform.
        if all(a <= self._c.NO_CURRENT_AMPS for a in amps):
            message = (f"Sensor path: all {len(amps)} pins read "
                       f"{self._c.NO_CURRENT_AMPS:.2f} A or less for "
                       f"{self._c.NO_CURRENT_SUSTAIN_SEC:.0f} s. The pin sensors see "
                       f"no current, so this tool cannot protect the card. On a BTF "
                       f"motherboard with slot (GC-HPWR) power this is expected: the "
                       f"load bypasses the card-side sensors. See the BTF note in the "
                       f"README.")
            summary = ("The pin sensors see no current. This tool cannot protect the "
                       "card in this setup.")
            return ("no_current", "no_current_zero", None, message, summary,
                    "no current on any pin, not protected")

        return None


# Which side of the threshold a sample is on. "near" is the hysteresis band just
# under the threshold: not over, but not clear enough to start the clear timer.
THRESHOLD_OVER = "over"
THRESHOLD_CLEARED = "cleared"
THRESHOLD_NEAR = "near"


class ThresholdTick(NamedTuple):
    """What one ThresholdTracker.update() decided, for the loop to act on."""

    # THRESHOLD_OVER, THRESHOLD_CLEARED or THRESHOLD_NEAR.
    branch: str
    # True once the sustained window has run out. Consumed by TieredResponse.evaluate.
    tier1_expired: bool
    # Why, in the words the shutdown reason and the log use. "" when not expired.
    tier1_reason: str


class ThresholdTracker:
    """Tier 1: the sustained over-threshold timer.

    Elevated current is ridden out for the configured window before anything acts,
    because a pin can spike past the threshold for a moment under a normal load. The
    timer starts on the first sample over threshold, and only stops once the current
    has been clear of the threshold (less the hysteresis) for CLEAR_STABLE_SECONDS: a
    sample in the band just under the threshold neither counts as over nor counts
    towards clearing (it restarts the clear timer).

    What happens when the window runs out is not decided here. update() reports it
    and TieredResponse decides whether that costs a GPU slowdown or the machine.

    No clock of its own: every decision uses the `now` handed in, so tests can drive
    it with any timeline. The threshold and window come in per call because the tray
    can change them at runtime and the next tick has to use the new values.
    """

    def __init__(self, logger: logging.Logger, notify: Optional[Notifier] = None):
        self.log = logger
        self._notify = notify or no_notify
        self.reset()

    def reset(self) -> None:
        """Drop the timer. A new connection or a suspend/resume gap starts from scratch."""
        self.over_start: Optional[float] = None
        self.consecutive_over = 0
        self.clear_start: Optional[float] = None
        self.last_over_notice = 0.0

    @staticmethod
    def classify(max_val: float, threshold: float) -> str:
        """Which side of the threshold this sample is on. Pure, no state touched.

        Public so the loop can set its status before update() says anything, which
        keeps the tray state ahead of the log line and toast as it always was.
        """
        if max_val >= threshold:
            return THRESHOLD_OVER
        if max_val < (threshold - CLEAR_HYSTERESIS_AMPS):
            return THRESHOLD_CLEARED
        return THRESHOLD_NEAR

    def update(self, now: float, max_label: str, max_val: float, max_unit: str,
               threshold: float, sustained_seconds_required: float) -> ThresholdTick:
        """One tick with the hottest pin's reading."""
        branch = self.classify(max_val, threshold)
        tier1_expired = False
        tier1_reason = ""

        if branch == THRESHOLD_OVER:
            self.consecutive_over += 1
            self.clear_start = None

            if self.over_start is None:
                self.over_start = now
                self.consecutive_over = 1
                self.last_over_notice = 0.0
                msg = (
                    f"Threshold exceeded: {max_label}={max_val:.2f}{max_unit} >= {threshold:.2f}A. "
                    f"Starting {sustained_seconds_required:.0f}s sustained timer."
                )
                self.log.warning(msg)
                self._notify(Notice(NOTICE_WARNING, 3001, msg,
                                    ("12VHPWR Guard - Warning", msg)))

            if (SUSTAINED_PROGRESS_LOG_EVERY_SEC
                    and (now - self.last_over_notice) >= SUSTAINED_PROGRESS_LOG_EVERY_SEC):
                sustained = now - self.over_start
                msg = (
                    f"Still over threshold for {sustained:.1f}s: {max_label}={max_val:.2f}{max_unit} "
                    f"(needs {sustained_seconds_required:.1f}s)"
                )
                self.log.warning(msg)
                self._notify(Notice(NOTICE_WARNING, 3002, msg))
                self.last_over_notice = now

            if self.consecutive_over >= CONSECUTIVE_SAMPLES_REQUIRED:
                sustained = now - self.over_start
                if sustained >= sustained_seconds_required:
                    # What happens next is the response ladder's call: limit the GPU
                    # first if it can, shut down if it cannot or if limiting does not
                    # bring the current back down.
                    tier1_expired = True
                    tier1_reason = (
                        f"sustained {sustained:.1f}s >= {sustained_seconds_required:.1f}s, "
                        f"{max_label}={max_val:.2f}{max_unit} >= {threshold:.2f}A"
                    )

        elif branch == THRESHOLD_CLEARED:
            self.consecutive_over = 0

            if self.clear_start is None:
                self.clear_start = now
            elif (now - self.clear_start) >= CLEAR_STABLE_SECONDS:
                if self.over_start is not None:
                    msg = (
                        f"Load cleared below {threshold - CLEAR_HYSTERESIS_AMPS:.2f}A for "
                        f"{CLEAR_STABLE_SECONDS:.1f}s. Timer reset."
                    )
                    self.log.info(msg)
                    self._notify(Notice(NOTICE_INFO, 4001, msg,
                                        ("12VHPWR Guard", "Load cleared. Timer reset.")))
                self.over_start = None
                self.last_over_notice = 0.0

        else:
            self.consecutive_over = 0
            self.clear_start = None

        return ThresholdTick(branch, tier1_expired, tier1_reason)


class TieredResponse:
    """Everything that happens once a pin crosses a tier threshold.

    Tier 1 detection is ThresholdTracker's job, the sustained timer above. This owns
    the response to it: slow the GPU down, give that a grace window to work,
    hold it long enough not to flap, release when the current has been clean for a
    while, and escalate to shutdown whenever any of that is not enough.

    evaluate() never shuts the machine down itself. It returns a reason and the caller
    acts on it, which is what lets the tests drive the whole ladder with the shutdown
    stubbed out.

    get_tier_values and get_response_mode are called on every tick, never cached,
    because the tray changes both at runtime and the next tick has to see it. Left
    out they mean the defaults. notify renders the Event Viewer entries and toasts
    (see Notice); log lines go straight to logger.
    """

    def __init__(self, get_mitigator, logger: logging.Logger,
                 get_tier_values: Optional[Callable[[], dict]] = None,
                 get_response_mode: Optional[Callable[[], str]] = None,
                 notify: Optional[Notifier] = None):
        self._get_mitigator = get_mitigator
        self.log = logger
        self._get_tier_values = get_tier_values or (lambda: dict(DEFAULT_TIER_VALUES))
        self._get_response_mode = get_response_mode or (lambda: DEFAULT_RESPONSE_MODE)
        self._notify = notify or no_notify

        self.engaged = False
        self.engaged_at: Optional[float] = None
        self.engaged_tier: Optional[int] = None
        self.grace_deadline: Optional[float] = None
        self.clear_since: Optional[float] = None
        self.released_at: Optional[float] = None
        self.critical_since: Optional[float] = None
        self.catastrophic_samples = 0
        self._unavailable_warned = False

    # -- mode ---------------------------------------------------------------
    def mitigator(self):
        return self._get_mitigator() if self._get_mitigator else None

    def can_mitigate(self) -> bool:
        m = self.mitigator()
        return m is not None and m.is_available

    def unavailable_reason(self) -> str:
        m = self.mitigator()
        if m is None:
            return "mitigation module unavailable"
        return m.unavailable_reason or "unknown"

    def effective_mode(self) -> str:
        """What will actually happen, which is not always what the setting says."""
        mode = self._get_response_mode()
        if mode != RESPONSE_TIERED:
            return mode
        if self.can_mitigate():
            return RESPONSE_TIERED

        if not self._unavailable_warned:
            self._unavailable_warned = True
            reason = self.unavailable_reason()
            msg = (f"Tiered response is selected but the GPU cannot be limited "
                   f"({reason}). Running shutdown-only until that changes.")
            self.log.warning(msg)
            self._notify(Notice(NOTICE_WARNING, 6004, msg, (
                "12VHPWR Guard", f"GPU limiting unavailable ({reason}). Shutdown-only.")))
        return RESPONSE_SHUTDOWN_ONLY

    # -- the ladder ---------------------------------------------------------
    def evaluate(self, now: float, max_val: float, max_label: str, max_unit: str,
                 threshold: float, tier1_expired: bool,
                 tier1_reason: str = "") -> Optional[str]:
        """One tick. Returns a shutdown reason, or None to keep running."""
        tiers = self._get_tier_values()
        mode = self.effective_mode()

        # Switching to shutdown-only while the GPU is limited gives it straight back.
        # Same rule as pausing: the user has taken the decision away from us.
        if self.engaged and mode == RESPONSE_SHUTDOWN_ONLY:
            self.release(now, "response mode switched to shutdown-only", arm_retrigger=False)

        # --- tier 3: catastrophic, no timers ------------------------------
        if max_val >= tiers["catastrophic_amps"]:
            self.catastrophic_samples += 1
        else:
            self.catastrophic_samples = 0

        if self.catastrophic_samples >= CATASTROPHIC_SAMPLES_REQUIRED:
            detail = (f"{max_label}={max_val:.2f}{max_unit} >= "
                      f"{tiers['catastrophic_amps']:.2f}A on "
                      f"{CATASTROPHIC_SAMPLES_REQUIRED} consecutive samples")
            note = "shutting down immediately"
            if mode == RESPONSE_TIERED and self._engage(now, 3, detail, grace=None):
                note = "GPU limited and shutting down immediately"
            return f"Tier 3 (catastrophic): {detail}. {note}."

        # --- tier 2: critical, short window -------------------------------
        if max_val >= tiers["critical_amps"]:
            if self.critical_since is None:
                self.critical_since = now
                msg = (f"Critical current: {max_label}={max_val:.2f}{max_unit} >= "
                       f"{tiers['critical_amps']:.2f}A. Responding in "
                       f"{tiers['critical_seconds']:.0f}s if it holds.")
                self.log.warning(msg)
                self._notify(Notice(NOTICE_WARNING, 6001, msg))
            elif (now - self.critical_since) >= tiers["critical_seconds"]:
                detail = (f"{max_label}={max_val:.2f}{max_unit} >= "
                          f"{tiers['critical_amps']:.2f}A sustained "
                          f"{now - self.critical_since:.1f}s")
                reason = self._trigger(now, 2, detail, CRITICAL_GRACE_SECONDS, mode, tiers)
                if reason:
                    return reason
        else:
            self.critical_since = None

        # --- tier 1: the existing sustained window ------------------------
        if tier1_expired:
            reason = self._trigger(now, 1, tier1_reason,
                                   tiers["mitigation_grace_seconds"], mode, tiers)
            if reason:
                return reason

        # --- living with an engaged mitigation ----------------------------
        if self.engaged:
            return self._while_mitigated(now, max_val, max_label, max_unit, threshold, tiers)
        return None

    def _trigger(self, now: float, tier: int, detail: str, grace: float,
                 mode: str, tiers: dict) -> Optional[str]:
        """A tier's condition has been met. Returns a shutdown reason or None."""
        # A fault that comes back this soon after a release is persistent. Riding it
        # out worked last time and it came back anyway, so stop riding it out.
        if (self.released_at is not None
                and (now - self.released_at) <= tiers["retrigger_window_seconds"]):
            since = now - self.released_at
            note = "shutting down"
            if mode == RESPONSE_TIERED and self._engage(now, tier, detail, grace=None):
                note = "GPU limited and shutting down"
            return (f"Tier {tier} re-triggered {since:.0f}s after the last release "
                    f"(within the {tiers['retrigger_window_seconds']:.0f}s window): "
                    f"{detail}. Persistent fault, {note}.")

        if mode == RESPONSE_SHUTDOWN_ONLY:
            if self._get_response_mode() == RESPONSE_SHUTDOWN_ONLY:
                why = "response mode is shutdown-only"
            else:
                why = f"GPU limiting unavailable ({self.unavailable_reason()})"
            return f"Tier {tier}: {detail}. No mitigation attempted, {why}."

        if not self._engage(now, tier, detail, grace):
            return (f"Tier {tier}: {detail}. Tried to limit the GPU and failed "
                    f"({self.unavailable_reason()}), shutting down instead.")
        return None

    def _engage(self, now: float, tier: int, detail: str, grace: Optional[float]) -> bool:
        """Apply the slowdown. Idempotent: an existing grace window keeps running."""
        if self.engaged:
            return True

        m = self.mitigator()
        if m is None or not m.engage(f"tier {tier}: {detail}"):
            return False

        self.engaged = True
        self.engaged_at = now
        self.engaged_tier = tier
        self.grace_deadline = (now + grace) if grace else None
        self.clear_since = None

        msg = (f"Tier {tier}: {detail}. GPU limited to its floor clock and minimum "
               f"power limit")
        if grace:
            msg += f"; shutting down if the current is still high in {grace:.0f}s"
        self.log.warning(msg)
        self._notify(Notice(NOTICE_WARNING, 6002, msg, (
            "12VHPWR Guard - GPU limited",
            f"Tier {tier} overcurrent. The GPU has been slowed down to protect the "
            f"connector. Save your work and check the cable.")))
        return True

    def _while_mitigated(self, now: float, max_val: float, max_label: str, max_unit: str,
                         threshold: float, tiers: dict) -> Optional[str]:
        # Did the slowdown actually work? This is the question the grace window exists
        # to answer: current this high at floor clocks means the contact is bad enough
        # that no clock speed is safe.
        if self.grace_deadline is not None and now >= self.grace_deadline:
            if max_val >= threshold:
                return (f"Tier {self.engaged_tier}: GPU was limited but "
                        f"{max_label}={max_val:.2f}{max_unit} is still >= "
                        f"{threshold:.2f}A. Limiting the card did not fix it.")
            self.grace_deadline = None
            msg = (f"GPU limit brought the current down to {max_val:.2f}{max_unit}. "
                   f"Holding for at least "
                   f"{tiers['mitigation_min_hold_seconds']:.0f}s.")
            self.log.info(msg)
            self._notify(Notice(NOTICE_INFO, 6003, msg))

        if max_val < (threshold - CLEAR_HYSTERESIS_AMPS):
            if self.clear_since is None:
                self.clear_since = now
        else:
            self.clear_since = None

        if (self.grace_deadline is None
                and self.clear_since is not None
                and (now - self.clear_since) >= tiers["mitigation_clear_seconds"]
                and (now - self.engaged_at) >= tiers["mitigation_min_hold_seconds"]):
            self.release(now, f"current stayed below {threshold - CLEAR_HYSTERESIS_AMPS:.2f}A "
                              f"for {tiers['mitigation_clear_seconds']:.0f}s")
        return None

    def release(self, now: float, why: str, arm_retrigger: bool = True) -> None:
        """Give the GPU back.

        arm_retrigger starts the window in which a returning fault is treated as
        persistent. A release the user asked for (pause, mode switch) or one forced by
        a suspend is not evidence about the connector, so those do not arm it.
        """
        if not self.engaged:
            return

        m = self.mitigator()
        restored = m.release() if m is not None else False

        self.engaged = False
        self.engaged_at = None
        self.engaged_tier = None
        self.grace_deadline = None
        self.clear_since = None
        self.critical_since = None
        self.catastrophic_samples = 0
        self.released_at = now if arm_retrigger else None

        msg = f"GPU limit released ({why})."
        if arm_retrigger:
            window = self._get_tier_values()["retrigger_window_seconds"]
            msg += (f" Another overcurrent within {window:.0f}s will be treated as a "
                    f"persistent fault and shut the machine down.")
        if not restored:
            msg += " WARNING: the card may not be fully restored, check the log above."
            # Event Viewer only, and ahead of the log line below, as it always was.
            # The read-back detail (both numbers) when the mitigator has it.
            self._notify(Notice(NOTICE_WARNING, 6007,
                                getattr(m, "last_restore_warning", None) or msg))

        self.log.warning(msg)
        self._notify(Notice(NOTICE_INFO, 6005, msg, (
            "12VHPWR Guard",
            "GPU restored to normal clocks and power limit." if restored
            else "GPU limit released, but the restore could not be verified. Check the log.")))

    def reset_timers(self) -> None:
        """Drop every in-flight timer. Used after a suspend/resume gap."""
        self.critical_since = None
        self.catastrophic_samples = 0
        self.released_at = None

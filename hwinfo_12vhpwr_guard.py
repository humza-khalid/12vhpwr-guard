import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import time
import subprocess
import threading
import logging
import math
import configparser
import queue
import xml.etree.ElementTree as ET
from logging.handlers import RotatingFileHandler
from typing import List, Tuple, Optional

# =========================
# CONFIG
# =========================

PIN_LABELS = [
    "GPU 12VHPWR Pin1 Current",
    "GPU 12VHPWR Pin2 Current",
    "GPU 12VHPWR Pin3 Current",
    "GPU 12VHPWR Pin4 Current",
    "GPU 12VHPWR Pin5 Current",
    "GPU 12VHPWR Pin6 Current",
]

# The defaults, the tier and advisory constants, the timing constants and the
# decision classes live in guard_core.py, shared with the Linux daemon so both
# platforms decide the same way. Imported by name so every one of them is still
# reachable as hwinfo_12vhpwr_guard.<name>, which the tests and the tools rely on.
# Patching one of these names here reaches PinAdvisor (it reads them from this
# module, see its binding below); the others read guard_core's own copy.
import guard_core
from guard_core import (
    # Defaults and the tiered response (v1.1)
    DEFAULT_THRESHOLD_AMPS,
    DEFAULT_SUSTAINED_SECONDS_REQUIRED,
    DEFAULT_CRITICAL_AMPS,
    DEFAULT_CRITICAL_SECONDS,
    DEFAULT_CATASTROPHIC_AMPS,
    CATASTROPHIC_SAMPLES_REQUIRED,
    DEFAULT_MITIGATION_GRACE_SECONDS,
    CRITICAL_GRACE_SECONDS,
    DEFAULT_MITIGATION_MIN_HOLD_SECONDS,
    DEFAULT_MITIGATION_CLEAR_SECONDS,
    DEFAULT_RETRIGGER_WINDOW_SECONDS,
    RESPONSE_TIERED,
    RESPONSE_SHUTDOWN_ONLY,
    VALID_RESPONSE_MODES,
    DEFAULT_RESPONSE_MODE,
    DEFAULT_TIER_VALUES,
    # Pin advisories and the sensor-path check (v1.4)
    DEFAULT_ADVISORIES_ENABLED,
    DEFAULT_ADVISORY_LOAD_AMPS,
    DEFAULT_DEAD_PIN_MAX_AMPS,
    DEFAULT_IMBALANCE_SPREAD_AMPS,
    DEFAULT_ADVISORY_SUSTAIN_SECONDS,
    DEFAULT_ADVISORY_REPEAT_SECONDS,
    DEAD_PIN_OTHERS_MIN_AMPS,
    IMBALANCE_MIN_MAX_AMPS,
    ADVISORY_CLEAR_SEC,
    NO_CURRENT_AMPS,
    NO_CURRENT_SUSTAIN_SEC,
    MISMATCH_MIN_WATTS,
    MISMATCH_RATIO,
    MISMATCH_SUSTAIN_SEC,
    ADVISORY_EVENT_IDS,
    DEFAULT_ADVISORY_VALUES,
    # Tier 1 sustained timer and the flight recorder
    CONSECUTIVE_SAMPLES_REQUIRED,
    SUSTAINED_PROGRESS_LOG_EVERY_SEC,
    CLEAR_HYSTERESIS_AMPS,
    CLEAR_STABLE_SECONDS,
    FLIGHT_SAMPLES,
    FLIGHT_KEEP_FILES,
    # Records and classes
    NOTICE_INFO,
    NOTICE_WARNING,
    NOTICE_ERROR,
    Notice,
    Advisory,
    THRESHOLD_OVER,
    THRESHOLD_CLEARED,
    THRESHOLD_NEAR,
    ThresholdTick,
    ThresholdTracker,
)

TIER_VALUES = dict(DEFAULT_TIER_VALUES)
RESPONSE_MODE = DEFAULT_RESPONSE_MODE

ADVISORY_VALUES = dict(DEFAULT_ADVISORY_VALUES)

# The on/off flag lives in the file under "advisories"; the dict key spells out
# what the value means.
ADVISORIES_CONFIG_KEY = "advisories"

# Which sensor source to use: "auto" prefers the card's own chip and falls back to
# HWiNFO; "astral" and "hwinfo" pin it to one. Set "hwinfo" to roll back the direct
# backend without reinstalling.
DEFAULT_SENSOR_BACKEND = "auto"
VALID_SENSOR_BACKENDS = ("auto", "astral", "hwinfo")
SENSOR_BACKEND = DEFAULT_SENSOR_BACKEND

# A gap this long between samples means suspend/resume or a severe stall, not a
# measurement. Sustained timers are reset rather than trusted across it.
RESUME_GRACE_SEC = 5.0

# =========================
# Self-restart after sensor loss (v1.4)
#
# A driver update takes the NVAPI session the direct backend holds with it, and
# nothing inside this process ever gets it back: the PR #1 experiment logged 131
# reconnect attempts in a row, all of them failed, and the guard sat blind until
# someone restarted the task by hand. A fresh process gets a fresh driver session,
# so after this long with no source at all the guard exits with RESTART_EXIT_CODE and
# the supervisor (see "Supervisor" near the end of this file) starts it again. Set 0
# to turn the whole thing off and keep waiting instead.
# =========================
DEFAULT_SENSOR_LOSS_RESTART_SECONDS = 300.0
# The guard's own "start me again" exit code, distinct from the 1 a Python crash
# exits with, so the supervisor can tell a planned restart from an unplanned one.
# 75 is EX_TEMPFAIL from sysexits.h: a temporary failure, try again.
RESTART_EXIT_CODE = 75
SENSOR_LOSS_RESTART_SEC = DEFAULT_SENSOR_LOSS_RESTART_SECONDS

# How long the loop waits between reconnect attempts once it has no source. Named
# rather than left inline because it is also the cadence at which the restart clock
# above gets looked at.
RECONNECT_RETRY_SEC = 3.0

# These will be loaded from config file or use defaults
THRESHOLD_AMPS = DEFAULT_THRESHOLD_AMPS
POLL_INTERVAL_SEC = 0.5
SUSTAINED_SECONDS_REQUIRED = DEFAULT_SUSTAINED_SECONDS_REQUIRED

# Thread-safe access to configurable values
_config_lock = threading.Lock()

# Shutdown behavior
SHUTDOWN_DELAY_SEC = 5
SHUTDOWN_FORCE_CLOSE_APPS = True

# Logs
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "12vhpwr_guard.log")
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 5

# The one logger, published by main() so the tray callbacks can write to the same
# file the monitor thread does. They run on the tray thread and are handed no
# logger of their own, and a setting the user changed has to be in the log before
# the trip line that the new value caused. None until main() runs (and under
# tests), so every use has to tolerate that.
LOGGER: Optional[logging.Logger] = None

# Config file
CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")

# Written when the guard fires an emergency shutdown, shown and removed at the next
# start. Without it, a shutdown that happens while the user is away is a mystery:
# the machine is just off, Windows gets the blame, and the bad cable gets plugged
# straight back into a heavy load.
LAST_SHUTDOWN_PATH = os.path.join(BASE_DIR, "last_shutdown.json")

# A paused guard protects nothing. Remind at a cadence that is noticeable without
# being punishment; 0 disables.
PAUSE_REMINDER_EVERY_SEC = 1800.0

# Shown in the tray menu and logged at startup so "which version are you running"
# is answerable without digging through files.
APP_VERSION = "1.4.0"

# Event Viewer source name
EVENT_SOURCE = "12VHPWR Guard"

# Single instance
# Global, not Local: there is one GPU per machine, so two Windows accounts signed in
# at once must not each run a guard that applies and restores the same GPU limit.
SINGLE_INSTANCE_MUTEX = r"Global\12VHPWR_Guard_SingleInstance"
ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183

# =========================
# Config file management (INI format)
# =========================
_TRUE_WORDS = ("on", "true", "yes", "1")
_FALSE_WORDS = ("off", "false", "no", "0")

def _config_bool(config, key: str, fallback: bool) -> bool:
    """Read an on/off style key. Anything unrecognised keeps the default."""
    raw = config.get("Settings", key, fallback=None)
    if raw is None:
        return fallback
    word = str(raw).strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    return fallback

def load_config() -> dict:
    """
    Load configuration from config.ini file, return dict with values or defaults.
    Note: This loads once at startup. Values are kept in memory and updated via set_config_values().
    The file is only read on startup, not polled continuously.
    """
    defaults = {
        "threshold_amps": DEFAULT_THRESHOLD_AMPS,
        "sustained_seconds_required": DEFAULT_SUSTAINED_SECONDS_REQUIRED,
        "sensor_backend": DEFAULT_SENSOR_BACKEND,
        "response_mode": DEFAULT_RESPONSE_MODE,
        "sensor_loss_restart_seconds": DEFAULT_SENSOR_LOSS_RESTART_SECONDS,
    }
    defaults.update(DEFAULT_TIER_VALUES)
    defaults.update(DEFAULT_ADVISORY_VALUES)

    if not os.path.exists(CONFIG_PATH):
        return defaults

    try:
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH, encoding="utf-8")

        # Get values from [Settings] section, with defaults
        threshold = config.getfloat("Settings", "threshold_amps", fallback=DEFAULT_THRESHOLD_AMPS)
        sustained = config.getfloat("Settings", "sustained_seconds_required", fallback=DEFAULT_SUSTAINED_SECONDS_REQUIRED)
        backend = config.get("Settings", "sensor_backend", fallback=DEFAULT_SENSOR_BACKEND).strip().lower()
        mode = config.get("Settings", "response_mode", fallback=DEFAULT_RESPONSE_MODE).strip().lower()

        # Validate
        # nan slips past a <= 0 test and inf is not a threshold: either would
        # silently switch tier 1 off, so both count as invalid like a negative.
        if (not (math.isfinite(threshold) and math.isfinite(sustained))
                or threshold <= 0 or sustained <= 0):
            raise ValueError("Values must be positive")
        if backend not in VALID_SENSOR_BACKENDS:
            backend = DEFAULT_SENSOR_BACKEND
        if mode not in VALID_RESPONSE_MODES:
            mode = DEFAULT_RESPONSE_MODE

        loaded = {
            "threshold_amps": threshold,
            "sustained_seconds_required": sustained,
            "sensor_backend": backend,
            "response_mode": mode,
        }

        # One bad tier value falls back to that default on its own; the rest of the
        # file is still worth honouring.
        for key, fallback in DEFAULT_TIER_VALUES.items():
            value = config.getfloat("Settings", key, fallback=fallback)
            loaded[key] = value if math.isfinite(value) and value > 0 else fallback

        # Same rule for the advisory numbers, and the on/off flag on top.
        for key, fallback in DEFAULT_ADVISORY_VALUES.items():
            if key == "advisories_enabled":
                continue
            value = config.getfloat("Settings", key, fallback=fallback)
            loaded[key] = value if math.isfinite(value) and value > 0 else fallback
        loaded["advisories_enabled"] = _config_bool(
            config, ADVISORIES_CONFIG_KEY, DEFAULT_ADVISORIES_ENABLED
        )

        # Read on its own because 0 is a real value here (the feature switched off),
        # so the "positive or fall back" rule the loops above use would silently
        # turn it back on. Only a negative number is nonsense.
        restart_after = config.getfloat(
            "Settings", "sensor_loss_restart_seconds",
            fallback=DEFAULT_SENSOR_LOSS_RESTART_SECONDS,
        )
        loaded["sensor_loss_restart_seconds"] = (
            restart_after if math.isfinite(restart_after) and restart_after >= 0
            else DEFAULT_SENSOR_LOSS_RESTART_SECONDS
        )
        return loaded
    except Exception:
        # If config is corrupted or missing section, return defaults
        return defaults

def save_config(threshold_amps: float, sustained_seconds_required: float) -> bool:
    """Save configuration to config.ini file. Returns True on success."""
    try:
        config = configparser.ConfigParser()

        # Read what is already there so keys we do not manage (sensor_backend, and
        # anything a future version adds) survive a tray-menu threshold change.
        try:
            config.read(CONFIG_PATH, encoding="utf-8")
        except Exception:
            pass

        if not config.has_section("Settings"):
            config.add_section("Settings")

        config.set("Settings", "threshold_amps", str(float(threshold_amps)))
        config.set("Settings", "sustained_seconds_required", str(float(sustained_seconds_required)))

        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            config.write(f)
        return True
    except Exception:
        return False

def save_setting(key: str, value) -> bool:
    """Write one key into [Settings], leaving every other key untouched."""
    try:
        config = configparser.ConfigParser()
        try:
            config.read(CONFIG_PATH, encoding="utf-8")
        except Exception:
            pass
        if not config.has_section("Settings"):
            config.add_section("Settings")
        config.set("Settings", key, str(value))
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            config.write(f)
        return True
    except Exception:
        return False

def get_config_values() -> Tuple[float, float]:
    """Thread-safe getter for current config values."""
    with _config_lock:
        return THRESHOLD_AMPS, SUSTAINED_SECONDS_REQUIRED

def get_tier_values() -> dict:
    """Thread-safe snapshot of the tier thresholds and windows."""
    with _config_lock:
        return dict(TIER_VALUES)

def set_tier_values(**kwargs) -> None:
    """Thread-safe setter for tier values. Unknown keys are ignored."""
    with _config_lock:
        for key, value in kwargs.items():
            if key in TIER_VALUES:
                TIER_VALUES[key] = float(value)

def get_advisory_values() -> dict:
    """Thread-safe snapshot of the advisory settings, including the on/off flag."""
    with _config_lock:
        return dict(ADVISORY_VALUES)

def set_advisory_values(**kwargs) -> None:
    """Thread-safe setter for advisory values. Unknown keys are ignored."""
    with _config_lock:
        for key, value in kwargs.items():
            if key not in ADVISORY_VALUES:
                continue
            ADVISORY_VALUES[key] = (bool(value) if key == "advisories_enabled"
                                    else float(value))

def get_response_mode() -> str:
    """Thread-safe getter. Changed from the tray at runtime, read every tick."""
    with _config_lock:
        return RESPONSE_MODE

def set_response_mode(mode: str) -> None:
    global RESPONSE_MODE
    if mode not in VALID_RESPONSE_MODES:
        return
    with _config_lock:
        RESPONSE_MODE = mode

def set_config_values(threshold_amps: float, sustained_seconds_required: float) -> None:
    """Thread-safe setter for config values."""
    global THRESHOLD_AMPS, SUSTAINED_SECONDS_REQUIRED
    with _config_lock:
        THRESHOLD_AMPS = float(threshold_amps)
        SUSTAINED_SECONDS_REQUIRED = float(sustained_seconds_required)

def reset_config_to_defaults() -> bool:
    """Reset config to default values and save to file."""
    set_config_values(DEFAULT_THRESHOLD_AMPS, DEFAULT_SUSTAINED_SECONDS_REQUIRED)
    return save_config(DEFAULT_THRESHOLD_AMPS, DEFAULT_SUSTAINED_SECONDS_REQUIRED)

# Load config on startup
_config_data = load_config()
set_config_values(_config_data["threshold_amps"], _config_data["sustained_seconds_required"])
set_tier_values(**_config_data)
set_advisory_values(**_config_data)
set_response_mode(_config_data.get("response_mode", DEFAULT_RESPONSE_MODE))
SENSOR_BACKEND = _config_data.get("sensor_backend", DEFAULT_SENSOR_BACKEND)
SENSOR_LOSS_RESTART_SEC = _config_data.get(
    "sensor_loss_restart_seconds", DEFAULT_SENSOR_LOSS_RESTART_SECONDS
)

# =========================
# Direct (HWiNFO-free) backend - optional import
# =========================
try:
    from astral_i2c import AstralPinSensor, SensorGoneError, TransientReadError
    HAVE_ASTRAL = True
except Exception:
    HAVE_ASTRAL = False

    class SensorGoneError(RuntimeError):
        """The data source died; the caller should tear down and reconnect."""

    class TransientReadError(RuntimeError):
        """One sample failed. Skip this tick, keep the connection."""

# Kept so existing references and any external tooling keep working.
HWiNFOGoneError = SensorGoneError

# =========================
# Reversible GPU slowdown - optional import
# =========================
try:
    from gpu_mitigation import GpuMitigator, is_process_elevated
    HAVE_MITIGATION = True
except Exception:
    HAVE_MITIGATION = False
    GpuMitigator = None
    is_process_elevated = None

# =========================
# Toast notifications (winotify)
# =========================
try:
    from winotify import Notification, audio
    HAVE_TOAST = True
except Exception:
    HAVE_TOAST = False

def toast(title: str, msg: str) -> None:
    if not HAVE_TOAST:
        return
    try:
        n = Notification(app_id=EVENT_SOURCE, title=title, msg=msg, duration="short")
        n.set_audio(audio.Default, loop=False)
        n.show()
    except Exception:
        pass

# =========================
# Event Viewer (pywin32)
# =========================
HAVE_EVENTLOG = False
try:
    import win32evtlogutil
    import win32con
    HAVE_EVENTLOG = True
except Exception:
    HAVE_EVENTLOG = False

def eventlog_register_source(logger: logging.Logger) -> None:
    """
    Registers the event source under the Application log.
    Usually requires admin once. Safe to call repeatedly.
    """
    if not HAVE_EVENTLOG:
        logger.warning("pywin32 not available; Event Viewer logging disabled.")
        return

    try:
        # Most compatible form across pywin32 versions:
        win32evtlogutil.AddSourceToRegistry(EVENT_SOURCE, eventLogType="Application")
    except TypeError:
        # Older builds: AddSourceToRegistry(appName) only
        try:
            win32evtlogutil.AddSourceToRegistry(EVENT_SOURCE)
        except Exception as ex:
            logger.warning(f"Could not register Event Viewer source (may need admin once): {ex}")
    except Exception as ex:
        logger.warning(f"Could not register Event Viewer source (may need admin once): {ex}")

def eventlog_write(event_type: int, event_id: int, message: str) -> None:
    if not HAVE_EVENTLOG:
        return
    try:
        win32evtlogutil.ReportEvent(
            appName=EVENT_SOURCE,
            eventID=event_id,
            eventCategory=0,
            eventType=event_type,
            strings=[message],
            data=b""
        )
    except Exception:
        pass

def _eventlog_type(level: str) -> int:
    """The Event Viewer entry type for a guard_core notice level.

    Only called once HAVE_EVENTLOG is known to be True, which is the only time
    win32con exists.
    """
    if level == NOTICE_ERROR:
        return win32con.EVENTLOG_ERROR_TYPE
    if level == NOTICE_WARNING:
        return win32con.EVENTLOG_WARNING_TYPE
    return win32con.EVENTLOG_INFORMATION_TYPE

def windows_notify(notice: Notice) -> None:
    """Render a guard_core Notice the way this file always has: Event Viewer, then toast.

    The caller has already written the log line, so the order is the one the inline
    code had (log, Event Viewer, toast). HAVE_EVENTLOG, eventlog_write and toast are
    looked up as module globals on every call, never captured, so a test that swaps
    any of them after a TieredResponse or ThresholdTracker was built still sees it.
    """
    if notice.event_id is not None and HAVE_EVENTLOG:
        eventlog_write(_eventlog_type(notice.level), notice.event_id, notice.message)
    if notice.toast is not None:
        toast(*notice.toast)

# =========================
# Tray icon (pystray + pillow)
# =========================
try:
    import pystray
    from pystray import MenuItem as item
    from PIL import Image, ImageDraw
    HAVE_TRAY = True
except Exception:
    HAVE_TRAY = False

class Status:
    OK = "OK"
    MISSING = "MISSING"
    OVER = "OVER"
    MITIGATED = "MITIGATED"
    SHUTDOWN = "SHUTDOWN"
    PAUSED = "PAUSED"
    ERROR = "ERROR"

def make_icon(status: str) -> "Image.Image":
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if status == Status.OK:
        color = (0, 200, 0, 255)
    elif status == Status.MISSING:
        color = (160, 160, 160, 255)
    elif status == Status.OVER:
        color = (255, 170, 0, 255)
    elif status == Status.MITIGATED:
        # Deeper than OVER's amber: the guard has already acted on the card.
        color = (255, 100, 0, 255)
    elif status == Status.PAUSED:
        color = (70, 140, 255, 255)
    elif status == Status.ERROR:
        # Distinct from SHUTDOWN: red must only ever mean "we shut the machine down".
        color = (150, 70, 220, 255)
    else:
        color = (230, 0, 0, 255)

    draw.ellipse((8, 8, size - 8, size - 8), fill=color)
    draw.ellipse((18, 16, 30, 28), fill=(255, 255, 255, 90))
    return img

# =========================
# HWiNFO SM2 (Shared Memory)
# =========================
# The env overrides exist so tools/fake_hwinfo_sm2.py can stand in for HWiNFO during
# development. Production always uses the Global\ names HWiNFO itself creates.
HWINFO_SHARED_MEM_PATH = os.environ.get("HWINFO_SM2_NAME", r"Global\HWiNFO_SENS_SM2")
HWINFO_SHARED_MEM_MUTEX = os.environ.get("HWINFO_SM2_MUTEX", r"Global\HWiNFO_SM2_MUTEX")

# HWiNFO stamps the header with "HWiS" while running and "DEAD" once it shuts down.
HWINFO_MAGIC = int.from_bytes(b"HWiS", "little")

# If the header's timestamp stops advancing, HWiNFO died without writing DEAD.
SM2_STALE_AFTER_SEC = 30.0

FILE_MAP_READ = 0x0004
WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

OpenFileMappingW = kernel32.OpenFileMappingW
OpenFileMappingW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
OpenFileMappingW.restype = wt.HANDLE

MapViewOfFile = kernel32.MapViewOfFile
MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
MapViewOfFile.restype = wt.LPVOID

UnmapViewOfFile = kernel32.UnmapViewOfFile
UnmapViewOfFile.argtypes = [wt.LPCVOID]
UnmapViewOfFile.restype = wt.BOOL

CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [wt.HANDLE]
CloseHandle.restype = wt.BOOL

OpenMutexW = kernel32.OpenMutexW
OpenMutexW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
OpenMutexW.restype = wt.HANDLE

WaitForSingleObject = kernel32.WaitForSingleObject
WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
WaitForSingleObject.restype = wt.DWORD

ReleaseMutex = kernel32.ReleaseMutex
ReleaseMutex.argtypes = [wt.HANDLE]
ReleaseMutex.restype = wt.BOOL

CreateMutexW = kernel32.CreateMutexW
CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
CreateMutexW.restype = wt.HANDLE

# Waiting needs SYNCHRONIZE; ReleaseMutex needs MODIFY_STATE. Asking for anything
# beyond those only makes OpenMutexW more likely to be denied.
SYNCHRONIZE = 0x00100000
MUTEX_MODIFY_STATE = 0x0001
MUTEX_NEEDED = SYNCHRONIZE | MUTEX_MODIFY_STATE

def _last_winerr(msg: str) -> RuntimeError:
    return RuntimeError(f"{msg} (WinErr={ctypes.get_last_error()})")

class HWiNFOHeader(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("magic", ctypes.c_uint32),
        ("version", ctypes.c_uint32),
        ("version2", ctypes.c_uint32),
        ("last_update", ctypes.c_int64),
        ("sensor_section_offset", ctypes.c_uint32),
        ("sensor_element_size", ctypes.c_uint32),
        ("sensor_element_count", ctypes.c_uint32),
        ("entry_section_offset", ctypes.c_uint32),
        ("entry_element_size", ctypes.c_uint32),
        ("entry_element_count", ctypes.c_uint32),
    ]

class HWiNFOEntry(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("sensor_index", ctypes.c_uint32),
        ("id", ctypes.c_uint32),
        ("name_original", ctypes.c_char * 128),
        ("name_user", ctypes.c_char * 128),
        ("unit", ctypes.c_char * 16),
        ("value", ctypes.c_double),
        ("value_min", ctypes.c_double),
        ("value_max", ctypes.c_double),
        ("value_avg", ctypes.c_double),
    ]

def _cstr(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()

def _read_header(view_ptr: int) -> HWiNFOHeader:
    """Snapshot the header out of shared memory."""
    return HWiNFOHeader.from_buffer_copy(
        ctypes.string_at(view_ptr, ctypes.sizeof(HWiNFOHeader))
    )

def _check_header(header: HWiNFOHeader) -> None:
    """Raise HWiNFOGoneError if HWiNFO has exited or stopped updating."""
    if header.magic != HWINFO_MAGIC:
        raise HWiNFOGoneError("HWiNFO shared memory signature invalid (HWiNFO closed)")

    # A timestamp in the future yields a negative age and counts as fresh.
    age = time.time() - header.last_update
    if age > SM2_STALE_AFTER_SEC:
        raise HWiNFOGoneError(f"HWiNFO data stale ({age:.0f}s since last update)")

def open_hwinfo_sm2() -> Tuple[wt.HANDLE, int, Optional[wt.HANDLE]]:
    h_map = OpenFileMappingW(FILE_MAP_READ, False, HWINFO_SHARED_MEM_PATH)
    if not h_map:
        raise _last_winerr("OpenFileMappingW failed. Is HWiNFO Sensors running + Shared Memory enabled?")

    # 0 maps the whole section; naming a size fails outright when the section is smaller.
    p_view = MapViewOfFile(h_map, FILE_MAP_READ, 0, 0, 0)
    if not p_view:
        CloseHandle(h_map)
        raise _last_winerr("MapViewOfFile failed")

    view_ptr = int(ctypes.cast(p_view, ctypes.c_void_p).value)

    # Refuse a leftover section belonging to an HWiNFO that already exited.
    try:
        _check_header(_read_header(view_ptr))
    except HWiNFOGoneError as ex:
        UnmapViewOfFile(ctypes.c_void_p(view_ptr))
        CloseHandle(h_map)
        raise RuntimeError(str(ex))

    h_mutex = OpenMutexW(MUTEX_NEEDED, False, HWINFO_SHARED_MEM_MUTEX)
    if not h_mutex:
        h_mutex = None  # no-mutex mode

    return h_map, view_ptr, h_mutex

def close_sm2(h_map, view_ptr, h_mutex) -> None:
    """Release the shared-memory handles. Safe with None/0 values."""
    try:
        if view_ptr:
            UnmapViewOfFile(ctypes.c_void_p(view_ptr))
    except Exception:
        pass
    try:
        if h_mutex:
            CloseHandle(h_mutex)
    except Exception:
        pass
    try:
        if h_map:
            CloseHandle(h_map)
    except Exception:
        pass

def read_entries(view_ptr: int, mutex) -> List[HWiNFOEntry]:
    if mutex:
        wait = WaitForSingleObject(mutex, 2000)
        if wait not in (WAIT_OBJECT_0, WAIT_ABANDONED):
            raise RuntimeError("Timeout waiting for HWiNFO mutex")

    try:
        header = _read_header(view_ptr)
        _check_header(header)

        if header.entry_section_offset == 0 or header.entry_element_count == 0:
            return []

        base = view_ptr + header.entry_section_offset
        entry_size = header.entry_element_size
        count = header.entry_element_count
        struct_size = ctypes.sizeof(HWiNFOEntry)

        # Copy while the mutex is held: from_address would leave us dereferencing
        # live shared memory after the release, racing HWiNFO's writer.
        out: List[HWiNFOEntry] = []
        for i in range(count):
            addr = base + i * entry_size
            raw = ctypes.string_at(addr, min(entry_size, struct_size))
            out.append(HWiNFOEntry.from_buffer_copy(raw.ljust(struct_size, b"\x00")))
        return out
    finally:
        if mutex:
            ReleaseMutex(mutex)

def find_pin_currents(entries: List[HWiNFOEntry]) -> List[Tuple[str, float, str]]:
    results = []
    targets = set(PIN_LABELS)

    for e in entries:
        name_user = _cstr(bytes(e.name_user))
        name_orig = _cstr(bytes(e.name_original))

        # Match on either name so sensors renamed in HWiNFO are still recognised.
        if name_orig in targets:
            label = name_orig
        elif name_user in targets:
            label = name_user
        else:
            continue

        unit = _cstr(bytes(e.unit)) or "A"
        results.append((label, float(e.value), unit))

    results.sort(key=lambda t: PIN_LABELS.index(t[0]))
    return results

# =========================
# Sensor backends
#
# Both expose connect() -> description, read_pins() -> [(label, amps, unit)], close().
# read_pins() raises TransientReadError for a skippable sample and SensorGoneError when
# the source has died and the caller should reconnect.
# =========================
class HwinfoBackend:
    """Per-pin currents via HWiNFO's SM2 shared memory."""

    key = "hwinfo"
    tag = "HWiNFO"

    def __init__(self):
        self.h_map = None
        self.view_ptr = None
        self.h_mutex = None

    def connect(self) -> str:
        self.h_map, self.view_ptr, self.h_mutex = open_hwinfo_sm2()
        return "HWiNFO SM2" + ("" if self.h_mutex else " (no-mutex mode)")

    @property
    def degraded(self) -> Optional[str]:
        if self.h_mutex is None:
            return "HWiNFO mutex not accessible. Running in no-mutex mode."
        return None

    def read_pins(self) -> List[Tuple[str, float, str]]:
        return find_pin_currents(read_entries(self.view_ptr, self.h_mutex))

    def close(self) -> None:
        close_sm2(self.h_map, self.view_ptr, self.h_mutex)
        self.h_map = self.view_ptr = self.h_mutex = None


class AstralBackend:
    """Per-pin currents straight from the card's IT8915 chip. No HWiNFO needed."""

    key = "astral"
    tag = "direct"

    def __init__(self):
        self.sensor = None

    def connect(self) -> str:
        if not HAVE_ASTRAL:
            raise RuntimeError("astral_i2c module unavailable")
        self.sensor = AstralPinSensor()
        return self.sensor.connect()

    @property
    def degraded(self) -> Optional[str]:
        return None

    @property
    def subsystem_id(self) -> Optional[int]:
        """Lets the mitigation side bind NVML to this exact card."""
        return getattr(self.sensor, "subsystem_id", None)

    def read_pins(self) -> List[Tuple[str, float, str]]:
        return self.sensor.read_pins()

    def close(self) -> None:
        if self.sensor is not None:
            self.sensor.close()
            self.sensor = None


class CrossCheck:
    """Observational only: when running direct, periodically compare against HWiNFO.

    Never influences a shutdown decision. Silently does nothing if HWiNFO is absent.

    Samples every minute but logs one summary per hour: a line per sample buries the
    entries that matter under a wall of agreement. Real disagreement (a worst-pin
    delta beyond anything sampling skew produces - field data tops out at 0.44A in
    normal use) is logged the moment it is seen, as a WARNING.
    """

    SUMMARY_EVERY_SEC = 3600.0
    WARN_DELTA_AMPS = 2.0

    def __init__(self, every_sec: float = 60.0):
        self.every_sec = every_sec
        self._next = 0.0
        self._handles = None
        self._announced = False
        self._summary_due = 0.0
        self._samples = 0
        self._worst = 0.0

    def maybe_log(self, logger: logging.Logger, backend, pins) -> None:
        if self.every_sec <= 0 or getattr(backend, "key", None) != "astral":
            return
        now = time.time()
        if now < self._next:
            return
        self._next = now + self.every_sec

        try:
            if self._handles is None:
                self._handles = open_hwinfo_sm2()
            _h_map, view_ptr, h_mutex = self._handles
            hw = find_pin_currents(read_entries(view_ptr, h_mutex))
            if len(hw) != len(pins):
                return
            direct_max = max(p[1] for p in pins)
            hwinfo_max = max(p[1] for p in hw)
            worst = max(abs(a[1] - b[1]) for a, b in zip(pins, hw))

            self._samples += 1
            self._worst = max(self._worst, worst)

            if not self._announced:
                # One line at startup proving the crosscheck is alive, then quiet.
                self._announced = True
                self._summary_due = now + self.SUMMARY_EVERY_SEC
                logger.info(
                    f"Crosscheck active: direct max {direct_max:.2f}A vs HWiNFO max "
                    f"{hwinfo_max:.2f}A (worst per-pin delta {worst:.2f}A); "
                    f"hourly summaries follow"
                )

            if worst >= self.WARN_DELTA_AMPS:
                logger.warning(
                    f"Crosscheck disagreement: direct max {direct_max:.2f}A vs HWiNFO "
                    f"max {hwinfo_max:.2f}A (worst per-pin delta {worst:.2f}A)"
                )

            if self._summary_due and now >= self._summary_due:
                logger.info(
                    f"Crosscheck: {self._samples} samples in the last hour, worst "
                    f"per-pin delta {self._worst:.2f}A"
                )
                self._samples = 0
                self._worst = 0.0
                self._summary_due = now + self.SUMMARY_EVERY_SEC
        except Exception:
            # HWiNFO not running or went away. Not our problem while running direct.
            self.close()

    def close(self) -> None:
        if self._handles is not None:
            try:
                close_sm2(*self._handles)
            except Exception:
                pass
            self._handles = None


class FlightRecorder(guard_core.FlightRecorder):
    """guard_core's flight recorder, writing into this install's logs/ directory.

    LOG_DIR and FLIGHT_KEEP_FILES are read from this module at dump time, as they
    were before the class moved, so redirecting LOG_DIR after the recorder exists
    (the tests point it at a tmp directory) still moves the dumps.
    """

    def __init__(self, maxlen: int = FLIGHT_SAMPLES):
        super().__init__(LOG_DIR, maxlen)

    @property
    def log_dir(self) -> str:
        return LOG_DIR

    @property
    def keep_files(self) -> int:
        return FLIGHT_KEEP_FILES


class PinAdvisor(guard_core.PinAdvisor):
    """guard_core's advisor, reading its fixed numbers from this module.

    Same constructor as before the move. The constants namespace is this module, so
    ADVISORY_CLEAR_SEC, NO_CURRENT_SUSTAIN_SEC, MISMATCH_SUSTAIN_SEC and the other
    sensor-path numbers are looked up here on every tick, and a patched value takes
    effect the moment it is set, exactly as it did when the class lived in this file.
    """

    def __init__(self, values: Optional[dict] = None):
        super().__init__(values, constants=sys.modules[__name__])


def candidate_backends(logger: logging.Logger) -> List[object]:
    """Backend classes to try, in order, for the configured sensor_backend setting."""
    if SENSOR_BACKEND == "hwinfo":
        return [HwinfoBackend]
    if SENSOR_BACKEND == "astral":
        return [AstralBackend]

    # auto: prefer the card's own chip, but only if one is actually present.
    if HAVE_ASTRAL:
        found = AstralPinSensor.detect()
        if found:
            logger.info(f"Astral card detected: {found}")
            return [AstralBackend, HwinfoBackend]
    return [HwinfoBackend]

# =========================
# Logging + Shutdown
# =========================
def setup_logger() -> logging.Logger:
    logger = logging.getLogger("12vhpwr_guard")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger

def note_shutdown_reason(reason: str) -> None:
    """Leave a note for the next start saying why this machine went down.

    Best-effort: the log and Event Viewer carry the reason regardless, this file
    exists so the next start can put it in the user's face.
    """
    payload = {
        "time": time.time(),
        "time_human": time.strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason,
        "version": APP_VERSION,
    }
    tmp = LAST_SHUTDOWN_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, LAST_SHUTDOWN_PATH)
    except Exception:
        pass

def announce_last_shutdown(logger: logging.Logger) -> None:
    """One-shot at startup: if the guard ended the last session, say so loudly.

    A corrupt note still announces (generic wording), because a corrupt note still
    means the guard shut this machine down. The file is removed either way.
    """
    if not os.path.exists(LAST_SHUTDOWN_PATH):
        return
    try:
        try:
            with open(LAST_SHUTDOWN_PATH, "r", encoding="utf-8") as f:
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
        logger.warning(msg)
        if HAVE_EVENTLOG:
            eventlog_write(win32con.EVENTLOG_WARNING_TYPE, 5091, msg)
        toast("12VHPWR Guard - read this", msg)
    finally:
        for path in (LAST_SHUTDOWN_PATH + ".tmp", LAST_SHUTDOWN_PATH):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

def shutdown_windows(logger: logging.Logger, reason: str) -> None:
    logger.critical(f"SHUTDOWN TRIGGERED: {reason}")
    note_shutdown_reason(reason)
    toast("12VHPWR Guard - Shutdown", reason)
    if HAVE_EVENTLOG:
        eventlog_write(win32con.EVENTLOG_ERROR_TYPE, 5090, reason)

    # Absolute path: the one safety-critical action must not depend on PATH.
    shutdown_exe = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "shutdown.exe"
    )
    args = [shutdown_exe, "/s", "/t", str(int(SHUTDOWN_DELAY_SEC))]
    if SHUTDOWN_FORCE_CLOSE_APPS:
        args.append("/f")
    subprocess.run(args, check=False)

# =========================
# Tiered response
# =========================
class TieredResponse(guard_core.TieredResponse):
    """guard_core's response ladder, bound to this file's settings and to Windows.

    Same constructor as before the move. The tier values and response mode come from
    the thread-safe getters above, called on every tick, so a tray change lands on
    the next one. Notices go to windows_notify: Event Viewer and toasts, with the same
    ids, titles, text and order as before.
    """

    def __init__(self, get_mitigator, logger: logging.Logger):
        super().__init__(get_mitigator, logger,
                         get_tier_values=get_tier_values,
                         get_response_mode=get_response_mode,
                         notify=windows_notify)

# =========================
# Autostart (Task Scheduler)
# =========================
# This only flips the Enabled flag on the task the installer already created. It
# deliberately does not create the task: the trigger, principal and restart
# settings live in install.ps1, and a second copy here would drift out of sync
# with it the first time either side changed.

TASK_NAME = "12VHPWR Guard"
TASK_SCHEMA_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
CREATE_NO_WINDOW = 0x08000000

# pystray asks for the checkmark state every time the menu is drawn, and each ask
# would otherwise cost a schtasks process launch.
_AUTOSTART_CACHE_TTL_SEC = 2.0
_autostart_cache = {"value": None, "at": 0.0}

# Bounds only the failure case: the confirm loop returns as soon as the change is
# visible, so a generous budget costs nothing on success and avoids reporting a
# failure for a change that did apply, just slowly, on a loaded machine.
_AUTOSTART_CONFIRM_TIMEOUT_SEC = 5.0
_AUTOSTART_CONFIRM_POLL_SEC = 0.1


def _is_elevated() -> bool:
    """Defer to gpu_mitigation's check so "elevated" has one definition in the project.

    Its token-based test is stricter than IsUserAnAdmin, and the mitigation path already
    gates on it, so the autostart toggle agreeing with it matters. The fallback only runs
    when that module failed to import, which is the same condition that already drops the
    guard to shutdown-only.
    """
    if is_process_elevated is not None:
        return is_process_elevated()
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _schtasks_path() -> str:
    # Absolute, for the same reason shutdown.exe is: PATH is not to be trusted here.
    return os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "schtasks.exe"
    )


def _clean_task_xml(raw: bytes) -> str:
    """Decode schtasks /xml output into something ElementTree will accept.

    schtasks always writes the header encoding="UTF-16", but only actually emits
    UTF-16 when stdout is a console; redirected to a pipe, as it is here, the
    bytes are single-byte. Handing those to the parser fails with "encoding
    specified in XML declaration is incorrect", so the declaration is dropped and
    the text is passed as str. ElementTree rejects a str that still carries an
    encoding declaration, which is why it has to go rather than be corrected.
    Both forms are decoded anyway so this does not depend on that behaviour.
    """
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        # The utf-16 codec consumes the BOM itself.
        text = raw.decode("utf-16", errors="replace")
    else:
        # utf-8-sig strips a UTF-8 BOM when one is present, plain utf-8 otherwise.
        text = raw.decode("utf-8-sig", errors="replace")
    text = text.lstrip()
    if text.startswith("<?xml") and "?>" in text:
        text = text.split("?>", 1)[1]
    return text


def query_autostart() -> Optional[bool]:
    """True/False if the logon task exists and is enabled/disabled, None if absent.

    Reads /xml rather than /fo LIST because the XML element names come from the
    Task Scheduler schema and are the same on every locale, while the LIST field
    labels are translated and would not parse on a non-English Windows.
    """
    try:
        proc = subprocess.run(
            [_schtasks_path(), "/query", "/tn", TASK_NAME, "/xml", "ONE"],
            capture_output=True, creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        root = ET.fromstring(_clean_task_xml(proc.stdout))
    except ET.ParseError:
        return None
    node = root.find(f"{TASK_SCHEMA_NS}Settings/{TASK_SCHEMA_NS}Enabled")
    # A missing <Enabled> means the schema default, which is true.
    if node is None or node.text is None:
        return True
    return node.text.strip().lower() == "true"


def autostart_state(force: bool = False) -> Optional[bool]:
    now = time.time()
    if force or (now - _autostart_cache["at"]) > _AUTOSTART_CACHE_TTL_SEC:
        _autostart_cache["value"] = query_autostart()
        _autostart_cache["at"] = now
    return _autostart_cache["value"]


def set_autostart(enable: bool) -> bool:
    """Enable/disable the logon task. True once the change is visible in the task."""
    args = ["/change", "/tn", TASK_NAME, "/enable" if enable else "/disable"]
    _autostart_cache["at"] = 0.0

    if _is_elevated():
        try:
            proc = subprocess.run(
                [_schtasks_path(), *args],
                capture_output=True, creationflags=CREATE_NO_WINDOW,
            )
            return proc.returncode == 0
        except Exception:
            return False

    # Started by hand rather than by the task, so the process is not elevated.
    # One UAC prompt beats sending the user off to find install.bat.
    params = " ".join(f'"{a}"' if " " in a else a for a in args)
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", _schtasks_path(), params, None, 0
        )
    except Exception:
        return False
    if rc <= 32:  # includes the user declining the UAC prompt
        return False

    # ShellExecuteW waits for the UAC decision but not for the process it starts,
    # and never reports that process's exit code, so the only honest confirmation
    # is reading the task back.
    deadline = time.time() + _AUTOSTART_CONFIRM_TIMEOUT_SEC
    while time.time() < deadline:
        time.sleep(_AUTOSTART_CONFIRM_POLL_SEC)
        if query_autostart() == enable:
            return True
    return False

# =========================
# Shared state for tray
# =========================
state_lock = threading.Lock()
shared = {
    "status": Status.MISSING,
    "paused": False,
    "last_message": "Starting...",
    "max_label": "",
    "max_val": 0.0,
    "max_unit": "A",
    "source": "",
    # "" until we know, then "ready" or the reason the GPU cannot be limited.
    "mitigation": "",
    # A few words about a live pin advisory, None when there is nothing to say.
    # Advisories never change the icon: the guard has not acted on anything.
    "advisory": None,
    # True while the pins report no current at all. This one does change the icon:
    # purple already means "cannot protect", and a guard that cannot see the load has
    # no business showing green. Cleared when the pins carry current again.
    "sensor_path_fault": False,
    # What the process should exit with. 0 is a clean stop (the user chose Exit, or
    # the machine is shutting down) and Task Scheduler leaves it alone; 1 asks the
    # scheduler for a restart. Set through request_exit() only.
    "exit_code": 0,
}

def set_state(**kwargs):
    with state_lock:
        shared.update(kwargs)

def get_state():
    with state_lock:
        return dict(shared)

# =========================
# Ending the process
# =========================
# The tray icon, published by run_tray() so a background thread that has to end the
# process can unblock icon.run() in the main thread. None before the tray starts and
# under tests, so every use has to tolerate that.
TRAY_ICON = None

def request_exit(code: int, stop_event: Optional[threading.Event] = None) -> None:
    """Ask the process to end with this code, from any thread.

    The monitor runs in a daemon thread and cannot call sys.exit() itself: that would
    end the thread and leave the tray running with nothing behind it. So the code goes
    into shared state, the loop is told to stop, and the tray icon is stopped, which
    returns main() from icon.run() and lets it exit with the code. A non-zero code is
    the whole point of the self-restart: the supervisor restarts the guard on any exit
    that is not 0.
    """
    set_state(exit_code=int(code))
    if stop_event is not None:
        stop_event.set()
    icon = TRAY_ICON
    if icon is not None:
        try:
            icon.stop()
        except Exception:
            pass

def pending_exit_code() -> int:
    """The code main() should exit with. 0 unless something asked for another one."""
    try:
        return int(get_state().get("exit_code", 0) or 0)
    except (TypeError, ValueError):
        return 0

def _span_text(seconds: float) -> str:
    """A window in seconds as something a toast can say out loud."""
    if seconds >= 120:
        return f"{seconds / 60:.0f} minutes"
    if seconds >= 60:
        return "1 minute"
    return f"{seconds:g} seconds"

# =========================
# Monitor thread
# =========================
def run_startup_recovery(logger: logging.Logger) -> None:
    """Undo a GPU limit left behind by a previous run that died while mitigating.

    Touches NVML only when the marker file is actually there, so a guard that never
    mitigates never loads it at all.
    """
    if not HAVE_MITIGATION:
        return
    m = GpuMitigator(logger=logger)
    try:
        msg = m.startup_recovery()
        if msg:
            logger.warning(msg)
            if HAVE_EVENTLOG:
                eventlog_write(win32con.EVENTLOG_WARNING_TYPE, 6006, msg)
            toast("12VHPWR Guard", "Restored GPU settings left behind by a previous run.")
    except Exception as ex:
        logger.error(f"Startup recovery failed: {ex}")
    finally:
        try:
            m.close()
        except Exception:
            pass

def monitor_loop(stop_event: threading.Event, logger: logging.Logger):
    eventlog_register_source(logger)
    run_startup_recovery(logger)
    announce_last_shutdown(logger)

    threshold_amps, sustained_seconds_required = get_config_values()
    tier_values = get_tier_values()
    logger.info(f"Starting 12VHPWR Guard v{APP_VERSION} (tray mode)...")
    logger.info(f"Log file: {LOG_PATH}")
    logger.info(
        f"Threshold={threshold_amps:.2f}A | Poll={POLL_INTERVAL_SEC}s | Sustained={sustained_seconds_required}s | "
        f"ForceClose={SHUTDOWN_FORCE_CLOSE_APPS}"
    )
    logger.info(
        f"Response mode={get_response_mode()} | "
        f"Tier 2 critical={tier_values['critical_amps']:.2f}A/{tier_values['critical_seconds']:.0f}s | "
        f"Tier 3 catastrophic={tier_values['catastrophic_amps']:.2f}A"
    )
    advisory_values = get_advisory_values()
    if advisory_values["advisories_enabled"]:
        logger.info(
            f"Advisories (warn only): load gate={advisory_values['advisory_load_amps']:.1f}A | "
            f"dead pin<={advisory_values['dead_pin_max_amps']:.2f}A | "
            f"spread>={advisory_values['imbalance_spread_amps']:.2f}A | "
            f"sustain={advisory_values['advisory_sustain_seconds']:.0f}s | "
            f"repeat={advisory_values['advisory_repeat_seconds']:.0f}s"
        )
    else:
        logger.info("Advisories: off")
    # Says which half of the sensor-path check is running. The pin half runs in both
    # modes and is never silenced by `advisories = off`; only the board power reading
    # needs NVML, which shutdown-only mode does not touch.
    if get_response_mode() == RESPONSE_TIERED:
        logger.info("Sensor-path check: pins and board power")
    else:
        logger.info("Sensor-path check: pins only (no NVML in shutdown-only mode)")
    # Read once here like the tier and advisory numbers, so a value the file changes
    # mid-run cannot move the deadline a live outage is already being measured against.
    restart_after = SENSOR_LOSS_RESTART_SEC
    logger.info(
        "Self-restart after direct sensor loss: "
        + (f"{restart_after:g} s" if restart_after > 0 else "off")
    )
    toast("12VHPWR Guard", "Monitoring started.")
    if HAVE_EVENTLOG:
        eventlog_write(win32con.EVENTLOG_INFORMATION_TYPE, 1000, "12VHPWR Guard started.")

    # ---- throttles: avoid toast/log spam when no source is available ----
    source_missing_notified = False
    source_connected_notified = False
    last_source_reminder = 0.0

    source_missing_logged = False
    last_source_log_reminder = 0.0

    # Optional reminders while missing (disabled by default)
    SOURCE_REMINDER_EVERY_SEC = 0       # e.g. 600 for every 10 minutes
    SOURCE_LOG_REMINDER_EVERY_SEC = 0   # e.g. 600 for every 10 minutes

    # sustained state. Tier 1's over/sustained/clear timer is guard_core's
    # ThresholdTracker, shared with Linux; its notices render through windows_notify.
    tier1 = ThresholdTracker(logger, notify=windows_notify)
    pins_missing = False
    last_sample_time: Optional[float] = None

    # active data source
    backend = None
    # Self-restart bookkeeping. direct_seen stays True for the rest of the process
    # once the card's own sensor has been read: it is what separates "the driver took
    # our session away" from "this machine never had a direct sensor to lose".
    # source_lost_at is the moment the guard went blind, None whenever it is not.
    direct_seen = False
    source_lost_at: Optional[float] = None
    crosscheck = CrossCheck()
    flight = FlightRecorder()
    advisor = PinAdvisor(advisory_values)
    backend_classes = candidate_backends(logger)

    # pause bookkeeping: a paused guard protects nothing, so it must not be
    # possible to pause it and forget it.
    paused_since: Optional[float] = None
    last_pause_reminder = 0.0

    # tiered response
    mitigator = None
    mitigation_connect_tried = False
    shutting_down = False

    def ensure_mitigator():
        """Bind NVML the first time tiered mode actually needs it.

        Shutdown-only never gets here, which is the point: that mode is the rollback
        if anything about the GPU limiting misbehaves, so it must not touch NVML at all.
        """
        nonlocal mitigator, mitigation_connect_tried
        if not HAVE_MITIGATION:
            return None
        if get_response_mode() != RESPONSE_TIERED:
            return mitigator
        if mitigator is None and not mitigation_connect_tried:
            mitigation_connect_tried = True
            candidate = GpuMitigator(logger=logger)
            # Bind to the same physical card the sensor backend is reading. NVML and
            # NVAPI report the subsystem id in the same format.
            candidate.connect(getattr(backend, "subsystem_id", None))
            mitigator = candidate
            set_state(mitigation="ready" if candidate.is_available
                      else (candidate.unavailable_reason or "unavailable"))
        return mitigator

    response = TieredResponse(ensure_mitigator, logger)
    logger.info(
        f"Sensor backend: setting={SENSOR_BACKEND}, "
        f"trying [{', '.join(c.key for c in backend_classes)}]"
    )

    def render_advisory(adv: Advisory) -> None:
        """Say one advisory out loud. Nothing here touches the GPU or the machine.

        PinAdvisor stays platform neutral, so every Windows-specific part of an
        advisory (toast, Event Viewer, tray tooltip, icon colour) happens here instead.
        """
        if adv.kind == "cleared":
            logger.info(adv.message)
            if HAVE_EVENTLOG:
                eventlog_write(win32con.EVENTLOG_INFORMATION_TYPE, 7004, adv.message)
            if adv.ended == "no_current":
                # The pins carry current again, so the readings mean something again
                # and the icon is allowed back to green on the next tick.
                set_state(advisory=None, sensor_path_fault=False)
            else:
                set_state(advisory=None)
            return

        logger.warning(adv.message)
        if HAVE_EVENTLOG:
            eventlog_write(win32con.EVENTLOG_WARNING_TYPE,
                           ADVISORY_EVENT_IDS.get(adv.kind, 7001), adv.message)
        if adv.kind == "no_current":
            # Not a warning about the cable but about this tool: it is watching a wire
            # that carries nothing. The flag holds the icon purple until it is cleared.
            set_state(advisory=adv.short, sensor_path_fault=True)
            toast("12VHPWR Guard - not protected", adv.summary)
        else:
            set_state(advisory=adv.short)
            toast("12VHPWR Guard - advisory", adv.summary)
        if adv.first:
            # The minute before the first warning is the evidence: it is what an RMA
            # claim or a bug report needs, and the fault may not repeat on demand.
            flight.dump(logger, "advisory")

    def set_ok_status(message: str) -> None:
        """Report a healthy tick, unless the guard cannot see the load at all.

        OVER, MITIGATED and SHUTDOWN are set elsewhere and still win: those come from
        readings that are plainly real, and they are acted on whatever the sensor path
        looks like. This is only about the guard's right to say OK.
        """
        if get_state().get("sensor_path_fault"):
            set_state(status=Status.ERROR,
                      last_message="Not protected: the pin sensors see no current")
        else:
            set_state(status=Status.OK, last_message=message)

    def board_power_watts() -> Optional[float]:
        """What the card says it is drawing, or None when we cannot ask.

        Shutdown-only mode reads nothing here, and the mode is checked rather than
        left to ensure_mitigator() returning None, so the promise holds even for a
        session that was switched out of tiered mode with a handle already bound.
        In tiered mode this asks the mitigator the loop already has, which the
        response ladder has bound on the first tick anyway, so nothing new is loaded.
        Best effort by construction: the mitigator is injectable, and losing a
        monitoring tick over an enrichment reading would be a worse bug than not
        having the number.
        """
        if get_response_mode() != RESPONSE_TIERED:
            return None
        m = ensure_mitigator()
        if m is None or not m.is_available:
            return None
        try:
            milliwatts = m.power_usage_mw()
        except Exception:
            return None
        return None if milliwatts is None else float(milliwatts) / 1000.0

    def try_connect() -> bool:
        """One pass over the candidate backends. True as soon as one connects."""
        nonlocal backend
        nonlocal source_missing_notified, source_connected_notified, last_source_reminder
        nonlocal source_missing_logged, last_source_log_reminder

        set_state(status=Status.ERROR, last_message="Waiting for pin sensors...")

        # Log only once per missing period
        if not source_missing_logged:
            logger.info("Waiting for a pin-current source...")
            source_missing_logged = True
            last_source_log_reminder = time.time()

        errors = []
        for cls in backend_classes:
            candidate = cls()
            try:
                description = candidate.connect()
            except Exception as ex:
                errors.append(f"{cls.key}: {ex}")
                try:
                    candidate.close()
                except Exception:
                    pass
                continue

            backend = candidate

            # Say why any preferred backend was skipped. Otherwise an Astral owner
            # silently running on HWiNFO has no way to find out why.
            for skipped in errors:
                logger.warning(f"Backend unavailable, trying next -> {skipped}")

            set_state(source=candidate.tag)
            msg = f"Connected: {description} [{candidate.tag}]"
            logger.info(msg)
            if HAVE_EVENTLOG:
                eventlog_write(win32con.EVENTLOG_INFORMATION_TYPE, 1002, msg)

            if not source_connected_notified:
                toast("12VHPWR Guard", f"Monitoring active via {candidate.tag}.")
                source_connected_notified = True

            # Reset missing state on recovery
            source_missing_notified = False
            source_missing_logged = False

            degraded = candidate.degraded
            if degraded:
                logger.warning(degraded)
                if HAVE_EVENTLOG:
                    eventlog_write(win32con.EVENTLOG_WARNING_TYPE, 1001, degraded)

            return True

        now = time.time()
        detail = "; ".join(errors) if errors else "no backend available"

        # Toast only once per missing period
        if not source_missing_notified:
            toast("12VHPWR Guard", "No pin sensor source. Start HWiNFO, or check the GPU.")
            source_missing_notified = True
            source_connected_notified = False
            last_source_reminder = now

        # Log only once per missing period (and optional slow reminders)
        if not source_missing_logged:
            logger.warning(f"No sensor source available: {detail}")
            source_missing_logged = True
            last_source_log_reminder = now
        elif SOURCE_LOG_REMINDER_EVERY_SEC and (now - last_source_log_reminder) >= SOURCE_LOG_REMINDER_EVERY_SEC:
            logger.warning(f"Still waiting for a sensor source: {detail}")
            last_source_log_reminder = now

        # Optional periodic toast reminder (disabled by default)
        if SOURCE_REMINDER_EVERY_SEC and (now - last_source_reminder) >= SOURCE_REMINDER_EVERY_SEC:
            toast("12VHPWR Guard", "Still waiting for a pin sensor source...")
            last_source_reminder = now

        return False

    def maybe_restart_for_sensor_loss() -> bool:
        """End the process when the direct sensor path has been gone too long.

        A driver update kills the NVAPI session the direct backend holds and nothing
        in this process ever gets it back (PR #1: 131 failed reconnects in a row, and
        a blind guard until the task was restarted by hand). A new process gets a new
        driver session, so past the window this exits with RESTART_EXIT_CODE and the
        supervisor process starts the guard again.

        Only for a session where the direct backend actually connected at some point.
        An unsupported card, or a `sensor_backend = hwinfo` install, keeps waiting
        purple exactly as before, so closing HWiNFO can never turn into a restart
        loop. A session that ran on the direct sensor, fell back to HWiNFO and then
        lost that too does restart: it has no source at all, and the direct path is
        the one a fresh process can fix.

        At most one restart per outage by construction, because the process ends.
        """
        if restart_after <= 0 or not direct_seen or source_lost_at is None:
            return False
        if (time.time() - source_lost_at) < restart_after:
            return False

        msg = (f"Restarting to recover the sensor path: no pin-current source for "
               f"{restart_after:g} s after the direct sensor was lost")
        logger.error(msg)
        if HAVE_EVENTLOG:
            eventlog_write(win32con.EVENTLOG_ERROR_TYPE, 9002, msg)
        toast("12VHPWR Guard - restarting",
              f"No sensor for {_span_text(restart_after)}, restarting to recover it.")
        request_exit(RESTART_EXIT_CODE, stop_event)
        return True

    try:
        while not stop_event.is_set():
            st = get_state()
            if st.get("paused"):
                # Checked before connecting so a paused guard stays quiet rather than
                # toasting about HWiNFO being absent.
                now_paused = time.time()
                if paused_since is None:
                    paused_since = now_paused
                    last_pause_reminder = now_paused
                    # Nothing is being watched from here on, so a half-formed
                    # advisory must not survive the pause either.
                    advisor.reset()
                    set_state(advisory=None, sensor_path_fault=False)
                if response.engaged:
                    # Pausing hands the decision back to the user. Leaving their GPU
                    # crippled while no longer watching it is not ours to do.
                    response.release(now_paused, "monitoring paused", arm_retrigger=False)
                if (PAUSE_REMINDER_EVERY_SEC
                        and (now_paused - last_pause_reminder) >= PAUSE_REMINDER_EVERY_SEC):
                    minutes = int((now_paused - paused_since) / 60)
                    msg = (f"Monitoring has been paused for {minutes} minutes - the card "
                           f"is unprotected until you resume from the tray.")
                    logger.warning(msg)
                    toast("12VHPWR Guard - still paused", msg)
                    last_pause_reminder = now_paused
                set_state(status=Status.PAUSED, last_message="Paused")
                time.sleep(0.5)
                continue

            if paused_since is not None:
                minutes = int((time.time() - paused_since) / 60)
                logger.info(f"Monitoring resumed after {minutes} min paused.")
                paused_since = None
                # Time spent paused is not time spent trying to reconnect, so it must
                # not count towards the self-restart. Starting the clock over is the
                # simple version of that, and it errs towards waiting longer.
                if source_lost_at is not None:
                    source_lost_at = time.time()

            # (Re)connect whenever we have no live data source.
            if backend is None:
                if not try_connect():
                    # Nothing in this process is going to fix a driver session that
                    # died, so after long enough a fresh one is the only answer.
                    if maybe_restart_for_sensor_loss():
                        break
                    stop_event.wait(RECONNECT_RETRY_SEC)
                    continue

                # A new connection starts its timers from scratch.
                tier1.reset()
                pins_missing = False
                last_sample_time = None
                # Protected again, whichever backend answered: an HWiNFO fallback is
                # a source, so it stops the restart clock the same way.
                source_lost_at = None
                if getattr(backend, "key", "") == AstralBackend.key:
                    direct_seen = True
                # The advisor and the state it drives are always cleared together, so
                # a purple hold can never outlive the episode that asked for it.
                advisor.reset()
                set_state(advisory=None, sensor_path_fault=False)

            try:
                pins = backend.read_pins()

                # A long gap means the machine slept or badly stalled, not that current
                # was high the whole time. Sensors can also report nonsense right after
                # resume, so never carry a sustained timer across the gap.
                sample_now = time.time()
                if last_sample_time is not None and (sample_now - last_sample_time) > RESUME_GRACE_SEC:
                    gap = sample_now - last_sample_time
                    logger.warning(
                        f"Sample gap {gap:.1f}s detected (suspend/resume?) - sustained timer reset."
                    )
                    tier1.reset()
                    advisor.reset()
                    set_state(advisory=None, sensor_path_fault=False)
                    # Belt and braces: a successful read means a successful connect
                    # cleared this already, but nothing measured across a suspend is
                    # allowed to survive it.
                    source_lost_at = None
                    # Readings right after a resume are not trustworthy, so anything
                    # decided on the strength of them is dropped too.
                    if response.engaged:
                        response.release(sample_now, "suspend/resume gap",
                                         arm_retrigger=False)
                    response.reset_timers()
                last_sample_time = sample_now

                if not pins:
                    if not pins_missing:
                        pins_missing = True
                        msg = "Pin sensors not found (name mismatch or sensors not active)."
                        logger.warning(msg)
                        if HAVE_EVENTLOG:
                            eventlog_write(win32con.EVENTLOG_WARNING_TYPE, 2001, msg)
                        toast("12VHPWR Guard", msg)
                    set_state(status=Status.MISSING, last_message="Sensors not found")
                    time.sleep(2)
                    continue
                else:
                    if pins_missing:
                        pins_missing = False
                        msg = "Pin sensors found again. Monitoring resumed."
                        logger.info(msg)
                        if HAVE_EVENTLOG:
                            eventlog_write(win32con.EVENTLOG_INFORMATION_TYPE, 2002, msg)
                        toast("12VHPWR Guard", msg)

                crosscheck.maybe_log(logger, backend, pins)
                flight.record(pins)

                # Warn only, and completely separate from the threshold logic below:
                # nothing the advisor says ever engages the mitigator or shuts down.
                # The advisor runs on every tick whatever `advisories` is set to: the
                # flag goes in as an argument and silences the two cable advisories
                # only, never the sensor-path check.
                for adv in advisor.evaluate(
                    sample_now, pins,
                    board_power_w=board_power_watts(),
                    advisories_enabled=get_advisory_values()["advisories_enabled"],
                ):
                    render_advisory(adv)

                max_label, max_val, max_unit = max(pins, key=lambda x: x[1])
                set_state(max_label=max_label, max_val=max_val, max_unit=max_unit)

                # Get current config values (thread-safe)
                threshold_amps, sustained_seconds_required = get_config_values()
                
                now = time.time()

                # The status goes up first, then the tier 1 timer has its say (its
                # warning, progress and cleared lines), the order it always ran in.
                branch = ThresholdTracker.classify(max_val, threshold_amps)
                if branch == THRESHOLD_OVER:
                    set_state(status=Status.OVER, last_message=f"Over: {max_label} {max_val:.2f}{max_unit}")
                elif branch == THRESHOLD_CLEARED:
                    set_ok_status(f"OK (max {max_val:.2f}{max_unit})")
                else:
                    set_ok_status(f"OK (near threshold: {max_val:.2f}{max_unit})")

                # Set by the tier 1 sustained timer, consumed by the response ladder
                # below. What happens when it expires is the ladder's call: limit the
                # GPU first if it can, shut down if it cannot or if limiting does not
                # bring the current back down.
                tick = tier1.update(now, max_label, max_val, max_unit,
                                    threshold_amps, sustained_seconds_required)
                tier1_expired = tick.tier1_expired
                tier1_reason = tick.tier1_reason

                # The response ladder decides what a tier crossing costs: a GPU
                # slowdown, or the machine. It runs every tick, including while a
                # mitigation is already engaged.
                was_engaged = response.engaged
                shutdown_reason = response.evaluate(
                    now=now,
                    max_val=max_val,
                    max_label=max_label,
                    max_unit=max_unit,
                    threshold=threshold_amps,
                    tier1_expired=tier1_expired,
                    tier1_reason=tier1_reason,
                )

                if response.engaged:
                    set_state(
                        status=Status.MITIGATED,
                        last_message=f"GPU limited: {max_label} {max_val:.2f}{max_unit}",
                    )
                    if not was_engaged and not shutdown_reason:
                        # The minute leading up to the engagement is the evidence.
                        # When engage and shutdown land in the same tick (tier 3,
                        # persistent-fault re-trigger), the shutdown dump below is
                        # the same data under the more accurate name.
                        flight.dump(logger, "engaged")

                if shutdown_reason:
                    set_state(status=Status.SHUTDOWN, last_message="Shutting down...")
                    shutting_down = True
                    flight.dump(logger, "shutdown")
                    shutdown_windows(logger, shutdown_reason)
                    stop_event.set()
                    break

                time.sleep(POLL_INTERVAL_SEC)

            except TransientReadError as ex:
                # One bad sample. Skip the tick without disturbing any timer; the
                # backend escalates to SensorGoneError if it keeps happening.
                logger.debug(f"Transient read error: {ex}")
                time.sleep(POLL_INTERVAL_SEC)

            except SensorGoneError as ex:
                # The source died. Without this a stale HWiNFO mapping would keep
                # returning last-known values and the guard would report OK forever.
                msg = f"Sensor source lost: {ex}"
                logger.warning(msg)
                if HAVE_EVENTLOG:
                    eventlog_write(win32con.EVENTLOG_WARNING_TYPE, 1004, msg)
                toast("12VHPWR Guard", "Sensor source lost. Waiting to reconnect...")
                if tier1.over_start is not None:
                    logger.warning("Sustained over-threshold timer reset by sensor disconnect.")
                # The advisory timers go with it: a condition seen before a blind
                # spell was not observed across it.
                advisor.reset()
                set_state(advisory=None, sensor_path_fault=False)
                if response.engaged:
                    # Deliberately stays engaged. We limited the card because the
                    # current was dangerous, and losing the ability to measure is not
                    # evidence that it stopped being dangerous. It releases normally
                    # once readings come back and stay clean.
                    logger.warning(
                        "GPU stays limited while the sensor is gone; it will be "
                        "released once clean readings return."
                    )

                set_state(status=Status.ERROR, last_message="Sensor source lost")

                # Releasing handles lets a dead HWiNFO section go away, so a restarted
                # HWiNFO can create a fresh one for us to re-open.
                try:
                    backend.close()
                except Exception:
                    pass
                backend = None
                last_sample_time = None
                set_state(source="")

                # The guard is blind as of now. If it is still blind this long from
                # here, and the direct sensor is what it lost, the process restarts.
                source_lost_at = time.time()

                # Re-arm the one-shot notifications for the next outage/recovery.
                source_missing_notified = False
                source_missing_logged = False
                source_connected_notified = False
                continue

            except Exception as ex:
                logger.error(f"Loop error: {ex}")
                if HAVE_EVENTLOG:
                    eventlog_write(win32con.EVENTLOG_ERROR_TYPE, 9001, f"Loop error: {ex}")
                set_state(status=Status.ERROR, last_message=f"Error: {ex}")
                time.sleep(1)

    finally:
        crosscheck.close()
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass

        if mitigator is not None:
            if shutting_down and response.engaged:
                # The machine is powering off in seconds and the card should stay
                # limited until it does. The marker file left behind is what makes the
                # next start put everything back, if the reboot itself does not.
                # Gated on engaged: a tiered-but-unavailable session that shuts down
                # never engaged anything, and must not log a claim about a marker
                # file that was never written.
                logger.info("Leaving the GPU limited through shutdown; the marker file "
                            "will trigger a restore on the next start.")
            else:
                try:
                    mitigator.close()
                except Exception:
                    pass

        logger.info("12VHPWR Guard stopped.")
        if HAVE_EVENTLOG:
            eventlog_write(win32con.EVENTLOG_INFORMATION_TYPE, 1003, "12VHPWR Guard stopped.")

# =========================
# Settings dialogs (Tk thread with queue for responsiveness)
# =========================
try:
    import tkinter as tk
    from tkinter import simpledialog, messagebox
    HAVE_TKINTER = True
except Exception:
    HAVE_TKINTER = False

class DialogService:
    """Dedicated Tk thread service for dialogs to keep tray UI responsive."""
    def __init__(self):
        if not HAVE_TKINTER:
            self.ready = threading.Event()
            self.ready.set()  # Mark as ready even without tkinter
            return
        
        self.req_q = queue.Queue()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.ready.wait(timeout=5)

    def _run(self):
        """Run the Tk mainloop in a dedicated thread."""
        if not HAVE_TKINTER:
            return
        
        self.tk = tk
        self.simpledialog = simpledialog
        self.messagebox = messagebox

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.attributes("-topmost", True)
        self.root.update_idletasks()
        self.ready.set()

        def pump():
            """Pump queue tasks into Tk event loop."""
            try:
                while True:
                    fn = self.req_q.get_nowait()
                    fn()
            except queue.Empty:
                pass
            self.root.after(50, pump)

        self.root.after(50, pump)
        self.root.mainloop()

    def _center_owner(self, owner, root):
        """Center the owner window on screen before showing dialog."""
        root.update_idletasks()
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        # owner is invisible, so 1x1 is fine
        x = int((sw - 1) / 2)
        y = int((sh - 1) / 2)
        owner.geometry(f"1x1+{x}+{y}")

    def _make_invisible_owner(self):
        """Invisible owner window so a dialog gets focus and sits above other windows."""
        owner = self.tk.Toplevel(self.root)
        owner.withdraw()
        owner.overrideredirect(True)          # no border/title
        owner.attributes("-alpha", 0.0)       # invisible
        owner.attributes("-topmost", True)    # force above other windows

        self._center_owner(owner, self.root)

        # Show owner invisibly so it can own the dialog and take focus
        owner.deiconify()
        owner.lift()
        owner.focus_force()

        # Drop topmost shortly after to avoid "always on top" issues
        owner.after(200, lambda: owner.attributes("-topmost", False))
        return owner

    @staticmethod
    def _destroy(owner) -> None:
        try:
            owner.destroy()
        except Exception:
            pass

    def _show_error(self, title: str, msg: str) -> None:
        owner = self._make_invisible_owner()
        try:
            self.messagebox.showerror(title, msg, parent=owner)
        finally:
            self._destroy(owner)

    def ask_float(self, title: str, prompt: str, initial: float) -> Optional[float]:
        """Ask for a float value via dialog. Returns None if cancelled or invalid."""
        if not HAVE_TKINTER:
            return None
        
        result_holder = {"val": None}
        done = threading.Event()

        def task():
            try:
                owner = self._make_invisible_owner()
                try:
                    # IMPORTANT: parent dialog to owner, not root
                    s = self.simpledialog.askstring(title, prompt, initialvalue=str(initial), parent=owner)
                finally:
                    self._destroy(owner)

                if s is None:
                    result_holder["val"] = None
                else:
                    v = float(s)
                    if v <= 0:
                        self._show_error("Invalid Value", "Value must be greater than 0.")
                        result_holder["val"] = None
                    else:
                        result_holder["val"] = v
            except ValueError:
                try:
                    self._show_error("Invalid Value", "Please enter a valid number.")
                except Exception:
                    pass
                result_holder["val"] = None
            except Exception:
                result_holder["val"] = None
            finally:
                done.set()

        self.req_q.put(task)
        done.wait()
        return result_holder["val"]

    def show_error(self, title: str, msg: str) -> None:
        """Show an error dialog, safe to call from any thread.

        _show_error touches Tk directly and is only safe on the Tk thread, which
        is why its existing caller runs inside a queued task. This marshals the
        same way rather than reaching into it from the caller's thread.
        """
        if not HAVE_TKINTER:
            return

        done = threading.Event()

        def task():
            try:
                self._show_error(title, msg)
            except Exception:
                pass
            finally:
                done.set()

        self.req_q.put(task)
        done.wait()

    def confirm(self, title: str, msg: str) -> bool:
        """Show a yes/no confirmation dialog. Returns True if yes, False if no."""
        if not HAVE_TKINTER:
            return False
        
        result_holder = {"val": False}
        done = threading.Event()

        def task():
            try:
                owner = self._make_invisible_owner()
                try:
                    result_holder["val"] = bool(self.messagebox.askyesno(title, msg, parent=owner))
                finally:
                    self._destroy(owner)
            except Exception:
                result_holder["val"] = False
            finally:
                done.set()

        self.req_q.put(task)
        done.wait()
        return result_holder["val"]

# Global dialog service instance (created in main())
dialog = None

# Held for the process lifetime so Windows keeps the single-instance mutex alive
_single_instance_handle = None

def acquire_single_instance() -> bool:
    """False when another copy of the guard already holds the mutex."""
    global _single_instance_handle
    ctypes.set_last_error(0)
    _single_instance_handle = CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
    err = ctypes.get_last_error()
    # A mutex created by an elevated guard under another account cannot be opened by
    # a standard user: that is ACCESS_DENIED with no handle, and it still means held.
    return bool(_single_instance_handle) and err not in (ERROR_ALREADY_EXISTS, ERROR_ACCESS_DENIED)

def confirm_unsafe_value(kind: str, new_value: float, default_value: float, unit: str) -> bool:
    """Values at or below the default need no warning; above it, protection is reduced."""
    if new_value <= default_value:
        return True
    if dialog is None:
        return False
    return dialog.confirm(
        "Warning: Reduced Protection",
        f"Raising the {kind} above the default ({default_value:g} {unit}) reduces "
        f"protection and increases the risk of connector overheating, melting, or fire.\n\n"
        f"Requested: {new_value:g} {unit}\n"
        f"Default: {default_value:g} {unit}\n\n"
        f"Apply anyway?"
    )

def log_setting_change(what: str, old, new) -> None:
    """One line and one Event Viewer entry per setting the user actually changed.

    Callers reach here only once the new value is live, so a cancelled dialog, a
    declined warning and a value that did not move all leave no trace. Without
    this the log's first sign of a lowered threshold is the trip it caused, which
    reads like the guard fired on its own.

    LOGGER is None before main() publishes it (and under tests); a tray click must
    not blow up on that, so the line is simply dropped.
    """
    msg = f"Setting changed by user: {what} {old} -> {new}"
    if LOGGER is not None:
        LOGGER.info(msg)
    if HAVE_EVENTLOG:
        eventlog_write(win32con.EVENTLOG_INFORMATION_TYPE, 1010, msg)

def change_threshold_amps(_icon, _item):
    """Open dialog to change threshold amps setting."""
    if dialog is None:
        return

    current_threshold, sustained = get_config_values()
    new_value = dialog.ask_float(
        "Change Threshold Amps",
        f"Enter new threshold current (Amps):\n(Current: {current_threshold:.2f} A)",
        current_threshold
    )
    if new_value is None:
        return
    if not confirm_unsafe_value("threshold", new_value, DEFAULT_THRESHOLD_AMPS, "A"):
        return
    set_config_values(new_value, sustained)
    # Logged on the applied value, not the saved one: a config.ini write that fails
    # still leaves this session monitoring at the new threshold.
    if new_value != current_threshold:
        log_setting_change(
            "threshold", f"{current_threshold:.2f} A", f"{new_value:.2f} A"
        )
    if save_config(new_value, sustained):
        toast("12VHPWR Guard", f"Threshold updated to {new_value:.2f} A")
    else:
        toast("12VHPWR Guard", "Failed to save threshold setting")

def change_sustained_seconds(_icon, _item):
    """Open dialog to change sustained seconds required setting."""
    if dialog is None:
        return
    
    threshold, current_sustained = get_config_values()
    new_value = dialog.ask_float(
        "Change Sustained Seconds Required",
        f"Enter new sustained seconds required:\n(Current: {current_sustained:.1f} s)",
        current_sustained
    )
    if new_value is None:
        return
    if not confirm_unsafe_value(
        "sustained time", new_value, DEFAULT_SUSTAINED_SECONDS_REQUIRED, "s"
    ):
        return
    set_config_values(threshold, new_value)
    if new_value != current_sustained:
        log_setting_change(
            "sustained seconds", f"{current_sustained:.1f} s", f"{new_value:.1f} s"
        )
    if save_config(threshold, new_value):
        toast("12VHPWR Guard", f"Sustained seconds updated to {new_value:.1f} s")
    else:
        toast("12VHPWR Guard", "Failed to save sustained seconds setting")

def reset_to_defaults(_icon, _item):
    """Reset settings to default values."""
    if dialog is None:
        return
    
    ok = dialog.confirm(
        "Reset to Defaults",
        f"Reset settings to default values?\n\nThreshold: {DEFAULT_THRESHOLD_AMPS:.2f} A\nSustained: {DEFAULT_SUSTAINED_SECONDS_REQUIRED:.1f} s"
    )
    if not ok:
        return
    old_threshold, old_sustained = get_config_values()
    saved = reset_config_to_defaults()
    # One line per value that moved, so a reset from defaults is silent rather than
    # two lines saying nothing changed.
    if old_threshold != DEFAULT_THRESHOLD_AMPS:
        log_setting_change(
            "threshold", f"{old_threshold:.2f} A", f"{DEFAULT_THRESHOLD_AMPS:.2f} A"
        )
    if old_sustained != DEFAULT_SUSTAINED_SECONDS_REQUIRED:
        log_setting_change(
            "sustained seconds",
            f"{old_sustained:.1f} s",
            f"{DEFAULT_SUSTAINED_SECONDS_REQUIRED:.1f} s",
        )
    if saved:
        toast("12VHPWR Guard", "Settings reset to defaults")
    else:
        toast("12VHPWR Guard", "Failed to reset settings")

def mitigation_unavailable_reason() -> str:
    """Empty when the GPU can be limited, otherwise why it cannot."""
    state = get_state().get("mitigation", "")
    return "" if (not state or state == "ready") else state

def effective_shutdown_only() -> bool:
    """True when a tier crossing will go straight to shutdown, whatever the setting."""
    if get_response_mode() == RESPONSE_SHUTDOWN_ONLY:
        return True
    return bool(mitigation_unavailable_reason())

def tiered_menu_label(_item=None) -> str:
    reason = mitigation_unavailable_reason()
    if reason:
        return f"Response: Tiered (unavailable - {reason})"
    return "Response: Tiered (limit GPU first, then shutdown)"

def apply_response_mode(mode: str) -> None:
    """Switch modes at runtime. No confirmation: neither direction weakens protection.

    Tiered keeps the session alive where it can, shutdown-only is the blunter of the
    two, and the tier timings apply either way. That is unlike raising a threshold,
    which does reduce protection and still asks.
    """
    current = get_response_mode()
    if current == mode:
        return
    set_response_mode(mode)
    log_setting_change("response mode", current, mode)
    saved = save_setting("response_mode", mode)

    if mode == RESPONSE_TIERED:
        reason = mitigation_unavailable_reason()
        msg = ("Overcurrent will slow the GPU down first."
               if not reason else
               f"Saved, but the GPU cannot be limited right now ({reason}), so "
               f"overcurrent still means shutdown.")
    else:
        msg = "Overcurrent will shut the machine down without limiting the GPU first."
    if not saved:
        msg += " (could not be saved to config.ini)"
    toast("12VHPWR Guard", msg)

def set_mode_tiered(_icon, _item):
    apply_response_mode(RESPONSE_TIERED)

def set_mode_shutdown_only(_icon, _item):
    apply_response_mode(RESPONSE_SHUTDOWN_ONLY)

# =========================
# Tray UI
# =========================
def open_log():
    try:
        os.startfile(LOG_PATH)
    except Exception:
        pass

def toggle_pause(_icon, _item):
    st = get_state()
    was_paused = bool(st.get("paused", False))
    set_state(paused=not was_paused)
    # The loop logs how long a pause lasted once it resumes; this says who started
    # and ended it, and when.
    log_setting_change(
        "monitoring",
        "paused" if was_paused else "running",
        "running" if was_paused else "paused",
    )

def toggle_autostart(_icon, _item):
    state = autostart_state(force=True)

    if state is None:
        if dialog is not None:
            dialog.show_error(
                "Autostart Not Installed",
                "The logon task does not exist yet.\n\n"
                "Run install.bat as administrator once to register it. "
                "This switch then turns it on and off without reinstalling.",
            )
        return

    target = not state
    if set_autostart(target):
        # Only once the task itself reads back changed. A declined UAC prompt or a
        # failed schtasks call leaves the setting alone, so there is nothing to log.
        log_setting_change(
            "autostart", "on" if state else "off", "on" if target else "off"
        )
        toast(
            "12VHPWR Guard",
            f"Start with Windows {'enabled' if target else 'disabled'}.",
        )
    elif dialog is not None:
        dialog.show_error(
            "Could Not Change Autostart",
            "Updating the scheduled task failed.\n\n"
            "Changing it needs administrator rights - approve the UAC prompt, "
            "or run install.bat as administrator.",
        )

def on_exit(icon, _item, stop_event: threading.Event):
    set_state(last_message="Exiting...")
    stop_event.set()
    try:
        icon.stop()
    except Exception:
        pass

def tray_title() -> str:
    st = get_state()
    status = st.get("status", Status.OK)
    paused = st.get("paused", False)
    max_label = st.get("max_label", "")
    max_val = st.get("max_val", 0.0)
    max_unit = st.get("max_unit", "A")
    source = st.get("source", "")
    src = f" | {source}" if source else ""

    # Warn-only, so it rides along with whatever the status already is rather than
    # replacing it.
    advisory = st.get("advisory") or ""
    adv = f" | advisory: {advisory}" if advisory else ""

    if paused:
        return "12VHPWR Guard (Paused)"
    if status == Status.MITIGATED:
        return f"12VHPWR Guard: LIMITED | {max_label} {max_val:.2f}{max_unit}{src}{adv}"
    if status in (Status.OVER, Status.SHUTDOWN):
        return f"12VHPWR Guard: {status} | {max_label} {max_val:.2f}{max_unit}{src}{adv}"
    if status == Status.MISSING:
        return "12VHPWR Guard: Sensors missing"
    if status == Status.ERROR:
        # Purple has always meant "cannot protect". There are two ways to get here
        # now, so say which one: no readings at all, or readings that carry nothing.
        if st.get("sensor_path_fault"):
            detail = advisory or "the pin sensors see no current"
            return f"12VHPWR Guard: NOT PROTECTED | {detail}{src}"
        return "12VHPWR Guard: Waiting for pin sensors"

    # Protection must never weaken silently: if a tier crossing is going straight to
    # shutdown, the tooltip says so even when the setting says otherwise.
    mode = " | shutdown-only" if effective_shutdown_only() else ""
    return f"12VHPWR Guard: OK | Max {max_val:.2f}{max_unit}{src}{adv}{mode}"

def run_tray(stop_event: threading.Event):
    global TRAY_ICON
    if not HAVE_TRAY:
        raise RuntimeError("pystray/Pillow not installed. Install: pip install pystray pillow")

    # Build menu with settings
    menu_items = [
        item(f"12VHPWR Guard v{APP_VERSION}", lambda *_: None, enabled=False),
        pystray.Menu.SEPARATOR,
        item("Open Log", lambda _icon, _item: open_log()),
        pystray.Menu.SEPARATOR,
        item("Settings", pystray.Menu(
            item("Change Threshold Amps...", change_threshold_amps),
            item("Change Sustained Seconds Required...", change_sustained_seconds),
            pystray.Menu.SEPARATOR,
            item(tiered_menu_label, set_mode_tiered, radio=True,
                 checked=lambda _i: get_response_mode() == RESPONSE_TIERED),
            item("Response: Shutdown only", set_mode_shutdown_only, radio=True,
                 checked=lambda _i: get_response_mode() == RESPONSE_SHUTDOWN_ONLY),
            pystray.Menu.SEPARATOR,
            item(
                "Start with Windows",
                toggle_autostart,
                checked=lambda _item: autostart_state() is True,
            ),
            pystray.Menu.SEPARATOR,
            item("Reset to Defaults", reset_to_defaults),
        )),
        pystray.Menu.SEPARATOR,
        item(lambda _item: "Resume" if get_state().get("paused") else "Pause", toggle_pause),
        item("Exit", lambda _icon, _item: on_exit(_icon, _item, stop_event)),
    ]
    
    icon = pystray.Icon(
        "12VHPWR Guard",
        make_icon(Status.MISSING),
        title=tray_title(),
        menu=pystray.Menu(*menu_items),
    )

    def updater():
        # The icon image is re-pushed every ICON_RESYNC_TICKS even without a status
        # change, not just on transitions. A one-shot update can be lost while
        # Explorer is still building the taskbar at logon (the re-registered tray
        # icon resurrects the startup image); the tooltip already self-heals because
        # it is reassigned every tick, and the image must do the same.
        ICON_RESYNC_TICKS = 10
        images = {}
        last_status = None
        ticks_since_push = 0
        while not stop_event.is_set():
            st = get_state()
            status = Status.PAUSED if st.get("paused") else st.get("status", Status.OK)

            ticks_since_push += 1
            if status != last_status or ticks_since_push >= ICON_RESYNC_TICKS:
                if status not in images:
                    images[status] = make_icon(status)
                icon.icon = images[status]
                last_status = status
                ticks_since_push = 0

            icon.title = tray_title()
            time.sleep(1.0)

        try:
            icon.stop()
        except Exception:
            pass

    # Published so the monitor thread can end the process through request_exit().
    # The updater above is the backstop for the moment before this: it watches the
    # stop event and stops the icon itself.
    TRAY_ICON = icon
    threading.Thread(target=updater, daemon=True).start()
    try:
        icon.run()
    finally:
        TRAY_ICON = None

# =========================
# Main
# =========================
def main():
    global dialog, LOGGER

    # Two guards would mean two tray icons and two shutdown commands.
    if not acquire_single_instance():
        toast("12VHPWR Guard", "Already running on this PC (possibly under another Windows account).")
        sys.exit(0)

    logger = setup_logger()
    # Published before the tray starts, so the first setting the user changes is
    # already logged. The monitor thread below gets the same instance.
    LOGGER = logger
    last_exit = os.environ.get(SUPERVISOR_LAST_EXIT_ENV)
    if last_exit:
        logger.warning(f"Restarted by the supervisor: the previous guard process exited "
                       f"with code {last_exit}.")
    stop_event = threading.Event()
    watch_supervisor(lambda: _stop_because_supervisor_gone(stop_event, logger))

    # Create dialog service (dedicated Tk thread) before starting tray
    dialog = DialogService()

    t = threading.Thread(target=monitor_loop, args=(stop_event, logger), daemon=True)
    t.start()

    run_tray(stop_event)

    # The tray is gone, but the monitor thread's cleanup is what puts the GPU limit back
    # and logs the stop, and it is a daemon thread: returning straight away ended the
    # process under it, so an Exit while limited left the card at its floor until the
    # next start's recovery. Give it the time it needs, short of the supervisor-gone
    # hard exit.
    stop_event.set()
    t.join(MONITOR_EXIT_JOIN_SEC)

    # 0 for a stop the user asked for, which the supervisor leaves alone.
    # RESTART_EXIT_CODE when the guard asked to be started again.
    sys.exit(pending_exit_code())

# =========================
# Supervisor
# =========================
# Task Scheduler's "restart on failure" only covers a task that fails to launch. A
# guard that started and then died (a crash, a kill, or the self-restart above) is
# never started again by it: measured on the rig 2026-10-28, the guard stayed down
# with LastTaskResult 0xFFFFFFFF and RestartCount 999 configured. So the process the
# task starts is a supervisor: it runs the real guard as a child and starts it again
# whenever it exits with anything but 0.
#
# It does nothing else (no sensor, no GPU, no tray, no log file), so there is next to
# nothing in it that can fail. It stays in this file so the scheduled task, the
# installer and every existing install keep working unchanged.
SUPERVISOR_CHILD_FLAG = "--child"
# Runs the guard in this process with no supervisor, for debugging.
NO_SUPERVISOR_FLAG = "--no-supervisor"
# Tells a restarted guard why it is running, so its log says so.
SUPERVISOR_LAST_EXIT_ENV = "TWELVEVHPWR_GUARD_LAST_EXIT"
# The supervisor's process id, so the guard can end when its supervisor does. Stopping
# the scheduled task ends the supervisor, but the venv launcher lets grandchildren
# break away from its job, so without this the guard outlived it (measured on the rig
# 2026-10-28): an upgrade would have kept the old code running, an uninstall a guard.
SUPERVISOR_PID_ENV = "TWELVEVHPWR_GUARD_SUPERVISOR_PID"
# How long a guard whose supervisor is gone gets to stop cleanly before it is ended.
SUPERVISOR_GONE_HARD_EXIT_SEC = 10.0
# How long main() waits for the monitor thread's cleanup on exit. Under the hard exit
# above, so a supervisor-gone stop still ends cleanly when the cleanup is healthy.
MONITOR_EXIT_JOIN_SEC = 8.0
# Seconds before restarting after an unplanned exit, one step further per crash in a
# row, so a guard that dies at startup cannot spin the CPU. A child that ran for
# SUPERVISOR_STABLE_SEC before dying starts the sequence over.
SUPERVISOR_BACKOFF_SEC = (5.0, 10.0, 20.0, 40.0, 60.0)
SUPERVISOR_STABLE_SEC = 120.0
# A planned restart already waited out its own window inside the guard.
SUPERVISOR_PLANNED_DELAY_SEC = 2.0


def _supervisor_command() -> List[str]:
    return [sys.executable, os.path.abspath(__file__), SUPERVISOR_CHILD_FLAG]


def _spawn_guard(cmd: List[str], env: dict):
    # No creation flags: under pythonw there is no console to hide, and under
    # python.exe the child shares the console so a developer still sees its output.
    return subprocess.Popen(cmd, cwd=BASE_DIR, env=env)


def _announce_restart(code: Optional[int], delay: float) -> None:
    """Event Viewer, plus a toast when the exit was not one the guard planned."""
    planned = code == RESTART_EXIT_CODE
    if code is None:
        msg = f"Could not start the guard process; trying again in {delay:g} s."
    elif planned:
        msg = (f"The guard asked to be restarted (exit code {code}); starting it again "
               f"in {delay:g} s.")
    else:
        msg = (f"The guard process stopped unexpectedly (exit code {code}); starting "
               f"it again in {delay:g} s.")
    if HAVE_EVENTLOG:
        etype = win32con.EVENTLOG_INFORMATION_TYPE if planned else win32con.EVENTLOG_WARNING_TYPE
        eventlog_write(etype, 9003, msg)
    if not planned:
        # The guard's own self-restart already toasted; a crash has nobody else to say it.
        toast("12VHPWR Guard - restarting", msg)


def _guard_shutdown_pending() -> bool:
    return os.path.exists(LAST_SHUTDOWN_PATH)


def supervise(spawn=_spawn_guard, sleep=time.sleep, clock=time.monotonic,
              announce=_announce_restart, command=None,
              shutdown_pending=_guard_shutdown_pending) -> int:
    """Run the guard as a child until it exits with 0. Returns 0."""
    cmd = command or _supervisor_command()
    crashes_in_a_row = 0
    last_exit: Optional[int] = None
    while True:
        env = dict(os.environ)
        env[SUPERVISOR_PID_ENV] = str(os.getpid())
        if last_exit is not None:
            env[SUPERVISOR_LAST_EXIT_ENV] = str(last_exit)
        started = clock()
        try:
            code = spawn(cmd, env).wait()
        except KeyboardInterrupt:
            # Ctrl-C in a console reaches the child too; nothing left to supervise.
            return 0
        except OSError:
            code = None
        if code == 0:
            return 0
        # The guard writes its shutdown note just before it shuts the machine down, and
        # Windows then ends it with a non-zero code. A guard restarted in that gap would
        # announce and delete the note mid-shutdown, and the next boot would never warn.
        if shutdown_pending():
            return 0

        if code == RESTART_EXIT_CODE:
            delay = SUPERVISOR_PLANNED_DELAY_SEC
            crashes_in_a_row = 0
        else:
            if code is not None and clock() - started >= SUPERVISOR_STABLE_SEC:
                crashes_in_a_row = 0
            delay = SUPERVISOR_BACKOFF_SEC[min(crashes_in_a_row, len(SUPERVISOR_BACKOFF_SEC) - 1)]
            crashes_in_a_row += 1
        last_exit = code if code is not None else -1
        try:
            announce(code, delay)
        except Exception:
            pass
        try:
            sleep(delay)
        except KeyboardInterrupt:
            return 0


def _open_process_for_wait(pid: int):
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    SYNCHRONIZE = 0x00100000
    return k32.OpenProcess(SYNCHRONIZE, False, pid)


def _wait_for_process(handle) -> None:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.WaitForSingleObject.restype = wt.DWORD
    k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    k32.CloseHandle.argtypes = [wt.HANDLE]
    INFINITE = 0xFFFFFFFF
    try:
        k32.WaitForSingleObject(handle, INFINITE)
    finally:
        k32.CloseHandle(handle)


def watch_supervisor(on_gone, pid_text: Optional[str] = None,
                     open_process=_open_process_for_wait,
                     wait=_wait_for_process) -> Optional[threading.Thread]:
    """Call on_gone() once the supervisor that started this guard has ended.

    Does nothing for a guard nobody supervises (--no-supervisor, or an older launcher).
    The handle is opened straight away, while the supervisor is certainly alive: it is
    blocked waiting on this very process, so the id cannot have been reused yet. A
    supervisor that cannot be opened is treated as already gone.
    """
    if pid_text is None:
        pid_text = os.environ.get(SUPERVISOR_PID_ENV)
    if not pid_text:
        return None
    try:
        pid = int(pid_text)
    except ValueError:
        return None
    handle = open_process(pid)
    if not handle:
        on_gone()
        return None

    def run():
        wait(handle)
        on_gone()

    t = threading.Thread(target=run, name="supervisor-watch", daemon=True)
    t.start()
    return t


def _stop_because_supervisor_gone(stop_event: threading.Event, logger: logging.Logger) -> None:
    logger.warning("The supervisor process ended (the scheduled task was stopped or the "
                   "supervisor was ended), so the guard is stopping as well.")
    request_exit(0, stop_event)

    def hard_exit():
        # Only reached when the clean stop above is stuck; a clean stop ends the
        # process first and takes this daemon thread with it.
        time.sleep(SUPERVISOR_GONE_HARD_EXIT_SEC)
        os._exit(0)

    threading.Thread(target=hard_exit, name="supervisor-gone-exit", daemon=True).start()


def entry(argv: List[str]) -> int:
    if SUPERVISOR_CHILD_FLAG in argv or NO_SUPERVISOR_FLAG in argv:
        main()
        return 0
    return supervise()


if __name__ == "__main__":
    sys.exit(entry(sys.argv[1:]))

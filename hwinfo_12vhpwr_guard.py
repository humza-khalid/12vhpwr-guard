import ctypes
import ctypes.wintypes as wt
import os
import sys
import time
import subprocess
import threading
import logging
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
TIER_VALUES = dict(DEFAULT_TIER_VALUES)
RESPONSE_MODE = DEFAULT_RESPONSE_MODE

# Which sensor source to use: "auto" prefers the card's own chip and falls back to
# HWiNFO; "astral" and "hwinfo" pin it to one. Set "hwinfo" to roll back the direct
# backend without reinstalling.
DEFAULT_SENSOR_BACKEND = "auto"
VALID_SENSOR_BACKENDS = ("auto", "astral", "hwinfo")
SENSOR_BACKEND = DEFAULT_SENSOR_BACKEND

# A gap this long between samples means suspend/resume or a severe stall, not a
# measurement. Sustained timers are reset rather than trusted across it.
RESUME_GRACE_SEC = 5.0

# These will be loaded from config file or use defaults
THRESHOLD_AMPS = DEFAULT_THRESHOLD_AMPS
POLL_INTERVAL_SEC = 0.5
SUSTAINED_SECONDS_REQUIRED = DEFAULT_SUSTAINED_SECONDS_REQUIRED
CONSECUTIVE_SAMPLES_REQUIRED = 1

# Thread-safe access to configurable values
_config_lock = threading.Lock()

# While over threshold, log progress every N seconds (set 0 to disable)
SUSTAINED_PROGRESS_LOG_EVERY_SEC = 5.0

# Shutdown behavior
SHUTDOWN_DELAY_SEC = 5
SHUTDOWN_FORCE_CLOSE_APPS = True

# Hysteresis / clear
CLEAR_HYSTERESIS_AMPS = 0.2
CLEAR_STABLE_SECONDS = 1.0

# Logs
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "12vhpwr_guard.log")
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 5

# Config file
CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")

# Shown in the tray menu and logged at startup so "which version are you running"
# is answerable without digging through files.
APP_VERSION = "1.1.2"

# Event Viewer source name
EVENT_SOURCE = "12VHPWR Guard"

# Single instance
SINGLE_INSTANCE_MUTEX = r"Local\12VHPWR_Guard_SingleInstance"
ERROR_ALREADY_EXISTS = 183

# =========================
# Config file management (INI format)
# =========================
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
    }
    defaults.update(DEFAULT_TIER_VALUES)

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
        if threshold <= 0 or sustained <= 0:
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
            loaded[key] = value if value > 0 else fallback
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
set_response_mode(_config_data.get("response_mode", DEFAULT_RESPONSE_MODE))
SENSOR_BACKEND = _config_data.get("sensor_backend", DEFAULT_SENSOR_BACKEND)

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

def shutdown_windows(logger: logging.Logger, reason: str) -> None:
    logger.critical(f"SHUTDOWN TRIGGERED: {reason}")
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
class TieredResponse:
    """Everything that happens once a pin crosses a tier threshold.

    Tier 1 detection stays where it always was, in the monitor loop's sustained timer.
    This owns the response to it: slow the GPU down, give that a grace window to work,
    hold it long enough not to flap, release when the current has been clean for a
    while, and escalate to shutdown whenever any of that is not enough.

    evaluate() never shuts the machine down itself. It returns a reason and the caller
    acts on it, which is what lets the tests drive the whole ladder with the shutdown
    stubbed out.
    """

    def __init__(self, get_mitigator, logger: logging.Logger):
        self._get_mitigator = get_mitigator
        self.log = logger

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
        mode = get_response_mode()
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
            if HAVE_EVENTLOG:
                eventlog_write(win32con.EVENTLOG_WARNING_TYPE, 6004, msg)
            toast("12VHPWR Guard", f"GPU limiting unavailable ({reason}). Shutdown-only.")
        return RESPONSE_SHUTDOWN_ONLY

    # -- the ladder ---------------------------------------------------------
    def evaluate(self, now: float, max_val: float, max_label: str, max_unit: str,
                 threshold: float, tier1_expired: bool,
                 tier1_reason: str = "") -> Optional[str]:
        """One tick. Returns a shutdown reason, or None to keep running."""
        tiers = get_tier_values()
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
                if HAVE_EVENTLOG:
                    eventlog_write(win32con.EVENTLOG_WARNING_TYPE, 6001, msg)
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
            if get_response_mode() == RESPONSE_SHUTDOWN_ONLY:
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
        if HAVE_EVENTLOG:
            eventlog_write(win32con.EVENTLOG_WARNING_TYPE, 6002, msg)
        toast("12VHPWR Guard - GPU limited",
              f"Tier {tier} overcurrent. The GPU has been slowed down to protect the "
              f"connector. Save your work and check the cable.")
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
            if HAVE_EVENTLOG:
                eventlog_write(win32con.EVENTLOG_INFORMATION_TYPE, 6003, msg)

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
            window = get_tier_values()["retrigger_window_seconds"]
            msg += (f" Another overcurrent within {window:.0f}s will be treated as a "
                    f"persistent fault and shut the machine down.")
        if not restored:
            msg += " WARNING: the card may not be fully restored, check the log above."

        self.log.warning(msg)
        if HAVE_EVENTLOG:
            eventlog_write(win32con.EVENTLOG_INFORMATION_TYPE, 6005, msg)
        toast("12VHPWR Guard", "GPU restored to normal clocks and power limit.")

    def reset_timers(self) -> None:
        """Drop every in-flight timer. Used after a suspend/resume gap."""
        self.critical_since = None
        self.catastrophic_samples = 0
        self.released_at = None

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
}

def set_state(**kwargs):
    with state_lock:
        shared.update(kwargs)

def get_state():
    with state_lock:
        return dict(shared)

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

    # sustained state
    over_start: Optional[float] = None
    consecutive_over = 0
    clear_start: Optional[float] = None
    last_over_notice = 0.0
    pins_missing = False
    last_sample_time: Optional[float] = None

    # active data source
    backend = None
    crosscheck = CrossCheck()
    backend_classes = candidate_backends(logger)

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

    try:
        while not stop_event.is_set():
            st = get_state()
            if st.get("paused"):
                # Checked before connecting so a paused guard stays quiet rather than
                # toasting about HWiNFO being absent.
                if response.engaged:
                    # Pausing hands the decision back to the user. Leaving their GPU
                    # crippled while no longer watching it is not ours to do.
                    response.release(time.time(), "monitoring paused", arm_retrigger=False)
                set_state(status=Status.PAUSED, last_message="Paused")
                time.sleep(0.5)
                continue

            # (Re)connect whenever we have no live data source.
            if backend is None:
                if not try_connect():
                    stop_event.wait(3)
                    continue

                # A new connection starts its timers from scratch.
                over_start = None
                consecutive_over = 0
                clear_start = None
                last_over_notice = 0.0
                pins_missing = False
                last_sample_time = None

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
                    over_start = None
                    clear_start = None
                    consecutive_over = 0
                    last_over_notice = 0.0
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

                max_label, max_val, max_unit = max(pins, key=lambda x: x[1])
                set_state(max_label=max_label, max_val=max_val, max_unit=max_unit)

                # Get current config values (thread-safe)
                threshold_amps, sustained_seconds_required = get_config_values()
                
                now = time.time()
                over = max_val >= threshold_amps
                cleared = max_val < (threshold_amps - CLEAR_HYSTERESIS_AMPS)

                # Set by the tier 1 sustained timer below, consumed by the response
                # ladder after this block.
                tier1_expired = False
                tier1_reason = ""

                if over:
                    set_state(status=Status.OVER, last_message=f"Over: {max_label} {max_val:.2f}{max_unit}")
                    consecutive_over += 1
                    clear_start = None

                    if over_start is None:
                        over_start = now
                        consecutive_over = 1
                        last_over_notice = 0.0
                        msg = (
                            f"Threshold exceeded: {max_label}={max_val:.2f}{max_unit} >= {threshold_amps:.2f}A. "
                            f"Starting {sustained_seconds_required:.0f}s sustained timer."
                        )
                        logger.warning(msg)
                        if HAVE_EVENTLOG:
                            eventlog_write(win32con.EVENTLOG_WARNING_TYPE, 3001, msg)
                        toast("12VHPWR Guard - Warning", msg)

                    if SUSTAINED_PROGRESS_LOG_EVERY_SEC and (now - last_over_notice) >= SUSTAINED_PROGRESS_LOG_EVERY_SEC:
                        sustained = now - over_start
                        msg = (
                            f"Still over threshold for {sustained:.1f}s: {max_label}={max_val:.2f}{max_unit} "
                            f"(needs {sustained_seconds_required:.1f}s)"
                        )
                        logger.warning(msg)
                        if HAVE_EVENTLOG:
                            eventlog_write(win32con.EVENTLOG_WARNING_TYPE, 3002, msg)
                        last_over_notice = now

                    if consecutive_over >= CONSECUTIVE_SAMPLES_REQUIRED:
                        sustained = now - over_start
                        if sustained >= sustained_seconds_required:
                            # What happens next is the response ladder's call: limit
                            # the GPU first if it can, shut down if it cannot or if
                            # limiting does not bring the current back down.
                            tier1_expired = True
                            tier1_reason = (
                                f"sustained {sustained:.1f}s >= {sustained_seconds_required:.1f}s, "
                                f"{max_label}={max_val:.2f}{max_unit} >= {threshold_amps:.2f}A"
                            )

                elif cleared:
                    set_state(status=Status.OK, last_message=f"OK (max {max_val:.2f}{max_unit})")
                    consecutive_over = 0

                    if clear_start is None:
                        clear_start = now
                    elif (now - clear_start) >= CLEAR_STABLE_SECONDS:
                        if over_start is not None:
                            msg = (
                                f"Load cleared below {threshold_amps - CLEAR_HYSTERESIS_AMPS:.2f}A for "
                                f"{CLEAR_STABLE_SECONDS:.1f}s. Timer reset."
                            )
                            logger.info(msg)
                            if HAVE_EVENTLOG:
                                eventlog_write(win32con.EVENTLOG_INFORMATION_TYPE, 4001, msg)
                            toast("12VHPWR Guard", "Load cleared. Timer reset.")
                        over_start = None
                        last_over_notice = 0.0

                else:
                    set_state(status=Status.OK, last_message=f"OK (near threshold: {max_val:.2f}{max_unit})")
                    consecutive_over = 0
                    clear_start = None

                # The response ladder decides what a tier crossing costs: a GPU
                # slowdown, or the machine. It runs every tick, including while a
                # mitigation is already engaged.
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

                if shutdown_reason:
                    set_state(status=Status.SHUTDOWN, last_message="Shutting down...")
                    shutting_down = True
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
                if over_start is not None:
                    logger.warning("Sustained over-threshold timer reset by sensor disconnect.")
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
            if shutting_down:
                # The machine is powering off in seconds and the card should stay
                # limited until it does. The marker file left behind is what makes the
                # next start put everything back, if the reboot itself does not.
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
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS

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
    if reset_config_to_defaults():
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
    if get_response_mode() == mode:
        return
    set_response_mode(mode)
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
    set_state(paused=not st.get("paused", False))

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

    if paused:
        return "12VHPWR Guard (Paused)"
    if status == Status.MITIGATED:
        return f"12VHPWR Guard: LIMITED | {max_label} {max_val:.2f}{max_unit}{src}"
    if status in (Status.OVER, Status.SHUTDOWN):
        return f"12VHPWR Guard: {status} | {max_label} {max_val:.2f}{max_unit}{src}"
    if status == Status.MISSING:
        return "12VHPWR Guard: Sensors missing"
    if status == Status.ERROR:
        return "12VHPWR Guard: Waiting for pin sensors"

    # Protection must never weaken silently: if a tier crossing is going straight to
    # shutdown, the tooltip says so even when the setting says otherwise.
    mode = " | shutdown-only" if effective_shutdown_only() else ""
    return f"12VHPWR Guard: OK | Max {max_val:.2f}{max_unit}{src}{mode}"

def run_tray(stop_event: threading.Event):
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

    threading.Thread(target=updater, daemon=True).start()
    icon.run()

# =========================
# Main
# =========================
def main():
    global dialog

    # Two guards would mean two tray icons and two shutdown commands.
    if not acquire_single_instance():
        toast("12VHPWR Guard", "Already running.")
        sys.exit(0)

    logger = setup_logger()
    stop_event = threading.Event()

    # Create dialog service (dedicated Tk thread) before starting tray
    dialog = DialogService()

    t = threading.Thread(target=monitor_loop, args=(stop_event, logger), daemon=True)
    t.start()

    run_tray(stop_event)

if __name__ == "__main__":
    main()

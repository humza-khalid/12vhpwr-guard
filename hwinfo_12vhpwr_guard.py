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
DRIVER_RESTART_COOLDOWN_SEC = 60.0

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
    if not os.path.exists(CONFIG_PATH):
        return {
            "threshold_amps": DEFAULT_THRESHOLD_AMPS,
            "sustained_seconds_required": DEFAULT_SUSTAINED_SECONDS_REQUIRED,
            "sensor_backend": DEFAULT_SENSOR_BACKEND,
        }

    try:
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH, encoding="utf-8")
        
        # Get values from [Settings] section, with defaults
        threshold = config.getfloat("Settings", "threshold_amps", fallback=DEFAULT_THRESHOLD_AMPS)
        sustained = config.getfloat("Settings", "sustained_seconds_required", fallback=DEFAULT_SUSTAINED_SECONDS_REQUIRED)
        backend = config.get("Settings", "sensor_backend", fallback=DEFAULT_SENSOR_BACKEND).strip().lower()

        # Validate
        if threshold <= 0 or sustained <= 0:
            raise ValueError("Values must be positive")
        if backend not in VALID_SENSOR_BACKENDS:
            backend = DEFAULT_SENSOR_BACKEND

        return {
            "threshold_amps": threshold,
            "sustained_seconds_required": sustained,
            "sensor_backend": backend,
        }
    except Exception:
        # If config is corrupted or missing section, return defaults
        return {
            "threshold_amps": DEFAULT_THRESHOLD_AMPS,
            "sustained_seconds_required": DEFAULT_SUSTAINED_SECONDS_REQUIRED,
            "sensor_backend": DEFAULT_SENSOR_BACKEND,
        }

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

def get_config_values() -> Tuple[float, float]:
    """Thread-safe getter for current config values."""
    with _config_lock:
        return THRESHOLD_AMPS, SUSTAINED_SECONDS_REQUIRED

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

    def read_pins(self) -> List[Tuple[str, float, str]]:
        return self.sensor.read_pins()

    def close(self) -> None:
        if self.sensor is not None:
            self.sensor.close()
            self.sensor = None


class CrossCheck:
    """Observational only: when running direct, periodically compare against HWiNFO.

    Never influences a shutdown decision. Silently does nothing if HWiNFO is absent.
    """

    def __init__(self, every_sec: float = 60.0):
        self.every_sec = every_sec
        self._next = 0.0
        self._handles = None

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
            logger.info(
                f"Crosscheck: direct max {direct_max:.2f}A vs HWiNFO max {hwinfo_max:.2f}A "
                f"(worst per-pin delta {worst:.2f}A)"
            )
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

def restart_gpu_driver(logger: logging.Logger, reason: str) -> None:
    logger.critical(f"DRIVER RESTART TRIGGERED: {reason}")
    toast("12VHPWR Guard - Driver Restart", reason)
    if HAVE_EVENTLOG:
        eventlog_write(win32con.EVENTLOG_WARNING_TYPE, 5091, reason)

    # Use PowerShell to disable and enable the display adapter
    cmd = [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "Get-PnpDevice -Class Display | Disable-PnpDevice -Confirm:$false; Start-Sleep -Seconds 2; Get-PnpDevice -Class Display | Enable-PnpDevice -Confirm:$false"
    ]
    kwargs = {}
    if os.name == 'nt':
        kwargs['creationflags'] = 0x08000000
    subprocess.run(cmd, check=False, **kwargs)

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
def monitor_loop(stop_event: threading.Event, logger: logging.Logger):
    eventlog_register_source(logger)

    threshold_amps, sustained_seconds_required = get_config_values()
    logger.info("Starting 12VHPWR Guard (tray mode)...")
    logger.info(f"Log file: {LOG_PATH}")
    logger.info(
        f"Threshold={threshold_amps:.2f}A | Poll={POLL_INTERVAL_SEC}s | Sustained={sustained_seconds_required}s | "
        f"ForceClose={SHUTDOWN_FORCE_CLOSE_APPS}"
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
    last_driver_restart = 0.0

    # active data source
    backend = None
    crosscheck = CrossCheck()
    backend_classes = candidate_backends(logger)
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

                if over:
                    set_state(status=Status.OVER, last_message=f"Over: {max_label} {max_val:.2f}{max_unit}")
                    consecutive_over += 1
                    clear_start = None

                    if over_start is None:
                        over_start = now
                        consecutive_over = 1
                        last_over_notice = 0.0
                        msg = (
                            f"Threshold exceeded: {max_label}={max_val:.2f}{max_unit} ≥ {threshold_amps:.2f}A. "
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
                            if now - last_driver_restart > DRIVER_RESTART_COOLDOWN_SEC:
                                reason = (
                                    f"Sustained {sustained:.1f}s ≥ {sustained_seconds_required:.1f}s. "
                                    f"Restarting GPU driver to cut load."
                                )
                                restart_gpu_driver(logger, reason)
                                last_driver_restart = now
                                over_start = now
                                last_over_notice = now
                            else:
                                reason = (
                                    f"Sustained {sustained:.1f}s ≥ {sustained_seconds_required:.1f}s after driver restart. "
                                    f"{max_label}={max_val:.2f}{max_unit} ≥ {threshold_amps:.2f}A"
                                )
                                set_state(status=Status.SHUTDOWN, last_message="Shutting down...")
                                shutdown_windows(logger, reason)
                                stop_event.set()
                                break

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
    if status in (Status.OVER, Status.SHUTDOWN):
        return f"12VHPWR Guard: {status} | {max_label} {max_val:.2f}{max_unit}{src}"
    if status == Status.MISSING:
        return "12VHPWR Guard: Sensors missing"
    if status == Status.ERROR:
        return "12VHPWR Guard: Waiting for pin sensors"
    return f"12VHPWR Guard: OK | Max {max_val:.2f}{max_unit}{src}"

def run_tray(stop_event: threading.Event):
    if not HAVE_TRAY:
        raise RuntimeError("pystray/Pillow not installed. Install: pip install pystray pillow")

    # Build menu with settings
    menu_items = [
        item("Open Log", lambda _icon, _item: open_log()),
        pystray.Menu.SEPARATOR,
        item("Settings", pystray.Menu(
            item("Change Threshold Amps...", change_threshold_amps),
            item("Change Sustained Seconds Required...", change_sustained_seconds),
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

"""
Reversible GPU slowdown for 12VHPWR Guard.

When a pin goes over threshold the guard's first response is to make the card stop
pulling current, which happens in about a second, instead of shutting the machine down,
which takes far longer and costs the user their session. This module owns that action:
lock the graphics clock to its floor and drop the power limit to the card's minimum,
then put both back exactly as they were.

Nothing here touches I2C. The sensor side stays read-only; this is the NVIDIA management
API (NVML), the same documented calls behind "nvidia-smi -lgc" and "nvidia-smi -pl".

=============================================================================
VERIFIED ON THE RIG (2026-08-06, ROG Astral RTX 5090 OC, driver 610.88)
  nvml.dll loads from System32 via plain ctypes.WinDLL("nvml.dll").
  nvmlDeviceGetPciInfo_v3().pciSubSystemId reports 0x89E31043 - the same DWORD format
  as the NVAPI allowlist in astral_i2c.py, so one physical card can be matched across
  both APIs by subsystem id.
  Power limit constraints on this card: min 400 W, max 600 W (API is milliwatts).
  400 W alone is NOT enough: balanced, that is still ~5.5 A per pin, and a bad contact
  can carry far more than its share. The clock lock is the lever that actually works -
  idle sits at 412 MHz and 0.3-0.9 A per pin. Both are applied together.
  Set calls require an elevated process on Windows. The installed scheduled task runs
  at RunLevel Highest; a hand-launched guard will not be able to mitigate.
=============================================================================

Both changes are volatile: a driver reload or reboot clears them. The marker file
handles the case that is not self-healing, a crash while mitigated followed by a
restart on the same driver session.
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import time
from typing import Optional, Tuple

# ------------------------------------------------------------------ NVML ids
NVML_SUCCESS = 0
NVML_ERROR_INVALID_ARGUMENT = 2
NVML_ERROR_NOT_SUPPORTED = 3
NVML_ERROR_NO_PERMISSION = 4
NVML_ERROR_INSUFFICIENT_SIZE = 7
NVML_ERROR_DRIVER_NOT_LOADED = 9

NVML_CLOCK_GRAPHICS = 0
NVML_DEVICE_NAME_BUFFER_SIZE = 96
NVML_MAX_CLOCK_ENTRIES = 256

# Clock floors to try, in order, if the driver refuses the enumerated minimum. Each
# step is higher and therefore less effective, so this only ever walks upward as a
# last resort - a mitigation that fails outright would leave the guard with nothing
# but the shutdown path.
CLOCK_FLOOR_LADDER = (500, 700, 1000)

# Written next to config.ini while mitigation is active, deleted on release.
MARKER_NAME = "mitigation.active"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MARKER_PATH = os.path.join(BASE_DIR, MARKER_NAME)


class NvmlError(RuntimeError):
    def __init__(self, func: str, code: int):
        super().__init__(f"{func} failed with NVML status {code}")
        self.func = func
        self.code = code


class NvmlPciInfo(ctypes.Structure):
    """nvmlPciInfo_t as used by nvmlDeviceGetPciInfo_v3."""
    _fields_ = [
        ("busIdLegacy", ctypes.c_char * 16),
        ("domain", ctypes.c_uint32),
        ("bus", ctypes.c_uint32),
        ("device", ctypes.c_uint32),
        ("pciDeviceId", ctypes.c_uint32),
        ("pciSubSystemId", ctypes.c_uint32),
        ("busId", ctypes.c_char * 32),
    ]


def is_process_elevated() -> bool:
    """True when this process runs elevated.

    Asked of the process token rather than answered by attempting a "set" call and
    seeing what happens: a probe write is a side effect, and a safety tool does not
    poke the GPU to learn about itself.
    """
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        kernel32.GetCurrentProcess.restype = wt.HANDLE
        advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
        advapi32.OpenProcessToken.restype = wt.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)
        ]
        advapi32.GetTokenInformation.restype = wt.BOOL
        kernel32.CloseHandle.argtypes = [wt.HANDLE]

        TOKEN_QUERY = 0x0008
        TokenElevation = 20

        token = wt.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY,
                                         ctypes.byref(token)):
            raise OSError("OpenProcessToken failed")
        try:
            elevated = ctypes.c_uint32(0)
            returned = wt.DWORD(0)
            ok = advapi32.GetTokenInformation(
                token, TokenElevation, ctypes.byref(elevated),
                ctypes.sizeof(elevated), ctypes.byref(returned)
            )
            if not ok:
                raise OSError("GetTokenInformation failed")
            return bool(elevated.value)
        finally:
            kernel32.CloseHandle(token)
    except Exception:
        # Fall back to the simpler question. Both failing means we assume no rights,
        # which costs a feature rather than silently pretending we have them.
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False


class _Nvml:
    """Thin ctypes binding over the handful of NVML calls the guard needs.

    Loaded lazily so importing this module costs nothing on a machine with no NVIDIA
    driver. Every call raises NvmlError on a non-zero status; the caller decides which
    failures matter.
    """

    def __init__(self):
        self._dll = None
        self._fn = {}
        self._initialized = False

    def _bind(self, name: str, restype, argtypes, required: bool = True):
        try:
            fn = getattr(self._dll, name)
        except AttributeError:
            if required:
                raise RuntimeError(f"{name} missing from nvml.dll (driver too old)")
            return None
        fn.restype = restype
        fn.argtypes = argtypes
        self._fn[name] = fn
        return fn

    def _call(self, name: str, *args) -> None:
        fn = self._fn.get(name)
        if fn is None:
            raise NvmlError(name, NVML_ERROR_NOT_SUPPORTED)
        status = fn(*args)
        if status != NVML_SUCCESS:
            raise NvmlError(name, status)

    def init(self) -> None:
        self._dll = ctypes.WinDLL("nvml.dll")

        c_uint_p = ctypes.POINTER(ctypes.c_uint32)
        handle_p = ctypes.POINTER(ctypes.c_void_p)

        self._bind("nvmlInit_v2", ctypes.c_int, [])
        self._bind("nvmlShutdown", ctypes.c_int, [])
        self._bind("nvmlDeviceGetCount_v2", ctypes.c_int, [c_uint_p])
        self._bind("nvmlDeviceGetHandleByIndex_v2", ctypes.c_int, [ctypes.c_uint32, handle_p])
        self._bind("nvmlDeviceGetPciInfo_v3", ctypes.c_int,
                   [ctypes.c_void_p, ctypes.POINTER(NvmlPciInfo)])
        self._bind("nvmlDeviceGetName", ctypes.c_int,
                   [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32])
        self._bind("nvmlDeviceGetPowerManagementLimit", ctypes.c_int,
                   [ctypes.c_void_p, c_uint_p])
        self._bind("nvmlDeviceGetPowerManagementLimitConstraints", ctypes.c_int,
                   [ctypes.c_void_p, c_uint_p, c_uint_p])
        self._bind("nvmlDeviceSetPowerManagementLimit", ctypes.c_int,
                   [ctypes.c_void_p, ctypes.c_uint32])
        self._bind("nvmlDeviceSetGpuLockedClocks", ctypes.c_int,
                   [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32])
        self._bind("nvmlDeviceResetGpuLockedClocks", ctypes.c_int, [ctypes.c_void_p])
        self._bind("nvmlDeviceGetClockInfo", ctypes.c_int,
                   [ctypes.c_void_p, ctypes.c_int, c_uint_p])
        # Clock enumeration is optional: GeForce drivers often answer NOT_SUPPORTED,
        # which is why CLOCK_FLOOR_LADDER exists.
        self._bind("nvmlDeviceGetSupportedMemoryClocks", ctypes.c_int,
                   [ctypes.c_void_p, c_uint_p, c_uint_p], required=False)
        self._bind("nvmlDeviceGetSupportedGraphicsClocks", ctypes.c_int,
                   [ctypes.c_void_p, ctypes.c_uint32, c_uint_p, c_uint_p], required=False)

        self._call("nvmlInit_v2")
        self._initialized = True

    def shutdown(self) -> None:
        if self._initialized:
            try:
                self._call("nvmlShutdown")
            except Exception:
                pass
            self._initialized = False
        self._fn = {}
        self._dll = None

    def device_count(self) -> int:
        count = ctypes.c_uint32(0)
        self._call("nvmlDeviceGetCount_v2", ctypes.byref(count))
        return int(count.value)

    def handle(self, index: int):
        dev = ctypes.c_void_p()
        self._call("nvmlDeviceGetHandleByIndex_v2", ctypes.c_uint32(index), ctypes.byref(dev))
        return dev

    def subsystem_id(self, handle) -> int:
        info = NvmlPciInfo()
        self._call("nvmlDeviceGetPciInfo_v3", handle, ctypes.byref(info))
        return int(info.pciSubSystemId)

    def name(self, handle) -> str:
        buf = ctypes.create_string_buffer(NVML_DEVICE_NAME_BUFFER_SIZE)
        self._call("nvmlDeviceGetName", handle, buf, NVML_DEVICE_NAME_BUFFER_SIZE)
        return buf.value.decode("utf-8", errors="replace")

    def power_limit_mw(self, handle) -> int:
        limit = ctypes.c_uint32(0)
        self._call("nvmlDeviceGetPowerManagementLimit", handle, ctypes.byref(limit))
        return int(limit.value)

    def power_limit_constraints_mw(self, handle) -> Tuple[int, int]:
        lo = ctypes.c_uint32(0)
        hi = ctypes.c_uint32(0)
        self._call("nvmlDeviceGetPowerManagementLimitConstraints",
                   handle, ctypes.byref(lo), ctypes.byref(hi))
        return int(lo.value), int(hi.value)

    def set_power_limit_mw(self, handle, milliwatts: int) -> None:
        self._call("nvmlDeviceSetPowerManagementLimit", handle, ctypes.c_uint32(int(milliwatts)))

    def set_locked_clocks(self, handle, min_mhz: int, max_mhz: int) -> None:
        self._call("nvmlDeviceSetGpuLockedClocks", handle,
                   ctypes.c_uint32(int(min_mhz)), ctypes.c_uint32(int(max_mhz)))

    def reset_locked_clocks(self, handle) -> None:
        self._call("nvmlDeviceResetGpuLockedClocks", handle)

    def graphics_clock_mhz(self, handle) -> int:
        clock = ctypes.c_uint32(0)
        self._call("nvmlDeviceGetClockInfo", handle, ctypes.c_int(NVML_CLOCK_GRAPHICS),
                   ctypes.byref(clock))
        return int(clock.value)

    def min_supported_graphics_clock_mhz(self, handle) -> Optional[int]:
        """Lowest graphics clock the driver admits to supporting, or None."""
        try:
            mem_clocks = self._enumerate("nvmlDeviceGetSupportedMemoryClocks", handle)
            if not mem_clocks:
                return None
            lowest = None
            for mem in (mem_clocks[0], mem_clocks[-1]):
                clocks = self._enumerate("nvmlDeviceGetSupportedGraphicsClocks", handle, mem)
                if clocks:
                    candidate = min(clocks)
                    lowest = candidate if lowest is None else min(lowest, candidate)
            return lowest
        except Exception:
            return None

    def _enumerate(self, func: str, handle, *extra):
        """NVML's two-step list protocol: ask for the size, then for the values."""
        fn = self._fn.get(func)
        if fn is None:
            return []
        count = ctypes.c_uint32(0)
        status = fn(handle, *extra, ctypes.byref(count), None)
        if status not in (NVML_SUCCESS, NVML_ERROR_INSUFFICIENT_SIZE):
            return []
        n = min(int(count.value), NVML_MAX_CLOCK_ENTRIES)
        if n <= 0:
            return []
        buf = (ctypes.c_uint32 * n)()
        count.value = n
        if fn(handle, *extra, ctypes.byref(count), buf) != NVML_SUCCESS:
            return []
        return [int(buf[i]) for i in range(min(n, int(count.value)))]


class GpuMitigator:
    """Applies and removes the emergency GPU slowdown.

    connect() never raises: no driver, no rights or no matching card are all normal
    "unavailable" answers, and the guard falls back to shutdown-only protection.
    engage() and release() are idempotent, so the monitor loop can call them on any
    tick without tracking whether they already happened.
    """

    def __init__(self, marker_path: Optional[str] = None, nvml_factory=None,
                 elevation_check=None, logger=None):
        self._marker_path = marker_path or MARKER_PATH
        self._nvml_factory = nvml_factory or _Nvml
        self._elevation_check = elevation_check or is_process_elevated
        self._log = logger

        self._nvml = None
        self._handle = None
        self._saved_power_limit_mw = None
        self._applied_power = False
        self._applied_clocks = False
        self._locked_at = None

        self.gpu_name = None
        self.subsystem_id = None
        self.power_limit_min_mw = None
        self.power_limit_max_mw = None
        self.clock_floor_mhz = None
        self.engaged = False
        self.engaged_reason = None
        self.unavailable_reason = "not connected"

    # -- logging ------------------------------------------------------------
    def _say(self, level: str, msg: str) -> None:
        if self._log is not None:
            getattr(self._log, level)(f"Mitigation: {msg}")

    # -- availability -------------------------------------------------------
    @property
    def is_available(self) -> bool:
        return self._handle is not None and self.unavailable_reason is None

    def connect(self, subsystem_id: Optional[int] = None) -> bool:
        """Bind to the GPU whose pins we are watching. True when mitigation can run."""
        if self.is_available:
            return True

        if not self._elevation_check():
            self.unavailable_reason = "not elevated"
            self._say("warning",
                      "unavailable (guard is not running elevated). Protection falls back "
                      "to shutdown-only. Install as a scheduled task, or run as admin.")
            return False

        nvml = self._nvml_factory()
        try:
            nvml.init()
        except Exception as ex:
            self.unavailable_reason = f"NVML unavailable ({ex})"
            self._say("warning", f"unavailable ({ex}). Protection falls back to shutdown-only.")
            try:
                nvml.shutdown()
            except Exception:
                pass
            return False

        try:
            handle = self._match_gpu(nvml, subsystem_id)
            if handle is None:
                self.unavailable_reason = "no matching NVIDIA GPU"
                self._say("warning", "no matching GPU found. Protection falls back to "
                                     "shutdown-only.")
                nvml.shutdown()
                return False

            self.gpu_name = self._safe(lambda: nvml.name(handle), "NVIDIA GPU")
            self.power_limit_min_mw, self.power_limit_max_mw = nvml.power_limit_constraints_mw(handle)
            self.clock_floor_mhz = nvml.min_supported_graphics_clock_mhz(handle)
        except Exception as ex:
            self.unavailable_reason = f"NVML query failed ({ex})"
            self._say("warning", f"unavailable ({ex}). Protection falls back to shutdown-only.")
            try:
                nvml.shutdown()
            except Exception:
                pass
            return False

        self._nvml = nvml
        self._handle = handle
        self.unavailable_reason = None

        floor = self.clock_floor_mhz if self.clock_floor_mhz else CLOCK_FLOOR_LADDER[0]
        self._say("info",
                  f"ready on {self.gpu_name} - power limit floor "
                  f"{self.power_limit_min_mw / 1000:.0f}W (of {self.power_limit_max_mw / 1000:.0f}W max), "
                  f"clock lock target {floor} MHz"
                  f"{'' if self.clock_floor_mhz else ' (driver did not enumerate clocks)'}")
        return True

    def _match_gpu(self, nvml, subsystem_id: Optional[int]):
        """Find the NVML handle for the card the sensor backend is reading.

        Matched by subsystem id when the caller knows it. Failing that, a known Astral
        id wins, and a machine with exactly one GPU is unambiguous anyway. Several GPUs
        and no identification is the one case where we refuse to guess.
        """
        count = nvml.device_count()
        handles = []
        for i in range(count):
            try:
                handle = nvml.handle(i)
                handles.append((handle, nvml.subsystem_id(handle)))
            except Exception:
                continue

        if subsystem_id is not None:
            for handle, sub in handles:
                if sub == subsystem_id:
                    self.subsystem_id = sub
                    return handle

        try:
            from astral_i2c import ASTRAL_SUBSYSTEM_IDS
        except Exception:
            ASTRAL_SUBSYSTEM_IDS = {}
        for handle, sub in handles:
            if sub in ASTRAL_SUBSYSTEM_IDS:
                self.subsystem_id = sub
                return handle

        if len(handles) == 1:
            self.subsystem_id = handles[0][1]
            return handles[0][0]
        return None

    @staticmethod
    def _safe(fn, fallback):
        try:
            return fn()
        except Exception:
            return fallback

    # -- the actual mitigation ---------------------------------------------
    def engage(self, reason: str = "") -> bool:
        """Lock clocks to the floor and drop the power limit. Idempotent.

        Returns True if the GPU is now limited. A partial result still returns True and
        is logged: the tier engine measures the outcome in amps and escalates to
        shutdown if the current does not actually fall.
        """
        if self.engaged:
            return True
        if not self.is_available:
            return False

        # Capture before changing anything. The user may run a custom power limit and
        # release() has to put back their value, not the card's default.
        try:
            self._saved_power_limit_mw = self._nvml.power_limit_mw(self._handle)
        except Exception as ex:
            self._saved_power_limit_mw = None
            self._say("warning", f"could not read current power limit ({ex})")

        # Clocks first: that is the change that actually moves current, and every
        # millisecond counts once a pin is over threshold.
        applied_clocks = self._lock_clocks()
        applied_power = self._drop_power_limit()

        if not applied_clocks and not applied_power:
            self._say("error", "engage failed, nothing applied - shutdown remains the "
                               "only protection")
            return False

        self._applied_clocks = applied_clocks
        self._applied_power = applied_power
        self.engaged = True
        self.engaged_reason = reason
        self._write_marker()

        self._say("warning",
                  f"ENGAGED ({reason}) - "
                  f"clocks {'locked to ' + str(self._locked_at) + ' MHz' if applied_clocks else 'NOT locked'}, "
                  f"power limit {'set to ' + str(self.power_limit_min_mw // 1000) + 'W' if applied_power else 'unchanged'}")
        return True

    def _lock_clocks(self) -> bool:
        self._locked_at = None
        candidates = []
        if self.clock_floor_mhz:
            candidates.append(self.clock_floor_mhz)
        candidates.extend(CLOCK_FLOOR_LADDER)

        seen = set()
        for mhz in candidates:
            if mhz in seen:
                continue
            seen.add(mhz)
            try:
                self._nvml.set_locked_clocks(self._handle, mhz, mhz)
                self._locked_at = mhz
                return True
            except Exception as ex:
                self._say("warning", f"clock lock at {mhz} MHz refused ({ex}), trying higher")
        self._say("error", "could not lock clocks at any frequency")
        return False

    def _drop_power_limit(self) -> bool:
        if not self.power_limit_min_mw:
            return False
        try:
            self._nvml.set_power_limit_mw(self._handle, self.power_limit_min_mw)
            return True
        except Exception as ex:
            self._say("warning", f"power limit change refused ({ex})")
            return False

    def release(self) -> bool:
        """Undo exactly what engage() changed. Idempotent and safe when not engaged."""
        if not self.engaged:
            self._clear_marker()
            return True

        restored = True

        if self._applied_power:
            if self._saved_power_limit_mw:
                try:
                    self._nvml.set_power_limit_mw(self._handle, self._saved_power_limit_mw)
                except Exception as ex:
                    restored = False
                    self._say("error", f"could not restore power limit ({ex})")
            else:
                # Never invent a value. Leaving the floor in place is visible and
                # fixable; guessing the user's limit is neither.
                restored = False
                self._say("error", "no saved power limit to restore - the card may still "
                                   "be at its minimum limit")

        if self._applied_clocks:
            try:
                self._nvml.reset_locked_clocks(self._handle)
            except Exception as ex:
                restored = False
                self._say("error", f"could not reset locked clocks ({ex})")

        self.engaged = False
        self.engaged_reason = None
        self._applied_power = False
        self._applied_clocks = False
        self._clear_marker()

        if restored:
            self._say("info", "released - clocks unlocked and power limit restored")
        return restored

    # -- crash recovery -----------------------------------------------------
    def startup_recovery(self) -> Optional[str]:
        """Undo a mitigation left behind by a guard that died while engaged.

        Called once at startup, and only when the marker file exists, so a run that
        never mitigates never touches NVML at all. Returns a message if it acted.
        """
        marker = self._read_marker()
        if marker is None:
            return None

        if not self.connect(marker.get("subsystem_id")):
            self._clear_marker()
            return ("Found a leftover mitigation marker but cannot reach the GPU "
                    f"({self.unavailable_reason}). If the card is still limited, reboot "
                    "or reset it with: nvidia-smi -rgc")

        saved = marker.get("saved_power_limit_mw")
        problems = []

        try:
            self._nvml.reset_locked_clocks(self._handle)
        except Exception as ex:
            problems.append(f"clock reset failed ({ex})")

        if marker.get("applied_power_limit"):
            if saved:
                try:
                    self._nvml.set_power_limit_mw(self._handle, int(saved))
                except Exception as ex:
                    problems.append(f"power limit restore failed ({ex})")
            else:
                problems.append("no saved power limit in the marker; the card may still "
                                "be at its minimum limit (fix with nvidia-smi -pl)")

        self._clear_marker()
        self.engaged = False
        self._applied_power = False
        self._applied_clocks = False

        detail = "; ".join(problems) if problems else "clocks and power limit restored"
        return (f"Previous run ended while the GPU was limited ({marker.get('reason', 'unknown')}) "
                f"- {detail}")

    def _write_marker(self) -> None:
        payload = {
            "engaged_at": time.time(),
            "pid": os.getpid(),
            "reason": self.engaged_reason or "",
            "subsystem_id": self.subsystem_id,
            "saved_power_limit_mw": self._saved_power_limit_mw,
            "applied_power_limit": self._applied_power,
            "applied_clocks": self._applied_clocks,
        }
        tmp = self._marker_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._marker_path)
        except Exception as ex:
            # Not fatal: the marker only matters if we crash before releasing.
            self._say("warning", f"could not write recovery marker ({ex})")

    def _read_marker(self) -> Optional[dict]:
        if not os.path.exists(self._marker_path):
            return None
        try:
            with open(self._marker_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            # A corrupt marker still means "a previous run was mitigated".
            return {}

    def _clear_marker(self) -> None:
        for path in (self._marker_path + ".tmp", self._marker_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

    # -- observation (used by the tools, never by the decision path) --------
    def graphics_clock_mhz(self) -> Optional[int]:
        if not self.is_available:
            return None
        return self._safe(lambda: self._nvml.graphics_clock_mhz(self._handle), None)

    def power_limit_mw(self) -> Optional[int]:
        if not self.is_available:
            return None
        return self._safe(lambda: self._nvml.power_limit_mw(self._handle), None)

    def close(self) -> None:
        """Release mitigation, then drop NVML. Never leaves the card limited."""
        try:
            self.release()
        except Exception:
            pass
        if self._nvml is not None:
            try:
                self._nvml.shutdown()
            except Exception:
                pass
        self._nvml = None
        self._handle = None
        self.unavailable_reason = "not connected"

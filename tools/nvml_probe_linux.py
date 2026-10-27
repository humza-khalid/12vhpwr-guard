"""
Linux Phase 0 probe: can the NVIDIA management library (NVML) do on Linux what the Windows
guard relies on, and is anything else on this machine going to fight it? READ-ONLY.

The Windows guard's first response to an overcurrent pin is to lock the graphics clock to its
floor and drop the power limit to the card's minimum, through NVML (gpu_mitigation.py). The
Linux daemon planned for v1.5 needs the same thing through libnvidia-ml.so.1. Before building
it, this tool answers the questions that decide whether the tiered response carries over:

- Does libnvidia-ml.so.1 load and initialise through plain ctypes?
- Is the ROG Astral visible, and does NVML report the same PCI subsystem id the sensor
  allowlist uses (so the daemon can match the card across both APIs, like Windows does)?
- What power limit range does the driver allow (400 to 600 W on the rig under Windows)?
- Does the driver enumerate a supported clock floor, or will the daemon have to walk the
  500 / 700 / 1000 MHz ladder the Windows guard falls back to?
- Is power usage readable (the v1.4 sensor-path check uses it)?
- Is i2c-dev loaded and set to load at boot, is a kernel driver already bound to the pin
  monitor chip, and are tools running that also set clocks or power limits?

Run it (root is not needed, it only reads):
    python3 tools/nvml_probe_linux.py

READ-ONLY, with no exception. This file binds only NVML getters. No setter, no reset and no
clear function is bound anywhere in it, and a test fails the build if one ever appears. The
reversible clock and power limit test lives in tools/nvml_limit_test_linux.py, which needs
root and asks first. Nothing here touches I2C either: the chip checks below only read sysfs.

Exit codes:
    0  NVML works and the report printed
    1  environment problem (not Linux)
    2  libnvidia-ml.so.1 missing, or nvmlInit failed
    3  NVML works but sees no NVIDIA GPU
"""

import argparse
import ctypes
import glob
import os
import platform
import shutil
import sys
from typing import Dict, List, Optional, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Same allowlist the Windows guard and the Linux sensor PoC use, imported rather than restated.
# astral_i2c only loads nvapi64.dll lazily inside a method, so this import is safe on Linux.
import astral_i2c  # noqa: E402

# ------------------------------------------------------------------ NVML ids
NVML_SUCCESS = 0
NVML_ERROR_NOT_SUPPORTED = 3
NVML_ERROR_INSUFFICIENT_SIZE = 7

NVML_CLOCK_GRAPHICS = 0
NVML_CLOCK_MEM = 2
NVML_TEMPERATURE_GPU = 0
NVML_DEVICE_NAME_BUFFER_SIZE = 96
NVML_SYSTEM_VERSION_BUFFER_SIZE = 80
NVML_MAX_CLOCK_ENTRIES = 256

# The Windows guard's fallback clock floors, repeated here only so the report can say what the
# daemon would do when the driver does not enumerate clocks. gpu_mitigation.CLOCK_FLOOR_LADDER
# is the source of truth; that module is Windows-only until the v1.5 split, so it is not imported.
CLOCK_FLOOR_LADDER = (500, 700, 1000)

NVML_LIBRARY_NAMES = ("libnvidia-ml.so.1", "libnvidia-ml.so")

# Tools that also set NVIDIA clocks, power limits or fan curves, or that talk to GPU I2C. Any of
# them running alongside the daemon could undo its limit or race its restore.
OTHER_GPU_TOOLS = {
    "openrgb": "OpenRGB (talks to GPU I2C for RGB)",
    "gwe": "GreenWithEnvy (sets NVIDIA power limits and clocks)",
    "lact": "LACT (sets GPU clocks and power limits)",
    "lactd": "LACT daemon (sets GPU clocks and power limits)",
    "coolercontrol": "CoolerControl (GPU fan and power control)",
    "coolercontrold": "CoolerControl daemon (GPU fan and power control)",
    "nvidia-powerd": "nvidia-powerd (NVIDIA Dynamic Boost daemon)",
    "nvidia-persistenced": "nvidia-persistenced (keeps the driver initialised, harmless)",
}


class NvmlError(RuntimeError):
    def __init__(self, func: str, code: int, text: Optional[str] = None):
        detail = f" ({text})" if text else ""
        super().__init__(f"{func} failed with NVML status {code}{detail}")
        self.func = func
        self.code = code


class NvmlPciInfo(ctypes.Structure):
    """nvmlPciInfo_t as used by nvmlDeviceGetPciInfo_v3. Same layout on Linux and Windows."""
    _fields_ = [
        ("busIdLegacy", ctypes.c_char * 16),
        ("domain", ctypes.c_uint32),
        ("bus", ctypes.c_uint32),
        ("device", ctypes.c_uint32),
        ("pciDeviceId", ctypes.c_uint32),
        ("pciSubSystemId", ctypes.c_uint32),
        ("busId", ctypes.c_char * 32),
    ]


class ReadOnlyNvml:
    """ctypes binding over NVML getters only.

    The library is loaded in init(), never at import, so importing this module (the tests do,
    on Windows) costs nothing. `lib` can be handed in to replace the real library in tests.
    Every call raises NvmlError on a non-zero status; the caller decides what matters.
    """

    def __init__(self, lib=None):
        self._lib = lib
        self._fn: Dict[str, object] = {}
        self._initialized = False
        self.library_name: Optional[str] = None

    # -- binding ------------------------------------------------------------
    def _load_library(self):
        last_error = None
        for name in NVML_LIBRARY_NAMES:
            try:
                lib = ctypes.CDLL(name)
                self.library_name = name
                return lib
            except OSError as ex:
                last_error = ex
        raise OSError(f"could not load {' or '.join(NVML_LIBRARY_NAMES)} ({last_error})")

    def _bind(self, name: str, restype, argtypes, required: bool = True):
        try:
            fn = getattr(self._lib, name)
        except AttributeError:
            if required:
                raise RuntimeError(f"{name} missing from the NVML library (driver too old)")
            return None
        try:
            fn.restype = restype
            fn.argtypes = argtypes
        except (AttributeError, TypeError):
            pass  # a test double; the real library always takes these
        self._fn[name] = fn
        return fn

    def _bind_getters(self) -> None:
        c_uint_p = ctypes.POINTER(ctypes.c_uint32)
        handle_p = ctypes.POINTER(ctypes.c_void_p)
        dev = ctypes.c_void_p

        self._bind("nvmlInit_v2", ctypes.c_int, [])
        self._bind("nvmlShutdown", ctypes.c_int, [])
        self._bind("nvmlErrorString", ctypes.c_char_p, [ctypes.c_int], required=False)
        self._bind("nvmlSystemGetDriverVersion", ctypes.c_int,
                   [ctypes.c_char_p, ctypes.c_uint32], required=False)
        self._bind("nvmlSystemGetNVMLVersion", ctypes.c_int,
                   [ctypes.c_char_p, ctypes.c_uint32], required=False)
        self._bind("nvmlDeviceGetCount_v2", ctypes.c_int, [c_uint_p])
        self._bind("nvmlDeviceGetHandleByIndex_v2", ctypes.c_int, [ctypes.c_uint32, handle_p])
        self._bind("nvmlDeviceGetPciInfo_v3", ctypes.c_int, [dev, ctypes.POINTER(NvmlPciInfo)])
        self._bind("nvmlDeviceGetName", ctypes.c_int, [dev, ctypes.c_char_p, ctypes.c_uint32])
        self._bind("nvmlDeviceGetPowerManagementLimit", ctypes.c_int, [dev, c_uint_p])
        self._bind("nvmlDeviceGetPowerManagementLimitConstraints", ctypes.c_int,
                   [dev, c_uint_p, c_uint_p])
        self._bind("nvmlDeviceGetPowerManagementDefaultLimit", ctypes.c_int, [dev, c_uint_p],
                   required=False)
        self._bind("nvmlDeviceGetEnforcedPowerLimit", ctypes.c_int, [dev, c_uint_p],
                   required=False)
        self._bind("nvmlDeviceGetPowerUsage", ctypes.c_int, [dev, c_uint_p], required=False)
        self._bind("nvmlDeviceGetClockInfo", ctypes.c_int, [dev, ctypes.c_int, c_uint_p])
        self._bind("nvmlDeviceGetMaxClockInfo", ctypes.c_int, [dev, ctypes.c_int, c_uint_p],
                   required=False)
        self._bind("nvmlDeviceGetTemperature", ctypes.c_int, [dev, ctypes.c_int, c_uint_p],
                   required=False)
        self._bind("nvmlDeviceGetPersistenceMode", ctypes.c_int,
                   [dev, ctypes.POINTER(ctypes.c_int)], required=False)
        # GeForce drivers often answer NOT_SUPPORTED here, which is why the ladder exists.
        self._bind("nvmlDeviceGetSupportedMemoryClocks", ctypes.c_int,
                   [dev, c_uint_p, c_uint_p], required=False)
        self._bind("nvmlDeviceGetSupportedGraphicsClocks", ctypes.c_int,
                   [dev, ctypes.c_uint32, c_uint_p, c_uint_p], required=False)

    def init(self) -> None:
        if self._lib is None:
            self._lib = self._load_library()
        self._bind_getters()
        self._call("nvmlInit_v2")
        self._initialized = True

    def shutdown(self) -> None:
        if self._initialized:
            try:
                self._call("nvmlShutdown")
            except Exception:
                pass
            self._initialized = False

    def error_text(self, code: int) -> Optional[str]:
        fn = self._fn.get("nvmlErrorString")
        if fn is None:
            return None
        try:
            text = fn(code)
            return text.decode("utf-8", errors="replace") if isinstance(text, bytes) else text
        except Exception:
            return None

    def _call(self, name: str, *args) -> None:
        fn = self._fn.get(name)
        if fn is None:
            raise NvmlError(name, NVML_ERROR_NOT_SUPPORTED, "not exported by this driver")
        status = fn(*args)
        if status != NVML_SUCCESS:
            raise NvmlError(name, status, self.error_text(status))

    def has(self, name: str) -> bool:
        return self._fn.get(name) is not None

    # -- system -------------------------------------------------------------
    def _system_string(self, func: str) -> Optional[str]:
        buf = ctypes.create_string_buffer(NVML_SYSTEM_VERSION_BUFFER_SIZE)
        try:
            self._call(func, buf, NVML_SYSTEM_VERSION_BUFFER_SIZE)
        except Exception:
            return None
        return buf.value.decode("utf-8", errors="replace")

    def driver_version(self) -> Optional[str]:
        return self._system_string("nvmlSystemGetDriverVersion")

    def nvml_version(self) -> Optional[str]:
        return self._system_string("nvmlSystemGetNVMLVersion")

    # -- devices ------------------------------------------------------------
    def device_count(self) -> int:
        count = ctypes.c_uint32(0)
        self._call("nvmlDeviceGetCount_v2", ctypes.byref(count))
        return int(count.value)

    def handle(self, index: int):
        dev = ctypes.c_void_p()
        self._call("nvmlDeviceGetHandleByIndex_v2", ctypes.c_uint32(index), ctypes.byref(dev))
        return dev

    def pci_info(self, handle) -> Tuple[int, str]:
        """(subsystem id, bus id string) for one device."""
        info = NvmlPciInfo()
        self._call("nvmlDeviceGetPciInfo_v3", handle, ctypes.byref(info))
        return int(info.pciSubSystemId), info.busId.decode("ascii", errors="replace")

    def name(self, handle) -> str:
        buf = ctypes.create_string_buffer(NVML_DEVICE_NAME_BUFFER_SIZE)
        self._call("nvmlDeviceGetName", handle, buf, NVML_DEVICE_NAME_BUFFER_SIZE)
        return buf.value.decode("utf-8", errors="replace")

    def _uint(self, func: str, handle, *extra) -> int:
        value = ctypes.c_uint32(0)
        self._call(func, handle, *extra, ctypes.byref(value))
        return int(value.value)

    def power_limit_mw(self, handle) -> int:
        return self._uint("nvmlDeviceGetPowerManagementLimit", handle)

    def default_power_limit_mw(self, handle) -> int:
        return self._uint("nvmlDeviceGetPowerManagementDefaultLimit", handle)

    def enforced_power_limit_mw(self, handle) -> int:
        return self._uint("nvmlDeviceGetEnforcedPowerLimit", handle)

    def power_usage_mw(self, handle) -> int:
        return self._uint("nvmlDeviceGetPowerUsage", handle)

    def power_limit_constraints_mw(self, handle) -> Tuple[int, int]:
        lo = ctypes.c_uint32(0)
        hi = ctypes.c_uint32(0)
        self._call("nvmlDeviceGetPowerManagementLimitConstraints",
                   handle, ctypes.byref(lo), ctypes.byref(hi))
        return int(lo.value), int(hi.value)

    def graphics_clock_mhz(self, handle) -> int:
        return self._uint("nvmlDeviceGetClockInfo", handle, ctypes.c_int(NVML_CLOCK_GRAPHICS))

    def memory_clock_mhz(self, handle) -> int:
        return self._uint("nvmlDeviceGetClockInfo", handle, ctypes.c_int(NVML_CLOCK_MEM))

    def max_graphics_clock_mhz(self, handle) -> int:
        return self._uint("nvmlDeviceGetMaxClockInfo", handle, ctypes.c_int(NVML_CLOCK_GRAPHICS))

    def temperature_c(self, handle) -> int:
        return self._uint("nvmlDeviceGetTemperature", handle, ctypes.c_int(NVML_TEMPERATURE_GPU))

    def persistence_mode(self, handle) -> bool:
        mode = ctypes.c_int(0)
        self._call("nvmlDeviceGetPersistenceMode", handle, ctypes.byref(mode))
        return bool(mode.value)

    def min_supported_graphics_clock_mhz(self, handle) -> Optional[int]:
        """Lowest graphics clock the driver admits to supporting, or None. Same walk as Windows."""
        try:
            mem_clocks = self._enumerate("nvmlDeviceGetSupportedMemoryClocks", handle)
            if not mem_clocks:
                return None
            lowest = None
            for mem in (mem_clocks[0], mem_clocks[-1]):
                clocks = self._enumerate("nvmlDeviceGetSupportedGraphicsClocks", handle,
                                         ctypes.c_uint32(mem))
                if clocks:
                    candidate = min(clocks)
                    lowest = candidate if lowest is None else min(lowest, candidate)
            return lowest
        except Exception:
            return None

    def _enumerate(self, func: str, handle, *extra) -> List[int]:
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


def _try(fn, *args):
    """(value, None) or (None, error text). The report shows failures instead of hiding them."""
    try:
        return fn(*args), None
    except Exception as ex:
        return None, str(ex)


def subsystem_label(subsystem_id: int) -> str:
    """The README's supported-cards notation, e.g. 1043:89E3."""
    return f"{subsystem_id & 0xFFFF:04X}:{(subsystem_id >> 16) & 0xFFFF:04X}"


def collect_device(nvml, index: int) -> dict:
    """Everything the report says about one GPU. Never raises; failures are recorded."""
    dev = {"index": index}
    handle, err = _try(nvml.handle, index)
    if handle is None:
        dev["error"] = err
        return dev
    pci, err = _try(nvml.pci_info, handle)
    if pci is not None:
        dev["subsystem_id"], dev["bus_id"] = pci
        dev["astral_model"] = astral_i2c.ASTRAL_SUBSYSTEM_IDS.get(pci[0])
    else:
        dev["pci_error"] = err
    dev["name"], _ = _try(nvml.name, handle)
    for key, fn in (
        ("power_limit_mw", nvml.power_limit_mw),
        ("default_power_limit_mw", nvml.default_power_limit_mw),
        ("enforced_power_limit_mw", nvml.enforced_power_limit_mw),
        ("power_usage_mw", nvml.power_usage_mw),
        ("constraints_mw", nvml.power_limit_constraints_mw),
        ("graphics_clock_mhz", nvml.graphics_clock_mhz),
        ("memory_clock_mhz", nvml.memory_clock_mhz),
        ("max_graphics_clock_mhz", nvml.max_graphics_clock_mhz),
        ("temperature_c", nvml.temperature_c),
        ("persistence_mode", nvml.persistence_mode),
    ):
        value, err = _try(fn, handle)
        dev[key] = value
        if value is None:
            dev[key + "_error"] = err
    dev["clock_floor_mhz"] = nvml.min_supported_graphics_clock_mhz(handle)
    return dev


# ------------------------------------------------------------- system checks
def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def distro_name(os_release: str = "/etc/os-release") -> Optional[str]:
    text = _read_text(os_release)
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


def i2c_dev_status(sys_root: str = "/sys", dev_root: str = "/dev", etc_root: str = "/etc") -> dict:
    """Is i2c-dev loaded now, and is it configured to load at boot?"""
    loaded = os.path.isdir(os.path.join(sys_root, "module", "i2c_dev")) or bool(
        glob.glob(os.path.join(dev_root, "i2c-*")))
    autoload_files = []
    candidates = glob.glob(os.path.join(etc_root, "modules-load.d", "*.conf"))
    candidates.append(os.path.join(etc_root, "modules"))
    for path in candidates:
        text = _read_text(path)
        if not text:
            continue
        for line in text.splitlines():
            if line.split("#", 1)[0].strip() in ("i2c-dev", "i2c_dev"):
                autoload_files.append(path)
                break
    return {"loaded": loaded, "autoload_files": autoload_files}


def chip_driver_bindings(sys_root: str = "/sys") -> List[Tuple[str, str]]:
    """Kernel drivers bound to any I2C device at address 0x2B, as (device, driver).

    A driver bound there (astral-hwmon, for one) owns the pin monitor chip, and i2c-dev then
    refuses I2C_SLAVE on that address with EBUSY. The daemon must read through that driver's
    hwmon files instead. Only sysfs is read; nothing is sent on the bus.
    """
    found = []
    for path in sorted(glob.glob(os.path.join(sys_root, "bus", "i2c", "devices", "*-002b"))):
        driver = os.path.join(path, "driver")
        if os.path.islink(driver):
            found.append((os.path.basename(path), os.path.basename(os.readlink(driver))))
        else:
            found.append((os.path.basename(path), "no driver bound"))
    return found


def hwmon_names(sys_root: str = "/sys") -> List[str]:
    names = []
    for path in sorted(glob.glob(os.path.join(sys_root, "class", "hwmon", "hwmon*", "name"))):
        name = _read_text(path)
        if name:
            names.append(name)
    return names


def running_gpu_tools(proc_root: str = "/proc") -> List[str]:
    """Descriptions of OTHER_GPU_TOOLS that have a running process right now."""
    seen = set()
    for comm_path in glob.glob(os.path.join(proc_root, "[0-9]*", "comm")):
        comm = _read_text(comm_path)
        if comm and comm.lower() in OTHER_GPU_TOOLS:
            seen.add(comm.lower())
    return [OTHER_GPU_TOOLS[name] for name in sorted(seen)]


def installed_gpu_tools() -> List[str]:
    return [OTHER_GPU_TOOLS[name] for name in sorted(OTHER_GPU_TOOLS)
            if shutil.which(name) and name != "nvidia-persistenced"]


GPU_UV_CONFIG = "/etc/gpu-uv/gpu-uv.conf"
GPU_UV_BINARY = "/usr/local/bin/gpu-uv"


def gpu_uv_clock_cap(path: str = GPU_UV_CONFIG) -> Tuple[bool, Optional[int]]:
    """(config present, max_clock_mhz) for the owner's gpu-uv undervolt, if installed.

    gpu-uv emulates an Afterburner flat curve: a positive clock offset capped by a clock lock
    of [lowest supported clock, max_clock_mhz]. Resetting that lock leaves the offset with no
    cap, which is an untested overclock, so anything that releases a clock lock on this
    machine must put gpu-uv's lock back instead. A present but unreadable config is reported
    as (True, None): the caller must then leave its own lock in place rather than reset.
    """
    if not os.path.exists(path):
        return False, None
    import configparser
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
        value = parser.getint("undervolt", "max_clock_mhz")
    except (configparser.Error, ValueError, OSError):
        return True, None
    return True, value if value > 0 else None


def collect_system() -> dict:
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    return {
        "distro": distro_name(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "root": is_root,
        "i2c_dev": i2c_dev_status(),
        "chip_bindings": chip_driver_bindings(),
        "hwmon_names": hwmon_names(),
        "tools_running": running_gpu_tools(),
        "tools_installed": installed_gpu_tools(),
        "gpu_uv": gpu_uv_clock_cap(),
        "gpu_uv_binary": os.path.exists(GPU_UV_BINARY),
    }


# ------------------------------------------------------------------ report
def _watts(mw: Optional[int]) -> str:
    if mw is None:
        return "n/a"
    return f"{mw / 1000:.0f} W" if mw % 1000 == 0 else f"{mw / 1000:.1f} W"


def _value(dev: dict, key: str, fmt) -> str:
    value = dev.get(key)
    if value is None:
        err = dev.get(key + "_error")
        return f"n/a ({err})" if err else "n/a"
    return fmt(value)


def render_device(dev: dict) -> List[str]:
    lines = [f"GPU {dev['index']}"]
    if "error" in dev:
        lines.append(f"  could not open: {dev['error']}")
        return lines
    lines.append(f"  name                {dev.get('name') or 'n/a'}")
    if "subsystem_id" in dev:
        sub = dev["subsystem_id"]
        model = dev.get("astral_model")
        verdict = f"supported: {model}" if model else "not on the Astral allowlist"
        lines.append(f"  PCI bus id          {dev.get('bus_id')}")
        lines.append(f"  subsystem id        0x{sub:08X} ({subsystem_label(sub)}), {verdict}")
    else:
        lines.append(f"  PCI info            n/a ({dev.get('pci_error')})")

    constraints = dev.get("constraints_mw")
    lines.append(f"  power limit now     {_value(dev, 'power_limit_mw', _watts)}")
    lines.append(f"  power limit default {_value(dev, 'default_power_limit_mw', _watts)}")
    lines.append(f"  enforced limit      {_value(dev, 'enforced_power_limit_mw', _watts)}")
    if constraints:
        lines.append(f"  power limit range   {_watts(constraints[0])} to {_watts(constraints[1])}")
    else:
        lines.append(f"  power limit range   {_value(dev, 'constraints_mw', str)}")
    lines.append(f"  power draw now      {_value(dev, 'power_usage_mw', _watts)}")
    lines.append(f"  graphics clock      {_value(dev, 'graphics_clock_mhz', lambda v: f'{v} MHz')}"
                 f" (max {_value(dev, 'max_graphics_clock_mhz', lambda v: f'{v} MHz')})")
    lines.append(f"  memory clock        {_value(dev, 'memory_clock_mhz', lambda v: f'{v} MHz')}")
    lines.append(f"  temperature         {_value(dev, 'temperature_c', lambda v: f'{v} C')}")
    lines.append(f"  persistence mode    {_value(dev, 'persistence_mode', lambda v: 'on' if v else 'off')}")
    floor = dev.get("clock_floor_mhz")
    if floor:
        lines.append(f"  clock lock target   {floor} MHz (enumerated by the driver)")
    else:
        lines.append(f"  clock lock target   not enumerated, the daemon would try "
                     f"{' / '.join(str(c) for c in CLOCK_FLOOR_LADDER)} MHz in turn")
    return lines


def render_system(sysinfo: dict) -> List[str]:
    i2c = sysinfo["i2c_dev"]
    lines = [
        "System",
        f"  distro              {sysinfo.get('distro') or 'unknown'}",
        f"  kernel              {sysinfo.get('kernel')}",
        f"  python              {sysinfo.get('python')}",
        f"  running as root     {'yes' if sysinfo.get('root') else 'no (fine, this probe only reads)'}",
        f"  i2c-dev loaded      {'yes' if i2c['loaded'] else 'no (sudo modprobe i2c-dev)'}",
        f"  i2c-dev at boot     "
        + (", ".join(i2c["autoload_files"]) if i2c["autoload_files"]
           else "not configured (the daemon's installer will add it)"),
    ]
    bindings = sysinfo["chip_bindings"]
    if bindings:
        for device, driver in bindings:
            lines.append(f"  chip at 0x2B        {device}: {driver}")
    else:
        lines.append("  chip at 0x2B        no kernel device registered (expected; our tools "
                     "read it directly)")
    names = sysinfo["hwmon_names"]
    lines.append(f"  hwmon sensors       {', '.join(names) if names else 'none'}")
    running = sysinfo["tools_running"]
    installed = sysinfo["tools_installed"]
    lines.append(f"  GPU tools running   {'; '.join(running) if running else 'none'}")
    lines.append(f"  GPU tools installed {'; '.join(installed) if installed else 'none'}")
    present, cap = sysinfo.get("gpu_uv", (False, None))
    if present:
        lines.append(f"  gpu-uv undervolt    config found, clock cap "
                     f"{f'{cap} MHz' if cap else 'UNREADABLE'}; binary "
                     f"{'present' if sysinfo.get('gpu_uv_binary') else 'missing'}")
    else:
        lines.append("  gpu-uv undervolt    not installed")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only NVML and environment probe for the planned 12VHPWR Guard "
                    "Linux daemon. Changes nothing.")
    parser.parse_args(argv)

    if not sys.platform.startswith("linux"):
        print("This probe is for Linux. On Windows the guard already uses NVML directly.",
              file=sys.stderr)
        return 1

    print("12VHPWR Guard - Linux NVML probe (read-only)")
    print()
    for line in render_system(collect_system()):
        print(line)
    print()

    nvml = ReadOnlyNvml()
    try:
        nvml.init()
    except Exception as ex:
        print(f"NVML           FAILED: {ex}")
        print("Is the NVIDIA proprietary driver installed, and does `nvidia-smi` work?")
        return 2

    try:
        print("NVML")
        print(f"  library             {nvml.library_name or 'loaded'}")
        print(f"  driver version      {nvml.driver_version() or 'n/a'}")
        print(f"  NVML version        {nvml.nvml_version() or 'n/a'}")
        print(f"  power draw readable "
              f"{'yes' if nvml.has('nvmlDeviceGetPowerUsage') else 'no (not exported)'}")
        count, err = _try(nvml.device_count)
        if not count:
            print(f"  GPUs                none{f' ({err})' if err else ''}")
            return 3
        print(f"  GPUs                {count}")
        print()
        for index in range(count):
            for line in render_device(collect_device(nvml, index)):
                print(line)
            print()
    finally:
        nvml.shutdown()

    print("Paste everything above back into the chat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

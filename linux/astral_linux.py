"""
Linux sensor backend: the ASUS ROG Astral per-pin 12VHPWR voltages and currents, read straight
from the card's ITE IT8915FN monitor chip over /dev/i2c-N, with no NVAPI and no HWiNFO.

This is the read path proven by tools/astral_poc_linux.py on a ROG Astral RTX 5090 OC under
Linux Mint, moved here so the Linux daemon and the proof of concept share one copy of it. The
PoC now imports everything below instead of carrying its own. AstralLinuxBackend at the bottom
has the same shape as the Windows backends in hwinfo_12vhpwr_guard.py (key, tag, connect(),
read_pins(), close(), degraded, subsystem_id), so the guard loop can drive it unchanged.

READ-ONLY, with no exception. The only byte this module ever puts on the I2C bus is the single
register pointer 0x80, and it goes out inside a read: read_write is always I2C_SMBUS_READ, on
the one transaction this file performs. There is no write path here at all: no SMBus write
helper, no generic "put bytes" function, and no option that can change the register or the
device address. Writing to the wrong device on a GPU I2C bus can permanently damage hardware.
The character device is opened O_RDONLY as well; i2c-dev does not gate its ioctl handlers on the
file mode, so read-only is enough to read and is one less way to write.
tests/test_astral_linux.py pins all of this structurally against the source of this file.

The transaction is the SMBus "I2C block data" read (the I2C_SMBUS ioctl with size
I2C_SMBUS_I2C_BLOCK_DATA), and that is not a style choice. On the NVIDIA i2c adapters a plain
repeated-START multi-byte read, a one byte write message followed by a read message in a single
combined transfer, returns all zeros silently: the ioctl succeeds, the chip is never really
spoken to, and the buffer decodes to a perfectly healthy 0.000 A on every pin. Measured by the
ksokolowski/astral-hwmon project on 2026-08-13 (their driver and their independent userspace
oracle both say so in as many words), and reproduced on 2026-09-05 on a ROG Astral RTX 5090 OC
under Linux Mint. That path is not kept anywhere in this file, not even as a fallback: a silent
wrong answer about pin currents is worse than no answer.

astral-hwmon also established which adapter to expect. Of the card's seven adapters only
"NVIDIA i2c adapter 1" answers at 0x2B; the other six return -EIO. All candidates are still
tried, because trying costs nothing and proves the negative. The adapter NUMBER is not stable
(i2c-1 one boot, i2c-4 the next), so the backend rediscovers by name and PCI address on every
connect and never caches it.

Protocol constants (chip address, register, block layout, the supported card allowlist) live in
astral_i2c.py at the repo root and are imported from there rather than restated, so the Windows
and Linux paths cannot drift. One address difference to keep straight: NVAPI wants the device
address shifted left by one (0x56), the Linux i2c-dev interface wants the plain 7 bit address
(0x2B). Same chip.

Import-safe anywhere: fcntl is imported only inside the single ioctl call site, so this module
loads on Windows, which is where its unit tests run against fakes.
"""

import ctypes
import os
import re
import sys
from typing import Callable, List, NamedTuple, Optional, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# astral_i2c only touches ctypes/sys/typing at import time and loads nvapi64.dll lazily inside a
# method, so importing it here is safe on Linux (and on Windows, which is where the tests run).
import astral_i2c  # noqa: E402
from astral_i2c import (  # noqa: E402
    ASTRAL_MAX_CONSECUTIVE_FAILURES,
    PIN_LABEL_FMT,
    SensorGoneError,
    TransientReadError,
)

# ------------------------------------------------------------------ chip access
# NVAPI takes the address pre-shifted (astral_i2c.IT8915_I2C_ADDRESS == 0x56). The kernel
# i2c-dev interface does the shifting itself and wants the 7 bit address, so use 0x2B here.
IT8915_ADDR_7BIT = 0x2B
IT8915_REG_START = astral_i2c.IT8915_REG_START      # 0x80
IT8915_DATA_SIZE = astral_i2c.IT8915_DATA_SIZE      # 24

# ------------------------------------------------------------- linux i2c-dev ABI
# From include/uapi/linux/i2c-dev.h and include/uapi/linux/i2c.h. These are plain ioctl numbers,
# not _IO() encoded ones.
I2C_SLAVE = 0x0703                  # bind this descriptor to one chip address. No bus traffic.
I2C_FUNCS = 0x0705                  # ask the driver what it can do. No bus traffic.
I2C_SMBUS = 0x0720                  # perform one SMBus style transfer

I2C_SMBUS_READ = 1                  # the only direction this file ever asks for
I2C_SMBUS_I2C_BLOCK_DATA = 8        # "read N bytes from register R", N chosen by the caller
I2C_SMBUS_BLOCK_MAX = 32            # union i2c_smbus_data's block[] is this + 2 bytes long

# The functionality bit for that transfer. I2C_FUNC_I2C (0x00000001, plain non SMBus transfers)
# is deliberately not checked any more: that is the family the zero-returning read belongs to,
# so an adapter advertising it tells us nothing we want to act on.
I2C_FUNC_SMBUS_READ_I2C_BLOCK = 0x04000000

PCI_DEVICES_ROOT = "/sys/bus/pci/devices"
I2C_DEVICES_ROOT = "/sys/bus/i2c/devices"
I2C_ADAPTER_ROOT = "/sys/class/i2c-adapter"
DEV_ROOT = "/dev"
# The NVIDIA driver's per-GPU info files. Only used for the marketing name in the description.
PROC_NVIDIA_GPUS_ROOT = "/proc/driver/nvidia/gpus"

PCI_VENDOR_NVIDIA = 0x10DE
PCI_CLASS_DISPLAY = 0x03

_ADAPTER_DIR_RE = re.compile(r"^i2c-(\d+)$")
_ADAPTER_NAME_PORT_RE = re.compile(r"adapter\s+(\d+)", re.IGNORECASE)


class I2cSmbusData(ctypes.Union):
    """union i2c_smbus_data. Only block[] is used here: block[0] is the count, block[1:] data."""
    _fields_ = [
        ("byte", ctypes.c_uint8),
        ("word", ctypes.c_uint16),
        ("block", ctypes.c_uint8 * (I2C_SMBUS_BLOCK_MAX + 2)),
    ]


class I2cSmbusIoctlData(ctypes.Structure):
    """struct i2c_smbus_ioctl_data: direction, register, transfer kind, and the data buffer."""
    _fields_ = [
        ("read_write", ctypes.c_uint8),             # 0
        ("command", ctypes.c_uint8),                # 1  (+2 pad)
        ("size", ctypes.c_uint32),                  # 4
        ("data", ctypes.POINTER(I2cSmbusData)),     # 8
    ]


class AdapterError(RuntimeError):
    """This adapter cannot serve the read. Try the next one."""


def assert_transaction_shape() -> None:
    """A wrong struct layout would put a malformed request on the bus. Fail before that.

    Also re-checks the constants shared with astral_i2c, so a change there cannot silently
    leave this file pointing at a different chip or register, or asking for more bytes than one
    SMBus block can carry.
    """
    ptr = ctypes.sizeof(ctypes.c_void_p)
    checks = [
        ("I2cSmbusIoctlData.read_write", I2cSmbusIoctlData.read_write.offset, 0),
        ("I2cSmbusIoctlData.command", I2cSmbusIoctlData.command.offset, 1),
        ("I2cSmbusIoctlData.size", I2cSmbusIoctlData.size.offset, 4),
        ("I2cSmbusIoctlData.data", I2cSmbusIoctlData.data.offset, 8),
        ("sizeof(I2cSmbusIoctlData)", ctypes.sizeof(I2cSmbusIoctlData), 8 + ptr),
        ("I2cSmbusData.block", I2cSmbusData.block.offset, 0),
        ("sizeof(I2cSmbusData)", ctypes.sizeof(I2cSmbusData), I2C_SMBUS_BLOCK_MAX + 2),
    ]
    for name, actual, expected in checks:
        if actual != expected:
            raise RuntimeError(
                f"{name} is {actual}, expected {expected} - refusing to touch I2C"
            )
    if astral_i2c.IT8915_I2C_ADDRESS != IT8915_ADDR_7BIT << 1:
        raise RuntimeError(
            "chip address disagrees with astral_i2c - refusing to touch I2C"
        )
    if IT8915_REG_START != 0x80 or IT8915_DATA_SIZE != 24:
        raise RuntimeError(
            f"unexpected register/length {IT8915_REG_START:#04x}/{IT8915_DATA_SIZE}"
            " - refusing to touch I2C"
        )
    if not 0 < IT8915_DATA_SIZE <= I2C_SMBUS_BLOCK_MAX:
        raise RuntimeError(
            f"{IT8915_DATA_SIZE} bytes does not fit in one SMBus block"
            " - refusing to touch I2C"
        )


def _ioctl(fd: int, request: int, arg):
    """The one and only ioctl call site in this file.

    fcntl is imported here, not at module scope, so the module imports on a machine that has no
    fcntl (Windows), which is where its unit tests run. Every function below takes an optional
    `ioctl` stand-in with this signature; None means this one.
    """
    import fcntl
    return fcntl.ioctl(fd, request, arg)


def read_pin_block(fd: int, ioctl: Optional[Callable] = None) -> bytes:
    """One SMBus "I2C block data" read from the IT8915. The only bus access in this file.

    Two ioctls, in this order, and nothing else:
      I2C_SLAVE  bind the descriptor to the 7 bit address 0x2B. Nothing reaches the bus.
      I2C_SMBUS  read_write=I2C_SMBUS_READ, command=0x80 (the register pointer, and the only
                 byte that goes out), size=I2C_SMBUS_I2C_BLOCK_DATA, data -> a union whose
                 block[0] is 24 on the way in. On return block[0] holds the count the kernel
                 produced and block[1..24] the bytes.

    block[0] is in/out, so the returned count is checked rather than assumed: a part filled
    buffer would otherwise decode trailing bytes left behind by whatever ran before.

    I2C_SLAVE (not the _FORCE variant) also means the kernel answers EBUSY when a driver such as
    astral-hwmon already owns 0x2B, so this read can never contend with one.
    """
    if ioctl is None:
        ioctl = _ioctl      # looked up per call, so a patched module attribute is honoured
    data = I2cSmbusData()
    data.block[0] = IT8915_DATA_SIZE
    payload = I2cSmbusIoctlData(
        read_write=I2C_SMBUS_READ,
        command=IT8915_REG_START,
        size=I2C_SMBUS_I2C_BLOCK_DATA,
        data=ctypes.pointer(data),
    )

    ioctl(fd, I2C_SLAVE, IT8915_ADDR_7BIT)
    ioctl(fd, I2C_SMBUS, payload)
    # data stays referenced by this frame until here, so the kernel never sees freed memory
    # behind payload.data.
    produced = data.block[0]
    if produced != IT8915_DATA_SIZE:
        raise AdapterError(
            f"block read at {IT8915_REG_START:#04x} produced {produced} bytes,"
            f" expected {IT8915_DATA_SIZE}"
        )
    return bytes(data.block[1:IT8915_DATA_SIZE + 1])


def adapter_functionality(fd: int, ioctl: Optional[Callable] = None) -> Optional[int]:
    """I2C_FUNCS: ask the driver what it can do. Pure query, nothing reaches the bus."""
    if ioctl is None:
        ioctl = _ioctl
    buf = ctypes.c_ulong(0)
    try:
        ioctl(fd, I2C_FUNCS, buf)
    except OSError:
        return None
    return buf.value


def functionality_note(funcs: Optional[int]) -> Optional[str]:
    """A note when the driver does not advertise block reads, or None when it does.

    A note, not a refusal, and that is a considered choice. The NVIDIA adapters come from a
    closed driver whose advertised bits are not a promise in either direction; the transfer is
    read-only whatever they say; and an adapter that genuinely cannot do it fails the I2C_SMBUS
    ioctl with an errno that the caller already handles and prints. Refusing on a missing bit
    would only let the caller talk itself out of the one transfer known to work on this hardware.
    """
    if funcs is None or funcs & I2C_FUNC_SMBUS_READ_I2C_BLOCK:
        return None
    return (f"driver does not advertise SMBus block reads (funcs {funcs:#010x}),"
            " trying anyway since the read is read-only either way")


def _open_read_only(path: str) -> int:
    """The backend's default opener. The flags live here, not with the caller, so no injected
    opener and no caller can ask for a writable descriptor through the backend."""
    return os.open(path, os.O_RDONLY)


# ---------------------------------------------------------------- sysfs helpers
def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return None


def _read_hex(path: str) -> Optional[int]:
    text = _read_text(path)
    if text is None:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


class GpuInfo(NamedTuple):
    address: str                    # "0000:01:00.0"
    device_id: int
    subsystem_vendor: int
    subsystem_device: int
    subsystem_id: int               # (device << 16) | vendor, the astral_i2c key
    model: Optional[str]            # allowlist name, None if unknown

    @property
    def subsystem_label(self) -> str:
        """README style, for example 1043:89E3."""
        return f"{self.subsystem_vendor:04X}:{self.subsystem_device:04X}"


def find_nvidia_gpus(pci_root: Optional[str] = None) -> List[GpuInfo]:
    """Every NVIDIA display-class PCI function, sorted by address, annotated with the allowlist."""
    pci_root = pci_root if pci_root is not None else PCI_DEVICES_ROOT
    try:
        entries = sorted(os.listdir(pci_root))
    except OSError:
        return []

    gpus: List[GpuInfo] = []
    for address in entries:
        base = os.path.join(pci_root, address)
        vendor = _read_hex(os.path.join(base, "vendor"))
        if vendor != PCI_VENDOR_NVIDIA:
            continue
        pci_class = _read_hex(os.path.join(base, "class"))
        # class is 0xCCSSPP (class, subclass, prog-if); display controllers are class 0x03.
        if pci_class is None or (pci_class >> 16) != PCI_CLASS_DISPLAY:
            continue
        sub_vendor = _read_hex(os.path.join(base, "subsystem_vendor"))
        sub_device = _read_hex(os.path.join(base, "subsystem_device"))
        if sub_vendor is None or sub_device is None:
            continue
        subsystem_id = (sub_device << 16) | sub_vendor
        gpus.append(GpuInfo(
            address=address,
            device_id=_read_hex(os.path.join(base, "device")) or 0,
            subsystem_vendor=sub_vendor,
            subsystem_device=sub_device,
            subsystem_id=subsystem_id,
            model=astral_i2c.ASTRAL_SUBSYSTEM_IDS.get(subsystem_id),
        ))
    return gpus


def pick_astral(gpus: List[GpuInfo]) -> Optional[GpuInfo]:
    """The first allowlisted Astral, or None."""
    for gpu in gpus:
        if gpu.model:
            return gpu
    return None


def gpu_marketing_name(pci_address: str, proc_root: Optional[str] = None) -> Optional[str]:
    """The "Model:" line of the NVIDIA driver's info file, e.g. "NVIDIA GeForce RTX 5090".

    Cosmetic only (it goes into the connect description); None when the file is not there.
    """
    proc_root = proc_root if proc_root is not None else PROC_NVIDIA_GPUS_ROOT
    text = _read_text(os.path.join(proc_root, pci_address, "information"))
    if not text:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == "model" and value.strip():
            return value.strip()
    return None


class Adapter(NamedTuple):
    index: int
    dev_path: str                   # /dev/i2c-N
    name: str                       # contents of the sysfs "name" file
    sysfs_path: str                 # resolved topology path
    owned_by_path: bool             # the GPU's PCI address appears in the resolved path
    name_matches: bool              # name says NVIDIA and carries the GPU's PCI address
    hint_port: Optional[int]        # the number in "NVIDIA i2c adapter N at ..."


def _address_variants(pci_address: str) -> List[str]:
    """The spellings of a PCI address that turn up in adapter names: 0000:01:00.0, 01:00.0, 1:00.0."""
    variants = [pci_address]
    if ":" in pci_address:
        tail = pci_address.split(":", 1)[1]         # "01:00.0"
        variants.append(tail)
        if ":" in tail:
            bus, rest = tail.split(":", 1)
            try:
                variants.append(f"{int(bus, 16):x}:{rest}")   # "1:00.0"
            except ValueError:
                pass
    return variants


def _candidate_sort_key(adapter: Adapter) -> Tuple[int, int, int]:
    """Resolved-topology matches first, then the port 1 hint, then the adapter index.

    The hint is only an ordering nudge: NVAPI reaches this chip on internal portId 1 and the
    NVIDIA driver names its adapters "NVIDIA i2c adapter N at ...", so N == 1 is worth trying
    first. Every candidate still gets tried.
    """
    return (
        0 if adapter.owned_by_path else 1,
        0 if adapter.hint_port == 1 else 1,
        adapter.index,
    )


def find_gpu_i2c_adapters(
    pci_address: str,
    i2c_root: Optional[str] = None,
    dev_root: Optional[str] = None,
    resolve=os.path.realpath,
) -> List[Adapter]:
    """Candidate i2c-dev adapters belonging to the given GPU, best guess first.

    Ownership is decided two ways, either of which is enough: the adapter's resolved sysfs path
    sits under the GPU's PCI address, or its name says NVIDIA and carries that address. An
    adapter with no /dev node is skipped, since it cannot be opened anyway. `resolve` is the
    seam the unit tests use to stand in for symlink resolution.
    """
    i2c_root = i2c_root if i2c_root is not None else I2C_DEVICES_ROOT
    dev_root = dev_root if dev_root is not None else DEV_ROOT
    try:
        entries = os.listdir(i2c_root)
    except OSError:
        return []

    variants = _address_variants(pci_address)
    found: List[Adapter] = []
    for entry in entries:
        match = _ADAPTER_DIR_RE.match(entry)
        if not match:
            continue
        index = int(match.group(1))
        dev_path = os.path.join(dev_root, f"i2c-{index}")
        if not os.path.exists(dev_path):
            continue

        sysfs_path = resolve(os.path.join(i2c_root, entry))
        name = _read_text(os.path.join(i2c_root, entry, "name")) or ""
        owned_by_path = any(v in sysfs_path for v in variants)
        name_matches = "nvidia" in name.lower() and any(v in name for v in variants)
        if not owned_by_path and not name_matches:
            continue

        port = _ADAPTER_NAME_PORT_RE.search(name)
        found.append(Adapter(
            index=index,
            dev_path=dev_path,
            name=name,
            sysfs_path=sysfs_path,
            owned_by_path=owned_by_path,
            name_matches=name_matches,
            hint_port=int(port.group(1)) if port else None,
        ))
    return sorted(found, key=_candidate_sort_key)


def discover_adapters(
    pci_address: str,
    i2c_root: Optional[str] = None,
    adapter_root: Optional[str] = None,
    dev_root: Optional[str] = None,
    resolve=os.path.realpath,
) -> List[Adapter]:
    """find_gpu_i2c_adapters on the bus view, then on the class view if the bus view was empty.

    Same adapters, two views of them in sysfs; the second is worth a look only when the first
    turned up nothing. Fresh every call: nothing about an adapter is remembered between calls.
    """
    adapters = find_gpu_i2c_adapters(pci_address, i2c_root=i2c_root, dev_root=dev_root,
                                     resolve=resolve)
    adapter_root = adapter_root if adapter_root is not None else I2C_ADAPTER_ROOT
    if not adapters and os.path.isdir(adapter_root):
        adapters = find_gpu_i2c_adapters(pci_address, i2c_root=adapter_root, dev_root=dev_root,
                                         resolve=resolve)
    return adapters


def chip_client_dir(adapter_index: int, i2c_root: Optional[str] = None) -> str:
    """The sysfs directory the kernel creates for a client at 0x2B on adapter N: "N-002b"."""
    i2c_root = i2c_root if i2c_root is not None else I2C_DEVICES_ROOT
    return os.path.join(i2c_root, f"{adapter_index}-{IT8915_ADDR_7BIT:04x}")


def bound_driver(adapter_index: int, i2c_root: Optional[str] = None,
                 resolve=os.path.realpath) -> Optional[str]:
    """The name of the kernel driver bound to 0x2B on adapter N, or None when nothing is.

    A bound client has a "driver" symlink into /sys/bus/i2c/drivers/<name>. lexists rather
    than exists, so even a dangling link counts as "somebody owns this chip": the cautious side.
    Pure sysfs, nothing is opened and nothing reaches the bus.
    """
    link = os.path.join(chip_client_dir(adapter_index, i2c_root), "driver")
    if not os.path.lexists(link):
        return None
    name = os.path.basename(os.path.normpath(resolve(link)))
    return name if name and name != "driver" else "unknown driver"


# ------------------------------------------------------------------- decoding
# The plausibility window, deliberately tighter than astral_i2c.is_plausible (which allows 0 to
# 16 V), because the question here is "is this the monitor chip answering at all". A device that
# is not the chip reads 0 V, and 0 V clears the Windows runtime gate as a perfectly healthy
# 0.000 A on every pin: that is exactly how the first Mint run accepted a frame of near-zeros as
# real data. The Linux backend applies this window to every sample, not only the probe.
#
# The floor is 6.0 V rather than something tight like 11.0 on purpose. A failing 12VHPWR
# connector raises contact resistance and so lowers the pin voltage under load, which means a
# tight floor would blind the reader to the very failure it exists to reveal. astral-hwmon
# measured 11.944 V on the lowest pin under a 562 W burn, so half of nominal still rejects
# garbage while leaving a sagging pin visible as a low number rather than as silence.
PROBE_MIN_VOLTS = 6.0
PROBE_MAX_VOLTS = 13.0
PROBE_MAX_AMPS = 30.0


def hex_dump(raw: bytes) -> str:
    return " ".join(f"{b:02X}" for b in raw)


def _pin_plausible(volts: float, amps: float) -> bool:
    return (PROBE_MIN_VOLTS <= volts <= PROBE_MAX_VOLTS) and (0.0 <= amps <= PROBE_MAX_AMPS)


def block_verdict(raw: bytes) -> Tuple[bool, str]:
    """Is this 24 byte block a believable sample? Returns (ok, reason).

    An adapter that is not the monitor chip typically answers all 0xFF (nothing acked, bus
    pulled high) or all 0x00, and either would decode into something that looks like data. A
    frame that is neither, yet still reads near 0 V on a pin, is the same lie in better clothes,
    so every pin is range checked and the first bad one is named with its values.
    """
    if len(raw) != IT8915_DATA_SIZE:
        return False, f"{len(raw)} bytes, expected {IT8915_DATA_SIZE}"
    if all(b == 0xFF for b in raw):
        return False, "all 0xFF, nothing answered at 0x2B"
    if all(b == 0x00 for b in raw):
        return False, "all zero, no data from this adapter"
    try:
        pins = astral_i2c.parse_pin_block(raw)
    except ValueError as ex:
        return False, str(ex)
    for pin, volts, amps in pins:
        if not _pin_plausible(volts, amps):
            return False, (
                f"pin{pin} implausible (V={volts:.3f} A={amps:.3f}), need"
                f" {PROBE_MIN_VOLTS:.1f} to {PROBE_MAX_VOLTS:.1f} V and 0 to"
                f" {PROBE_MAX_AMPS:.0f} A on every pin"
            )
    return True, "plausible"


# ------------------------------------------------------------------- backend
class AstralLinuxBackend:
    """Per-pin currents straight from the card's IT8915 chip over i2c-dev. Linux counterpart of
    AstralBackend in hwinfo_12vhpwr_guard.py, with the same key and tag (the loop treats key
    "astral" as the direct sensor) and the same error policy as astral_i2c.AstralPinSensor:

      a single failed or implausible sample   -> astral_i2c.TransientReadError (skip the tick)
      ASTRAL_MAX_CONSECUTIVE_FAILURES in a row -> astral_i2c.SensorGoneError (reconnect)
      the /dev/i2c-N node gone                 -> SensorGoneError at once

    Every constructor argument is a seam for the unit tests; the defaults are the real system.
    `opener(path) -> fd` decides nothing about flags (the default opens O_RDONLY), `closer(fd)`
    defaults to os.close, and `ioctl` (same signature as _ioctl) defaults to this module's _ioctl.
    """

    key = "astral"
    tag = "direct"

    def __init__(
        self,
        *,
        pci_root: Optional[str] = None,
        i2c_root: Optional[str] = None,
        adapter_root: Optional[str] = None,
        dev_root: Optional[str] = None,
        proc_root: Optional[str] = None,
        resolve: Optional[Callable[[str], str]] = None,
        opener: Optional[Callable[[str], int]] = None,
        closer: Optional[Callable[[int], None]] = None,
        ioctl: Optional[Callable] = None,
        platform: Optional[str] = None,
        geteuid: Optional[Callable[[], int]] = None,
    ):
        self._pci_root = pci_root
        self._i2c_root = i2c_root
        self._adapter_root = adapter_root
        self._dev_root = dev_root
        self._proc_root = proc_root
        self._resolve = resolve if resolve is not None else os.path.realpath
        self._opener = opener if opener is not None else _open_read_only
        self._closer = closer if closer is not None else os.close
        self._ioctl = ioctl             # None: read_pin_block uses the module's _ioctl
        self._platform = platform
        self._geteuid = geteuid

        self._fd: Optional[int] = None
        self._consecutive_failures = 0
        self._subsystem_id: Optional[int] = None
        self.dev_path: Optional[str] = None
        self.adapter: Optional[Adapter] = None
        self.gpu: Optional[GpuInfo] = None
        self.gpu_name: Optional[str] = None
        self.model: Optional[str] = None
        self.last_voltages: List[float] = []

    # -- environment --------------------------------------------------------
    def _check_environment(self) -> None:
        platform = self._platform if self._platform is not None else sys.platform
        if not platform.startswith("linux"):
            raise RuntimeError(f"the Linux Astral backend needs Linux (running on {platform})")

        geteuid = self._geteuid if self._geteuid is not None else getattr(os, "geteuid", None)
        if geteuid is None or geteuid() != 0:
            raise RuntimeError("root required: /dev/i2c-* is not readable by ordinary users")

        dev_root = self._dev_root if self._dev_root is not None else DEV_ROOT
        try:
            nodes = [n for n in os.listdir(dev_root) if _ADAPTER_DIR_RE.match(n)]
        except OSError:
            nodes = []
        if not nodes:
            raise RuntimeError(
                f"no {dev_root}/i2c-* nodes: the i2c-dev module is not loaded"
                " (sudo modprobe i2c-dev)"
            )

    # -- public API ---------------------------------------------------------
    def connect(self) -> str:
        """Find the Astral, find its chip, prove it answers, keep the descriptor open.

        Everything is rediscovered on every call: adapter numbers move between boots, and a
        reconnect after a driver reload may find the chip under a new number. Raises
        RuntimeError with the reason when it cannot connect.
        """
        self.close()
        self._check_environment()
        assert_transaction_shape()

        gpus = find_nvidia_gpus(self._pci_root)
        target = pick_astral(gpus)
        if target is None:
            seen = ", ".join(f"{g.address} subsystem {g.subsystem_label}" for g in gpus)
            detail = f"NVIDIA display adapters: {seen}" if seen else "no NVIDIA display adapter"
            raise RuntimeError(f"no supported ASUS ROG Astral GPU found ({detail})")

        adapters = discover_adapters(
            target.address, i2c_root=self._i2c_root, adapter_root=self._adapter_root,
            dev_root=self._dev_root, resolve=self._resolve,
        )
        if not adapters:
            raise RuntimeError(
                f"no i2c-dev adapter could be tied to {target.model} at {target.address}"
                " (is the nvidia driver loaded?)"
            )

        # Before any descriptor is opened: if a kernel driver owns the chip, step aside. The
        # kernel would refuse the bind anyway (EBUSY), but this names the culprit.
        for adapter in adapters:
            driver = bound_driver(adapter.index, self._i2c_root, self._resolve)
            if driver:
                raise RuntimeError(
                    f"kernel driver '{driver}' is bound to the monitor chip"
                    f" ({adapter.index}-{IT8915_ADDR_7BIT:04x} on {adapter.dev_path}), so the"
                    f" guard cannot read the pins. Unload it (for example: sudo modprobe -r"
                    f" {driver}) and restart the guard"
                )

        failures = []
        for adapter in adapters:
            ok, raw, reason = self._probe(adapter)
            if ok:
                self._subsystem_id = target.subsystem_id
                self.adapter = adapter
                self.dev_path = adapter.dev_path
                self.gpu = target
                self.model = target.model
                self.gpu_name = gpu_marketing_name(target.address, self._proc_root) or "NVIDIA GPU"
                self._consecutive_failures = 0
                self.last_voltages = [v for _p, v, _a in astral_i2c.parse_pin_block(raw)]
                return f"{self.gpu_name} [{self.model}] on {self.dev_path}"
            failures.append(f"{adapter.dev_path}: {reason}")

        raise RuntimeError(
            f"no adapter of {target.model} returned plausible per-pin data ("
            + "; ".join(failures) + ")"
        )

    def _probe(self, adapter: Adapter) -> Tuple[bool, Optional[bytes], str]:
        """One read on one candidate. On success the descriptor stays open as self._fd."""
        try:
            fd = self._opener(adapter.dev_path)
        except OSError as ex:
            return False, None, f"open failed: {ex}"
        try:
            raw = read_pin_block(fd, self._ioctl)
            ok, reason = block_verdict(raw)
            if not ok:
                reason = f"{reason} [raw {hex_dump(raw)}]"
        except (OSError, AdapterError) as ex:
            ok, raw, reason = False, None, str(ex)
        if ok:
            self._fd = fd
        else:
            self._close_fd(fd)
        return ok, raw, reason

    @property
    def degraded(self) -> Optional[str]:
        return None

    @property
    def subsystem_id(self) -> Optional[int]:
        """Lets the mitigation side bind NVML to this exact card (NVML uses the same DWORD)."""
        return self._subsystem_id

    def read_pins(self) -> List[Tuple[str, float, str]]:
        """One sample as [(label, amps, "A")] for pins 1..6, matching the HWiNFO shape.

        Raises TransientReadError for a single bad sample, SensorGoneError once failures
        persist to ASTRAL_MAX_CONSECUTIVE_FAILURES or the device node has gone away.
        """
        if self._fd is None:
            raise SensorGoneError("not connected")

        try:
            raw = read_pin_block(self._fd, self._ioctl)
            ok, reason = block_verdict(raw)
            if not ok:
                raise TransientReadError(f"implausible sample: {reason}")
        except (OSError, AdapterError, TransientReadError) as ex:
            if self.dev_path is not None and not os.path.exists(self.dev_path):
                raise SensorGoneError(f"{self.dev_path} has gone away ({ex})") from ex
            self._consecutive_failures += 1
            if self._consecutive_failures >= ASTRAL_MAX_CONSECUTIVE_FAILURES:
                raise SensorGoneError(
                    f"{self._consecutive_failures} consecutive failed I2C reads (last: {ex})"
                ) from ex
            if isinstance(ex, TransientReadError):
                raise
            raise TransientReadError(f"I2C read failed: {ex}") from ex

        parsed = astral_i2c.parse_pin_block(raw)
        self._consecutive_failures = 0
        self.last_voltages = [v for _pin, v, _a in parsed]
        return [(PIN_LABEL_FMT.format(pin), amps, "A") for pin, _volts, amps in parsed]

    def _close_fd(self, fd: int) -> None:
        try:
            self._closer(fd)
        except OSError:
            pass

    def close(self) -> None:
        """Close the descriptor and forget the connection. Safe to call any number of times."""
        fd, self._fd = self._fd, None
        if fd is not None:
            self._close_fd(fd)
        self._subsystem_id = None
        self.adapter = None
        self.dev_path = None

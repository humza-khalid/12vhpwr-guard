"""
Linux proof of concept: read the ASUS ROG Astral per-pin 12VHPWR voltages and currents straight
from the card's ITE IT8915FN monitor chip over /dev/i2c-N, with no NVAPI and no HWiNFO.

This is the Linux counterpart of tools/astral_poc.py (which goes through NVAPI on Windows). It
exists to confirm the sensor path on Linux ahead of the headless Linux daemon planned for v1.5.
It guards nothing by itself, it only prints what the card reports, and it is not tied to any
particular workload: whatever runs on the machine, the pins are the pins.

Run it:
    sudo modprobe i2c-dev
    sudo python3 tools/astral_poc_linux.py                    # one line per second until Ctrl-C
    sudo python3 tools/astral_poc_linux.py --once             # a single sample
    sudo python3 tools/astral_poc_linux.py --list             # candidate adapters, then exit
    sudo python3 tools/astral_poc_linux.py --adapter 4 --raw --volts

READ-ONLY, with no exception. The only byte this program ever puts on the I2C bus is the single
register pointer 0x80, sent as message 1 of one combined I2C_RDWR transaction whose message 2 is
a 24 byte read from the same address. There is no other write path in this file: no SMBus write
helper, no generic "write bytes" function, and no option that can change the register or the
device address. Writing to the wrong device on a GPU I2C bus can permanently damage hardware.
The character device is opened O_RDONLY as well, so the descriptor itself cannot be written.

Protocol constants (chip address, register, block layout, the supported card allowlist) live in
astral_i2c.py at the repo root and are imported from there rather than restated, so the Windows
and Linux paths cannot drift. See that file's header for how they were verified against real
hardware. One address difference to keep straight: NVAPI wants the device address shifted left
by one (0x56), the Linux i2c-dev interface wants the plain 7 bit address (0x2B). Same chip.

Exit codes:
    0  a plausible read succeeded (also --list, --help, and Ctrl-C)
    1  environment problem (not Linux, not root, no /dev/i2c-* nodes, bad option value)
    2  no supported ROG Astral card found (see --allow-unknown)
    3  no candidate adapter returned plausible data
"""

import argparse
import ctypes
import os
import re
import sys
import time
from typing import List, NamedTuple, Optional, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# astral_i2c only touches ctypes/sys/typing at import time and loads nvapi64.dll lazily inside a
# method, so importing it here is safe on Linux (and on Windows, which is where the tests run).
import astral_i2c  # noqa: E402

# ------------------------------------------------------------------ chip access
# NVAPI takes the address pre-shifted (astral_i2c.IT8915_I2C_ADDRESS == 0x56). The kernel
# i2c-dev interface does the shifting itself and wants the 7 bit address, so use 0x2B here.
IT8915_ADDR_7BIT = 0x2B
IT8915_REG_START = astral_i2c.IT8915_REG_START      # 0x80
IT8915_DATA_SIZE = astral_i2c.IT8915_DATA_SIZE      # 24

# ------------------------------------------------------------- linux i2c-dev ABI
# From include/uapi/linux/i2c-dev.h and include/uapi/linux/i2c.h. These are plain ioctl numbers,
# not _IO() encoded ones.
I2C_FUNCS = 0x0705
I2C_RDWR = 0x0707
I2C_M_RD = 0x0001           # the only i2c_msg flag used here
I2C_FUNC_I2C = 0x00000001   # adapter can do plain (non SMBus) transactions

PCI_DEVICES_ROOT = "/sys/bus/pci/devices"
I2C_DEVICES_ROOT = "/sys/bus/i2c/devices"
I2C_ADAPTER_ROOT = "/sys/class/i2c-adapter"
DEV_ROOT = "/dev"

PCI_VENDOR_NVIDIA = 0x10DE
PCI_CLASS_DISPLAY = 0x03

NVIDIA_SMI_QUERY = "nvidia-smi --query-gpu=name,pci.sub_device_id --format=csv"

_ADAPTER_DIR_RE = re.compile(r"^i2c-(\d+)$")
_ADAPTER_NAME_PORT_RE = re.compile(r"adapter\s+(\d+)", re.IGNORECASE)


class I2cMsg(ctypes.Structure):
    """struct i2c_msg. addr is the 7 bit address; the kernel shifts it onto the wire."""
    _fields_ = [
        ("addr", ctypes.c_uint16),                  # 0
        ("flags", ctypes.c_uint16),                 # 2
        ("len", ctypes.c_uint16),                   # 4  (+2 pad)
        ("buf", ctypes.POINTER(ctypes.c_uint8)),    # 8
    ]


class I2cRdwrIoctlData(ctypes.Structure):
    """struct i2c_rdwr_ioctl_data: a pointer to the message array plus the message count."""
    _fields_ = [
        ("msgs", ctypes.POINTER(I2cMsg)),           # 0
        ("nmsgs", ctypes.c_uint32),                 # sizeof(void *)
    ]


class AdapterError(RuntimeError):
    """This adapter cannot serve the read. Try the next one."""


def assert_transaction_shape() -> None:
    """A wrong struct layout would put a malformed request on the bus. Fail before that.

    Also re-checks the two constants shared with astral_i2c, so a change there cannot silently
    leave this file pointing at a different chip or register.
    """
    ptr = ctypes.sizeof(ctypes.c_void_p)
    checks = [
        ("I2cMsg.addr", I2cMsg.addr.offset, 0),
        ("I2cMsg.flags", I2cMsg.flags.offset, 2),
        ("I2cMsg.len", I2cMsg.len.offset, 4),
        ("I2cMsg.buf", I2cMsg.buf.offset, 8),
        ("sizeof(I2cMsg)", ctypes.sizeof(I2cMsg), 8 + ptr),
        ("I2cRdwrIoctlData.msgs", I2cRdwrIoctlData.msgs.offset, 0),
        ("I2cRdwrIoctlData.nmsgs", I2cRdwrIoctlData.nmsgs.offset, ptr),
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


def _ioctl(fd: int, request: int, arg):
    """The one and only ioctl call site in this file.

    fcntl is imported here, not at module scope, so the module imports and shows --help on a
    machine that has no fcntl (Windows), which is where its unit tests run.
    """
    import fcntl
    return fcntl.ioctl(fd, request, arg)


def read_pin_block(fd: int) -> bytes:
    """One combined read transaction against the IT8915. The only bus access in this file.

    Message 1: write the single register pointer byte 0x80 to 0x2B (1 byte, no flags).
    Message 2: read 24 bytes from 0x2B (I2C_M_RD), with a repeated start, no stop in between.
    Nothing else is ever written.
    """
    reg = (ctypes.c_uint8 * 1)(IT8915_REG_START)
    data = (ctypes.c_uint8 * IT8915_DATA_SIZE)()

    msgs = (I2cMsg * 2)()
    msgs[0].addr = IT8915_ADDR_7BIT
    msgs[0].flags = 0                       # write, and the only byte is the register pointer
    msgs[0].len = 1
    msgs[0].buf = ctypes.cast(reg, ctypes.POINTER(ctypes.c_uint8))
    msgs[1].addr = IT8915_ADDR_7BIT
    msgs[1].flags = I2C_M_RD
    msgs[1].len = IT8915_DATA_SIZE
    msgs[1].buf = ctypes.cast(data, ctypes.POINTER(ctypes.c_uint8))

    payload = I2cRdwrIoctlData(msgs=ctypes.cast(msgs, ctypes.POINTER(I2cMsg)), nmsgs=2)
    _ioctl(fd, I2C_RDWR, payload)
    # reg/data/msgs stay referenced by this frame until here, so the kernel never sees freed
    # memory behind those pointers.
    return bytes(data)


def adapter_functionality(fd: int) -> Optional[int]:
    """I2C_FUNCS: ask the driver what it can do. Pure query, nothing reaches the bus."""
    buf = ctypes.c_ulong(0)
    try:
        _ioctl(fd, I2C_FUNCS, buf)
    except OSError:
        return None
    return buf.value


def read_from_adapter(dev_path: str) -> bytes:
    """Open one adapter read-only, take a single sample, close it again."""
    fd = os.open(dev_path, os.O_RDONLY)
    try:
        funcs = adapter_functionality(fd)
        if funcs is not None and not funcs & I2C_FUNC_I2C:
            raise AdapterError(
                f"adapter reports no plain I2C support (funcs {funcs:#010x}), SMBus only"
            )
        return read_pin_block(fd)
    finally:
        os.close(fd)


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


# ------------------------------------------------------------------- decoding
def hex_dump(raw: bytes) -> str:
    return " ".join(f"{b:02X}" for b in raw)


def block_verdict(raw: bytes) -> Tuple[bool, str]:
    """Is this 24 byte block a believable sample? Returns (ok, reason).

    An adapter that is not the monitor chip typically answers all 0xFF (nothing acked, bus
    pulled high) or all 0x00, and either would decode into something that looks like data.
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
        if not astral_i2c.is_plausible(volts, amps):
            return False, f"pin{pin} implausible (V={volts:.3f} A={amps:.3f})"
    return True, "plausible"


def format_sample(pins: List[Tuple[int, float, float]], show_volts: bool) -> str:
    """One line: timestamp, the six currents, the total, and the hottest pin."""
    amps = [a for _pin, _v, a in pins]
    max_pin, _mv, max_amps = max(pins, key=lambda p: p[2])
    parts = [time.strftime("%H:%M:%S"), " A " + " ".join(f"{a:6.3f}" for a in amps)]
    if show_volts:
        parts.append(" V " + " ".join(f"{v:6.3f}" for _pin, v, _a in pins))
    parts.append(f" total {sum(amps):6.3f} A")
    parts.append(f" max pin{max_pin} {max_amps:.3f} A")
    line = " ".join(parts)
    if not all(astral_i2c.is_plausible(v, a) for _pin, v, a in pins):
        line += "   IMPLAUSIBLE"
    return line


# ----------------------------------------------------------------------- CLI
def preflight(require_root: bool) -> Optional[str]:
    """Environment checks. Returns an error message, or None when it is safe to proceed."""
    if not sys.platform.startswith("linux"):
        return (f"this proof of concept is Linux only (running on {sys.platform}).\n"
                "On Windows use tools/astral_poc.py, which reads the same chip through NVAPI.")

    if require_root and os.geteuid() != 0:
        return ("root required: /dev/i2c-* is not readable by ordinary users.\n"
                "Re-run it as: sudo python3 tools/astral_poc_linux.py")

    nodes = [n for n in os.listdir(DEV_ROOT) if _ADAPTER_DIR_RE.match(n)] \
        if os.path.isdir(DEV_ROOT) else []
    if not nodes:
        return ("no /dev/i2c-* nodes, so the i2c-dev interface is not available.\n"
                "Load it with: sudo modprobe i2c-dev   (persist it with "
                "'echo i2c-dev | sudo tee /etc/modules-load.d/i2c-dev.conf')")
    return None


def _adapter_index(entry: str) -> int:
    match = _ADAPTER_DIR_RE.match(entry)
    return int(match.group(1)) if match else -1


def describe_all_adapters() -> None:
    """Every i2c-dev adapter on the machine, GPU or not. Diagnosis material, nothing is opened."""
    try:
        entries = sorted(os.listdir(I2C_DEVICES_ROOT), key=_adapter_index)
    except OSError:
        print("  (cannot read " + I2C_DEVICES_ROOT + ")")
        return
    shown = 0
    for entry in entries:
        if not _ADAPTER_DIR_RE.match(entry):
            continue
        name = _read_text(os.path.join(I2C_DEVICES_ROOT, entry, "name")) or "<no name>"
        node = "" if os.path.exists(os.path.join(DEV_ROOT, entry)) else "   (no /dev node)"
        print(f"  {entry:<10} {name}{node}")
        shown += 1
    if not shown:
        print("  none")


def describe_adapters(adapters: List[Adapter]) -> None:
    if not adapters:
        print("  none found")
        return
    for adapter in adapters:
        why = []
        if adapter.owned_by_path:
            why.append("sysfs path")
        if adapter.name_matches:
            why.append("name")
        print(f"  {adapter.dev_path:<14} {adapter.name or '<no name>'}")
        print(f"      matched by {' + '.join(why)}; sysfs {adapter.sysfs_path}")


def probe_adapters(adapters: List[Adapter]) -> Tuple[Optional[Adapter], Optional[bytes], bool]:
    """Try each candidate until one returns a plausible block. Returns (adapter, raw, saw_zeros)."""
    saw_zeros = False
    for adapter in adapters:
        label = f"  {adapter.dev_path} ({adapter.name or 'no name'})"
        try:
            raw = read_from_adapter(adapter.dev_path)
        except (OSError, AdapterError) as ex:
            print(f"{label}: {ex}", flush=True)
            continue
        ok, reason = block_verdict(raw)
        print(f"{label}: {reason}", flush=True)
        if not ok:
            print(f"      raw {hex_dump(raw)}", flush=True)
            if all(b == 0x00 for b in raw):
                saw_zeros = True
            continue
        return adapter, raw, saw_zeros
    return None, None, saw_zeros


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="astral_poc_linux.py",
        description=("Read ASUS ROG Astral 12VHPWR per-pin values over Linux i2c-dev. "
                     "Read-only: the only byte sent is the register pointer 0x80."),
        epilog=("Needs root and the i2c-dev module (sudo modprobe i2c-dev). "
                "Exit codes: 0 ok, 1 environment, 2 no supported card, 3 no working adapter."),
    )
    parser.add_argument("--list", action="store_true",
                        help="list the candidate I2C adapters for the card and exit")
    parser.add_argument("--adapter", type=int, metavar="N",
                        help="force /dev/i2c-N instead of probing the candidates")
    parser.add_argument("--once", action="store_true", help="take a single sample then exit")
    parser.add_argument("--interval", type=float, default=1.0, metavar="SECONDS",
                        help="seconds between samples (default 1.0)")
    parser.add_argument("--volts", action="store_true", help="also print the per-pin voltages")
    parser.add_argument("--raw", action="store_true", help="also print the 24 raw bytes as hex")
    parser.add_argument("--allow-unknown", action="store_true",
                        help="probe an NVIDIA card that is not in the supported list (still read-only)")
    args = parser.parse_args(argv)

    if args.interval <= 0:
        print("--interval must be greater than zero")
        return 1

    problem = preflight(require_root=not args.list)
    if problem:
        print(problem)
        return 1
    if args.list and sys.platform.startswith("linux") and os.geteuid() != 0:
        print("note: not running as root, so the listing works but reading will not.\n")

    assert_transaction_shape()

    gpus = find_nvidia_gpus()
    if not gpus:
        print("no NVIDIA display adapter found in /sys/bus/pci/devices.")
        print("Per-pin monitoring needs the IT8915 chip that only ROG Astral cards carry.")
        return 2

    print("NVIDIA display adapters:")
    for gpu in gpus:
        tag = f"ASTRAL, {gpu.model}" if gpu.model else "not a supported Astral"
        print(f"  {gpu.address}  device 0x{gpu.device_id:04X}  subsystem {gpu.subsystem_label}"
              f"  -> {tag}")

    target = pick_astral(gpus)
    if target is None:
        gpu = gpus[0]
        print(f"\n{gpu.address} is NVIDIA but subsystem {gpu.subsystem_label} is not in the")
        print("supported list, so per-pin data is not expected here.")
        print("If this is an Astral variant, confirm the id with:")
        print(f"  {NVIDIA_SMI_QUERY}")
        print("and open an issue with the output; adding a verified id is a one line change.")
        if not args.allow_unknown:
            print("\nStopping. Re-run with --allow-unknown to probe it anyway (still read-only).")
            return 2
        print("\n--allow-unknown given, probing anyway. Nothing is written either way.")
        target = gpu
    else:
        print(f"\nTarget: {target.model} at {target.address} (subsystem {target.subsystem_label})")

    adapters = find_gpu_i2c_adapters(target.address)
    if not adapters and os.path.isdir(I2C_ADAPTER_ROOT):
        # Same adapters, different view of them. Worth a second look if the bus view was empty.
        adapters = find_gpu_i2c_adapters(target.address, i2c_root=I2C_ADAPTER_ROOT)

    if args.list:
        print(f"\nCandidate I2C adapters for {target.address}:")
        describe_adapters(adapters)
        print("\nEvery I2C adapter on this machine:")
        describe_all_adapters()
        return 0

    if args.adapter is not None:
        dev_path = os.path.join(DEV_ROOT, f"i2c-{args.adapter}")
        if not os.path.exists(dev_path):
            print(f"\n{dev_path} does not exist. Try --list.")
            return 1
        forced = next((a for a in adapters if a.index == args.adapter), None)
        if forced is None:
            print(f"\n{dev_path} is not a detected candidate for {target.address}, using it anyway.")
            sysfs_dir = os.path.join(I2C_DEVICES_ROOT, f"i2c-{args.adapter}")
            forced = Adapter(
                index=args.adapter,
                dev_path=dev_path,
                name=_read_text(os.path.join(sysfs_dir, "name")) or "",
                sysfs_path=os.path.realpath(sysfs_dir),
                owned_by_path=False,
                name_matches=False,
                hint_port=None,
            )
        adapters = [forced]
    elif not adapters:
        print(f"\nNo I2C adapter could be tied to {target.address}.")
        print("The NVIDIA driver names its adapters 'NVIDIA i2c adapter N at ...'. If none of")
        print("these look like the GPU, try one by hand with --adapter N (still read-only).")
        print("\nEvery I2C adapter on this machine:")
        describe_all_adapters()
        return 3

    print("\nProbing adapters (read-only, register 0x80, 24 bytes):")
    adapter, _first_sample, saw_zeros = probe_adapters(adapters)
    if adapter is None:
        print("\nNo adapter returned plausible per-pin data.")
        if saw_zeros:
            print("One adapter answered all zeros. On a BTF board the card draws through the")
            print("GC-HPWR slot connector and the card-side sensors carry no load, which reads")
            print("exactly like this. See the BTF note in README.md.")
        print("Send the lines above plus the output of 'sudo i2cdetect -l' for diagnosis.")
        return 3

    print(f"\nReading {target.model or 'NVIDIA GPU'} on {adapter.dev_path}"
          f" every {args.interval:g}s. Ctrl-C to stop.\n")
    print("pin".rjust(11) + " " + " ".join(f"{i:6d}" for i in range(1, 7)))

    failures = 0
    fd = os.open(adapter.dev_path, os.O_RDONLY)
    try:
        while True:
            try:
                raw = read_pin_block(fd)
            except OSError as ex:
                failures += 1
                print(f"read failed: {ex}", flush=True)
                if args.once or failures >= astral_i2c.ASTRAL_MAX_CONSECUTIVE_FAILURES:
                    return 3
                time.sleep(args.interval)
                continue
            failures = 0
            print(format_sample(astral_i2c.parse_pin_block(raw), args.volts), flush=True)
            if args.raw:
                print("raw".rjust(11) + " " + hex_dump(raw), flush=True)
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
        return 0
    finally:
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main())

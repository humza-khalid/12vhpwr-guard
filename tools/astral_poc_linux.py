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
    sudo python3 tools/astral_poc_linux.py --adapter 1 --raw --volts

READ-ONLY, with no exception. The only byte this program ever puts on the I2C bus is the single
register pointer 0x80, and it goes out inside a read: read_write is always I2C_SMBUS_READ, on
the one transaction this tool performs. That transaction, and the rest of the read path
(discovery, the plausibility gate), now lives in linux/astral_linux.py, the sensor backend the
Linux daemon uses, so a run of this tool on real hardware proves the daemon's own read path.
There is no write path here or there at all: no SMBus write helper, no generic "put bytes"
function, and no option that can change the register or the device address. Writing to the
wrong device on a GPU I2C bus can permanently damage hardware. The character device is opened
O_RDONLY as well; i2c-dev does not gate its ioctl handlers on the file mode, so read-only is
enough to read and is one less way to write.

The transaction is the SMBus "I2C block data" read (the I2C_SMBUS ioctl with size
I2C_SMBUS_I2C_BLOCK_DATA), and that is not a style choice. On the NVIDIA i2c adapters a plain
repeated-START multi-byte read, a one byte write message followed by a read message in a single
combined transfer, returns all zeros silently: the ioctl succeeds, the chip is never really
spoken to, and the buffer decodes to a perfectly healthy 0.000 A on every pin. Measured by the
ksokolowski/astral-hwmon project on 2026-08-13 (their driver and their independent userspace
oracle both say so in as many words), and reproduced here on 2026-09-05 on a ROG Astral RTX 5090
OC under Linux Mint. That path is not kept anywhere, not even as a fallback: a silent wrong
answer about pin currents is worse than no answer.

astral-hwmon also established which adapter to expect. Of the card's seven adapters only
"NVIDIA i2c adapter 1" answers at 0x2B; the other six return -EIO. All candidates are still
tried, because trying costs nothing and proves the negative.

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
import os
import sys
import time
from typing import List, Optional, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LINUX_DIR = os.path.join(_REPO_ROOT, "linux")
for _path in (_REPO_ROOT, _LINUX_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# astral_i2c only touches ctypes/sys/typing at import time and loads nvapi64.dll lazily inside a
# method, so importing it here is safe on Linux (and on Windows, which is where the tests run).
import astral_i2c  # noqa: E402

# The read path itself lives in linux/astral_linux.py, shared with the Linux daemon, so the
# thing this tool proves on real hardware is the thing the guard runs. Everything is re-imported
# under its old name, so this module's public surface (and its tests) did not change.
import astral_linux  # noqa: E402
from astral_linux import (  # noqa: E402,F401
    DEV_ROOT,
    I2C_ADAPTER_ROOT,
    I2C_DEVICES_ROOT,
    I2C_FUNC_SMBUS_READ_I2C_BLOCK,
    I2C_FUNCS,
    I2C_SLAVE,
    I2C_SMBUS,
    I2C_SMBUS_BLOCK_MAX,
    I2C_SMBUS_I2C_BLOCK_DATA,
    I2C_SMBUS_READ,
    IT8915_ADDR_7BIT,
    IT8915_DATA_SIZE,
    IT8915_REG_START,
    PCI_CLASS_DISPLAY,
    PCI_DEVICES_ROOT,
    PCI_VENDOR_NVIDIA,
    PROBE_MAX_AMPS,
    PROBE_MAX_VOLTS,
    PROBE_MIN_VOLTS,
    _ADAPTER_DIR_RE,
    Adapter,
    AdapterError,
    GpuInfo,
    I2cSmbusData,
    I2cSmbusIoctlData,
    _address_variants,
    _candidate_sort_key,
    _ioctl,
    _pin_plausible,
    _read_text,
    assert_transaction_shape,
    block_verdict,
    discover_adapters,
    find_gpu_i2c_adapters,
    find_nvidia_gpus,
    functionality_note,
    hex_dump,
    pick_astral,
)

NVIDIA_SMI_QUERY = "nvidia-smi --query-gpu=name,pci.sub_device_id --format=csv"

def read_pin_block(fd: int) -> bytes:
    """One SMBus "I2C block data" read: astral_linux.read_pin_block through this module's _ioctl.

    _ioctl is looked up on this module at call time, so the tool keeps one patchable seam of its
    own (which is how tests/test_linux_poc.py fakes the bus) while the transaction itself, and
    its docstring, live in the shared backend.
    """
    return astral_linux.read_pin_block(fd, ioctl=_ioctl)


def adapter_functionality(fd: int) -> Optional[int]:
    """I2C_FUNCS through this module's _ioctl. Pure query, nothing reaches the bus."""
    return astral_linux.adapter_functionality(fd, ioctl=_ioctl)


def read_from_adapter(dev_path: str) -> Tuple[bytes, Optional[str]]:
    """Open one adapter read-only, take a single sample, close it again.

    Returns the raw bytes plus a note about the driver's advertised functionality (None when
    there is nothing to say), so the caller decides how to show it.
    """
    fd = os.open(dev_path, os.O_RDONLY)
    try:
        note = functionality_note(adapter_functionality(fd))
        return read_pin_block(fd), note
    finally:
        os.close(fd)


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
    # The same window block_verdict used to accept the adapter, so a stream that quietly
    # degrades to zeros is tagged rather than printed as a healthy looking 0.000 A.
    if not all(_pin_plausible(v, a) for _pin, v, a in pins):
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
            raw, note = read_from_adapter(adapter.dev_path)
        except (OSError, AdapterError) as ex:
            print(f"{label}: {ex}", flush=True)
            continue
        if note:
            print(f"{label}: note: {note}", flush=True)
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

    # The bus view of sysfs first, the class view if that was empty: same adapters, two views.
    adapters = discover_adapters(target.address)

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
        print("The NVIDIA driver names its adapters 'NVIDIA i2c adapter N at ...', and the")
        print("monitor chip sits behind adapter 1. If none of these look like the GPU, try one")
        print("by hand with --adapter N (still read-only).")
        print("\nEvery I2C adapter on this machine:")
        describe_all_adapters()
        return 3

    print("\nProbing adapters (read-only SMBus block read, register 0x80, 24 bytes):")
    adapter, _first_sample, saw_zeros = probe_adapters(adapters)
    if adapter is None:
        print("\nNo adapter returned plausible per-pin data.")
        print("Only 'NVIDIA i2c adapter 1' is expected to carry the IT8915. On this card the")
        print("other adapters do not answer at 0x2B at all, so a plausible frame from one of")
        print("them would be a surprise worth reporting rather than a result to trust.")
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
            except (OSError, AdapterError) as ex:
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

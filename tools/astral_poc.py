"""
Phase 1 proof-of-concept: read ASUS ROG Astral per-pin 12VHPWR currents directly from the
card's ITE IT8915FN monitor chip over the GPU's I2C bus, via NVAPI. No HWiNFO involved.

READ-ONLY. This file must never bind or call NvAPI_I2CWriteEx (0x283AC65A). Writes to the
wrong device on a GPU I2C bus can permanently damage hardware.

Usage:
    python tools/astral_poc.py                # 1 Hz table until Ctrl+C
    python tools/astral_poc.py --once         # single sample
    python tools/astral_poc.py --once --raw   # also dump the 24 raw bytes
    python tools/astral_poc.py --crosscheck   # 60 s A/B against HWiNFO shared memory

=============================================================================
VERIFIED CONSTANTS (2026-08-04) - dual witness, see STANDALONE_PLAN.md Phase 0
  A: Timic3/astral-power-monitoring src/monitor.rs (branch master)
     + its dep nvapi-sys 0.1.3 (arcnmx/nvapi-rs sys/src/{i2c,nvid,types}.rs)
  B: LibreHardwareMonitor master, Interop/NvApi.cs + Hardware/Gpu/NvidiaGpu.cs
     (TryReadAstral12VHPwrPinSensors)

  NV_I2C_INFO_V3 is 64 bytes on x64; version = sizeof | (3 << 16) = 0x00030040.
  The "_EX_V3" struct used by witness A is byte-identical - its pbRead/cbSize/i2cSpeedKhz
  sit at the same offsets 40/44/48 as non-EX cbSize/i2cSpeed/i2cSpeedKhz, and both
  witnesses write 24 / 0xFFFF / 4 there.

  Data: 24 bytes = 6 blocks x (u16 BE voltage mV, u16 BE current mA), blocks in REVERSE
  pin order. Pin1 = bytes 20/22 ... Pin6 = bytes 0/2. Scale x0.001.
=============================================================================
"""

import argparse
import ctypes
import os
import sys
import time
from typing import List, Optional, Tuple

# ---------------------------------------------------------------- NVAPI ids
NVAPI_INITIALIZE = 0x0150E828
NVAPI_UNLOAD = 0xD22BDD7E
NVAPI_ENUM_PHYSICAL_GPUS = 0xE5AC921F
NVAPI_GPU_GET_PCI_IDENTIFIERS = 0x2DDFB66E
NVAPI_GPU_GET_FULL_NAME = 0xCEEE8E9F
NVAPI_I2C_READ_EX = 0x4D7B0709
# NvAPI_I2CWriteEx is 0x283AC65A. Recorded so nobody re-derives it. NEVER BIND IT.

NVAPI_OK = 0
NVAPI_MAX_PHYSICAL_GPUS = 64
NVAPI_SHORT_STRING_MAX = 64

# ------------------------------------------------------------ IT8915 access
IT8915_I2C_ADDRESS = 0x2B << 1      # 0x56 - NVAPI wants the address shifted left by one
IT8915_REG_START = 0x80
IT8915_DATA_SIZE = 24
I2C_PORT_ID = 0x1                   # verified: 1, NOT 4
I2C_SPEED_DEPRECATED = 0xFFFF
I2C_SPEED_100KHZ = 4

# subsystem id -> marketing name (LibreHardwareMonitor's list + field-verified additions)
ASTRAL_SUBSYSTEM_IDS = {
    0x89EA1043: "ROG Astral RTX 5090D OC",
    0x8A611043: "ROG Astral RTX 5090 Matrix",
    0x89EC1043: "ROG Astral RTX 5090 LC",
    0x89E31043: "ROG Astral RTX 5090 OC",
    0x89DE1043: "ROG Astral RTX 5080 OC",
    0x8A2E1043: "ROG Astral RTX 5090 OC White",
    0x8A2B1043: "ROG Astral RTX 5080 OC White",
    0x8A451043: "ROG Astral RTX 5080 OC Hatsune Miku",
    0x8A5A1043: "ROG Astral RTX 5090 BTF OC",
}

# Sanity bounds. A garbage read (0xFFFF -> 65.535) must never look like a real measurement.
PLAUSIBLE_MAX_AMPS = 30.0
PLAUSIBLE_MIN_VOLTS = 0.0
PLAUSIBLE_MAX_VOLTS = 16.0


class NvI2cInfoV3(ctypes.Structure):
    """NV_I2C_INFO_V3. Field order and padding verified against both witnesses."""
    _pack_ = 8
    _fields_ = [
        ("version", ctypes.c_uint32),           # 0
        ("displayMask", ctypes.c_uint32),       # 4
        ("bIsDDCPort", ctypes.c_uint8),         # 8
        ("i2cDevAddress", ctypes.c_uint8),      # 9   (+6 pad)
        ("pbI2cRegAddress", ctypes.c_void_p),   # 16
        ("regAddrSize", ctypes.c_uint32),       # 24  (+4 pad)
        ("pbData", ctypes.c_void_p),            # 32
        ("cbSize", ctypes.c_uint32),            # 40
        ("i2cSpeed", ctypes.c_uint32),          # 44
        ("i2cSpeedKhz", ctypes.c_uint32),       # 48
        ("portId", ctypes.c_uint8),             # 52  (+3 pad)
        ("bIsPortIdSet", ctypes.c_uint32),      # 56  (+4 trailing pad) => 64
    ]


def _assert_struct_layout() -> None:
    """A wrong layout would send a malformed request to the I2C bus. Fail loudly first."""
    expected = [
        ("version", 0), ("displayMask", 4), ("bIsDDCPort", 8), ("i2cDevAddress", 9),
        ("pbI2cRegAddress", 16), ("regAddrSize", 24), ("pbData", 32), ("cbSize", 40),
        ("i2cSpeed", 44), ("i2cSpeedKhz", 48), ("portId", 52), ("bIsPortIdSet", 56),
    ]
    size = ctypes.sizeof(NvI2cInfoV3)
    if size != 64:
        raise RuntimeError(f"NvI2cInfoV3 is {size} bytes, expected 64 - refusing to touch I2C")
    for name, off in expected:
        actual = getattr(NvI2cInfoV3, name).offset
        if actual != off:
            raise RuntimeError(
                f"NvI2cInfoV3.{name} at offset {actual}, expected {off} - refusing to touch I2C"
            )


NV_I2C_INFO_VER3 = (3 << 16) | 64


class NvapiError(RuntimeError):
    pass


class Nvapi:
    """Minimal read-only NVAPI binding."""

    def __init__(self):
        if sys.maxsize <= 2 ** 32:
            raise NvapiError("64-bit Python required (nvapi64.dll)")
        try:
            self._dll = ctypes.WinDLL("nvapi64.dll")
        except OSError as ex:
            raise NvapiError(f"nvapi64.dll not found - is an NVIDIA driver installed? ({ex})")

        query = self._dll.nvapi_QueryInterface
        query.argtypes = [ctypes.c_uint32]
        query.restype = ctypes.c_void_p
        self._query = query

        self._initialize = self._bind(NVAPI_INITIALIZE, ctypes.c_int)
        self._unload = self._bind(NVAPI_UNLOAD, ctypes.c_int)
        self._enum_gpus = self._bind(
            NVAPI_ENUM_PHYSICAL_GPUS, ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32),
        )
        self._get_pci_ids = self._bind(
            NVAPI_GPU_GET_PCI_IDENTIFIERS, ctypes.c_int,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
        )
        self._get_full_name = self._bind(
            NVAPI_GPU_GET_FULL_NAME, ctypes.c_int, ctypes.c_void_p, ctypes.c_char_p
        )
        self._i2c_read_ex = self._bind(
            NVAPI_I2C_READ_EX, ctypes.c_int,
            ctypes.c_void_p, ctypes.POINTER(NvI2cInfoV3), ctypes.POINTER(ctypes.c_uint32),
        )

        status = self._initialize()
        if status != NVAPI_OK:
            raise NvapiError(f"NvAPI_Initialize failed (status {status})")
        self._initialized = True

    def _bind(self, func_id: int, restype, *argtypes):
        ptr = self._query(func_id)
        if not ptr:
            raise NvapiError(f"nvapi_QueryInterface({func_id:#010x}) returned NULL")
        return ctypes.CFUNCTYPE(restype, *argtypes)(ptr)

    def enum_gpus(self) -> List[ctypes.c_void_p]:
        handles = (ctypes.c_void_p * NVAPI_MAX_PHYSICAL_GPUS)()
        count = ctypes.c_uint32(0)
        status = self._enum_gpus(handles, ctypes.byref(count))
        if status != NVAPI_OK:
            raise NvapiError(f"NvAPI_EnumPhysicalGPUs failed (status {status})")
        return [ctypes.c_void_p(handles[i]) for i in range(count.value)]

    def gpu_name(self, handle) -> str:
        buf = ctypes.create_string_buffer(NVAPI_SHORT_STRING_MAX)
        if self._get_full_name(handle, buf) != NVAPI_OK:
            return "<unknown>"
        return buf.value.decode("utf-8", errors="replace")

    def pci_ids(self, handle) -> Tuple[int, int, int, int]:
        dev = ctypes.c_uint32(0)
        subsys = ctypes.c_uint32(0)
        rev = ctypes.c_uint32(0)
        ext = ctypes.c_uint32(0)
        status = self._get_pci_ids(
            handle, ctypes.byref(dev), ctypes.byref(subsys),
            ctypes.byref(rev), ctypes.byref(ext),
        )
        if status != NVAPI_OK:
            raise NvapiError(f"NvAPI_GPU_GetPCIIdentifiers failed (status {status})")
        return dev.value, subsys.value, rev.value, ext.value

    def read_pin_block(self, handle) -> bytes:
        """One read-only 24-byte transaction against the IT8915 at 0x2B."""
        data = (ctypes.c_uint8 * IT8915_DATA_SIZE)()
        reg = ctypes.c_uint8(IT8915_REG_START)

        info = NvI2cInfoV3(
            version=NV_I2C_INFO_VER3,
            displayMask=0,
            bIsDDCPort=0,
            i2cDevAddress=IT8915_I2C_ADDRESS,
            pbI2cRegAddress=ctypes.cast(ctypes.byref(reg), ctypes.c_void_p),
            regAddrSize=1,
            pbData=ctypes.cast(data, ctypes.c_void_p),
            cbSize=IT8915_DATA_SIZE,
            i2cSpeed=I2C_SPEED_DEPRECATED,
            i2cSpeedKhz=I2C_SPEED_100KHZ,
            portId=I2C_PORT_ID,
            bIsPortIdSet=1,
        )
        out = ctypes.c_uint32(0)
        status = self._i2c_read_ex(handle, ctypes.byref(info), ctypes.byref(out))
        # keep reg/data alive until here
        if status != NVAPI_OK:
            raise NvapiError(f"NvAPI_I2CReadEx failed (status {status})")
        return bytes(data)

    def close(self) -> None:
        if getattr(self, "_initialized", False):
            try:
                self._unload()
            except Exception:
                pass
            self._initialized = False


def parse_pin_block(raw: bytes) -> List[Tuple[int, float, float]]:
    """24 raw bytes -> [(pin_number, volts, amps)] for pins 1..6.

    Blocks are stored in reverse pin order: Pin1 lives at bytes 20-23, Pin6 at 0-3.
    Within a block: voltage at +0, current at +2, both u16 big-endian in mV / mA.
    """
    if len(raw) != IT8915_DATA_SIZE:
        raise ValueError(f"expected {IT8915_DATA_SIZE} bytes, got {len(raw)}")

    def u16_be(off: int) -> int:
        return (raw[off] << 8) | raw[off + 1]

    out = []
    for pin in range(1, 7):
        base = (6 - pin) * 4          # pin1 -> 20, pin2 -> 16, ... pin6 -> 0
        out.append((pin, u16_be(base) * 0.001, u16_be(base + 2) * 0.001))
    return out


def is_plausible(volts: float, amps: float) -> bool:
    return (PLAUSIBLE_MIN_VOLTS <= volts <= PLAUSIBLE_MAX_VOLTS) and (0.0 <= amps <= PLAUSIBLE_MAX_AMPS)


def find_astral(api: Nvapi, verbose: bool = True):
    """Return (handle, label) for the first Astral card, or (None, None)."""
    gpus = api.enum_gpus()
    if verbose:
        print(f"NVAPI reports {len(gpus)} GPU(s):")

    match = None
    for handle in gpus:
        name = api.gpu_name(handle)
        dev, subsys, rev, _ext = api.pci_ids(handle)
        known = ASTRAL_SUBSYSTEM_IDS.get(subsys)
        if verbose:
            tag = f"ASTRAL - {known}" if known else "not an Astral (per-pin unsupported)"
            print(f"  {name}")
            print(f"    deviceId=0x{dev:08X}  subSystemId=0x{subsys:08X}  rev=0x{rev:X}  -> {tag}")
        if known and match is None:
            match = (handle, f"{name} [{known}]")
    return match if match else (None, None)


def print_table(pins: List[Tuple[int, float, float]], raw: Optional[bytes] = None) -> None:
    total_a = sum(a for _, _, a in pins)
    total_w = sum(v * a for _, v, a in pins)
    print(f"  {'Pin':<5} {'Volts':>8} {'Amps':>8} {'Watts':>9}   note")
    for pin, volts, amps in pins:
        note = "" if is_plausible(volts, amps) else "IMPLAUSIBLE"
        print(f"  {pin:<5} {volts:>8.3f} {amps:>8.3f} {volts * amps:>9.2f}   {note}")
    print(f"  {'TOTAL':<5} {'':>8} {total_a:>8.3f} {total_w:>9.2f}")
    if raw is not None:
        print("  raw:", " ".join(f"{b:02X}" for b in raw))


def run_crosscheck(api: Nvapi, handle, seconds: int = 60) -> int:
    """Sample direct I2C and HWiNFO back to back; report per-pin agreement."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import hwinfo_12vhpwr_guard as g

    print(f"Cross-checking against HWiNFO for {seconds}s (needs HWiNFO running with SM2)...")
    try:
        h_map, view_ptr, h_mutex = g.open_hwinfo_sm2()
    except Exception as ex:
        print(f"  HWiNFO shared memory unavailable: {ex}")
        print("  Start HWiNFO64 with Shared Memory Support enabled, then retry.")
        return 1

    worst = [0.0] * 6
    total = [0.0] * 6
    samples = 0
    # Ordering proof: accumulate error for our mapping vs the reversed one. If our pin
    # order were flipped, the reversed variant would fit HWiNFO better. Comparing the two
    # works even when pins are nearly balanced, unlike "which pin is highest".
    err_asis = 0.0
    err_reversed = 0.0
    last_pair = None

    try:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                direct = parse_pin_block(api.read_pin_block(handle))
                hw = g.find_pin_currents(g.read_entries(view_ptr, h_mutex))
            except Exception as ex:
                print(f"  sample failed: {ex}")
                time.sleep(1.0)
                continue

            if len(hw) != 6:
                print(f"  HWiNFO returned {len(hw)} pins, need 6 - is the card an Astral?")
                return 1

            samples += 1
            d_amps = [a for _, _, a in direct]
            h_amps = [a for _, a, _ in hw]
            last_pair = (d_amps, h_amps)

            for i in range(6):
                delta = abs(d_amps[i] - h_amps[i])
                total[i] += delta
                worst[i] = max(worst[i], delta)
                err_asis += delta
                err_reversed += abs(d_amps[5 - i] - h_amps[i])

            time.sleep(1.0)
    finally:
        g.close_sm2(h_map, view_ptr, h_mutex)

    if not samples:
        print("  no samples collected")
        return 1

    if last_pair:
        print("\n  last sample, amps side by side:")
        print("    pin      1      2      3      4      5      6")
        print("    direct " + " ".join(f"{v:6.3f}" for v in last_pair[0]))
        print("    hwinfo " + " ".join(f"{v:6.3f}" for v in last_pair[1]))

    print(f"\n  {samples} samples")
    print(f"  {'Pin':<5} {'mean delta':>12} {'worst delta':>13}   verdict")
    ok = True
    for i in range(6):
        mean = total[i] / samples
        verdict = "ok" if worst[i] < 0.5 else "TOO HIGH"
        if worst[i] >= 0.5:
            ok = False
        print(f"  {i + 1:<5} {mean:>11.3f}A {worst[i]:>12.3f}A   {verdict}")

    n = samples * 6
    print(f"\n  ordering check (total abs error over {n} pin-samples):")
    print(f"    our mapping : {err_asis / n:.4f}A per pin")
    print(f"    if reversed : {err_reversed / n:.4f}A per pin")
    if err_asis < err_reversed * 0.5:
        order_ok, order_note = True, "our mapping fits clearly better - ORDER CONFIRMED"
    elif err_reversed < err_asis * 0.5:
        order_ok, order_note = False, "REVERSED fits better - PIN ORDER IS WRONG"
    else:
        order_ok, order_note = True, ("inconclusive - pins too balanced to distinguish; "
                                      "re-run under load")
    print(f"    -> {order_note}")

    print(f"\n  RESULT: {'PASS' if ok and order_ok else 'FAIL'}")
    return 0 if (ok and order_ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Read Astral 12VHPWR per-pin currents via NVAPI (read-only)")
    ap.add_argument("--once", action="store_true", help="single sample then exit")
    ap.add_argument("--raw", action="store_true", help="also print the raw 24 bytes")
    ap.add_argument("--crosscheck", action="store_true", help="60s A/B comparison against HWiNFO")
    ap.add_argument("--seconds", type=int, default=60, help="cross-check duration")
    args = ap.parse_args()

    _assert_struct_layout()

    try:
        api = Nvapi()
    except NvapiError as ex:
        print(f"NVAPI unavailable: {ex}")
        return 1

    try:
        handle, label = find_astral(api, verbose=not args.crosscheck)
        if handle is None:
            print("\nNo supported ASUS ROG Astral card found. Per-pin monitoring needs the")
            print("IT8915 chip that only the Astral line carries. Nothing was read.")
            return 0

        if args.crosscheck:
            print(f"Astral detected: {label}")
            return run_crosscheck(api, handle, args.seconds)

        print(f"\nReading from: {label}\n")
        while True:
            raw = api.read_pin_block(handle)
            pins = parse_pin_block(raw)
            print(time.strftime("%H:%M:%S"))
            print_table(pins, raw if args.raw else None)
            if args.once:
                return 0
            print()
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 0
    except NvapiError as ex:
        print(f"\nNVAPI error: {ex}")
        return 1
    finally:
        api.close()


if __name__ == "__main__":
    sys.exit(main())

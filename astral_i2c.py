"""
Direct per-pin 12VHPWR monitoring for ASUS ROG Astral cards.

Reads the card's own ITE IT8915FN monitor chip over the GPU I2C bus via NVAPI, so the
guard does not need HWiNFO running. Falls outside this module: the threshold/shutdown
engine, which consumes read_pins() exactly as it consumes the HWiNFO path.

READ-ONLY. This module must never bind or call NvAPI_I2CWriteEx (0x283AC65A). Writes to
the wrong device on a GPU I2C bus can permanently damage hardware.

=============================================================================
VERIFIED CONSTANTS (2026-08-04) - dual witness, see STANDALONE_PLAN.md Phase 0
  A: Timic3/astral-power-monitoring src/monitor.rs (branch master, Unlicense)
     + its dep nvapi-sys 0.1.3 (arcnmx/nvapi-rs sys/src/{i2c,nvid,types}.rs)
  B: LibreHardwareMonitor master, Interop/NvApi.cs + Hardware/Gpu/NvidiaGpu.cs
     (TryReadAstral12VHPwrPinSensors) - protocol reference only, no code copied.

  NV_I2C_INFO_V3 is 64 bytes on x64; version = sizeof | (3 << 16) = 0x00030040.
  The "_EX_V3" struct used by witness A is byte-identical - its pbRead/cbSize/i2cSpeedKhz
  sit at offsets 40/44/48 where non-EX has cbSize/i2cSpeed/i2cSpeedKhz, and both
  witnesses write 24 / 0xFFFF / 4 there.

  portId = 1 (NOT 4). Device address = 0x2B << 1. Register 0x80, 24 bytes.
  Data: 6 blocks x (u16 BE voltage mV, u16 BE current mA) in REVERSE pin order.
  Pin1 = bytes 20/22, Pin2 = 16/18, Pin3 = 12/14, Pin4 = 8/10, Pin5 = 4/6, Pin6 = 0/2.

  Confirmed against real hardware 2026-08-04 on a ROG Astral RTX 5090 OC (0x89E31043):
  values agree with HWiNFO to within 0.06 A worst case.
=============================================================================
"""

import ctypes
import sys
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
IT8915_I2C_ADDRESS = 0x2B << 1
IT8915_REG_START = 0x80
IT8915_DATA_SIZE = 24
I2C_PORT_ID = 0x1
I2C_SPEED_DEPRECATED = 0xFFFF
I2C_SPEED_100KHZ = 4
NV_I2C_INFO_VER3 = (3 << 16) | 64

ASTRAL_SUBSYSTEM_IDS = {
    0x89EA1043: "ROG Astral RTX 5090D OC",
    0x8A611043: "ROG Astral RTX 5090 Matrix",
    0x89EC1043: "ROG Astral RTX 5090 LC",
    0x89E31043: "ROG Astral RTX 5090 OC",
    0x89DE1043: "ROG Astral RTX 5080 OC",
    # Field-verified in issue #3 (working sensor reads) + OpenRGB device registry.
    0x8A2E1043: "ROG Astral RTX 5090 OC White",
    # Field-verified in issue #3: nvidia-smi confirms the id, sensor reads work.
    0x8A2B1043: "ROG Astral RTX 5080 OC White",
    # Id straight from the owner's nvidia-smi (issue #4); official Astral-family SKU.
    0x8A451043: "ROG Astral RTX 5080 OC Hatsune Miku",
    # Field-verified via Reddit (2026-08-21): nvidia-smi id + HWiNFO showing live card-side
    # per-pin data with the card in a STANDARD board on the 16-pin cable. In a BTF board
    # (GC-HPWR slot power) the card-side sensors do not carry the load - see README.
    0x8A5A1043: "ROG Astral RTX 5090 BTF OC",
    # Same card, second subsystem id revision: owner's nvidia-smi in issue #5 plus the
    # OpenRGB registry, which lists 0x8A3C and 0x8A5A side by side for this exact SKU.
    # The BTF board caveat above applies to this id as well.
    0x8A3C1043: "ROG Astral RTX 5090 BTF OC",
}

# Labels must match the HWiNFO backend exactly so downstream code is source-agnostic.
PIN_LABEL_FMT = "GPU 12VHPWR Pin{} Current"

# A garbage read (0xFFFF -> 65.535) must never reach the threshold engine. Real danger
# currents (10-20 A) pass through untouched; only impossible values are rejected.
PLAUSIBLE_MAX_AMPS = 30.0
PLAUSIBLE_MIN_VOLTS = 0.0
PLAUSIBLE_MAX_VOLTS = 16.0

# Consecutive transient failures before we declare the sensor gone.
ASTRAL_MAX_CONSECUTIVE_FAILURES = 5


class SensorGoneError(RuntimeError):
    """The data source died; the caller should tear down and reconnect."""


class TransientReadError(RuntimeError):
    """One sample failed or was implausible. Skip this tick, keep the connection."""


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


_EXPECTED_OFFSETS = [
    ("version", 0), ("displayMask", 4), ("bIsDDCPort", 8), ("i2cDevAddress", 9),
    ("pbI2cRegAddress", 16), ("regAddrSize", 24), ("pbData", 32), ("cbSize", 40),
    ("i2cSpeed", 44), ("i2cSpeedKhz", 48), ("portId", 52), ("bIsPortIdSet", 56),
]


def assert_struct_layout() -> None:
    """A wrong layout would put a malformed request on the I2C bus. Fail before that."""
    size = ctypes.sizeof(NvI2cInfoV3)
    if size != 64:
        raise RuntimeError(f"NvI2cInfoV3 is {size} bytes, expected 64 - refusing to touch I2C")
    for name, off in _EXPECTED_OFFSETS:
        actual = getattr(NvI2cInfoV3, name).offset
        if actual != off:
            raise RuntimeError(
                f"NvI2cInfoV3.{name} at offset {actual}, expected {off} - refusing to touch I2C"
            )


def parse_pin_block(raw: bytes) -> List[Tuple[int, float, float]]:
    """24 raw bytes -> [(pin_number, volts, amps)] for pins 1..6.

    Blocks are stored in reverse pin order: Pin1 at bytes 20-23, Pin6 at 0-3.
    Within a block: voltage at +0, current at +2, u16 big-endian, mV / mA.
    """
    if len(raw) != IT8915_DATA_SIZE:
        raise ValueError(f"expected {IT8915_DATA_SIZE} bytes, got {len(raw)}")

    def u16_be(off: int) -> int:
        return (raw[off] << 8) | raw[off + 1]

    out = []
    for pin in range(1, 7):
        base = (6 - pin) * 4
        out.append((pin, u16_be(base) * 0.001, u16_be(base + 2) * 0.001))
    return out


def is_plausible(volts: float, amps: float) -> bool:
    return (PLAUSIBLE_MIN_VOLTS <= volts <= PLAUSIBLE_MAX_VOLTS) and (0.0 <= amps <= PLAUSIBLE_MAX_AMPS)


class AstralPinSensor:
    """Read-only per-pin sensor backed by the card's IT8915 chip."""

    def __init__(self):
        self._dll = None
        self._query = None
        self._funcs = {}
        self._handle = None
        self._initialized = False
        self._consecutive_failures = 0
        self.gpu_name = None
        self.model = None
        self.subsystem_id: Optional[int] = None
        self.last_voltages: List[float] = []

    # -- NVAPI plumbing -----------------------------------------------------
    def _load(self) -> None:
        if sys.maxsize <= 2 ** 32:
            raise RuntimeError("64-bit Python required for nvapi64.dll")
        assert_struct_layout()

        self._dll = ctypes.WinDLL("nvapi64.dll")
        query = self._dll.nvapi_QueryInterface
        query.argtypes = [ctypes.c_uint32]
        query.restype = ctypes.c_void_p
        self._query = query

        self._funcs["init"] = self._bind(NVAPI_INITIALIZE, ctypes.c_int)
        self._funcs["unload"] = self._bind(NVAPI_UNLOAD, ctypes.c_int)
        self._funcs["enum"] = self._bind(
            NVAPI_ENUM_PHYSICAL_GPUS, ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32),
        )
        self._funcs["pci"] = self._bind(
            NVAPI_GPU_GET_PCI_IDENTIFIERS, ctypes.c_int,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
        )
        self._funcs["name"] = self._bind(
            NVAPI_GPU_GET_FULL_NAME, ctypes.c_int, ctypes.c_void_p, ctypes.c_char_p
        )
        self._funcs["i2c_read"] = self._bind(
            NVAPI_I2C_READ_EX, ctypes.c_int,
            ctypes.c_void_p, ctypes.POINTER(NvI2cInfoV3), ctypes.POINTER(ctypes.c_uint32),
        )

        if self._funcs["init"]() != NVAPI_OK:
            raise RuntimeError("NvAPI_Initialize failed")
        self._initialized = True

    def _bind(self, func_id: int, restype, *argtypes):
        ptr = self._query(func_id)
        if not ptr:
            raise RuntimeError(f"nvapi_QueryInterface({func_id:#010x}) returned NULL")
        return ctypes.CFUNCTYPE(restype, *argtypes)(ptr)

    def _find_astral(self):
        handles = (ctypes.c_void_p * NVAPI_MAX_PHYSICAL_GPUS)()
        count = ctypes.c_uint32(0)
        if self._funcs["enum"](handles, ctypes.byref(count)) != NVAPI_OK:
            raise RuntimeError("NvAPI_EnumPhysicalGPUs failed")

        for i in range(count.value):
            handle = ctypes.c_void_p(handles[i])
            dev = ctypes.c_uint32(0)
            subsys = ctypes.c_uint32(0)
            rev = ctypes.c_uint32(0)
            ext = ctypes.c_uint32(0)
            if self._funcs["pci"](handle, ctypes.byref(dev), ctypes.byref(subsys),
                                  ctypes.byref(rev), ctypes.byref(ext)) != NVAPI_OK:
                continue
            model = ASTRAL_SUBSYSTEM_IDS.get(subsys.value)
            if model:
                buf = ctypes.create_string_buffer(NVAPI_SHORT_STRING_MAX)
                name = (buf.value.decode("utf-8", errors="replace")
                        if self._funcs["name"](handle, buf) == NVAPI_OK else "NVIDIA GPU")
                # Kept so the mitigation side can bind NVML to this same physical card:
                # NVML reports the subsystem id in the identical DWORD format.
                self.subsystem_id = subsys.value
                return handle, name, model
        return None, None, None

    # -- public API ---------------------------------------------------------
    @classmethod
    def detect(cls) -> Optional[str]:
        """Return a description if a supported Astral card is present, else None.

        Never raises: absence of an NVIDIA driver is a normal 'not available' answer.
        """
        probe = cls()
        try:
            probe._load()
            _handle, name, model = probe._find_astral()
            return f"{name} [{model}]" if model else None
        except Exception:
            return None
        finally:
            probe.close()

    def connect(self) -> str:
        """Bind to the Astral card. Raises RuntimeError if unavailable."""
        self._load()
        handle, name, model = self._find_astral()
        if handle is None:
            self.close()
            raise RuntimeError("no supported ASUS ROG Astral GPU found")
        self._handle = handle
        self.gpu_name = name
        self.model = model
        self._consecutive_failures = 0
        # Prove the chip actually answers before declaring success.
        self.read_pins()
        return f"{name} [{model}]"

    def _read_raw(self) -> bytes:
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
        status = self._funcs["i2c_read"](self._handle, ctypes.byref(info), ctypes.byref(out))
        if status != NVAPI_OK:
            raise TransientReadError(f"NvAPI_I2CReadEx status {status}")
        return bytes(data)

    def read_pins(self) -> List[Tuple[str, float, str]]:
        """One sample as [(label, amps, "A")] for pins 1..6, matching the HWiNFO shape.

        Raises TransientReadError for a single bad sample, SensorGoneError once failures
        persist past ASTRAL_MAX_CONSECUTIVE_FAILURES.
        """
        if self._handle is None:
            raise SensorGoneError("not connected")

        try:
            parsed = parse_pin_block(self._read_raw())
            for _pin, volts, amps in parsed:
                if not is_plausible(volts, amps):
                    raise TransientReadError(
                        f"implausible sample (V={volts:.3f} A={amps:.3f})"
                    )
        except TransientReadError:
            self._consecutive_failures += 1
            if self._consecutive_failures >= ASTRAL_MAX_CONSECUTIVE_FAILURES:
                raise SensorGoneError(
                    f"{self._consecutive_failures} consecutive failed I2C reads"
                )
            raise

        self._consecutive_failures = 0
        self.last_voltages = [v for _pin, v, _a in parsed]
        return [(PIN_LABEL_FMT.format(pin), amps, "A") for pin, _volts, amps in parsed]

    def close(self) -> None:
        self._handle = None
        if self._initialized:
            try:
                self._funcs["unload"]()
            except Exception:
                pass
            self._initialized = False
        self._funcs = {}
        self._dll = None
        self._query = None

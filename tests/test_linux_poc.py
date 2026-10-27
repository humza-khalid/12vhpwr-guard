"""Unit tests for tools/astral_poc_linux.py.

These run anywhere, including Windows: the tool imports fcntl lazily inside its single ioctl
call site, and every test that exercises the bus transaction substitutes a fake for it. No
hardware and no /dev/i2c-* node is touched.

Self-contained by design (no conftest dependency): the repo root goes on sys.path here, and the
tool is loaded from its path so the tests work from any working directory.
"""

import ctypes
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TOOL_PATH = REPO_ROOT / "tools" / "astral_poc_linux.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("astral_poc_linux", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


poc = _load_tool()

ASTRAL_5090_OC = 0x89E31043


# ------------------------------------------------------------------- helpers
def build_block(pins):
    """Encode six (volts, amps) pairs the way the chip lays them out: reverse pin order.

    Pin 1 lands in bytes 20..23, pin 6 in bytes 0..3. Voltage first (u16 BE mV), then current
    (u16 BE mA).
    """
    raw = bytearray(24)
    for index, (volts, amps) in enumerate(pins):
        base = (6 - (index + 1)) * 4
        mv = int(round(volts * 1000))
        ma = int(round(amps * 1000))
        raw[base] = (mv >> 8) & 0xFF
        raw[base + 1] = mv & 0xFF
        raw[base + 2] = (ma >> 8) & 0xFF
        raw[base + 3] = ma & 0xFF
    return bytes(raw)


def pci_dir(address):
    """Windows cannot create a directory whose name contains ':', so swap the separator there.

    The tool only carries the directory name through as the address string, it never parses it,
    so the substitution changes nothing the test is checking. On Linux the real names are used.
    """
    return address.replace(":", "-") if os.name == "nt" else address


def write_pci_device(root, address, vendor, device, sub_vendor, sub_device, pci_class):
    path = root / pci_dir(address)
    path.mkdir(parents=True)
    (path / "vendor").write_text(f"0x{vendor:04x}\n")
    (path / "device").write_text(f"0x{device:04x}\n")
    (path / "subsystem_vendor").write_text(f"0x{sub_vendor:04x}\n")
    (path / "subsystem_device").write_text(f"0x{sub_device:04x}\n")
    (path / "class").write_text(f"0x{pci_class:06x}\n")
    return path


class FakeIoctl:
    """Stands in for the module's one ioctl call site and records what it was handed.

    Emulates the kernel side of the two ioctls a read is made of. I2C_SLAVE records the address
    the descriptor was bound to; I2C_SMBUS checks the request is a 24 byte block READ of
    register 0x80, fills block[1..24] with the synthetic frame, and writes the produced count
    back into block[0] the way the kernel does. Any other request is a test failure.
    """

    def __init__(self, payload_bytes=None, errno=None, funcs=None, produced=None):
        self.payload_bytes = payload_bytes
        self.errno = errno
        self.funcs = poc.I2C_FUNC_SMBUS_READ_I2C_BLOCK if funcs is None else funcs
        self.produced = produced     # override the count the kernel reports back
        self.calls = []              # (fd, request) for every ioctl, in order
        self.addresses = []          # every address selected with I2C_SLAVE
        self.reads = []              # (read_write, command, size, requested) per I2C_SMBUS

    def __call__(self, fd, request, arg):
        self.calls.append((fd, request))
        if request == poc.I2C_FUNCS:
            arg.value = self.funcs
            return 0
        if request == poc.I2C_SLAVE:
            assert isinstance(arg, int), "I2C_SLAVE takes the plain 7 bit address"
            self.addresses.append(arg)
            return 0
        if request != poc.I2C_SMBUS:
            raise AssertionError(f"unexpected ioctl request {request:#06x}")
        if self.errno is not None:
            raise OSError(self.errno, "fake failure")

        block = arg.data.contents.block
        requested = block[0]
        self.reads.append((arg.read_write, arg.command, arg.size, requested))
        assert arg.read_write == poc.I2C_SMBUS_READ, "this tool may only ever read"
        assert arg.command == poc.IT8915_REG_START
        assert arg.size == poc.I2C_SMBUS_I2C_BLOCK_DATA
        assert requested == poc.IT8915_DATA_SIZE
        if self.payload_bytes is not None:
            for offset, value in enumerate(self.payload_bytes[:requested]):
                block[1 + offset] = value
        block[0] = requested if self.produced is None else self.produced
        return 0


# --------------------------------------------------------------- struct shape
def test_transaction_shape_holds_on_this_platform():
    poc.assert_transaction_shape()
    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    assert ctypes.sizeof(poc.I2cSmbusIoctlData) == 8 + pointer_size
    assert poc.I2cSmbusIoctlData.read_write.offset == 0
    assert poc.I2cSmbusIoctlData.command.offset == 1
    assert poc.I2cSmbusIoctlData.size.offset == 4
    assert poc.I2cSmbusIoctlData.data.offset == 8
    assert ctypes.sizeof(poc.I2cSmbusData) == poc.I2C_SMBUS_BLOCK_MAX + 2


def test_seven_bit_address_is_the_nvapi_address_unshifted():
    import astral_i2c

    assert poc.IT8915_ADDR_7BIT == 0x2B
    assert astral_i2c.IT8915_I2C_ADDRESS == poc.IT8915_ADDR_7BIT << 1


# ------------------------------------------------------- (a) PCI sysfs parsing
@pytest.fixture()
def pci_root(tmp_path):
    root = tmp_path / "sys" / "bus" / "pci" / "devices"
    root.mkdir(parents=True)
    # The rig: ROG Astral RTX 5090 OC, subsystem 1043:89E3.
    write_pci_device(root, "0000:01:00.0", 0x10DE, 0x2B85, 0x1043, 0x89E3, 0x030000)
    # The same card's HD audio function: NVIDIA, but not a display class device.
    write_pci_device(root, "0000:01:00.1", 0x10DE, 0x22E8, 0x1043, 0x89E3, 0x040300)
    # A second NVIDIA card that is not an Astral.
    write_pci_device(root, "0000:04:00.0", 0x10DE, 0x2705, 0x1462, 0x5110, 0x030000)
    # Not NVIDIA at all.
    write_pci_device(root, "0000:00:02.0", 0x8086, 0xA780, 0x1043, 0x8882, 0x030000)
    return root


def test_find_nvidia_gpus_filters_by_vendor_and_class(pci_root):
    gpus = poc.find_nvidia_gpus(str(pci_root))
    assert [g.address for g in gpus] == [pci_dir("0000:01:00.0"), pci_dir("0000:04:00.0")]


def test_astral_is_recognised_by_subsystem_id(pci_root):
    astral = poc.pick_astral(poc.find_nvidia_gpus(str(pci_root)))
    assert astral is not None
    assert astral.address == pci_dir("0000:01:00.0")
    assert astral.subsystem_id == ASTRAL_5090_OC
    assert astral.model == "ROG Astral RTX 5090 OC"
    assert astral.subsystem_label == "1043:89E3"


def test_unknown_nvidia_card_has_no_model_but_a_readme_style_label(pci_root):
    gpus = poc.find_nvidia_gpus(str(pci_root))
    other = [g for g in gpus if g.address == pci_dir("0000:04:00.0")][0]
    assert other.model is None
    assert other.subsystem_label == "1462:5110"


def test_missing_pci_root_is_not_an_error(tmp_path):
    assert poc.find_nvidia_gpus(str(tmp_path / "nope")) == []


def test_pick_astral_returns_none_without_a_supported_card(pci_root):
    gpus = [g for g in poc.find_nvidia_gpus(str(pci_root)) if g.model is None]
    assert poc.pick_astral(gpus) is None


# ------------------------------------------------ (b) adapter candidate choice
GPU_ADDRESS = "0000:01:00.0"
GPU_SYSFS = "/sys/devices/pci0000:00/0000:00:01.0/0000:01:00.0"


@pytest.fixture()
def i2c_tree(tmp_path):
    """A fake i2c sysfs plus /dev, with a resolver standing in for symlink resolution."""
    i2c_root = tmp_path / "sys" / "bus" / "i2c" / "devices"
    dev_root = tmp_path / "dev"
    i2c_root.mkdir(parents=True)
    dev_root.mkdir(parents=True)

    adapters = {
        # index: (name, resolved sysfs path, create a /dev node)
        0: ("SMBus PIIX4 adapter port 0 at 0b00", "/sys/devices/pci0000:00/0000:00:14.0/i2c-0", True),
        4: ("NVIDIA i2c adapter 1 at 1:00.0", f"{GPU_SYSFS}/i2c-4", True),
        5: ("NVIDIA i2c adapter 4 at 1:00.0", f"{GPU_SYSFS}/i2c-5", True),
        6: ("NVIDIA i2c adapter 5 at 1:00.0", f"{GPU_SYSFS}/i2c-6", False),
        7: ("nvkm-0000:01:00.0-bus-0000", f"{GPU_SYSFS}/i2c-7", True),
        8: ("NVIDIA i2c adapter 2 at 1:00.0", "/sys/devices/platform/relocated/i2c-8", True),
        9: ("NVIDIA i2c adapter 1 at 4:00.0", "/sys/devices/pci0000:00/0000:04:00.0/i2c-9", True),
    }
    resolved = {}
    for index, (name, sysfs_path, make_node) in adapters.items():
        entry = i2c_root / f"i2c-{index}"
        entry.mkdir()
        (entry / "name").write_text(name + "\n")
        resolved[f"i2c-{index}"] = sysfs_path
        if make_node:
            (dev_root / f"i2c-{index}").write_text("")
    # Something in the directory that is not an adapter at all.
    (i2c_root / "0-002b").mkdir()

    def resolve(path):
        return resolved.get(os.path.basename(path), path)

    return i2c_root, dev_root, resolve


def test_adapter_candidates_are_gpu_owned_and_ordered(i2c_tree):
    i2c_root, dev_root, resolve = i2c_tree
    found = poc.find_gpu_i2c_adapters(
        GPU_ADDRESS, i2c_root=str(i2c_root), dev_root=str(dev_root), resolve=resolve
    )
    # 0 and 9 belong to other devices, 6 has no /dev node.
    # Order: sysfs-path matches first, the "adapter 1" hint next, then the index.
    assert [a.index for a in found] == [4, 5, 7, 8]
    by_index = {a.index: a for a in found}
    assert by_index[4].owned_by_path and by_index[4].name_matches
    assert by_index[4].hint_port == 1
    assert by_index[7].owned_by_path and not by_index[7].name_matches   # path only
    assert by_index[8].name_matches and not by_index[8].owned_by_path   # name only
    assert by_index[5].dev_path == os.path.join(str(dev_root), "i2c-5")


def test_adapters_of_another_gpu_are_not_candidates(i2c_tree):
    i2c_root, dev_root, resolve = i2c_tree
    found = poc.find_gpu_i2c_adapters(
        "0000:04:00.0", i2c_root=str(i2c_root), dev_root=str(dev_root), resolve=resolve
    )
    assert [a.index for a in found] == [9]


def test_missing_i2c_root_is_not_an_error(tmp_path):
    assert poc.find_gpu_i2c_adapters(GPU_ADDRESS, i2c_root=str(tmp_path / "nope")) == []


def test_address_variants_cover_the_short_spellings():
    assert poc._address_variants("0000:01:00.0") == ["0000:01:00.0", "01:00.0", "1:00.0"]


# ------------------------------------------------- (c) the bus transaction
PINS = [(11.900, 8.100), (11.910, 8.200), (11.920, 8.300),
        (11.930, 8.400), (11.940, 8.500), (11.950, 20.000)]


def test_read_pin_block_issues_slave_then_one_block_read(monkeypatch):
    import astral_i2c

    block = build_block(PINS)
    fake = FakeIoctl(payload_bytes=block)
    monkeypatch.setattr(poc, "_ioctl", fake)

    raw = poc.read_pin_block(fd=7)

    # Exactly two ioctls, in order, on the caller's descriptor, and nothing else.
    assert fake.calls == [(7, poc.I2C_SLAVE), (7, poc.I2C_SMBUS)]
    assert fake.addresses == [0x2B]                # the 7 bit address, not the NVAPI 0x56
    # Direction, register, transfer kind, requested length. The register pointer 0x80 is the
    # only byte that leaves this program, and it leaves inside a read.
    assert fake.reads == [(poc.I2C_SMBUS_READ, 0x80, poc.I2C_SMBUS_I2C_BLOCK_DATA, 24)]

    assert raw == block
    decoded = astral_i2c.parse_pin_block(raw)
    assert [pin for pin, _v, _a in decoded] == [1, 2, 3, 4, 5, 6]
    for (pin, volts, amps), (want_v, want_a) in zip(decoded, PINS):
        assert volts == pytest.approx(want_v, abs=0.0005), f"pin{pin} volts"
        assert amps == pytest.approx(want_a, abs=0.0005), f"pin{pin} amps"


def test_a_part_filled_block_is_refused_rather_than_decoded(monkeypatch):
    """block[0] comes back as the count the kernel produced; a short one is a transport fault."""
    fake = FakeIoctl(payload_bytes=build_block(PINS), produced=8)
    monkeypatch.setattr(poc, "_ioctl", fake)

    with pytest.raises(poc.AdapterError) as excinfo:
        poc.read_pin_block(fd=3)
    assert "8 bytes" in str(excinfo.value)


def test_a_failing_block_read_surfaces_the_errno(monkeypatch):
    fake = FakeIoctl(errno=5)          # EIO, what the six wrong adapters return
    monkeypatch.setattr(poc, "_ioctl", fake)

    with pytest.raises(OSError):
        poc.read_pin_block(fd=3)
    assert fake.calls == [(3, poc.I2C_SLAVE), (3, poc.I2C_SMBUS)]


def test_pin_one_is_decoded_from_the_last_four_bytes():
    block = build_block(PINS)
    assert block[20:24] == bytes([0x2E, 0x7C, 0x1F, 0xA4])   # 11900 mV, 8100 mA
    assert block[0:4] == bytes([0x2E, 0xAE, 0x4E, 0x20])     # pin 6: 11950 mV, 20000 mA


def test_read_from_adapter_checks_functionality_then_reads(tmp_path, monkeypatch):
    node = tmp_path / "i2c-4"
    node.write_text("")
    fake = FakeIoctl(payload_bytes=build_block(PINS))
    monkeypatch.setattr(poc, "_ioctl", fake)

    raw, note = poc.read_from_adapter(str(node))

    assert [request for _fd, request in fake.calls] == [
        poc.I2C_FUNCS, poc.I2C_SLAVE, poc.I2C_SMBUS
    ]
    assert fake.reads == [(poc.I2C_SMBUS_READ, 0x80, poc.I2C_SMBUS_I2C_BLOCK_DATA, 24)]
    assert note is None
    assert raw == build_block(PINS)


def test_adapter_without_the_block_read_bit_is_noted_not_skipped(tmp_path, monkeypatch):
    """A missing functionality bit is a note. The closed NVIDIA driver is not a reliable oracle,
    and the read is read-only whatever it advertises, so the tool tries anyway."""
    node = tmp_path / "i2c-0"
    node.write_text("")
    fake = FakeIoctl(payload_bytes=build_block(PINS), funcs=0x00080000)   # some other bit
    monkeypatch.setattr(poc, "_ioctl", fake)

    raw, note = poc.read_from_adapter(str(node))

    assert [request for _fd, request in fake.calls] == [
        poc.I2C_FUNCS, poc.I2C_SLAVE, poc.I2C_SMBUS
    ]
    assert raw == build_block(PINS)
    assert note is not None and "0x00080000" in note


def test_functionality_note_is_silent_when_the_bit_is_advertised():
    assert poc.functionality_note(poc.I2C_FUNC_SMBUS_READ_I2C_BLOCK | 0x1) is None
    assert poc.functionality_note(None) is None          # the query itself failed
    assert poc.functionality_note(0) is not None


# ------------------------------------------------------ (d) plausibility gate
# What the rig actually returned on 2026-09-05, from /dev/i2c-1 on a ROG Astral RTX 5090 OC,
# through the repeated-START read that has since been removed. Not all zeros, so it slipped
# past that check, and every pin decoded to about 0.000 A and 0.0 V.
RIG_ZERO_FRAME = bytes.fromhex(
    "00 00 00 00 00 00 00 00 00 01 00 00 00 00 00 01 00 00 00 01 00 30 00 00"
)

# A real idle frame, the first of the thirty astral-hwmon captured on the same model of card
# (its first 24 bytes, the six pin blocks). About 12.17 V and half an amp per pin.
IDLE_FRAME = bytes.fromhex(
    "2f 90 01 f4 2f 88 02 1c 2f 88 01 e0 2f 90 01 f4 2f 88 01 f4 2f 90 01 e0"
)


def test_plausible_block_is_accepted():
    ok, reason = poc.block_verdict(build_block(PINS))
    assert ok, reason


def test_the_rig_frame_from_the_removed_read_path_is_rejected():
    assert len(RIG_ZERO_FRAME) == 24
    assert any(RIG_ZERO_FRAME), "the point of this frame is that it is not all zeros"

    ok, reason = poc.block_verdict(RIG_ZERO_FRAME)
    assert not ok
    assert "pin1" in reason, reason                  # the first pin out of range
    assert "V=0.048" in reason, reason               # named with its values
    assert "A=0.000" in reason, reason


def test_the_rig_frame_would_have_passed_the_runtime_gate():
    """Why this tool needs a tighter window than astral_i2c: 0 V is 'plausible' there."""
    import astral_i2c

    pins = astral_i2c.parse_pin_block(RIG_ZERO_FRAME)
    assert all(astral_i2c.is_plausible(volts, amps) for _pin, volts, amps in pins)
    assert not all(poc._pin_plausible(volts, amps) for _pin, volts, amps in pins)


def test_a_real_idle_frame_is_accepted_and_decodes_to_twelve_volts():
    import astral_i2c

    assert len(IDLE_FRAME) == 24
    ok, reason = poc.block_verdict(IDLE_FRAME)
    assert ok, reason

    for pin, volts, amps in astral_i2c.parse_pin_block(IDLE_FRAME):
        assert 12.1 <= volts <= 12.2, f"pin{pin} volts {volts}"
        assert 0.48 <= amps <= 0.54, f"pin{pin} amps {amps}"


def test_a_sagging_pin_stays_visible_rather_than_being_rejected():
    """The 6.0 V floor is loose on purpose: a failing connector lowers the pin voltage."""
    pins = list(PINS)
    pins[3] = (7.500, 9.000)
    ok, reason = poc.block_verdict(build_block(pins))
    assert ok, reason


def test_a_pin_below_the_floor_is_rejected_by_voltage():
    pins = list(PINS)
    pins[4] = (5.000, 9.000)
    ok, reason = poc.block_verdict(build_block(pins))
    assert not ok
    assert "pin5" in reason
    assert "V=5.000" in reason


def test_a_pin_above_the_ceiling_is_rejected_by_voltage():
    pins = list(PINS)
    pins[0] = (13.500, 9.000)
    ok, reason = poc.block_verdict(build_block(pins))
    assert not ok
    assert "pin1" in reason
    assert "V=13.500" in reason


def test_all_ff_block_is_rejected():
    ok, reason = poc.block_verdict(b"\xff" * 24)
    assert not ok
    assert "0xFF" in reason


def test_all_zero_block_is_rejected():
    ok, reason = poc.block_verdict(bytes(24))
    assert not ok
    assert "zero" in reason


def test_out_of_range_current_is_rejected():
    pins = list(PINS)
    pins[2] = (11.9, 65.535)        # what a 0xFFFF current decodes to
    ok, reason = poc.block_verdict(build_block(pins))
    assert not ok
    assert "pin3" in reason


def test_short_block_is_rejected():
    ok, reason = poc.block_verdict(b"\x01\x02\x03")
    assert not ok
    assert "expected 24" in reason


def test_format_sample_reports_the_max_pin():
    import astral_i2c

    line = poc.format_sample(astral_i2c.parse_pin_block(build_block(PINS)), show_volts=True)
    assert "max pin6 20.000 A" in line
    assert " V " in line
    assert "11.950" in line
    assert "IMPLAUSIBLE" not in line


# ------------------------------------------------------------- (e) entry point
def test_importing_the_module_does_not_pull_in_fcntl():
    """fcntl has to stay inside the ioctl call site, or the module cannot load on Windows."""
    code = (
        "import sys, importlib.util;"
        "before = 'fcntl' in sys.modules;"
        "spec = importlib.util.spec_from_file_location('t', sys.argv[1]);"
        "module = importlib.util.module_from_spec(spec);"
        "spec.loader.exec_module(module);"
        "print(before, 'fcntl' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(TOOL_PATH)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    before, after = result.stdout.split()[-2:]
    assert after == before, "importing the tool must not import fcntl"


def test_help_runs_on_any_platform():
    result = subprocess.run(
        [sys.executable, str(TOOL_PATH), "--help"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "--adapter" in result.stdout
    assert "--list" in result.stdout
    assert "Read-only" in result.stdout or "read-only" in result.stdout


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="checks the non-Linux refusal")
def test_running_off_linux_exits_one():
    result = subprocess.run(
        [sys.executable, str(TOOL_PATH), "--once"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 1
    assert "Linux only" in result.stdout


def test_no_write_path_exists_in_the_module():
    """The read-only guarantee is structural: nothing here may put bytes on the bus.

    Since v1.5 the bus access itself lives in linux/astral_linux.py, whose own test applies
    the full structural ban (READ only, exactly FUNCS/SLAVE/SMBUS, one fcntl call site). What
    is left to hold here: the tool makes no ioctl of its own (it only hands its _ioctl seam to
    the backend), opens every descriptor read-only, and carries none of the banned paths. The
    repeated-START path is on the banned list because it silently returned zeros on this
    hardware, so it must not creep back in as a fallback.
    """
    source = TOOL_PATH.read_text(encoding="utf-8")
    assert "fcntl" not in source, "the only ioctl call site is in linux/astral_linux.py"
    requests = re.findall(r"(?<![\w.])_?ioctl\(\s*\w+\s*,\s*(\w+)", source)
    assert not requests, f"the tool must not issue ioctl requests itself: {requests}"
    assert source.count("ioctl=_ioctl") == 2, "both wrappers hand the backend this module's seam"
    assert re.findall(r"os\.open\([^)]*?,\s*(os\.\w+)", source) == ["os.O_RDONLY"] * 2
    for banned in ("os.write", "O_WRONLY", "O_RDWR", "I2C_SMBUS_WRITE",
                   "write_byte", "write_block", "I2C_RDWR", "I2C_M_RD", "I2C_SLAVE_FORCE"):
        assert banned not in source, f"{banned} must not appear in a read-only tool"

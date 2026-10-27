"""Unit tests for linux/astral_linux.py, the Linux sensor backend.

These run anywhere, including Windows: the module imports fcntl lazily inside its single ioctl
call site, and every test hands the backend a fake sysfs tree, a fake /dev, a fake opener and a
fake ioctl. No hardware and no real /dev/i2c-* node is touched, and no real descriptor is closed.

Self-contained like test_linux_poc.py (no conftest dependency).
"""

import errno
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LINUX_DIR = REPO_ROOT / "linux"
for _p in (str(REPO_ROOT), str(LINUX_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import astral_i2c  # noqa: E402
import astral_linux as al  # noqa: E402

MODULE_PATH = LINUX_DIR / "astral_linux.py"
POC_PATH = REPO_ROOT / "tools" / "astral_poc_linux.py"

ASTRAL_5090_OC = 0x89E31043


# ------------------------------------------------------------------- helpers
def build_block(pins):
    """Encode six (volts, amps) pairs the way the chip lays them out: reverse pin order."""
    raw = bytearray(24)
    for index, (volts, amps) in enumerate(pins):
        base = (6 - (index + 1)) * 4
        mv = int(round(volts * 1000))
        ma = int(round(amps * 1000))
        raw[base:base + 4] = bytes([(mv >> 8) & 0xFF, mv & 0xFF, (ma >> 8) & 0xFF, ma & 0xFF])
    return bytes(raw)


PINS = [(11.900, 8.100), (11.910, 8.200), (11.920, 8.300),
        (11.930, 8.400), (11.940, 8.500), (11.950, 20.000)]
GOOD = build_block(PINS)


def pci_dir(address):
    """Windows cannot create a directory whose name contains ':' (see test_linux_poc.py)."""
    return address.replace(":", "-") if os.name == "nt" else address


GPU_ADDRESS = pci_dir("0000:01:00.0")
OTHER_GPU = pci_dir("0000:04:00.0")


class FakeBus:
    """The opener, closer and ioctl the backend is given, sharing one fake descriptor table.

    `frames` maps a dev path to what the chip behind it answers: 24 bytes, or an errno int to
    fail the I2C_SMBUS ioctl with. The ioctl side checks every request is a 24 byte block READ
    of register 0x80 at 0x2B, like the kernel would see it, and fills the buffer.
    """

    def __init__(self):
        self.frames = {}
        self.fds = {}               # fd -> dev path, for the open descriptors
        self.opened = []            # dev paths, in open order
        self.closed = []            # fds, in close order
        self.requests = []          # (dev path, request) for every ioctl
        self.reads = []             # (read_write, command, size, requested)
        self.addresses = []
        self._next_fd = 100

    def opener(self, path):
        if not os.path.exists(path):
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
        fd = self._next_fd
        self._next_fd += 1
        self.fds[fd] = path
        self.opened.append(path)
        return fd

    def closer(self, fd):
        assert fd in self.fds, f"closing fd {fd} which is not open"
        del self.fds[fd]
        self.closed.append(fd)

    def ioctl(self, fd, request, arg):
        path = self.fds[fd]
        self.requests.append((path, request))
        if request == al.I2C_SLAVE:
            assert isinstance(arg, int)
            self.addresses.append(arg)
            return 0
        if request != al.I2C_SMBUS:
            raise AssertionError(f"unexpected ioctl request {request:#06x}")
        block = arg.data.contents.block
        requested = block[0]
        self.reads.append((arg.read_write, arg.command, arg.size, requested))
        assert arg.read_write == al.I2C_SMBUS_READ, "the backend may only ever read"
        assert arg.command == 0x80
        assert arg.size == al.I2C_SMBUS_I2C_BLOCK_DATA
        assert requested == 24
        frame = self.frames.get(path, errno.EIO)
        if isinstance(frame, int):
            raise OSError(frame, os.strerror(frame))
        for offset, value in enumerate(frame[:requested]):
            block[1 + offset] = value
        block[0] = requested
        return 0


class FakeSystem:
    """A fake /sys, /dev and /proc, plus a resolver standing in for symlink resolution."""

    def __init__(self, root):
        self.pci_root = root / "sys" / "bus" / "pci" / "devices"
        self.i2c_root = root / "sys" / "bus" / "i2c" / "devices"
        self.adapter_root = root / "sys" / "class" / "i2c-adapter"     # left absent
        self.dev_root = root / "dev"
        self.proc_root = root / "proc" / "driver" / "nvidia" / "gpus"
        for path in (self.pci_root, self.i2c_root, self.dev_root, self.proc_root):
            path.mkdir(parents=True)
        self.resolved = {}
        self.bus = FakeBus()

    def add_gpu(self, address, sub_device=0x89E3, sub_vendor=0x1043, name=None):
        path = self.pci_root / address
        path.mkdir()
        (path / "vendor").write_text("0x10de\n")
        (path / "device").write_text("0x2b85\n")
        (path / "subsystem_vendor").write_text(f"0x{sub_vendor:04x}\n")
        (path / "subsystem_device").write_text(f"0x{sub_device:04x}\n")
        (path / "class").write_text("0x030000\n")
        if name:
            info = self.proc_root / address
            info.mkdir()
            (info / "information").write_text(
                f"Model: \t\t {name}\nIRQ:   \t\t 189\nBus Location: \t {address}\n"
            )

    def add_adapter(self, index, name, owner=GPU_ADDRESS, frame=None):
        entry = self.i2c_root / f"i2c-{index}"
        entry.mkdir()
        (entry / "name").write_text(name + "\n")
        (self.dev_root / f"i2c-{index}").write_text("")
        self.resolved[os.path.normpath(str(entry))] = (
            f"/sys/devices/pci0000:00/0000:00:01.0/{owner}/i2c-{index}"
        )
        if frame is not None:
            self.bus.frames[self.dev(index)] = frame

    def remove_adapter(self, index):
        entry = self.i2c_root / f"i2c-{index}"
        (entry / "name").unlink()
        entry.rmdir()
        (self.dev_root / f"i2c-{index}").unlink()
        self.resolved.pop(os.path.normpath(str(entry)), None)
        self.bus.frames.pop(self.dev(index), None)

    def bind_driver(self, index, driver):
        link = self.i2c_root / f"{index}-002b" / "driver"
        link.mkdir(parents=True)
        self.resolved[os.path.normpath(str(link))] = f"/sys/bus/i2c/drivers/{driver}"

    def resolve(self, path):
        return self.resolved.get(os.path.normpath(path), path)

    def dev(self, index):
        return os.path.join(str(self.dev_root), f"i2c-{index}")

    def backend(self, **overrides):
        kwargs = dict(
            pci_root=str(self.pci_root),
            i2c_root=str(self.i2c_root),
            adapter_root=str(self.adapter_root),
            dev_root=str(self.dev_root),
            proc_root=str(self.proc_root),
            resolve=self.resolve,
            opener=self.bus.opener,
            closer=self.bus.closer,
            ioctl=self.bus.ioctl,
            platform="linux",
            geteuid=lambda: 0,
        )
        kwargs.update(overrides)
        return al.AstralLinuxBackend(**kwargs)


@pytest.fixture()
def rig(tmp_path):
    """The rig as Phase 0 saw it on one boot: the Astral, its adapters, the chip on i2c-4."""
    system = FakeSystem(tmp_path)
    system.add_gpu(GPU_ADDRESS, name="NVIDIA GeForce RTX 5090")
    system.add_adapter(0, "SMBus PIIX4 adapter port 0 at 0b00", owner="0000:00:14.0", frame=GOOD)
    system.add_adapter(3, "NVIDIA i2c adapter 0 at 1:00.0", frame=errno.EIO)
    system.add_adapter(4, "NVIDIA i2c adapter 1 at 1:00.0", frame=GOOD)
    system.add_adapter(5, "NVIDIA i2c adapter 4 at 1:00.0", frame=errno.EIO)
    return system


@pytest.fixture()
def connected(rig):
    backend = rig.backend()
    backend.connect()
    return rig, backend


# ------------------------------------------------- read-only, structurally
def test_no_write_path_exists_in_the_backend():
    """Same rules as the PoC's ban test, applied to the module that now does the bus access."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    for banned in ("os.write", "O_WRONLY", "O_RDWR", "I2C_SMBUS_WRITE",
                   "write_byte", "write_block", "I2C_RDWR", "I2C_M_RD", "I2C_SLAVE_FORCE"):
        assert banned not in source, f"{banned} must not appear in a read-only backend"

    directions = re.findall(r"read_write\s*=\s*(\w+)", source)
    assert directions, "the transaction must state its direction explicitly"
    assert set(directions) == {"I2C_SMBUS_READ"}, directions

    # Every descriptor this module opens itself is read-only, and there is exactly one opener.
    assert re.findall(r"os\.open\([^)]*?,\s*(os\.\w+)", source) == ["os.O_RDONLY"]

    # The ioctl requests the module makes, through _ioctl or an injected stand-in for it.
    requests = re.findall(r"(?<![\w.])_?ioctl\(\s*\w+\s*,\s*(\w+)", source)
    assert sorted(requests) == ["I2C_FUNCS", "I2C_SLAVE", "I2C_SMBUS"], requests
    # And one real ioctl call site, with fcntl imported only inside it.
    assert source.count("fcntl.ioctl(") == 1
    assert not re.search(r"^(import fcntl|from fcntl)", source, re.MULTILINE)


def test_importing_the_backend_does_not_pull_in_fcntl():
    code = (
        "import sys, importlib.util;"
        "before = 'fcntl' in sys.modules;"
        "spec = importlib.util.spec_from_file_location('t', sys.argv[1]);"
        "module = importlib.util.module_from_spec(spec);"
        "spec.loader.exec_module(module);"
        "print(before, 'fcntl' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(MODULE_PATH)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    before, after = result.stdout.split()[-2:]
    assert after == before, "importing the backend must not import fcntl"


def test_the_poc_uses_the_backend_copy_of_the_read_path():
    """No second copy left behind in the PoC: the shared names are the backend's objects."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("astral_poc_linux_s3", POC_PATH)
    poc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(poc)
    for name in ("block_verdict", "find_nvidia_gpus", "find_gpu_i2c_adapters", "pick_astral",
                 "I2cSmbusIoctlData", "AdapterError", "_ioctl", "PROBE_MIN_VOLTS"):
        assert getattr(poc, name) is getattr(al, name), name


# ------------------------------------------------------------ backend shape
def test_backend_identity_matches_the_windows_direct_backend(rig):
    backend = rig.backend()
    assert backend.key == "astral"
    assert backend.tag == "direct"
    assert backend.degraded is None
    assert backend.subsystem_id is None             # nothing until connected
    backend.connect()
    assert backend.subsystem_id == ASTRAL_5090_OC
    assert backend.degraded is None


def test_connect_finds_the_chip_and_describes_it(rig):
    backend = rig.backend()
    description = backend.connect()
    assert description == (
        f"NVIDIA GeForce RTX 5090 [ROG Astral RTX 5090 OC] on {rig.dev(4)}"
    )
    assert backend.dev_path == rig.dev(4)
    # The "adapter 1" hint goes first, so the chip is found on the first probe, and the one
    # descriptor stays open for the reads that follow.
    assert rig.bus.opened == [rig.dev(4)]
    assert list(rig.bus.fds.values()) == [rig.dev(4)]
    assert rig.bus.addresses == [0x2B]
    assert backend.last_voltages == pytest.approx([v for v, _a in PINS], abs=0.0005)


def test_connect_without_the_proc_file_still_describes_the_card(tmp_path):
    system = FakeSystem(tmp_path)
    system.add_gpu(GPU_ADDRESS)                     # no /proc/driver/nvidia entry
    system.add_adapter(4, "NVIDIA i2c adapter 1 at 1:00.0", frame=GOOD)
    assert system.backend().connect() == f"NVIDIA GPU [ROG Astral RTX 5090 OC] on {system.dev(4)}"


def test_a_plausible_frame_decodes_to_labels_and_amps_in_pin_order(connected):
    rig, backend = connected
    pins = backend.read_pins()
    assert [label for label, _a, _u in pins] == [
        astral_i2c.PIN_LABEL_FMT.format(n) for n in range(1, 7)
    ]
    assert pins[0][0] == "GPU 12VHPWR Pin1 Current"
    assert all(unit == "A" for _l, _a, unit in pins)
    assert [a for _l, a, _u in pins] == pytest.approx([a for _v, a in PINS], abs=0.0005)
    assert backend.last_voltages == pytest.approx([v for v, _a in PINS], abs=0.0005)


def test_each_read_is_one_slave_bind_and_one_block_read(connected):
    rig, backend = connected
    rig.bus.requests.clear()
    rig.bus.reads.clear()
    backend.read_pins()
    assert rig.bus.requests == [(rig.dev(4), al.I2C_SLAVE), (rig.dev(4), al.I2C_SMBUS)]
    assert rig.bus.reads == [(al.I2C_SMBUS_READ, 0x80, al.I2C_SMBUS_I2C_BLOCK_DATA, 24)]


def test_a_sagging_pin_stays_visible(connected):
    """The 6.0 V floor is loose on purpose: a failing connector lowers the pin voltage."""
    rig, backend = connected
    pins = list(PINS)
    pins[3] = (7.500, 9.000)
    rig.bus.frames[rig.dev(4)] = build_block(pins)
    assert backend.read_pins()[3][1] == pytest.approx(9.0, abs=0.0005)


# ------------------------------------------------------------- error policy
@pytest.mark.parametrize("frame, reason", [
    (bytes(24), "zero"),
    (b"\xff" * 24, "0xFF"),
])
def test_an_all_zero_or_all_ff_frame_is_a_transient_error(connected, frame, reason):
    rig, backend = connected
    rig.bus.frames[rig.dev(4)] = frame
    with pytest.raises(astral_i2c.TransientReadError) as excinfo:
        backend.read_pins()
    assert reason in str(excinfo.value)


def test_a_pin_below_the_floor_is_a_transient_error(connected):
    rig, backend = connected
    pins = list(PINS)
    pins[4] = (5.000, 9.000)
    rig.bus.frames[rig.dev(4)] = build_block(pins)
    with pytest.raises(astral_i2c.TransientReadError) as excinfo:
        backend.read_pins()
    assert "pin5" in str(excinfo.value)


def test_a_failed_read_keeps_last_voltages_from_the_last_good_one(connected):
    rig, backend = connected
    rig.bus.frames[rig.dev(4)] = bytes(24)
    with pytest.raises(astral_i2c.TransientReadError):
        backend.read_pins()
    assert backend.last_voltages == pytest.approx([v for v, _a in PINS], abs=0.0005)


def test_five_bad_reads_in_a_row_mean_the_sensor_is_gone(connected):
    rig, backend = connected
    limit = astral_i2c.ASTRAL_MAX_CONSECUTIVE_FAILURES
    assert limit == 5
    rig.bus.frames[rig.dev(4)] = bytes(24)
    for _ in range(limit - 1):
        with pytest.raises(astral_i2c.TransientReadError):
            backend.read_pins()
    with pytest.raises(astral_i2c.SensorGoneError) as excinfo:
        backend.read_pins()
    assert "5 consecutive" in str(excinfo.value)


def test_a_good_read_resets_the_failure_count(connected):
    rig, backend = connected
    limit = astral_i2c.ASTRAL_MAX_CONSECUTIVE_FAILURES
    rig.bus.frames[rig.dev(4)] = bytes(24)
    for _ in range(limit - 1):
        with pytest.raises(astral_i2c.TransientReadError):
            backend.read_pins()
    rig.bus.frames[rig.dev(4)] = GOOD
    backend.read_pins()
    rig.bus.frames[rig.dev(4)] = bytes(24)
    for _ in range(limit - 1):
        with pytest.raises(astral_i2c.TransientReadError):
            backend.read_pins()
    with pytest.raises(astral_i2c.SensorGoneError):
        backend.read_pins()


def test_an_ioctl_oserror_counts_as_a_failure(connected):
    rig, backend = connected
    limit = astral_i2c.ASTRAL_MAX_CONSECUTIVE_FAILURES
    rig.bus.frames[rig.dev(4)] = errno.EIO
    for _ in range(limit - 1):
        with pytest.raises(astral_i2c.TransientReadError) as excinfo:
            backend.read_pins()
        assert not isinstance(excinfo.value, OSError)
    with pytest.raises(astral_i2c.SensorGoneError):
        backend.read_pins()


def test_a_short_block_counts_as_a_failure(connected):
    rig, backend = connected

    def short_reply(fd, request, arg):
        rig.bus.ioctl(fd, request, arg)
        if request == al.I2C_SMBUS:
            arg.data.contents.block[0] = 8

    backend._ioctl = short_reply
    with pytest.raises(astral_i2c.TransientReadError) as excinfo:
        backend.read_pins()
    assert "8 bytes" in str(excinfo.value)


def test_a_vanished_dev_node_is_gone_at_once(connected):
    rig, backend = connected
    rig.bus.frames[rig.dev(4)] = errno.ENODEV
    os.remove(rig.dev(4))
    with pytest.raises(astral_i2c.SensorGoneError) as excinfo:
        backend.read_pins()
    assert "gone away" in str(excinfo.value)


def test_read_before_connect_or_after_close_is_sensor_gone(rig):
    backend = rig.backend()
    with pytest.raises(astral_i2c.SensorGoneError):
        backend.read_pins()
    backend.connect()
    backend.close()
    with pytest.raises(astral_i2c.SensorGoneError):
        backend.read_pins()


# ------------------------------------------------------- discovery on connect
def test_connect_rediscovers_a_renumbered_adapter(rig):
    """Phase 0: the chip was on i2c-1 one boot and i2c-4 the next. Nothing may be cached."""
    backend = rig.backend()
    assert backend.connect().endswith(f"on {rig.dev(4)}")
    backend.close()

    # Next boot: the GPU's adapters come up under new numbers, and i2c-4 is now someone else's.
    for index in (3, 4, 5):
        rig.remove_adapter(index)
    rig.add_adapter(1, "NVIDIA i2c adapter 1 at 1:00.0", frame=GOOD)
    rig.add_adapter(2, "NVIDIA i2c adapter 0 at 1:00.0", frame=errno.EIO)
    rig.add_adapter(4, "SMBus I801 adapter at efa0", owner="0000:00:1f.4", frame=GOOD)

    assert backend.connect().endswith(f"on {rig.dev(1)}")
    assert backend.dev_path == rig.dev(1)
    assert rig.bus.opened[-1] == rig.dev(1)
    assert backend.read_pins()[0][0] == "GPU 12VHPWR Pin1 Current"


def test_connect_probes_past_adapters_that_do_not_answer(tmp_path):
    system = FakeSystem(tmp_path)
    system.add_gpu(GPU_ADDRESS)
    # No port hint for the chip's adapter this time, so the dead ones are tried first.
    system.add_adapter(2, "NVIDIA i2c adapter 0 at 1:00.0", frame=errno.EIO)
    system.add_adapter(3, "NVIDIA i2c adapter 2 at 1:00.0", frame=bytes(24))
    system.add_adapter(6, "NVIDIA i2c adapter 3 at 1:00.0", frame=GOOD)
    backend = system.backend()
    assert backend.connect().endswith(f"on {system.dev(6)}")
    assert system.bus.opened == [system.dev(2), system.dev(3), system.dev(6)]
    # The two that failed were closed again; only the chip's descriptor is open.
    assert list(system.bus.fds.values()) == [system.dev(6)]


def test_no_adapter_answering_fails_with_the_reasons_and_leaves_nothing_open(tmp_path):
    system = FakeSystem(tmp_path)
    system.add_gpu(GPU_ADDRESS)
    system.add_adapter(2, "NVIDIA i2c adapter 0 at 1:00.0", frame=errno.EIO)
    system.add_adapter(4, "NVIDIA i2c adapter 1 at 1:00.0", frame=bytes(24))
    backend = system.backend()
    with pytest.raises(RuntimeError) as excinfo:
        backend.connect()
    message = str(excinfo.value)
    assert "no adapter" in message and "plausible" in message
    assert system.dev(2) in message and system.dev(4) in message
    assert "all zero" in message
    assert system.bus.fds == {}
    with pytest.raises(astral_i2c.SensorGoneError):
        backend.read_pins()


def test_no_adapter_tied_to_the_card_is_a_clear_error(tmp_path):
    system = FakeSystem(tmp_path)
    system.add_gpu(GPU_ADDRESS)
    system.add_adapter(0, "SMBus PIIX4 adapter port 0 at 0b00", owner="0000:00:14.0", frame=GOOD)
    with pytest.raises(RuntimeError) as excinfo:
        system.backend().connect()
    assert "no i2c-dev adapter" in str(excinfo.value)
    assert system.bus.opened == []


def test_a_bound_kernel_driver_makes_connect_refuse_and_names_it(rig):
    rig.bind_driver(4, "astral-hwmon")
    backend = rig.backend()
    with pytest.raises(RuntimeError) as excinfo:
        backend.connect()
    message = str(excinfo.value)
    assert "astral-hwmon" in message
    assert "4-002b" in message
    assert "Unload" in message or "unload" in message
    # The guard steps aside before opening anything: not one descriptor, not one ioctl.
    assert rig.bus.opened == []
    assert rig.bus.requests == []
    assert backend.subsystem_id is None


def test_bound_driver_reads_the_driver_link(rig):
    assert al.bound_driver(4, str(rig.i2c_root), rig.resolve) is None
    rig.bind_driver(4, "astral-hwmon")
    assert al.bound_driver(4, str(rig.i2c_root), rig.resolve) == "astral-hwmon"
    assert al.bound_driver(3, str(rig.i2c_root), rig.resolve) is None


def test_no_supported_card_is_a_clear_error(tmp_path):
    system = FakeSystem(tmp_path)
    system.add_gpu(OTHER_GPU, sub_vendor=0x1462, sub_device=0x5110)
    system.add_adapter(9, "NVIDIA i2c adapter 1 at 4:00.0", owner=OTHER_GPU, frame=GOOD)
    with pytest.raises(RuntimeError) as excinfo:
        system.backend().connect()
    message = str(excinfo.value)
    assert "no supported ASUS ROG Astral GPU found" in message
    assert "1462:5110" in message
    assert system.bus.opened == []


def test_no_nvidia_card_at_all_is_a_clear_error(tmp_path):
    system = FakeSystem(tmp_path)
    system.add_adapter(0, "SMBus PIIX4 adapter port 0 at 0b00", owner="0000:00:14.0")
    with pytest.raises(RuntimeError) as excinfo:
        system.backend().connect()
    assert "no supported ASUS ROG Astral GPU found" in str(excinfo.value)
    assert "no NVIDIA display adapter" in str(excinfo.value)


# --------------------------------------------------------------- environment
def test_not_linux_is_a_clear_error(rig):
    with pytest.raises(RuntimeError) as excinfo:
        rig.backend(platform="win32").connect()
    assert "needs Linux" in str(excinfo.value)
    assert "win32" in str(excinfo.value)
    assert rig.bus.opened == []


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="checks the real platform default")
def test_the_default_platform_check_refuses_off_linux(rig):
    with pytest.raises(RuntimeError) as excinfo:
        rig.backend(platform=None).connect()
    assert "needs Linux" in str(excinfo.value)


def test_not_root_is_a_clear_error(rig):
    with pytest.raises(RuntimeError) as excinfo:
        rig.backend(geteuid=lambda: 1000).connect()
    assert "root required" in str(excinfo.value)
    assert rig.bus.opened == []


def test_no_i2c_dev_nodes_is_a_clear_error(tmp_path):
    system = FakeSystem(tmp_path)
    system.add_gpu(GPU_ADDRESS)
    with pytest.raises(RuntimeError) as excinfo:
        system.backend().connect()
    assert "i2c-dev" in str(excinfo.value)
    assert "modprobe" in str(excinfo.value)


# --------------------------------------------------------------------- close
def test_close_closes_the_descriptor_and_is_idempotent(connected):
    rig, backend = connected
    (fd,) = rig.bus.fds
    backend.close()
    assert rig.bus.closed == [fd]
    assert rig.bus.fds == {}
    assert backend.subsystem_id is None
    backend.close()                                 # a second close is a no-op
    backend.close()
    assert rig.bus.closed == [fd]


def test_close_before_connect_is_a_no_op(rig):
    backend = rig.backend()
    backend.close()
    assert rig.bus.closed == []


def test_reconnect_closes_the_previous_descriptor(connected):
    rig, backend = connected
    (first,) = rig.bus.fds
    backend.connect()
    assert first in rig.bus.closed
    assert len(rig.bus.fds) == 1 and first not in rig.bus.fds


def test_the_default_opener_is_read_only(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(al.os, "open", lambda path, flags: seen.append((path, flags)) or 3)
    assert al._open_read_only("/dev/i2c-4") == 3
    assert seen == [("/dev/i2c-4", os.O_RDONLY)]

"""Unit tests for tools/nvml_probe_linux.py and tools/nvml_limit_test_linux.py.

Run anywhere, including Windows: neither tool loads libnvidia-ml at import, and every test
hands in a fake NVML. Self-contained like test_linux_poc.py (no conftest dependency).
"""

import ctypes
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
for p in (str(REPO_ROOT), str(TOOLS)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


probe = _load("nvml_probe_linux")
limit = _load("nvml_limit_test_linux")

ASTRAL_5090_OC = 0x89E31043
BUS = "00000000:01:00.0"


class FakeNvml:
    """Method-level stand-in for LimitNvml. Records every call in order."""

    def __init__(self, *, subsystem=ASTRAL_5090_OC, limit_mw=600000, constraints=(400000, 600000),
                 floor=180, usage_mw=45000, refuse_clocks=(), refuse_power=False,
                 sticky_power=False, refuse_reset=False):
        self.calls = []
        self.subsystem = subsystem
        self.limit_mw = limit_mw
        self.constraints = constraints
        self.floor = floor
        self.usage_mw = usage_mw
        self.refuse_clocks = set(refuse_clocks)
        self.refuse_power = refuse_power
        self.sticky_power = sticky_power   # the driver accepts restores but never applies them
        self.refuse_reset = refuse_reset
        self.locked = None
        self.initialized = False

    # lifecycle
    def init(self):
        self.initialized = True

    def shutdown(self):
        self.initialized = False

    # getters
    def device_count(self):
        return 1

    def handle(self, index):
        return f"h{index}"

    def pci_info(self, handle):
        return self.subsystem, BUS

    def power_limit_mw(self, handle):
        return self.limit_mw

    def power_limit_constraints_mw(self, handle):
        return self.constraints

    def min_supported_graphics_clock_mhz(self, handle):
        return self.floor

    def power_usage_mw(self, handle):
        return self.usage_mw

    def graphics_clock_mhz(self, handle):
        return self.locked[1] if self.locked else 412

    # setters
    def set_locked_clocks(self, handle, lo, hi):
        self.calls.append(("lock", lo, hi))
        if lo in self.refuse_clocks:
            raise probe.NvmlError("nvmlDeviceSetGpuLockedClocks", 2, "invalid argument")
        self.locked = (lo, hi)

    def reset_locked_clocks(self, handle):
        self.calls.append(("reset",))
        if self.refuse_reset:
            raise probe.NvmlError("nvmlDeviceResetGpuLockedClocks", 4, "no permission")
        self.locked = None

    def set_power_limit_mw(self, handle, mw):
        self.calls.append(("power", mw))
        if self.refuse_power:
            raise probe.NvmlError("nvmlDeviceSetPowerManagementLimit", 3, "not supported")
        if self.sticky_power and mw != self.constraints[0]:
            return
        self.limit_mw = mw


def run(nvml, tmp_path, **kw):
    out = []
    code = limit.run_limit_test(nvml, "h0", BUS, hold_seconds=kw.pop("hold", 3),
                                marker_path=str(tmp_path / "marker.json"),
                                say=out.append, sleep=lambda s: None, **kw)
    return code, "\n".join(out)


# ------------------------------------------------------------ read-only probe
def test_probe_binds_no_setter_reset_or_clear():
    src = (TOOLS / "nvml_probe_linux.py").read_text(encoding="utf-8")
    assert not re.search(r"nvml\w*(Set|Reset|Clear)\w*", src)


def test_probe_import_does_not_load_the_library():
    nvml = probe.ReadOnlyNvml()
    assert nvml._lib is None and nvml.library_name is None


def test_limit_tool_adds_only_the_three_mitigation_setters():
    src = (TOOLS / "nvml_limit_test_linux.py").read_text(encoding="utf-8")
    names = set(re.findall(r"nvml\w*(?:Set|Reset|Clear)\w*", src))
    assert names == {"nvmlDeviceSetPowerManagementLimit", "nvmlDeviceSetGpuLockedClocks",
                     "nvmlDeviceResetGpuLockedClocks"}


def test_neither_tool_touches_i2c():
    for name in ("nvml_probe_linux.py", "nvml_limit_test_linux.py"):
        src = (TOOLS / name).read_text(encoding="utf-8")
        assert "ioctl" not in src and "fcntl" not in src and "os.open" not in src


class _FakeFn:
    """A ctypes-like function: writes its result through the byref() it is handed."""

    def __init__(self, impl):
        self.impl = impl

    def __call__(self, *args):
        return self.impl(*args)


def test_binding_reads_values_through_byref():
    def set_uint(value):
        def impl(*args):
            args[-1]._obj.value = value
            return 0
        return impl

    class Lib:
        nvmlInit_v2 = _FakeFn(lambda: 0)
        nvmlShutdown = _FakeFn(lambda: 0)
        nvmlDeviceGetCount_v2 = _FakeFn(set_uint(1))
        nvmlDeviceGetHandleByIndex_v2 = _FakeFn(lambda i, h: 0)
        nvmlDeviceGetPciInfo_v3 = _FakeFn(lambda h, p: 0)
        nvmlDeviceGetName = _FakeFn(lambda h, b, n: 0)
        nvmlDeviceGetPowerManagementLimit = _FakeFn(set_uint(600000))
        nvmlDeviceGetPowerManagementLimitConstraints = _FakeFn(lambda h, lo, hi: 0)
        nvmlDeviceGetClockInfo = _FakeFn(set_uint(412))

    nvml = probe.ReadOnlyNvml(lib=Lib())
    nvml.init()
    assert nvml.device_count() == 1
    assert nvml.power_limit_mw("h") == 600000
    assert nvml.graphics_clock_mhz("h") == 412
    # Optional getters this fake does not export surface as a clean NvmlError.
    with pytest.raises(probe.NvmlError):
        nvml.power_usage_mw("h")


def test_a_failing_call_raises_with_the_status():
    class Lib:
        nvmlInit_v2 = _FakeFn(lambda: 9)
        nvmlShutdown = _FakeFn(lambda: 0)
        nvmlDeviceGetCount_v2 = nvmlDeviceGetHandleByIndex_v2 = nvmlDeviceGetPciInfo_v3 = \
            nvmlDeviceGetName = nvmlDeviceGetPowerManagementLimit = \
            nvmlDeviceGetPowerManagementLimitConstraints = nvmlDeviceGetClockInfo = \
            _FakeFn(lambda *a: 0)

    with pytest.raises(probe.NvmlError) as info:
        probe.ReadOnlyNvml(lib=Lib()).init()
    assert info.value.code == 9


def test_probe_report_recognises_the_astral_and_shows_the_range():
    dev = probe.collect_device(FakeProbeNvml(), 0)
    text = "\n".join(probe.render_device(dev))
    assert "ROG Astral RTX 5090 OC" in text
    assert "1043:89E3" in text
    assert "400 W to 600 W" in text
    assert "180 MHz (enumerated" in text


def test_probe_report_explains_the_ladder_when_clocks_are_not_enumerated():
    nv = FakeProbeNvml()
    nv.floor = None
    text = "\n".join(probe.render_device(probe.collect_device(nv, 0)))
    assert "500 / 700 / 1000 MHz" in text


def test_probe_report_shows_a_failing_getter_instead_of_hiding_it():
    nv = FakeProbeNvml()

    def boom(handle):
        raise probe.NvmlError("nvmlDeviceGetPowerUsage", 3, "not supported")
    nv.power_usage_mw = boom
    text = "\n".join(probe.render_device(probe.collect_device(nv, 0)))
    assert "power draw now      n/a (nvmlDeviceGetPowerUsage failed with NVML status 3" in text


class FakeProbeNvml(FakeNvml):
    def name(self, handle):
        return "NVIDIA GeForce RTX 5090"

    def default_power_limit_mw(self, handle):
        return 600000

    def enforced_power_limit_mw(self, handle):
        return 600000

    def memory_clock_mhz(self, handle):
        return 405

    def max_graphics_clock_mhz(self, handle):
        return 3090

    def temperature_c(self, handle):
        return 38

    def persistence_mode(self, handle):
        return False


def test_i2c_dev_autoload_is_found_in_modules_load_d(tmp_path):
    (tmp_path / "etc" / "modules-load.d").mkdir(parents=True)
    (tmp_path / "etc" / "modules-load.d" / "i2c.conf").write_text("# comment\ni2c-dev\n")
    status = probe.i2c_dev_status(str(tmp_path / "sys"), str(tmp_path / "dev"), str(tmp_path / "etc"))
    assert status["loaded"] is False
    assert status["autoload_files"] == [str(tmp_path / "etc" / "modules-load.d" / "i2c.conf")]


def test_running_gpu_tools_are_named(tmp_path):
    for pid, comm in (("101", "gwe"), ("102", "bash"), ("103", "openrgb")):
        (tmp_path / pid).mkdir()
        (tmp_path / pid / "comm").write_text(comm + "\n")
    found = probe.running_gpu_tools(str(tmp_path))
    assert len(found) == 2 and any("GreenWithEnvy" in f for f in found)


def test_gpu_uv_config_is_parsed(tmp_path):
    conf = tmp_path / "gpu-uv.conf"
    assert probe.gpu_uv_clock_cap(str(conf)) == (False, None)
    conf.write_text("[undervolt]\nmax_clock_mhz = 2895\noffset_mhz = 263\n[power]\npower_limit_w = 480\n")
    assert probe.gpu_uv_clock_cap(str(conf)) == (True, 2895)
    conf.write_text("[undervolt]\noffset_mhz = 263\n")
    assert probe.gpu_uv_clock_cap(str(conf)) == (True, None)


# ------------------------------------------------------------- limit test
def test_happy_path_engages_like_the_guard_and_restores_verified(tmp_path):
    nv = FakeNvml()
    code, out = run(nv, tmp_path)
    assert code == 0, out
    assert nv.calls == [("lock", 180, 180), ("power", 400000), ("power", 600000), ("reset",)]
    assert nv.limit_mw == 600000 and nv.locked is None
    assert not (tmp_path / "marker.json").exists()
    assert "clock held at the lock 3/3" in out
    assert "sudo nvidia-smi -i 00000000:01:00.0 -pl 600" in out


def test_refused_floor_walks_up_the_ladder(tmp_path):
    nv = FakeNvml(refuse_clocks={180})
    code, out = run(nv, tmp_path)
    assert code == 0, out
    assert ("lock", 500, 500) in nv.calls
    assert "refused" in out and "applied at 500 MHz" in out


def test_nothing_applied_returns_five_and_leaves_no_marker(tmp_path):
    nv = FakeNvml(refuse_clocks={180, 500, 700, 1000}, refuse_power=True)
    code, out = run(nv, tmp_path)
    assert code == 5
    assert ("reset",) not in nv.calls
    assert not (tmp_path / "marker.json").exists()


def test_unverified_restore_keeps_the_marker_and_prints_recovery(tmp_path):
    nv = FakeNvml(sticky_power=True)
    code, out = run(nv, tmp_path)
    assert code == 4
    assert [c for c in nv.calls if c[0] == "power"] == [("power", 400000), ("power", 600000), ("power", 600000)]
    assert (tmp_path / "marker.json").exists()
    assert "NOT VERIFIED" in out and "--recover" in out


def test_interrupt_during_hold_still_releases(tmp_path):
    nv = FakeNvml()
    ticks = []

    def sleep(s):
        ticks.append(s)
        if len(ticks) == 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        limit.run_limit_test(nv, "h0", BUS, hold_seconds=5, marker_path=str(tmp_path / "m"),
                             say=lambda s: None, sleep=sleep)
    assert nv.limit_mw == 600000 and nv.locked is None
    assert not (tmp_path / "m").exists()


def test_failed_clock_reset_returns_four(tmp_path):
    code, out = run(FakeNvml(refuse_reset=True), tmp_path)
    assert code == 4 and (tmp_path / "marker.json").exists()


# ----------------------------------------------- gpu-uv: restore, never reset
def test_with_gpu_uv_release_restores_its_lock_and_never_resets(tmp_path):
    nv = FakeNvml()
    code, out = run(nv, tmp_path, gpu_uv=(True, 2895))
    assert code == 0, out
    assert ("reset",) not in nv.calls
    assert nv.calls[-1] == ("lock", 180, 2895)
    assert nv.locked == (180, 2895)
    assert "sudo gpu-uv apply" in out and "-rgc" not in out


def test_with_unreadable_gpu_uv_config_the_floor_lock_stays(tmp_path):
    nv = FakeNvml()
    code, out = run(nv, tmp_path, gpu_uv=(True, None))
    assert code == 4
    assert ("reset",) not in nv.calls
    assert nv.locked == (180, 180)
    assert "sudo gpu-uv apply" in out


def test_with_gpu_uv_and_no_enumerated_floor_the_floor_lock_stays(tmp_path):
    nv = FakeNvml(floor=None)
    code, out = run(nv, tmp_path, gpu_uv=(True, 2895))
    assert code == 4
    assert ("reset",) not in nv.calls
    assert nv.locked == (500, 500)


def test_recover_with_gpu_uv_restores_its_lock(tmp_path):
    marker = tmp_path / "m.json"
    limit.write_marker(str(marker), BUS, 600000)
    nv = FakeNvml(limit_mw=400000)
    nv.locked = (180, 180)
    code = limit.recover(nv, "h0", BUS, limit.read_marker(str(marker)), str(marker),
                         gpu_uv=(True, 2895), say=lambda s: None, sleep=lambda s: None)
    assert code == 0
    assert nv.limit_mw == 600000 and nv.locked == (180, 2895)
    assert ("reset",) not in nv.calls and not marker.exists()


# ------------------------------------------------------------------ main()
def main_args(tmp_path, nv, argv=("--yes",), root=True, answer="y", gpu_uv=(False, None)):
    return limit.main(list(argv), is_root=lambda: root, nvml_factory=lambda: nv,
                      ask=lambda prompt: answer, marker_path=str(tmp_path / "marker.json"),
                      sleep=lambda s: None, gpu_uv_check=lambda: gpu_uv)


def test_main_refuses_without_root(tmp_path):
    nv = FakeNvml()
    assert main_args(tmp_path, nv, root=False) == 1
    assert nv.calls == []


def test_main_refuses_a_busy_gpu(tmp_path):
    nv = FakeNvml(usage_mw=320000)
    assert main_args(tmp_path, nv) == 1
    assert nv.calls == []


def test_main_refuses_when_an_earlier_marker_exists(tmp_path):
    limit.write_marker(str(tmp_path / "marker.json"), BUS, 600000)
    nv = FakeNvml()
    assert main_args(tmp_path, nv) == 1
    assert nv.calls == []


def test_main_declined_prompt_changes_nothing(tmp_path):
    nv = FakeNvml()
    assert main_args(tmp_path, nv, argv=(), answer="n") == 1
    assert nv.calls == []


def test_main_refuses_an_unknown_card_without_the_flag(tmp_path):
    nv = FakeNvml(subsystem=0x12345678)
    assert main_args(tmp_path, nv) == 3
    assert main_args(tmp_path, FakeNvml(subsystem=0x12345678), argv=("--yes", "--allow-unknown")) == 0


def test_main_full_run_passes_and_shuts_nvml_down(tmp_path):
    nv = FakeNvml()
    assert main_args(tmp_path, nv) == 0
    assert nv.initialized is False


def test_main_recover_uses_the_gpu_uv_lock(tmp_path):
    limit.write_marker(str(tmp_path / "marker.json"), BUS, 600000)
    nv = FakeNvml(limit_mw=400000)
    assert main_args(tmp_path, nv, argv=("--recover",), gpu_uv=(True, 2895)) == 0
    assert nv.locked == (180, 2895) and ("reset",) not in nv.calls


@pytest.mark.parametrize("tool", ["nvml_probe_linux.py", "nvml_limit_test_linux.py"])
def test_help_runs_on_any_platform(tool):
    proc = subprocess.run([sys.executable, str(TOOLS / tool), "--help"],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0 and "usage" in proc.stdout.lower()


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="checks the off-Linux refusal")
@pytest.mark.parametrize("tool", ["nvml_probe_linux.py", "nvml_limit_test_linux.py"])
def test_off_linux_exits_one(tool):
    proc = subprocess.run([sys.executable, str(TOOLS / tool)], capture_output=True, text=True,
                          timeout=30)
    assert proc.returncode == 1

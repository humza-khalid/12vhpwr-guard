"""S2 (B-012): gpu_mitigation.py on Linux, and the gpu-uv rule.

Both platforms are exercised on this one machine by flipping gpu_mitigation._IS_WINDOWS
and injecting the library loaders, so nothing here loads a real NVML or touches the card.
The fake card is the one the F5 suite uses.
"""

import ctypes
import os
import subprocess
import sys
import textwrap

import pytest

import gpu_mitigation as gm
from gpu_mitigation import MARKER_NAME, GpuMitigator
from helpers import CountingLogger
from test_f5_release import FLOOR_CLOCK_MHZ, MIN_MW, SUB_ID, USER_LIMIT_MW, FakeNvml

GPU_UV_CAP = 2895


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(gm, "_IS_WINDOWS", False)


@pytest.fixture
def gpu_uv_conf(tmp_path):
    path = tmp_path / "gpu-uv.conf"
    path.write_text(
        "# max_clock_mhz is a 12vhpwr_guard B-012 contract\n"
        f"[undervolt]\nmax_clock_mhz = {GPU_UV_CAP}\noffset_mhz = 263\npstates = 0,2\n"
        "[power]\npower_limit_w = 480\n", encoding="utf-8")
    return str(path)


def mitigator(tmp_path, fake, logger=None, **kw):
    return GpuMitigator(marker_path=str(tmp_path / MARKER_NAME), nvml_factory=lambda: fake,
                        elevation_check=lambda: True, logger=logger or CountingLogger(), **kw)


def engaged(tmp_path, fake, logger=None, **kw):
    m = mitigator(tmp_path, fake, logger, **kw)
    assert m.connect(SUB_ID) is True, m.unavailable_reason
    assert m.engage("test") is True
    assert fake.locked_clocks == (FLOOR_CLOCK_MHZ, FLOOR_CLOCK_MHZ)
    return m


# ------------------------------------------------------------ importability
def test_gpu_mitigation_imports_with_every_windows_module_blocked():
    """The Linux daemon imports this file; nothing Windows-only may load at import."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = textwrap.dedent(f"""
        import sys
        for name in ("ctypes.wintypes", "win32api", "win32con", "win32evtlogutil",
                     "winotify", "pystray", "tkinter"):
            sys.modules[name] = None
        sys.path.insert(0, {repo!r})
        import gpu_mitigation
        print("ok")
    """)
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


# ---------------------------------------------------------------- elevation
def test_linux_elevation_is_root(linux, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    assert gm.is_process_elevated() is True
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    assert gm.is_process_elevated() is False


def test_linux_not_root_says_so_in_linux_terms(linux, tmp_path):
    log = CountingLogger()
    m = GpuMitigator(marker_path=str(tmp_path / MARKER_NAME), nvml_factory=FakeNvml,
                     elevation_check=lambda: False, logger=log)
    assert m.connect(SUB_ID) is False
    assert m.unavailable_reason == "not root"
    assert "not running as root" in log.all_text() and "sudo" in log.all_text()


def test_windows_not_elevated_text_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(gm, "_IS_WINDOWS", True)
    log = CountingLogger()
    m = GpuMitigator(marker_path=str(tmp_path / MARKER_NAME), nvml_factory=FakeNvml,
                     elevation_check=lambda: False, logger=log)
    assert m.connect(SUB_ID) is False
    assert m.unavailable_reason == "not elevated"
    assert ("Mitigation: unavailable (guard is not running elevated). Protection falls back "
            "to shutdown-only. Install as a scheduled task, or run as admin.") in log.records["warning"]


# ------------------------------------------------------------- library load
def test_linux_loads_libnvidia_ml_so_1_first(linux, monkeypatch):
    tried = []

    def fake_cdll(name):
        tried.append(name)
        return object()

    monkeypatch.setattr(ctypes, "CDLL", fake_cdll)
    nvml = gm._Nvml()
    nvml._load_library()
    assert tried == ["libnvidia-ml.so.1"] and nvml.library_name == "libnvidia-ml.so.1"


def test_linux_falls_back_to_the_unversioned_name(linux, monkeypatch):
    tried = []

    def fake_cdll(name):
        tried.append(name)
        if name.endswith(".so.1"):
            raise OSError("not found")
        return object()

    monkeypatch.setattr(ctypes, "CDLL", fake_cdll)
    nvml = gm._Nvml()
    nvml._load_library()
    assert tried == ["libnvidia-ml.so.1", "libnvidia-ml.so"]
    assert nvml.library_name == "libnvidia-ml.so"


def test_linux_without_any_nvml_library_raises_a_clear_error(linux, monkeypatch):
    def fake_cdll(name):
        raise OSError("not found")
    monkeypatch.setattr(ctypes, "CDLL", fake_cdll)
    with pytest.raises(OSError, match="libnvidia-ml.so.1 or libnvidia-ml.so"):
        gm._Nvml()._load_library()


def test_windows_still_loads_nvml_dll(monkeypatch):
    monkeypatch.setattr(gm, "_IS_WINDOWS", True)
    loaded = []
    monkeypatch.setattr(ctypes, "WinDLL", lambda name: loaded.append(name) or object(),
                        raising=False)
    nvml = gm._Nvml()
    nvml._load_library()
    assert loaded == ["nvml.dll"] and nvml.library_name == "nvml.dll"


def test_missing_symbol_names_the_library_that_was_loaded(linux):
    nvml = gm._Nvml()
    nvml._dll = object()
    nvml.library_name = "libnvidia-ml.so.1"
    with pytest.raises(RuntimeError, match="missing from libnvidia-ml.so.1"):
        nvml._bind("nvmlInit_v2", ctypes.c_int, [])


# ------------------------------------------------------------- gpu-uv rule
def test_gpu_uv_config_parsing(tmp_path, gpu_uv_conf):
    assert gm.gpu_uv_clock_cap(gpu_uv_conf) == (True, GPU_UV_CAP)
    assert gm.gpu_uv_clock_cap(str(tmp_path / "absent.conf")) == (False, None)
    broken = tmp_path / "broken.conf"
    broken.write_text("[undervolt]\noffset_mhz = 263\n", encoding="utf-8")
    assert gm.gpu_uv_clock_cap(str(broken)) == (True, None)


def test_gpu_uv_contract_names_match_the_gpu_uv_side():
    assert gm.GPU_UV_CONFIG == "/etc/gpu-uv/gpu-uv.conf"
    assert (gm.GPU_UV_SECTION, gm.GPU_UV_KEY) == ("undervolt", "max_clock_mhz")


def test_platform_default_is_on_for_linux_and_off_for_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(gm, "_IS_WINDOWS", False)
    assert mitigator(tmp_path, FakeNvml())._gpu_uv_config == gm.GPU_UV_CONFIG
    monkeypatch.setattr(gm, "_IS_WINDOWS", True)
    assert mitigator(tmp_path, FakeNvml())._gpu_uv_config is None


def test_with_gpu_uv_release_relocks_to_its_cap_and_never_resets(linux, tmp_path, gpu_uv_conf):
    fake = FakeNvml()
    log = CountingLogger()
    m = engaged(tmp_path, fake, log, gpu_uv_config=gpu_uv_conf)
    assert m.release() is True
    assert fake.reset_clock_calls == 0, "a reset would uncap gpu-uv's offset"
    assert fake.locked_clocks == (FLOOR_CLOCK_MHZ, GPU_UV_CAP)
    assert fake.limit_mw == USER_LIMIT_MW, "the power limit still goes back to the saved value"
    assert not (tmp_path / MARKER_NAME).exists()
    assert f"clock lock handed back to gpu-uv ({FLOOR_CLOCK_MHZ}-{GPU_UV_CAP} MHz)" in log.all_text()


def test_without_gpu_uv_config_release_resets_as_before(linux, tmp_path):
    fake = FakeNvml()
    m = engaged(tmp_path, fake, gpu_uv_config=str(tmp_path / "absent.conf"))
    assert m.release() is True
    assert fake.reset_clock_calls == 1 and fake.locked_clocks is None


def test_unreadable_gpu_uv_config_keeps_the_floor_lock(linux, tmp_path):
    broken = tmp_path / "gpu-uv.conf"
    broken.write_text("[undervolt]\nmax_clock_mhz = lots\n", encoding="utf-8")
    fake = FakeNvml()
    log = CountingLogger()
    m = engaged(tmp_path, fake, log, gpu_uv_config=str(broken))
    assert m.release() is False, "not fully restored, so the guard warns"
    assert fake.reset_clock_calls == 0
    assert fake.locked_clocks == (FLOOR_CLOCK_MHZ, FLOOR_CLOCK_MHZ), "slow and safe, never uncapped"
    assert fake.limit_mw == USER_LIMIT_MW
    assert "sudo gpu-uv apply" in log.all_text()


def test_no_enumerated_floor_keeps_the_floor_lock(linux, tmp_path, gpu_uv_conf):
    class NoFloor(FakeNvml):
        def min_supported_graphics_clock_mhz(self, handle):
            return None

    fake = NoFloor()
    log = CountingLogger()
    m = mitigator(tmp_path, fake, log, gpu_uv_config=gpu_uv_conf)
    assert m.connect(SUB_ID) and m.engage("test")
    locked = fake.locked_clocks
    assert m.release() is False
    assert fake.reset_clock_calls == 0 and fake.locked_clocks == locked
    assert "sudo gpu-uv apply" in log.all_text()


def test_windows_ignores_a_gpu_uv_config_even_if_one_existed(monkeypatch, tmp_path, gpu_uv_conf):
    monkeypatch.setattr(gm, "_IS_WINDOWS", True)
    monkeypatch.setattr(gm, "GPU_UV_CONFIG", gpu_uv_conf)
    fake = FakeNvml()
    m = engaged(tmp_path, fake)
    assert m.release() is True
    assert fake.reset_clock_calls == 1 and fake.locked_clocks is None


def test_startup_recovery_with_gpu_uv_relocks_instead_of_resetting(linux, tmp_path, gpu_uv_conf):
    fake = FakeNvml()
    m = engaged(tmp_path, fake, gpu_uv_config=gpu_uv_conf)
    # The guard dies while engaged: the marker stays, the card stays limited.
    assert (tmp_path / MARKER_NAME).exists()
    fresh = FakeNvml(limit_mw=MIN_MW)
    fresh.locked_clocks = (FLOOR_CLOCK_MHZ, FLOOR_CLOCK_MHZ)
    r = mitigator(tmp_path, fresh, gpu_uv_config=gpu_uv_conf)
    msg = r.startup_recovery()
    assert msg is not None and "restored" in msg
    assert fresh.reset_clock_calls == 0
    assert fresh.locked_clocks == (FLOOR_CLOCK_MHZ, GPU_UV_CAP)
    assert fresh.limit_mw == USER_LIMIT_MW
    assert not (tmp_path / MARKER_NAME).exists()


def test_unreachable_gpu_with_gpu_uv_points_at_gpu_uv_apply(linux, tmp_path, gpu_uv_conf):
    fake = FakeNvml()
    engaged(tmp_path, fake, gpu_uv_config=gpu_uv_conf)
    r = GpuMitigator(marker_path=str(tmp_path / MARKER_NAME), nvml_factory=FakeNvml,
                     elevation_check=lambda: False, gpu_uv_config=gpu_uv_conf)
    msg = r.startup_recovery()
    assert "sudo gpu-uv apply" in msg and "-rgc" not in msg

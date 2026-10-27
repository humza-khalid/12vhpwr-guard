"""Static checks on the Linux packaging: the systemd unit, install.sh, uninstall.sh, the example
config, and the Linux test collection split (v1.5, stage S4b).

Nothing here installs anything or needs root. The unit and the scripts are read as text and
checked against the contract the daemon is written to (paths, directives, the never-delete
rules). bash -n runs when bash is on PATH (Git Bash on Windows, any Linux) and is skipped
otherwise. Self-contained (no conftest fixture, no Windows guard), so it also runs in Linux CI.
"""

import configparser
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
LINUX_DIR = REPO_ROOT / "linux"
UNIT_PATH = LINUX_DIR / "12vhpwr-guard.service"
INSTALL = LINUX_DIR / "install.sh"
UNINSTALL = LINUX_DIR / "uninstall.sh"
CONFIG_EXAMPLE = LINUX_DIR / "config.ini.example"
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import guard_core  # noqa: E402
import platform_split  # noqa: E402

# The installed layout, relative to /opt/12vhpwr-guard. Shared contract with guardd.py.
CONTRACT_FILES = (
    "guard_core.py", "gpu_mitigation.py", "astral_i2c.py",
    "linux/guardd.py", "linux/astral_linux.py", "linux/notify.py", "linux/guard_cli.py",
)


def read(path):
    return path.read_text(encoding="utf-8")


def parse_unit(text):
    """{section: [(key, value), ...]} keeping repeats and order; comments and blanks dropped."""
    sections = {}
    current = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
            continue
        key, _, value = line.partition("=")
        sections[current].append((key.strip(), value.strip()))
    return sections


@pytest.fixture(scope="module")
def unit():
    return parse_unit(read(UNIT_PATH))


def values(unit, section, key):
    return [v for k, v in unit.get(section, []) if k == key]


# ------------------------------------------------------------------ the unit
REQUIRED_SERVICE = {
    "Type": "notify",
    "NotifyAccess": "main",
    "WatchdogSec": "30",
    "Restart": "always",
    "RestartSec": "2",
    "ExecStart": "/usr/bin/python3 /opt/12vhpwr-guard/linux/guardd.py",
    "ExecStartPre": "-/sbin/modprobe i2c-dev",
    "RuntimeDirectory": "12vhpwr-guard",
    "RuntimeDirectoryMode": "0755",
    "RuntimeDirectoryPreserve": "restart",
    "StateDirectory": "12vhpwr-guard",
    "StateDirectoryMode": "0755",
    "LogsDirectory": "12vhpwr-guard",
    "LogsDirectoryMode": "0755",
    "NoNewPrivileges": "yes",
    "ProtectSystem": "full",
    "ProtectHome": "read-only",
    "PrivateTmp": "yes",
}

# Each of these could stop the daemon reading /dev/i2c-* or /dev/nvidia*, loading i2c-dev,
# reaching a user's session bus, running runuser, or powering off through systemd.
BANNED_SERVICE = (
    "PrivateDevices", "DevicePolicy", "DeviceAllow", "ProtectKernelModules",
    "ProtectKernelTunables", "ProtectControlGroups", "CapabilityBoundingSet",
    "AmbientCapabilities", "SystemCallFilter", "RestrictAddressFamilies", "PrivateNetwork",
    "PrivateUsers", "IPAddressDeny", "MemoryDenyWriteExecute", "User", "Group", "DynamicUser",
    "ReadOnlyPaths", "InaccessiblePaths", "RestrictSUIDSGID", "ProtectProc",
)


@pytest.mark.parametrize("key, expected", sorted(REQUIRED_SERVICE.items()))
def test_unit_has_every_contract_directive(unit, key, expected):
    assert values(unit, "Service", key) == [expected], key


@pytest.mark.parametrize("key", BANNED_SERVICE)
def test_unit_has_none_of_the_banned_directives(unit, key):
    assert values(unit, "Service", key) == [], f"{key} would get in the way of protection"


def test_unit_sections_and_install_target(unit):
    assert set(unit) == {"Unit", "Service", "Install"}
    assert values(unit, "Install", "WantedBy") == ["multi-user.target"]
    assert values(unit, "Unit", "After") == ["systemd-modules-load.service"]
    assert values(unit, "Unit", "Description")
    assert values(unit, "Unit", "Documentation")[0].startswith("https://github.com/")
    # A crash loop must never leave the unit failed and the machine unprotected.
    assert values(unit, "Unit", "StartLimitIntervalSec") == ["0"]


def test_execstart_points_into_the_installed_layout(unit):
    interpreter, script = values(unit, "Service", "ExecStart")[0].split()
    assert interpreter == "/usr/bin/python3"
    assert script.startswith("/opt/12vhpwr-guard/")
    assert script[len("/opt/12vhpwr-guard/"):] in CONTRACT_FILES


def test_every_hardening_line_is_commented():
    """Each hardening option has a comment directly above it saying why it is safe here."""
    lines = read(UNIT_PATH).splitlines()
    for key in ("NoNewPrivileges", "ProtectSystem", "ProtectHome", "PrivateTmp"):
        i = next(n for n, line in enumerate(lines) if line.startswith(key + "="))
        assert lines[i - 1].startswith("#"), key


# ------------------------------------------------------------------ the scripts
bash = shutil.which("bash")


@pytest.mark.skipif(bash is None, reason="bash not on PATH")
@pytest.mark.parametrize("script", [INSTALL, UNINSTALL], ids=lambda p: p.name)
def test_scripts_pass_bash_n(script):
    proc = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("script", [INSTALL, UNINSTALL], ids=lambda p: p.name)
def test_scripts_are_strict_and_root_only(script):
    text = read(script)
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    assert 'EUID' in text and 'uname -s' in text


def test_install_references_every_contract_file():
    text = read(INSTALL)
    for f in CONTRACT_FILES:
        assert re.search(rf"^\s+{re.escape(f)}\s*$", text, re.M), f"FILES list lacks {f}"
    assert 'PREFIX="/opt/12vhpwr-guard"' in text


def test_install_never_deletes_the_config_or_data():
    text = read(INSTALL)
    for line in text.splitlines():
        if re.search(r"\brm\b", line) and not line.lstrip().startswith("#"):
            assert "CONF" not in line and "/etc/12vhpwr-guard" not in line, line
            assert "/var/lib/12vhpwr-guard" not in line and "/var/log/12vhpwr-guard" not in line
    # The config is only written when none exists.
    assert '[[ -e "$CONF_FILE" ]]' in text


def test_install_writes_the_modules_load_file_and_loads_i2c_dev():
    text = read(INSTALL)
    assert 'MODULES_LOAD="/etc/modules-load.d/12vhpwr-guard.conf"' in text
    assert re.search(r"printf '[^']*\\ni2c-dev\\n'", text), "the file must contain i2c-dev"
    assert "modprobe i2c-dev" in text


def test_install_unit_cli_and_upgrade_order():
    text = read(INSTALL)
    assert "systemctl daemon-reload" in text
    assert 'systemctl enable --now "$UNIT_NAME"' in text
    assert 'ln -sfn "$PREFIX/linux/guard_cli.py" "$CLI_LINK"' in text
    assert 'CLI_LINK="/usr/local/bin/12vhpwr-guard"' in text
    assert 'chmod 0755 "$PREFIX/linux/guard_cli.py"' in text
    stop = text.index('systemctl stop "$UNIT_NAME"')
    copy = text.index('install -m 0644 -o root -g root "$REPO_ROOT/$f" "$PREFIX/$f"')
    start = text.index('systemctl enable --now "$UNIT_NAME"')
    assert stop < copy < start, "an upgrade stops, copies, then starts"
    assert "systemctl --no-pager status" in text and '"$CLI_LINK" status' in text


def test_install_checks_python_and_warns_about_driver_and_bound_chip():
    text = read(INSTALL)
    assert 'PYTHON="/usr/bin/python3"' in text and "(3, 10)" in text
    assert "nvidia-smi" in text
    assert "*-002b" in text and "astral-hwmon" in text


def _function_body(text, name):
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start)
    return text[start:end]


def test_uninstall_keeps_config_and_data_unless_purge():
    text = read(UNINSTALL)
    purge = _function_body(text, "purge_data")
    outside = text.replace(purge, "")
    for var in ("$CONF_DIR", "$VAR_DIR", "$LOG_DIR"):
        assert var in purge
        for line in outside.splitlines():
            if re.search(r"\brm\b", line) and not line.lstrip().startswith("#"):
                assert var not in line, f"{var} removed outside purge_data: {line}"
    assert 'if [[ "$PURGE" == yes ]]; then\n    purge_data' in text
    assert "--purge) PURGE=yes" in text


def test_uninstall_removes_what_install_added():
    text = read(UNINSTALL)
    assert 'systemctl disable --now "$UNIT_NAME"' in text
    assert 'rm -f "$UNIT_DEST"' in text and "systemctl daemon-reload" in text
    assert 'rm -rf "$PREFIX"' in text and 'PREFIX="/opt/12vhpwr-guard"' in text
    assert 'rm -f "$MODULES_LOAD"' in text
    assert 'rm -f "$CLI_LINK"' in text


def test_uninstall_tells_how_to_restore_a_limited_card():
    text = read(UNINSTALL)
    assert 'MARKER="$RUN_DIR/mitigation.active"' in text
    assert 'RUN_DIR="/run/12vhpwr-guard"' in text
    assert "nvidia-smi -rgc" in text and "nvidia-smi -pl" in text
    assert "gpu-uv apply" in text and "/etc/gpu-uv/gpu-uv.conf" in text
    # The marker is looked at before the stop too, because systemd removes /run/12vhpwr-guard.
    assert text.index("MARKER_BEFORE=yes") < text.index("systemctl disable --now")


# ------------------------------------------------------------------ the example config
def windows_defaults():
    """The defaults hwinfo_12vhpwr_guard.load_config falls back to, as config.ini text values.

    Built from guard_core (which imports anywhere) plus the four Windows-module constants,
    restated here because that module cannot be imported on Linux. tests/test_f4_settings.py
    covers the Windows side of these.
    """
    d = {
        "threshold_amps": guard_core.DEFAULT_THRESHOLD_AMPS,
        "sustained_seconds_required": guard_core.DEFAULT_SUSTAINED_SECONDS_REQUIRED,
        "sensor_backend": "auto",
        "response_mode": guard_core.DEFAULT_RESPONSE_MODE,
        "sensor_loss_restart_seconds": 300.0,
    }
    d.update(guard_core.DEFAULT_TIER_VALUES)
    for key, value in guard_core.DEFAULT_ADVISORY_VALUES.items():
        if key == "advisories_enabled":
            d["advisories"] = "on" if value else "off"
        else:
            d[key] = value
    return d


def test_config_example_parses_with_the_windows_defaults():
    parser = configparser.ConfigParser()   # the same plain parser the guard uses
    parser.read_string(read(CONFIG_EXAMPLE))
    assert parser.sections() == ["Settings"]
    expected = windows_defaults()
    assert set(parser["Settings"]) == set(expected)
    for key, value in expected.items():
        raw = parser.get("Settings", key)
        if isinstance(value, float):
            assert float(raw) == value, key
        else:
            assert raw == value, key


def test_config_example_has_no_inline_comments():
    """configparser would read "9.5  # amps" as the value, and the key would fall back."""
    for line in read(CONFIG_EXAMPLE).splitlines():
        if "=" in line and not line.lstrip().startswith(("#", ";")):
            assert "#" not in line and ";" not in line, line


@pytest.mark.skipif(sys.platform != "win32" or os.environ.get("GUARD_TESTS_AS_NON_WINDOWS") == "1",
                    reason="needs the Windows guard module")
def test_config_example_matches_the_windows_module_defaults():
    import hwinfo_12vhpwr_guard as g
    assert g.DEFAULT_SENSOR_BACKEND == "auto"
    assert g.DEFAULT_SENSOR_LOSS_RESTART_SECONDS == 300.0
    assert g.ADVISORIES_CONFIG_KEY == "advisories"


# ------------------------------------------------------------------ Linux test collection
KNOWN_WINDOWS_ONLY = {
    "test_f1_advisor.py", "test_f2_sensor_path.py", "test_f3_restart.py",
    "test_f4_settings.py", "test_f5_release.py", "test_s2_mitigation_platform.py",
    "test_supervisor.py", "test_tiers.py", "test_v13.py",
}
KNOWN_LINUX_SAFE = {
    "test_guard_core.py", "test_astral_linux.py", "test_linux_poc.py",
    "test_nvml_linux_tools.py", "test_linux_notify.py", "test_linux_cli.py",
    "test_linux_packaging.py",
}


def test_split_puts_known_files_on_the_right_side():
    ignored = set(platform_split.windows_only_test_files(str(TESTS_DIR)))
    assert KNOWN_WINDOWS_ONLY <= ignored
    assert not (KNOWN_LINUX_SAFE & ignored)


def test_split_follows_test_to_test_imports(tmp_path):
    (tmp_path / "test_a.py").write_text("import helpers\n", encoding="utf-8")
    (tmp_path / "test_b.py").write_text("from test_a import X\n", encoding="utf-8")
    (tmp_path / "test_c.py").write_text("import test_b\n", encoding="utf-8")
    (tmp_path / "test_d.py").write_text(
        "try:\n    import hwinfo_12vhpwr_guard as g\nexcept Exception:\n    g = None\n"
        "def f():\n    import helpers\n", encoding="utf-8")
    (tmp_path / "test_e.py").write_text("import guard_core\n", encoding="utf-8")
    assert platform_split.windows_only_test_files(str(tmp_path)) == [
        "test_a.py", "test_b.py", "test_c.py"]


def test_simulated_linux_collection_ignores_exactly_the_windows_only_files():
    """Runs pytest --collect-only through conftest's non-Windows branch, in a subprocess.

    GUARD_TESTS_AS_NON_WINDOWS=1 makes conftest behave as on Linux and blocks the Windows
    guard and helpers from importing, so a file that still needed them would error here.
    """
    if os.environ.get("GUARD_TESTS_AS_NON_WINDOWS") == "1":
        pytest.skip("already inside the simulated run")
    env = dict(os.environ, GUARD_TESTS_AS_NON_WINDOWS="1")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         str(TESTS_DIR)],
        capture_output=True, text=True, timeout=300, cwd=str(REPO_ROOT), env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    collected = {line.split("::")[0].replace("\\", "/").rsplit("/", 1)[-1]
                 for line in proc.stdout.splitlines() if "::" in line}
    every = {p.name for p in TESTS_DIR.glob("test_*.py")}
    ignored = set(platform_split.windows_only_test_files(str(TESTS_DIR)))
    assert collected == every - ignored
    assert KNOWN_WINDOWS_ONLY <= ignored


def test_linux_files_are_lf_in_the_working_copy_and_pinned_by_gitattributes():
    """The Linux install often runs straight off a Windows drive. A CRLF install.sh, unit file
    or #! line breaks bash, systemd and the CLI, so the working copy itself must be LF."""
    repo = Path(__file__).resolve().parent.parent
    rules = (repo / ".gitattributes").read_text(encoding="utf-8").split()
    for rule in ("*.sh", "*.service", "linux/**"):
        assert rule in rules, f".gitattributes is missing {rule}"
    for path in sorted((repo / "linux").iterdir()):
        if path.is_file():
            assert b"\r\n" not in path.read_bytes(), f"{path.name} has CRLF line endings"

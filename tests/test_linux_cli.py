"""Unit tests for linux/guard_cli.py, the `12vhpwr-guard` command.

Run anywhere, including Windows: the state file is a tmp_path file, the clock is a number,
exec and euid are fakes, and the notifier is a double, so nothing is run and no desktop is
touched. Self-contained (no conftest fixture, no Windows guard), so it also runs in Linux CI.
"""

import io
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LINUX_DIR = REPO_ROOT / "linux"
if str(LINUX_DIR) not in sys.path:
    sys.path.insert(0, str(LINUX_DIR))

import guard_cli as cli  # noqa: E402
import notify  # noqa: E402

NOW = 1_800_000_000.0


def state(**over):
    base = {
        "version": "1.5.0", "pid": 4242, "updated": NOW - 0.4, "status": "OK",
        "message": "All pins normal", "max_label": "GPU 12VHPWR Pin3 Current",
        "max_val": 8.617, "max_unit": "A", "source": "direct", "mode": "tiered",
        "mitigation": "ready", "advisory": None, "sensor_path_fault": None,
    }
    base.update(over)
    return base


def write(tmp_path, data, raw=None):
    path = tmp_path / "state.json"
    path.write_text(raw if raw is not None else json.dumps(data), encoding="utf-8")
    return str(path)


def call(argv, path, now=NOW, **kw):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, state_path=path, now=lambda: now, out=out, err=err, **kw)
    return code, out.getvalue(), err.getvalue()


# ------------------------------------------------------------------ status
def test_status_ok_summary_and_exit_0(tmp_path):
    code, out, _ = call(["status"], write(tmp_path, state()))
    assert code == 0
    assert out.splitlines()[0] == "12VHPWR Guard: OK (protected)"
    assert "GPU 12VHPWR Pin3 Current: 8.62 A" in out
    assert "Source:      direct" in out
    assert "Mode:        tiered" in out
    assert "Mitigation:  ready" in out
    assert "Advisory:    none" in out
    assert "0.4 s ago by pid 4242" in out


@pytest.mark.parametrize("status", ["OVER", "MITIGATED", "WAITING", "ERROR", "SHUTDOWN"])
def test_attention_statuses_exit_2(tmp_path, status):
    code, out, _ = call(["status"], write(tmp_path, state(status=status)))
    assert code == 2
    assert out.startswith(f"12VHPWR Guard: {status} (")


def test_unknown_status_is_never_reported_healthy(tmp_path):
    code, out, _ = call(["status"], write(tmp_path, state(status="SOMETHING_NEW")))
    assert code == 2
    assert "unknown status" in out


def test_stopped_counts_as_not_running(tmp_path):
    code, _, _ = call(["status"], write(tmp_path, state(status="STOPPED")))
    assert code == 1


def test_missing_state_file_exits_1(tmp_path):
    code, out, err = call(["status"], str(tmp_path / "nope.json"))
    assert code == 1 and out == ""
    assert "NOT RUNNING" in err and "systemctl status 12vhpwr-guard" in err


def test_corrupt_state_file_exits_1(tmp_path):
    code, _, err = call(["status"], write(tmp_path, None, raw="{not json"))
    assert code == 1 and "cannot read" in err
    code, _, _ = call(["status"], write(tmp_path, None, raw="[1, 2]"))
    assert code == 1


@pytest.mark.parametrize("age, expected", [
    (0.0, 0), (9.9, 0), (10.0, 0), (10.1, 1), (600.0, 1), (-11.0, 1), (-5.0, 0),
])
def test_stale_detection_at_ten_seconds(tmp_path, age, expected):
    code, _, _ = call(["status"], write(tmp_path, state(updated=NOW - age)))
    assert code == expected, age


def test_stale_wins_over_an_attention_status(tmp_path):
    code, out, _ = call(["status"], write(tmp_path, state(status="SHUTDOWN", updated=NOW - 60)))
    assert code == 1
    assert out.startswith("12VHPWR Guard: NOT RUNNING (last state SHUTDOWN, written 60.0 s ago)")


def test_missing_or_bad_timestamp_is_stale(tmp_path):
    data = state()
    del data["updated"]
    assert call(["status"], write(tmp_path, data))[0] == 1
    assert call(["status"], write(tmp_path, state(updated="soon")))[0] == 1


def test_waiting_with_no_reading_and_the_optional_lines(tmp_path):
    data = state(status="WAITING", max_label=None, max_val=None, max_unit=None, source=None,
                 mode="shutdown_only", mitigation="unavailable: not root",
                 advisory="Pin 4 reads no current", sensor_path_fault="pins total 0 A at 410 W",
                 message="Waiting for pin sensors: NOT PROTECTED")
    code, out, _ = call(["status"], write(tmp_path, data))
    assert code == 2
    assert "Hottest pin: no reading" in out
    assert "Source:      none" in out
    assert "Mitigation:  unavailable: not root" in out
    assert "Advisory:    Pin 4 reads no current" in out
    assert "Sensor path: pins total 0 A at 410 W" in out
    assert "Message:     Waiting for pin sensors: NOT PROTECTED" in out


def test_json_prints_the_raw_file_and_keeps_the_exit_code(tmp_path):
    raw = json.dumps(state(status="MITIGATED"), indent=1)
    code, out, _ = call(["status", "--json"], write(tmp_path, None, raw=raw))
    assert out == raw + "\n"
    assert code == 2


def test_no_subcommand_prints_help_and_exits_1(tmp_path):
    code, out, err = call([], str(tmp_path / "x"))
    assert code == 1 and "status" in err


# ------------------------------------------------------------------ log
def test_log_execs_journalctl_for_the_unit_with_extra_args(tmp_path):
    seen = []
    code, _, _ = call(["log", "-f", "--since", "today"], str(tmp_path / "x"),
                      which=lambda n: "/usr/bin/journalctl" if n == "journalctl" else None,
                      execv=lambda path, argv: seen.append((path, argv)))
    assert code == 0
    assert seen == [("/usr/bin/journalctl",
                     ["/usr/bin/journalctl", "-u", "12vhpwr-guard", "-n", "200", "--no-pager",
                      "-f", "--since", "today"])]


def test_log_without_journalctl_exits_1(tmp_path, monkeypatch):
    monkeypatch.setattr(notify.os.path, "isfile", lambda p: False)
    code, _, err = call(["log"], str(tmp_path / "x"), which=lambda n: None,
                        execv=lambda *a: pytest.fail("must not exec"))
    assert code == 1 and "journalctl not found" in err


def test_log_exec_failure_exits_1(tmp_path):
    def fail(path, argv):
        raise OSError("exec format error")
    code, _, err = call(["log"], str(tmp_path / "x"),
                        which=lambda n: "/usr/bin/journalctl", execv=fail)
    assert code == 1 and "could not run journalctl" in err


# ------------------------------------------------------------------ test-notify
def report(reached, users=("alice",), skipped=None, errors=()):
    sessions = [notify.Session(1000 + i, u, f"/run/user/{1000 + i}/bus")
                for i, u in enumerate(users)]
    return notify.NotifyReport(sessions if not skipped else [], reached, skipped, list(errors))


def test_test_notify_needs_root(tmp_path):
    code, _, err = call(["test-notify"], str(tmp_path / "x"), geteuid=lambda: 1000,
                        notifier=lambda *a: pytest.fail("must not notify"))
    assert code == 1 and "sudo 12vhpwr-guard test-notify" in err


def test_test_notify_reports_the_count(tmp_path):
    sent = []

    def notifier(title, body, critical):
        sent.append((title, body, critical))
        return report(2, users=("alice", "bob"))
    code, out, _ = call(["test-notify"], str(tmp_path / "x"), geteuid=lambda: 0,
                        notifier=notifier)
    assert code == 0
    assert sent == [("12VHPWR Guard - test", cli.TEST_NOTIFY_BODY, False)]
    assert "Reached 2 of 2 desktop sessions." in out
    assert "alice (uid 1000)" in out and "bob (uid 1001)" in out


def test_test_notify_zero_reached_exits_1_with_the_reason(tmp_path):
    code, out, _ = call(["test-notify"], str(tmp_path / "x"), geteuid=lambda: 0,
                        notifier=lambda *a: report(0, skipped="notify-send not found"))
    assert code == 1 and "Reached 0 desktop sessions: notify-send not found" in out
    code, out, _ = call(["test-notify"], str(tmp_path / "x"), geteuid=lambda: 0,
                        notifier=lambda *a: report(0, errors=["alice: notify-send exited 1"]))
    assert code == 1
    assert "Reached 0 of 1 desktop session." in out and "failed: alice" in out


# ------------------------------------------------------------------ the installed file
def test_shebang_runs_isolated_system_python():
    first = (LINUX_DIR / "guard_cli.py").read_text(encoding="utf-8").splitlines()[0]
    assert first == "#!/usr/bin/python3 -I"


def test_cli_finds_notify_next_to_its_real_path():
    source = (LINUX_DIR / "guard_cli.py").read_text(encoding="utf-8")
    assert "os.path.realpath(__file__)" in source
    assert cli._HERE == str(LINUX_DIR) or Path(cli._HERE) == LINUX_DIR

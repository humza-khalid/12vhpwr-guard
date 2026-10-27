"""Unit tests for linux/notify.py, the daemon's desktop notifications and wall messages.

Run anywhere, including Windows: every outside effect (the /run/user listing, the socket check,
the password database, tool lookup, the subprocess) is handed in as a fake, so no command is
ever run and nobody's desktop is touched.

Self-contained (no conftest fixture, no Windows guard), so it also runs in the Linux CI job.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LINUX_DIR = REPO_ROOT / "linux"
if str(LINUX_DIR) not in sys.path:
    sys.path.insert(0, str(LINUX_DIR))

import notify  # noqa: E402

TOOLS = {
    "runuser": "/usr/sbin/runuser",
    "env": "/usr/bin/env",
    "notify-send": "/usr/bin/notify-send",
    "wall": "/usr/bin/wall",
    "journalctl": "/usr/bin/journalctl",
}

USERS = {1000: "alice", 1001: "bob", 0: "root", 999: "gdm", 1002: "carol"}


def fake_which(table):
    return lambda name: table.get(name)


class Recorder:
    """A runner double: keeps every argv (and stdin) and answers with a fixed exit code."""

    def __init__(self, code=0, raises=None, per_user=None):
        self.calls = []
        self.code = code
        self.raises = raises
        self.per_user = per_user or {}

    def __call__(self, argv, timeout, stdin_text=None):
        self.calls.append((list(argv), timeout, stdin_text))
        user = argv[2] if len(argv) > 2 else None
        outcome = self.per_user.get(user, self.raises)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, int):
            return outcome
        return self.code


def run(title="T", body="B", critical=False, *, entries=("1000", "1001"), sockets=None,
        runner=None, tools=TOOLS, users=USERS):
    sockets = set(sockets) if sockets is not None else {f"/run/user/{e}/bus" for e in entries}
    runner = runner or Recorder()
    report = notify.notify_sessions(
        title, body, critical,
        run_root="/run/user",
        runner=runner,
        which=fake_which(tools),
        user_name=users.get,
        is_socket=lambda p: p.replace("\\", "/") in sockets,
        listdir=lambda root: list(entries),
    )
    return report, runner


def norm(argv):
    """os.path.join uses backslashes on Windows; the argv is built for Linux."""
    return [a.replace("\\", "/") for a in argv]


# ------------------------------------------------------------------ argv
def test_one_argv_per_user_with_that_users_bus():
    report, rec = run("Title here", "Body here")
    assert report.reached == 2 and report.skipped is None and report.errors == []
    argvs = sorted(norm(c[0]) for c in rec.calls)
    assert argvs[0] == [
        "/usr/sbin/runuser", "-u", "alice", "--",
        "/usr/bin/env",
        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
        "XDG_RUNTIME_DIR=/run/user/1000",
        "/usr/bin/notify-send", "--app-name=12VHPWR Guard",
        "--", "Title here", "Body here",
    ]
    assert argvs[1][2] == "bob"
    assert "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus" in argvs[1]


def test_critical_adds_urgency_before_the_title():
    _, rec = run("Shutdown", "now", critical=True, entries=("1000",))
    argv = rec.calls[0][0]
    i = argv.index("-u", 3)   # the first -u belongs to runuser
    assert argv[i + 1] == "critical"
    assert argv.index("--", 5) > i, "urgency goes before notify-send's end of options"
    assert argv[-2:] == ["Shutdown", "now"]


def test_normal_urgency_has_no_flag():
    _, rec = run(entries=("1000",))
    assert "critical" not in rec.calls[0][0]


def test_text_is_passed_as_one_argument_each_never_through_a_shell():
    title = "-x; rm -rf / $(reboot) `id`"
    body = "a\nb & c | d > e"
    _, rec = run(title, body, entries=("1000",))
    argv = rec.calls[0][0]
    assert argv[-2:] == [title, body]
    assert argv[0].startswith("/"), "absolute tool path, no shell lookup"


def test_nul_bytes_are_dropped_from_the_text():
    _, rec = run("a\x00b", "c\x00", entries=("1000",))
    assert rec.calls[0][0][-2:] == ["ab", "c"]


def test_the_timeout_reaches_the_runner():
    _, rec = run(entries=("1000",))
    assert rec.calls[0][1] == notify.NOTIFY_TIMEOUT_SEC


# ------------------------------------------------------------------ who gets one
def test_only_uid_1000_and_up_with_a_bus_socket():
    entries = ("0", "999", "1000", "1001", "1002", "not-a-uid", "1003")
    # 1001 has a run dir but no bus socket; 1003 has no password entry.
    sockets = {"/run/user/0/bus", "/run/user/999/bus", "/run/user/1000/bus",
               "/run/user/1002/bus", "/run/user/1003/bus"}
    report, rec = run(entries=entries, sockets=sockets)
    assert [s.user for s in report.sessions] == ["alice", "carol"]
    assert sorted(c[0][2] for c in rec.calls) == ["alice", "carol"]


def test_odd_user_names_are_skipped():
    users = {1000: "-rf", 1001: "bad name", 1002: "ok"}
    report, _ = run(entries=("1000", "1001", "1002"), users=users)
    assert [s.user for s in report.sessions] == ["ok"]


def test_no_sessions_means_zero_and_a_reason():
    report, rec = run(entries=())
    assert report.reached == 0 and rec.calls == []
    assert "no logged-in user" in report.skipped


def test_unreadable_run_root_means_zero():
    def boom(root):
        raise PermissionError("nope")
    report = notify.notify_sessions("t", "b", run_root="/run/user", runner=Recorder(),
                                    which=fake_which(TOOLS), user_name=USERS.get,
                                    is_socket=lambda p: True, listdir=boom)
    assert report.reached == 0 and report.skipped


# ------------------------------------------------------------------ missing tools
@pytest.mark.parametrize("missing", ["notify-send", "runuser", "env"])
def test_a_missing_tool_skips_without_raising(missing, monkeypatch):
    # find_tool also looks in the fixed directories; make sure that finds nothing either.
    monkeypatch.setattr(notify.os.path, "isfile", lambda p: False)
    tools = {k: v for k, v in TOOLS.items() if k != missing}
    report, rec = run(tools=tools)
    assert report.reached == 0
    assert rec.calls == [], "nothing is run when a tool is missing"
    assert missing in report.skipped
    if missing == "notify-send":
        assert "libnotify-bin" in report.skipped


def test_find_tool_ignores_a_relative_which_answer(monkeypatch):
    monkeypatch.setattr(notify.os.path, "isfile", lambda p: False)
    assert notify.find_tool("notify-send", lambda n: "bin/notify-send") is None
    assert notify.find_tool("notify-send", lambda n: "/opt/x/notify-send") == "/opt/x/notify-send"


def test_find_tool_falls_back_to_the_fixed_directories(monkeypatch):
    monkeypatch.setattr(notify.os.path, "isfile",
                        lambda p: p.replace("\\", "/") == "/usr/sbin/runuser")
    monkeypatch.setattr(notify.os, "access", lambda p, mode: True)
    assert notify.find_tool("runuser", lambda n: None).replace("\\", "/") == "/usr/sbin/runuser"


def test_find_tool_survives_a_which_that_raises(monkeypatch):
    monkeypatch.setattr(notify.os.path, "isfile", lambda p: False)

    def bad(name):
        raise OSError("PATH unreadable")
    assert notify.find_tool("wall", bad) is None


# ------------------------------------------------------------------ failures never raise
@pytest.mark.parametrize("error", [
    OSError("exec failed"),
    subprocess.TimeoutExpired(["notify-send"], 4.0),
    ValueError("embedded null byte"),
    RuntimeError("anything"),
])
def test_a_failing_subprocess_never_raises(error):
    report, _ = run(runner=Recorder(raises=error))
    assert report.reached == 0
    assert len(report.errors) == 2


def test_a_nonzero_exit_is_not_a_reach_and_others_still_count():
    report, _ = run(runner=Recorder(per_user={"alice": 1}))
    assert report.reached == 1
    assert report.errors == ["alice: notify-send exited 1"]


def test_a_timeout_names_the_user():
    report, _ = run(entries=("1000",),
                    runner=Recorder(raises=subprocess.TimeoutExpired(["x"], 4.0)))
    assert report.errors and report.errors[0].startswith("alice: no answer within")


def test_send_desktop_notification_returns_the_count(monkeypatch):
    calls = []

    def fake(title, body, critical=False):
        calls.append((title, body, critical))
        return notify.NotifyReport([], 3, None, [])
    monkeypatch.setattr(notify, "notify_sessions", fake)
    assert notify.send_desktop_notification("t", "b", critical=True) == 3
    assert calls == [("t", "b", True)]


def test_send_desktop_notification_never_raises_even_if_everything_breaks(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(notify, "notify_sessions", broken)
    assert notify.send_desktop_notification("t", "b") == 0


def test_notify_sessions_reports_an_internal_error_instead_of_raising():
    def broken_listdir(root):
        return None   # not iterable: the outer guard turns the TypeError into a report
    report = notify.notify_sessions("t", "b", runner=Recorder(), which=fake_which(TOOLS),
                                    user_name=USERS.get, is_socket=lambda p: True,
                                    listdir=broken_listdir)
    assert report.reached == 0


# ------------------------------------------------------------------ wall
def test_wall_sends_the_message_on_stdin_not_as_an_argument():
    rec = Recorder()
    assert notify.send_wall("Shutting down: Pin3 at 16 A", runner=rec,
                            which=fake_which(TOOLS)) is True
    argv, timeout, stdin_text = rec.calls[0]
    assert argv == ["/usr/bin/wall"]
    assert stdin_text == "Shutting down: Pin3 at 16 A\n"
    assert timeout == notify.WALL_TIMEOUT_SEC


def test_wall_missing_or_failing_returns_false(monkeypatch):
    monkeypatch.setattr(notify.os.path, "isfile", lambda p: False)
    assert notify.send_wall("x", runner=Recorder(), which=fake_which({})) is False
    assert notify.send_wall("x", runner=Recorder(code=1), which=fake_which(TOOLS)) is False
    assert notify.send_wall("x", runner=Recorder(raises=OSError("no")),
                            which=fake_which(TOOLS)) is False


def test_module_has_no_shell_true():
    source = (LINUX_DIR / "notify.py").read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "os.system" not in source and "os.popen" not in source

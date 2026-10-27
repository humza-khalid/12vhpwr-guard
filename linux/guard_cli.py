#!/usr/bin/python3 -I
"""
12vhpwr-guard: the command line view of the Linux daemon (v1.5, stage S4b).

Installed at /opt/12vhpwr-guard/linux/guard_cli.py and linked as /usr/local/bin/12vhpwr-guard.
Linux has no tray in v1.5, so this is how a person asks the guard what it is doing:

    12vhpwr-guard status [--json]   what the daemon last wrote to /run/12vhpwr-guard/state.json
    12vhpwr-guard log [ARGS...]     journalctl -u 12vhpwr-guard -n 200 --no-pager [ARGS...]
    sudo 12vhpwr-guard test-notify  send one ordinary desktop notification to every session

`status` exit codes, so scripts and a monitoring check can use it:
    0  the daemon is running and protecting (status OK)
    1  not running: no state file, an unreadable one, an update older than STALE_AFTER_SEC,
       or a daemon that said it stopped
    2  running but needs attention: OVER, MITIGATED, WAITING, ERROR, SHUTDOWN, or a status
       this version does not know

The shebang runs Python isolated (-I): no PYTHONPATH, no user site directory and no script
directory on sys.path, because this is often run with sudo. The one module it needs, notify.py,
is found next to the real file (the symlink is followed with realpath) and added explicitly.

Stdlib only. Every outside effect goes through a seam on main() so tests/test_linux_cli.py can
drive it on Windows.
"""

import argparse
import json
import os
import sys
import time
from typing import Callable, List, Optional, Sequence, TextIO

_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import notify  # noqa: E402

UNIT = "12vhpwr-guard"
STATE_PATH = "/run/12vhpwr-guard/state.json"

# The daemon rewrites the state file on every loop (the Windows guard polls every 0.5 s, and
# the daemon shares its timings). Ten seconds without an update is a stopped, hung or
# crash-looping daemon, not jitter. A third of the unit's 30 s watchdog, so status says so
# well before systemd steps in.
STALE_AFTER_SEC = 10.0

EXIT_OK = 0
EXIT_NOT_RUNNING = 1
EXIT_ATTENTION = 2

ATTENTION_STATUSES = ("OVER", "MITIGATED", "WAITING", "ERROR", "SHUTDOWN")

# One plain line per status, shown after the status word.
STATUS_MEANING = {
    "OK": "protected",
    "OVER": "a pin is over the threshold, the timer is running",
    "MITIGATED": "the GPU is limited to bring the current down",
    "WAITING": "waiting for the pin sensors, NOT protected",
    "ERROR": "the guard hit an error, NOT protected",
    "SHUTDOWN": "shutting the machine down",
    "STOPPED": "the daemon stopped, NOT protected",
}

TEST_NOTIFY_TITLE = "12VHPWR Guard - test"
TEST_NOTIFY_BODY = "If you can read this, the guard can reach your desktop."


# ------------------------------------------------------------------ status
def read_state(path: str = STATE_PATH):
    """(raw text, parsed dict) or (None, None) when there is no file. Raises on a bad file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return None, None
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("state file is not a JSON object")
    return raw, data


def state_age(state: dict, now: float) -> Optional[float]:
    """Seconds since the daemon last wrote, or None when the file carries no usable time."""
    try:
        return now - float(state.get("updated"))
    except (TypeError, ValueError):
        return None


def is_stale(age: Optional[float]) -> bool:
    """No time, too old, or too far in the future.

    The future case is a wall clock that was stepped back after the last write: the file would
    otherwise look fresh for as long as the step was, with nothing updating it.
    """
    return age is None or age > STALE_AFTER_SEC or age < -STALE_AFTER_SEC


def exit_code_for(state: Optional[dict], now: float) -> int:
    if state is None or is_stale(state_age(state, now)):
        return EXIT_NOT_RUNNING
    status = str(state.get("status", "")).upper()
    if status == "OK":
        return EXIT_OK
    if status == "STOPPED":
        return EXIT_NOT_RUNNING
    # The listed attention states, and anything unknown: never report a status this version
    # cannot read as healthy.
    return EXIT_ATTENTION


def _format_age(age: Optional[float]) -> str:
    if age is None:
        return "unknown"
    if age < 0:
        return f"{-age:.1f} s in the future (clock changed?)"
    if age < 120:
        return f"{age:.1f} s ago"
    if age < 7200:
        return f"{age / 60:.0f} min ago"
    return f"{age / 3600:.1f} h ago"


def _format_reading(state: dict) -> str:
    label = state.get("max_label")
    value = state.get("max_val")
    unit = state.get("max_unit") or ""
    if label is None and value is None:
        return "no reading"
    try:
        shown = f"{float(value):.2f} {unit}".rstrip()
    except (TypeError, ValueError):
        shown = "no value"
    return f"{label or 'unknown pin'}: {shown}"


def format_status(state: dict, now: float) -> str:
    """The human summary `status` prints. Pure, so tests can check the wording."""
    status = str(state.get("status", "UNKNOWN")).upper()
    age = state_age(state, now)
    meaning = STATUS_MEANING.get(status, "unknown status")
    lines = []
    if is_stale(age):
        lines.append(f"12VHPWR Guard: NOT RUNNING (last state {status}, written {_format_age(age)})")
        lines.append(f"  Check the service: systemctl status {UNIT}")
    else:
        lines.append(f"12VHPWR Guard: {status} ({meaning})")
    message = state.get("message")
    if message:
        lines.append(f"  Message:     {message}")
    lines.append(f"  Hottest pin: {_format_reading(state)}")
    lines.append(f"  Source:      {state.get('source') or 'none'}")
    lines.append(f"  Mode:        {state.get('mode') or 'unknown'}")
    lines.append(f"  Mitigation:  {state.get('mitigation') or 'unknown'}")
    lines.append(f"  Advisory:    {state.get('advisory') or 'none'}")
    fault = state.get("sensor_path_fault")
    if fault:
        lines.append(f"  Sensor path: {fault}")
    pid = state.get("pid")
    pid_text = f" by pid {pid}" if pid is not None else ""
    lines.append(f"  Updated:     {_format_age(age)}{pid_text}")
    return "\n".join(lines)


def cmd_status(args, state_path: str, now: Callable[[], float], out: TextIO, err: TextIO) -> int:
    try:
        raw, state = read_state(state_path)
    except Exception as ex:
        print(f"12VHPWR Guard: cannot read {state_path}: {ex}", file=err)
        return EXIT_NOT_RUNNING
    if state is None:
        print(f"12VHPWR Guard: NOT RUNNING (no {state_path})", file=err)
        print(f"  Check the service: systemctl status {UNIT}", file=err)
        return EXIT_NOT_RUNNING
    t = now()
    if args.json:
        out.write(raw if raw.endswith("\n") else raw + "\n")
    else:
        print(format_status(state, t), file=out)
    return exit_code_for(state, t)


# ------------------------------------------------------------------ log
def build_log_argv(journalctl: str, extra: Sequence[str]) -> List[str]:
    """journalctl for the unit; extra arguments (-f, -b, a later -n) are passed through."""
    return [journalctl, "-u", UNIT, "-n", "200", "--no-pager", *extra]


def cmd_log(extra: Sequence[str], which, execv, err: TextIO) -> int:
    journalctl = notify.find_tool("journalctl", which)
    if journalctl is None:
        print("12VHPWR Guard: journalctl not found; this needs a systemd system", file=err)
        return 1
    argv = build_log_argv(journalctl, extra)
    try:
        execv(journalctl, argv)  # replaces this process; only returns when exec failed
    except OSError as ex:
        print(f"12VHPWR Guard: could not run journalctl: {ex}", file=err)
        return 1
    return 0  # reached only with a test double for execv


# ------------------------------------------------------------------ test-notify
def cmd_test_notify(geteuid, notifier, out: TextIO, err: TextIO) -> int:
    if geteuid() != 0:
        # runuser only works from root, the same way the daemon uses it.
        print("12VHPWR Guard: test-notify needs root: sudo 12vhpwr-guard test-notify", file=err)
        return 1
    report = notifier(TEST_NOTIFY_TITLE, TEST_NOTIFY_BODY, False)
    if report.skipped:
        print(f"Reached 0 desktop sessions: {report.skipped}", file=out)
        return 1
    noun = "session" if len(report.sessions) == 1 else "sessions"
    print(f"Reached {report.reached} of {len(report.sessions)} desktop {noun}.", file=out)
    for session in report.sessions:
        print(f"  {session.user} (uid {session.uid})", file=out)
    for line in report.errors:
        print(f"  failed: {line}", file=out)
    return 0 if report.reached > 0 else 1


# ------------------------------------------------------------------ main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="12vhpwr-guard",
        description="12VHPWR Guard on Linux: daemon status, its log, and a notification test.")
    sub = parser.add_subparsers(dest="command", metavar="{status,log,test-notify}")
    status = sub.add_parser("status", help="what the daemon is doing right now")
    status.add_argument("--json", action="store_true", help="print the raw state file")
    sub.add_parser("log", help="the daemon's journal (extra arguments go to journalctl)",
                   add_help=False)
    sub.add_parser("test-notify", help="send a test desktop notification (needs root)")
    return parser


def main(argv: Optional[Sequence[str]] = None, *,
         state_path: str = STATE_PATH,
         now: Callable[[], float] = time.time,
         which=None,
         execv=os.execv,
         geteuid=None,
         notifier=notify.notify_sessions,
         out: Optional[TextIO] = None,
         err: Optional[TextIO] = None) -> int:
    """Entry point. The keyword arguments are test seams; the installed command uses defaults."""
    argv = list(sys.argv[1:] if argv is None else argv)
    out = out or sys.stdout
    err = err or sys.stderr
    if which is None:
        import shutil
        which = shutil.which
    if geteuid is None:
        geteuid = getattr(os, "geteuid", lambda: -1)

    # `log` takes journalctl's own options (-f, -b, --since ...), which argparse would reject
    # as unknown, so everything after the word is handed over untouched.
    if argv and argv[0] == "log":
        return cmd_log(argv[1:], which, execv, err)

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "status":
        return cmd_status(args, state_path, now, out, err)
    if args.command == "test-notify":
        return cmd_test_notify(geteuid, notifier, out, err)
    parser.print_help(file=err)
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""
Desktop notifications and wall messages for the Linux daemon (v1.5, stage S4b).

The daemon runs as root from boot under systemd, so it has no desktop of its own and no session
bus to talk to. To reach the people at the machine it looks for every logged-in user that has a
session bus socket at /run/user/<uid>/bus and, for each one, runs

    runuser -u <user> -- env DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus \
        XDG_RUNTIME_DIR=/run/user/<uid> notify-send --app-name="12VHPWR Guard" [-u critical] \
        -- <title> <body>

as that user. runuser (not su) because it is the tool meant for root dropping to a user: it asks
for no password, it is not setuid, and it works under the unit's NoNewPrivileges=yes because
going from root down to a user is not gaining a privilege. The session bus checks the caller's
uid, so notify-send has to run as the user and not as root.

A shutdown also goes out with wall, so a text console or an SSH session sees it too.

Two promises the daemon relies on:
- Nothing here ever raises. A notification is a courtesy on top of the protection, never part of
  it, so every failure (tool missing, user gone, bus dead, timeout, odd bytes in a label) ends as
  a count of 0 or False plus a debug line, and the daemon carries on protecting.
- Nothing goes through a shell. Every command is an argv list, so a pin label or a message can
  never be read as shell syntax. Tools are run by absolute path, found once per call.

Timing: all sessions are notified in parallel and the whole call is bounded by
NOTIFY_TIMEOUT_SEC (plus a little thread overhead), whatever the number of users. A stuck
notify-send (a session whose notification server never answers) is killed together with its
process group when the timeout runs out.

Stdlib only. pwd is imported lazily inside the default user lookup, so this module imports on
Windows too, which is where its unit tests run against fakes (tests/test_linux_notify.py).
"""

import logging
import os
import posixpath
import shutil
import signal
import stat
import subprocess
import threading
import time
from typing import Callable, List, NamedTuple, Optional, Sequence

APP_NAME = "12VHPWR Guard"

# Real logins start at 1000 on Debian, Ubuntu and Mint. Below that are system accounts, which
# never have a desktop worth notifying.
MIN_UID = 1000

# Per call, not per user: sessions run in parallel. notify-send normally answers in well under
# a second; this only bounds a session whose notification server has hung.
NOTIFY_TIMEOUT_SEC = 4.0
WALL_TIMEOUT_SEC = 4.0

RUN_USER_ROOT = "/run/user"

# A root service does not always get a full PATH (systemd gives a fixed default, sudo may give
# secure_path), so after shutil.which these directories are tried by hand.
FALLBACK_DIRS = ("/usr/bin", "/usr/sbin", "/bin", "/sbin")

LOG = logging.getLogger("12vhpwr-guard.notify")


class Session(NamedTuple):
    uid: int
    user: str
    bus: str            # absolute path of the session bus socket


class NotifyReport(NamedTuple):
    """What one notification attempt did. send_desktop_notification returns only .reached."""
    sessions: List[Session]     # every session that was found and tried
    reached: int                # how many notify-send runs exited 0
    skipped: Optional[str]      # why nothing was tried at all (missing tool, no sessions), or None
    errors: List[str]           # one line per session that failed, for a debug log or the CLI


# ------------------------------------------------------------------ seams (defaults)
def find_tool(name: str, which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """Absolute path of an executable, or None.

    shutil.which first, keeping its answer only when it is absolute (a relative PATH entry would
    make the result depend on the working directory), then the fixed FALLBACK_DIRS.
    """
    try:
        found = which(name)
    except Exception:
        found = None
    if found and os.path.isabs(found):
        return found
    for directory in FALLBACK_DIRS:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _user_name(uid: int) -> Optional[str]:
    """The login name for a uid from the password database, or None when there is none."""
    try:
        import pwd  # POSIX only; imported here so the module still loads on Windows
        return pwd.getpwuid(uid).pw_name
    except Exception:
        return None


def _is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def _kill_group(proc: "subprocess.Popen") -> None:
    """SIGKILL the child's whole process group, so runuser's own notify-send child dies too."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run(argv: Sequence[str], timeout: float, stdin_text: Optional[str] = None) -> int:
    """Default runner: start argv in its own session, wait up to timeout, return the exit code.

    Raises subprocess.TimeoutExpired (after killing the process group) or OSError; the public
    functions below turn both into a failed count rather than letting them out.
    """
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,  # own process group, so a timeout can kill runuser's child too
    )
    data = stdin_text.encode("utf-8", errors="replace") if stdin_text is not None else None
    try:
        proc.communicate(input=data, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.wait(timeout=1.0)
        except Exception:
            pass
        raise
    return proc.returncode


# ------------------------------------------------------------------ sessions
def logged_in_sessions(run_root: str = RUN_USER_ROOT,
                       user_name: Callable[[int], Optional[str]] = _user_name,
                       is_socket: Callable[[str], bool] = _is_socket,
                       listdir: Callable[[str], List[str]] = os.listdir) -> List[Session]:
    """Every user with uid >= MIN_UID, a name, and a session bus socket under run_root.

    /run/user/<uid> exists only while that user has at least one session (systemd-logind
    creates it at login and removes it at the last logout), so this is "who is logged in" as
    far as a desktop notification is concerned. Sorted by uid so the order is stable.
    """
    try:
        entries = listdir(run_root)
    except Exception:
        return []
    sessions = []
    for entry in entries:
        if not entry.isdigit():
            continue
        uid = int(entry)
        if uid < MIN_UID:
            continue
        # posixpath: these are Linux paths, and the argv must not depend on where tests run.
        bus = posixpath.join(run_root, entry, "bus")
        if not is_socket(bus):
            continue
        name = user_name(uid)
        # A name with whitespace or a leading dash is not something runuser should be handed;
        # useradd would never make one, so it is skipped rather than quoted.
        if not name or name.startswith("-") or any(c.isspace() for c in name):
            continue
        sessions.append(Session(uid, name, bus))
    sessions.sort(key=lambda s: s.uid)
    return sessions


def _clean(text: str) -> str:
    """argv cannot carry a NUL byte (exec would refuse it), so drop any before building one."""
    return str(text).replace("\x00", "")


def build_notify_argv(runuser: str, env_tool: str, notify_send: str, session: Session,
                      title: str, body: str, critical: bool = False) -> List[str]:
    """The argv that shows one notification in one user's session.

    "--" before the title ends notify-send's own options, so a title that happens to start with
    a dash is shown rather than parsed as a flag.
    """
    run_dir = posixpath.dirname(session.bus)
    argv = [
        runuser, "-u", session.user, "--",
        env_tool,
        f"DBUS_SESSION_BUS_ADDRESS=unix:path={session.bus}",
        f"XDG_RUNTIME_DIR={run_dir}",
        notify_send, f"--app-name={APP_NAME}",
    ]
    if critical:
        argv += ["-u", "critical"]
    argv += ["--", _clean(title), _clean(body)]
    return argv


# ------------------------------------------------------------------ public API
def notify_sessions(title: str, body: str, critical: bool = False, *,
                    run_root: str = RUN_USER_ROOT,
                    runner: Callable[..., int] = _run,
                    which: Callable[[str], Optional[str]] = shutil.which,
                    user_name: Callable[[int], Optional[str]] = _user_name,
                    is_socket: Callable[[str], bool] = _is_socket,
                    listdir: Callable[[str], List[str]] = os.listdir,
                    timeout: float = NOTIFY_TIMEOUT_SEC) -> NotifyReport:
    """Show one notification in every logged-in session and say what happened. Never raises.

    The keyword arguments are test seams; the daemon and the CLI use the defaults.
    """
    try:
        tools = {name: find_tool(name, which) for name in ("runuser", "env", "notify-send")}
        missing = [name for name, path in tools.items() if path is None]
        if missing:
            hint = " (install libnotify-bin)" if "notify-send" in missing else ""
            reason = f"{', '.join(missing)} not found{hint}"
            LOG.debug("desktop notification skipped: %s", reason)
            return NotifyReport([], 0, reason, [])

        sessions = logged_in_sessions(run_root, user_name, is_socket, listdir)
        if not sessions:
            reason = f"no logged-in user with a session bus under {run_root}"
            LOG.debug("desktop notification skipped: %s", reason)
            return NotifyReport([], 0, reason, [])

        # One slot per session: None once that session was reached, otherwise why not. Every
        # slot starts as "still running", so a thread that outlives the join below counts as
        # a failure rather than a reach.
        results: List[Optional[str]] = [f"{s.user}: still running after {timeout:g} s"
                                        for s in sessions]

        def one(index: int, session: Session) -> None:
            argv = build_notify_argv(tools["runuser"], tools["env"], tools["notify-send"],
                                     session, title, body, critical)
            try:
                code = runner(argv, timeout)
                results[index] = None if code == 0 else f"{session.user}: notify-send exited {code}"
            except subprocess.TimeoutExpired:
                results[index] = f"{session.user}: no answer within {timeout:g} s"
            except Exception as ex:
                results[index] = f"{session.user}: {type(ex).__name__}: {ex}"

        threads = [threading.Thread(target=one, args=(i, s), daemon=True, name=f"notify-{s.uid}")
                   for i, s in enumerate(sessions)]
        for t in threads:
            t.start()
        # One shared deadline, so the call is bounded by the timeout however many users there
        # are. The runner already enforces the timeout; the extra second only covers a runner
        # that does not (a test double, or a kill that took a moment).
        deadline = time.monotonic() + timeout + 1.0
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))

        errors = [r for r in results if r is not None]
        for line in errors:
            LOG.debug("desktop notification failed for %s", line)
        reached = len(sessions) - len(errors)
        return NotifyReport(sessions, reached, None, errors)
    except Exception as ex:  # the promise: whatever happened above, the caller gets a report
        LOG.debug("desktop notification failed: %s: %s", type(ex).__name__, ex)
        return NotifyReport([], 0, f"internal error: {type(ex).__name__}: {ex}", [])


def send_desktop_notification(title: str, body: str, critical: bool = False) -> int:
    """How many desktop sessions showed the notification. Never raises.

    critical=True asks for critical urgency, which most desktops keep on screen until it is
    dismissed. The daemon uses it for shutdown and "not protected".
    """
    try:
        return notify_sessions(title, body, critical).reached
    except Exception:
        return 0


def build_wall_argv(wall: str) -> List[str]:
    """wall with no arguments, because the message goes in on stdin.

    Not as an argument: util-linux wall treats a single argument that names an existing file as
    that file, and this runs as root, so a message that happened to be a path would broadcast
    the file instead.
    """
    return [wall]


def send_wall(message: str, *, runner: Callable[..., int] = _run,
              which: Callable[[str], Optional[str]] = shutil.which,
              timeout: float = WALL_TIMEOUT_SEC) -> bool:
    """Broadcast message to every terminal with wall. True when wall exited 0. Never raises."""
    try:
        wall = find_tool("wall", which)
        if wall is None:
            LOG.debug("wall message skipped: wall not found")
            return False
        code = runner(build_wall_argv(wall), timeout, _clean(message) + "\n")
        if code != 0:
            LOG.debug("wall exited %s", code)
        return code == 0
    except Exception as ex:
        LOG.debug("wall message failed: %s: %s", type(ex).__name__, ex)
        return False

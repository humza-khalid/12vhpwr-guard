"""Tests for the supervisor (B-016).

Task Scheduler never restarts a guard that started and then died, so the scheduled task's
process supervises the real guard as a child. These tests drive supervise() with fakes, plus
one run against real child processes so the spawn path itself is exercised.
"""

import sys
import textwrap

import pytest

import hwinfo_12vhpwr_guard as g


class FakeChild:
    def __init__(self, code, runs_for=0.0, clock=None):
        self.code = code
        self.runs_for = runs_for
        self.clock = clock

    def wait(self):
        if self.clock is not None:
            self.clock.now += self.runs_for
        if isinstance(self.code, BaseException):
            raise self.code
        return self.code


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def drive(codes, runs_for=0.0):
    """Run supervise() over a scripted list of child exit codes."""
    clock = Clock()
    spawned, slept, announced = [], [], []
    script = list(codes)

    def spawn(cmd, env):
        spawned.append((cmd, dict(env)))
        code = script.pop(0)
        if isinstance(code, OSError):
            raise code
        return FakeChild(code, runs_for, clock)

    def sleep(seconds):
        slept.append(seconds)
        clock.now += seconds

    result = g.supervise(spawn=spawn, sleep=sleep, clock=clock,
                         announce=lambda code, delay: announced.append((code, delay)),
                         command=["guard", "--child"], shutdown_pending=lambda: False)
    return result, spawned, slept, announced


def test_a_guard_that_dies_during_its_own_shutdown_is_not_restarted(tmp_path, monkeypatch):
    note = tmp_path / "last_shutdown.json"
    note.write_text("{}")
    monkeypatch.setattr(g, "LAST_SHUTDOWN_PATH", str(note))
    spawned = []
    result = g.supervise(spawn=lambda c, e: spawned.append(c) or FakeChild(4294967295),
                         sleep=lambda s: pytest.fail("must not wait to restart"),
                         clock=Clock(), announce=lambda c, d: pytest.fail("must not announce"),
                         command=["x"])
    assert result == 0 and len(spawned) == 1
    assert note.exists(), "the note is left for the next boot to announce"


def test_a_clean_exit_ends_the_supervisor_without_a_restart():
    result, spawned, slept, announced = drive([0])
    assert result == 0
    assert len(spawned) == 1 and slept == [] and announced == []


def test_a_crash_is_restarted_after_the_first_backoff_step():
    result, spawned, slept, announced = drive([1, 0])
    assert result == 0
    assert len(spawned) == 2
    assert slept == [g.SUPERVISOR_BACKOFF_SEC[0]]
    assert announced == [(1, g.SUPERVISOR_BACKOFF_SEC[0])]


def test_a_killed_guard_is_restarted():
    # TerminateProcess(-1) comes back from Popen as this unsigned value on Windows.
    result, spawned, slept, announced = drive([4294967295, 0])
    assert len(spawned) == 2 and announced[0][0] == 4294967295


def test_crashes_in_a_row_back_off_and_cap():
    result, spawned, slept, _ = drive([1] * 7 + [0])
    assert slept == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0]


def test_a_child_that_ran_stably_resets_the_backoff():
    result, spawned, slept, _ = drive([1, 1, 0], runs_for=g.SUPERVISOR_STABLE_SEC)
    assert slept == [5.0, 5.0]


def test_a_planned_restart_is_quick_and_resets_the_crash_count():
    result, spawned, slept, announced = drive([1, 1, g.RESTART_EXIT_CODE, 1, 0])
    assert slept == [5.0, 10.0, g.SUPERVISOR_PLANNED_DELAY_SEC, 5.0]
    assert announced[2] == (g.RESTART_EXIT_CODE, g.SUPERVISOR_PLANNED_DELAY_SEC)


def test_a_restarted_child_is_told_the_previous_exit_code():
    _, spawned, _, _ = drive([g.RESTART_EXIT_CODE, 0])
    assert g.SUPERVISOR_LAST_EXIT_ENV not in spawned[0][1]
    assert spawned[1][1][g.SUPERVISOR_LAST_EXIT_ENV] == str(g.RESTART_EXIT_CODE)


def test_a_failed_spawn_is_retried_with_backoff():
    result, spawned, slept, announced = drive([OSError("no python"), 0])
    assert result == 0 and len(spawned) == 2
    assert announced == [(None, 5.0)]
    assert spawned[1][1][g.SUPERVISOR_LAST_EXIT_ENV] == "-1"


def test_ctrl_c_while_waiting_ends_the_supervisor():
    result, spawned, slept, _ = drive([KeyboardInterrupt()])
    assert result == 0 and slept == []


def test_a_failing_announce_never_stops_the_restart():
    def boom(code, delay):
        raise RuntimeError("toast broke")
    codes = [1, 0]
    result = g.supervise(spawn=lambda c, e: FakeChild(codes.pop(0)), sleep=lambda s: None,
                         clock=Clock(), announce=boom, command=["x"],
                         shutdown_pending=lambda: False)
    assert result == 0 and codes == []


def test_the_child_command_reruns_this_script_with_the_child_flag():
    cmd = g._supervisor_command()
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("hwinfo_12vhpwr_guard.py")
    assert cmd[2] == g.SUPERVISOR_CHILD_FLAG


@pytest.mark.parametrize("argv", [[g.SUPERVISOR_CHILD_FLAG], [g.NO_SUPERVISOR_FLAG]])
def test_entry_runs_the_guard_directly_for_the_child_and_debug_flags(monkeypatch, argv):
    calls = []
    monkeypatch.setattr(g, "main", lambda: calls.append("main"))
    monkeypatch.setattr(g, "supervise", lambda: calls.append("supervise") or 0)
    assert g.entry(argv) == 0
    assert calls == ["main"]


def test_entry_supervises_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(g, "main", lambda: calls.append("main"))
    monkeypatch.setattr(g, "supervise", lambda: calls.append("supervise") or 0)
    assert g.entry([]) == 0
    assert calls == ["supervise"]


def test_an_unplanned_exit_toasts_and_writes_a_warning(monkeypatch):
    toasts, events = [], []
    monkeypatch.setattr(g, "toast", lambda title, msg: toasts.append((title, msg)))
    monkeypatch.setattr(g, "eventlog_write", lambda t, i, m: events.append((t, i, m)))
    monkeypatch.setattr(g, "HAVE_EVENTLOG", True)
    g._announce_restart(1, 5.0)
    assert len(toasts) == 1 and "unexpectedly" in toasts[0][1]
    assert events[0][1] == 9003 and events[0][0] == g.win32con.EVENTLOG_WARNING_TYPE


def test_a_planned_restart_writes_an_info_event_and_no_toast(monkeypatch):
    toasts, events = [], []
    monkeypatch.setattr(g, "toast", lambda title, msg: toasts.append((title, msg)))
    monkeypatch.setattr(g, "eventlog_write", lambda t, i, m: events.append((t, i, m)))
    monkeypatch.setattr(g, "HAVE_EVENTLOG", True)
    g._announce_restart(g.RESTART_EXIT_CODE, 2.0)
    assert toasts == []
    assert events[0][1] == 9003 and events[0][0] == g.win32con.EVENTLOG_INFORMATION_TYPE


def test_the_child_is_told_the_supervisor_pid():
    import os
    _, spawned, _, _ = drive([1, 0])
    assert all(env[g.SUPERVISOR_PID_ENV] == str(os.getpid()) for _, env in spawned)


def test_an_unsupervised_guard_watches_nothing():
    gone = []
    assert g.watch_supervisor(lambda: gone.append(1), pid_text="") is None
    assert g.watch_supervisor(lambda: gone.append(1), pid_text="not-a-pid") is None
    assert gone == []


def test_a_supervisor_that_cannot_be_opened_counts_as_gone():
    gone = []
    t = g.watch_supervisor(lambda: gone.append(1), pid_text="1234",
                           open_process=lambda pid: None, wait=lambda h: pytest.fail("no wait"))
    assert t is None and gone == [1]


def test_the_guard_is_told_when_the_supervisor_ends():
    gone, waited = [], []
    t = g.watch_supervisor(lambda: gone.append(1), pid_text="1234",
                           open_process=lambda pid: "handle", wait=waited.append)
    t.join(5)
    assert waited == ["handle"] and gone == [1]


def test_a_real_supervisor_process_ending_is_noticed():
    import subprocess
    import threading
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    gone = threading.Event()
    try:
        t = g.watch_supervisor(gone.set, pid_text=str(proc.pid))
        assert t is not None
        assert not gone.wait(0.5), "still alive, so nothing yet"
        proc.kill()
        assert gone.wait(10), "the watcher noticed the process ending"
    finally:
        proc.kill()
        proc.wait()


def test_supervisor_gone_asks_for_a_clean_exit_first(monkeypatch):
    import threading
    requested, hard = [], []
    monkeypatch.setattr(g, "request_exit", lambda code, ev: requested.append(code))
    monkeypatch.setattr(g, "SUPERVISOR_GONE_HARD_EXIT_SEC", 0.0)
    monkeypatch.setattr(g.os, "_exit", lambda code: hard.append(code))
    logger = g.logging.getLogger("test-supervisor-gone")
    g._stop_because_supervisor_gone(threading.Event(), logger)
    for _ in range(50):
        if hard:
            break
        g.time.sleep(0.05)
    assert requested == [0] and hard == [0]


def test_main_waits_for_the_monitor_cleanup_before_exiting(monkeypatch):
    """Exit from the tray must not end the process under the monitor's cleanup, which is
    where the GPU limit goes back and the stop is logged."""
    import logging
    import threading
    finished = threading.Event()

    def slow_monitor(stop_event, logger):
        stop_event.wait(5)
        g.time.sleep(0.3)          # the release and the "stopped" line take a moment
        finished.set()

    monkeypatch.setattr(g, "acquire_single_instance", lambda: True)
    monkeypatch.setattr(g, "setup_logger", lambda: logging.getLogger("test-main-exit"))
    monkeypatch.setattr(g, "DialogService", lambda: object())
    monkeypatch.setattr(g, "monitor_loop", slow_monitor)
    monkeypatch.setattr(g, "run_tray", lambda stop_event: None)   # the user chose Exit
    monkeypatch.delenv(g.SUPERVISOR_PID_ENV, raising=False)
    monkeypatch.delenv(g.SUPERVISOR_LAST_EXIT_ENV, raising=False)
    with pytest.raises(SystemExit) as info:
        g.main()
    assert info.value.code == 0
    assert finished.is_set(), "main() returned before the monitor thread finished its cleanup"


def test_real_child_processes_are_restarted_until_a_clean_exit(tmp_path):
    """Exit 3, then 75, then 0, through real subprocesses and the real spawn function."""
    counter = tmp_path / "runs.txt"
    seen = tmp_path / "env.txt"
    child = tmp_path / "child.py"
    child.write_text(textwrap.dedent(f"""
        import os, sys
        counter = {str(counter)!r}
        runs = int(open(counter).read()) if os.path.exists(counter) else 0
        open(counter, "w").write(str(runs + 1))
        with open({str(seen)!r}, "a") as f:
            f.write(os.environ.get({g.SUPERVISOR_LAST_EXIT_ENV!r}, "none") + "\\n")
        sys.exit([3, {g.RESTART_EXIT_CODE}, 0][runs])
    """))
    announced = []
    result = g.supervise(sleep=lambda s: None,
                         announce=lambda code, delay: announced.append(code),
                         command=[sys.executable, str(child)], shutdown_pending=lambda: False)
    assert result == 0
    assert counter.read_text() == "3"
    assert announced == [3, g.RESTART_EXIT_CODE]
    assert seen.read_text().split() == ["none", "3", str(g.RESTART_EXIT_CODE)]

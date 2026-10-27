"""F4: the audit trail for settings the user changes from the tray.

During the 2026-09-04 shutdown test the threshold went from 9.5 A to 6.5 A and the
response mode from tiered to shutdown-only, and the log's first sign of either was
the trip line itself. That reads like the guard fired on its own. These tests pin
the line down: one entry per change that was actually applied, none for a dialog
that was cancelled, a warning that was declined, a value that did not move, or an
autostart change the task refused.

Nothing here touches the real dialogs, Task Scheduler or Event Viewer: the dialog
service, set_autostart and eventlog_write are all stubbed.
"""

import pytest

from helpers import CountingLogger

import hwinfo_12vhpwr_guard as g

PREFIX = "Setting changed by user:"

# The event id test needs the real win32con constant, which only exists when
# pywin32 imported. HAVE_EVENTLOG is forced False by the guard fixture, so the
# module attribute is what says whether the constant is reachable.
HAVE_WIN32CON = getattr(g, "win32con", None) is not None


class FakeDialog:
    """Stands in for DialogService. The test decides what the user clicked.

    ask_float pops from a list, so one test can drive two dialogs in a row;
    returning None is the user cancelling. confirm answers both the unsafe-value
    warning and the reset confirmation.
    """

    def __init__(self, floats=(), confirm=True):
        self.floats = list(floats)
        self._confirm = confirm
        self.errors = []

    def ask_float(self, _title, _prompt, initial):
        return self.floats.pop(0) if self.floats else initial

    def confirm(self, _title, _msg):
        return self._confirm

    def show_error(self, title, msg):
        self.errors.append((title, msg))


@pytest.fixture
def audit(guard, monkeypatch):
    """The guard with a recording logger installed and a fake dialog service.

    Config starts at the documented defaults so every test knows what it is moving
    away from, whatever ran before it.
    """
    log = CountingLogger()
    monkeypatch.setattr(g, "LOGGER", log)
    g.set_config_values(g.DEFAULT_THRESHOLD_AMPS, g.DEFAULT_SUSTAINED_SECONDS_REQUIRED)
    g.set_response_mode(g.DEFAULT_RESPONSE_MODE)
    guard.log = log
    guard.set_dialog = lambda d: monkeypatch.setattr(g, "dialog", d)
    return guard


def lines(log):
    """Every setting-change line, in order."""
    return [m for m in log.records["info"] if m.startswith(PREFIX)]


# =========================================================================
# Threshold: the setting the incident turned on
# =========================================================================
def test_f4_1_threshold_changed(audit):
    """F4.1: a real threshold change logs exactly one line with both values."""
    audit.set_dialog(FakeDialog(floats=[6.5]))

    g.change_threshold_amps(None, None)

    assert lines(audit.log) == [f"{PREFIX} threshold 9.50 A -> 6.50 A"]
    assert g.get_config_values()[0] == 6.5, "the change still applies"


def test_f4_2_threshold_cancelled(audit):
    """F4.2: cancelling the dialog changes nothing, so it logs nothing."""
    audit.set_dialog(FakeDialog(floats=[None]))

    g.change_threshold_amps(None, None)

    assert lines(audit.log) == [], "cancelled dialog leaves no audit line"
    assert g.get_config_values()[0] == g.DEFAULT_THRESHOLD_AMPS


def test_f4_3_threshold_unchanged(audit):
    """F4.3: re-entering the current value is not a change."""
    audit.set_dialog(FakeDialog(floats=[g.DEFAULT_THRESHOLD_AMPS]))

    g.change_threshold_amps(None, None)

    assert lines(audit.log) == [], "same value in, no line out"


def test_f4_4_threshold_unsafe_declined(audit):
    """F4.4: declining the reduced-protection warning aborts before the change."""
    audit.set_dialog(FakeDialog(floats=[12.0], confirm=False))

    g.change_threshold_amps(None, None)

    assert lines(audit.log) == [], "declined warning leaves no audit line"
    assert g.get_config_values()[0] == g.DEFAULT_THRESHOLD_AMPS, "and no change"


def test_f4_5_threshold_unsafe_accepted(audit):
    """F4.5: accepting the warning is still a change, and still logged."""
    audit.set_dialog(FakeDialog(floats=[12.0], confirm=True))

    g.change_threshold_amps(None, None)

    assert lines(audit.log) == [f"{PREFIX} threshold 9.50 A -> 12.00 A"]


# =========================================================================
# Sustained seconds, reset, response mode
# =========================================================================
def test_f4_6_sustained_changed(audit):
    """F4.6: sustained seconds logs in seconds, not amps."""
    audit.set_dialog(FakeDialog(floats=[10.0]))

    g.change_sustained_seconds(None, None)

    assert lines(audit.log) == [f"{PREFIX} sustained seconds 15.0 s -> 10.0 s"]


def test_f4_7_sustained_cancelled(audit):
    """F4.7: cancel and same-value are silent here too."""
    audit.set_dialog(FakeDialog(floats=[None]))
    g.change_sustained_seconds(None, None)
    assert lines(audit.log) == []

    audit.set_dialog(FakeDialog(floats=[g.DEFAULT_SUSTAINED_SECONDS_REQUIRED]))
    g.change_sustained_seconds(None, None)
    assert lines(audit.log) == []


def test_f4_8_reset_logs_only_what_moved(audit):
    """F4.8: reset from two non-default values logs one line each."""
    audit.set_dialog(FakeDialog(confirm=True))
    g.set_config_values(6.5, 10.0)

    g.reset_to_defaults(None, None)

    assert lines(audit.log) == [
        f"{PREFIX} threshold 6.50 A -> 9.50 A",
        f"{PREFIX} sustained seconds 10.0 s -> 15.0 s",
    ]
    assert g.get_config_values() == (9.5, 15.0)


def test_f4_9_reset_partial_and_from_defaults(audit):
    """F4.9: only the value that moved is logged, and a no-op reset is silent."""
    audit.set_dialog(FakeDialog(confirm=True))
    g.set_config_values(6.5, g.DEFAULT_SUSTAINED_SECONDS_REQUIRED)

    g.reset_to_defaults(None, None)
    assert lines(audit.log) == [f"{PREFIX} threshold 6.50 A -> 9.50 A"]

    # Already at the defaults now, so a second reset changes nothing.
    audit.log.records["info"].clear()
    g.reset_to_defaults(None, None)
    assert lines(audit.log) == [], "reset from defaults logs nothing"


def test_f4_10_reset_cancelled(audit):
    """F4.10: declining the reset confirmation logs nothing and changes nothing."""
    audit.set_dialog(FakeDialog(confirm=False))
    g.set_config_values(6.5, 10.0)

    g.reset_to_defaults(None, None)

    assert lines(audit.log) == []
    assert g.get_config_values() == (6.5, 10.0)


def test_f4_11_response_mode(audit):
    """F4.11: a mode switch logs once, re-picking the same mode logs nothing."""
    g.apply_response_mode(g.RESPONSE_SHUTDOWN_ONLY)
    assert lines(audit.log) == [f"{PREFIX} response mode tiered -> shutdown_only"]
    assert g.get_response_mode() == g.RESPONSE_SHUTDOWN_ONLY

    g.apply_response_mode(g.RESPONSE_SHUTDOWN_ONLY)
    assert len(lines(audit.log)) == 1, "same mode again is not a change"

    g.apply_response_mode(g.RESPONSE_TIERED)
    assert lines(audit.log)[-1] == f"{PREFIX} response mode shutdown_only -> tiered"


# =========================================================================
# Pause and autostart
# =========================================================================
def test_f4_12_toggle_pause_both_ways(audit):
    """F4.12: both edges of the pause switch are on the record."""
    g.set_state(paused=False)

    g.toggle_pause(None, None)
    g.toggle_pause(None, None)

    assert lines(audit.log) == [
        f"{PREFIX} monitoring running -> paused",
        f"{PREFIX} monitoring paused -> running",
    ]
    assert g.get_state()["paused"] is False


def test_f4_13_autostart_applied(audit, monkeypatch):
    """F4.13: an autostart change the task accepted is logged."""
    monkeypatch.setattr(g, "autostart_state", lambda force=False: True)
    monkeypatch.setattr(g, "set_autostart", lambda enable: True)

    g.toggle_autostart(None, None)

    assert lines(audit.log) == [f"{PREFIX} autostart on -> off"]


def test_f4_14_autostart_refused(audit, monkeypatch):
    """F4.14: a declined UAC prompt or failed schtasks call changes nothing."""
    dlg = FakeDialog()
    audit.set_dialog(dlg)
    monkeypatch.setattr(g, "autostart_state", lambda force=False: False)
    monkeypatch.setattr(g, "set_autostart", lambda enable: False)

    g.toggle_autostart(None, None)

    assert lines(audit.log) == [], "nothing changed, so nothing is logged"
    assert dlg.errors, "the existing failure dialog still fires"


def test_f4_15_autostart_task_missing(audit, monkeypatch):
    """F4.15: no logon task at all is not a setting change."""
    dlg = FakeDialog()
    audit.set_dialog(dlg)
    monkeypatch.setattr(g, "autostart_state", lambda force=False: None)
    monkeypatch.setattr(
        g, "set_autostart",
        lambda enable: pytest.fail("set_autostart must not run without a task"),
    )

    g.toggle_autostart(None, None)

    assert lines(audit.log) == []
    assert dlg.errors, "the existing not-installed dialog still fires"


# =========================================================================
# Event Viewer and the None logger
# =========================================================================
@pytest.mark.skipif(not HAVE_WIN32CON, reason="pywin32 not installed")
def test_f4_16_event_1010(audit, monkeypatch):
    """F4.16: one Event Viewer entry per change, id 1010, same text as the log."""
    events = []
    monkeypatch.setattr(g, "HAVE_EVENTLOG", True)
    monkeypatch.setattr(
        g, "eventlog_write",
        lambda event_type, event_id, message: events.append((event_type, event_id, message)),
    )
    audit.set_dialog(FakeDialog(floats=[6.5]))

    g.change_threshold_amps(None, None)

    assert len(events) == 1, f"exactly one event: {events}"
    event_type, event_id, message = events[0]
    assert event_id == 1010
    assert event_type == g.win32con.EVENTLOG_INFORMATION_TYPE, "informational, not a fault"
    assert message == lines(audit.log)[0], "Event Viewer and the log say the same thing"


def test_f4_17_no_event_when_nothing_changed(audit, monkeypatch):
    """F4.17: the cancel paths stay out of Event Viewer as well as the log."""
    events = []
    monkeypatch.setattr(g, "HAVE_EVENTLOG", True)
    monkeypatch.setattr(
        g, "eventlog_write",
        lambda event_type, event_id, message: events.append(event_id),
    )
    audit.set_dialog(FakeDialog(floats=[None]))

    g.change_threshold_amps(None, None)

    assert events == []


def test_f4_18_no_logger_does_not_raise(guard, monkeypatch):
    """F4.18: a callback firing before main() published LOGGER must not blow up.

    Uses the raw guard fixture, not audit: the point is that LOGGER is None.
    """
    monkeypatch.setattr(g, "LOGGER", None)
    monkeypatch.setattr(g, "HAVE_EVENTLOG", False)

    g.log_setting_change("threshold", "9.50 A", "6.50 A")

    monkeypatch.setattr(g, "dialog", FakeDialog(floats=[6.5]))
    g.change_threshold_amps(None, None)
    assert g.get_config_values()[0] == 6.5, "the change still applies without a logger"

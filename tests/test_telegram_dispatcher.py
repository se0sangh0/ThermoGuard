from types import SimpleNamespace

from thermal_monitoring.analysis import notifier
from thermal_monitoring.analysis.threshold import Status
from thermal_monitoring.tools import telegram_dispatcher
from thermal_monitoring.tools.telegram_dispatcher import TelegramDispatcher


class _ImmediateRoot:
    @staticmethod
    def after(_delay, callback):
        callback()


def _make_dispatcher():
    dashboard = SimpleNamespace(
        lifecycle="running",
        root=_ImmediateRoot(),
        cfg=SimpleNamespace(
            identity=SimpleNamespace(robot_id="Robot-01"),
        ),
        _add_operating_log=lambda *_args: None,
    )
    return TelegramDispatcher(dashboard)


def test_failed_critical_alarm_retries_after_backoff(monkeypatch):
    dispatcher = _make_dispatcher()
    now = [0.0]
    attempts = []
    monkeypatch.setattr(
        notifier,
        "get_settings",
        lambda: {"configured": True, "enabled": True},
    )
    monkeypatch.setattr(telegram_dispatcher.time, "monotonic", lambda: now[0])

    def fail_dispatch(result, captured_at):
        attempts.append((result, captured_at))
        dispatcher._complete_dispatch(False, captured_at)

    dispatcher._dispatch = fail_dispatch
    initial = {
        "base": "capture-1",
        "alarm": True,
        "alarm_status": Status.CRITICAL,
        "status": Status.CRITICAL,
        "max_temp": 70.0,
        "roi_name": "ROI-1",
    }
    continuing = {
        **initial,
        "alarm": False,
    }

    dispatcher.maybe_dispatch(initial, True, "capture-1")
    now[0] = 30.0
    dispatcher.maybe_dispatch(continuing, True, "capture-2")
    now[0] = 61.0
    dispatcher.maybe_dispatch(continuing, True, "capture-3")

    assert [captured_at for _, captured_at in attempts] == [
        "capture-1",
        "capture-1",
    ]


def test_successful_retry_clears_pending_alarm(monkeypatch):
    dispatcher = _make_dispatcher()
    now = [0.0]
    attempts = []
    monkeypatch.setattr(
        notifier,
        "get_settings",
        lambda: {"configured": True, "enabled": True},
    )
    monkeypatch.setattr(telegram_dispatcher.time, "monotonic", lambda: now[0])

    def dispatch(result, captured_at):
        attempts.append(captured_at)
        dispatcher._complete_dispatch(len(attempts) == 2, captured_at)

    dispatcher._dispatch = dispatch
    initial = {
        "base": "capture-1",
        "alarm": True,
        "alarm_status": Status.CRITICAL,
        "status": Status.CRITICAL,
        "max_temp": 70.0,
        "roi_name": "ROI-1",
    }
    continuing = {**initial, "alarm": False}

    dispatcher.maybe_dispatch(initial, True, "capture-1")
    now[0] = 61.0
    dispatcher.maybe_dispatch(continuing, True, "capture-2")
    now[0] = 122.0
    dispatcher.maybe_dispatch(continuing, True, "capture-3")

    assert attempts == ["capture-1", "capture-1"]
    assert dispatcher._pending_result is None
    assert dispatcher._last_telegram_capture == "capture-1"


def test_recovery_cancels_pending_alarm(monkeypatch):
    dispatcher = _make_dispatcher()
    monkeypatch.setattr(
        notifier,
        "get_settings",
        lambda: {"configured": True, "enabled": False},
    )
    critical = {
        "alarm": True,
        "alarm_status": Status.CRITICAL,
        "status": Status.CRITICAL,
        "max_temp": 70.0,
    }
    normal = {
        "alarm": False,
        "alarm_status": Status.NORMAL,
        "status": Status.NORMAL,
        "max_temp": 35.0,
    }

    dispatcher.maybe_dispatch(critical, True, "capture-1")
    assert dispatcher._pending_result is critical

    dispatcher.maybe_dispatch(normal, True, "capture-2")

    assert dispatcher._pending_result is None


def test_valid_frame_replaces_unattempted_bad_trigger(monkeypatch):
    dispatcher = _make_dispatcher()
    attempts = []
    monkeypatch.setattr(
        notifier,
        "get_settings",
        lambda: {"configured": True, "enabled": True},
    )

    def dispatch(result, captured_at):
        attempts.append((result["base"], captured_at))
        dispatcher._complete_dispatch(True, captured_at)

    dispatcher._dispatch = dispatch
    bad = {
        "base": "bad",
        "alarm": True,
        "alarm_status": Status.CRITICAL,
        "status": Status.CRITICAL,
        "max_temp": 70.0,
        "image_quality_reason": "corrupt",
    }
    good = {
        **bad,
        "base": "good",
        "alarm": False,
    }

    dispatcher.maybe_dispatch(bad, False, "capture-1")
    dispatcher.maybe_dispatch(good, True, "capture-2")

    assert attempts == [("good", "capture-2")]


def test_warning_frame_does_not_replace_bad_critical_trigger(monkeypatch):
    dispatcher = _make_dispatcher()
    attempts = []
    monkeypatch.setattr(
        notifier,
        "get_settings",
        lambda: {"configured": True, "enabled": True},
    )
    dispatcher._dispatch = lambda result, captured_at: attempts.append(
        (result["base"], captured_at)
    )
    bad_critical = {
        "base": "bad-critical",
        "alarm": True,
        "alarm_status": Status.CRITICAL,
        "status": Status.CRITICAL,
        "max_temp": 70.0,
        "image_quality_reason": "corrupt",
    }
    warning = {
        "base": "warning",
        "alarm": False,
        "alarm_status": Status.WARNING,
        "status": Status.WARNING,
        "max_temp": 52.0,
    }

    dispatcher.maybe_dispatch(bad_critical, False, "capture-1")
    dispatcher.maybe_dispatch(warning, True, "capture-2")

    assert attempts == []
    assert dispatcher._pending_result is bad_critical
    assert dispatcher._pending_quality_ok is False


def test_synchronous_dispatch_start_failure_remains_retryable(monkeypatch):
    dispatcher = _make_dispatcher()
    now = [0.0]
    attempts = []
    monkeypatch.setattr(
        notifier,
        "get_settings",
        lambda: {"configured": True, "enabled": True},
    )
    monkeypatch.setattr(telegram_dispatcher.time, "monotonic", lambda: now[0])

    def dispatch(_result, _captured_at):
        attempts.append("attempt")
        raise RuntimeError("thread unavailable")

    dispatcher._dispatch = dispatch
    result = {
        "base": "capture-1",
        "alarm": True,
        "alarm_status": Status.CRITICAL,
        "status": Status.CRITICAL,
        "max_temp": 70.0,
    }

    dispatcher.maybe_dispatch(result, True, "capture-1")
    now[0] = 61.0
    dispatcher.maybe_dispatch({**result, "alarm": False}, True, "capture-2")

    assert attempts == ["attempt", "attempt"]
    assert dispatcher._dispatch_inflight is False


class _ImmediateThread:
    def __init__(self, target=None, daemon=None):
        self.target = target

    def start(self):
        if self.target:
            self.target()


def test_dispatch_passes_backend_url_from_cfg(monkeypatch):
    dispatcher = _make_dispatcher()
    dispatcher._dash.cfg.backend = SimpleNamespace(
        url="http://dashboard-backend.local:8000"
    )
    captured_calls = []

    def fake_send_alarm(*args, **kwargs):
        captured_calls.append(kwargs)
        return True

    monkeypatch.setattr(notifier, "send_alarm", fake_send_alarm)
    monkeypatch.setattr(
        telegram_dispatcher.threading,
        "Thread",
        _ImmediateThread,
    )

    dispatcher._dispatch(
        {
            "max_temp": 80.0,
            "status": Status.CRITICAL,
            "base": "cap-1",
        },
        "capture-1",
    )

    assert len(captured_calls) == 1
    assert captured_calls[0]["backend_url"] == (
        "http://dashboard-backend.local:8000"
    )


def test_dispatch_passes_none_when_backend_attr_absent(monkeypatch):
    dispatcher = _make_dispatcher()
    captured_calls = []

    def fake_send_alarm(*args, **kwargs):
        captured_calls.append(kwargs)
        return True

    monkeypatch.setattr(notifier, "send_alarm", fake_send_alarm)
    monkeypatch.setattr(
        telegram_dispatcher.threading,
        "Thread",
        _ImmediateThread,
    )

    dispatcher._dispatch(
        {
            "max_temp": 80.0,
            "status": Status.CRITICAL,
            "base": "cap-2",
        },
        "capture-2",
    )

    assert len(captured_calls) == 1
    assert captured_calls[0]["backend_url"] is None

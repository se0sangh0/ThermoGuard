from types import SimpleNamespace

from thermal_monitoring.analysis import notifier
from thermal_monitoring.analysis.threshold import Status
from thermal_monitoring.tools.telegram_dispatcher import TelegramDispatcher


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise notifier.requests.HTTPError(f"HTTP {self.status_code}")


class _ImmediateRoot:
    @staticmethod
    def after(_delay, callback):
        callback()


def _dispatcher(logs):
    dashboard = SimpleNamespace(
        lifecycle="running",
        root=_ImmediateRoot(),
        _add_operating_log=lambda *args: logs.append(args),
    )
    return TelegramDispatcher(dashboard)


def test_configure_toggle_and_logout_persist_local_env(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("KEEP_ME=value\n", encoding="utf-8")
    monkeypatch.setattr(notifier, "DOTENV_PATH", env_path)
    monkeypatch.setattr(notifier, "BOT_TOKEN", "")
    monkeypatch.setattr(notifier, "CHAT_ID", "")
    monkeypatch.setattr(notifier, "TELEGRAM_ENABLED", False)

    notifier.configure("test-token", "-100123", enabled=False, persist=True)
    assert notifier.is_configured()
    assert not notifier.is_enabled()
    assert "BOT_TOKEN=test-token" in env_path.read_text(encoding="utf-8")

    notifier.set_enabled(True, persist=True)
    assert notifier.is_enabled()
    assert "TELEGRAM_ENABLED=true" in env_path.read_text(encoding="utf-8")

    notifier.logout(persist=True)
    saved = env_path.read_text(encoding="utf-8")
    assert not notifier.is_configured()
    assert "BOT_TOKEN=" not in saved
    assert "CHAT_ID=" not in saved
    assert "TELEGRAM_ENABLED=false" in saved
    assert "KEEP_ME=value" in saved


def test_connection_validates_bot_and_chat(monkeypatch):
    responses = iter([
        _Response(payload={"ok": True, "result": {"username": "thermo_test_bot"}}),
        _Response(payload={"ok": True, "result": {"id": -100123}}),
    ])
    monkeypatch.setattr(notifier.requests, "get", lambda *args, **kwargs: next(responses))

    connected, detail = notifier.test_connection("token", "-100123")

    assert connected
    assert "@thermo_test_bot" in detail


def test_send_alarm_does_not_call_api_when_delivery_disabled(monkeypatch):
    monkeypatch.setattr(notifier, "BOT_TOKEN", "token")
    monkeypatch.setattr(notifier, "CHAT_ID", "-100123")
    monkeypatch.setattr(notifier, "TELEGRAM_ENABLED", False)
    monkeypatch.setattr(
        notifier.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("API called")),
    )

    assert notifier.send_alarm("", 55.0, "Warning", "Robot-01") is False


def test_dashboard_does_not_dispatch_warning(monkeypatch):
    dispatched = []
    dispatcher = _dispatcher([])
    dispatcher._dispatch = (
        lambda result, captured_at: dispatched.append((result, captured_at))
    )
    monkeypatch.setattr(
        notifier,
        "get_settings",
        lambda: {"configured": True, "enabled": True},
    )
    result = {
        "alarm": False,
        "status": Status.WARNING,
        "max_temp": 52.0,
        "overall_max_roi_name": "ROI-01",
    }

    dispatcher.maybe_dispatch(
        result,
        quality_ok=True,
        captured_at="capture-1",
    )

    assert dispatched == []


def test_dashboard_does_not_dispatch_when_delivery_is_disabled(monkeypatch):
    logs = []
    dispatcher = _dispatcher(logs)
    dispatcher._dispatch = lambda *_args: (_ for _ in ()).throw(
        AssertionError("dispatch called")
    )
    monkeypatch.setattr(
        notifier,
        "get_settings",
        lambda: {"configured": True, "enabled": False},
    )

    dispatcher.maybe_dispatch(
        {"alarm": True, "status": Status.CRITICAL, "max_temp": 70.0},
        quality_ok=True,
        captured_at="capture-2",
    )

    assert any("비활성화" in detail for _, _, detail in logs)


def test_save_delivery_result_uses_explicit_backend_url(monkeypatch):
    posted = []

    def fake_post(url, json=None, timeout=None):
        posted.append((url, json))
        return _Response(200, {"status": "created", "delivery_id": 100})

    monkeypatch.setattr(notifier.requests, "post", fake_post)

    result = notifier.save_delivery_result(
        alert_id=10,
        success=True,
        backend_url="http://custom-backend.local:8000///",
    )

    assert result is True
    assert posted[0][0] == (
        "http://custom-backend.local:8000/api/notification-deliveries"
    )
    assert posted[0][1]["alert_id"] == 10
    assert posted[0][1]["delivery_status"] == "success"


def test_save_delivery_result_falls_back_to_fastapi_url(monkeypatch):
    posted = []

    def fake_post(url, json=None, timeout=None):
        posted.append(url)
        return _Response(200, {"status": "created", "delivery_id": 101})

    monkeypatch.setattr(
        notifier,
        "FASTAPI_URL",
        "http://default-fastapi.local:5000/",
    )
    monkeypatch.setattr(notifier.requests, "post", fake_post)

    result = notifier.save_delivery_result(
        alert_id=11,
        success=False,
        error_message="Telegram timeout",
        backend_url=None,
    )

    assert result is True
    assert posted == [
        "http://default-fastapi.local:5000/api/notification-deliveries"
    ]


def test_send_alarm_keeps_telegram_result_separate_from_delivery_save(
    monkeypatch,
):
    monkeypatch.setattr(notifier, "BOT_TOKEN", "valid-token")
    monkeypatch.setattr(notifier, "CHAT_ID", "12345")
    monkeypatch.setattr(notifier, "TELEGRAM_ENABLED", True)

    def fake_post(url, data=None, timeout=None, **kwargs):
        assert "sendMessage" in url
        return _Response(200, {"ok": True})

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    save_calls = []

    def fake_save_delivery_result(*args, **kwargs):
        save_calls.append(kwargs)
        return False

    monkeypatch.setattr(
        notifier,
        "save_delivery_result",
        fake_save_delivery_result,
    )

    without_alert = notifier.send_alarm(
        image_path="",
        temp=50.0,
        status="Critical",
        alert_id=None,
    )
    with_alert = notifier.send_alarm(
        image_path="",
        temp=60.0,
        status="Critical",
        alert_id=99,
        backend_url="http://explicit-backend.local:8000/",
    )

    assert without_alert is True
    assert with_alert is True
    assert len(save_calls) == 1
    assert save_calls[0]["alert_id"] == 99
    assert save_calls[0]["backend_url"] == (
        "http://explicit-backend.local:8000/"
    )

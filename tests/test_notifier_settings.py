from thermal_monitoring.analysis import notifier
from thermal_monitoring.analysis.threshold import Status
from thermal_monitoring.tools.product_dashboard import ProductDashboard


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


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


def test_dashboard_dispatches_first_warning_transition(monkeypatch):
    dashboard = ProductDashboard.__new__(ProductDashboard)
    dashboard._last_telegram_capture = None
    dashboard._add_operating_log = lambda *args: None
    dispatched = []
    dashboard._dispatch_telegram = lambda result: dispatched.append(result)
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

    dashboard._maybe_dispatch_telegram(
        result,
        quality_ok=True,
        captured_at="capture-1",
        warning_transition=True,
    )

    assert dispatched == [result]


def test_dashboard_does_not_dispatch_when_delivery_is_disabled(monkeypatch):
    dashboard = ProductDashboard.__new__(ProductDashboard)
    dashboard._last_telegram_capture = None
    logs = []
    dashboard._add_operating_log = lambda *args: logs.append(args)
    dashboard._dispatch_telegram = lambda result: (_ for _ in ()).throw(
        AssertionError("dispatch called")
    )
    monkeypatch.setattr(
        notifier,
        "get_settings",
        lambda: {"configured": True, "enabled": False},
    )

    dashboard._maybe_dispatch_telegram(
        {"alarm": True, "status": Status.CRITICAL, "max_temp": 70.0},
        quality_ok=True,
        captured_at="capture-2",
    )

    assert any("비활성화" in detail for _, _, detail in logs)

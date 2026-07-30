from thermal_monitoring.tools import threshold_api_client


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _sync():
    return threshold_api_client.sync_threshold_profiles(
        base_url="http://127.0.0.1:8000",
        timeout=5.0,
        camera_id=7,
        roi_ids=[12],
        baseline_temp=35.0,
        warning_delta=15.0,
        critical_delta=25.0,
        min_hotspot_size=3,
        min_hotspot_size_max=10,
        alarm_cooldown_sec=600.0,
    )


def test_sync_threshold_profiles_creates_exact_roi_profile(monkeypatch):
    calls = []
    responses = iter([
        {"thresholds": []},
        {"status": "created", "threshold_id": 31},
    ])

    def fake_request(method, url, timeout, **kwargs):
        calls.append((method, url, kwargs.get("json"), timeout))
        return FakeResponse(next(responses))

    monkeypatch.setattr(
        threshold_api_client.requests,
        "request",
        fake_request,
    )

    result = _sync()

    assert result.created == 1
    assert result.updated == 0
    assert result.roi_ids == (12,)
    assert calls[1][0] == "POST"
    assert calls[1][2] == {
        "camera_id": 7,
        "roi_id": 12,
        "baseline_temp": 35.0,
        "warning_delta": 15.0,
        "critical_delta": 25.0,
        "min_hotspot_size": 3,
        "min_hotspot_size_max": 10,
        "alarm_cooldown_sec": 600,
    }


def test_sync_threshold_profiles_updates_latest_exact_roi_profile(monkeypatch):
    calls = []
    responses = iter([
        {
            "thresholds": [
                {
                    "threshold_id": 30,
                    "camera_id": 7,
                    "roi_id": 12,
                    "valid_to": None,
                },
                {
                    "threshold_id": 31,
                    "camera_id": 7,
                    "roi_id": 12,
                    "valid_to": None,
                },
                {
                    "threshold_id": 32,
                    "camera_id": 7,
                    "roi_id": None,
                    "valid_to": None,
                },
            ]
        },
        {"status": "updated", "threshold_id": 31},
    ])

    def fake_request(method, url, timeout, **kwargs):
        calls.append((method, url, kwargs.get("json"), timeout))
        return FakeResponse(next(responses))

    monkeypatch.setattr(
        threshold_api_client.requests,
        "request",
        fake_request,
    )

    result = _sync()

    assert result.created == 0
    assert result.updated == 1
    assert calls[1][0] == "PATCH"
    assert calls[1][1].endswith("/api/thresholds/31")


def test_sync_threshold_profiles_surfaces_backend_error(monkeypatch):
    monkeypatch.setattr(
        threshold_api_client.requests,
        "request",
        lambda *_args, **_kwargs: FakeResponse({
            "status": "error",
            "error": "threshold table rejected the row",
        }),
    )

    try:
        _sync()
    except threshold_api_client.ThresholdApiError as exc:
        assert "rejected" in str(exc)
    else:
        raise AssertionError("Backend status=error must raise ThresholdApiError")

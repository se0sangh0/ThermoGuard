from thermal_monitoring.tools import asset_api_client


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_register_asset_hierarchy_uses_existing_api_order(monkeypatch):
    calls = []
    responses = iter([
        {"cameras": []},
        {"status": "created", "factory_id": 1},
        {"status": "created", "line_id": 2},
        {"status": "created", "robot_id": 3},
        {"status": "created", "camera_id": 4},
    ])

    def fake_request(method, url, timeout, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        return FakeResponse(next(responses))

    monkeypatch.setattr(asset_api_client.requests, "request", fake_request)
    result = asset_api_client.register_asset_hierarchy(
        base_url="http://127.0.0.1:8000",
        timeout=5,
        factory_name="1공장",
        line_name="조립라인 A",
        robot_code="Robot-01",
        robot_name="조립로봇",
        camera_code="CAM-01",
        camera_ip="192.168.0.51",
    )

    assert result == asset_api_client.AssetRegistration(1, 2, 3, 4)
    assert [call[1].rsplit("/", 1)[-1] for call in calls] == [
        "cameras", "factories", "production-lines", "robots", "cameras",
    ]
    assert calls[2][2]["factory_id"] == 1
    assert calls[3][2]["line_id"] == 2
    assert calls[4][2]["robot_id"] == 3
    assert calls[4][2]["capture_mode"] == "both"
    assert calls[4][2]["normal_interval_sec"] == 30.0
    assert calls[4][2]["warning_interval_sec"] == 5.0


def test_camera_asset_payload_uses_runtime_capture_contract(monkeypatch):
    calls = []
    responses = iter([
        {"cameras": []},
        {"status": "created", "factory_id": 1},
        {"status": "created", "line_id": 2},
        {"status": "created", "robot_id": 3},
        {"status": "created", "camera_id": 4},
    ])

    def fake_request(method, url, timeout, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        return FakeResponse(next(responses))

    monkeypatch.setattr(asset_api_client.requests, "request", fake_request)
    asset_api_client.register_asset_hierarchy(
        base_url="http://127.0.0.1:8000",
        timeout=5,
        factory_name="1공장",
        line_name="조립라인 A",
        robot_code="Robot-01",
        robot_name="조립로봇",
        camera_code="CAM-01",
        camera_ip="192.168.0.51",
        capture_mode="thermal",
        normal_interval_sec=45.0,
        warning_interval_sec=3.0,
    )

    camera_payload = calls[-1][2]
    assert camera_payload["capture_mode"] == "thermal"
    assert camera_payload["normal_interval_sec"] == 45.0
    assert camera_payload["warning_interval_sec"] == 3.0


def test_register_asset_hierarchy_does_not_reuse_unmatched_only_camera(monkeypatch):
    calls = []
    responses = iter([
        {"cameras": [{
            "robot_id": 3,
            "camera_id": 4,
            "camera_code": "OLD-CAM",
            "ip_address": "192.168.0.10",
        }]},
        {"status": "created", "factory_id": 11},
        {"status": "created", "line_id": 12},
        {"status": "created", "robot_id": 13},
        {"status": "created", "camera_id": 14},
    ])

    def fake_request(method, url, timeout, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        return FakeResponse(next(responses))

    monkeypatch.setattr(asset_api_client.requests, "request", fake_request)
    result = asset_api_client.register_asset_hierarchy(
        base_url="http://127.0.0.1:8000",
        timeout=5,
        factory_name="변경된 공장명",
        line_name="변경된 라인명",
        robot_code="Robot-01",
        robot_name="변경된 로봇명",
        camera_code="CAM-01",
        camera_ip="192.168.0.51",
    )

    assert result == asset_api_client.AssetRegistration(11, 12, 13, 14)
    assert [call[0] for call in calls] == ["GET", "POST", "POST", "POST", "POST"]


def test_register_asset_hierarchy_rejects_conflicting_code_and_ip_matches(monkeypatch):
    monkeypatch.setattr(
        asset_api_client.requests,
        "request",
        lambda *_args, **_kwargs: FakeResponse({
            "cameras": [
                {"camera_id": 4, "robot_id": 3, "camera_code": "CAM-01",
                 "ip_address": "192.168.0.10"},
                {"camera_id": 5, "robot_id": 3, "camera_code": "CAM-02",
                 "ip_address": "192.168.0.51"},
            ]
        }),
    )

    try:
        asset_api_client.register_asset_hierarchy(
            base_url="http://127.0.0.1:8000",
            timeout=5,
            factory_name="1공장",
            line_name="조립라인 A",
            robot_code="Robot-01",
            robot_name="조립로봇",
            camera_code="CAM-01",
            camera_ip="192.168.0.51",
        )
    except asset_api_client.AssetApiError as exc:
        assert "서로 다른" in str(exc)
    else:
        raise AssertionError("conflicting camera identities must fail closed")


def test_register_asset_hierarchy_reuses_saved_ids_and_refreshes_camera_policy(monkeypatch):
    calls = []

    def request(method, url, timeout, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        return FakeResponse({"status": "created", "camera_id": 4, "existing": True})

    monkeypatch.setattr(asset_api_client.requests, "request", request)
    result = asset_api_client.register_asset_hierarchy(
        base_url="http://127.0.0.1:8000",
        timeout=5,
        factory_name="1공장",
        line_name="조립라인 A",
        robot_code="Robot-01",
        robot_name="조립로봇",
        camera_code="CAM-01",
        camera_ip="192.168.0.51",
        factory_id=1,
        line_id=2,
        robot_id=3,
        camera_id=4,
    )

    assert result == asset_api_client.AssetRegistration(1, 2, 3, 4)
    assert len(calls) == 1
    assert calls[0][0] == "POST"
    assert calls[0][1].endswith("/api/cameras")
    assert calls[0][2]["normal_interval_sec"] == 30.0
    assert calls[0][2]["warning_interval_sec"] == 5.0

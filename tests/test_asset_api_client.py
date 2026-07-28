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
        {"status": "created", "factory_id": 1},
        {"status": "created", "line_id": 2},
        {"status": "created", "robot_id": 3},
        {"cameras": []},
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
        "factories", "production-lines", "robots", "cameras", "cameras",
    ]
    assert calls[1][2]["factory_id"] == 1
    assert calls[2][2]["line_id"] == 2
    assert calls[4][2]["robot_id"] == 3


def test_register_asset_hierarchy_reuses_saved_database_ids(monkeypatch):
    def unexpected_request(*_args, **_kwargs):
        raise AssertionError("saved IDs must not create duplicate rows")

    monkeypatch.setattr(asset_api_client.requests, "request", unexpected_request)
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

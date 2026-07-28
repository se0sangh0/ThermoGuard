from types import SimpleNamespace

from thermal_monitoring.tools import roi_api_client


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_sync_rois_posts_dragged_thermal_coordinates(monkeypatch):
    get_payloads = iter([
        {"cameras": [{"camera_id": 7, "camera_code": "CAM-01",
                      "ip_address": "192.168.0.51"}]},
        {"rois": []},
    ])
    posts = []
    monkeypatch.setattr(
        roi_api_client.requests, "get",
        lambda *_args, **_kwargs: FakeResponse(next(get_payloads)),
    )

    def fake_post(url, json, timeout):
        posts.append((url, json, timeout))
        return FakeResponse({"status": "created", "roi_id": 11})

    monkeypatch.setattr(roi_api_client.requests, "post", fake_post)
    result = roi_api_client.sync_rois(
        "http://127.0.0.1:8000", "CAM-01", "192.168.0.51",
        [SimpleNamespace(name="ROI-1", x1=10, y1=20, x2=110, y2=220)],
    )

    assert result.camera_id == 7
    assert result.created == 1
    assert posts[0][1] == {
        "camera_id": 7, "roi_name": "ROI-1",
        "x1": 10, "y1": 20, "x2": 110, "y2": 220,
        "version": 1, "enabled": True,
    }


def test_sync_rois_does_not_post_unchanged_definition(monkeypatch):
    get_payloads = iter([
        {"cameras": [{"camera_id": 7, "camera_code": "CAM-01",
                      "ip_address": "192.168.0.51"}]},
        {"rois": [{"camera_id": 7, "roi_name": "ROI-1",
                   "x1": 10, "y1": 20, "x2": 110, "y2": 220,
                   "version": 2, "enabled": True}]},
    ])
    monkeypatch.setattr(
        roi_api_client.requests, "get",
        lambda *_args, **_kwargs: FakeResponse(next(get_payloads)),
    )
    monkeypatch.setattr(
        roi_api_client.requests, "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unchanged ROI must not be posted")
        ),
    )

    result = roi_api_client.sync_rois(
        "http://127.0.0.1:8000", "CAM-01", "192.168.0.51",
        [SimpleNamespace(name="ROI-1", x1=10, y1=20, x2=110, y2=220)],
    )
    assert result.created == 0
    assert result.unchanged == 1

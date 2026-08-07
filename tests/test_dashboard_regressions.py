import threading
from types import SimpleNamespace

import numpy as np

from thermal_monitoring import config
from thermal_monitoring.analysis import roi
from thermal_monitoring.analysis.threshold import Status
from thermal_monitoring.tools import product_dashboard, threshold_api_client
from thermal_monitoring.tools.product_dashboard import ProductDashboard, SettingsDialog
from thermal_monitoring.tools.telegram_dispatcher import TelegramDispatcher


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_dashboard_accepts_missing_visual_only_in_warning_mode():
    dashboard = ProductDashboard.__new__(ProductDashboard)
    dashboard.cfg = SimpleNamespace(tools=SimpleNamespace(mode="both"))
    dashboard.capture = SimpleNamespace(warning_mode=True)

    assert dashboard._visual_required_for_quality(None) is False

    dashboard.capture.warning_mode = False
    assert dashboard._visual_required_for_quality(None) is True
    assert dashboard._visual_required_for_quality(np.zeros((2, 2, 3))) is True


def test_dashboard_selects_newest_thermal_during_warning_mode(monkeypatch):
    dashboard = ProductDashboard.__new__(ProductDashboard)
    dashboard.cfg = SimpleNamespace(
        tools=SimpleNamespace(mode="both"),
        paths=SimpleNamespace(dataset_dir="/dataset"),
    )
    dashboard.capture = SimpleNamespace(warning_mode=True)
    calls = []
    monkeypatch.setattr(
        product_dashboard,
        "latest_analysis_pair",
        lambda dataset_dir, **kwargs: (
            calls.append((dataset_dir, kwargs))
            or {"base": "latest"}
        ),
    )

    pair = dashboard._latest_pair()

    assert pair == {"base": "latest"}
    assert calls == [("/dataset", {"visual_mode": False})]


def test_dashboard_keeps_visual_grace_in_normal_both_mode(monkeypatch):
    dashboard = ProductDashboard.__new__(ProductDashboard)
    dashboard.cfg = SimpleNamespace(
        tools=SimpleNamespace(mode="both"),
        paths=SimpleNamespace(dataset_dir="/dataset"),
    )
    dashboard.capture = SimpleNamespace(warning_mode=False)
    calls = []
    monkeypatch.setattr(
        product_dashboard,
        "latest_analysis_pair",
        lambda dataset_dir, **kwargs: (
            calls.append((dataset_dir, kwargs))
            or {"base": "complete"}
        ),
    )

    dashboard._latest_pair()

    assert calls == [("/dataset", {"visual_mode": True})]


def test_roi_analysis_preserves_database_roi_id(tmp_path, monkeypatch):
    cfg = config.AppConfig()
    cfg.roi.rois = [
        config.RoiEntry(
            name="ROI-2",
            x1=0,
            y1=0,
            x2=640,
            y2=480,
            db_roi_id=42,
        )
    ]
    monkeypatch.setattr(roi, "load_config", lambda: cfg)
    npy_path = tmp_path / "capture_thermal.npy"
    np.save(npy_path, np.full((8, 8), 35.0, dtype=np.float32))

    loaded = roi.load_roi_config()
    results = roi.extract_all_rois_from_npy(str(npy_path), loaded)

    assert loaded.rois[0]["db_roi_id"] == 42
    assert results[0].roi_name == "ROI-2"
    assert results[0].db_roi_id == 42


def test_backend_alert_is_linked_and_merged_without_duplicate():
    dashboard = ProductDashboard.__new__(ProductDashboard)
    dashboard.cfg = SimpleNamespace(
        identity=SimpleNamespace(robot_id="Robot-01"),
    )
    dashboard.events = []
    dashboard._render_alert_cards = lambda: None

    local = dashboard._append_event(
        "Critical",
        70.0,
        "확인 필요",
        "ROI-2",
    )
    dashboard._link_backend_alert(local["id"], 91)
    dashboard._merge_backend_alerts([
        {
            "alert_id": 91,
            "occurred_at": "2026-07-29 12:00:00",
            "robot_code": "Robot-01",
            "roi_name": "ROI-2",
            "severity": "critical",
            "max_temp": 70.0,
            "event_status": "open",
            "acknowledged_at": None,
        }
    ])

    assert len(dashboard.events) == 1
    assert dashboard.events[0]["backend_id"] == "91"
    assert dashboard.events[0]["action"] == "확인 필요"


def test_backend_link_completion_without_alert_id_releases_local_event():
    dashboard = ProductDashboard.__new__(ProductDashboard)
    dashboard.cfg = SimpleNamespace(
        identity=SimpleNamespace(robot_id="Robot-01"),
    )
    dashboard.events = []
    dashboard._render_alert_cards = lambda: None
    local = dashboard._append_event(
        "Warning",
        55.0,
        "확인 필요",
        "ROI-2",
    )
    local["backend_pending"] = True

    dashboard._link_backend_alert(local["id"], None)

    assert local["backend_pending"] is False
    assert "backend_id" not in local


def test_acknowledge_waits_while_backend_link_is_pending():
    dashboard = ProductDashboard.__new__(ProductDashboard)
    dashboard.events = [{
        "id": "local",
        "backend_pending": True,
        "time": "2026-07-29 12:00:00",
        "asset": "Robot-01 · ROI-2",
        "state": "Critical",
        "temp": 70.0,
        "action": "확인 필요",
        "acknowledged_at": None,
    }]

    dashboard._acknowledge_event("local")

    assert dashboard.events[0]["action"] == "확인 필요"
    assert dashboard.events[0]["acknowledged_at"] is None


def test_backend_alert_merge_refreshes_acknowledged_state():
    dashboard = ProductDashboard.__new__(ProductDashboard)
    dashboard.events = [{
        "id": "local",
        "backend_id": "91",
        "time": "2026-07-29 12:00:00",
        "asset": "Robot-01 · ROI-2",
        "state": "Critical",
        "temp": 70.0,
        "action": "확인 필요",
        "acknowledged_at": None,
    }]
    dashboard._render_alert_cards = lambda: None

    dashboard._merge_backend_alerts([
        {
            "alert_id": 91,
            "occurred_at": "2026-07-29 12:00:00",
            "robot_code": "Robot-01",
            "roi_name": "ROI-2",
            "severity": "critical",
            "max_temp": 70.0,
            "event_status": "acknowledged",
            "acknowledged_at": "2026-07-29 12:01:00",
        }
    ])

    assert dashboard.events[0]["action"] == "확인 완료"
    assert dashboard.events[0]["acknowledged_at"] == "2026-07-29 12:01:00"


def test_backend_alert_merge_preserves_inflight_ack_state():
    dashboard = ProductDashboard.__new__(ProductDashboard)
    dashboard.events = [{
        "id": "local",
        "backend_id": "91",
        "time": "2026-07-29 12:00:00",
        "asset": "Robot-01 · ROI-2",
        "state": "Critical",
        "temp": 70.0,
        "action": "확인 필요",
        "ack_pending": True,
        "acknowledged_at": None,
    }]
    dashboard._render_alert_cards = lambda: None

    dashboard._merge_backend_alerts([
        {
            "alert_id": 91,
            "occurred_at": "2026-07-29 12:00:00",
            "robot_code": "Robot-01",
            "roi_name": "ROI-2",
            "severity": "critical",
            "max_temp": 70.0,
            "event_status": "open",
            "acknowledged_at": None,
        }
    ])

    assert dashboard.events[0]["ack_pending"] is True


def test_threshold_sync_updates_saved_roi_profiles(monkeypatch):
    settings = SettingsDialog.__new__(SettingsDialog)
    operating_logs = []
    settings.d = SimpleNamespace(
        cfg=SimpleNamespace(
            backend=SimpleNamespace(
                enabled=True,
                url="http://backend",
                timeout_sec=5,
            ),
            identity=SimpleNamespace(db_camera_id=7),
            roi=SimpleNamespace(
                rois=[
                    SimpleNamespace(db_roi_id=12),
                    SimpleNamespace(db_roi_id=13),
                ],
                baseline_temp=35.0,
                warning_delta=15.0,
                critical_delta=25.0,
            ),
            hotspot=SimpleNamespace(
                min_size=3,
                min_size_max=10,
            ),
            monitoring=SimpleNamespace(alarm_cooldown_sec=600.0),
        ),
        lifecycle="running",
        _add_operating_log=lambda *args: operating_logs.append(args),
    )
    calls = []
    monkeypatch.setattr(
        threshold_api_client,
        "sync_threshold_profiles",
        lambda **kwargs: (
            calls.append(kwargs)
            or threshold_api_client.ThresholdSyncResult(
                camera_id=7,
                roi_ids=(12, 13),
                created=1,
                updated=1,
            )
        ),
    )

    result = settings._sync_thresholds_to_backend()

    assert result.roi_ids == (12, 13)
    assert calls[0]["camera_id"] == 7
    assert calls[0]["roi_ids"] == [12, 13]
    assert calls[0]["baseline_temp"] == 35.0
    assert operating_logs[-1][1] == "저장 완료"


def test_threshold_sync_waits_until_roi_has_database_id(monkeypatch):
    settings = SettingsDialog.__new__(SettingsDialog)
    operating_logs = []
    settings.d = SimpleNamespace(
        cfg=SimpleNamespace(
            backend=SimpleNamespace(
                enabled=True,
                url="http://backend",
                timeout_sec=5,
            ),
            identity=SimpleNamespace(db_camera_id=7),
            roi=SimpleNamespace(
                rois=[SimpleNamespace(db_roi_id=None)],
                baseline_temp=35.0,
                warning_delta=15.0,
                critical_delta=25.0,
            ),
            hotspot=SimpleNamespace(
                min_size=3,
                min_size_max=10,
            ),
            monitoring=SimpleNamespace(alarm_cooldown_sec=600.0),
        ),
        lifecycle="running",
        _add_operating_log=lambda *args: operating_logs.append(args),
    )
    monkeypatch.setattr(
        threshold_api_client,
        "sync_threshold_profiles",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("ROI DB ID가 없으면 threshold API를 호출하면 안 됩니다.")
        ),
    )

    result = settings._sync_thresholds_to_backend()

    assert result.roi_ids == ()
    assert result.created == 0
    assert operating_logs[-1][1] == "보류"


def test_measurement_uses_matching_roi_identity_and_abnormal_statuses(monkeypatch):
    posted = []
    dashboard = SimpleNamespace(
        lifecycle="running",
        cfg=SimpleNamespace(
            backend=SimpleNamespace(
                enabled=True,
                url="http://backend",
                timeout_sec=5,
            ),
            identity=SimpleNamespace(
                db_camera_id=7,
                robot_id="Robot-01",
            ),
        ),
        metrics=SimpleNamespace(
            api_successes=0,
            api_timeouts=0,
            api_connection_errors=0,
            api_other_errors=0,
        ),
        _record_api_result=lambda *_args, **_kwargs: None,
    )
    dispatcher = TelegramDispatcher(dashboard)
    monkeypatch.setattr(
        "thermal_monitoring.tools.telegram_dispatcher.requests.post",
        lambda url, **kwargs: (
            posted.append((url, kwargs["json"]))
            or _Response({"status": "created", "alert_id": None})
        ),
    )
    result = {
        "base": "capture",
        "measurement_roi": SimpleNamespace(db_roi_id=12),
        "roi_name": "ROI-2",
        "max_temp": 61.0,
        "min_temp": 30.0,
        "mean_temp": 40.0,
        "hot_temp_95": 55.0,
        "over_temp_pixels": 30,
        "max_hotspot_size": 12,
        "status": Status.CRITICAL,
        "alarm_status": Status.WARNING,
        "measurement_status": Status.WARNING,
        "alarm": False,
    }

    dispatcher.post_measurement(result)
    result["measurement_status"] = Status.CRITICAL
    dispatcher.post_measurement(result)

    payload = posted[0][1]
    assert payload["camera_id"] == 7
    assert payload["roi_id"] == 12
    assert payload["max_temp"] == 61.0
    assert [post_payload["status"] for _, post_payload in posted] == [
        "warning",
        "critical",
    ]


def test_normal_measurement_is_persisted_and_completes_backend_event(monkeypatch):
    posted = []
    backend_event = threading.Event()
    dashboard = SimpleNamespace(
        lifecycle="running",
        cfg=SimpleNamespace(
            backend=SimpleNamespace(
                enabled=True,
                url="http://backend",
                timeout_sec=5,
            ),
            identity=SimpleNamespace(
                db_camera_id=7,
                robot_id="Robot-01",
            ),
        ),
        metrics=SimpleNamespace(
            api_successes=0,
            api_timeouts=0,
            api_connection_errors=0,
            api_other_errors=0,
        ),
        _record_api_result=lambda *_args, **_kwargs: None,
    )
    dispatcher = TelegramDispatcher(dashboard)
    monkeypatch.setattr(
        "thermal_monitoring.tools.telegram_dispatcher.requests.post",
        lambda *args, **kwargs: (
            posted.append((args, kwargs))
            or _Response({"status": "created", "capture_id": 1, "alert_id": None})
        ),
    )
    monkeypatch.setattr(
        dispatcher,
        "_ensure_threshold_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Normal 측정은 threshold sync를 호출하면 안 됩니다.")
        ),
    )
    result = {
        "base": "capture",
        "measurement_roi": SimpleNamespace(db_roi_id=12),
        "roi_name": "ROI-2",
        "max_temp": 31.0,
        "min_temp": 25.0,
        "mean_temp": 28.0,
        "hot_temp_95": 30.0,
        "status": Status.CRITICAL,
        "measurement_status": Status.NORMAL,
        "alarm": False,
        "_backend_posted_event": backend_event,
    }

    dispatcher.post_measurement(result)

    assert len(posted) == 1
    assert posted[0][1]["json"]["status"] == "normal"
    assert backend_event.is_set()
    assert vars(dashboard.metrics) == {
        "api_successes": 1,
        "api_timeouts": 0,
        "api_connection_errors": 0,
        "api_other_errors": 0,
    }


def test_measurement_repairs_missing_threshold_and_retries_once(monkeypatch):
    responses = iter([
        _Response({
            "status": "error",
            "error": "적용 가능한 threshold profile이 없습니다.",
        }),
        _Response({
            "status": "created",
            "capture_id": 51,
            "alert_id": None,
        }),
    ])
    posts = []
    operating_logs = []
    dashboard = SimpleNamespace(
        lifecycle="running",
        root=SimpleNamespace(after=lambda _delay, callback: callback()),
        cfg=SimpleNamespace(
            backend=SimpleNamespace(
                enabled=True,
                url="http://backend",
                timeout_sec=5,
            ),
            identity=SimpleNamespace(
                db_camera_id=7,
                robot_id="Robot-01",
            ),
            roi=SimpleNamespace(
                baseline_temp=35.0,
                warning_delta=15.0,
                critical_delta=25.0,
            ),
            hotspot=SimpleNamespace(
                min_size=3,
                min_size_max=10,
            ),
            monitoring=SimpleNamespace(alarm_cooldown_sec=600.0),
        ),
        metrics=SimpleNamespace(
            api_successes=0,
            api_timeouts=0,
            api_connection_errors=0,
            api_other_errors=0,
        ),
        _record_api_result=lambda *_args, **_kwargs: None,
        _add_operating_log=lambda *args: operating_logs.append(args),
    )
    dispatcher = TelegramDispatcher(dashboard)
    repaired = []
    monkeypatch.setattr(
        "thermal_monitoring.tools.telegram_dispatcher.requests.post",
        lambda url, **kwargs: (
            posts.append((url, kwargs["json"]))
            or next(responses)
        ),
    )
    monkeypatch.setattr(
        dispatcher,
        "_ensure_threshold_profile",
        lambda camera_id, roi_id: (
            repaired.append((camera_id, roi_id))
            or threshold_api_client.ThresholdSyncResult(
                camera_id=camera_id,
                roi_ids=(roi_id,),
                created=1,
                updated=0,
            )
        ),
    )
    result = {
        "base": "capture",
        "measurement_roi": SimpleNamespace(db_roi_id=12),
        "roi_name": "ROI-1",
        "max_temp": 40.0,
        "min_temp": 30.0,
        "mean_temp": 35.0,
        "hot_temp_95": 38.0,
        "over_temp_pixels": 0,
        "max_hotspot_size": 0,
        "status": Status.WARNING,
        "alarm": False,
    }

    dispatcher.post_measurement(result)

    assert repaired == [(7, 12)]
    assert len(posts) == 2
    assert dashboard.metrics.api_successes == 1
    assert dashboard.metrics.api_other_errors == 0
    assert operating_logs[-1][1] == "자동 복구"


def test_measurement_without_database_roi_id_is_not_posted(monkeypatch):
    posted = []
    dashboard = SimpleNamespace(
        lifecycle="running",
        cfg=SimpleNamespace(
            backend=SimpleNamespace(
                enabled=True,
                url="http://backend",
                timeout_sec=5,
            ),
            identity=SimpleNamespace(
                db_camera_id=7,
                robot_id="Robot-01",
            ),
        ),
        metrics=SimpleNamespace(
            api_successes=0,
            api_timeouts=0,
            api_connection_errors=0,
            api_other_errors=0,
        ),
        _record_api_result=lambda *_args, **_kwargs: None,
    )
    dispatcher = TelegramDispatcher(dashboard)
    monkeypatch.setattr(
        "thermal_monitoring.tools.telegram_dispatcher.requests.post",
        lambda *args, **kwargs: posted.append((args, kwargs)),
    )
    result = {
        "measurement_roi": SimpleNamespace(db_roi_id=None),
        "roi_name": "ROI-2",
        "max_temp": 61.0,
        "mean_temp": 40.0,
        "hot_temp_95": 55.0,
        "status": Status.WARNING,
        "alarm": False,
    }

    dispatcher.post_measurement(result)

    assert posted == []
    assert dashboard.metrics.api_other_errors == 1

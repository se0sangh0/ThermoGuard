import threading
import queue
from types import SimpleNamespace

import numpy as np

from thermal_monitoring import config
from thermal_monitoring.analysis import roi
from thermal_monitoring.analysis.threshold import Status
from thermal_monitoring.tools import (
    asset_api_client,
    product_dashboard,
    threshold_api_client,
)
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


def test_worker_ui_post_uses_queue_not_tk_from_worker_thread():
    dashboard = ProductDashboard.__new__(ProductDashboard)
    scheduled = []
    dashboard.lifecycle = "running"
    dashboard._ui_queue = queue.Queue()
    dashboard._ui_dispatch_timer = None
    dashboard.root = SimpleNamespace(after=lambda delay, callback: scheduled.append((delay, callback)) or "timer")
    applied = []

    thread = threading.Thread(
        target=lambda: dashboard._post_to_ui(lambda: applied.append("applied")),
    )
    thread.start()
    thread.join(timeout=1)

    assert scheduled == []
    dashboard._drain_ui_queue()
    assert applied == ["applied"]
    assert scheduled and scheduled[-1][0] == 50


def test_worker_ui_post_is_ignored_after_shutdown():
    dashboard = ProductDashboard.__new__(ProductDashboard)
    dashboard.lifecycle = "closed"
    dashboard._ui_queue = queue.Queue()

    assert dashboard._post_to_ui(lambda: None) is False
    assert dashboard._ui_queue.empty()


def test_dashboard_has_no_automatic_destructive_maintenance_entrypoints():
    """Retention deletion and broad dataset repair stay outside the GUI."""
    assert not hasattr(ProductDashboard, "_run_integrity")
    assert not hasattr(ProductDashboard, "_run_cleanup")
    assert not hasattr(ProductDashboard, "_run_normal_removal")


def test_dashboard_queues_metadata_after_a_new_analysis(monkeypatch):
    dashboard = ProductDashboard.__new__(ProductDashboard)
    submitted = []
    dashboard._maintenance_executor = SimpleNamespace(
        submit=lambda function, *args: submitted.append((function, args))
    )
    dashboard.cfg = SimpleNamespace(paths=SimpleNamespace(dataset_dir="/dataset"))

    dashboard._queue_metadata_update()

    assert submitted == [(dashboard._run_metadata_update, ("/dataset",))]


def test_collection_dashboard_does_not_require_backend_commissioning(monkeypatch):

    dashboard = ProductDashboard.__new__(ProductDashboard)
    dashboard.lifecycle = "running"
    dashboard.monitoring = False
    dashboard.capture_paused_by_user = False
    dashboard._commissioning_block_announced = False
    dashboard.cfg = SimpleNamespace(
        backend=SimpleNamespace(enabled=False),
        identity=SimpleNamespace(db_camera_id=None),
    )
    starts = []
    dashboard._stopping_capture = None
    dashboard._restart_after_capture_stop = False
    dashboard._cancel_connection_retry = lambda: None
    dashboard._start_capture_session = lambda: starts.append("started")
    monkeypatch.setattr(product_dashboard, "factory_mode_enabled", lambda: True)

    dashboard.start_monitoring()

    assert dashboard.capture_paused_by_user is False
    assert starts == ["started"]


def test_dashboard_does_not_probe_camera_outside_running_capture(monkeypatch):
    dashboard = ProductDashboard.__new__(ProductDashboard)
    dashboard.lifecycle = "running"
    dashboard.capture = SimpleNamespace(running=True)
    dashboard._connection_check_running = False
    dashboard._resume_after_connection_check = False
    dashboard.metrics = SimpleNamespace(connection_attempts=0)
    monkeypatch.setattr(
        product_dashboard.requests,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("running CaptureSession must own camera HTTP")
        ),
    )

    dashboard._check_connection_async()

    assert dashboard._connection_check_running is False
    assert dashboard.metrics.connection_attempts == 0


def test_capture_error_log_does_not_start_independent_connection_probe():
    dashboard = ProductDashboard.__new__(ProductDashboard)
    dashboard.lifecycle = "running"
    dashboard.metrics = SimpleNamespace(
        capture_attempts=0,
        capture_successes=0,
        exception_count=0,
    )
    dashboard._add_operating_log = lambda *_args: None
    dashboard._record_api_message = lambda *_args: None
    dashboard._update_connection_stability_display = lambda: None
    dashboard._update_metric_text = lambda: None
    dashboard._check_connection_async = lambda: (_ for _ in ()).throw(
        AssertionError("capture error must not create a second camera request")
    )

    dashboard._handle_capture_log("[thermal] HTTP 503")

    assert dashboard.metrics.capture_attempts == 1
    assert dashboard.metrics.exception_count == 1


def test_invalid_pair_is_not_persisted_as_a_backend_alarm(monkeypatch):
    """Invalid imagery cannot create a DB-only Critical event."""
    dashboard = ProductDashboard.__new__(ProductDashboard)
    dashboard.lifecycle = "running"
    dashboard._analysis_generation = 1
    dashboard.latest_status = Status.NORMAL
    dashboard.latest_alarm_status = Status.NORMAL
    dashboard.state = SimpleNamespace(status=Status.NORMAL)
    dashboard._last_quality_capture_id = None
    dashboard._last_alert_capture = None
    dashboard._latest_pair_fresh = False
    dashboard._latest_pair_quality_ok = False
    dashboard.capture = None
    dashboard.cfg = SimpleNamespace(
        camera=SimpleNamespace(capture_interval_sec=30),
        backend=SimpleNamespace(enabled=True),
    )
    dashboard.metrics = SimpleNamespace(
        anomaly_today=0,
        analysis_ok=0,
        image_quality_checks=0,
        image_quality_successes=0,
    )
    dashboard._image_quality_window = []
    metadata_updates = []
    dashboard._queue_metadata_update = lambda: metadata_updates.append(True)
    dashboard._add_operating_log = lambda *_args: None
    dashboard._update_values_with_result = lambda *_args: None
    dashboard._finish_analysis = lambda *_args: None
    dashboard._draw_status_gauge = lambda: None
    dashboard._draw_temperature_trend = lambda: None
    dashboard._show_image = lambda *_args: None
    dashboard._render_alert_cards = lambda: None
    dashboard.visual_photo = None
    dashboard.thermal_photo = None
    dashboard.visual_label = object()
    dashboard.thermal_label = object()
    dashboard.telegram = SimpleNamespace(
        post_measurement=lambda *_args: (_ for _ in ()).throw(
            AssertionError("invalid pair must not be posted")
        ),
        maybe_dispatch=lambda *_args: None,
    )

    result = {
        "base": "invalid-capture",
        "status": Status.CRITICAL,
        "alarm_status": Status.CRITICAL,
        "alarm": True,
        "max_temp": 80.0,
        "mean_temp": 70.0,
        "overall_max_temp": 80.0,
        "overall_max_roi_name": "ROI-1",
        "captured_at": product_dashboard.datetime.now(),
        "image_quality_ok": False,
        "image_quality_reason": "visual missing",
        "visual_img": None,
        "overlay": None,
    }

    dashboard._apply_analysis_result(result, 1)

    # Radiometric capture inventory is independent of visual quality, while
    # the mocked post_measurement above proves no invalid DB measurement runs.
    assert metadata_updates == [True]


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


def test_critical_popup_only_fires_when_entering_critical():
    should_show = ProductDashboard._should_show_critical_popup

    assert should_show(Status.NORMAL, Status.CRITICAL) is True
    assert should_show(Status.WARNING, Status.CRITICAL) is True
    assert should_show(Status.CRITICAL, Status.CRITICAL) is False
    assert should_show(Status.NORMAL, Status.WARNING) is False
    assert should_show(Status.WARNING, Status.NORMAL) is False


def test_critical_popup_can_fire_again_after_recovery():
    statuses = [
        Status.NORMAL,
        Status.CRITICAL,
        Status.CRITICAL,
        Status.WARNING,
        Status.CRITICAL,
    ]

    transitions = [
        ProductDashboard._should_show_critical_popup(previous, current)
        for previous, current in zip(statuses, statuses[1:])
    ]

    assert transitions == [True, False, False, True]


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


def test_threshold_sync_uses_camera_wide_profile_until_roi_has_database_id(monkeypatch):
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
    calls = []
    monkeypatch.setattr(
        threshold_api_client,
        "sync_threshold_profiles",
        lambda **kwargs: calls.append(kwargs)
        or threshold_api_client.ThresholdSyncResult(
            camera_id=7,
            roi_ids=(),
            created=1,
            updated=0,
        ),
    )

    result = settings._sync_thresholds_to_backend()

    assert result.roi_ids == ()
    assert result.created == 1
    assert calls[0]["roi_ids"] == []
    assert operating_logs[-1][1] == "저장 완료"


def _settings_save_dialog(tmp_path, cfg):
    """Build a Tk-free SettingsDialog fixture for save transaction tests."""

    dialog = SettingsDialog.__new__(SettingsDialog)
    dialog.d = SimpleNamespace(
        cfg=cfg,
        capture=None,
        monitoring=False,
        _stopping_capture=None,
        _gige_reader=None,
        _stopping_gige_reader=None,
        capture_paused_by_user=False,
        _commissioning_block_announced=True,
        _add_operating_log=lambda *_args: None,
        _stop_gige_probe=lambda: None,
        _wait_for_capture_stop=lambda *_args, **_kwargs: None,
        apply_saved_settings_immediately=lambda: None,
    )
    dialog.win = object()
    dialog.ip = SimpleNamespace(get=lambda: "127.0.0.1")
    dialog.dataset_dir = SimpleNamespace(get=lambda: str(tmp_path / "dataset"))
    dialog.baseline = SimpleNamespace(get=lambda: "35")
    dialog.warning = SimpleNamespace(get=lambda: "15")
    dialog.critical = SimpleNamespace(get=lambda: "25")
    dialog.close = lambda: None
    return dialog


def test_settings_bootstrap_requires_explicit_confirmation_and_persists_ids(
    tmp_path, monkeypatch
):
    cfg = config.AppConfig()
    cfg.backend.enabled = False
    cfg.identity.db_camera_id = None
    dialog = _settings_save_dialog(tmp_path, cfg)
    saved = []
    confirmations = []
    sequence = []
    monkeypatch.setattr(product_dashboard, "factory_mode_enabled", lambda: True)
    monkeypatch.setattr(
        product_dashboard.messagebox,
        "askyesno",
        lambda *_args, **_kwargs: confirmations.append(True) or True,
    )
    monkeypatch.setattr(product_dashboard.messagebox, "showerror", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        asset_api_client,
        "register_asset_hierarchy",
        lambda **kwargs: (
            sequence.append("register")
            or asset_api_client.AssetRegistration(1, 2, 3, 4)
        ),
    )
    monkeypatch.setattr(
        dialog,
        "_sync_thresholds_to_backend",
        lambda **kwargs: sequence.append("threshold"),
    )
    monkeypatch.setattr(
        product_dashboard,
        "save_collection_config",
        lambda candidate: sequence.append("save") or saved.append(candidate),
    )

    dialog.save()

    assert confirmations == [True]
    assert sequence == ["register", "threshold", "save"]
    assert saved and saved[0].backend.enabled is True
    assert saved[0].identity.db_camera_id == 4
    assert dialog.d.cfg.backend.enabled is True
    assert dialog.d.capture_paused_by_user is False
    assert dialog.d._commissioning_block_announced is False


def test_factory_settings_save_requires_quiescent_capture_before_db_changes(
    tmp_path, monkeypatch
):
    cfg = config.AppConfig()
    dialog = _settings_save_dialog(tmp_path, cfg)
    dialog.d.monitoring = True
    dialog.d.capture = object()
    errors = []
    monkeypatch.setattr(product_dashboard, "factory_mode_enabled", lambda: True)
    monkeypatch.setattr(
        product_dashboard.messagebox,
        "showerror",
        lambda *args, **_kwargs: errors.append(args),
    )
    monkeypatch.setattr(
        asset_api_client,
        "register_asset_hierarchy",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("capture must stop before backend registration")
        ),
    )
    monkeypatch.setattr(
        product_dashboard,
        "save_collection_config",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("capture must stop before local persistence")
        ),
    )

    dialog.save()

    assert errors and errors[-1][0] == "촬영 정지 필요"
    assert dialog.d.cfg is cfg


def test_factory_settings_save_waits_for_pending_gige_cleanup(tmp_path, monkeypatch):
    cfg = config.AppConfig()
    dialog = _settings_save_dialog(tmp_path, cfg)
    dialog.d._stopping_gige_reader = object()
    errors = []
    monkeypatch.setattr(product_dashboard, "factory_mode_enabled", lambda: True)
    monkeypatch.setattr(
        product_dashboard.messagebox,
        "showerror",
        lambda *args, **_kwargs: errors.append(args),
    )
    monkeypatch.setattr(
        product_dashboard,
        "save_collection_config",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("pending GigE cleanup must block settings save")
        ),
    )

    dialog.save()

    assert errors and errors[-1][0] == "촬영 정지 필요"
    assert dialog.d.cfg is cfg


def test_factory_roi_change_requires_quiescent_capture(tmp_path, monkeypatch):
    cfg = config.AppConfig()
    dialog = _settings_save_dialog(tmp_path, cfg)
    dialog.d.monitoring = True
    errors = []
    monkeypatch.setattr(product_dashboard, "factory_mode_enabled", lambda: True)
    monkeypatch.setattr(
        product_dashboard.messagebox,
        "showerror",
        lambda *args, **_kwargs: errors.append(args),
    )

    dialog.open_roi_editor()

    assert errors and errors[-1][0] == "촬영 정지 필요"


def test_settings_threshold_failure_keeps_prior_runtime_config(tmp_path, monkeypatch):
    cfg = config.AppConfig()
    cfg.backend.enabled = True
    cfg.identity.factory_id = 1
    cfg.identity.line_id = 2
    cfg.identity.db_robot_id = 3
    cfg.identity.db_camera_id = 4
    cfg.paths.dataset_dir = str(tmp_path / "old-dataset")
    cfg.paths.overlay_dir = str(tmp_path / "old-dataset" / "overlay")
    dialog = _settings_save_dialog(tmp_path, cfg)
    errors = []
    sequence = []
    monkeypatch.setattr(product_dashboard.messagebox, "showerror", lambda *args, **_kwargs: errors.append(args))
    monkeypatch.setattr(
        asset_api_client,
        "register_asset_hierarchy",
        lambda **kwargs: (
            sequence.append("register")
            or asset_api_client.AssetRegistration(1, 2, 3, 4)
        ),
    )
    monkeypatch.setattr(
        dialog,
        "_sync_thresholds_to_backend",
        lambda **_kwargs: (
            sequence.append("threshold")
            or (_ for _ in ()).throw(RuntimeError("threshold unavailable"))
        ),
    )
    monkeypatch.setattr(
        product_dashboard,
        "save_collection_config",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not save before threshold sync")),
    )

    dialog.save()

    assert dialog.d.cfg is cfg
    assert cfg.paths.dataset_dir == str(tmp_path / "old-dataset")
    assert cfg.roi.warning_delta == 15.0
    assert cfg.roi.critical_delta == 25.0
    assert dialog.d.monitoring is False
    assert dialog.d.capture is None
    assert sequence == ["register", "threshold"]
    assert errors and errors[-1][0] == "카메라 정보 DB 저장 실패"


def test_settings_rejects_invalid_threshold_order_before_backend_or_local_write(
    tmp_path, monkeypatch
):
    cfg = config.AppConfig()
    cfg.backend.enabled = True
    cfg.identity.factory_id = 1
    cfg.identity.line_id = 2
    cfg.identity.db_robot_id = 3
    cfg.identity.db_camera_id = 4
    dialog = _settings_save_dialog(tmp_path, cfg)
    dialog.warning = SimpleNamespace(get=lambda: "25")
    dialog.critical = SimpleNamespace(get=lambda: "20")
    errors = []
    monkeypatch.setattr(product_dashboard, "factory_mode_enabled", lambda: True)
    monkeypatch.setattr(
        product_dashboard.messagebox,
        "showerror",
        lambda *args, **_kwargs: errors.append(args),
    )
    monkeypatch.setattr(
        asset_api_client,
        "register_asset_hierarchy",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid thresholds must not reach asset API")
        ),
    )
    monkeypatch.setattr(
        product_dashboard,
        "save_collection_config",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("invalid thresholds must not be persisted")
        ),
    )

    dialog.save()

    assert dialog.d.cfg is cfg
    assert cfg.roi.warning_delta == 15.0
    assert cfg.roi.critical_delta == 25.0
    assert errors and errors[-1][0] == "안전 검증 실패"


def test_threshold_sync_caps_settings_backend_timeout(monkeypatch):
    settings = SettingsDialog.__new__(SettingsDialog)
    settings.d = SimpleNamespace(
        cfg=SimpleNamespace(
            backend=SimpleNamespace(enabled=True, url="http://backend", timeout_sec=600),
            identity=SimpleNamespace(db_camera_id=7),
            roi=SimpleNamespace(
                rois=[SimpleNamespace(db_roi_id=12)],
                baseline_temp=35.0,
                warning_delta=15.0,
                critical_delta=25.0,
            ),
            hotspot=SimpleNamespace(min_size=3, min_size_max=10),
            monitoring=SimpleNamespace(alarm_cooldown_sec=600.0),
        ),
        _add_operating_log=lambda *_args: None,
    )
    calls = []
    monkeypatch.setattr(
        threshold_api_client,
        "sync_threshold_profiles",
        lambda **kwargs: calls.append(kwargs)
        or threshold_api_client.ThresholdSyncResult(7, (12,), 0, 1),
    )

    settings._sync_thresholds_to_backend()

    assert calls[0]["timeout"] == config.BACKEND_IO_TIMEOUT_MAX_SEC


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

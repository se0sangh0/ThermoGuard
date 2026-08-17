from pathlib import Path
from types import SimpleNamespace

from thermal_monitoring.tools import calibration, product_dashboard
from thermal_monitoring.tools.product_dashboard import SettingsDialog


def test_calibration_point_metrics_report_distribution_without_thresholds():
    metrics = calibration.calibration_point_metrics(
        [(0, 0), (640, 0), (640, 480), (0, 480), (320, 240), (500, 300)],
        640,
        480,
    )

    assert metrics["point_count"] == 6
    assert metrics["x_span_ratio"] == 1.0
    assert metrics["y_span_ratio"] == 1.0
    assert metrics["hull_area_ratio"] == 1.0


def test_calibration_point_metrics_allow_narrow_distribution():
    metrics = calibration.calibration_point_metrics(
        [(100, 100), (120, 100), (120, 120), (100, 120), (110, 110), (115, 115)],
        640,
        480,
    )

    assert metrics["point_count"] == 6
    assert metrics["hull_area_ratio"] < 0.01


def _dialog(tmp_path: Path):
    logs = []
    lifecycle = []
    dialog = SettingsDialog.__new__(SettingsDialog)
    dialog.d = SimpleNamespace(
        cfg=SimpleNamespace(
            paths=SimpleNamespace(
                dataset_dir=str(tmp_path),
                homography_path=str(tmp_path / "thermal_to_rgb.npy"),
            ),
        ),
        metrics=SimpleNamespace(exception_count=0),
        _add_operating_log=lambda *args: logs.append(args),
    )
    dialog.win = SimpleNamespace(
        winfo_exists=lambda: True,
        after_idle=lambda callback: lifecycle.append(("after_idle", callback)),
    )
    dialog._begin_tool = lambda tool_name: lifecycle.append(
        ("begin", tool_name),
    ) or True
    dialog._end_tool = lambda: lifecycle.append(("end",))
    dialog._show_tool_guard = lambda: lifecycle.append(("guard",))
    dialog._pump_tool_events = lambda: None
    dialog._tool_display_bounds = lambda: (10, 20, 1920, 1080)
    return dialog, logs, lifecycle


def test_open_calibration_uses_existing_calibration_api(monkeypatch, tmp_path):
    thermal = tmp_path / "20260729120000_000001.jpg"
    visual = tmp_path / "20260729120000_000001_visual.jpg"
    thermal.touch()
    visual.touch()
    dialog, logs, lifecycle = _dialog(tmp_path)
    calls = []

    monkeypatch.setattr(
        calibration,
        "run_calibration",
        lambda *args, **kwargs: calls.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(
        product_dashboard.messagebox,
        "askyesno",
        lambda *args, **kwargs: False,
    )

    dialog.open_calibration()

    assert calls[0][0] == (str(thermal), str(visual))
    assert calls[0][1]["event_pump"] is dialog._pump_tool_events
    assert calls[0][1]["display_bounds"] == (10, 20, 1920, 1080)
    assert callable(calls[0][1]["result_callback"])
    assert lifecycle == [("begin", "캘리브레이션"), ("guard",), ("end",)]
    assert ("캘리브레이션", "완료", str(tmp_path / "thermal_to_rgb.npy")) in logs


def test_open_calibration_treats_api_cancel_as_unsaved(monkeypatch, tmp_path):
    thermal = tmp_path / "20260729120000_000001.jpg"
    visual = tmp_path / "20260729120000_000001_visual.jpg"
    thermal.touch()
    visual.touch()
    dialog, logs, lifecycle = _dialog(tmp_path)

    monkeypatch.setattr(calibration, "run_calibration", lambda *args, **kwargs: False)

    dialog.open_calibration()

    assert lifecycle == [("begin", "캘리브레이션"), ("guard",), ("end",)]
    assert ("캘리브레이션", "종료", "저장 없이 종료") in logs


def test_roi_precheck_resolves_relative_calibration_from_config(
    monkeypatch, tmp_path
):
    config_dir = tmp_path / "home" / "operator" / ".config" / "thermoguard"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    (config_dir / "thermal_to_rgb.npy").touch()
    service_cwd = tmp_path / "service-cwd"
    service_cwd.mkdir()
    monkeypatch.chdir(service_cwd)
    monkeypatch.setenv("THERMOGUARD_CONFIG", str(config_path))

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    thermal = dataset / "20260729120000_000001.jpg"
    visual = dataset / "20260729120000_000001_visual.jpg"
    thermal.touch()
    visual.touch()
    dialog = SettingsDialog.__new__(SettingsDialog)
    dialog.d = SimpleNamespace(
        cfg=SimpleNamespace(
            paths=SimpleNamespace(
                dataset_dir=str(dataset),
                homography_path="thermal_to_rgb.npy",
            )
        )
    )
    dialog.win = object()
    dialog._require_factory_capture_quiescent = lambda _action: True
    started = []
    dialog._begin_tool = lambda name: started.append(name) or False
    warnings = []
    monkeypatch.setattr(
        product_dashboard.messagebox,
        "showwarning",
        lambda *args, **kwargs: warnings.append((args, kwargs)),
    )

    dialog.open_roi_editor()

    assert started == ["ROI 설정"]
    assert warnings == []


def test_open_calibration_restores_tool_state_after_api_error(
    monkeypatch,
    tmp_path,
):
    thermal = tmp_path / "20260729120000_000001.jpg"
    visual = tmp_path / "20260729120000_000001_visual.jpg"
    thermal.touch()
    visual.touch()
    dialog, logs, lifecycle = _dialog(tmp_path)
    errors = []

    def fail(*args, **kwargs):
        raise RuntimeError("calibration failed")

    monkeypatch.setattr(calibration, "run_calibration", fail)
    monkeypatch.setattr(
        product_dashboard.messagebox,
        "showerror",
        lambda *args, **kwargs: errors.append((args, kwargs)),
    )

    dialog.open_calibration()

    assert lifecycle == [("begin", "캘리브레이션"), ("guard",), ("end",)]
    assert dialog.d.metrics.exception_count == 1
    assert ("캘리브레이션", "예외 처리", "calibration failed") in logs
    assert errors[0][0] == ("캘리브레이션", "calibration failed")


def test_calibration_event_pump_tolerates_destroyed_guard():
    dialog = SettingsDialog.__new__(SettingsDialog)

    def destroyed():
        raise product_dashboard.tk.TclError("window was destroyed")

    dialog._tool_guard_window = SimpleNamespace(
        winfo_exists=lambda: True,
        update=destroyed,
    )

    dialog._pump_tool_events()

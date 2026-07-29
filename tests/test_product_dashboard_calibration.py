from pathlib import Path
from types import SimpleNamespace

from thermal_monitoring.tools import calibration, product_dashboard
from thermal_monitoring.tools.product_dashboard import SettingsDialog


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

    assert calls == [(
        (str(thermal), str(visual)),
        {
            "event_pump": dialog._pump_tool_events,
            "display_bounds": (10, 20, 1920, 1080),
        },
    )]
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

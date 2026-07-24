from thermal_monitoring.tools.tk_image_dialogs import (
    fit_image_rect,
    recommended_window_size,
)
from thermal_monitoring.tools.product_dashboard import SettingsDialog


def test_fit_image_rect_preserves_aspect_ratio_and_centers_image():
    rect = fit_image_rect(640, 480, 0, 0, 1000, 500)

    assert (rect.width, rect.height) == (666, 500)
    assert rect.x == 167
    assert rect.y == 0


def test_image_rect_coordinate_round_trip_after_resize():
    for area_width, area_height in ((800, 400), (1100, 650), (600, 700)):
        rect = fit_image_rect(640, 480, 0, 0, area_width, area_height)
        canvas_point = rect.to_canvas(321, 239)
        restored = rect.to_source(*canvas_point)

        assert abs(restored[0] - 321) <= 1
        assert abs(restored[1] - 239) <= 1


def test_image_rect_rejects_letterbox_area():
    rect = fit_image_rect(640, 480, 0, 0, 1000, 500)

    assert not rect.contains(20, 250)
    assert rect.contains(rect.x, rect.y)


def test_resolution_aware_popup_sizes_for_1920_by_1200():
    roi_size = recommended_window_size(
        1920, 1200, 0.70, 0.70, (600, 360), (1400, 900),
    )
    calibration_size = recommended_window_size(
        1920, 1200, 0.82, 0.75, (680, 400), (1600, 950),
    )

    assert roi_size == (1344, 840)
    assert calibration_size == (1574, 900)


def test_resolution_aware_popup_sizes_respect_limits():
    assert recommended_window_size(
        800, 600, 0.70, 0.70, (600, 360), (1400, 900),
    ) == (600, 420)
    assert recommended_window_size(
        3840, 2160, 0.82, 0.75, (680, 400), (1600, 950),
    ) == (1600, 950)


def test_latest_complete_pair_skips_newer_thermal_only_capture(tmp_path):
    old_thermal = tmp_path / "20260724120000_000001.jpg"
    old_visual = tmp_path / "20260724120000_000001_visual.jpg"
    new_thermal = tmp_path / "20260724120030_000001.jpg"
    old_thermal.touch()
    old_visual.touch()
    new_thermal.touch()

    pair = SettingsDialog._latest_complete_image_pair(tmp_path)

    assert pair == (old_thermal, old_visual)


def test_latest_complete_pair_returns_none_without_visual(tmp_path):
    (tmp_path / "20260724120030_000001.jpg").touch()

    assert SettingsDialog._latest_complete_image_pair(tmp_path) is None

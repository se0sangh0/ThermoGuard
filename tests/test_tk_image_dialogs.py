import numpy as np

from thermal_monitoring.tools.tk_image_dialogs import (
    calibration_hull_canvas_points,
    fit_image_rect,
    recommended_window_size,
    roi_is_inside_calibration_hull,
    roi_coordinate_text,
    thermal_bounds_for_roi,
    transformed_roi_bounds,
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


def test_calibration_hull_uses_roi_image_coordinate_transform():
    rect = fit_image_rect(1920, 1080, 0, 0, 1000, 500)
    hull = np.array([[[100, 100]], [[500, 100]], [[500, 400]], [[100, 400]]])

    assert calibration_hull_canvas_points(hull, rect) == [
        *rect.to_canvas(100, 100),
        *rect.to_canvas(500, 100),
        *rect.to_canvas(500, 400),
        *rect.to_canvas(100, 400),
    ]


def test_legacy_calibration_without_points_has_no_visible_hull():
    rect = fit_image_rect(640, 480, 0, 0, 640, 480)

    assert calibration_hull_canvas_points(None, rect) == []


def test_roi_hull_check_accepts_inside_and_boundary_but_rejects_outside():
    hull = np.array([[[10, 10]], [[100, 10]], [[100, 100]], [[10, 100]]], dtype=np.float32)

    assert roi_is_inside_calibration_hull(
        {"x1": 20, "y1": 20, "x2": 90, "y2": 90}, hull,
    )
    assert roi_is_inside_calibration_hull(
        {"x1": 10, "y1": 10, "x2": 100, "y2": 100}, hull,
    )
    assert not roi_is_inside_calibration_hull(
        {"x1": 5, "y1": 20, "x2": 90, "y2": 90}, hull,
    )


def test_roi_hull_check_allows_legacy_calibration_without_hull():
    assert roi_is_inside_calibration_hull(
        {"x1": 0, "y1": 0, "x2": 100, "y2": 100}, None,
    )


def test_roi_coordinate_text_formats_bounds_and_size():
    assert roi_coordinate_text({
        "x1": 120, "y1": 80, "x2": 420, "y2": 260,
    }) == "(120, 80)-(420, 260) · 300×180 px"


def test_transformed_roi_bounds_uses_inverse_calibration_coordinates():
    visual_to_thermal = np.array([
        [0.25, 0.0, 0.0],
        [0.0, 0.25, 0.0],
        [0.0, 0.0, 1.0],
    ], dtype=np.float32)

    assert transformed_roi_bounds({
        "x1": 1000, "y1": 400, "x2": 1800, "y2": 1200,
    }, visual_to_thermal) == {
        "x1": 250, "y1": 100, "x2": 450, "y2": 300,
    }


def test_existing_roi_keeps_saved_thermal_bounds_until_edited():
    inverse = np.diag([0.25, 0.25, 1.0]).astype(np.float32)
    roi = {
        "x1": 1000, "y1": 400, "x2": 1800, "y2": 1200,
        "_thermal_bounds": {"x1": 125, "y1": 117, "x2": 566, "y2": 371},
    }

    assert thermal_bounds_for_roi(roi, inverse) == {
        "x1": 125, "y1": 117, "x2": 566, "y2": 371,
    }


def test_edited_roi_uses_inverse_calibration_bounds():
    inverse = np.diag([0.25, 0.25, 1.0]).astype(np.float32)
    roi = {"x1": 1000, "y1": 400, "x2": 1800, "y2": 1200}

    assert thermal_bounds_for_roi(roi, inverse) == {
        "x1": 250, "y1": 100, "x2": 450, "y2": 300,
    }


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

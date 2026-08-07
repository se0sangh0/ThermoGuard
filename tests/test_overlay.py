"""GUI-UPDATE: 실제 데이터셋이 있을 때 오버레이를 검증하고 없으면 건너뛴다."""

import os
import unittest

import cv2
import numpy as np

from thermal_monitoring.analysis.roi import load_roi_config, extract_roi_from_npy
from thermal_monitoring.analysis.threshold import evaluate_threshold
from thermal_monitoring.analysis.overlay import (
    create_overlay,
    create_visual_roi_overlay,
    save_overlay,
)


DATASET_DIR = "thermal_dataset"


class OverlayIntegrationTests(unittest.TestCase):
    def test_latest_dataset_overlay(self):
        if not os.path.isdir(DATASET_DIR):
            self.skipTest("thermal_dataset is not available")

        npy_files = sorted(
            f for f in os.listdir(DATASET_DIR) if f.endswith("_thermal.npy")
        )
        if not npy_files:
            self.skipTest("thermal_dataset has no thermal NPY files")

        latest_npy = npy_files[-1]
        base = latest_npy.replace("_thermal.npy", "")
        thermal_jpg = os.path.join(DATASET_DIR, f"{base}.jpg")
        visual_jpg = os.path.join(DATASET_DIR, f"{base}_visual.jpg")
        npy_path = os.path.join(DATASET_DIR, latest_npy)

        config = load_roi_config()
        result = extract_roi_from_npy(npy_path, config)
        status = evaluate_threshold(
            result.hot_temp_95,
            result.max_temp,
            config.baseline_temp,
            config.warning_delta,
            config.critical_delta,
            max_hotspot_size=result.max_hotspot_size,
        )

        overlay = create_overlay(
            thermal_jpg_path=thermal_jpg,
            visual_jpg_path=visual_jpg,
            roi_bounds=result.roi_bounds,
            max_temp=result.max_temp,
            mean_temp=result.mean_temp,
            hot_temp=result.hot_temp_95,
            status=status.value,
            hotspot_centroids=result.hotspot_centroids,
        )
        out = save_overlay(base, overlay)

        self.assertIsNotNone(overlay)
        self.assertTrue(os.path.isfile(out))


def test_visual_roi_overlay_projects_polygon_and_name(tmp_path):
    calibration = tmp_path / "calibration.npy"
    np.save(calibration, {
        "H": np.array([[2.0, 0.0, 10.0], [0.0, 2.0, 20.0], [0.0, 0.0, 1.0]]),
        "thermal_size": (640, 480),
        "visual_size": (1280, 960),
    })
    visual = np.zeros((960, 1280, 3), dtype=np.uint8)

    projected, warning = create_visual_roi_overlay(
        visual,
        [(10, 20, 100, 120)],
        ["ROBOT-ARM"],
        ["Critical"],
        calibration_path=str(calibration),
        thermal_size=(640, 480),
    )

    assert warning is None
    assert np.any(projected != visual)
    # (10, 20) -> (30, 60), Critical uses a red BGR outline.
    patch = projected[55:66, 25:36]
    assert int(patch[:, :, 2].max()) > 0


def test_visual_roi_overlay_hides_roi_on_resolution_mismatch(tmp_path):
    calibration = tmp_path / "calibration.npy"
    np.save(calibration, {
        "H": np.eye(3),
        "thermal_size": (640, 480),
        "visual_size": (1920, 1080),
    })
    visual = np.full((960, 1280, 3), 17, dtype=np.uint8)

    projected, warning = create_visual_roi_overlay(
        visual,
        [(10, 20, 100, 120)],
        ["ROI-1"],
        ["Normal"],
        calibration_path=str(calibration),
        thermal_size=(640, 480),
    )

    assert warning == "캘리브레이션 해상도 불일치"
    assert np.array_equal(projected, visual)


if __name__ == "__main__":
    unittest.main()

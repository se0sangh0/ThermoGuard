import csv
import os
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from thermal_monitoring.analysis.overlay import create_overlay
from thermal_monitoring.capture.capture import CaptureSession
from thermal_monitoring.data.checking import run_check
from thermal_monitoring.data.metadata import run_metadata
from thermal_monitoring.data import pairs


class DataWorkflowTests(unittest.TestCase):
    """GUI-UPDATE: 계획서 기능과 수정 결함을 실데이터 없이 검증한다."""
    def test_capture_urls_use_resolved_camera_ip(self):
        session = CaptureSession(cam_ip=None)
        self.assertNotIn("None", session._urls["thermal"])
        self.assertIn(session.cam_ip, session._urls["thermal"])

    def test_check_uses_selected_directory_and_recovers_missing_npy(self):
        with tempfile.TemporaryDirectory() as dataset_dir:
            jpg_path = os.path.join(dataset_dir, "20260720120000.jpg")
            with open(jpg_path, "wb") as stream:
                stream.write(b"test-jpg")

            thermal = np.full((480, 640), 36.5, dtype=np.float32)
            with patch(
                "thermal_monitoring.data.checking.extract_from_jpeg",
                return_value=(thermal, {}),
            ):
                result = run_check(save_dir=dataset_dir)

            self.assertEqual(result.missing_npy, 1)
            self.assertEqual(result.fixed, 1)
            self.assertTrue(os.path.isfile(
                os.path.join(dataset_dir, "20260720120000_thermal.npy")))

    def test_check_removes_orphan_npy(self):
        with tempfile.TemporaryDirectory() as dataset_dir:
            orphan_path = os.path.join(dataset_dir, "orphan_thermal.npy")
            np.save(orphan_path, np.zeros((2, 2), dtype=np.float32))

            result = run_check(save_dir=dataset_dir)

            self.assertEqual(result.orphan_npy, 1)
            self.assertEqual(result.removed, 1)
            self.assertFalse(os.path.exists(orphan_path))

    def test_metadata_csv_is_created_from_valid_pair(self):
        with tempfile.TemporaryDirectory() as dataset_dir:
            base = "20260720123000"
            with open(os.path.join(dataset_dir, f"{base}.jpg"), "wb") as stream:
                stream.write(b"test-jpg")
            np.save(
                os.path.join(dataset_dir, f"{base}_thermal.npy"),
                np.full((480, 640), 36.0, dtype=np.float32),
            )

            result = run_metadata(save_dir=dataset_dir)

            csv_path = os.path.join(dataset_dir, "metadata.csv")
            self.assertEqual(result.total_pairs, 1)
            self.assertEqual(result.new, 1)
            self.assertTrue(os.path.isfile(csv_path))
            with open(csv_path, "r", encoding="utf-8") as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1][0], base)

    def test_latest_analysis_pair_orders_by_capture_timestamp_across_layouts(self):
        """A newer nested capture must outrank a legacy JPG at dataset root."""
        with tempfile.TemporaryDirectory() as dataset_dir:
            root = os.path.join(dataset_dir, "20260813085603_414398.jpg")
            nested_dir = os.path.join(dataset_dir, "2026-08-13", "12-18")
            os.makedirs(nested_dir)
            nested = os.path.join(nested_dir, "20260813161700_123456.jpg")
            nested_visual = os.path.join(
                nested_dir, "20260813161700_123456_visual.jpg"
            )
            for path in (root, nested, nested_visual):
                with open(path, "wb") as stream:
                    stream.write(b"test-jpg")
            # The legacy root image is old enough to be outside visual grace;
            # selection therefore depends only on capture-time ordering.
            os.utime(root, (1, 1))

            matrix = np.full((2, 2), 31.5, dtype=np.float32)
            with patch(
                "thermal_monitoring.data.pairs.extract_from_jpeg",
                return_value=(matrix, {}),
            ) as extract:
                pair = pairs.latest_analysis_pair(dataset_dir)

            self.assertEqual(pair["base"], "20260813161700_123456")
            self.assertEqual(pair["thermal"], pairs.Path(nested))
            self.assertTrue(pair["npy"].is_file())
            extract.assert_called_once_with(nested)

    def test_latest_complete_pair_orders_nested_and_legacy_by_capture_time(self):
        with tempfile.TemporaryDirectory() as dataset_dir:
            legacy = pairs.Path(dataset_dir) / "20260813085603_414398.jpg"
            legacy_visual = legacy.with_name(f"{legacy.stem}_visual.jpg")
            nested_dir = pairs.Path(dataset_dir) / "2026-08-13" / "12-18"
            nested_dir.mkdir(parents=True)
            nested = nested_dir / "20260813161700_123456.jpg"
            nested_visual = nested.with_name(f"{nested.stem}_visual.jpg")
            for path in (legacy, legacy_visual, nested, nested_visual):
                path.write_bytes(b"test-jpg")

            self.assertEqual(
                pairs.latest_complete_pair(dataset_dir),
                (nested, nested_visual),
            )

    def test_thermal_jpgs_falls_back_to_mtime_for_unknown_names(self):
        with tempfile.TemporaryDirectory() as dataset_dir:
            older = pairs.Path(dataset_dir) / "external-old.jpg"
            newer = pairs.Path(dataset_dir) / "external-new.jpg"
            older.write_bytes(b"test-jpg")
            newer.write_bytes(b"test-jpg")
            os.utime(older, ns=(1_000_000_000, 1_000_000_000))
            os.utime(newer, ns=(2_000_000_000, 2_000_000_000))

            self.assertEqual(pairs.thermal_jpgs(dataset_dir), [older, newer])

    def test_overlay_falls_back_to_thermal_without_visual_image(self):
        with tempfile.TemporaryDirectory() as dataset_dir:
            thermal_path = os.path.join(dataset_dir, "thermal.jpg")
            cv2.imwrite(
                thermal_path,
                np.zeros((480, 640, 3), dtype=np.uint8),
            )

            overlay = create_overlay(
                thermal_jpg_path=thermal_path,
                visual_jpg_path="",
                roi_bounds=(0, 0, 640, 480),
                max_temp=42.0,
                mean_temp=36.0,
                hot_temp=40.0,
                status="Normal",
                homography=np.eye(3, dtype=np.float32),
                hotspot_centroids=[(320, 240, 42.0)],
            )

            self.assertEqual(overlay.shape[:2], (480, 640))


if __name__ == "__main__":
    unittest.main()

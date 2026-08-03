import json
import os
import tempfile
import unittest

from thermal_monitoring.config import (
    DEFAULT_BASELINE_TEMP,
    DEFAULT_CAMERA_IP,
    DEFAULT_DATASET_DIR,
    DEFAULT_TOOLS_MODE,
    NORMAL_CAPTURE_INTERVAL_SEC,
    TEMP_MONITOR_INTERVAL_SEC,
    AppConfig,
    load_config,
    reset_cache,
    save_config,
)


class ConfigPersistenceTests(unittest.TestCase):
    def setUp(self):
        reset_cache()
        self.temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        reset_cache()
        self.temporary_directory.cleanup()

    def test_save_persists_only_local_paths(self):
        config_path = os.path.join(self.temporary_directory.name, "config.json")
        cfg = AppConfig()
        cfg.paths.dataset_dir = "custom_dataset"
        cfg.tools.exiftool_path = "/opt/local/bin/exiftool"
        cfg.camera.ip = "10.0.0.99"
        cfg.roi.baseline_temp = 99.0

        save_config(cfg, config_path)

        with open(config_path, "r", encoding="utf-8") as stream:
            payload = json.load(stream)
        self.assertEqual(set(payload), {"paths", "tools"})
        self.assertEqual(set(payload["tools"]), {"exiftool_path"})
        self.assertEqual(payload["paths"]["dataset_dir"], "custom_dataset")
        self.assertEqual(
            payload["tools"]["exiftool_path"], "/opt/local/bin/exiftool"
        )

    def test_load_ignores_legacy_non_path_values(self):
        config_path = os.path.join(self.temporary_directory.name, "config.json")
        legacy_payload = {
            "camera": {"ip": "1.2.3.4", "capture_interval_sec": 999.0},
            "roi": {"baseline_temp": 100.0},
            "monitoring": {"process_interval_sec": 999.0},
            "paths": {"dataset_dir": "legacy_dataset"},
            "tools": {
                "exiftool_path": "/legacy/exiftool",
                "mode": "thermal",
            },
        }
        with open(config_path, "w", encoding="utf-8") as stream:
            json.dump(legacy_payload, stream)

        cfg = load_config(config_path, force_reload=True)

        self.assertEqual(cfg.paths.dataset_dir, "legacy_dataset")
        self.assertEqual(cfg.tools.exiftool_path, "/legacy/exiftool")
        self.assertEqual(cfg.camera.ip, DEFAULT_CAMERA_IP)
        self.assertEqual(
            cfg.camera.capture_interval_sec, NORMAL_CAPTURE_INTERVAL_SEC
        )
        self.assertEqual(
            cfg.camera.warning_interval_sec, TEMP_MONITOR_INTERVAL_SEC
        )
        self.assertEqual(cfg.roi.baseline_temp, DEFAULT_BASELINE_TEMP)
        self.assertEqual(cfg.tools.mode, DEFAULT_TOOLS_MODE)

    def test_save_updates_cache_for_same_config_path(self):
        config_path = os.path.join(self.temporary_directory.name, "config.json")
        cfg = AppConfig()
        cfg.paths.dataset_dir = "cached_dataset"

        save_config(cfg, config_path)

        self.assertIs(load_config(config_path), cfg)

    def test_force_reload_refreshes_paths_without_losing_runtime_values(self):
        config_path = os.path.join(self.temporary_directory.name, "config.json")
        cfg = AppConfig()
        cfg.camera.ip = "10.20.30.40"
        cfg.roi.baseline_temp = 47.5
        save_config(cfg, config_path)
        with open(config_path, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "paths": {"dataset_dir": "reloaded_dataset"},
                    "camera": {"ip": "legacy-value-must-be-ignored"},
                },
                stream,
            )

        reloaded = load_config(config_path, force_reload=True)

        self.assertIs(reloaded, cfg)
        self.assertEqual(reloaded.paths.dataset_dir, "reloaded_dataset")
        self.assertEqual(reloaded.camera.ip, "10.20.30.40")
        self.assertEqual(reloaded.roi.baseline_temp, 47.5)

    def test_force_reload_clears_removed_local_tool_path(self):
        config_path = os.path.join(self.temporary_directory.name, "config.json")
        cfg = AppConfig()
        cfg.tools.exiftool_path = "/old/exiftool"
        save_config(cfg, config_path)
        with open(config_path, "w", encoding="utf-8") as stream:
            json.dump({"paths": {"dataset_dir": "dataset"}}, stream)

        reloaded = load_config(config_path, force_reload=True)

        self.assertIs(reloaded, cfg)
        self.assertEqual(reloaded.tools.exiftool_path, "")

    def test_force_reload_resets_removed_paths_without_losing_runtime_values(self):
        config_path = os.path.join(self.temporary_directory.name, "config.json")
        cfg = AppConfig()
        cfg.camera.ip = "10.20.30.40"
        cfg.paths.dataset_dir = "old_dataset"
        save_config(cfg, config_path)
        os.remove(config_path)

        reloaded = load_config(config_path, force_reload=True)

        self.assertIs(reloaded, cfg)
        self.assertEqual(reloaded.paths.dataset_dir, DEFAULT_DATASET_DIR)
        self.assertEqual(reloaded.camera.ip, "10.20.30.40")

    def test_atomic_save_creates_nested_parent_without_temp_residue(self):
        nested_directory = os.path.join(
            self.temporary_directory.name, "nested", "settings"
        )
        config_path = os.path.join(nested_directory, "config.json")
        cfg = AppConfig()
        cfg.paths.dataset_dir = "nested_dataset"

        save_config(cfg, config_path)

        self.assertTrue(os.path.isfile(config_path))
        with open(config_path, "r", encoding="utf-8") as stream:
            self.assertEqual(
                json.load(stream)["paths"]["dataset_dir"], "nested_dataset"
            )
        self.assertEqual(
            [name for name in os.listdir(nested_directory) if name.endswith(".tmp")],
            [],
        )


if __name__ == "__main__":
    unittest.main()

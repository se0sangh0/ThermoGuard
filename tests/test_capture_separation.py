"""REST 이미지 캡처와 온도 감시 분리 회귀 테스트."""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


# 개발 환경에 requests가 없어도 실제 HTTP 없이 로직을 검증한다.
try:
    import requests as _requests  # noqa: F401
except ModuleNotFoundError:
    requests_stub = types.ModuleType("requests")
    requests_stub.exceptions = types.SimpleNamespace(
        Timeout=type("Timeout", (Exception,), {}),
        ConnectionError=type("ConnectionError", (Exception,), {}),
    )
    requests_stub.get = Mock(name="requests.get")
    sys.modules["requests"] = requests_stub


from thermal_monitoring.capture.capture import CaptureSession


class CaptureSeparationTests(unittest.TestCase):
    def make_session(self, **kwargs) -> CaptureSession:
        temp_dir = self.enterContext(tempfile.TemporaryDirectory())
        return CaptureSession(
            cam_ip="127.0.0.1",
            save_dir=temp_dir,
            log_callback=lambda _message: None,
            **kwargs,
        )

    def test_warning_mode_does_not_change_rest_interval(self):
        session = self.make_session(interval=17.0)

        session.set_warning_mode(True)
        self.assertTrue(session.warning_mode)
        self.assertEqual(session.interval, 17.0)

        session.set_warning_mode(False)
        self.assertFalse(session.warning_mode)
        self.assertEqual(session.interval, 17.0)

    def test_wait_loop_does_not_call_legacy_probe_callback(self):
        probe_callback = Mock()
        session = self.make_session(interval=13.0, probe_callback=probe_callback)
        session._running = True
        session._stop_event = Mock()
        session._stop_event.wait.return_value = True

        with patch.object(
            session,
            "_capture_images",
            return_value=(("thermal.jpg", None), True),
        ) as capture_images:
            session._run()

        capture_images.assert_called_once_with(["thermal", "visual"])
        session._stop_event.wait.assert_called_once_with(13.0)
        probe_callback.assert_not_called()

    def test_both_mode_regular_capture_keeps_visual_during_warning(self):
        session = self.make_session(mode="both", interval=19.0)
        session.set_warning_mode(True)
        session._running = True
        session._stop_event = Mock()
        session._stop_event.wait.return_value = True
        captured_types: list[list[str]] = []

        def fake_capture(img_types, *, log_label=None):
            self.assertTrue(session._capture_lock.locked())
            captured_types.append(img_types)
            return (("thermal.jpg", "visual.jpg"), True)

        with patch.object(session, "_capture_images", side_effect=fake_capture):
            session._run()

        self.assertEqual(captured_types, [["thermal", "visual"]])
        self.assertEqual(session.last_saved_pair, ("thermal.jpg", "visual.jpg"))
        self.assertEqual(session.interval, 19.0)

    def test_explicit_capture_is_serialized_and_updates_last_pair(self):
        session = self.make_session(mode="both")
        session._running = True
        publish_lock_states: list[bool] = []

        class RecordingLock:
            def __enter__(_self):
                publish_lock_states.append(session._capture_lock.locked())

            def __exit__(_self, _exc_type, _exc, _tb):
                return False

        session._last_pair_lock = RecordingLock()

        def fake_capture(img_types, *, log_label=None):
            self.assertTrue(session._capture_lock.locked())
            self.assertEqual(img_types, ["thermal", "visual"])
            self.assertEqual(log_label, "requested")
            return (("thermal.jpg", "visual.jpg"), True)

        with patch.object(session, "_capture_images", side_effect=fake_capture):
            result = session.capture_both_once()

        self.assertEqual(result, ("thermal.jpg", "visual.jpg"))
        self.assertEqual(session._last_pair, result)
        self.assertEqual(publish_lock_states, [True])

    def test_failed_pair_does_not_write_partial_file(self):
        session = self.make_session(mode="both")
        session._running = True

        def fake_fetch(img_type):
            if img_type == "thermal":
                return img_type, b"thermal", None
            return img_type, None, "[visual] failed"

        with patch.object(session, "_fetch_image", side_effect=fake_fetch):
            result = session.capture_both_once()

        self.assertEqual(result, (None, None))
        self.assertEqual(list(Path(session.save_dir).iterdir()), [])
        self.assertEqual(session.last_saved_pair, (None, None))

    def test_second_file_commit_failure_rolls_back_pair(self):
        session = self.make_session(mode="both")
        session._running = True
        original_replace = os.replace
        replace_count = 0
        commit_targets: list[str] = []

        def fake_fetch(img_type):
            return img_type, img_type.encode("ascii"), None

        def fail_second_replace(source, target):
            nonlocal replace_count
            replace_count += 1
            commit_targets.append(str(target))
            if replace_count == 2:
                raise OSError("simulated final commit failure")
            return original_replace(source, target)

        with (
            patch.object(session, "_fetch_image", side_effect=fake_fetch),
            patch(
                "thermal_monitoring.capture.capture.os.replace",
                side_effect=fail_second_replace,
            ),
        ):
            result = session.capture_both_once()

        self.assertEqual(result, (None, None))
        self.assertTrue(commit_targets[0].endswith("_visual.jpg"))
        self.assertFalse(commit_targets[1].endswith("_visual.jpg"))
        self.assertEqual(list(Path(session.save_dir).iterdir()), [])
        self.assertEqual(session.last_saved_pair, (None, None))

    def test_request_stop_interrupts_interval_wait(self):
        session = self.make_session()
        session._running = True

        session.request_stop()

        self.assertFalse(session.running)
        self.assertTrue(session._stop_event.is_set())


if __name__ == "__main__":
    unittest.main()

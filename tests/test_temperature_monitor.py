from __future__ import annotations

import inspect
import threading
import unittest

from thermal_monitoring.config import TEMP_MONITOR_INTERVAL_SEC
from thermal_monitoring.monitoring.temperature import TemperatureMonitor


class _RecordingEvent:
    def __init__(self, wait_results: list[bool] | None = None) -> None:
        self.waits: list[float | None] = []
        self._set = False
        self._wait_results = iter(wait_results or [True])
        self.wait_called = threading.Event()

    def clear(self) -> None:
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        self.wait_called.set()
        return next(self._wait_results, True)


class TemperatureMonitorTests(unittest.TestCase):
    def test_poll_once_emits_only_state_transitions(self) -> None:
        values = iter([40.0, 51.0, 55.0, 49.0, 48.0])
        samples: list[float] = []
        elevated: list[float] = []
        recovered: list[float] = []
        monitor = TemperatureMonitor(
            lambda: next(values),
            threshold=50.0,
            on_sample=samples.append,
            on_elevated=elevated.append,
            on_recovered=recovered.append,
        )

        for _ in range(5):
            monitor.poll_once()

        self.assertEqual(samples, [40.0, 51.0, 55.0, 49.0, 48.0])
        self.assertEqual(elevated, [51.0])
        self.assertEqual(recovered, [49.0])
        self.assertFalse(monitor.elevated)

    def test_none_and_reader_error_preserve_state_and_later_recover(self) -> None:
        failure = RuntimeError("temporary read failure")
        values = iter([51.0, None, failure, 49.0])
        errors: list[Exception] = []

        def read_temperature() -> float | None:
            value = next(values)
            if isinstance(value, Exception):
                raise value
            return value

        recovered: list[float] = []
        monitor = TemperatureMonitor(
            read_temperature,
            threshold=50.0,
            on_recovered=recovered.append,
            on_error=errors.append,
        )

        self.assertEqual(monitor.poll_once(), 51.0)
        self.assertTrue(monitor.elevated)
        self.assertIsNone(monitor.poll_once())
        self.assertTrue(monitor.elevated)
        self.assertIsNone(monitor.poll_once())
        self.assertTrue(monitor.elevated)
        self.assertEqual(monitor.poll_once(), 49.0)
        self.assertEqual(errors, [failure])
        self.assertEqual(recovered, [49.0])

    def test_callback_errors_are_reported_without_blocking_other_work(self) -> None:
        errors: list[Exception] = []
        elevated: list[float] = []

        def fail_sample(_temperature: float) -> None:
            raise RuntimeError("sample callback failed")

        monitor = TemperatureMonitor(
            lambda: 60.0,
            threshold=50.0,
            on_sample=fail_sample,
            on_elevated=elevated.append,
            on_error=errors.append,
        )

        self.assertEqual(monitor.poll_once(), 60.0)
        self.assertEqual(elevated, [60.0])
        self.assertTrue(monitor.elevated)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)

    def test_on_error_failure_is_suppressed(self) -> None:
        def fail_reader() -> float:
            raise RuntimeError("read failed")

        def fail_error_handler(_error: Exception) -> None:
            raise RuntimeError("error callback failed")

        monitor = TemperatureMonitor(
            fail_reader,
            threshold=50.0,
            on_error=fail_error_handler,
        )

        self.assertIsNone(monitor.poll_once())

    def test_worker_survives_none_reader_and_callback_errors(self) -> None:
        reader_failure = RuntimeError("reader failed")
        values = iter([None, reader_failure, 55.0])
        errors: list[Exception] = []
        elevated: list[float] = []
        elevated_called = threading.Event()
        event = _RecordingEvent(wait_results=[False, False, True])

        def read_temperature() -> float | None:
            value = next(values)
            if isinstance(value, Exception):
                raise value
            return value

        def fail_sample(_temperature: float) -> None:
            raise RuntimeError("sample callback failed")

        def record_elevated(temperature: float) -> None:
            elevated.append(temperature)
            elevated_called.set()

        monitor = TemperatureMonitor(
            read_temperature,
            threshold=50.0,
            on_sample=fail_sample,
            on_elevated=record_elevated,
            on_error=errors.append,
            _event_factory=lambda: event,
        )

        self.assertTrue(monitor.start())
        self.assertTrue(elevated_called.wait(timeout=1.0))
        monitor.stop(timeout=1.0)

        self.assertEqual(len(event.waits), 3)
        self.assertEqual(elevated, [55.0])
        self.assertEqual(len(errors), 2)
        self.assertIs(errors[0], reader_failure)
        self.assertIsInstance(errors[1], RuntimeError)
        self.assertFalse(monitor.running)

    def test_default_interval_is_passed_to_interruptible_event_wait(self) -> None:
        event = _RecordingEvent()
        monitor = TemperatureMonitor(
            lambda: 20.0,
            threshold=50.0,
            _event_factory=lambda: event,
        )

        self.assertTrue(monitor.start())
        self.assertTrue(event.wait_called.wait(timeout=1.0))
        monitor.stop(timeout=1.0)

        self.assertEqual(event.waits, [TEMP_MONITOR_INTERVAL_SEC])
        self.assertEqual(TEMP_MONITOR_INTERVAL_SEC, 5.0)
        self.assertFalse(monitor.running)

    def test_start_is_idempotent_and_stop_wakes_worker(self) -> None:
        entered = threading.Event()

        def read_temperature() -> float:
            entered.set()
            return 20.0

        monitor = TemperatureMonitor(read_temperature, threshold=50.0)

        self.assertTrue(monitor.start())
        self.assertTrue(entered.wait(timeout=1.0))
        self.assertFalse(monitor.start())
        monitor.stop(timeout=1.0)
        self.assertFalse(monitor.running)

    def test_restart_reemits_elevated_transition(self) -> None:
        elevated: list[float] = []
        first_event = _RecordingEvent()
        second_event = _RecordingEvent()
        events = iter([first_event, second_event])
        monitor = TemperatureMonitor(
            lambda: 55.0,
            threshold=50.0,
            on_elevated=elevated.append,
            _event_factory=lambda: next(events),
        )

        self.assertTrue(monitor.start())
        self.assertTrue(first_event.wait_called.wait(timeout=1.0))
        monitor.stop(timeout=1.0)

        # Event는 생성자에서 한 번만 만들어지므로 재시작 가능하도록 테스트
        # 이벤트를 교체한다. 공개 계약은 새 세션의 상태 전이 재발행이다.
        monitor._stop_event = second_event
        self.assertTrue(monitor.start())
        self.assertTrue(second_event.wait_called.wait(timeout=1.0))
        monitor.stop(timeout=1.0)

        self.assertEqual(elevated, [55.0, 55.0])

    def test_module_has_no_capture_or_file_io_dependencies(self) -> None:
        import thermal_monitoring.monitoring.temperature as module

        source = inspect.getsource(module)
        self.assertNotIn("requests", source)
        self.assertNotIn("cv2", source)
        self.assertNotIn("open(", source)
        self.assertNotIn("pathlib", source)


if __name__ == "__main__":
    unittest.main()

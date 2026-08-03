"""온도 공급자와 캡처 파이프라인 사이의 독립적인 감시 루프."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol

from thermal_monitoring.config import TEMP_MONITOR_INTERVAL_SEC


class _StopEvent(Protocol):
    """테스트에서 실제 시간을 기다리지 않기 위한 최소 Event 계약."""

    def is_set(self) -> bool: ...

    def clear(self) -> None: ...

    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


class TemperatureMonitor:
    """주입된 온도 판독기를 일정 주기로 실행하고 임계 전이를 알립니다.

    이 클래스는 GigE SDK, REST API, 이미지 또는 파일 저장을 알지 못합니다.
    실제 카메라 연결 계층은 ``read_temperature`` 호출자에서 별도로 구성합니다.
    """

    def __init__(
        self,
        read_temperature: Callable[[], float | None],
        threshold: float,
        *,
        interval_sec: float = TEMP_MONITOR_INTERVAL_SEC,
        on_sample: Callable[[float], None] | None = None,
        on_elevated: Callable[[float], None] | None = None,
        on_recovered: Callable[[float], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        _event_factory: Callable[[], _StopEvent] = threading.Event,
    ) -> None:
        if interval_sec <= 0:
            raise ValueError("interval_sec must be greater than zero")

        self._read_temperature = read_temperature
        self._threshold = float(threshold)
        self._interval_sec = float(interval_sec)
        self._on_sample = on_sample
        self._on_elevated = on_elevated
        self._on_recovered = on_recovered
        self._on_error = on_error
        self._stop_event = _event_factory()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._elevated = False

    @property
    def running(self) -> bool:
        """감시 워커 스레드가 실행 중이면 ``True``입니다."""

        with self._state_lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def elevated(self) -> bool:
        """마지막 유효 샘플이 임계 상태이면 ``True``입니다."""

        with self._state_lock:
            return self._elevated

    def _report_error(self, error: Exception) -> None:
        if self._on_error is None:
            return
        try:
            self._on_error(error)
        except Exception:
            # 오류 보고 콜백 자체의 실패가 감시 워커를 중단시키면 안 됩니다.
            pass

    def _notify(self, callback: Callable[[float], None] | None, value: float) -> None:
        if callback is None:
            return
        try:
            callback(value)
        except Exception as exc:
            self._report_error(exc)

    def poll_once(self) -> float | None:
        """온도를 한 번 읽고 필요한 콜백과 상태 전이를 수행합니다.

        판독 실패나 콜백 실패는 ``on_error``로 전달한 뒤 삼킵니다. ``None``은
        유효한 샘플이 없는 것으로 보며 기존 임계 상태를 변경하지 않습니다.
        """

        try:
            sample = self._read_temperature()
        except Exception as exc:
            self._report_error(exc)
            return None

        if sample is None:
            return None

        try:
            temperature = float(sample)
        except (TypeError, ValueError) as exc:
            self._report_error(exc)
            return None

        self._notify(self._on_sample, temperature)

        with self._state_lock:
            was_elevated = self._elevated
            is_elevated = temperature >= self._threshold
            self._elevated = is_elevated

        if is_elevated and not was_elevated:
            self._notify(self._on_elevated, temperature)
        elif was_elevated and not is_elevated:
            self._notify(self._on_recovered, temperature)
        return temperature

    def start(self) -> bool:
        """워커를 시작합니다. 이미 실행 중이면 새 워커를 만들지 않습니다."""

        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            # 재시작은 새 감시 세션이다. 직전 세션이 elevated 상태에서
            # 종료됐더라도 첫 고온 샘플의 on_elevated 전이를 다시 전달한다.
            self._elevated = False
            thread = threading.Thread(
                target=self._run,
                name="temperature-monitor",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        return True

    def stop(self, timeout: float | None = None) -> None:
        """대기 중인 워커를 즉시 깨우고 종료될 때까지 기다립니다."""

        self._stop_event.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    self.poll_once()
                except Exception as exc:
                    # 향후 poll_once가 확장되더라도 워커 생존 계약을 지킵니다.
                    self._report_error(exc)
                if self._stop_event.wait(self._interval_sec):
                    break
        finally:
            with self._state_lock:
                if self._thread is threading.current_thread():
                    self._thread = None

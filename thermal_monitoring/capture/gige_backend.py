"""
gige_backend.py - FLIR A50 GigE Vision 영구 온도 판독기

Spinnaker(PySpin) SDK를 통해 GigE Vision 프로토콜로 FLIR A50 카메라에
직접 연결하고, Mono16 radiometric 프레임을 지속적으로 수신해 최신
온도값을 스레드 안전하게 노출한다.

TemperatureMonitor의 ``read_temperature`` 콜백으로 주입 가능.

의존성: PySpin (Spinnaker SDK, Windows/Linux), 미설치 시 gracefully degrade
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import numpy as np

from ..logger import get_logger

_log = get_logger("capture.gige")

try:
    import PySpin
    _PYSPIN_AVAILABLE = True
except ImportError:
    _PYSPIN_AVAILABLE = False
    PySpin = None


# TemperatureLinear10mK 변환 상수
_TEMP_SCALE = 0.01       # raw * 0.01 = Kelvin
_KELVIN_OFFSET = 273.15  # Kelvin - 273.15 = Celsius

# 재연결 파라미터
_MAX_RECONNECT_ATTEMPTS = 3
_RECONNECT_DELAY_SEC = 2.0
_FRAME_TIMEOUT_MS = 2000
_CONNECTION_STALE_SEC = 10.0

# A50 Radiometric 해상도
_A50_WIDTH = 464
_A50_HEIGHT = 348


class GigeTemperatureReader:
    """FLIR A50 GigE Vision 영구 온도 판독기.

    백그라운드 데몬 스레드에서 카메라 연결 -> Mono16 스트리밍 -> 프레임별
    온도 변환을 처리하고, ``read_temperature()``로 최신 온도값을 노출한다.
    연결이 끊기면 자동으로 재연결을 시도한다.
    """

    def __init__(
        self,
        device_index: int = 0,
        roi_bounds: tuple[int, int, int, int] | None = None,
    ) -> None:
        self._device_index = device_index
        self._roi_bounds = roi_bounds
        self._latest_temp: float | None = None
        self._temp_lock = threading.Lock()
        self._last_frame_time: float = 0.0
        self._running = False
        self._thread: threading.Thread | None = None

        # Spinnaker 리소스
        self._system: "PySpin.System" | None = None
        self._cam: "PySpin.Camera" | None = None
        self._cam_list: "PySpin.CameraList" | None = None

    # ── 공개 프로퍼티 ────────────────────────────────────────

    @property
    def connected(self) -> bool:
        """마지막 프레임이 최근이면 ``True``."""
        if self._last_frame_time == 0.0:
            return False
        return (time.monotonic() - self._last_frame_time) < _CONNECTION_STALE_SEC

    def read_temperature(self) -> float | None:
        """최신 온도값 (C)을 반환한다. ``TemperatureMonitor`` 콜백용."""
        with self._temp_lock:
            return self._latest_temp

    # ── 생명주기 ──────────────────────────────────────────────

    def start(self) -> bool:
        """GigE 카메라 연결을 초기화하고 백그라운드 수집 스레드를 시작한다.

        Returns:
            연결 성공 시 ``True``, PySpin 미설치 또는 장치 없음이면 ``False``.
        """
        if not _PYSPIN_AVAILABLE:
            _log.warning("PySpin not available - GigE temperature reader disabled")
            return False

        self._running = True
        if not self._connect():
            self._running = False
            return False

        self._thread = threading.Thread(
            target=self._acquisition_loop,
            name="gige-temperature-reader",
            daemon=True,
        )
        self._thread.start()
        _log.info("GigE temperature reader started (device=%d)", self._device_index)
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """백그라운드 수집을 중지하고 카메라 연결을 종료한다."""
        _log.info("GigE temperature reader stop requested")
        self._running = False
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None
        self._cleanup()
        _log.info("GigE temperature reader stopped")

    # ── 내부: 연결 ──────────────────────────────────────────

    def _connect(self) -> bool:
        """카메라 검색 -> 연결 -> 설정."""
        try:
            self._system = PySpin.System.GetInstance()
            self._cam_list = self._system.GetCameras()
            n = self._cam_list.GetSize()
            if n == 0:
                _log.warning("No GigE camera found")
                self._cleanup()
                return False
            if self._device_index >= n:
                _log.error(
                    "Device index %d out of range (%d devices found)",
                    self._device_index, n,
                )
                self._cleanup()
                return False

            self._cam = self._cam_list.GetByIndex(self._device_index)
            self._cam.Init()
            nm = self._cam.GetNodeMap()

            # 카메라 정보
            model = PySpin.CStringPtr(nm.GetNode("DeviceModelName")).GetValue()
            sn = PySpin.CStringPtr(nm.GetNode("DeviceSerialNumber")).GetValue()
            _log.info("Camera: %s (SN: %s)", model, sn)

            # IRFormat -> TemperatureLinear10mK (0.01K 단위)
            ir_node = PySpin.CEnumerationPtr(nm.GetNode("IRFormat"))
            ir_node.FromString("TemperatureLinear10mK")
            _log.info("IRFormat: %s", ir_node.GetCurrentEntry().GetSymbolic())

            # 해상도 설정
            PySpin.CIntegerPtr(nm.GetNode("Width")).SetValue(_A50_WIDTH)
            PySpin.CIntegerPtr(nm.GetNode("Height")).SetValue(_A50_HEIGHT)
            PySpin.CIntegerPtr(nm.GetNode("OffsetX")).SetValue(0)
            PySpin.CIntegerPtr(nm.GetNode("OffsetY")).SetValue(0)

            # 연속 획득 모드
            self._cam.AcquisitionMode.SetValue(PySpin.AcquisitionMode_Continuous)
            self._cam.TLStream.StreamBufferHandlingMode.SetValue(
                PySpin.StreamBufferHandlingMode_NewestOnly
            )

            self._cam.BeginAcquisition()
            _log.info("GigE acquisition started (Continuous, %dx%d)", _A50_WIDTH, _A50_HEIGHT)
            return True

        except Exception as exc:
            _log.error("GigE connection failed: %s", exc, exc_info=True)
            self._cleanup()
            return False

    def _reconnect(self) -> bool:
        """연결이 끊겼을 때 최대 3회 재시도."""
        _log.warning("GigE connection lost - attempting reconnect")
        self._cleanup()
        for attempt in range(1, _MAX_RECONNECT_ATTEMPTS + 1):
            _log.info("Reconnect attempt %d/%d", attempt, _MAX_RECONNECT_ATTEMPTS)
            time.sleep(_RECONNECT_DELAY_SEC)
            if not self._running:
                return False
            if self._connect():
                _log.info("GigE reconnected successfully")
                return True
        _log.error("GigE reconnect failed after %d attempts", _MAX_RECONNECT_ATTEMPTS)
        return False

    def _cleanup(self) -> None:
        """Spinnaker 리소스를 안전하게 정리한다."""
        cam, self._cam = self._cam, None
        if cam is not None:
            try:
                cam.EndAcquisition()
            except Exception:
                pass
            try:
                cam.DeInit()
            except Exception:
                pass
            del cam

        cam_list, self._cam_list = self._cam_list, None
        if cam_list is not None:
            try:
                cam_list.Clear()
            except Exception:
                pass
            del cam_list

        system, self._system = self._system, None
        if system is not None:
            try:
                system.ReleaseInstance()
            except Exception:
                pass
            del system

    # ── 내부: 수집 루프 ─────────────────────────────────────

    def _acquisition_loop(self) -> None:
        """백그라운드 데몬 스레드의 메인 루프."""
        while self._running:
            cam = self._cam
            if cam is None:
                _log.warning("GigE camera reference lost - exiting acquisition loop")
                self._running = False
                break

            try:
                img = cam.GetNextImage(_FRAME_TIMEOUT_MS)
            except PySpin.SpinnakerException as exc:
                _log.error("GigE GetNextImage failed: %s", exc)
                if self._running:
                    if self._reconnect():
                        continue
                break

            if img is None or img.IsIncomplete():
                status = "timeout" if img is None else img.GetImageStatus()
                _log.warning("GigE frame incomplete: %s - attempting reconnect", status)
                if img is not None:
                    try:
                        img.Release()
                    except Exception:
                        pass
                if self._running:
                    if self._reconnect():
                        continue
                break

            try:
                data = img.GetData()
                raw = np.frombuffer(data, dtype=np.uint16).reshape(
                    img.GetHeight(), img.GetWidth()
                )

                # TemperatureLinear10mK -> Celsius
                temp_image = raw.astype(np.float32) * _TEMP_SCALE - _KELVIN_OFFSET

                # ROI 또는 전체 프레임 최대 온도 추출
                if self._roi_bounds is not None:
                    x1, y1, x2, y2 = self._roi_bounds
                    h, w = temp_image.shape
                    x1 = max(0, min(x1, w - 1))
                    y1 = max(0, min(y1, h - 1))
                    x2 = max(x1 + 1, min(x2, w))
                    y2 = max(y1 + 1, min(y2, h))
                    roi = temp_image[y1:y2, x1:x2]
                    max_temp = float(np.max(roi))
                else:
                    max_temp = float(np.max(temp_image))

                with self._temp_lock:
                    self._latest_temp = max_temp
                self._last_frame_time = time.monotonic()

            except Exception as exc:
                _log.error("GigE frame processing error: %s", exc, exc_info=True)
                if self._running:
                    if self._reconnect():
                        continue
                break
            finally:
                try:
                    img.Release()
                except Exception:
                    pass

        _log.info("GigE acquisition loop exited")

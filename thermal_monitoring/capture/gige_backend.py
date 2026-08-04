"""
gige_backend.py - FLIR A50 GigE Vision 온도 판독기

Spinnaker(PySpin) SDK를 통해 GigE Vision 프로토콜로 FLIR A50 카메라에
직접 연결하고, Mono16 radiometric 프레임을 지속적으로 수신해 최신
온도값을 스레드 안전하게 노출한다.

의존성: PySpin (Spinnaker SDK), 미설치 시 gracefully degrade
"""

from __future__ import annotations

import threading
import time

import numpy as np

from ..logger import get_logger

_log = get_logger("capture.gige")

try:
    import PySpin
    _PYSPIN_AVAILABLE = True
except ImportError:
    _PYSPIN_AVAILABLE = False
    PySpin = None

_TEMP_SCALE = 0.01
_KELVIN_OFFSET = 273.15
_MAX_RECONNECT_ATTEMPTS = 3
_RECONNECT_DELAY_SEC = 2.0
_FRAME_TIMEOUT_MS = 2000
_CONNECTION_STALE_SEC = 10.0
_A50_WIDTH = 464
_A50_HEIGHT = 348


class GigeTemperatureReader:
    """FLIR A50 GigE Vision 온도 판독기.

    백그라운드 데몬 스레드에서 카메라 연결 -> Mono16 스트리밍 -> 온도 변환을
    처리하고, ``read_temperature()``로 최신 최대 온도값을 노출한다.
    """

    def __init__(self, device_index: int = 0) -> None:
        self._device_index = device_index
        self._latest_temp: float | None = None
        self._temp_lock = threading.Lock()
        self._last_frame_time: float = 0.0
        self._running = False
        self._thread: threading.Thread | None = None
        self._system = None
        self._cam = None
        self._cam_list = None

    @property
    def connected(self) -> bool:
        if self._last_frame_time == 0.0:
            return False
        return (time.monotonic() - self._last_frame_time) < _CONNECTION_STALE_SEC

    def read_temperature(self) -> float | None:
        """최신 온도값 (C). 항상 스레드 안전하게 반환한다."""
        with self._temp_lock:
            return self._latest_temp

    def start(self) -> bool:
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
        _log.info("GigE temperature reader stop requested")
        self._running = False
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None
        self._cleanup()
        _log.info("GigE temperature reader stopped")

    def _connect(self) -> bool:
        try:
            self._system = PySpin.System.GetInstance()
            self._cam_list = self._system.GetCameras()
            n = self._cam_list.GetSize()
            if n == 0:
                _log.warning("No GigE camera found")
                self._cleanup()
                return False
            if self._device_index >= n:
                _log.error("Device index %d out of range (%d devices)", self._device_index, n)
                self._cleanup()
                return False
            self._cam = self._cam_list.GetByIndex(self._device_index)
            self._cam.Init()
            nm = self._cam.GetNodeMap()
            model = PySpin.CStringPtr(nm.GetNode("DeviceModelName")).GetValue()
            sn = PySpin.CStringPtr(nm.GetNode("DeviceSerialNumber")).GetValue()
            _log.info("Camera: %s (SN: %s)", model, sn)
            ir_node = PySpin.CEnumerationPtr(nm.GetNode("IRFormat"))
            ir_node.FromString("TemperatureLinear10mK")
            _log.info("IRFormat: %s", ir_node.GetCurrentEntry().GetSymbolic())
            PySpin.CIntegerPtr(nm.GetNode("Width")).SetValue(_A50_WIDTH)
            PySpin.CIntegerPtr(nm.GetNode("Height")).SetValue(_A50_HEIGHT)
            PySpin.CIntegerPtr(nm.GetNode("OffsetX")).SetValue(0)
            PySpin.CIntegerPtr(nm.GetNode("OffsetY")).SetValue(0)
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

    def _acquisition_loop(self) -> None:
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
                if self._running and self._reconnect():
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
                if self._running and self._reconnect():
                    continue
                break
            try:
                data = img.GetData()
                raw = np.frombuffer(data, dtype=np.uint16).reshape(img.GetHeight(), img.GetWidth())
                temp_image = raw.astype(np.float32) * _TEMP_SCALE - _KELVIN_OFFSET
                max_temp = float(np.max(temp_image))
                with self._temp_lock:
                    self._latest_temp = max_temp
                self._last_frame_time = time.monotonic()
            except Exception as exc:
                _log.error("GigE frame processing error: %s", exc, exc_info=True)
                if self._running and self._reconnect():
                    continue
                break
            finally:
                try:
                    img.Release()
                except Exception:
                    pass
        _log.info("GigE acquisition loop exited")

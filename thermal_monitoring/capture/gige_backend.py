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

from ..config import factory_mode_enabled
from ..logger import get_logger
from ..runtime_lock import (
    DashboardRuntimeAuthorizationError,
    dashboard_runtime_authorized,
)

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
        # SDK objects must have one clear owner while acquisition is active.
        # In particular, ``System.ReleaseInstance`` must never race a worker
        # blocked in GetNextImage().
        self._lifecycle_lock = threading.RLock()
        self._last_frame_time: float = 0.0
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._stopped_event = threading.Event()
        self._stopped_event.set()
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

    @property
    def stopped(self) -> bool:
        """Whether the acquisition worker has exited and released its SDK objects."""
        return self._stopped_event.is_set()

    @property
    def running(self) -> bool:
        """Whether acquisition has not yet received a stop request."""
        return self._running and not self._stop_event.is_set()

    def start(self) -> bool:
        if factory_mode_enabled() and not dashboard_runtime_authorized():
            raise DashboardRuntimeAuthorizationError(
                "Factory GigE acquisition is authorized only through the active "
                "ThermoGuard dashboard runtime."
            )
        if not _PYSPIN_AVAILABLE:
            _log.warning("PySpin not available - GigE temperature reader disabled")
            return False

        with self._lifecycle_lock:
            # Do not replace an SDK session until its worker has fully exited.
            # A stop timeout is intentionally a pending state, not permission
            # to start a second reader for the same camera.
            if not self._stopped_event.is_set():
                _log.warning("GigE reader is still stopping; start request ignored")
                return False

            self._running = True
            self._stop_event.clear()
            self._stopped_event.clear()
            if not self._connect():
                self._running = False
                self._stop_event.set()
                self._stopped_event.set()
                return False

            self._thread = threading.Thread(
                target=self._run_worker,
                name="gige-temperature-reader",
                daemon=True,
            )
            try:
                self._thread.start()
            except Exception:
                # The thread has not started, so this is the one case where
                # the caller may release the just-created SDK objects.
                self._running = False
                self._stop_event.set()
                self._cleanup()
                self._thread = None
                self._stopped_event.set()
                _log.exception("GigE acquisition worker could not start")
                return False
        _log.info("GigE temperature reader started (device=%d)", self._device_index)
        return True

    def request_stop(self) -> None:
        """Request a stop without releasing SDK resources from the caller.

        The acquisition worker owns cleanup after it leaves camera I/O.  This
        makes a stuck SDK call observable as a pending stop instead of turning
        it into a use-after-release race.
        """
        with self._lifecycle_lock:
            if self._stopped_event.is_set():
                return
            self._running = False
            self._stop_event.set()
        _log.info("GigE temperature reader stop requested")

    def wait_stopped(self, timeout: float | None = None) -> bool:
        """Wait until the worker has exited *and* released its SDK objects."""
        thread = self._thread
        if thread is threading.current_thread():
            # A worker cannot wait for its own finally/cleanup block.
            return False
        if not self._stopped_event.wait(timeout):
            return False
        if thread is not None:
            thread.join(timeout=0)
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        """Request shutdown and return whether it completed within ``timeout``.

        False means cleanup is still owned by the live acquisition worker.  A
        caller must retain this reader and wait again rather than releasing or
        replacing the SDK session.
        """
        self.request_stop()
        if self.wait_stopped(timeout=timeout):
            _log.info("GigE temperature reader stopped")
            return True
        _log.error(
            "GigE reader stop is still pending after %.1fs; SDK resources remain owned by the worker",
            timeout,
        )
        return False

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
            if self._stop_event.wait(_RECONNECT_DELAY_SEC):
                return False
            if self._connect():
                _log.info("GigE reconnected successfully")
                return True
        _log.error("GigE reconnect failed after %d attempts", _MAX_RECONNECT_ATTEMPTS)
        return False

    def _cleanup(self) -> bool:
        """Release SDK objects only from a safe owner context.

        The normal callers are the acquisition worker (reconnect/finally) and
        ``start`` before any worker exists.  The guard is deliberately kept
        here as a defence against a future caller reintroducing cleanup from a
        UI or shutdown thread.
        """
        with self._lifecycle_lock:
            worker = self._thread
            if worker is not None and worker.is_alive() and worker is not threading.current_thread():
                _log.error("Refusing GigE SDK cleanup while acquisition worker is alive")
                return False

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
        return True

    def _run_worker(self) -> None:
        """Own the worker lifecycle so cleanup follows the final camera call."""
        try:
            self._acquisition_loop()
        except Exception:
            _log.exception("GigE acquisition worker crashed")
        finally:
            with self._lifecycle_lock:
                self._running = False
                self._stop_event.set()
            try:
                self._cleanup()
            finally:
                with self._lifecycle_lock:
                    if self._thread is threading.current_thread():
                        self._thread = None
                    self._stopped_event.set()
            _log.info("GigE acquisition worker fully stopped")

    def _acquisition_loop(self) -> None:
        while self._running and not self._stop_event.is_set():
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

            if img is None:
                _log.warning("GigE frame incomplete: timeout - attempting reconnect")
                if self._running and self._reconnect():
                    continue
                break

            reconnect_needed = False
            try:
                if img.IsIncomplete():
                    _log.warning(
                        "GigE frame incomplete: %s - attempting reconnect",
                        img.GetImageStatus(),
                    )
                    reconnect_needed = True
                else:
                    data = img.GetData()
                    raw = np.frombuffer(data, dtype=np.uint16).reshape(
                        img.GetHeight(), img.GetWidth()
                    )
                    temp_image = raw.astype(np.float32) * _TEMP_SCALE - _KELVIN_OFFSET
                    max_temp = float(np.max(temp_image))
                    with self._temp_lock:
                        self._latest_temp = max_temp
                    self._last_frame_time = time.monotonic()
            except Exception as exc:
                _log.error("GigE frame processing error: %s", exc, exc_info=True)
                reconnect_needed = True
            finally:
                try:
                    img.Release()
                except Exception:
                    pass

            # A PySpin image handle must be released before EndAcquisition /
            # DeInit in _reconnect.  This ordering matters even though both
            # operations run in the same acquisition worker.
            if reconnect_needed:
                if self._running and self._reconnect():
                    continue
                break
        _log.info("GigE acquisition loop exited")

"""
capture.py - FLIR A50 이미지 캡처 모듈

import해서 사용하거나 tools.py GUI에서 호출할 수 있도록 리팩터링되었습니다.

사용법 (스크립트):
    python capture.py

사용법 (import):
    from capture import CaptureSession
    session = CaptureSession(cam_ip="192.168.0.51", mode="both", interval=10.0)
    session.start()
    ...
    session.stop()
"""

import os
import math
import threading
import time
from datetime import datetime
from typing import Optional

import requests

from ..config import (
    AppConfig,
    bounded_backend_timeout,
    factory_mode_enabled,
    load_config,
    resolve_runtime_path,
)
from ..data.pairs import capture_subdir
from ..logger import get_logger
from ..runtime_lock import (
    DashboardRuntimeAuthorizationError,
    dashboard_runtime_authorized,
)
from .thermal_utils import probe_thermal_from_url

_log = get_logger("capture")


def camera_image_url(cam_ip: str, *, visual: bool = False) -> str:
    """FLIR A50 현재 이미지 엔드포인트 URL. thermal/visual 공용 (URL 중복 제거)."""
    fmt = "JPEG_visual" if visual else "JPEG"
    return f"http://{cam_ip}/api/image/current?imgformat={fmt}"

# 카메라 REST API의 일시적 오류(서버 busy·동시 요청 등) → 짧은 백오프로 재시도.
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_TRANSIENT_RETRIES = 2               # 최초 1회 + 추가 재시도 2회
_RETRY_BACKOFF_SEC = (0.5, 1.0)          # 재시도 회차별 대기
_MAX_RETRY_AFTER_SEC = 30.0              # Retry-After 헤더 반영 상한
_MAX_SHARED_BACKOFF_SEC = 30.0
_DISCONNECT_FAILURE_THRESHOLD = 3


class CaptureSession:
    def __init__(
        self,
        cam_ip: str | None = None,
        mode: str | None = None,
        interval: float | None = None,
        save_dir: str | None = None,
        log_callback=None,
        probe_callback=None,
        status_callback=None,
        probe_interval: float | None = None,
        cfg: AppConfig | None = None,
    ):
        # The dashboard has already strict-loaded this exact configuration.
        # Passing it avoids a second, legacy non-strict load during a factory
        # session.  Any direct factory consumer must strict-load an approved
        # config, so it cannot create a default file as a side effect.
        cfg = cfg or load_config(
            force_reload=factory_mode_enabled(),
            strict=factory_mode_enabled(),
        )
        self.cam_ip = cam_ip or cfg.camera.ip
        self.mode = mode or cfg.tools.mode      # "thermal" or "both"
        self.interval = float(
            cfg.camera.capture_interval_sec if interval is None else interval
        )
        self.save_dir = str(resolve_runtime_path(save_dir or cfg.paths.dataset_dir))
        self.log_callback = log_callback  # callable(str) for GUI output
        self.probe_callback = probe_callback  # callable(float) — max_temp을 받아 Warning 이상이면 True 반환
        self.status_callback = status_callback  # callable(state, detail)
        self._running = False
        self._thread = None
        self._lifecycle_lock = threading.Lock()
        self._capture_io_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._stopped_event = threading.Event()
        self._stopped_event.set()
        self._consecutive_failures = 0
        self._was_connected = False
        self._transient_streak = 0
        self._next_allowed_request = 0.0
        # Full thermal+visual file capture and the metadata-only temperature
        # probe are two independent cadences.  The backend's historical
        # normal/warning interval fields map to these 30s/5s roles.
        self._normal_interval = self.interval
        self.probe_interval = float(
            cfg.camera.warning_interval_sec
            if probe_interval is None else probe_interval
        )
        if not math.isfinite(self.interval) or self.interval <= 0:
            raise ValueError("capture interval must be a finite positive number")
        if not math.isfinite(self.probe_interval) or self.probe_interval <= 0:
            raise ValueError("probe interval must be a finite positive number")
        self._warning_interval = self.probe_interval
        self._probe_elevated = False
        self._interval_lock = threading.Lock()
        # 가장 최근 캡처 사이클에서 저장된 (thermal, visual) 경로. 알람 오버레이가
        # 카메라를 다시 치지 않고 최신 프레임을 재사용할 수 있게 노출한다.
        self._last_pair: tuple[str | None, str | None] = (None, None)
        self._last_pair_lock = threading.Lock()
        self._backend_enabled = cfg.backend.enabled
        self._backend_url = cfg.backend.url
        self._backend_timeout = bounded_backend_timeout(cfg.backend.timeout_sec)
        self._db_camera_id = cfg.identity.db_camera_id
        # GUI-UPDATE: cam_ip 인자가 None이어도 config에서 확정된 self.cam_ip를 사용한다.
        self._urls = {
            "thermal": camera_image_url(self.cam_ip),
            "visual": camera_image_url(self.cam_ip, visual=True),
        }
        _log.info("CaptureSession initialized: ip=%s mode=%s interval=%.1fs save_dir=%s",
                  self.cam_ip, self.mode, self.interval, self.save_dir)

    def _report_connection_status_async(self, status: str) -> None:
        """Keep optional DB status reporting out of the capture shutdown path."""

        threading.Thread(
            target=self._report_connection_status,
            args=(status,),
            name="capture-status-report",
            daemon=True,
        ).start()

    def _log(self, msg: str):
        if self.log_callback:
            self.log_callback(msg)
        else:
            print(msg)

    def _notify_status(self, state: str, detail: str) -> None:
        callback = self.status_callback
        if callback is not None:
            try:
                callback(state, detail)
            except Exception:
                _log.debug("capture status callback failed", exc_info=True)

    def _report_connection_status(self, status: str) -> None:
        if not self._backend_enabled or self._db_camera_id is None:
            return
        try:
            requests.patch(
                f"{self._backend_url}/api/cameras/{self._db_camera_id}/status",
                json={"connection_status": status},
                timeout=self._backend_timeout,
            )
        except requests.RequestException:
            _log.debug("camera status API unavailable", exc_info=True)

    def start(self):
        if factory_mode_enabled() and not dashboard_runtime_authorized():
            raise DashboardRuntimeAuthorizationError(
                "Factory camera capture is authorized only through the active "
                "ThermoGuard dashboard runtime. Start the approved dashboard "
                "launcher instead of importing CaptureSession directly."
            )
        with self._lifecycle_lock:
            if self._running:
                _log.warning("Capture session already running — ignored start()")
                self._log("[capture] Already running.")
                return
            if self._thread is not None and self._thread.is_alive():
                _log.warning("Capture session is still stopping — ignored start()")
                self._log("[capture] Previous session is still stopping.")
                return
            self._running = True
            self._probe_elevated = False
            self._stop_event.clear()
            self._stopped_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        _log.info("Capture started: interval=%.1fs mode=%s", self.interval, self.mode)
        self._log(f"[capture] Started (interval={self.interval}s, mode={self.mode})")

    def stop(self):
        _log.info("Capture stop requested")
        self.request_stop()
        timeout = max(5.0, float(self.interval) + 5.0)
        if self.wait_stopped(timeout=timeout):
            _log.info("Capture stopped (consecutive_failures=%d)", self._consecutive_failures)
            self._log("[capture] Stopped.")
        else:
            _log.error("Capture did not stop within %.1fs", timeout)
            self._log("[capture] Stop is still pending.")

    def request_stop(self):
        """캡처 중단 요청만 하고 join은 하지 않는다 (UI 블로킹 방지)."""
        _log.info("Capture stop requested (non-blocking)")
        with self._lifecycle_lock:
            self._running = False
            self._stop_event.set()
        self._log("[capture] Stop requested (non-blocking).")

    def wait_stopped(self, timeout: float | None = None) -> bool:
        """Wait until the capture thread has fully left camera I/O.

        A caller must not start a replacement session before this returns true:
        otherwise two sessions can issue overlapping requests to one camera.
        """
        if self._stopped_event.wait(timeout):
            thread = self._thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=0)
            return True
        return False

    @property
    def stopped(self) -> bool:
        return self._stopped_event.is_set()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def warning_mode(self) -> bool:
        """Compatibility property; file capture no longer becomes thermal-only."""
        return False

    @property
    def last_saved_pair(self) -> tuple[str | None, str | None]:
        """가장 최근 캡처 사이클에서 저장된 (thermal, visual) 경로.

        경고 모드(thermal-only 캡처)면 visual은 None, 아직 캡처 전이면 (None, None).
        오버레이 생성 시 카메라 추가 접근 없이 최신 프레임을 쓰기 위한 용도.
        """
        with self._last_pair_lock:
            return self._last_pair

    def capture_both_once(self) -> tuple[str | None, str | None]:
        """알람용 일회성 캡처: thermal 후 visual 직렬 요청·저장.

        Returns:
            (thermal_jpg_path, visual_jpg_path) — 실패 시 (None, None)
            visual_jpg_path는 mode가 'thermal'이면 None.
        """
        if not self._running:
            _log.warning("capture_both_once: session not running")
            return (None, None)

        # The regular loop and manual refresh share one camera.  Serialize the
        # complete request/write transaction so an operator click cannot race a
        # periodic capture and produce mismatched files or camera overload.
        with self._capture_io_lock:
            if not self._running:
                return (None, None)
            do_visual = self.mode == "both"
            now = datetime.now()
            filenametime = now.strftime("%Y%m%d%H%M%S_%f")
            save_subdir = capture_subdir(self.save_dir, now)
            os.makedirs(save_subdir, exist_ok=True)

            results: dict[str, str | None] = {"thermal": None, "visual": None}
            img_types = ["thermal", "visual"] if do_visual else ["thermal"]
            retry_budget = [_MAX_TRANSIENT_RETRIES]
            for img_type in img_types:
                if self._stop_event.is_set():
                    break
                _, content, error = self._fetch_image(
                    img_type,
                    retry_budget=retry_budget,
                )
                if error or content is None:
                    _log.warning("capture_both_once: %s failed", img_type)
                    self._log(error or f"[{img_type}] capture failed")
                    break
                suffix = "_visual" if img_type == "visual" else ""
                jpg_path = os.path.join(save_subdir, f"{filenametime}{suffix}.jpg")
                with open(jpg_path, "wb") as f:
                    f.write(content)
                results[img_type] = jpg_path
                self._log(
                    f"[{datetime.now().strftime('%H:%M:%S')}] [alarm] "
                    f"[{img_type}] saved ({len(content)} bytes)"
                )

            if results["thermal"]:
                with self._last_pair_lock:
                    self._last_pair = (results["thermal"], results.get("visual"))

            return (results["thermal"], results.get("visual"))

    def set_warning_mode(self, active: bool) -> None:
        """Deprecated compatibility hook.

        Warning detection is now a metadata-only probe at ``probe_interval``;
        it must never mutate the independent full-file capture cadence.
        """
        if not active:
            self._probe_elevated = False

    def _retry_delay(self, resp, attempt: int) -> float:
        """재시도 대기 시간. Retry-After(초) 헤더가 있으면 반영하되 상한으로 캡."""
        base = _RETRY_BACKOFF_SEC[min(attempt, len(_RETRY_BACKOFF_SEC) - 1)]
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                base = max(base, float(retry_after))  # 초 단위 형식만 처리 (HTTP-date는 무시)
            except ValueError:
                pass
        return min(base, _MAX_RETRY_AFTER_SEC)

    def _wait_for_request_window(self) -> bool:
        delay = max(0.0, self._next_allowed_request - time.monotonic())
        return bool(delay and self._stop_event.wait(delay))

    def _record_transient(self, delay: float, detail: str) -> None:
        self._transient_streak += 1
        circuit_delay = min(
            _MAX_SHARED_BACKOFF_SEC,
            max(delay, 0.5 * (2 ** min(self._transient_streak - 1, 6))),
        )
        self._next_allowed_request = max(
            self._next_allowed_request,
            time.monotonic() + circuit_delay,
        )
        _log.warning(
            "camera transient attempt=%d; shared backoff %.1fs (%s)",
            self._transient_streak,
            circuit_delay,
            detail,
        )
        self._notify_status("backoff", f"{detail}; retry in {circuit_delay:.1f}s")

    def _record_request_success(self) -> None:
        recovered = self._transient_streak > 0
        self._transient_streak = 0
        self._next_allowed_request = 0.0
        if recovered:
            _log.info("Camera HTTP request recovered")
            self._notify_status("recovered", "camera HTTP recovered")

    def _fetch_image(
        self,
        img_type: str,
        *,
        retry_budget: list[int] | None = None,
    ) -> tuple[str, bytes | None, str | None]:
        """단일 이미지 캡처 (thermal/visual 공용). 503 등 일시적 오류는 짧은 백오프로 재시도.

        Returns:
            (img_type, content_bytes | None, error_str | None)
        """
        url = self._urls[img_type]
        if retry_budget is None:
            retry_budget = [_MAX_TRANSIENT_RETRIES]
        attempt = 0
        while True:
            if self._wait_for_request_window() or self._stop_event.is_set():
                return img_type, None, f"[{img_type}] Stopped"
            try:
                r = requests.get(url, timeout=10)
            except requests.exceptions.Timeout:
                _log.error("[%s] Timeout connecting to %s", img_type, self.cam_ip)
                detail = f"[{img_type}] Timeout"
                delay = _RETRY_BACKOFF_SEC[min(attempt, len(_RETRY_BACKOFF_SEC) - 1)]
                self._record_transient(delay, detail)
                if retry_budget[0] > 0 and self._running:
                    retry_budget[0] -= 1
                    attempt += 1
                    continue
                return img_type, None, detail
            except requests.exceptions.ConnectionError as e:
                _log.error("[%s] Connection refused: %s (%s)", img_type, self.cam_ip, e)
                detail = f"[{img_type}] Connection error"
                delay = _RETRY_BACKOFF_SEC[min(attempt, len(_RETRY_BACKOFF_SEC) - 1)]
                self._record_transient(delay, detail)
                if retry_budget[0] > 0 and self._running:
                    retry_budget[0] -= 1
                    attempt += 1
                    continue
                return img_type, None, detail
            except Exception as e:
                _log.error("[%s] Unexpected error: %s", img_type, e, exc_info=True)
                return img_type, None, str(e)

            if r.status_code == 200:
                content_type = r.headers.get("Content-Type", "")
                if "image" not in content_type.lower() and content_type != "octet-stream":
                    _log.warning("[%s] Unexpected Content-Type: %s", img_type, content_type)
                    return img_type, None, f"[{img_type}] Not an image. Content-Type: {content_type}"
                self._record_request_success()
                return img_type, r.content, None

            # 일시적 상태 코드 → 남은 재시도가 있으면 백오프 후 재시도
            if r.status_code in _TRANSIENT_STATUSES:
                delay = self._retry_delay(r, attempt)
                detail = f"[{img_type}] HTTP {r.status_code}"
                self._record_transient(delay, detail)
                if retry_budget[0] > 0 and self._running:
                    retry_budget[0] -= 1
                    attempt += 1
                    _log.warning(
                        "[%s] HTTP %d — shared retry budget remaining=%d",
                        img_type,
                        r.status_code,
                        retry_budget[0],
                    )
                    continue
                return img_type, None, detail

            _log.warning("[%s] HTTP %d from %s", img_type, r.status_code, self.cam_ip)
            return img_type, None, f"[{img_type}] HTTP {r.status_code}"

    def _run(self):
        try:
            os.makedirs(self.save_dir, exist_ok=True)
            started_at = time.monotonic()
            with self._capture_io_lock:
                if self._running:
                    self._capture_cycle()

            # Deadlines stay anchored to monotonic start time.  Slow HTTP or
            # ExifTool work can skip a missed slot, but never accumulates drift
            # by sleeping a full interval after processing.
            next_capture = started_at + self._normal_interval
            next_probe = started_at + self.probe_interval
            now = time.monotonic()
            while next_capture <= now:
                next_capture += self._normal_interval
            while next_probe <= now:
                next_probe += self.probe_interval
            probe_tick = 0

            while self._running:
                now = time.monotonic()
                capture_due = now >= next_capture
                probe_due = (
                    self.probe_callback is not None
                    and self._was_connected
                    and now >= next_probe
                )

                if capture_due:
                    while next_capture <= now:
                        next_capture += self._normal_interval
                    with self._capture_io_lock:
                        if self._running:
                            self._capture_cycle()
                    # Drop deadlines missed while camera I/O was in progress.
                    now = time.monotonic()
                    while next_capture <= now:
                        next_capture += self._normal_interval
                    while next_probe <= now:
                        next_probe += self.probe_interval
                    continue

                if probe_due:
                    while next_probe <= now:
                        next_probe += self.probe_interval
                    probe_tick += 1
                    immediate_capture = False
                    with self._capture_io_lock:
                        if not self._running:
                            break
                        if self._wait_for_request_window():
                            break
                        _log.info(
                            "probe #%d: checking (probe_interval=%.1fs)",
                            probe_tick,
                            self.probe_interval,
                        )
                        temp = probe_thermal_from_url(
                            self._urls["thermal"], timeout=5.0
                        )
                        if temp is None:
                            _log.warning("probe #%d: failed", probe_tick)
                            self._record_transient(
                                self.probe_interval,
                                "temperature probe failed",
                            )
                        else:
                            self._record_request_success()
                            _log.info("probe #%d: max_temp=%.1f°C", probe_tick, temp)
                            elevated = bool(self.probe_callback(temp))
                            immediate_capture = elevated and not self._probe_elevated
                            self._probe_elevated = elevated
                            if immediate_capture and self._running:
                                _log.info(
                                    "probe #%d: ELEVATED (%.1f°C) — "
                                    "triggering one immediate full capture",
                                    probe_tick,
                                    temp,
                                )
                                self._log(
                                    f"[capture] Probe: {temp:.1f}°C — "
                                    "immediate full capture triggered"
                                )
                                self._capture_cycle()
                    # An immediate capture is extra; it does not move the
                    # periodic 30-second full-capture deadline.
                    now = time.monotonic()
                    while next_probe <= now:
                        next_probe += self.probe_interval
                    continue

                deadlines = [next_capture]
                if self.probe_callback is not None and self._was_connected:
                    deadlines.append(next_probe)
                if self._stop_event.wait(max(0.0, min(deadlines) - now)):
                    break
        finally:
            with self._lifecycle_lock:
                self._running = False
            self._stopped_event.set()
            _log.info("Capture thread exited")

    def _capture_cycle(self):
        """Run one periodic capture while ``_capture_io_lock`` is held."""
        if not self._running:
            return
        img_types = ["thermal", "visual"] if self.mode == "both" else ["thermal"]
        try:
            now = datetime.now()
            filenametime = now.strftime("%Y%m%d%H%M%S_%f")
            save_subdir = capture_subdir(self.save_dir, now)
            os.makedirs(save_subdir, exist_ok=True)
            saved_thermal: str | None = None
            saved_visual: str | None = None

            # A single camera cannot reliably serve thermal and visual bursts.
            # Use one shared retry budget and always request thermal first.
            retry_budget = [_MAX_TRANSIENT_RETRIES]
            for img_type in img_types:
                if not self._running:
                    break
                _, content, error = self._fetch_image(
                    img_type,
                    retry_budget=retry_budget,
                )
                if error or content is None:
                    self._log(error or f"[{img_type}] capture failed")
                    # Without thermal there is no radiometric pair; do not add
                    # a visual request to an already busy camera.
                    if img_type == "thermal":
                        break
                    continue
                suffix = "_visual" if img_type == "visual" else ""
                jpg_path = os.path.join(save_subdir, f"{filenametime}{suffix}.jpg")
                with open(jpg_path, "wb") as f:
                    f.write(content)
                if img_type == "visual":
                    saved_visual = jpg_path
                else:
                    saved_thermal = jpg_path
                self._log(
                    f"[{datetime.now().strftime('%H:%M:%S')}] [{img_type}] saved "
                    f"({len(content)} bytes)"
                )

            # 알람 오버레이가 카메라를 다시 치지 않도록 최신 저장 쌍을 공개
            if saved_thermal:
                with self._last_pair_lock:
                    self._last_pair = (saved_thermal, saved_visual)

            # Thermal success confirms camera connectivity.  A visual-only
            # failure is an incomplete pair, not a disconnected camera.
            if saved_thermal:
                if not self._was_connected:
                    _log.info("Camera connection restored: %s", self.cam_ip)
                    self._report_connection_status_async("connected")
                    self._was_connected = True
                    self._notify_status("recovered", "thermal capture recovered")
                self._consecutive_failures = 0
                if self.mode == "both" and saved_visual is None:
                    _log.warning("Capture pair degraded: visual image unavailable")
                    self._notify_status("degraded", "visual image unavailable")
            else:
                self._consecutive_failures += 1
                _log.warning(
                    "Thermal capture failed (%d consecutive cycles)",
                    self._consecutive_failures,
                )
                if (
                    self._was_connected
                    and self._consecutive_failures >= _DISCONNECT_FAILURE_THRESHOLD
                ):
                    self._report_connection_status_async("disconnected")
                    self._was_connected = False
                    self._notify_status(
                        "disconnected",
                        f"thermal capture failed {self._consecutive_failures} cycles",
                    )

        except requests.exceptions.Timeout:
            _log.error("Timeout in capture loop")
            self._log("[capture] Timeout")
        except requests.exceptions.ConnectionError:
            _log.error("Connection error in capture loop: %s", self.cam_ip)
            self._log("[capture] Connection error - check camera IP")
        except Exception as e:
            _log.error("Capture loop error: %s", e, exc_info=True)
            self._log(f"[capture] Error: {e}")


# ------------------------------------------------------------
# 직접 실행 (CLI)
# ------------------------------------------------------------
if __name__ == "__main__":
    from ..operational_mode import exit_legacy_operation

    exit_legacy_operation("python -m thermal_monitoring.capture.capture")

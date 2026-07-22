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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

import requests

from ..config import load_config
from ..logger import get_logger
from .thermal_utils import probe_thermal_from_url

_log = get_logger("capture")

# 카메라 REST API의 일시적 오류(서버 busy·동시 요청 등) → 짧은 백오프로 재시도.
_TRANSIENT_STATUSES = frozenset({502, 503, 504, 429})
_MAX_TRANSIENT_RETRIES = 2               # 최초 1회 + 추가 재시도 2회
_RETRY_BACKOFF_SEC = (0.5, 1.0)          # 재시도 회차별 대기
_MAX_RETRY_AFTER_SEC = 2.0               # Retry-After 헤더 반영 시 상한 (인터벌 초과 방지)


class CaptureSession:
    def __init__(
        self,
        cam_ip: str | None = None,
        mode: str | None = None,
        interval: float | None = None,
        save_dir: str | None = None,
        log_callback=None,
        probe_callback=None,
    ):
        cfg = load_config()
        self.cam_ip = cam_ip or cfg.camera.ip
        self.mode = mode or cfg.tools.mode      # "thermal" or "both"
        self.interval = interval or cfg.camera.capture_interval_sec
        self.save_dir = save_dir or cfg.paths.dataset_dir
        self.log_callback = log_callback  # callable(str) for GUI output
        self.probe_callback = probe_callback  # callable(float) — max_temp을 받아 Warning 이상이면 True 반환
        self._running = False
        self._thread = None
        self._consecutive_failures = 0
        self._was_connected = False
        self._normal_interval = self.interval
        self._warning_interval = cfg.camera.warning_interval_sec
        self._interval_lock = threading.Lock()
        # 가장 최근 캡처 사이클에서 저장된 (thermal, visual) 경로. 알람 오버레이가
        # 카메라를 다시 치지 않고 최신 프레임을 재사용할 수 있게 노출한다.
        self._last_pair: tuple[str | None, str | None] = (None, None)
        self._last_pair_lock = threading.Lock()
        # GUI-UPDATE: cam_ip 인자가 None이어도 config에서 확정된 self.cam_ip를 사용한다.
        self._urls = {
            "thermal": f"http://{self.cam_ip}/api/image/current?imgformat=JPEG",
            "visual": f"http://{self.cam_ip}/api/image/current?imgformat=JPEG_visual",
        }
        _log.info("CaptureSession initialized: ip=%s mode=%s interval=%.1fs save_dir=%s",
                  self.cam_ip, self.mode, self.interval, self.save_dir)

    def _log(self, msg: str):
        if self.log_callback:
            self.log_callback(msg)
        else:
            print(msg)

    def start(self):
        if self._running:
            _log.warning("Capture session already running — ignored start()")
            self._log("[capture] Already running.")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        _log.info("Capture started: interval=%.1fs mode=%s", self.interval, self.mode)
        self._log(f"[capture] Started (interval={self.interval}s, mode={self.mode})")

    def stop(self):
        _log.info("Capture stop requested")
        self._running = False
        if self._thread:
            self._thread.join(timeout=self.interval + 5)
        _log.info("Capture stopped (consecutive_failures=%d)", self._consecutive_failures)
        self._log("[capture] Stopped.")

    def request_stop(self):
        """캡처 중단 요청만 하고 join은 하지 않는다 (UI 블로킹 방지)."""
        _log.info("Capture stop requested (non-blocking)")
        self._running = False
        self._log("[capture] Stop requested (non-blocking).")

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_saved_pair(self) -> tuple[str | None, str | None]:
        """가장 최근 캡처 사이클에서 저장된 (thermal, visual) 경로.

        경고 모드(thermal-only 캡처)면 visual은 None, 아직 캡처 전이면 (None, None).
        오버레이 생성 시 카메라 추가 접근 없이 최신 프레임을 쓰기 위한 용도.
        """
        with self._last_pair_lock:
            return self._last_pair

    def capture_both_once(self) -> tuple[str | None, str | None]:
        """알람용 일회성 캡처: thermal + visual 동시 요청 → 디스크 저장 → 경로 반환.

        Returns:
            (thermal_jpg_path, visual_jpg_path) — 실패 시 (None, None)
            visual_jpg_path는 mode가 'thermal'이면 None.
        """
        if not self._running:
            _log.warning("capture_both_once: session not running")
            return (None, None)

        do_visual = self.mode == "both"
        filenametime = datetime.now().strftime("%Y%m%d%H%M%S_%f")

        results: dict[str, str | None] = {"thermal": None, "visual": None}
        img_types = ["thermal", "visual"] if do_visual else ["thermal"]

        if len(img_types) > 1:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {executor.submit(self._fetch_image, t): t for t in img_types}
                for future in as_completed(futures):
                    img_type, content, error = future.result()
                    if error or content is None:
                        _log.warning("capture_both_once: %s failed, aborting", img_type)
                        return (None, None)
                    suffix = "_visual" if img_type == "visual" else ""
                    jpg_path = os.path.join(self.save_dir, f"{filenametime}{suffix}.jpg")
                    with open(jpg_path, "wb") as f:
                        f.write(content)
                    results[img_type] = jpg_path
                    self._log(f"[{datetime.now().strftime('%H:%M:%S')}] [alarm] [{img_type}] saved ({len(content)} bytes)")
        else:
            _, content, error = self._fetch_image("thermal")
            if error or content is None:
                return (None, None)
            jpg_path = os.path.join(self.save_dir, f"{filenametime}.jpg")
            with open(jpg_path, "wb") as f:
                f.write(content)
            results["thermal"] = jpg_path

        if results["thermal"]:
            with self._last_pair_lock:
                self._last_pair = (results["thermal"], results.get("visual"))

        return (results["thermal"], results.get("visual"))

    def set_warning_mode(self, active: bool) -> None:
        """과열 감지 시 캡처 주기를 warning_interval로 전환. 평상시 복귀."""
        with self._interval_lock:
            new_interval = self._warning_interval if active else self._normal_interval
            if self.interval != new_interval:
                old = self.interval
                self.interval = new_interval
                _log.info("Capture interval changed: %.1fs → %.1fs (%s)",
                          old, new_interval, "warning" if active else "normal")
                self._log(f"[capture] Interval changed: {old:.1f}s → {new_interval:.1f}s " +
                          f"({'warning' if active else 'normal'} mode)")

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

    def _fetch_image(self, img_type: str) -> tuple[str, bytes | None, str | None]:
        """단일 이미지 캡처 (thermal/visual 공용). 503 등 일시적 오류는 짧은 백오프로 재시도.

        Returns:
            (img_type, content_bytes | None, error_str | None)
        """
        url = self._urls[img_type]
        last_err: str | None = None
        for attempt in range(_MAX_TRANSIENT_RETRIES + 1):
            try:
                r = requests.get(url, timeout=10)
            except requests.exceptions.Timeout:
                _log.error("[%s] Timeout connecting to %s", img_type, self.cam_ip)
                return img_type, None, f"[{img_type}] Timeout"
            except requests.exceptions.ConnectionError as e:
                _log.error("[%s] Connection refused: %s (%s)", img_type, self.cam_ip, e)
                return img_type, None, f"[{img_type}] Connection error"
            except Exception as e:
                _log.error("[%s] Unexpected error: %s", img_type, e, exc_info=True)
                return img_type, None, str(e)

            if r.status_code == 200:
                content_type = r.headers.get("Content-Type", "")
                if "image" not in content_type.lower() and content_type != "octet-stream":
                    _log.warning("[%s] Unexpected Content-Type: %s", img_type, content_type)
                    return img_type, None, f"[{img_type}] Not an image. Content-Type: {content_type}"
                return img_type, r.content, None

            # 일시적 상태 코드 → 남은 재시도가 있으면 백오프 후 재시도
            if r.status_code in _TRANSIENT_STATUSES and attempt < _MAX_TRANSIENT_RETRIES and self._running:
                delay = self._retry_delay(r, attempt)
                _log.warning("[%s] HTTP %d from %s — retry %d/%d after %.1fs",
                             img_type, r.status_code, self.cam_ip,
                             attempt + 1, _MAX_TRANSIENT_RETRIES, delay)
                last_err = f"[{img_type}] HTTP {r.status_code}"
                time.sleep(delay)
                continue

            _log.warning("[%s] HTTP %d from %s", img_type, r.status_code, self.cam_ip)
            return img_type, None, f"[{img_type}] HTTP {r.status_code}"

        return img_type, None, last_err or f"[{img_type}] transient failure"

    def _run(self):
        os.makedirs(self.save_dir, exist_ok=True)

        while self._running:
            # 경고 모드에서는 thermal만 캡처 (visual은 알람 시점에만 필요)
            with self._interval_lock:
                is_normal_cycle = self.interval == self._normal_interval
            img_types = ["thermal", "visual"] if (self.mode == "both" and is_normal_cycle) else ["thermal"]
            try:
                filenametime = datetime.now().strftime("%Y%m%d%H%M%S_%f")
                saved_thermal: str | None = None
                saved_visual: str | None = None

                # Thermal + Visual 동시 요청으로 정렬 오차 최소화 (503 등은 _fetch_image가 재시도)
                all_ok = True
                if len(img_types) > 1:
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        futures = {executor.submit(self._fetch_image, t): t for t in img_types}
                        for future in as_completed(futures):
                            if not self._running:
                                break
                            img_type, content, error = future.result()
                            if error:
                                self._log(error)
                                all_ok = False
                                continue
                            if content is None:
                                all_ok = False
                                continue
                            suffix = "_visual" if img_type == "visual" else ""
                            jpg_path = os.path.join(self.save_dir, f"{filenametime}{suffix}.jpg")
                            with open(jpg_path, "wb") as f:
                                f.write(content)
                            if img_type == "visual":
                                saved_visual = jpg_path
                            else:
                                saved_thermal = jpg_path
                            self._log(f"[{datetime.now().strftime('%H:%M:%S')}] [{img_type}] saved "
                                      f"({len(content)} bytes)")
                else:
                    for img_type in img_types:
                        if not self._running:
                            break
                        _, content, error = self._fetch_image(img_type)
                        if error:
                            self._log(error)
                            all_ok = False
                            continue
                        if content is None:
                            all_ok = False
                            continue
                        jpg_path = os.path.join(self.save_dir, f"{filenametime}.jpg")
                        with open(jpg_path, "wb") as f:
                            f.write(content)
                        saved_thermal = jpg_path
                        self._log(f"[{datetime.now().strftime('%H:%M:%S')}] [{img_type}] saved "
                                  f"({len(content)} bytes)")

                # 알람 오버레이가 카메라를 다시 치지 않도록 최신 저장 쌍을 공개
                if saved_thermal:
                    with self._last_pair_lock:
                        self._last_pair = (saved_thermal, saved_visual)

                # 연결 상태 추적
                if all_ok:
                    if not self._was_connected:
                        _log.info("Camera connection restored: %s", self.cam_ip)
                        self._was_connected = True
                    self._consecutive_failures = 0
                else:
                    self._consecutive_failures += 1
                    self._was_connected = False
                    if self._consecutive_failures == 5:
                        _log.warning("Camera unreachable for 5 consecutive attempts: %s", self.cam_ip)
                    elif self._consecutive_failures == 30:
                        _log.error("Camera unreachable for 30 consecutive attempts: %s", self.cam_ip)

            except requests.exceptions.Timeout:
                _log.error("Timeout in capture loop")
                self._log("[capture] Timeout")
            except requests.exceptions.ConnectionError:
                _log.error("Connection error in capture loop: %s", self.cam_ip)
                self._log("[capture] Connection error - check camera IP")
            except Exception as e:
                _log.error("Capture loop error: %s", e, exc_info=True)
                self._log(f"[capture] Error: {e}")

            # 대기 시간 동안 1초 간격으로 경량 프로브 수행
            deadline = time.monotonic() + self.interval
            probe_backoff = 0  # 프로브 실패 시 대기 시간 (초)
            probe_tick = 0
            while self._running:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

                # 정상 모드일 때만 경량 프로브로 과열 감시
                with self._interval_lock:
                    is_normal = self.interval == self._normal_interval

                if is_normal and self.probe_callback and self._was_connected and probe_backoff <= 0:
                    probe_tick += 1
                    _log.info("probe #%d: checking (interval=%.1fs, remaining=%.1fs)", probe_tick, self.interval, remaining)
                    temp = probe_thermal_from_url(self._urls["thermal"], timeout=5.0)
                    if temp is not None:
                        _log.info("probe #%d: max_temp=%.1f°C", probe_tick, temp)
                        if self.probe_callback(temp):
                            _log.info("probe #%d: ELEVATED (%.1f°C) — triggering immediate capture", probe_tick, temp)
                            self._log(f"[capture] Probe: {temp:.1f}°C — immediate capture triggered")
                            break
                    else:
                        _log.warning("probe #%d: failed — backing off ~6s", probe_tick)
                        probe_backoff = 2

                if probe_backoff > 0:
                    probe_backoff -= 1

                time.sleep(min(3.0, remaining))


# ------------------------------------------------------------
# 직접 실행 (CLI)
# ------------------------------------------------------------
if __name__ == "__main__":
    session = CaptureSession()
    try:
        session.start()
        while session.running:
            time.sleep(1)
    except KeyboardInterrupt:
        session.stop()

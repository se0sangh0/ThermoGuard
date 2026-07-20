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

    @property
    def running(self) -> bool:
        return self._running

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

    def _run(self):
        os.makedirs(self.save_dir, exist_ok=True)
        img_types = ["thermal", "visual"] if self.mode == "both" else ["thermal"]

        while self._running:
            try:
                filenametime = datetime.now().strftime("%Y%m%d%H%M%S_%f")

                def _fetch_one(img_type: str) -> tuple[str, bytes | None, str | None]:
                    """단일 이미지 타입 캡처 — 병렬 호출용."""
                    try:
                        r = requests.get(self._urls[img_type], timeout=10)
                        if r.status_code == 200:
                            content_type = r.headers.get("Content-Type", "")
                            if "image" not in content_type.lower() and content_type != "octet-stream":
                                _log.warning("[%s] Unexpected Content-Type: %s", img_type, content_type)
                                return img_type, None, f"[{img_type}] Not an image. Content-Type: {content_type}"
                            return img_type, r.content, None
                        _log.warning("[%s] HTTP %d from %s", img_type, r.status_code, self.cam_ip)
                        return img_type, None, f"[{img_type}] HTTP {r.status_code}"
                    except requests.exceptions.Timeout:
                        _log.error("[%s] Timeout connecting to %s", img_type, self.cam_ip)
                        return img_type, None, f"[{img_type}] Timeout"
                    except requests.exceptions.ConnectionError as e:
                        _log.error("[%s] Connection refused: %s (%s)", img_type, self.cam_ip, e)
                        return img_type, None, f"[{img_type}] Connection error"
                    except Exception as e:
                        _log.error("[%s] Unexpected error: %s", img_type, e, exc_info=True)
                        return img_type, None, str(e)

                # Thermal + Visual 동시 요청으로 정렬 오차 최소화
                all_ok = True
                if len(img_types) > 1:
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        futures = {executor.submit(_fetch_one, t): t for t in img_types}
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
                            self._log(f"[{datetime.now().strftime('%H:%M:%S')}] [{img_type}] saved "
                                      f"({len(content)} bytes)")
                else:
                    for img_type in img_types:
                        if not self._running:
                            break
                        _, content, error = _fetch_one(img_type)
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
                        self._log(f"[{datetime.now().strftime('%H:%M:%S')}] [{img_type}] saved "
                                  f"({len(content)} bytes)")

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
            while self._running:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

                # 정상 모드일 때만 프로브 (이미 warning 모드면 풀캡처로 충분)
                with self._interval_lock:
                    is_normal = self.interval == self._normal_interval

                if is_normal and self.probe_callback and self._was_connected and probe_backoff <= 0:
                    _log.debug("probe: running thermal check")
                    temp = probe_thermal_from_url(self._urls["thermal"], timeout=5.0)
                    if temp is not None:
                        _log.debug("probe: max_temp=%.1f°C", temp)
                        if self.probe_callback(temp):
                            _log.info("Probe detected elevated temp (%.1f°C) — triggering immediate capture", temp)
                            self._log(f"[capture] Probe: {temp:.1f}°C — immediate capture triggered")
                            break
                    else:
                        _log.warning("probe failed — backing off 5s")
                        probe_backoff = 5

                if probe_backoff > 0:
                    probe_backoff -= 1

                time.sleep(min(1.0, remaining))


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

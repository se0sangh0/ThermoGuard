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
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

import requests

from ..config import (
    DEFAULT_TOOLS_MODE,
    NORMAL_CAPTURE_INTERVAL_SEC,
    TEMP_MONITOR_INTERVAL_SEC,
    load_config,
)
from ..logger import get_logger

_log = get_logger("capture")


def camera_image_url(cam_ip: str, *, visual: bool = False) -> str:
    """FLIR A50 현재 이미지 엔드포인트 URL. thermal/visual 공용 (URL 중복 제거)."""
    fmt = "JPEG_visual" if visual else "JPEG"
    return f"http://{cam_ip}/api/image/current?imgformat={fmt}"

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
        self.mode = mode or DEFAULT_TOOLS_MODE  # "thermal" or "both"
        self.interval = (
            interval if interval is not None else NORMAL_CAPTURE_INTERVAL_SEC
        )
        self.save_dir = save_dir or cfg.paths.dataset_dir
        self.log_callback = log_callback  # callable(str) for GUI output
        # 호환성을 위해 받지만 REST 캡처 루프에서는 호출하지 않는다.
        # 온도 감시는 카메라 연결 작업에서 별도 GigE 루프로 제공한다.
        self.probe_callback = probe_callback
        self._running = False
        self._thread = None
        self._stop_event = threading.Event()
        # 주기 캡처와 UI/알람의 명시적 캡처가 카메라 REST를 동시에 치지 않게 한다.
        self._capture_lock = threading.Lock()
        self._consecutive_failures = 0
        self._was_connected = False
        # 기존 UI 호환 필드. warning 상태는 interval과 독립적이다.
        self._normal_interval = self.interval
        self._warning_interval = TEMP_MONITOR_INTERVAL_SEC
        self._interval_lock = threading.Lock()
        self._warning_mode = False
        # 가장 최근 캡처 사이클에서 저장된 (thermal, visual) 경로. 알람 오버레이가
        # 카메라를 다시 치지 않고 최신 프레임을 재사용할 수 있게 노출한다.
        self._last_pair: tuple[str | None, str | None] = (None, None)
        self._last_pair_lock = threading.Lock()
        # GUI-UPDATE: cam_ip 인자가 None이어도 config에서 확정된 self.cam_ip를 사용한다.
        self._urls = {
            "thermal": camera_image_url(self.cam_ip),
            "visual": camera_image_url(self.cam_ip, visual=True),
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
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        _log.info("Capture started: interval=%.1fs mode=%s", self.interval, self.mode)
        self._log(f"[capture] Started (interval={self.interval}s, mode={self.mode})")

    def stop(self):
        _log.info("Capture stop requested")
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=15.0)
        _log.info("Capture stopped (consecutive_failures=%d)", self._consecutive_failures)
        self._log("[capture] Stopped.")

    def request_stop(self):
        """캡처 중단 요청만 하고 join은 하지 않는다 (UI 블로킹 방지)."""
        _log.info("Capture stop requested (non-blocking)")
        self._running = False
        self._stop_event.set()
        self._log("[capture] Stop requested (non-blocking).")

    @property
    def running(self) -> bool:
        return self._running

    @property
    def warning_mode(self) -> bool:
        """Return the compatibility warning flag without changing REST capture behavior."""
        with self._interval_lock:
            return self._warning_mode

    @property
    def last_saved_pair(self) -> tuple[str | None, str | None]:
        """가장 최근 캡처 사이클에서 저장된 (thermal, visual) 경로.

        ``mode='thermal'``이면 visual은 None, 아직 캡처 전이면
        (None, None). warning_mode는 저장 파일 구성에 영향을 주지 않는다.
        오버레이 생성 시 카메라 추가 접근 없이 최신 프레임을 쓰기 위한 용도.
        """
        with self._last_pair_lock:
            return self._last_pair

    def capture_both_once(self) -> tuple[str | None, str | None]:
        """명시적 일회 캡처 요청을 수행하고 저장 경로를 반환한다.

        Returns:
            (thermal_jpg_path, visual_jpg_path) — 실패 시 (None, None)
            visual_jpg_path는 mode가 'thermal'이면 None.
        """
        if not self._running:
            _log.warning("capture_both_once: session not running")
            return (None, None)

        img_types = ["thermal", "visual"] if self.mode == "both" else ["thermal"]
        with self._capture_lock:
            if not self._running:
                return (None, None)
            pair, all_ok = self._capture_images(img_types, log_label="requested")
            if all_ok and pair[0] is not None:
                # 캡처와 게시를 같은 직렬화 구간에서 완료해 더 최신인 정기
                # 캡처 결과를 이전 명시 캡처 결과가 덮어쓰지 않게 한다.
                with self._last_pair_lock:
                    self._last_pair = pair

        if not all_ok or pair[0] is None:
            return (None, None)
        return pair

    def _capture_images(
        self,
        img_types: list[str],
        *,
        log_label: str | None = None,
    ) -> tuple[tuple[str | None, str | None], bool]:
        """하나의 REST 캡처 사이클을 수행한다.

        호출자가 ``_capture_lock``을 보유해야 하며, thermal/visual 결과와
        전체 성공 여부를 반환한다.
        """
        filenametime = datetime.now().strftime("%Y%m%d%H%M%S_%f")
        results: dict[str, str | None] = {"thermal": None, "visual": None}
        contents: dict[str, bytes] = {}
        all_ok = True

        def save_images_atomically() -> bool:
            """요청된 이미지 전체를 저장하거나 실패 시 모두 정리한다."""
            pending: list[tuple[str, str, str]] = []
            committed: list[str] = []
            try:
                os.makedirs(self.save_dir, exist_ok=True)
                for img_type in img_types:
                    suffix = "_visual" if img_type == "visual" else ""
                    final_path = os.path.join(
                        self.save_dir, f"{filenametime}{suffix}.jpg"
                    )
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        dir=self.save_dir,
                        prefix=f".{filenametime}{suffix}.",
                        suffix=".tmp",
                        delete=False,
                    ) as stream:
                        temporary_path = stream.name
                        pending.append((img_type, temporary_path, final_path))
                        stream.write(contents[img_type])
                        stream.flush()
                        os.fsync(stream.fileno())

                # 데이터 스캐너는 thermal JPG를 기준으로 새 캡처를 찾는다.
                # mode=both에서는 visual을 먼저 커밋하고 thermal을 마지막에
                # 공개해 thermal 파일 자체가 완성된 쌍의 커밋 마커가 되게 한다.
                commit_order = sorted(
                    pending,
                    key=lambda item: item[0] == "thermal",
                )
                for _img_type, temporary_path, final_path in commit_order:
                    os.replace(temporary_path, final_path)
                    committed.append(final_path)

                label = f" [{log_label}]" if log_label else ""
                for img_type, _temporary_path, final_path in pending:
                    results[img_type] = final_path
                    self._log(
                        f"[{datetime.now().strftime('%H:%M:%S')}]{label} "
                        f"[{img_type}] saved ({len(contents[img_type])} bytes)"
                    )
                return True
            except OSError as exc:
                self._log(f"[capture] File save failed: {exc}")
                return False
            finally:
                if len(committed) != len(img_types):
                    for _img_type, temporary_path, _final_path in pending:
                        try:
                            os.unlink(temporary_path)
                        except FileNotFoundError:
                            pass
                        except OSError as exc:
                            _log.warning(
                                "Failed to remove capture temp file %s: %s",
                                temporary_path,
                                exc,
                            )
                    for final_path in committed:
                        try:
                            os.unlink(final_path)
                        except FileNotFoundError:
                            pass
                        except OSError as exc:
                            _log.warning(
                                "Failed to roll back capture file %s: %s",
                                final_path,
                                exc,
                            )

        if len(img_types) > 1:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {executor.submit(self._fetch_image, t): t for t in img_types}
                for future in as_completed(futures):
                    if not self._running:
                        all_ok = False
                        continue
                    img_type, content, error = future.result()
                    if error or content is None:
                        if error:
                            self._log(error)
                        all_ok = False
                        continue
                    contents[img_type] = content
        else:
            img_type = img_types[0]
            _, content, error = self._fetch_image(img_type)
            if error or content is None:
                if error:
                    self._log(error)
                all_ok = False
            else:
                contents[img_type] = content

        # mode=both인 캡처는 요청된 두 이미지가 모두 준비된 뒤에만 저장한다.
        # 한쪽 REST 요청이 실패했을 때 불완전한 thermal-only 파일을 남기지 않는다.
        if all_ok and self._running and len(contents) == len(img_types):
            all_ok = save_images_atomically()
        else:
            all_ok = False

        return (results["thermal"], results["visual"]), all_ok

    def set_warning_mode(self, active: bool) -> None:
        """호환용 warning 상태를 기록한다.

        온도 감시와 이미지 캡처가 분리되었으므로 이 상태는 REST 주기,
        요청 유형, 파일 생성에 영향을 주지 않는다.
        """
        with self._interval_lock:
            changed = self._warning_mode != bool(active)
            self._warning_mode = bool(active)
        if changed:
            _log.info(
                "Capture warning flag changed: %s (REST interval remains %.1fs)",
                "warning" if active else "normal",
                self.interval,
            )

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
                if self._stop_event.wait(delay):
                    return img_type, None, f"[{img_type}] Capture stopped"
                continue

            _log.warning("[%s] HTTP %d from %s", img_type, r.status_code, self.cam_ip)
            return img_type, None, f"[{img_type}] HTTP {r.status_code}"

        return img_type, None, last_err or f"[{img_type}] transient failure"

    def _run(self):
        os.makedirs(self.save_dir, exist_ok=True)

        while self._running:
            # warning_mode와 무관하게 세션 mode의 REST 캡처 구성을 유지한다.
            img_types = ["thermal", "visual"] if self.mode == "both" else ["thermal"]
            try:
                with self._capture_lock:
                    if not self._running:
                        break
                    pair, all_ok = self._capture_images(img_types)
                    # 알람 오버레이가 카메라를 다시 치지 않도록 최신 저장 쌍을
                    # 캡처 직렬화 구간 안에서 공개한다.
                    if pair[0]:
                        with self._last_pair_lock:
                            self._last_pair = pair

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

            # 온도 프로브는 GigE 감시 루프가 담당한다. 이 루프는 정해진
            # REST 캡처 주기만 대기하며 stop/request_stop에 즉시 반응한다.
            if self._stop_event.wait(self.interval):
                break


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

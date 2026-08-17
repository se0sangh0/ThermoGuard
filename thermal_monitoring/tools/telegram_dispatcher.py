"""Telegram 알람 전송 결정·실행·DB 기록 — ProductDashboard delegate."""

from __future__ import annotations

import os
import hashlib
import threading
import tempfile
import time
from datetime import datetime
from typing import Optional
from pathlib import Path

import cv2
import requests

from ..analysis.threshold import Status
from ..config import bounded_backend_timeout
from ..logger import get_logger

_log = get_logger("tools.telegram_dispatcher")


class TelegramDispatcher:
    """ProductDashboard의 텔레그램 알람 전송 및 백엔드 DB 기록을 위임받는다."""

    RETRY_BACKOFF_SECONDS = 60.0
    BACKEND_LINK_AUDIT_WAIT_SECONDS = 15.0

    def __init__(
        self,
        dashboard,  # ProductDashboard (weak ref)
    ):
        self._dash = dashboard
        self._last_telegram_capture: Optional[datetime] = None
        self._pending_result: Optional[dict] = None
        self._pending_captured_at = None
        self._pending_quality_ok = False
        self._pending_attempt_count = 0
        self._last_attempt_monotonic = 0.0
        self._dispatch_inflight = False
        self._state_lock = threading.Lock()
        self._threshold_sync_lock = threading.Lock()

    # ── helpers ──────────────────────────────────────────────

    def _running(self) -> bool:
        return self._dash.lifecycle == "running"

    def _op_log(self, result: str, detail: str):
        if self._running():
            callback = lambda: self._dash._add_operating_log("Telegram", result, detail)
            post_to_ui = getattr(self._dash, "_post_to_ui", None)
            if post_to_ui is not None:
                post_to_ui(callback)
            else:  # compatibility with focused non-Tk test doubles
                self._dash.root.after(0, callback)

    def _trace(self, msg: str, *args):
        _log.debug(f"[TELEGRAM] {msg}", *args)

    def _ensure_threshold_profile(self, camera_id: int, roi_id: int):
        """Create/update the exact ROI profile before retrying a rejected measurement."""
        from .threshold_api_client import sync_threshold_profiles

        with self._threshold_sync_lock:
            cfg = self._dash.cfg
            return sync_threshold_profiles(
                base_url=cfg.backend.url,
                timeout=bounded_backend_timeout(cfg.backend.timeout_sec),
                camera_id=camera_id,
                roi_ids=[roi_id],
                baseline_temp=cfg.roi.baseline_temp,
                warning_delta=cfg.roi.warning_delta,
                critical_delta=cfg.roi.critical_delta,
                min_hotspot_size=cfg.hotspot.min_size,
                min_hotspot_size_max=cfg.hotspot.min_size_max,
                alarm_cooldown_sec=cfg.monitoring.alarm_cooldown_sec,
            )

    # ── 전송 결정 ────────────────────────────────────────────

    def maybe_dispatch(
        self,
        result: dict,
        quality_ok: bool,
        captured_at,
    ) -> None:
        """상태 머신이 승인한 Critical 알람만 전송한다."""
        triggered = bool(result.get("alarm"))
        current_status = result.get("alarm_status", result.get("status"))

        with self._state_lock:
            if current_status == Status.NORMAL:
                self._pending_result = None
                self._pending_captured_at = None
                self._pending_quality_ok = False
                self._pending_attempt_count = 0
                self._last_attempt_monotonic = 0.0

            if (
                triggered
                and captured_at != self._last_telegram_capture
                and captured_at != self._pending_captured_at
            ):
                self._pending_result = result
                self._pending_captured_at = captured_at
                self._pending_quality_ok = quality_ok
                self._pending_attempt_count = 0
                self._last_attempt_monotonic = 0.0
            elif (
                self._pending_result is not None
                and self._pending_attempt_count == 0
                and not self._pending_quality_ok
                and quality_ok
                and current_status == Status.CRITICAL
            ):
                # A trigger detected from a bad frame must never send that frame
                # later. Replace it with the first valid frame while the same
                # abnormal condition is still active.
                self._pending_result = result
                self._pending_captured_at = captured_at
                self._pending_quality_ok = True

            pending_result = self._pending_result
            pending_captured_at = self._pending_captured_at
            pending_quality_ok = self._pending_quality_ok

        if pending_result is None:
            self._trace(
                "SKIP — no critical alarm trigger (alarm=%s status=%s)",
                result.get("alarm", False), current_status,
            )
            return

        from ..analysis import notifier
        settings = notifier.get_settings()

        if not settings["configured"]:
            self._trace("SKIP — not configured")
            self._op_log("실패", "미로그인 — 환경설정에서 Telegram에 로그인하세요")
            return
        if not settings["enabled"]:
            self._trace("SKIP — disabled")
            self._op_log("실패", "알림 전송 비활성화")
            return
        if not pending_quality_ok:
            self._trace(
                "SKIP — quality not ok: %s",
                pending_result.get("image_quality_reason", "?"),
            )
            self._op_log(
                "실패",
                pending_result.get("image_quality_reason", "영상 품질 미달로 미발송"),
            )
            return

        now = time.monotonic()
        with self._state_lock:
            if self._dispatch_inflight:
                self._trace("SKIP — dispatch already in flight")
                return
            if self._pending_result is None:
                return
            if (
                self._pending_attempt_count > 0
                and now - self._last_attempt_monotonic < self.RETRY_BACKOFF_SECONDS
            ):
                self._trace(
                    "SKIP — retry backoff %.1fs remaining",
                    self.RETRY_BACKOFF_SECONDS - (now - self._last_attempt_monotonic),
                )
                return
            pending_result = self._pending_result
            pending_captured_at = self._pending_captured_at
            is_retry = self._pending_attempt_count > 0
            self._pending_attempt_count += 1
            self._last_attempt_monotonic = now
            self._dispatch_inflight = True

        self._trace(
            "DISPATCH — base=%s temp=%.1f retry=%s",
            pending_result.get("base", "?"),
            pending_result["max_temp"],
            is_retry,
        )
        self._op_log(
            "처리 중",
            f"{pending_result.get('roi_name', 'ROI')} · "
            f"{pending_result['max_temp']:.1f}°C",
        )
        try:
            self._dispatch(pending_result, pending_captured_at)
        except Exception as exc:
            # Thread creation and test doubles can fail synchronously. Release
            # the in-flight guard so the pending alarm remains retryable.
            self._complete_dispatch(False, pending_captured_at)
            _log.error("Telegram dispatch start error: %s", exc, exc_info=True)
            self._op_log("실패", f"전송 시작 실패: {exc}")

    # ── 전송 실행 ────────────────────────────────────────────

    def _complete_dispatch(self, success: bool, captured_at) -> None:
        """Complete an asynchronous attempt and retain failures for retry."""
        with self._state_lock:
            self._dispatch_inflight = False
            if not success:
                return
            self._last_telegram_capture = captured_at
            if self._pending_captured_at == captured_at:
                self._pending_result = None
                self._pending_captured_at = None
                self._pending_quality_ok = False
                self._pending_attempt_count = 0
                self._last_attempt_monotonic = 0.0

    def _record_unlinked_delivery_after_backend_post(
        self,
        result: dict,
        backend_event,
        *,
        success: bool,
        backend_url: str | None,
    ) -> None:
        """Link an already-sent Telegram result once a slow POST yields an ID.

        Alarm transmission never waits for database persistence.  This bounded
        background waiter restores the audit trail when the concurrent
        measurement POST completes shortly afterwards, without adding latency
        to the Critical notification path.
        """

        if backend_event is None:
            return

        def record() -> None:
            try:
                if not backend_event.wait(self.BACKEND_LINK_AUDIT_WAIT_SECONDS):
                    _log.warning(
                        "Telegram delivery audit not linked within %.1fs",
                        self.BACKEND_LINK_AUDIT_WAIT_SECONDS,
                    )
                    return
                alert_id = result.get("alert_id")
                if alert_id is None:
                    _log.warning(
                        "Telegram delivery audit unavailable: backend returned no alert_id"
                    )
                    return
                from ..analysis.notifier import save_delivery_result

                saved = save_delivery_result(
                    alert_id=int(alert_id),
                    success=success,
                    http_status=None,
                    error_message=(
                        None
                        if success
                        else "Telegram dispatch failed before backend alert link"
                    ),
                    retry_count=0,
                    backend_url=backend_url,
                )
                if not saved:
                    _log.warning("Telegram delivery audit POST failed for linked alert")
            except Exception as exc:
                # Do not permit best-effort auditing to affect alarm retry or
                # reveal backend/credential values in a UI callback.
                _log.warning(
                    "Telegram delivery audit link failed (%s)",
                    type(exc).__name__,
                )

        threading.Thread(target=record, daemon=True).start()

    def _dispatch(self, result: dict, captured_at):
        temp = float(result.get("hot_temp_95", result.get("max_temp", 0.0)))
        overlay = result.get("overlay")
        base = str(result.get("base", "")) or "latest"
        cfg = getattr(self._dash, "cfg", None)
        backend = getattr(cfg, "backend", None)
        backend_url = getattr(backend, "url", None)
        status = result.get(
            "measurement_status",
            result.get("alarm_status", result.get("status", Status.CRITICAL)),
        )
        status_value = status.value if isinstance(status, Status) else str(status)

        def work():
            from ..analysis.notifier import send_alarm

            self._trace("worker START: base=%s temp=%.1f status=%s", base, temp, status_value)

            # Critical notification is independent of database persistence.
            # Waiting for a slow/unavailable DB here can delay a factory alarm
            # by twelve seconds, so capture the ID only if it is already ready.
            backend_event = result.get("_backend_posted_event")
            alert_id = result.get("alert_id")
            if alert_id is None and backend_event is not None and backend_event.is_set():
                alert_id = result.get("alert_id")
            if alert_id is None and backend_event is not None:
                _log.warning("backend alert link is not ready; sending Telegram without DB link")

            tmp_path = None
            image_path = ""
            ok = False
            delivery_attempted = False
            if overlay is not None:
                tmp_path = os.path.join(
                    tempfile.gettempdir(), f"thermoguard_alarm_{base}.jpg"
                )
                try:
                    if cv2.imwrite(tmp_path, overlay):
                        image_path = tmp_path
                except Exception:
                    image_path = ""

            try:
                self._trace(
                    "calling send_alarm alert_id=%s image=%s", alert_id, bool(image_path),
                )
                delivery_attempted = True
                ok = send_alarm(
                    image_path=image_path,
                    temp=temp,
                    status=status_value,
                    alert_id=alert_id,
                    backend_url=backend_url,
                )
                self._trace("send_alarm returned ok=%s", ok)
                self._op_log(
                    "성공" if ok else "실패",
                    f"{temp:.1f}°C · {'사진' if image_path else '텍스트'}",
                )
            except RuntimeError:
                self._op_log("실패", "dashboard 환경 파일의 BOT_TOKEN / CHAT_ID를 확인하세요")
            except Exception as e:
                # Requests exceptions can contain the token-bearing Telegram
                # endpoint.  Keep both logs and UI status free of that value.
                _log.error("Telegram dispatch error (%s)", type(e).__name__)
                self._op_log("실패", f"전송 오류 ({type(e).__name__})")
            finally:
                if delivery_attempted and alert_id is None:
                    self._record_unlinked_delivery_after_backend_post(
                        result,
                        backend_event,
                        success=ok,
                        backend_url=backend_url,
                    )
                if tmp_path:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                self._complete_dispatch(ok, captured_at)

        threading.Thread(target=work, daemon=True).start()

    # ── 백엔드 측정값 POST ───────────────────────────────────

    @staticmethod
    def _file_payload(file_type: str, path_value) -> dict | None:
        if not path_value:
            return None
        path = Path(path_value)
        if not path.is_file():
            return None
        width = height = None
        if path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
            image = cv2.imread(str(path))
            if image is not None:
                height, width = image.shape[:2]
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return {
            "file_type": file_type,
            "storage_path": str(path.resolve()),
            "width": width,
            "height": height,
            "size_bytes": path.stat().st_size,
            "checksum_sha256": digest.hexdigest(),
        }

    def post_measurement(self, result: dict) -> None:
        """측정값을 POST /api/measurements 로 전송 (백그라운드 스레드)."""
        backend_event = result.get("_backend_posted_event")

        try:
            if not self._dash.cfg.backend.enabled:
                return

            measurement_status = result.get(
                "measurement_status",
                result.get("alarm_status", result["status"]),
            )
            status_value = (
                measurement_status.value.lower()
                if isinstance(measurement_status, Status)
                else str(measurement_status).lower()
            )
            camera_id = self._dash.cfg.identity.db_camera_id
            roi = result.get("measurement_roi")
            roi_id = getattr(roi, "db_roi_id", None) if roi is not None else None
            if camera_id is None or roi_id is None:
                self._dash.metrics.api_other_errors += 1
                _log.error(
                    "measurement POST skipped: missing DB identity "
                    "(camera_id=%s roi_id=%s roi=%s)",
                    camera_id,
                    roi_id,
                    result.get("roi_name", ""),
                )
                return

            do_alarm = bool(result.get("alarm", False))

            files = []
            for file_type, key in (
                ("thermal_jpg", "thermal_path"),
                ("visual_jpg", "visual_path"),
                ("thermal_npy", "npy_path"),
                ("overlay", "overlay_path"),
            ):
                item = self._file_payload(file_type, result.get(key))
                if item is not None:
                    files.append(item)

            thermal_shape = getattr(result.get("thermal_img"), "shape", None)
            visual_shape = getattr(result.get("visual_img"), "shape", None)
            quality_ok = bool(result.get("image_quality_ok", False))

            self._trace(
                "post_measurement: do_alarm=%s base=%s status=%s",
                do_alarm, result.get("base", "?"), status_value,
            )

            payload = {
                "camera_id": camera_id,
                "roi_id": roi_id,
                "max_temp": result["max_temp"],
                "min_temp": result.get("min_temp", 0.0),
                "mean_temp": result["mean_temp"],
                "percentile_95_temp": result["hot_temp_95"],
                "over_temp_pixels": result.get("over_temp_pixels", 0),
                "max_hotspot_size": result.get("max_hotspot_size", 0),
                "status": status_value,
                "algorithm_version": "v2.0",
                "do_alarm": do_alarm,
                "alarm_message": (
                    f"{result['max_temp']:.1f}°C · "
                    f"{status_value}"
                ),
                "captured_at": (
                    result["captured_at"].isoformat()
                    if result.get("captured_at") is not None else None
                ),
                "capture_mode": (
                    "warning" if status_value in {"warning", "critical"} else "normal"
                ),
                "thermal_status": "success" if result.get("thermal_path") else "failed",
                "visual_status": "success" if result.get("visual_path") else "skipped",
                "pair_status": "complete" if quality_ok else "invalid",
                "files": files,
                "hotspots": [
                    {
                        "center_x": max(0, int(point[0])),
                        "center_y": max(0, int(point[1])),
                        "max_temp": float(point[2]),
                        "area_pixels": None,
                    }
                    for point in result.get("hotspots", [])
                ],
                "image_quality": {
                    "is_valid": quality_ok,
                    "reason_code": "valid" if quality_ok else "invalid_pair",
                    "reason_message": result.get("image_quality_reason"),
                    "thermal_width": thermal_shape[1] if thermal_shape else None,
                    "thermal_height": thermal_shape[0] if thermal_shape else None,
                    "visual_width": visual_shape[1] if visual_shape else None,
                    "visual_height": visual_shape[0] if visual_shape else None,
                    "mean_difference": result.get("image_quality_mean_difference"),
                },
            }
            measurement_url = (
                f"{self._dash.cfg.backend.url}/api/measurements"
            )
            resp = requests.post(
                measurement_url,
                json=payload,
                timeout=bounded_backend_timeout(self._dash.cfg.backend.timeout_sec),
            )
            data = resp.json() if resp.status_code == 200 else {}
            threshold_missing = (
                resp.status_code == 200
                and data.get("status") == "error"
                and "threshold profile" in str(data.get("error", "")).lower()
            )
            if threshold_missing:
                _log.warning(
                    "measurement rejected without threshold; synchronizing and "
                    "retrying once: camera_id=%s roi_id=%s",
                    camera_id,
                    roi_id,
                )
                sync_result = self._ensure_threshold_profile(camera_id, roi_id)
                self._op_log(
                    "자동 복구",
                    f"ROI {roi_id} threshold 생성 {sync_result.created}개 · "
                    f"갱신 {sync_result.updated}개",
                )
                resp = requests.post(
                    measurement_url,
                    json=payload,
                    timeout=bounded_backend_timeout(self._dash.cfg.backend.timeout_sec),
                )
                data = resp.json() if resp.status_code == 200 else {}

            if resp.status_code == 200:
                if data.get("status") == "created":
                    alert_id = data.get("alert_id")
                    result["alert_id"] = alert_id
                    if do_alarm and alert_id is None:
                        # Critical trigger가 승인됐는데 alert_events가 생성되지
                        # 않았다면 Telegram 결과를 notification_deliveries에
                        # 연결할 수 없으므로 정상 저장으로 오인하지 않게 한다.
                        self._dash.metrics.api_other_errors += 1
                        _log.error(
                            "backend POST inconsistent: do_alarm=True but "
                            "alert_id=None (capture_id=%s)",
                            data.get("capture_id"),
                        )
                        self._op_log(
                            "실패",
                            "Critical 알람의 alert_id가 생성되지 않았습니다",
                        )
                    elif alert_id is not None:
                        self._dash.metrics.api_successes += 1
                        _log.info(
                            "backend alarm POST ok: capture_id=%s alert_id=%s",
                            data.get("capture_id"),
                            alert_id,
                        )
                        self._trace(
                            "saved alert_id=%s to result dict",
                            alert_id,
                        )
                    else:
                        self._dash.metrics.api_successes += 1
                        _log.info(
                            "backend measurement POST ok: capture_id=%s "
                            "(do_alarm=False, alert_id not expected)",
                            data.get("capture_id"),
                        )
                else:
                    _log.warning("backend POST rejected: %s", data)
                    self._dash.metrics.api_other_errors += 1
            else:
                self._dash._record_api_result(False, status_code=resp.status_code, error_kind="http")
        except requests.exceptions.Timeout:
            self._dash.metrics.api_timeouts += 1
        except requests.exceptions.ConnectionError:
            self._dash.metrics.api_connection_errors += 1
        except Exception as exc:
            self._dash.metrics.api_other_errors += 1
            _log.error("measurement POST failed: %s", exc, exc_info=True)
            self._op_log("실패", str(exc))
        finally:
            if backend_event is not None:
                try:
                    self._trace("backend_event.set() — alert_id=%s", result.get("alert_id"))
                    backend_event.set()
                except Exception:
                    pass
            local_event_id = result.get("_local_event_id")
            if local_event_id and self._running():
                callback = lambda event_id=local_event_id, linked_id=result.get("alert_id"): (
                    self._dash._link_backend_alert(event_id, linked_id)
                )
                post_to_ui = getattr(self._dash, "_post_to_ui", None)
                if post_to_ui is not None:
                    post_to_ui(callback)
                else:  # compatibility with focused non-Tk test doubles
                    self._dash.root.after(0, callback)

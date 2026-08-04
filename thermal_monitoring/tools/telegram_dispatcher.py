"""Telegram 알람 전송 결정·실행·DB 기록 — ProductDashboard delegate."""

from __future__ import annotations

import os
import threading
import tempfile
import time
from datetime import datetime
from typing import Optional

import cv2
import requests

from ..analysis.threshold import Status
from ..logger import get_logger

_log = get_logger("tools.telegram_dispatcher")


class TelegramDispatcher:
    """ProductDashboard의 텔레그램 알람 전송 및 백엔드 DB 기록을 위임받는다."""

    RETRY_BACKOFF_SECONDS = 60.0

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
            self._dash.root.after(
                0,
                lambda: self._dash._add_operating_log("Telegram", result, detail),
            )

    def _trace(self, msg: str, *args):
        _log.debug(f"[TELEGRAM] {msg}", *args)

    def _ensure_threshold_profile(self, camera_id: int, roi_id: int):
        """Create/update the exact ROI profile before retrying a rejected measurement."""
        from .threshold_api_client import sync_threshold_profiles

        with self._threshold_sync_lock:
            cfg = self._dash.cfg
            return sync_threshold_profiles(
                base_url=cfg.backend.url,
                timeout=cfg.backend.timeout_sec,
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

            alert_id = None
            backend_event = result.get("_backend_posted_event")
            if backend_event is not None:
                self._trace("waiting for _backend_posted_event (max 12s)...")
                if backend_event.wait(12.0):
                    alert_id = result.get("alert_id")
                    self._trace("got alert_id=%s", alert_id)
                else:
                    _log.warning(
                        "backend event timeout (12s) — alert_id remains None"
                    )
            else:
                self._trace("no _backend_posted_event — alert_id=None")

            tmp_path = None
            image_path = ""
            ok = False
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
                self._op_log("실패", ".env의 BOT_TOKEN / CHAT_ID를 확인하세요")
            except Exception as e:
                _log.error("Telegram dispatch error: %s", e, exc_info=True)
                self._op_log("실패", str(e))
            finally:
                if tmp_path:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                self._complete_dispatch(ok, captured_at)

        threading.Thread(target=work, daemon=True).start()

    # ── 백엔드 측정값 POST ───────────────────────────────────

    def post_measurement(self, result: dict) -> None:
        """측정값을 POST /api/measurements 로 전송 (백그라운드 스레드)."""
        backend_event = result.get("_backend_posted_event")

        try:
            if not self._dash.cfg.backend.enabled:
                return

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

            measurement_status = result.get(
                "measurement_status",
                result.get("alarm_status", result["status"]),
            )
            status_value = (
                measurement_status.value.lower()
                if isinstance(measurement_status, Status)
                else str(measurement_status).lower()
            )
            do_alarm = bool(result.get("alarm", False))

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
            }
            measurement_url = (
                f"{self._dash.cfg.backend.url}/api/measurements"
            )
            resp = requests.post(
                measurement_url,
                json=payload,
                timeout=self._dash.cfg.backend.timeout_sec,
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
                    "성공",
                    f"ROI {roi_id} threshold 생성 {sync_result.created}개 · "
                    f"갱신 {sync_result.updated}개",
                )
                resp = requests.post(
                    measurement_url,
                    json=payload,
                    timeout=self._dash.cfg.backend.timeout_sec,
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
                try:
                    self._dash.root.after(
                        0,
                        lambda event_id=local_event_id, linked_id=result.get("alert_id"): (
                            self._dash._link_backend_alert(event_id, linked_id)
                        ),
                    )
                except Exception:
                    pass

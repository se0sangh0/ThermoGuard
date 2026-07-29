"""Telegram 알람 전송 결정·실행·DB 기록 — ProductDashboard delegate."""

from __future__ import annotations

import os
import threading
import tempfile
from datetime import datetime
from typing import Optional

import cv2
import requests

from ..analysis.threshold import Status
from ..logger import get_logger

_log = get_logger("tools.telegram_dispatcher")


class TelegramDispatcher:
    """ProductDashboard의 텔레그램 알람 전송 및 백엔드 DB 기록을 위임받는다."""

    def __init__(
        self,
        dashboard,  # ProductDashboard (weak ref)
    ):
        self._dash = dashboard
        self._last_telegram_capture: Optional[datetime] = None

    # ── helpers ──────────────────────────────────────────────

    def _running(self) -> bool:
        return self._dash.lifecycle == "running"

    def _op_log(self, result: str, detail: str):
        if self._running():
            self._dash.root.after(
                0,
                lambda: self._dash._add_operating_log("텔레그램 알림", result, detail),
            )

    def _trace(self, msg: str, *args):
        _log.debug(f"[TELEGRAM] {msg}", *args)

    # ── 전송 결정 ────────────────────────────────────────────

    def maybe_dispatch(
        self,
        result: dict,
        quality_ok: bool,
        captured_at,
        *,
        warning_transition: bool = False,
    ) -> None:
        """경고 진입/위험 알람을 전송하고 보내지 않는 경우도 로그로 남긴다."""
        if not result.get("alarm") and not warning_transition:
            self._trace(
                "SKIP — no trigger (alarm=%s warning_transition=%s)",
                result.get("alarm", False), warning_transition,
            )
            return

        from ..analysis import notifier
        settings = notifier.get_settings()

        if not settings["configured"]:
            self._trace("SKIP — not configured")
            self._op_log("보류", "미로그인 — 환경설정에서 Telegram에 로그인하세요")
            return
        if not settings["enabled"]:
            self._trace("SKIP — disabled")
            self._op_log("보류", "알림 전송 비활성화")
            return
        if not quality_ok:
            self._trace(
                "SKIP — quality not ok: %s",
                result.get("image_quality_reason", "?"),
            )
            self._op_log("보류", result.get("image_quality_reason", "영상 품질 미달로 미발송"))
            return

        if captured_at == self._last_telegram_capture:
            self._trace("SKIP — same capture")
            self._op_log("보류", "동일 캡처 재분석 — 중복 발송 방지")
            return

        self._trace("DISPATCH — base=%s temp=%.1f", result.get("base", "?"), result["max_temp"])
        self._last_telegram_capture = captured_at
        self._op_log(
            "전송 시도",
            f"{result.get('overall_max_roi_name', 'ROI')} · {result['max_temp']:.1f}°C",
        )
        self._dispatch(result)

    # ── 전송 실행 ────────────────────────────────────────────

    def _dispatch(self, result: dict):
        temp = float(result.get("hot_temp_95", result.get("max_temp", 0.0)))
        overlay = result.get("overlay")
        base = str(result.get("base", "")) or "latest"
        robot_id = self._dash.cfg.identity.robot_id
        status = result.get("status", Status.CRITICAL)
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
                    robot_id=robot_id,
                    alert_id=alert_id,
                )
                self._trace("send_alarm returned ok=%s", ok)
                self._op_log(
                    "전송 성공" if ok else "전송 실패",
                    f"{robot_id} · {temp:.1f}°C · {'사진' if image_path else '텍스트'}",
                )
            except RuntimeError:
                self._op_log("미설정", ".env의 BOT_TOKEN / CHAT_ID를 확인하세요")
            except Exception as e:
                _log.error("Telegram dispatch error: %s", e, exc_info=True)
                self._op_log("오류", str(e))
            finally:
                if tmp_path:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

        threading.Thread(target=work, daemon=True).start()

    # ── 백엔드 측정값 POST ───────────────────────────────────

    def post_measurement(self, result: dict) -> None:
        """측정값을 POST /api/measurements 로 전송 (백그라운드 스레드)."""
        if not self._dash.cfg.backend.enabled:
            return

        camera_id = self._dash.cfg.identity.db_camera_id or 1
        roi = result.get("overall_max_roi")
        roi_id = getattr(roi, "db_roi_id", None) if roi is not None else 1
        if roi_id is None:
            roi_id = 1

        backend_event = result.get("_backend_posted_event")
        do_alarm = bool(result.get("alarm", False))

        self._trace(
            "post_measurement: do_alarm=%s base=%s status=%s",
            do_alarm, result.get("base", "?"), result["status"].value.lower(),
        )

        try:
            payload = {
                "camera_id": camera_id,
                "roi_id": roi_id,
                "max_temp": result["max_temp"],
                "min_temp": result.get("min_temp", 0.0),
                "mean_temp": result["mean_temp"],
                "percentile_95_temp": result["hot_temp_95"],
                "over_temp_pixels": result.get("over_temp_pixels", 0),
                "max_hotspot_size": result.get("max_hotspot_size", 0),
                "status": result["status"].value.lower(),
                "algorithm_version": "v2.0",
                "do_alarm": do_alarm,
                "alarm_message": (
                    f"{self._dash.cfg.identity.robot_id} · "
                    f"{result['max_temp']:.1f}°C · "
                    f"{result['status'].value}"
                ),
            }
            resp = requests.post(
                f"{self._dash.cfg.backend.url}/api/measurements",
                json=payload,
                timeout=self._dash.cfg.backend.timeout_sec,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "created":
                    result["alert_id"] = data.get("alert_id")
                    self._dash.metrics.api_successes += 1
                    _log.info(
                        "backend POST ok: capture_id=%s alert_id=%s",
                        data.get("capture_id"),
                        data.get("alert_id"),
                    )
                    self._trace("saved alert_id=%s to result dict", data.get("alert_id"))
                else:
                    _log.warning("backend POST rejected: %s", data)
            else:
                self._dash._record_api_result(False, status_code=resp.status_code, error_kind="http")
        except requests.exceptions.Timeout:
            self._dash.metrics.api_timeouts += 1
        except requests.exceptions.ConnectionError:
            self._dash.metrics.api_connection_errors += 1
        except Exception:
            self._dash.metrics.api_other_errors += 1
        finally:
            if backend_event is not None:
                try:
                    self._trace("backend_event.set() — alert_id=%s", result.get("alert_id"))
                    backend_event.set()
                except Exception:
                    pass

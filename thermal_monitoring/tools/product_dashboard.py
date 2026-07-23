"""현장 관리자용 상품형 열화상 모니터링 대시보드."""

from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import requests
import tkinter as tk
from PIL import Image, ImageTk
from tkinter import filedialog, messagebox, ttk

from ..analysis.overlay import create_overlay
from ..analysis.roi import (
    RoiResult,
    load_roi_config,
    extract_all_rois_from_npy,
    _get_roi_bounds_list,
    merge_roi_hotspot_centroids,
)
from ..analysis.threshold import (
    MonitorState,
    Status,
    evaluate_rois_with_state,
    apply_roi_state_updates,
)
from ..capture.capture import CaptureSession
from ..capture.thermal_utils import extract_from_jpeg
from ..config import load_config, save_config
from ..logger import get_logger

_file_log = get_logger("tools.dashboard")

try:
    RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
except AttributeError:  # Pillow < 9.1 compatibility on older Ubuntu systems
    RESAMPLE_LANCZOS = Image.LANCZOS


COLORS = {
    "navy": "#0b2038", "blue": "#1469be", "green": "#159447",
    "orange": "#e88925", "red": "#d64040", "bg": "#f3f6f9",
    "card": "#ffffff", "line": "#d9e2ea", "text": "#1f2e3d",
    "muted": "#647587", "dark": "#151b22",
}


@dataclass
class RuntimeMetrics:
    connection_attempts: int = 0
    connection_successes: int = 0
    capture_attempts: int = 0
    capture_successes: int = 0
    analysis_ok: int = 0          # 분석 정상 완료 (저장된 파일 기반)
    analysis_fail: int = 0        # 분석 실패
    image_quality_checks: int = 0
    image_quality_successes: int = 0
    exception_count: int = 0
    anomaly_today: int = 0

    @staticmethod
    def rate(ok: int, total: int) -> float:
        return 100.0 if total == 0 else ok * 100.0 / total


class ProductDashboard:
    REFRESH_SECONDS = 30
    REFRESH_FAST_SECONDS = 5    # Warning/Critical 상태일 때 분석 간격

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("로봇 열화상 모니터링")
        self.root.geometry("1440x900")
        self.root.minsize(1180, 760)
        self.root.configure(bg=COLORS["bg"])

        self.cfg = load_config(force_reload=True)
        self.lifecycle = "running"  # running -> closing -> closed
        self.monitoring = False
        self.capture_paused_by_user = False
        self.capture: Optional[CaptureSession] = None
        self.timer_id: Optional[str] = None
        self.processed: set[str] = set()
        self.state = MonitorState()
        self.metrics = RuntimeMetrics()
        self.latest_result: Optional[RoiResult] = None
        self.latest_status = Status.NORMAL
        self.last_update: Optional[datetime] = None
        self.visual_photo = None
        self.thermal_photo = None
        self.events: list[tuple] = []
        self.operating_logs: list[tuple[str, str, str, str]] = []
        # 최근 화면 품질을 기준으로 정상률을 계산한다. 누적 전체 기준보다
        # 현재 발생한 영상 이상이 즉시 수치에 반영된다.
        self._image_quality_window: list[bool] = []

        self._analysis_executor = ThreadPoolExecutor(max_workers=1)
        self._analysis_running = False
        self._analysis_pending = False
        self._analysis_generation = 0
        self._manual_capture_running = False

        self._configure_style()
        self._build_ui()
        self._set_system_state("확인 중", COLORS["orange"])
        self._check_connection_async()
        self._schedule_refresh(1000)

    def _configure_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=28, font=("맑은 고딕", 10),
                        background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=("맑은 고딕", 10, "bold"))
        style.configure("Action.TButton", font=("맑은 고딕", 10, "bold"), padding=7)

    def _build_ui(self):
        self._build_header()
        body = tk.Frame(self.root, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=20, pady=(14, 16))
        self._build_kpis(body)
        self._build_images(body)
        self._build_temperature_strip(body)
        self._build_events(body)
        self._build_footer(body)

    def _build_header(self):
        header = tk.Frame(self.root, bg=COLORS["navy"], height=76)
        header.pack(fill="x"); header.pack_propagate(False)
        left = tk.Frame(header, bg=COLORS["navy"]); left.pack(side="left", padx=22, pady=10)
        tk.Label(left, text="로봇 열화상 모니터링", bg=COLORS["navy"], fg="white",
                 font=("맑은 고딕", 20, "bold")).pack(anchor="w")
        tk.Label(left, text=f"1공장  ·  조립라인 A  ·  {self.cfg.identity.robot_id}",
                 bg=COLORS["navy"], fg="#c7d6e5", font=("맑은 고딕", 10)).pack(anchor="w")

        right = tk.Frame(header, bg=COLORS["navy"]); right.pack(side="right", padx=20, pady=17)
        self.header_state = tk.Label(right, text="● 확인 중", bg=COLORS["navy"], fg="#ffd166",
                                     font=("맑은 고딕", 11, "bold"))
        self.header_state.pack(side="left", padx=12)
        self.header_time = tk.Label(right, text="마지막 갱신 —", bg=COLORS["navy"], fg="#c7d6e5",
                                    font=("맑은 고딕", 10))
        self.header_time.pack(side="left", padx=12)
        self.header_refresh_interval = tk.Label(
            right, text=f"{self.REFRESH_SECONDS}초마다 자동 갱신",
            bg=COLORS["navy"], fg="#c7d6e5", font=("맑은 고딕", 10),
        )
        self.header_refresh_interval.pack(side="left", padx=12)
        self.capture_toggle_button = ttk.Button(
            right, text="■  촬영 정지", style="Action.TButton",
            command=self.toggle_capture,
        )
        self.capture_toggle_button.pack(side="left", padx=4)
        self.refresh_button = ttk.Button(
            right, text="↻  새로고침", style="Action.TButton",
            command=self.capture_and_refresh,
        )
        self.refresh_button.pack(side="left", padx=4)
        ttk.Button(right, text="▤  운영 로그", style="Action.TButton",
                   command=self.open_operating_log).pack(side="left", padx=4)
        ttk.Button(right, text="⚙  환경설정", style="Action.TButton",
                   command=self.open_settings).pack(side="left", padx=4)

    def _card(self, parent, title, value, caption, color):
        frame = tk.Frame(parent, bg="white", highlightbackground=COLORS["line"],
                         highlightthickness=1, padx=18, pady=12)
        tk.Label(frame, text=title, bg="white", fg=COLORS["muted"],
                 font=("맑은 고딕", 10, "bold")).pack(anchor="w")
        val = tk.Label(frame, text=value, bg="white", fg=color,
                       font=("맑은 고딕", 22, "bold"))
        val.pack(anchor="w", pady=(2, 0))
        if caption:
            tk.Label(frame, text=caption, bg="white", fg=COLORS["muted"],
                     font=("맑은 고딕", 9)).pack(anchor="w")
        return frame, val

    def _build_kpis(self, parent):
        row = tk.Frame(parent, bg=COLORS["bg"]); row.pack(fill="x")
        cards = [
            ("현재 상태", "정상", "", COLORS["green"]),
            ("최고 온도", "-- °C", f"경고 기준 {self.cfg.roi.baseline_temp + self.cfg.roi.warning_delta:.1f}°C", COLORS["blue"]),
            ("금일 이상 감지", "0건", "미확인 0건", COLORS["orange"]),
            ("모니터링 정상률", "100.0%", "최근 영상 품질 기준", COLORS["blue"]),
        ]
        self.kpi_values = []
        for i, args in enumerate(cards):
            f, label = self._card(row, *args)
            f.pack(side="left", fill="x", expand=True, padx=(0 if i == 0 else 6, 0))
            self.kpi_values.append(label)

    def _image_panel(self, parent, title):
        frame = tk.Frame(parent, bg="white", highlightbackground=COLORS["line"], highlightthickness=1)
        head = tk.Frame(frame, bg="white"); head.pack(fill="x", padx=12, pady=8)
        tk.Label(head, text=title, bg="white", fg=COLORS["text"],
                 font=("맑은 고딕", 12, "bold")).pack(side="left")
        stamp = tk.Label(head, text="촬영 시각 —", bg="white", fg=COLORS["muted"], font=("맑은 고딕", 9))
        stamp.pack(side="right")
        image = tk.Label(frame, text="첫 이미지 수신 대기 중", bg=COLORS["dark"], fg="#9aa9b6",
                         font=("맑은 고딕", 11))
        image.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        return frame, image, stamp

    def _build_images(self, parent):
        row = tk.Frame(parent, bg=COLORS["bg"]); row.pack(fill="both", expand=True, pady=(12, 0))
        left, self.visual_label, self.visual_stamp = self._image_panel(row, "가시광 이미지")
        right, self.thermal_label, self.thermal_stamp = self._image_panel(row, "열화상 이미지")
        left.pack(side="left", fill="both", expand=True, padx=(0, 6))
        right.pack(side="left", fill="both", expand=True, padx=(6, 0))

    def _build_temperature_strip(self, parent):
        strip = tk.Frame(parent, bg="white", highlightbackground=COLORS["line"], highlightthickness=1)
        strip.pack(fill="x", pady=(10, 0))
        self.temp_labels = []
        for title, value, color in [("ROI 최고", "-- °C", COLORS["red"]), ("평균", "-- °C", COLORS["blue"]),
                                    ("95th", "-- °C", "#7347b8"), ("핫스팟", "0개", "#16808f")]:
            cell = tk.Frame(strip, bg="white"); cell.pack(side="left", fill="x", expand=True, padx=14, pady=9)
            tk.Label(cell, text=title, bg="white", fg=COLORS["muted"], font=("맑은 고딕", 10, "bold")).pack(side="left")
            lab = tk.Label(cell, text=value, bg="white", fg=color, font=("맑은 고딕", 16, "bold"))
            lab.pack(side="right"); self.temp_labels.append(lab)

    def _build_events(self, parent):
        outer = tk.Frame(parent, bg="white", highlightbackground=COLORS["line"], highlightthickness=1)
        outer.pack(fill="x", pady=(10, 0))
        head = tk.Frame(outer, bg="white"); head.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(head, text="최근 이상 감지 이력", bg="white", fg=COLORS["text"],
                 font=("맑은 고딕", 12, "bold")).pack(side="left")
        ttk.Button(head, text="선택 항목 확인 처리", command=self.acknowledge_selected).pack(side="right", padx=4)
        ttk.Button(head, text="이상 이력 전체보기", command=self.show_all_events).pack(side="right", padx=4)
        columns = ("time", "asset", "state", "temp", "action")
        self.event_tree = ttk.Treeview(outer, columns=columns, show="headings", height=4)
        for key, label, width in [("time", "발생 시각", 170), ("asset", "설비", 250),
                                  ("state", "상태", 110), ("temp", "최고 온도", 110), ("action", "처리 상태", 150)]:
            self.event_tree.heading(key, text=label); self.event_tree.column(key, width=width, anchor="center")
        self.event_tree.pack(fill="x", padx=12, pady=(0, 10))

    def _build_footer(self, parent):
        footer = tk.Frame(parent, bg=COLORS["bg"]); footer.pack(fill="x", pady=(8, 0))
        self.metric_label = tk.Label(footer, bg=COLORS["bg"], fg=COLORS["muted"],
                                     font=("맑은 고딕", 9))
        self.metric_label.pack(side="left")
        ttk.Button(footer, text="상세 운영 로그", command=self.open_operating_log).pack(side="right")
        self._update_metric_text()

    def _set_system_state(self, text, color):
        self.header_state.configure(text=f"● {text}", fg=color)

    def _check_connection_async(self):
        self.metrics.connection_attempts += 1
        def work():
            ok = False
            try:
                response = requests.get(f"http://{self.cfg.camera.ip}/api/image/current?imgformat=JPEG", timeout=5)
                ok = response.status_code == 200
            except Exception:
                self.metrics.exception_count += 1
            if ok:
                self.metrics.connection_successes += 1
            if self.lifecycle == "running":
                self.root.after(0, lambda: self._connection_result(ok))
        threading.Thread(target=work, daemon=True).start()

    def _connection_result(self, ok):
        if ok:
            self._add_operating_log("연결", "성공", f"카메라 {self.cfg.camera.ip} 응답 확인")
            if self.capture_paused_by_user:
                self._set_system_state("촬영 정지", COLORS["orange"])
            else:
                self._set_system_state("정상 운영 중", COLORS["green"])
            if not self.monitoring and not self.capture_paused_by_user:
                self.start_monitoring()
        else:
            self._set_system_state("연결 지연", COLORS["orange"])
            self._add_operating_log("연결", "실패", f"카메라 {self.cfg.camera.ip} 응답 없음")
        self._update_metric_text()

    def start_monitoring(self):
        if self.monitoring or self.lifecycle != "running":
            return
        self.capture_paused_by_user = False
        self.monitoring = True
        self.capture_toggle_button.configure(text="■  촬영 정지")
        roi = self.cfg.roi
        baseline = roi.baseline_temp
        warning_delta = roi.warning_delta

        def _probe_callback(max_temp: float) -> bool:
            capture = self.capture
            if not self.monitoring or capture is None:
                return False
            if max_temp >= baseline + warning_delta:
                capture.set_warning_mode(True)
                self._schedule_refresh(100)  # 즉시 분석 가속
                w_interval = getattr(capture, '_warning_interval', 5.0)
                self._add_operating_log("프로브", "과열 감지", f"{max_temp:.1f}°C — 캡처 주기 {w_interval:.0f}초로 전환")
                return True
            else:
                capture.set_warning_mode(False)
                return False

        self.capture = CaptureSession(
            cam_ip=self.cfg.camera.ip, mode=self.cfg.tools.mode,
            interval=max(10.0, float(self.cfg.camera.capture_interval_sec)),
            save_dir=self.cfg.paths.dataset_dir, log_callback=self._capture_log,
            probe_callback=_probe_callback,
        )
        self.capture.start()

    def toggle_capture(self):
        """현장 사용자가 촬영만 정지하거나 다시 시작할 수 있게 한다."""
        if self.monitoring:
            self.stop_monitoring()
        else:
            self.start_monitoring()
            self._set_system_state("정상 운영 중", COLORS["green"])
            self._add_operating_log("촬영", "시작", "사용자가 촬영을 다시 시작함")

    def stop_monitoring(self):
        if not self.monitoring:
            return
        self.capture_paused_by_user = True
        self.monitoring = False
        capture = self.capture
        self.capture = None
        if capture:
            capture.request_stop()
        self.capture_toggle_button.configure(text="▶  촬영 시작")
        self._set_system_state("촬영 정지", COLORS["orange"])
        self._add_operating_log("촬영", "정지", "사용자가 촬영을 정지함")

    def _capture_log(self, message: str):
        if "saved" in message:
            self.metrics.capture_attempts += 1; self.metrics.capture_successes += 1
            self._add_operating_log("캡처", "성공", message)
        elif any(word in message.lower() for word in ("error", "timeout", "http", "connection")):
            self.metrics.capture_attempts += 1; self.metrics.exception_count += 1
            self._add_operating_log("캡처", "예외 처리", message)
        self._update_metric_text_async()

    def _schedule_refresh(self, delay_ms=None):
        if self.lifecycle != "running":
            return
        if self.timer_id:
            self.root.after_cancel(self.timer_id)
        self.timer_id = self.root.after(delay_ms or self.REFRESH_SECONDS * 1000, self.refresh_now)

    def refresh_now(self):
        """자동 타이머용: 저장된 최신 촬영 결과를 다시 분석한다."""
        if self.lifecycle != "running":
            return
        self.timer_id = None
        self._schedule_analysis()
        self._update_metric_text()
        # 환경설정에서 지정한 화면 갱신 주기를 상태와 관계없이 적용한다.
        self._schedule_refresh(self.REFRESH_SECONDS * 1000)

    def capture_and_refresh(self):
        """버튼 클릭 시 새 Thermal/Visual을 촬영하고 그 결과로 화면을 갱신한다."""
        if self.lifecycle != "running" or self._manual_capture_running:
            return
        capture = self.capture
        if not self.monitoring or capture is None or not capture.running:
            messagebox.showinfo(
                "새로고침",
                "촬영이 정지되어 있습니다. 촬영 시작 후 다시 시도하세요.",
                parent=self.root,
            )
            return

        if self.timer_id:
            self.root.after_cancel(self.timer_id)
            self.timer_id = None
        self._manual_capture_running = True
        self.refresh_button.configure(text="촬영 중...", state="disabled")
        self._add_operating_log("수동 촬영", "시작", "새로고침 버튼으로 즉시 촬영 요청")
        self._analysis_executor.submit(self._run_capture_refresh_worker, capture)

    def _run_capture_refresh_worker(self, capture: CaptureSession):
        try:
            thermal_path, visual_path = capture.capture_both_once()
            if not thermal_path:
                raise RuntimeError("새 열화상 이미지를 촬영하지 못했습니다.")
            if self.cfg.tools.mode == "both" and not visual_path:
                raise RuntimeError("새 가시광 이미지를 촬영하지 못했습니다.")

            thermal = Path(thermal_path)
            visual = Path(visual_path) if visual_path else None
            npy = thermal.with_name(f"{thermal.stem}_thermal.npy")
            matrix, _ = extract_from_jpeg(str(thermal))
            np.save(npy, matrix)
            pair = (thermal.stem, thermal, visual, npy)
            result = self._process_pair_to_dict(pair)
            self.root.after(0, lambda: self._apply_capture_refresh_result(result))
        except Exception as exc:
            message = str(exc)
            self.root.after(0, lambda msg=message: self._handle_capture_refresh_error(msg))

    def _apply_capture_refresh_result(self, result: dict):
        try:
            self._add_operating_log(
                "수동 촬영", "완료", f"{result['base']} 촬영 및 화면 갱신 완료"
            )
            self._apply_analysis_result(result, self._analysis_generation)
        finally:
            self._finish_capture_refresh()

    def _handle_capture_refresh_error(self, message: str):
        try:
            self._add_operating_log("수동 촬영", "실패", message)
            self._handle_analysis_error(message, self._analysis_generation)
            messagebox.showerror("새로고침 실패", message, parent=self.root)
        finally:
            self._finish_capture_refresh()

    def _finish_capture_refresh(self):
        self._manual_capture_running = False
        if self.lifecycle != "running":
            return
        self.refresh_button.configure(text="↻  새로고침", state="normal")
        self._schedule_refresh(self.REFRESH_SECONDS * 1000)

    def _schedule_analysis(self):
        if self._analysis_running:
            self._analysis_pending = True
            return
        self._analysis_running = True
        self._analysis_pending = False
        gen = self._analysis_generation + 1
        self._analysis_generation = gen
        self._analysis_executor.submit(self._run_analysis_worker, gen)

    def _run_analysis_worker(self, generation: int):
        try:
            pair = self._latest_pair()
            if not pair:
                self.root.after(0, lambda: self._finish_analysis(generation))
                return
            result = self._process_pair_to_dict(pair)
            self.root.after(0, lambda: self._apply_analysis_result(result, generation))
        except Exception as exc:
            message = str(exc)
            self.root.after(
                0,
                lambda msg=message: self._handle_analysis_error(msg, generation),
            )

    def _handle_analysis_error(self, message: str, generation: int):
        """Worker 오류를 Tk 메인 스레드에서 로그와 화면에 반영한다."""
        if self.lifecycle != "running":
            self._finish_analysis(generation)
            return
        self.metrics.exception_count += 1
        self._add_operating_log("분석", "예외 처리", message)
        self._append_event("Error", 0.0, f"분석 예외: {message}")
        self._update_metric_text()
        self._finish_analysis(generation)

    def _latest_pair(self):
        """Return the newest thermal, visual and NPY paths for analysis."""
        dataset = Path(self.cfg.paths.dataset_dir)
        if not dataset.exists():
            return None

        thermal_files = sorted(
            path for path in dataset.glob("*.jpg")
            if "_visual" not in path.name
        )
        if not thermal_files:
            return None

        # Thermal과 Visual은 병렬 요청 후 각각 저장되므로 아주 짧은 시간
        # 동안 Thermal 파일만 존재할 수 있다. 5초까지는 직전 완성 쌍을
        # 사용하고, 그 이후에도 Visual이 없으면 최신 쌍을 품질 이상으로
        # 전달해 정상률과 운영 로그에 즉시 반영한다.
        if self.cfg.tools.mode == "both":
            newest = thermal_files[-1]
            newest_visual = dataset / f"{newest.stem}_visual.jpg"
            newest_age = max(0.0, time.time() - newest.stat().st_mtime)
            if newest_visual.exists() or newest_age >= 5.0:
                thermal = newest
            else:
                thermal = next(
                    (
                        path for path in reversed(thermal_files[:-1])
                        if (dataset / f"{path.stem}_visual.jpg").exists()
                    ),
                    None,
                )
            if thermal is None:
                return None
        else:
            thermal = thermal_files[-1]
        base = thermal.stem
        visual = dataset / f"{base}_visual.jpg"
        npy = dataset / f"{base}_thermal.npy"
        if not npy.exists():
            matrix, _ = extract_from_jpeg(str(thermal))
            np.save(npy, matrix)
        return base, thermal, visual if visual.exists() else None, npy

    def _process_pair_to_dict(self, pair) -> dict:
        base, thermal, visual, npy = pair
        captured_at = self._capture_time_from_file(base, thermal)
        thermal_img = cv2.imread(str(thermal))
        visual_img = None
        if visual and visual.exists():
            visual_img = cv2.imread(str(visual))
        image_quality_ok, image_quality_reason = self._assess_image_quality(
            thermal_img, visual_img
        )

        roi_cfg = load_roi_config()
        roi_results = extract_all_rois_from_npy(str(npy), roi_cfg)

        per_roi_statuses, worst, alarm = evaluate_rois_with_state(
            roi_results,
            baseline=roi_cfg.baseline_temp,
            warning_delta=roi_cfg.warning_delta,
            critical_delta=roi_cfg.critical_delta,
            state=self.state,
        )
        status = worst["status"]
        roi_result = worst["roi"]

        apply_roi_state_updates(self.state, per_roi_statuses)

        # 전역 최악 상태 갱신
        self.state.status = status

        roi_result.hotspot_centroids = merge_roi_hotspot_centroids(roi_results)

        overlay = create_overlay(
            # This panel is explicitly the Thermal view. Passing the visual
            # path would make create_overlay use RGB as its background when a
            # Homography exists, duplicating the visible-image panel.
            str(thermal), "", roi_result.roi_bounds,
            roi_result.max_temp, roi_result.mean_temp, roi_result.hot_temp_95,
            status.value, hotspot_centroids=roi_result.hotspot_centroids,
            roi_bounds_list=_get_roi_bounds_list(roi_cfg),
            roi_names=[r.roi_name for r in roi_results] if len(roi_results) > 1 else None,
        )

        return {
            "base": base, "overlay": overlay, "visual_img": visual_img,
            "max_temp": roi_result.max_temp, "mean_temp": roi_result.mean_temp,
            "hot_temp_95": roi_result.hot_temp_95,
            "hotspot_count": len(roi_result.hotspot_centroids),
            "status": status, "alarm": alarm,
            "roi_bounds": roi_result.roi_bounds,
            "roi_results": roi_results,
            "captured_at": captured_at,
            "image_quality_ok": image_quality_ok,
            "image_quality_reason": image_quality_reason,
        }

    @staticmethod
    def _capture_time_from_file(base: str, thermal: Path) -> datetime:
        """저장 파일명에 기록된 캡처 요청 시각을 읽는다.

        CaptureSession은 촬영 직전에 YYYYmmddHHMMSS_ffffff 형식으로
        파일명을 생성한다. 이전 형식의 파일도 표시할 수 있게 초 단위
        형식을 함께 지원하고, 형식이 다른 외부 파일은 수정 시각을 쓴다.
        """
        for timestamp_format in ("%Y%m%d%H%M%S_%f", "%Y%m%d%H%M%S"):
            try:
                return datetime.strptime(base, timestamp_format)
            except ValueError:
                continue
        return datetime.fromtimestamp(thermal.stat().st_mtime)

    def _assess_image_quality(self, thermal_img, visual_img) -> tuple[bool, str]:
        """화면에 표시할 Thermal/Visual 한 쌍이 서로 유효한지 검사한다."""
        if thermal_img is None or thermal_img.size == 0:
            return False, "열화상 이미지 누락 또는 읽기 실패"

        if self.cfg.tools.mode != "both":
            return True, "정상"

        if visual_img is None or visual_img.size == 0:
            return False, "가시광 이미지 누락 또는 읽기 실패"

        thermal_shape = thermal_img.shape[:2]
        visual_shape = visual_img.shape[:2]
        if thermal_shape == visual_shape:
            return False, "가시광·열화상 영상 종류 중복 의심(동일 해상도)"

        # 현재 카메라 데이터 규격은 Visual이 Thermal보다 고해상도다.
        # 역전되면 파일 종류가 뒤바뀌었을 가능성이 높다.
        if thermal_img.size >= visual_img.size:
            return False, "가시광·열화상 영상 종류 혼동 의심(해상도 역전)"

        thermal_small = cv2.resize(thermal_img, (160, 120))
        visual_small = cv2.resize(visual_img, (160, 120))
        mean_difference = float(cv2.absdiff(thermal_small, visual_small).mean())
        if mean_difference < 3.0:
            return False, "가시광·열화상 동일 영상 감지"

        return True, "정상"

    def _apply_analysis_result(self, result: dict, generation: int):
        if self.lifecycle != "running":
            self._finish_analysis(generation)
            return
        if generation < self._analysis_generation:
            return

        status = result["status"]
        previous = self.state.status
        self.state.status = status
        if status != Status.NORMAL and previous == Status.NORMAL:
            self.metrics.anomaly_today += 1
            self._append_event(status.value, result["max_temp"], "확인 필요")
        elif status == Status.NORMAL and previous != Status.NORMAL:
            if self.capture:
                self.capture.set_warning_mode(False)
            self._add_operating_log("과열 해제", "정상 복귀",
                                    f"캡처 주기 {self.capture._normal_interval:.0f}초로 복원" if self.capture
                                    else "정상 복귀")

        quality_ok = bool(result.get("image_quality_ok", False))
        # 두 영상은 검증을 통과한 한 쌍일 때만 동시에 교체한다. 한쪽씩
        # 갱신하면 캡처 저장 시차나 잘못된 파일 쌍 때문에 좌우 영상이
        # 뒤바뀌어 보일 수 있으므로, 이상 프레임은 표시하지 않고 직전의
        # 정상 화면을 유지한다.
        if quality_ok:
            self._show_image(self.visual_label, result["visual_img"], "visual")
            self._show_image(self.thermal_label, result["overlay"], "thermal")
        else:
            if self.visual_photo is None:
                self._show_image(self.visual_label, None, "visual")
            if self.thermal_photo is None:
                self._show_image(self.thermal_label, None, "thermal")

        self.metrics.image_quality_checks += 1
        if quality_ok:
            self.metrics.image_quality_successes += 1
        else:
            self._add_operating_log(
                "영상 품질", "비정상", result.get("image_quality_reason", "영상 확인 필요")
            )
        self._image_quality_window.append(quality_ok)
        del self._image_quality_window[:-20]
        self.latest_status = status
        self.last_update = datetime.now()
        self.metrics.analysis_ok += 1
        self._add_operating_log("분석", "정상 완료",
                                f"{result['base']} · {status.value} · Max {result['max_temp']:.1f}°C")
        self._update_values_with_result(result)
        self._finish_analysis(generation)

    def _update_values_with_result(self, result: dict):
        s = result["status"]
        korean = {Status.NORMAL: "정상", Status.WARNING: "경고", Status.CRITICAL: "위험"}[s]
        color = {Status.NORMAL: COLORS["green"], Status.WARNING: COLORS["orange"], Status.CRITICAL: COLORS["red"]}[s]
        self.kpi_values[0].configure(text=korean, fg=color)
        self.kpi_values[1].configure(text=f"{result['max_temp']:.1f} °C")
        self.kpi_values[2].configure(text=f"{self.metrics.anomaly_today}건")
        quality_rate = (
            100.0 * sum(self._image_quality_window) / len(self._image_quality_window)
            if self._image_quality_window else 100.0
        )
        quality_color = (
            COLORS["green"] if quality_rate == 100.0
            else COLORS["orange"] if quality_rate >= 80.0
            else COLORS["red"]
        )
        self.kpi_values[3].configure(text=f"{quality_rate:.1f}%", fg=quality_color)
        vals = (f"{result['max_temp']:.1f} °C", f"{result['mean_temp']:.1f} °C",
                f"{result['hot_temp_95']:.1f} °C", f"{result['hotspot_count']}개")
        for lab, value in zip(self.temp_labels, vals):
            lab.configure(text=value)
        if result.get("image_quality_ok", False):
            captured_at = result.get("captured_at") or self.last_update
            stamp = captured_at.strftime("촬영 시각 %Y-%m-%d %H:%M:%S")
            self.visual_stamp.configure(text=stamp)
            self.thermal_stamp.configure(text=stamp)
        else:
            issue = result.get("image_quality_reason", "영상 종류 확인 필요")
            hold_text = f"갱신 보류 · {issue}"
            self.visual_stamp.configure(text=hold_text)
            self.thermal_stamp.configure(text=hold_text)
        self.header_time.configure(text=self.last_update.strftime("마지막 갱신 %H:%M:%S"))

    def _finish_analysis(self, generation: int):
        self._analysis_running = False
        if self._analysis_pending:
            self.root.after(0, self._schedule_analysis)

    def _draw_visible_roi(self, img, roi):
        x1, y1, x2, y2 = roi; h, w = img.shape[:2]
        sx, sy = w / 640.0, h / 480.0
        cv2.rectangle(img, (int(x1*sx), int(y1*sy)), (int(x2*sx), int(y2*sy)), (0, 255, 0), max(2, w//700))
        cv2.putText(img, "ROI-01", (int(x1*sx), max(25, int(y1*sy)-8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,0), 2)

    def _show_image(self, label, image, kind):
        if image is None:
            missing_text = (
                "열화상 이미지를 불러올 수 없습니다"
                if kind == "thermal"
                else "가시광 이미지를 불러올 수 없습니다"
            )
            label.configure(image="", text=missing_text)
            if kind == "thermal":
                self.thermal_photo = None
            else:
                self.visual_photo = None
            return
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb); pil.thumbnail((650, 340), RESAMPLE_LANCZOS)
        photo = ImageTk.PhotoImage(pil)
        label.configure(image=photo, text="")
        if kind == "thermal": self.thermal_photo = photo
        else: self.visual_photo = photo

    def _append_event(self, state, temp, action):
        row = (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               f"1공장 · 조립라인 A · {self.cfg.identity.robot_id}", state,
               f"{temp:.1f} °C" if temp else "—", action)
        self.events.insert(0, row); self.event_tree.insert("", 0, values=row)

    def acknowledge_selected(self):
        selected = self.event_tree.selection()
        if not selected:
            messagebox.showinfo("확인 처리", "처리할 이상 항목을 선택하세요.", parent=self.root); return
        for item in selected:
            values = list(self.event_tree.item(item, "values")); values[4] = "확인 완료"
            self.event_tree.item(item, values=values)

    def show_all_events(self):
        messagebox.showinfo("이상 이력", "현재 시제품은 실행 중 감지 이력을 표시합니다.\nDB 연동 시 전체 기간 조회가 제공됩니다.", parent=self.root)

    def _update_metric_text_async(self):
        if self.lifecycle == "running": self.root.after(0, self._update_metric_text)

    def _update_metric_text(self):
        m = self.metrics
        self.metric_label.configure(text=(
            f"카메라 연결 {m.rate(m.connection_successes,m.connection_attempts):.1f}%   ·   "
            f"캡처 성공 {m.rate(m.capture_successes,m.capture_attempts):.1f}%   ·   "
            f"분석 정상 완료 {m.analysis_ok}회   ·   예외 처리 {m.exception_count}회"))

    def open_operating_log(self):
        win = tk.Toplevel(self.root); win.title("운영 로그"); win.geometry("920x520"); win.transient(self.root)
        summary = ttk.LabelFrame(win, text="운영 지표", padding=10); summary.pack(fill="x", padx=12, pady=(12,6))
        m = self.metrics
        ttk.Label(summary, text=(f"연결 성공률 {m.rate(m.connection_successes,m.connection_attempts):.1f}%   |   "
                                 f"캡처 성공률 {m.rate(m.capture_successes,m.capture_attempts):.1f}%   |   "
                                 f"분석 정상 완료 {m.analysis_ok}회   |   예외 처리 {m.exception_count}회   |   "
                                 f"상태 {self.lifecycle}"), font=("맑은 고딕",10,"bold")).pack(anchor="w")
        frame = ttk.LabelFrame(win, text="시간순 기록", padding=8); frame.pack(fill="both", expand=True, padx=12, pady=(6,12))
        columns = ("time", "category", "result", "detail")
        tree = ttk.Treeview(frame, columns=columns, show="headings")
        for key, label, width in (("time","시각",155),("category","구분",90),("result","결과",100),("detail","상세 내용",510)):
            tree.heading(key,text=label); tree.column(key,width=width,anchor="w" if key=="detail" else "center")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview); tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left",fill="both",expand=True); scroll.pack(side="right",fill="y")
        for row in self.operating_logs:
            tree.insert("", "end", values=row)

    def _add_operating_log(self, category: str, result: str, detail: str):
        row = (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), category, result, detail)
        self.operating_logs.insert(0, row)
        del self.operating_logs[1000:]
        _file_log.info("[%s] %s | %s", category, result, detail)

    def open_settings(self):
        SettingsDialog(self)

    def on_close(self):
        if self.lifecycle != "running": return
        self.lifecycle = "closing"
        self._add_operating_log("프로그램", "종료 시작", "running → closing")
        self._set_system_state("종료 중", COLORS["orange"])
        if self.timer_id:
            self.root.after_cancel(self.timer_id); self.timer_id = None
        if self.capture:
            self.capture.request_stop()
        self.monitoring = False
        self.lifecycle = "closed"
        self._add_operating_log("프로그램", "종료 완료", "closing → closed")
        self._analysis_executor.shutdown(wait=False)
        self.root.destroy()


class SettingsDialog:
    def __init__(self, dashboard: ProductDashboard):
        self.d = dashboard; self.win = tk.Toplevel(dashboard.root)
        self.win.title("환경설정"); self.win.geometry("720x620"); self.win.transient(dashboard.root); self.win.grab_set()
        notebook = ttk.Notebook(self.win); notebook.pack(fill="both", expand=True, padx=14, pady=14)
        general = ttk.Frame(notebook, padding=16); roi = ttk.Frame(notebook, padding=16); advanced = ttk.Frame(notebook, padding=16)
        notebook.add(general, text="일반"); notebook.add(roi, text="감시 영역"); notebook.add(advanced, text="고급 설정")
        self.ip = tk.StringVar(value=self.d.cfg.camera.ip)
        self.dataset_dir = tk.StringVar(value=self.d.cfg.paths.dataset_dir)
        self.baseline = tk.StringVar(value=str(self.d.cfg.roi.baseline_temp))
        self.warning = tk.StringVar(value=str(self.d.cfg.roi.warning_delta))
        self.critical = tk.StringVar(value=str(self.d.cfg.roi.critical_delta))
        self._field(general, "카메라 주소", self.ip, 0)
        self._path_field(general, "데이터 저장 폴더", self.dataset_dir, 1)
        ttk.Label(general, text="촬영 이미지, 온도 배열과 오버레이가 선택한 폴더에 저장됩니다.").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=12)
        ttk.Label(roi, text="가시광 이미지에서 감시할 설비 영역을 지정합니다.", font=("맑은 고딕", 11, "bold")).pack(anchor="w", pady=8)
        ttk.Button(roi, text="가시광 이미지에서 ROI 설정", command=self.open_roi_editor).pack(anchor="w", pady=8)
        ttk.Separator(roi, orient="horizontal").pack(fill="x", pady=14)
        ttk.Label(roi, text="Thermal-RGB 위치 보정", font=("맑은 고딕", 11, "bold")).pack(anchor="w", pady=(0,4))
        ttk.Label(roi, text="카메라 설치 위치가 바뀐 경우 두 영상의 대응점을 다시 지정합니다.").pack(anchor="w", pady=(0,8))
        ttk.Button(roi, text="캘리브레이션 실행", command=self.open_calibration).pack(anchor="w", pady=4)
        self._field(advanced, "정상 기준 온도(°C)", self.baseline, 0)
        self._field(advanced, "경고 상승폭(°C)", self.warning, 1)
        self._field(advanced, "위험 상승폭(°C)", self.critical, 2)
        buttons = ttk.Frame(self.win); buttons.pack(fill="x", padx=14, pady=(0,14))
        ttk.Button(buttons, text="취소", command=self.win.destroy).pack(side="right", padx=4)
        ttk.Button(buttons, text="저장", style="Action.TButton", command=self.save).pack(side="right", padx=4)

    @staticmethod
    def _field(parent, label, variable, row):
        ttk.Label(parent, text=label).grid(row=row,column=0,sticky="w",pady=8,padx=(0,12))
        ttk.Entry(parent, textvariable=variable, width=34).grid(row=row,column=1,sticky="ew",pady=8)
        parent.columnconfigure(1, weight=1)

    def _path_field(self, parent, label, variable, row):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=8, padx=(0, 12))
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=8)
        ttk.Button(parent, text="찾아보기...", command=self._browse_dataset_dir).grid(
            row=row, column=2, sticky="e", padx=(8, 0), pady=8)
        parent.columnconfigure(1, weight=1)

    def _browse_dataset_dir(self):
        current = os.path.expandvars(os.path.expanduser(self.dataset_dir.get().strip()))
        initial = current if current and os.path.isdir(current) else os.getcwd()
        selected = filedialog.askdirectory(
            parent=self.win,
            title="데이터 저장 폴더 선택",
            initialdir=initial,
            mustexist=False,
        )
        if selected:
            self.dataset_dir.set(os.path.normpath(selected))

    def open_roi_editor(self):
        dataset = Path(self.d.cfg.paths.dataset_dir)
        if not dataset.exists():
            messagebox.showwarning("ROI 설정", "데이터셋 폴더가 없습니다.", parent=self.win); return
        jpgs = sorted(dataset.glob("*.jpg"))
        thermal_files = [p for p in jpgs if "_visual" not in p.name]
        if not thermal_files:
            messagebox.showwarning("ROI 설정", "이미지가 없습니다. 먼저 이미지를 수집하세요.", parent=self.win); return
        thermal = thermal_files[-1]
        visual = dataset / f"{thermal.stem}_visual.jpg"
        self.d._add_operating_log("ROI 설정", "시작", str(visual if visual.exists() else thermal))
        self.win.grab_release()
        try:
            from .roi_selector import main as roi_main
            if visual.exists():
                sys.argv = ["roi_selector", str(thermal), str(visual)]
            else:
                sys.argv = ["roi_selector", str(thermal)]
            roi_main()
            self.d.cfg = load_config(force_reload=True)
            self.d._add_operating_log("ROI 설정", "완료", f"{len(self.d.cfg.roi.rois)}개 영역 저장됨")
        except Exception as exc:
            self.d._add_operating_log("ROI 설정", "예외 처리", str(exc))
            messagebox.showerror("ROI 설정", str(exc), parent=self.win)
        finally:
            if self.win.winfo_exists():
                self.win.grab_set()

    def open_calibration(self):
        dataset = Path(self.d.cfg.paths.dataset_dir)
        thermal_files = sorted(p for p in dataset.glob("*.jpg") if "_visual" not in p.name) if dataset.exists() else []
        if not thermal_files:
            messagebox.showwarning("캘리브레이션", "Thermal 이미지가 없습니다. 먼저 이미지를 수집하세요.", parent=self.win); return
        thermal = thermal_files[-1]; visual = dataset / f"{thermal.stem}_visual.jpg"
        if not visual.exists():
            messagebox.showwarning("캘리브레이션", "대응하는 가시광 이미지가 없습니다.", parent=self.win); return
        self.d._add_operating_log("캘리브레이션", "시작", thermal.name)
        self.win.grab_release()
        try:
            from .calibration import run_calibration
            saved = run_calibration(str(thermal), str(visual))
            if saved:
                self.d._add_operating_log("캘리브레이션", "완료", self.d.cfg.paths.homography_path)
                # ROI selector 자동 실행 유도
                if messagebox.askyesno(
                    "ROI 설정",
                    "캘리브레이션이 완료되었습니다.\n\n"
                    "가시광 이미지에서 ROI 영역을 설정하시겠습니까?\n"
                    "(가시광에서 지정한 ROI는 열화상 좌표로 자동 변환됩니다.)",
                    parent=self.win,
                ):
                    self.win.grab_set()  # 선취득 → roi_editor 진입 직전 release
                    self.open_roi_editor()
            else:
                self.d._add_operating_log("캘리브레이션", "종료", "저장 없이 종료")
        except Exception as exc:
            self.d.metrics.exception_count += 1
            self.d._add_operating_log("캘리브레이션", "예외 처리", str(exc))
            messagebox.showerror("캘리브레이션", str(exc), parent=self.win)
        finally:
            if self.win.winfo_exists():
                self.win.grab_set()

    def save(self):
        try:
            camera_ip = self.ip.get().strip()
            dataset_value = self.dataset_dir.get().strip()
            if not dataset_value:
                messagebox.showerror("입력 오류", "데이터 저장 폴더를 선택하세요.", parent=self.win)
                return

            dataset_path = os.path.normpath(os.path.expandvars(os.path.expanduser(dataset_value)))
            overlay_path = os.path.join(dataset_path, "overlay")
            os.makedirs(dataset_path, exist_ok=True)
            os.makedirs(overlay_path, exist_ok=True)

            capture_settings_changed = (
                camera_ip != self.d.cfg.camera.ip
                or dataset_path != os.path.normpath(self.d.cfg.paths.dataset_dir)
            )
            self.d.cfg.camera.ip = camera_ip
            self.d.cfg.paths.dataset_dir = dataset_path
            self.d.cfg.paths.overlay_dir = overlay_path
            self.d.cfg.roi.baseline_temp = float(self.baseline.get())
            self.d.cfg.roi.warning_delta = float(self.warning.get())
            self.d.cfg.roi.critical_delta = float(self.critical.get())
            save_config(self.d.cfg)
            self.d._add_operating_log("환경설정", "저장 경로 변경", dataset_path)
            # 화면 갱신 주기는 운영 화면 정책에 따라 30초로 고정한다.
            self.d._schedule_refresh(self.d.REFRESH_SECONDS * 1000)

            if capture_settings_changed and self.d.capture:
                self.d.capture.request_stop()
                self.d.capture = None
                self.d.monitoring = False
                self.d.root.after(300, self.d.start_monitoring)

            self.d._check_connection_async()
            self.win.destroy()
        except OSError as exc:
            messagebox.showerror("저장 경로 오류", f"폴더를 만들거나 사용할 수 없습니다.\n{exc}", parent=self.win)
        except ValueError:
            messagebox.showerror("입력 오류", "숫자 설정값을 확인하세요.", parent=self.win)


def main():
    root = tk.Tk(); app = ProductDashboard(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()

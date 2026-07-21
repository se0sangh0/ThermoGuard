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
from ..analysis.roi import RoiResult, extract_roi_from_npy, load_roi_config, extract_all_rois_from_npy, _get_roi_bounds_list
from ..analysis.threshold import MonitorState, Status, evaluate_with_state
from ..capture.capture import CaptureSession
from ..capture.thermal_utils import extract_from_jpeg
from ..config import load_config, save_config

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
    exception_count: int = 0
    anomaly_today: int = 0

    @staticmethod
    def rate(ok: int, total: int) -> float:
        return 100.0 if total == 0 else ok * 100.0 / total


class ProductDashboard:
    REFRESH_SECONDS = 20
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

        self._analysis_executor = ThreadPoolExecutor(max_workers=1)
        self._analysis_running = False
        self._analysis_pending = False
        self._analysis_generation = 0

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
        tk.Label(right, text=f"{self.REFRESH_SECONDS}초마다 자동 갱신", bg=COLORS["navy"],
                 fg="#c7d6e5", font=("맑은 고딕", 10)).pack(side="left", padx=12)
        ttk.Button(right, text="↻  새로고침", style="Action.TButton",
                   command=self.refresh_now).pack(side="left", padx=4)
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
        tk.Label(frame, text=caption, bg="white", fg=COLORS["muted"],
                 font=("맑은 고딕", 9)).pack(anchor="w")
        return frame, val

    def _build_kpis(self, parent):
        row = tk.Frame(parent, bg=COLORS["bg"]); row.pack(fill="x")
        cards = [
            ("현재 상태", "정상", "설비 상태가 안전합니다", COLORS["green"]),
            ("최고 온도", "-- °C", f"경고 기준 {self.cfg.roi.baseline_temp + self.cfg.roi.warning_delta:.1f}°C", COLORS["blue"]),
            ("금일 이상 감지", "0건", "미확인 0건", COLORS["orange"]),
            ("모니터링 정상률", "100.0%", "캡처 성공 기준", COLORS["blue"]),
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
            self._set_system_state("정상 운영 중", COLORS["green"])
            self._add_operating_log("연결", "성공", f"카메라 {self.cfg.camera.ip} 응답 확인")
            if not self.monitoring:
                self.start_monitoring()
        else:
            self._set_system_state("연결 지연", COLORS["orange"])
            self._add_operating_log("연결", "실패", f"카메라 {self.cfg.camera.ip} 응답 없음")
        self._update_metric_text()

    def start_monitoring(self):
        if self.monitoring or self.lifecycle != "running":
            return
        self.monitoring = True
        roi = self.cfg.roi
        baseline = roi.baseline_temp
        warning_delta = roi.warning_delta

        def _probe_callback(max_temp: float) -> bool:
            if max_temp >= baseline + warning_delta:
                self.capture.set_warning_mode(True)
                self._schedule_refresh(100)  # 즉시 분석 가속
                self._add_operating_log("프로브", "과열 감지", f"{max_temp:.1f}°C — 캡처 주기 1초로 전환")
                return True
            return False

        self.capture = CaptureSession(
            cam_ip=self.cfg.camera.ip, mode=self.cfg.tools.mode,
            interval=max(10.0, float(self.cfg.camera.capture_interval_sec)),
            save_dir=self.cfg.paths.dataset_dir, log_callback=self._capture_log,
            probe_callback=_probe_callback,
        )
        self.capture.start()

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
        if self.lifecycle != "running":
            return
        self.timer_id = None
        self._schedule_analysis()
        self._update_metric_text()
        # Warning/Critical 상태면 가속 분석, 아니면 기본 주기
        interval = self.REFRESH_FAST_SECONDS * 1000 if self.state.status != Status.NORMAL else self.REFRESH_SECONDS * 1000
        self._schedule_refresh(interval)

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
        roi_cfg = load_roi_config()
        roi_results = extract_all_rois_from_npy(str(npy), roi_cfg)
        roi_result = max(roi_results, key=lambda r: r.hot_temp_95 if r.hot_temp_95 is not None else -1)

        all_hotspots = []
        for rr in roi_results:
            all_hotspots.extend(rr.hotspot_centroids)
        unique_hotspots = []
        for spot in all_hotspots:
            is_dup = False
            for u in unique_hotspots:
                if abs(spot[0] - u[0]) < 5 and abs(spot[1] - u[1]) < 5:
                    if spot[2] > u[2]:
                        unique_hotspots[unique_hotspots.index(u)] = spot
                    is_dup = True
                    break
            if not is_dup:
                unique_hotspots.append(spot)
        roi_result.hotspot_centroids = unique_hotspots

        status, alarm = evaluate_with_state(
            roi_result.hot_temp_95, roi_result.max_temp, roi_result.mean_temp,
            roi_cfg.baseline_temp, roi_cfg.warning_delta,
            roi_cfg.critical_delta, self.state,
            roi_result.over_temp_pixels, roi_result.max_hotspot_size,
        )

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

        visual_img = None
        if visual and visual.exists():
            visual_img = cv2.imread(str(visual))

        return {
            "base": base, "overlay": overlay, "visual_img": visual_img,
            "max_temp": roi_result.max_temp, "mean_temp": roi_result.mean_temp,
            "hot_temp_95": roi_result.hot_temp_95,
            "hotspot_count": len(roi_result.hotspot_centroids),
            "status": status, "alarm": alarm,
            "roi_bounds": roi_result.roi_bounds,
            "roi_results": roi_results,
        }

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

        self._show_image(self.thermal_label, result["overlay"], "thermal")
        self._show_image(self.visual_label, result["visual_img"], "visual")
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
        self.kpi_values[3].configure(text=f"{self.metrics.rate(self.metrics.capture_successes, self.metrics.capture_attempts):.1f}%")
        vals = (f"{result['max_temp']:.1f} °C", f"{result['mean_temp']:.1f} °C",
                f"{result['hot_temp_95']:.1f} °C", f"{result['hotspot_count']}개")
        for lab, value in zip(self.temp_labels, vals):
            lab.configure(text=value)
        stamp = self.last_update.strftime("촬영 시각 %Y-%m-%d %H:%M:%S")
        self.visual_stamp.configure(text=stamp)
        self.thermal_stamp.configure(text=stamp)
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
        self.interval = tk.StringVar(value=str(max(10, int(self.d.REFRESH_SECONDS))))
        self.dataset_dir = tk.StringVar(value=self.d.cfg.paths.dataset_dir)
        self.baseline = tk.StringVar(value=str(self.d.cfg.roi.baseline_temp))
        self.warning = tk.StringVar(value=str(self.d.cfg.roi.warning_delta))
        self.critical = tk.StringVar(value=str(self.d.cfg.roi.critical_delta))
        self._field(general, "카메라 주소", self.ip, 0)
        self._field(general, "화면 갱신 주기(초)", self.interval, 1)
        self._path_field(general, "데이터 저장 폴더", self.dataset_dir, 2)
        ttk.Label(general, text="촬영 이미지, 온도 배열과 오버레이가 선택한 폴더에 저장됩니다.").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=12)
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
        visuals = sorted(dataset.glob("*_visual.jpg")) if dataset.exists() else []
        if not visuals:
            messagebox.showwarning("ROI 설정", "가시광 이미지가 없습니다. 먼저 이미지를 수집하세요.", parent=self.win); return
        self.d._add_operating_log("ROI 설정", "시작", str(visuals[-1]))
        self.win.grab_release()
        try:
            from .roi_selector import main as roi_main
            sys.argv = ["roi_selector", str(visuals[-1])]
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
            self.d.REFRESH_SECONDS = max(10, min(30, int(float(self.interval.get()))))
            self.d.cfg.roi.baseline_temp = float(self.baseline.get())
            self.d.cfg.roi.warning_delta = float(self.warning.get())
            self.d.cfg.roi.critical_delta = float(self.critical.get())
            save_config(self.d.cfg)
            self.d._add_operating_log("환경설정", "저장 경로 변경", dataset_path)

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

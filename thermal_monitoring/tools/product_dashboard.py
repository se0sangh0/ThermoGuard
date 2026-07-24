"""현장 관리자용 상품형 열화상 모니터링 대시보드."""

from __future__ import annotations

import os
import re
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
    "navy": "#101820", "blue": "#2475d0", "green": "#22b14c",
    "orange": "#f2a313", "red": "#ef3f3f", "bg": "#0d1115",
    "card": "#151b20", "line": "#35404a", "text": "#f0f4f7",
    "muted": "#a9b4bd", "dark": "#090d11", "panel": "#11171c",
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
    api_successes: int = 0
    api_timeouts: int = 0
    api_http_4xx: int = 0
    api_http_5xx: int = 0
    api_connection_errors: int = 0
    api_other_errors: int = 0

    @staticmethod
    def rate(ok: int, total: int) -> float:
        return 100.0 if total == 0 else ok * 100.0 / total

    @property
    def api_failures(self) -> int:
        return (
            self.api_timeouts + self.api_http_4xx + self.api_http_5xx
            + self.api_connection_errors + self.api_other_errors
        )


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
        self.events: list[dict] = []
        self.operating_logs: list[tuple[str, str, str, str]] = []
        self.operating_log_window: Optional[tk.Toplevel] = None
        self._operating_log_opening = False
        self.settings_dialog: Optional[SettingsDialog] = None
        self.temperature_history: list[tuple[datetime, float]] = []
        self._last_history_capture: Optional[datetime] = None
        self._last_alert_capture: Optional[datetime] = None
        self._trend_hover_points: list[tuple[float, float, datetime, float]] = []
        # 최근 화면 품질을 기준으로 정상률을 계산한다. 누적 전체 기준보다
        # 현재 발생한 영상 이상이 즉시 수치에 반영된다.
        self._image_quality_window: list[bool] = []
        self._connection_ok: Optional[bool] = None
        self._connection_check_running = False
        self._resume_after_connection_check = False
        self._last_quality_capture_id: Optional[str] = None
        self._latest_pair_quality_ok = False
        self._latest_pair_fresh = False
        self._last_successful_capture_at: Optional[datetime] = None

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
                        background=COLORS["card"], fieldbackground=COLORS["card"],
                        foreground=COLORS["text"])
        style.configure("Treeview.Heading", font=("맑은 고딕", 10, "bold"))
        style.configure("Action.TButton", font=("맑은 고딕", 10, "bold"), padding=8)
        style.configure("Side.TButton", font=("맑은 고딕", 11, "bold"), padding=11)

    def _build_ui(self):
        self._build_header()
        body = tk.Frame(self.root, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=10, pady=10)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=6, minsize=380)
        body.grid_rowconfigure(3, weight=4, minsize=250)

        self._build_toolbar(body)

        images = tk.Frame(body, bg=COLORS["bg"])
        images.grid(row=1, column=0, sticky="nsew", pady=(8, 6))
        images.grid_columnconfigure(0, weight=1, uniform="image_columns")
        images.grid_columnconfigure(1, weight=1, uniform="image_columns")
        images.grid_rowconfigure(0, weight=1)
        self._build_images(images)

        carousel = tk.Frame(body, bg=COLORS["bg"])
        carousel.grid(row=3, column=0, sticky="nsew")
        carousel.grid_columnconfigure(0, weight=1)
        carousel.grid_rowconfigure(0, weight=1)
        self.carousel_pages = []
        for _ in range(3):
            page = tk.Frame(carousel, bg=COLORS["bg"])
            page.grid(row=0, column=0, sticky="nsew")
            self.carousel_pages.append(page)
        self._build_alert_panel(self.carousel_pages[0])
        self._build_robot_map(self.carousel_pages[1])
        self._build_trend_panel(self.carousel_pages[2])
        self._build_carousel_navigation(body)
        self.carousel_index = 0
        self._show_carousel_page(0)

    def _build_header(self):
        header = tk.Frame(self.root, bg=COLORS["navy"], height=82,
                          highlightbackground=COLORS["line"], highlightthickness=1)
        header.pack(fill="x"); header.pack_propagate(False)
        left = tk.Frame(header, bg=COLORS["navy"]); left.pack(side="left", padx=24, pady=11)
        tk.Label(left, text="1공장 로봇 열화상 모니터링", bg=COLORS["navy"], fg="white",
                 font=("맑은 고딕", 20, "bold")).pack(anchor="w")
        tk.Label(left, text=f"1공장  ·  조립라인 A  ·  {self.cfg.identity.robot_id}",
                 bg=COLORS["navy"], fg="#c7d6e5", font=("맑은 고딕", 10)).pack(anchor="w")

        right = tk.Frame(header, bg=COLORS["navy"]); right.pack(side="right", padx=22, pady=16)
        self.header_state = tk.Label(right, text="● 확인 중", bg=COLORS["navy"], fg="#ffd166",
                                     font=("맑은 고딕", 11, "bold"))
        self.header_state.pack(anchor="e")
        self.header_time = tk.Label(right, text="마지막 갱신 —", bg=COLORS["navy"], fg="#c7d6e5",
                                    font=("맑은 고딕", 10))
        self.header_time.pack(side="left", padx=(0, 14), pady=(5, 0))
        self.header_refresh_interval = tk.Label(
            right, text=f"{self.REFRESH_SECONDS}초마다 자동 갱신",
            bg=COLORS["navy"], fg="#c7d6e5", font=("맑은 고딕", 10),
        )
        self.header_refresh_interval.pack(side="left", pady=(5, 0))
        self.header_stability = tk.Label(right, text="API 연결 안정성 —", bg=COLORS["navy"],
                                         fg=COLORS["muted"], font=("맑은 고딕", 10, "bold"))
        self.header_stability.pack(side="left", padx=(14, 0), pady=(5, 0))

    def _build_toolbar(self, parent):
        toolbar = tk.Frame(parent, bg=COLORS["panel"],
                           highlightbackground=COLORS["line"], highlightthickness=1)
        toolbar.grid(row=0, column=0, sticky="ew")
        tk.Label(toolbar, text="모니터링 제어", bg=COLORS["panel"], fg=COLORS["text"],
                 font=("맑은 고딕", 11, "bold")).pack(side="left", padx=14)
        controls = tk.Frame(toolbar, bg=COLORS["panel"])
        controls.pack(side="right", padx=8, pady=7)
        self.capture_toggle_button = ttk.Button(controls, text="■  촬영 정지", style="Action.TButton",
                                                command=self.toggle_capture)
        self.capture_toggle_button.pack(side="left", padx=3)
        self.refresh_button = ttk.Button(controls, text="↻  새로고침", style="Action.TButton",
                                         command=self.capture_and_refresh)
        self.refresh_button.pack(side="left", padx=3)
        ttk.Button(controls, text="▤  운영 로그", style="Action.TButton",
                   command=self.open_operating_log).pack(side="left", padx=3)
        ttk.Button(controls, text="⚙  환경설정", style="Action.TButton",
                   command=self.open_settings).pack(side="left", padx=3)

    def _build_alert_panel(self, parent):
        panel = tk.Frame(parent, bg=COLORS["panel"],
                         highlightbackground=COLORS["line"], highlightthickness=1)
        panel.pack(fill="both", expand=True)
        head = tk.Frame(panel, bg=COLORS["panel"]); head.pack(fill="x", padx=16, pady=(12, 8))
        tk.Label(head, text="미확인 알림", bg=COLORS["panel"], fg=COLORS["text"],
                 font=("맑은 고딕", 17, "bold")).pack(side="left")
        self.alert_count_label = tk.Label(head, text="0건", bg=COLORS["panel"], fg=COLORS["muted"],
                                          font=("맑은 고딕", 11, "bold"))
        self.alert_count_label.pack(side="right")

        alert_wrap = tk.Frame(panel, bg=COLORS["panel"])
        alert_wrap.pack(fill="both", expand=True, padx=10)
        self.alert_canvas = tk.Canvas(alert_wrap, bg=COLORS["panel"], highlightthickness=0)
        alert_scroll = ttk.Scrollbar(alert_wrap, orient="vertical", command=self.alert_canvas.yview)
        self.alert_cards = tk.Frame(self.alert_canvas, bg=COLORS["panel"])
        self._alert_window = self.alert_canvas.create_window((0, 0), window=self.alert_cards, anchor="nw")
        self.alert_canvas.configure(yscrollcommand=alert_scroll.set)
        self.alert_canvas.pack(side="left", fill="both", expand=True)
        alert_scroll.pack(side="right", fill="y")
        self.alert_cards.bind("<Configure>", lambda _e: self.alert_canvas.configure(
            scrollregion=self.alert_canvas.bbox("all")))
        self.alert_canvas.bind("<Configure>", lambda e: self.alert_canvas.itemconfigure(
            self._alert_window, width=e.width))

        self._render_alert_cards()

    def _build_carousel_navigation(self, parent):
        navigation = tk.Frame(parent, bg=COLORS["bg"])
        navigation.grid(row=2, column=0, sticky="ew", pady=(2, 6))
        self.info_tab_buttons = []
        tabs = ("미확인 알림  0건", "로봇 위치", "온도 추이")
        for index, title in enumerate(tabs):
            button = tk.Button(
                navigation, text=title,
                command=lambda selected=index: self._show_carousel_page(selected),
                bg=COLORS["panel"], fg=COLORS["muted"],
                activebackground=COLORS["blue"], activeforeground="white",
                relief="flat", bd=0, padx=22, pady=9,
                font=("맑은 고딕", 10, "bold"), cursor="hand2",
            )
            button.pack(side="left", padx=(0, 5))
            self.info_tab_buttons.append(button)

    def _show_carousel_page(self, index):
        self.carousel_index = index
        self.carousel_pages[index].tkraise()
        for button_index, button in enumerate(self.info_tab_buttons):
            selected = button_index == index
            button.configure(
                bg=COLORS["blue"] if selected else COLORS["panel"],
                fg="white" if selected else COLORS["muted"],
                relief="sunken" if selected else "flat",
            )
        if index == 1:
            self.root.after_idle(self._draw_robot_map)
        elif index == 2:
            self.root.after_idle(self._draw_temperature_trend)

    def _image_panel(self, parent, title):
        frame = tk.Frame(parent, bg=COLORS["card"], highlightbackground=COLORS["line"], highlightthickness=1)
        head = tk.Frame(frame, bg=COLORS["card"]); head.pack(fill="x", padx=12, pady=8)
        tk.Label(head, text=title, bg=COLORS["card"], fg=COLORS["text"],
                 font=("맑은 고딕", 12, "bold")).pack(side="left")
        stamp = tk.Label(head, text="촬영 시각 —", bg=COLORS["card"], fg=COLORS["muted"], font=("맑은 고딕", 9))
        stamp.pack(side="right")
        image = tk.Label(frame, text="첫 이미지 수신 대기 중", bg=COLORS["dark"], fg="#9aa9b6",
                         font=("맑은 고딕", 11))
        image.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        return frame, image, stamp

    def _build_images(self, parent):
        left, self.visual_label, self.visual_stamp = self._image_panel(parent, "가시광 이미지")
        right, self.thermal_label, self.thermal_stamp = self._image_panel(parent, "열화상 이미지")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 5))
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=(0, 5))

    def _build_robot_map(self, parent):
        panel = tk.Frame(parent, bg=COLORS["card"], highlightbackground=COLORS["line"], highlightthickness=1)
        panel.pack(fill="both", expand=True)
        head = tk.Frame(panel, bg=COLORS["card"]); head.pack(fill="x", padx=14, pady=10)
        tk.Label(head, text="공장 지도 및 로봇 위치", bg=COLORS["card"], fg=COLORS["text"],
                 font=("맑은 고딕", 12, "bold")).pack(side="left")
        tk.Label(head, text="임시 데이터", bg=COLORS["card"], fg=COLORS["orange"],
                 font=("맑은 고딕", 9, "bold")).pack(side="right")
        self.map_canvas = tk.Canvas(panel, bg="#e9edf0", highlightthickness=0, height=270)
        self.map_canvas.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.map_canvas.bind("<Configure>", lambda _e: self._draw_robot_map())

    def _build_trend_panel(self, parent):
        panel = tk.Frame(parent, bg=COLORS["card"], highlightbackground=COLORS["line"], highlightthickness=1)
        panel.pack(fill="both", expand=True)
        head = tk.Frame(panel, bg=COLORS["card"]); head.pack(fill="x", padx=14, pady=(10, 4))
        tk.Label(head, text="전체 ROI 최대 온도 추이", bg=COLORS["card"], fg=COLORS["text"],
                 font=("맑은 고딕", 12, "bold")).pack(side="left")
        self.trend_status_label = tk.Label(head, text="현재 상태: 확인 중", bg=COLORS["card"],
                                           fg=COLORS["orange"], font=("맑은 고딕", 10, "bold"))
        self.trend_status_label.pack(side="right")
        values = tk.Frame(panel, bg=COLORS["card"]); values.pack(fill="x", padx=14)
        self.trend_max_label = tk.Label(values, text="최대 온도 -- °C", bg=COLORS["card"],
                                        fg=COLORS["text"], font=("맑은 고딕", 16, "bold"))
        self.trend_max_label.pack(side="left")
        self.trend_delta_label = tk.Label(values, text="기준 대비 -- °C", bg=COLORS["card"],
                                          fg=COLORS["muted"], font=("맑은 고딕", 10))
        self.trend_delta_label.pack(side="left", padx=16)
        self.trend_roi_label = tk.Label(values, text="최대 온도 ROI —", bg=COLORS["card"],
                                        fg=COLORS["muted"], font=("맑은 고딕", 10))
        self.trend_roi_label.pack(side="right")
        self.trend_canvas = tk.Canvas(panel, bg=COLORS["dark"], highlightthickness=0, height=230)
        self.trend_canvas.pack(fill="both", expand=True, padx=12, pady=(8, 12))
        self.trend_canvas.bind("<Configure>", lambda _e: self._draw_temperature_trend())
        self.trend_canvas.bind("<Motion>", self._show_trend_hover)
        self.trend_canvas.bind("<Leave>", lambda _e: self._clear_trend_hover())

    def _render_alert_cards(self):
        for child in self.alert_cards.winfo_children():
            child.destroy()
        pending = [event for event in self.events if event["action"] == "확인 필요"]
        self.alert_count_label.configure(text=f"{len(pending)}건")
        if hasattr(self, "info_tab_buttons"):
            self.info_tab_buttons[0].configure(text=f"미확인 알림  {len(pending)}건")
        if not pending:
            tk.Label(self.alert_cards, text="미확인 알림이 없습니다.", bg=COLORS["panel"],
                     fg=COLORS["muted"], font=("맑은 고딕", 10)).pack(pady=28)
            return
        for event in pending:
            state = event["state"]
            color = COLORS["red"] if state in ("Critical", "위험") else COLORS["orange"]
            korean = "위험" if state in ("Critical", "위험") else "경고"
            card = tk.Frame(self.alert_cards, bg=COLORS["card"],
                            highlightbackground=color, highlightthickness=2)
            card.pack(fill="x", pady=5, padx=2)
            tk.Label(card, text=event["time"], bg=COLORS["card"], fg=COLORS["muted"],
                     font=("맑은 고딕", 9)).pack(anchor="w", padx=12, pady=(10, 2))
            tk.Label(card, text=f"{korean} · {event['asset']}", bg=COLORS["card"], fg=color,
                     font=("맑은 고딕", 12, "bold")).pack(anchor="w", padx=12)
            tk.Label(card, text=f"최고 온도 {event['temp']:.1f}°C", bg=COLORS["card"], fg=color,
                     font=("맑은 고딕", 13, "bold")).pack(anchor="w", padx=12, pady=(3, 8))
            tk.Button(card, text="확인", command=lambda event_id=event["id"]: self._acknowledge_event(event_id),
                      bg=color, fg="white", activebackground=color, activeforeground="white",
                      relief="flat", font=("맑은 고딕", 10, "bold"), cursor="hand2").pack(
                          fill="x", padx=12, pady=(0, 10))

    def _acknowledge_event(self, event_id: str):
        for event in self.events:
            if event["id"] == event_id:
                event["action"] = "확인 완료"
                event["acknowledged_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._add_operating_log("알림", "확인 완료", f"{event['asset']} · {event['temp']:.1f}°C")
                break
        self._render_alert_cards()

    def _draw_robot_map(self):
        canvas = self.map_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 420)
        height = max(canvas.winfo_height(), 240)
        margin = 18
        canvas.create_rectangle(margin, margin, width - margin, height - margin,
                                fill="#f7f8f9", outline="#99a4ad", width=2)
        left = margin + 80
        right = width - margin - 80
        middle = (left + right) // 2
        canvas.create_rectangle(left, margin, middle, height - margin, fill="#dceaf8", outline="#8796a3")
        canvas.create_rectangle(middle, margin, right, height - margin, fill="#e5f1e6", outline="#8796a3")
        canvas.create_text((left + middle)//2, margin + 24, text="라인 1", fill="#24333f",
                           font=("맑은 고딕", 11, "bold"))
        canvas.create_text((middle + right)//2, margin + 24, text="라인 2", fill="#24333f",
                           font=("맑은 고딕", 11, "bold"))
        canvas.create_text(margin + 38, height//2, text="입고\n구역", fill="#24333f",
                           font=("맑은 고딕", 9, "bold"), justify="center")
        canvas.create_text(width - margin - 38, height//2, text="검사\n구역", fill="#24333f",
                           font=("맑은 고딕", 9, "bold"), justify="center")
        marker_color = {Status.NORMAL: COLORS["green"], Status.WARNING: COLORS["orange"],
                        Status.CRITICAL: COLORS["red"]}.get(self.latest_status, COLORS["green"])
        rx, ry = (left + middle)//2, height//2 + 20
        canvas.create_oval(rx - 13, ry - 13, rx + 13, ry + 13, fill=marker_color, outline="white", width=2)
        canvas.create_rectangle(rx + 18, ry - 30, rx + 155, ry + 36, fill="white",
                                outline=marker_color, width=2)
        canvas.create_text(rx + 30, ry - 12, anchor="w", text=self.cfg.identity.robot_id,
                           fill="#1e2a33", font=("맑은 고딕", 11, "bold"))
        canvas.create_text(rx + 30, ry + 14, anchor="w", text="라인 1 · 조립 설비",
                           fill="#53636f", font=("맑은 고딕", 9))

    def _draw_temperature_trend(self):
        canvas = self.trend_canvas
        canvas.delete("all")
        self._trend_hover_points = []
        width = max(canvas.winfo_width(), 480)
        height = max(canvas.winfo_height(), 220)
        left, top, right, bottom = 54, 18, width - 18, height - 36
        baseline = self.cfg.roi.baseline_temp
        warning = baseline + self.cfg.roi.warning_delta
        critical = baseline + self.cfg.roi.critical_delta
        values = [value for _, value in self.temperature_history]
        y_min = min([baseline, *values], default=baseline) - 5
        y_max = max([critical, *values], default=critical) + 5
        if y_max <= y_min:
            y_max = y_min + 10

        def y_for(value):
            return bottom - (value - y_min) / (y_max - y_min) * (bottom - top)

        canvas.create_line(left, top, left, bottom, fill="#6f7b84")
        canvas.create_line(left, bottom, right, bottom, fill="#6f7b84")
        for value, label, color in ((warning, "경고 기준", COLORS["orange"]),
                                    (critical, "위험 기준", COLORS["red"])):
            y = y_for(value)
            canvas.create_line(left, y, right, y, fill=color, dash=(5, 3))
            canvas.create_text(right - 4, y - 8, anchor="e", text=f"{label} {value:.1f}°C",
                               fill=color, font=("맑은 고딕", 8, "bold"))
        canvas.create_text(8, top, anchor="nw", text=f"{y_max:.0f}", fill=COLORS["muted"], font=("맑은 고딕", 8))
        canvas.create_text(8, bottom - 10, anchor="nw", text=f"{y_min:.0f}", fill=COLORS["muted"], font=("맑은 고딕", 8))
        if len(self.temperature_history) < 2:
            if self.temperature_history:
                captured_at, value = self.temperature_history[0]
                canvas.create_text(left, bottom + 18, anchor="w",
                                   text=captured_at.strftime("%H:%M:%S"),
                                   fill=COLORS["muted"], font=("맑은 고딕", 8))
                canvas.create_oval(left - 3, y_for(value) - 3, left + 3, y_for(value) + 3,
                                   fill=COLORS["green"], outline="")
                self._trend_hover_points.append((left, y_for(value), captured_at, float(value)))
            canvas.create_text((left + right)//2, (top + bottom)//2,
                               text="촬영 데이터가 쌓이면 온도 추이가 표시됩니다.",
                               fill=COLORS["muted"], font=("맑은 고딕", 10))
            return
        points = []
        count = len(self.temperature_history)
        for index, (captured_at, value) in enumerate(self.temperature_history):
            x = left + index / max(1, count - 1) * (right - left)
            y = y_for(value)
            points.extend((x, y))
            self._trend_hover_points.append((x, y, captured_at, float(value)))
        canvas.create_line(*points, fill=COLORS["green"], width=3, smooth=True)
        for x, y, _, _ in self._trend_hover_points:
            canvas.create_oval(x - 3, y - 3, x + 3, y + 3,
                               fill=COLORS["green"], outline=COLORS["dark"])
        tick_count = min(5, count)
        tick_indexes = sorted({
            round(position * (count - 1) / max(1, tick_count - 1))
            for position in range(tick_count)
        })
        for tick_index in tick_indexes:
            captured_at, _ = self.temperature_history[tick_index]
            x = left + tick_index / max(1, count - 1) * (right - left)
            canvas.create_line(x, bottom, x, bottom + 5, fill="#6f7b84")
            anchor = "w" if tick_index == 0 else "e" if tick_index == count - 1 else "center"
            canvas.create_text(x, bottom + 18, anchor=anchor,
                               text=captured_at.strftime("%H:%M:%S"),
                               fill=COLORS["muted"], font=("맑은 고딕", 8))

    def _show_trend_hover(self, event):
        self._clear_trend_hover()
        if not self._trend_hover_points:
            return
        nearest = min(
            self._trend_hover_points,
            key=lambda point: (point[0] - event.x) ** 2 + (point[1] - event.y) ** 2,
        )
        x, y, captured_at, temperature = nearest
        if (x - event.x) ** 2 + (y - event.y) ** 2 > 14 ** 2:
            return
        text = f"{captured_at:%Y-%m-%d %H:%M:%S}\n최대 온도 {temperature:.1f} °C"
        place_left = x > self.trend_canvas.winfo_width() - 190
        place_below = y < 65
        text_x = x - 12 if place_left else x + 12
        text_y = y + 12 if place_below else y - 12
        anchor = (
            "ne" if place_left and place_below
            else "se" if place_left
            else "nw" if place_below
            else "sw"
        )
        text_id = self.trend_canvas.create_text(
            text_x, text_y, anchor=anchor, text=text,
            fill="white", font=("맑은 고딕", 9, "bold"),
            tags="trend_tooltip",
        )
        bbox = self.trend_canvas.bbox(text_id)
        if bbox:
            padding = 6
            background_id = self.trend_canvas.create_rectangle(
                bbox[0] - padding, bbox[1] - padding,
                bbox[2] + padding, bbox[3] + padding,
                fill="#202a32", outline=COLORS["green"],
                tags="trend_tooltip",
            )
            self.trend_canvas.tag_lower(background_id, text_id)

    def _clear_trend_hover(self):
        if hasattr(self, "trend_canvas"):
            self.trend_canvas.delete("trend_tooltip")

    def _set_system_state(self, text, color):
        self.header_state.configure(text=f"● {text}", fg=color)

    def _check_connection_async(self, resume_on_success=False):
        self._resume_after_connection_check |= bool(resume_on_success)
        if self._connection_check_running or self.lifecycle != "running":
            return
        self._connection_check_running = True
        self.metrics.connection_attempts += 1
        def work():
            result = {"ok": False, "status_code": None, "error_kind": None}
            try:
                response = requests.get(f"http://{self.cfg.camera.ip}/api/image/current?imgformat=JPEG", timeout=5)
                result["status_code"] = response.status_code
                result["ok"] = response.status_code == 200
                if not result["ok"]:
                    result["error_kind"] = "http"
            except requests.exceptions.Timeout:
                result["error_kind"] = "timeout"
                self.metrics.exception_count += 1
            except requests.exceptions.ConnectionError:
                result["error_kind"] = "connection"
                self.metrics.exception_count += 1
            except Exception:
                result["error_kind"] = "other"
                self.metrics.exception_count += 1
            if result["ok"]:
                self.metrics.connection_successes += 1
            if self.lifecycle == "running":
                self.root.after(0, lambda: self._connection_result(result))
        threading.Thread(target=work, daemon=True).start()

    def _connection_result(self, result):
        self._connection_check_running = False
        resume_on_success = self._resume_after_connection_check
        self._resume_after_connection_check = False
        ok = bool(result["ok"])
        self._connection_ok = ok
        self._record_api_result(
            ok,
            status_code=result.get("status_code"),
            error_kind=result.get("error_kind"),
        )
        if ok:
            self._add_operating_log("연결", "성공", f"카메라 {self.cfg.camera.ip} 응답 확인")
            if resume_on_success:
                self.capture_paused_by_user = False
            if self.capture_paused_by_user:
                self._set_system_state("촬영 정지", COLORS["orange"])
            else:
                self._set_system_state("정상 운영 중", COLORS["green"])
            if not self.monitoring and not self.capture_paused_by_user:
                self.start_monitoring()
        else:
            self._set_system_state("연결 없음", COLORS["red"])
            detail = (
                f"HTTP {result['status_code']}" if result.get("status_code")
                else result.get("error_kind") or "응답 없음"
            )
            self._add_operating_log("연결", "실패", f"카메라 {self.cfg.camera.ip} · {detail}")
            if resume_on_success:
                self.capture_toggle_button.configure(text="▶  촬영 시작", state="normal")
        self._update_connection_stability_display()
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
            self._set_system_state("연결 확인 중", COLORS["orange"])
            self.capture_toggle_button.configure(text="연결 확인 중...", state="disabled")
            self._add_operating_log("촬영", "연결 확인", "촬영 시작 전 카메라 응답 확인")
            self._check_connection_async(resume_on_success=True)

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
            self._record_api_result(True)
        elif any(word in message.lower() for word in ("error", "timeout", "http", "connection")):
            self.metrics.capture_attempts += 1; self.metrics.exception_count += 1
            self._add_operating_log("캡처", "예외 처리", message)
            self._record_api_message(message)
            self.root.after(0, self._check_connection_async)
        self.root.after(0, self._update_connection_stability_display)
        self._update_metric_text_async()

    def _record_api_result(self, success, status_code=None, error_kind=None):
        if success:
            self.metrics.api_successes += 1
        elif error_kind == "timeout":
            self.metrics.api_timeouts += 1
        elif error_kind == "connection":
            self.metrics.api_connection_errors += 1
        elif status_code is not None and 400 <= int(status_code) < 500:
            self.metrics.api_http_4xx += 1
        elif status_code is not None and 500 <= int(status_code) < 600:
            self.metrics.api_http_5xx += 1
        else:
            self.metrics.api_other_errors += 1

    def _record_api_message(self, message):
        lower = message.lower()
        match = re.search(r"http\s+(\d{3})", lower)
        status_code = int(match.group(1)) if match else None
        error_kind = (
            "timeout" if "timeout" in lower
            else "connection" if "connection" in lower
            else "http" if status_code is not None
            else "other"
        )
        self._record_api_result(False, status_code=status_code, error_kind=error_kind)

    def _update_connection_stability_display(self):
        failures = self.metrics.api_failures
        total = self.metrics.api_successes + failures
        if total == 0:
            self.header_stability.configure(text="API 연결 안정성 —", fg=COLORS["muted"])
            return
        rate = 100.0 * self.metrics.api_successes / total
        color = (
            COLORS["green"] if rate >= 99.0
            else COLORS["orange"] if rate >= 90.0
            else COLORS["red"]
        )
        self.header_stability.configure(
            text=(
                f"API 연결 안정성 {rate:.1f}% · Timeout {self.metrics.api_timeouts} · "
                f"4xx {self.metrics.api_http_4xx} · 5xx {self.metrics.api_http_5xx}"
            ),
            fg=color,
        )

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
        overall_max_roi = max(roi_results, key=lambda rr: rr.max_temp)
        overall_max_temp = float(overall_max_roi.max_temp)
        warning_temp = roi_cfg.baseline_temp + roi_cfg.warning_delta
        critical_temp = roi_cfg.baseline_temp + roi_cfg.critical_delta
        if overall_max_temp >= critical_temp:
            status = Status.CRITICAL
        elif overall_max_temp >= warning_temp:
            status = Status.WARNING
        else:
            status = Status.NORMAL

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
            "overall_max_temp": overall_max_temp,
            "overall_max_roi_name": overall_max_roi.roi_name or "ROI-01",
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
        previous = self.latest_status
        captured_at = result.get("captured_at") or datetime.now()
        quality_ok = bool(result.get("image_quality_ok", False))
        capture_id = str(result.get("base", ""))
        freshness_limit = max(
            self.REFRESH_SECONDS * 2,
            float(self.cfg.camera.capture_interval_sec) * 2,
        ) + 5.0
        capture_age = max(0.0, (datetime.now() - captured_at).total_seconds())
        self._latest_pair_fresh = capture_age <= freshness_limit
        is_new_capture = bool(capture_id) and capture_id != self._last_quality_capture_id
        if is_new_capture:
            self._last_quality_capture_id = capture_id
            self._latest_pair_quality_ok = quality_ok
            if quality_ok:
                self._last_successful_capture_at = captured_at
        self.state.status = status
        if (
            quality_ok
            and status != Status.NORMAL
            and captured_at != self._last_alert_capture
        ):
            self.metrics.anomaly_today += 1
            self._append_event(
                status.value,
                result.get("overall_max_temp", result["max_temp"]),
                "확인 필요",
                result.get("overall_max_roi_name", "ROI-01"),
                event_time=captured_at,
            )
            self._last_alert_capture = captured_at
        elif status == Status.NORMAL and previous != Status.NORMAL:
            if self.capture:
                self.capture.set_warning_mode(False)
            self._add_operating_log("과열 해제", "정상 복귀",
                                    f"캡처 주기 {self.capture._normal_interval:.0f}초로 복원" if self.capture
                                    else "정상 복귀")

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

        # 같은 디스크 파일을 30초마다 재분석해도 품질 표본은 한 번만 집계한다.
        # 오래된 파일은 현재 정상률의 근거로 사용하지 않는다.
        if is_new_capture and self._latest_pair_fresh:
            self.metrics.image_quality_checks += 1
            if quality_ok:
                self.metrics.image_quality_successes += 1
            else:
                self._add_operating_log(
                    "영상 품질", "비정상", result.get("image_quality_reason", "영상 확인 필요")
                )
            self._image_quality_window.append(quality_ok)
            del self._image_quality_window[:-20]
        elif is_new_capture:
            self._add_operating_log(
                "영상 품질", "갱신 없음",
                f"{capture_id} · 촬영 후 {capture_age:.0f}초 경과",
            )
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
        self._update_connection_stability_display()
        overall_max = result.get("overall_max_temp", result["max_temp"])
        overall_roi = result.get("overall_max_roi_name", "ROI-01")
        delta = overall_max - self.cfg.roi.baseline_temp
        self.trend_status_label.configure(text=f"현재 상태: {korean}", fg=color)
        self.trend_max_label.configure(text=f"최대 온도 {overall_max:.1f} °C", fg=color)
        self.trend_delta_label.configure(text=f"정상 기준 대비 {delta:+.1f} °C")
        self.trend_roi_label.configure(text=f"최대 온도 ROI {overall_roi}")
        captured_at = result.get("captured_at") or self.last_update
        if captured_at != self._last_history_capture:
            self.temperature_history.append((captured_at, float(overall_max)))
            del self.temperature_history[:-120]
            self._last_history_capture = captured_at
        self._draw_temperature_trend()
        self._draw_robot_map()
        if result.get("image_quality_ok", False):
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
        pil = Image.fromarray(rgb); pil.thumbnail((720, 430), RESAMPLE_LANCZOS)
        photo = ImageTk.PhotoImage(pil)
        label.configure(image=photo, text="")
        if kind == "thermal": self.thermal_photo = photo
        else: self.visual_photo = photo

    def _append_event(self, state, temp, action, roi_name="ROI-01", event_time=None):
        now = event_time or datetime.now()
        event = {
            "id": now.strftime("%Y%m%d%H%M%S_%f"),
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "asset": f"{self.cfg.identity.robot_id} · {roi_name}",
            "state": state,
            "temp": float(temp) if temp is not None else 0.0,
            "action": action,
            "acknowledged_at": None,
        }
        self.events.insert(0, event)
        self._render_alert_cards()

    def acknowledge_selected(self):
        messagebox.showinfo("확인 처리", "왼쪽 미확인 알림 카드의 확인 버튼을 사용하세요.", parent=self.root)

    def show_all_events(self):
        messagebox.showinfo("이상 이력", "현재 시제품은 실행 중 감지 이력을 표시합니다.\nDB 연동 시 전체 기간 조회가 제공됩니다.", parent=self.root)

    def _update_metric_text_async(self):
        if self.lifecycle == "running": self.root.after(0, self._update_metric_text)

    def _update_metric_text(self):
        if not hasattr(self, "metric_label"):
            return
        m = self.metrics
        self.metric_label.configure(text=(
            f"카메라 연결 {m.rate(m.connection_successes,m.connection_attempts):.1f}%   ·   "
            f"캡처 성공 {m.rate(m.capture_successes,m.capture_attempts):.1f}%   ·   "
            f"분석 정상 완료 {m.analysis_ok}회   ·   예외 처리 {m.exception_count}회"))

    def open_operating_log(self):
        if self._operating_log_opening:
            return

        if self.operating_log_window:
            try:
                if self.operating_log_window.winfo_exists():
                    self.operating_log_window.deiconify()
                    self.operating_log_window.lift()
                    self.operating_log_window.focus_force()
                    return
            except tk.TclError:
                pass
            self.operating_log_window = None

        # 참조가 유실되더라도 같은 이름의 Tk 창이 남아 있으면 새로 만들지 않는다.
        try:
            existing = self.root.nametowidget(".operating_log")
            if existing.winfo_exists():
                self.operating_log_window = existing
                existing.deiconify()
                existing.lift()
                existing.focus_force()
                return
        except (KeyError, tk.TclError):
            pass

        self._operating_log_opening = True
        try:
            win = tk.Toplevel(self.root, name="operating_log")
            win.title("운영 로그"); win.geometry("920x520"); win.transient(self.root)
            self.operating_log_window = win
        finally:
            self._operating_log_opening = False

        def close_log_window():
            self.operating_log_window = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close_log_window)
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
        if self.settings_dialog:
            try:
                if self.settings_dialog.win.winfo_exists():
                    self.settings_dialog.win.deiconify()
                    self.settings_dialog.win.lift()
                    self.settings_dialog.win.focus_force()
                    return
            except tk.TclError:
                pass
            self.settings_dialog = None
        self.settings_dialog = SettingsDialog(self)

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
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self._roi_editor_running = False
        self._calibration_running = False
        self._tool_running: Optional[str] = None
        self._tool_window_titles: tuple[str, ...] = ()
        self._tool_guard_window: Optional[tk.Toplevel] = None
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
        self.roi_button = ttk.Button(roi, text="가시광 이미지에서 ROI 설정", command=self.open_roi_editor)
        self.roi_button.pack(anchor="w", pady=8)
        ttk.Separator(roi, orient="horizontal").pack(fill="x", pady=14)
        ttk.Label(roi, text="Thermal-RGB 위치 보정", font=("맑은 고딕", 11, "bold")).pack(anchor="w", pady=(0,4))
        ttk.Label(roi, text="카메라 설치 위치가 바뀐 경우 두 영상의 대응점을 다시 지정합니다.").pack(anchor="w", pady=(0,8))
        self.calibration_button = ttk.Button(roi, text="캘리브레이션 실행", command=self.open_calibration)
        self.calibration_button.pack(anchor="w", pady=4)
        self._field(advanced, "정상 기준 온도(°C)", self.baseline, 0)
        self._field(advanced, "경고 상승폭(°C)", self.warning, 1)
        self._field(advanced, "위험 상승폭(°C)", self.critical, 2)
        buttons = ttk.Frame(self.win); buttons.pack(fill="x", padx=14, pady=(0,14))
        ttk.Button(buttons, text="취소", command=self.close).pack(side="right", padx=4)
        ttk.Button(buttons, text="저장", style="Action.TButton", command=self.save).pack(side="right", padx=4)

    def close(self):
        if self._tool_running:
            return
        self.d.settings_dialog = None
        if self.win.winfo_exists():
            self.win.destroy()

    def _focus_running_tool(self):
        """이미 실행 중인 OpenCV 창을 새로 만들지 않고 앞으로 가져온다."""
        for title in self._tool_window_titles:
            try:
                if cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) >= 1:
                    cv2.setWindowProperty(title, cv2.WND_PROP_TOPMOST, 1)
            except cv2.error:
                continue

    def _show_tool_guard(self):
        """설정창 클릭이 대기열에 쌓이지 않도록 모달 안내창이 입력을 선점한다."""
        guard = tk.Toplevel(self.win)
        guard.title("작업 진행 중")
        guard.transient(self.win)
        guard.resizable(False, False)
        guard.geometry("320x130")
        guard.protocol("WM_DELETE_WINDOW", lambda: None)

        body = ttk.Frame(guard, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="실행 중인 작업창이 있습니다.",
            font=("맑은 고딕", 11, "bold"),
        ).pack(pady=(0, 16))
        ttk.Button(
            body,
            text="확인",
            command=self._focus_running_tool,
        ).pack()

        guard.update_idletasks()
        x = self.win.winfo_rootx() + (self.win.winfo_width() - guard.winfo_width()) // 2
        y = self.win.winfo_rooty() + (self.win.winfo_height() - guard.winfo_height()) // 2
        guard.geometry(f"+{max(0, x)}+{max(0, y)}")
        guard.grab_set()
        guard.lift()
        guard.focus_force()
        self._tool_guard_window = guard

    def _pump_tool_events(self):
        """OpenCV 루프 중 모달 안내창 이벤트만 처리해 클릭 적체를 비운다."""
        if self._tool_guard_window and self._tool_guard_window.winfo_exists():
            self._tool_guard_window.update()

    def _tool_display_bounds(self):
        """설정창이 위치한 모니터의 작업 영역을 OpenCV 도구에 전달한다."""
        self.win.update_idletasks()
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                class MONITORINFO(ctypes.Structure):
                    _fields_ = [
                        ("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD),
                    ]

                user32 = ctypes.windll.user32
                user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
                user32.MonitorFromWindow.restype = ctypes.c_void_p
                user32.GetMonitorInfoW.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(MONITORINFO),
                ]
                user32.GetMonitorInfoW.restype = wintypes.BOOL
                monitor = user32.MonitorFromWindow(
                    self.win.winfo_id(), 2,
                )
                info = MONITORINFO()
                info.cbSize = ctypes.sizeof(info)
                if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    work = info.rcWork
                    return (
                        work.left,
                        work.top,
                        work.right - work.left,
                        work.bottom - work.top,
                    )
            except (AttributeError, OSError, tk.TclError):
                pass
        return (
            self.win.winfo_vrootx(),
            self.win.winfo_vrooty(),
            self.win.winfo_vrootwidth(),
            self.win.winfo_vrootheight(),
        )

    def _begin_tool(self, tool_name: str, window_titles: tuple[str, ...]) -> bool:
        """OpenCV 도구는 프로세스에서 한 번에 하나만 실행한다."""
        if self._tool_running:
            self._focus_running_tool()
            self.d._add_operating_log(
                tool_name, "기존 창 표시", f"{self._tool_running} 창을 앞으로 가져옴"
            )
            return False
        self._tool_running = tool_name
        self._tool_window_titles = window_titles
        self._roi_editor_running = tool_name == "ROI 설정"
        self._calibration_running = tool_name == "캘리브레이션"
        self.win.grab_release()
        self._show_tool_guard()
        return True

    def _end_tool(self):
        if self._tool_guard_window:
            try:
                if self._tool_guard_window.winfo_exists():
                    self._tool_guard_window.grab_release()
                    self._tool_guard_window.destroy()
            except tk.TclError:
                pass
            self._tool_guard_window = None
        self._tool_running = None
        self._tool_window_titles = ()
        self._roi_editor_running = False
        self._calibration_running = False
        if self.win.winfo_exists():
            self.win.grab_set()

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
        if self._tool_running:
            self._focus_running_tool()
            return
        dataset = Path(self.d.cfg.paths.dataset_dir)
        if not dataset.exists():
            messagebox.showwarning("ROI 설정", "데이터셋 폴더가 없습니다.", parent=self.win); return
        jpgs = sorted(dataset.glob("*.jpg"))
        thermal_files = [p for p in jpgs if "_visual" not in p.name]
        if not thermal_files:
            messagebox.showwarning("ROI 설정", "이미지가 없습니다. 먼저 이미지를 수집하세요.", parent=self.win); return
        thermal = thermal_files[-1]
        visual = dataset / f"{thermal.stem}_visual.jpg"
        if not self._begin_tool(
            "ROI 설정",
            ("ROI Selector - Visual (H)", "ROI Selector - Thermal"),
        ):
            return
        self.d._add_operating_log("ROI 설정", "시작", str(visual if visual.exists() else thermal))
        try:
            from .roi_selector import main as roi_main
            if visual.exists():
                sys.argv = ["roi_selector", str(thermal), str(visual)]
            else:
                sys.argv = ["roi_selector", str(thermal)]
            roi_main(
                event_pump=self._pump_tool_events,
                display_bounds=self._tool_display_bounds(),
            )
            self.d.cfg = load_config(force_reload=True)
            self.d._add_operating_log("ROI 설정", "완료", f"{len(self.d.cfg.roi.rois)}개 영역 저장됨")
        except Exception as exc:
            self.d._add_operating_log("ROI 설정", "예외 처리", str(exc))
            messagebox.showerror("ROI 설정", str(exc), parent=self.win)
        finally:
            self._end_tool()

    def open_calibration(self):
        if self._tool_running:
            self._focus_running_tool()
            return
        dataset = Path(self.d.cfg.paths.dataset_dir)
        thermal_files = sorted(p for p in dataset.glob("*.jpg") if "_visual" not in p.name) if dataset.exists() else []
        if not thermal_files:
            messagebox.showwarning("캘리브레이션", "Thermal 이미지가 없습니다. 먼저 이미지를 수집하세요.", parent=self.win); return
        thermal = thermal_files[-1]; visual = dataset / f"{thermal.stem}_visual.jpg"
        if not visual.exists():
            messagebox.showwarning("캘리브레이션", "대응하는 가시광 이미지가 없습니다.", parent=self.win); return
        if not self._begin_tool("캘리브레이션", ("Thermal", "RGB")):
            return
        self.d._add_operating_log("캘리브레이션", "시작", thermal.name)
        saved = False
        try:
            from .calibration import run_calibration
            saved = bool(run_calibration(
                str(thermal),
                str(visual),
                event_pump=self._pump_tool_events,
                display_bounds=self._tool_display_bounds(),
            ))
            if saved:
                self.d._add_operating_log("캘리브레이션", "완료", self.d.cfg.paths.homography_path)
            else:
                self.d._add_operating_log("캘리브레이션", "종료", "저장 없이 종료")
        except Exception as exc:
            self.d.metrics.exception_count += 1
            self.d._add_operating_log("캘리브레이션", "예외 처리", str(exc))
            messagebox.showerror("캘리브레이션", str(exc), parent=self.win)
            saved = False
        finally:
            self._end_tool()
        if saved and self.win.winfo_exists() and messagebox.askyesno(
            "ROI 설정",
            "캘리브레이션이 완료되었습니다.\n\n"
            "가시광 이미지에서 ROI 영역을 설정하시겠습니까?\n"
            "(가시광에서 지정한 ROI는 열화상 좌표로 자동 변환됩니다.)",
            parent=self.win,
        ):
            self.win.after_idle(self.open_roi_editor)

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
            self.close()
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

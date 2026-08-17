"""현장 관리자용 상품형 열화상 모니터링 대시보드."""

from __future__ import annotations

import math
import os
import queue
import re
import threading
import time
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import cv2
import requests
import tkinter as tk
from PIL import Image, ImageTk
from tkinter import filedialog, messagebox, ttk

from ..analysis.overlay import create_overlay, create_visual_roi_overlay
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
from ..capture.capture import CaptureSession, camera_image_url
from ..data import pairs
from ..data.metadata import run_metadata
from ..data.pairs import capture_time_from_file, latest_analysis_pair
from ..data.quality import assess_image_quality
from ..config import (
    PROJECT_ROOT,
    ConfigValidationError,
    bounded_backend_timeout,
    factory_mode_enabled,
    load_config,
    resolve_runtime_path,
    save_collection_config,
    validate_config,
)
from ..logger import get_logger
from ..runtime_lock import dashboard_runtime_scope
from .telegram_dispatcher import TelegramDispatcher

_file_log = get_logger("tools.dashboard")

# Legacy PySpin support is deliberately lazy and is not imported by the
# supported HTTP REST + ExifTool dashboard path.  Keeping the symbol allows a
# separately qualified legacy caller to opt in without making PySpin a factory
# startup dependency.
GigeTemperatureReader = None

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
    TREND_HISTORY_DAYS = 7
    TREND_API_LIMIT = 150000
    TREND_DRAW_POINTS = 1000
    GIGE_READY_TIMEOUT_SEC = 10.0
    HISTORY_PERIODS = {
        "1시간": 1,
        "1일": 24,
        "3일": 72,
        "7일": 168,
    }

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("로봇 열화상 모니터링")
        self.root.geometry("1440x900")
        self.root.minsize(1180, 760)
        self.root.configure(bg=COLORS["bg"])

        # This installation is used for supervised data collection.  Preserve
        # the historical permissive loader so the current site config can open
        # the GUI even when it does not satisfy factory commissioning policy.
        self.cfg = load_config(force_reload=True)
        self.lifecycle = "running"  # running -> closing -> closed
        self.monitoring = False
        self.capture_paused_by_user = False
        self._commissioning_block_announced = False
        self.capture: Optional[CaptureSession] = None
        self._stopping_capture: Optional[CaptureSession] = None
        self._restart_after_capture_stop = False
        self.timer_id: Optional[str] = None
        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._ui_dispatch_timer: Optional[str] = None
        self.processed: set[str] = set()
        self.state = MonitorState(
            alarm_cooldown=float(self.cfg.monitoring.alarm_cooldown_sec)
        )
        self.metrics = RuntimeMetrics()
        self.latest_result: Optional[RoiResult] = None
        self.latest_status = Status.NORMAL          # 표시용(raw) 이전 상태
        self.latest_alarm_status = Status.NORMAL    # 팝업용(클러스터 판정) 이전 상태
        self.critical_popup: Optional[tk.Toplevel] = None
        self.last_update: Optional[datetime] = None
        self.visual_photo = None
        self.thermal_photo = None
        self._visual_source = None
        self._thermal_source = None
        self._image_render_ids = {"visual": None, "thermal": None}
        self.events: list[dict] = []
        self.alert_window: Optional[tk.Toplevel] = None
        self.alert_tree: Optional[ttk.Treeview] = None
        self.alert_filter_var: Optional[tk.StringVar] = None
        self.alert_period_var: Optional[tk.StringVar] = None
        self.alert_history_hours = 168
        self.alert_range_label: Optional[ttk.Label] = None
        self.operating_logs: list[tuple[str, str, str, str]] = []
        self.operating_log_window: Optional[tk.Toplevel] = None
        self.operating_log_summary_label: Optional[ttk.Label] = None
        self.operating_log_tree: Optional[ttk.Treeview] = None
        self._operating_log_opening = False
        self.settings_dialog: Optional[SettingsDialog] = None
        self.temperature_history: list[tuple[datetime, float]] = []
        self.trend_period_var = tk.StringVar(value="7일")
        self.trend_history_hours = 168
        self._last_history_capture: Optional[datetime] = None
        self._last_alert_capture: Optional[datetime] = None
        self.telegram = TelegramDispatcher(self)
        self._trend_hover_points: list[tuple[float, float, datetime, float]] = []
        # 최근 화면 품질을 기준으로 정상률을 계산한다. 누적 전체 기준보다
        # 현재 발생한 영상 이상이 즉시 수치에 반영된다.
        self._image_quality_window: list[bool] = []
        self._connection_ok: Optional[bool] = None
        self._connection_check_running = False
        self._resume_after_connection_check = False
        self._connection_retry_timer: Optional[str] = None
        self._connection_retry_attempt = 0
        self._last_quality_capture_id: Optional[str] = None
        self._latest_pair_quality_ok = False
        self._latest_pair_fresh = False
        self._last_successful_capture_at: Optional[datetime] = None
        self._capture_started_at: Optional[datetime] = None
        self._capture_stale_announced = False

        # GigE 5초 프로브
        self._gige_reader: Optional[GigeTemperatureReader] = None
        # A reader that timed out during shutdown retains its SDK ownership
        # until its own worker has left GetNextImage and completed cleanup.
        self._stopping_gige_reader: Optional[GigeTemperatureReader] = None
        self._gige_probe_timer: Optional[str] = None
        self._gige_stop_wait_timer: Optional[str] = None
        self._gige_ready_timer: Optional[str] = None
        self._gige_ready_deadline: Optional[float] = None
        self._gige_failure_announced = False

        self._analysis_executor = ThreadPoolExecutor(max_workers=1)
        # Slow storage scans and backend calls must never queue ahead of live
        # image analysis and critical alarm evaluation.
        self._maintenance_executor = ThreadPoolExecutor(max_workers=1)
        self._analysis_running = False
        self._analysis_pending = False
        self._analysis_generation = 0
        self._manual_capture_running = False

        self._last_integrity_check = 0.0
        self._last_metadata_update = 0.0
        self._last_cleanup_check = 0.0
        self._last_backend_sync = 0.0

        self._configure_style()
        self._build_ui()
        self._ui_dispatch_timer = self.root.after(50, self._drain_ui_queue)
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
        style.configure(
            "Active.Action.TButton",
            font=("맑은 고딕", 10, "bold"),
            padding=8,
            background=COLORS["blue"],
            foreground="white",
        )
        style.map(
            "Active.Action.TButton",
            background=[("active", COLORS["blue"]), ("pressed", COLORS["blue"])],
            foreground=[("active", "white"), ("pressed", "white")],
        )
        style.configure(
            "Active.TButton",
            background=COLORS["blue"],
            foreground="white",
        )
        style.map(
            "Active.TButton",
            background=[("active", COLORS["blue"]), ("pressed", COLORS["blue"])],
            foreground=[("active", "white"), ("pressed", "white")],
        )
        style.configure("Side.TButton", font=("맑은 고딕", 11, "bold"), padding=11)

    # ── Tk thread handoff ─────────────────────────────────────

    def _post_to_ui(self, callback: Callable[[], None]) -> bool:
        """Queue worker results for execution only by the Tk main thread.

        Calling ``root.after`` directly from worker threads races Tk teardown.
        A queue lets shutdown discard pending callbacks without a worker ever
        touching the destroyed interpreter.
        """
        if getattr(self, "lifecycle", None) != "running":
            return False
        ui_queue = getattr(self, "_ui_queue", None)
        if ui_queue is None:
            return False
        ui_queue.put(callback)
        return True

    def _drain_ui_queue(self) -> None:
        self._ui_dispatch_timer = None
        if self.lifecycle != "running":
            return
        for _ in range(100):
            try:
                callback = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception:
                _file_log.exception("dashboard UI callback failed")
        if self.lifecycle == "running":
            try:
                self._ui_dispatch_timer = self.root.after(50, self._drain_ui_queue)
            except tk.TclError:
                self.lifecycle = "closed"

    def _discard_ui_callbacks(self) -> None:
        while True:
            try:
                self._ui_queue.get_nowait()
            except queue.Empty:
                return

    def _build_ui(self):
        self._build_header()
        body = tk.Frame(self.root, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=10, pady=10)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=3, minsize=150)
        body.grid_rowconfigure(3, weight=7, minsize=190)
        self.dashboard_body = body

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
        self.carousel_container = carousel
        self.carousel_pages = []
        page = tk.Frame(carousel, bg=COLORS["bg"])
        page.grid(row=0, column=0, sticky="nsew")
        self.carousel_pages.append(page)
        self._build_trend_panel(page)
        self._build_carousel_navigation(body)
        self.carousel_expanded = False
        self._set_carousel_expanded(False)
        self._update_carousel_navigation()

    def _build_header(self):
        header = tk.Frame(self.root, bg=COLORS["navy"], height=82,
                          highlightbackground=COLORS["line"], highlightthickness=1)
        header.pack(fill="x"); header.pack_propagate(False)
        left = tk.Frame(header, bg=COLORS["navy"]); left.pack(side="left", padx=24, pady=11)
        tk.Label(left, text="로봇 열화상 모니터링", bg=COLORS["navy"], fg="white",
                 font=("맑은 고딕", 20, "bold")).pack(anchor="w")

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
        self.capture_toggle_button = ttk.Button(controls, text="▶  촬영 시작", style="Action.TButton",
                                                command=self.toggle_capture)
        self.capture_toggle_button.pack(side="left", padx=3)
        self.refresh_button = ttk.Button(controls, text="↻  새로고침", style="Action.TButton",
                                         command=self.capture_and_refresh)
        self.refresh_button.pack(side="left", padx=3)
        self.operating_log_button = ttk.Button(
            controls, text="▤  운영 로그", style="Action.TButton",
            command=self.open_operating_log,
        )
        self.operating_log_button.pack(side="left", padx=3)
        self.settings_button = ttk.Button(
            controls, text="⚙  환경설정", style="Action.TButton",
            command=self.open_settings,
        )
        self.settings_button.pack(side="left", padx=3)

    def _build_carousel_navigation(self, parent):
        navigation = tk.Frame(parent, bg=COLORS["bg"])
        navigation.grid(row=2, column=0, sticky="ew", pady=(2, 6))
        self.alert_history_button = tk.Button(
            navigation,
            text="미확인 알림  0건",
            command=self.open_alert_history,
            bg=COLORS["panel"], fg=COLORS["muted"],
            activebackground=COLORS["blue"], activeforeground="white",
            relief="flat", bd=0, padx=22, pady=9,
            font=("맑은 고딕", 10, "bold"), cursor="hand2",
        )
        self.alert_history_button.pack(side="left", padx=(0, 5))
        self.trend_toggle_button = tk.Button(
            navigation,
            text="온도 추이",
            command=self._toggle_temperature_trend,
            bg=COLORS["panel"], fg=COLORS["muted"],
            activebackground=COLORS["blue"], activeforeground="white",
            relief="flat", bd=0, padx=22, pady=9,
            font=("맑은 고딕", 10, "bold"), cursor="hand2",
        )
        self.trend_toggle_button.pack(side="left", padx=(0, 5))

    def _toggle_temperature_trend(self):
        self._set_carousel_expanded(not self.carousel_expanded)
        self._update_carousel_navigation()
        if self.carousel_expanded:
            if self.cfg.backend.enabled:
                self._maintenance_executor.submit(self._sync_temperature_history)
            self.root.after_idle(self._draw_temperature_trend)

    def _on_trend_period_changed(self, _event=None):
        label = self.trend_period_var.get()
        self.trend_history_hours = self.HISTORY_PERIODS.get(label, 168)
        self.trend_title_label.configure(text="최근 전체온도추이")
        self._draw_temperature_trend()
        if self.cfg.backend.enabled:
            self._maintenance_executor.submit(self._sync_temperature_history)

    def _set_carousel_expanded(self, expanded):
        self.carousel_expanded = expanded
        if expanded:
            self.carousel_container.grid()
            self.dashboard_body.grid_rowconfigure(1, weight=3, minsize=150)
            self.dashboard_body.grid_rowconfigure(3, weight=7, minsize=190)
        else:
            self.carousel_container.grid_remove()
            self.dashboard_body.grid_rowconfigure(1, weight=1, minsize=420)
            self.dashboard_body.grid_rowconfigure(3, weight=0, minsize=0)
        self.root.after_idle(self._redraw_dashboard_content)

    def _update_carousel_navigation(self):
        self.trend_toggle_button.configure(
            bg=COLORS["blue"] if self.carousel_expanded else COLORS["panel"],
            fg="white" if self.carousel_expanded else COLORS["muted"],
            relief="sunken" if self.carousel_expanded else "flat",
        )

    def _redraw_dashboard_content(self):
        self._schedule_dashboard_image_render("visual")
        self._schedule_dashboard_image_render("thermal")
        if not self.carousel_expanded:
            return
        self.root.after_idle(self._draw_status_gauge)
        self.root.after_idle(self._draw_temperature_trend)

    def _image_panel(self, parent, title):
        frame = tk.Frame(parent, bg=COLORS["card"], highlightbackground=COLORS["line"], highlightthickness=1)
        head = tk.Frame(frame, bg=COLORS["card"]); head.pack(fill="x", padx=12, pady=8)
        tk.Label(head, text=title, bg=COLORS["card"], fg=COLORS["text"],
                 font=("맑은 고딕", 12, "bold")).pack(side="left")
        stamp = tk.Label(head, text="촬영 시각 —", bg=COLORS["card"], fg=COLORS["muted"], font=("맑은 고딕", 9))
        stamp.pack(side="right")
        image = tk.Canvas(
            frame,
            bg=COLORS["dark"],
            highlightthickness=0,
            width=1,
            height=1,
        )
        image.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        return frame, image, stamp

    def _build_images(self, parent):
        left, self.visual_label, self.visual_stamp = self._image_panel(parent, "가시광 이미지")
        right, self.thermal_label, self.thermal_stamp = self._image_panel(parent, "열화상 이미지")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 5))
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=(0, 5))
        self.visual_label.bind(
            "<Configure>",
            lambda _event: self._schedule_dashboard_image_render("visual"),
        )
        self.thermal_label.bind(
            "<Configure>",
            lambda _event: self._schedule_dashboard_image_render("thermal"),
        )
        self.root.after_idle(lambda: self._schedule_dashboard_image_render("visual"))
        self.root.after_idle(lambda: self._schedule_dashboard_image_render("thermal"))

    def _build_trend_panel(self, parent):
        panel = tk.Frame(parent, bg=COLORS["card"], highlightbackground=COLORS["line"], highlightthickness=1)
        panel.pack(fill="both", expand=True)
        head = tk.Frame(panel, bg=COLORS["card"]); head.pack(fill="x", padx=14, pady=(10, 4))
        self.trend_title_label = tk.Label(
            head,
            text="최근 전체온도추이",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("맑은 고딕", 12, "bold"),
        )
        self.trend_title_label.pack(side="left")
        trend_period_box = ttk.Combobox(
            head,
            textvariable=self.trend_period_var,
            values=tuple(self.HISTORY_PERIODS),
            state="readonly",
            width=8,
        )
        trend_period_box.pack(side="left", padx=(14, 0))
        trend_period_box.bind("<<ComboboxSelected>>", self._on_trend_period_changed)
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
        charts = tk.Frame(panel, bg=COLORS["card"])
        charts.pack(fill="both", expand=True, padx=12, pady=(8, 12))
        self.status_gauge_canvas = tk.Canvas(
            charts,
            bg=COLORS["dark"],
            highlightthickness=0,
            width=300,
            height=230,
        )
        self.status_gauge_canvas.pack(side="left", fill="y", padx=(0, 8))
        self.status_gauge_canvas.bind(
            "<Configure>",
            lambda _event: self._draw_status_gauge(),
        )
        self.trend_canvas = tk.Canvas(charts, bg=COLORS["dark"], highlightthickness=0, height=230)
        self.trend_canvas.pack(side="left", fill="both", expand=True)
        self.trend_canvas.bind("<Configure>", lambda _e: self._draw_temperature_trend())
        self.trend_canvas.bind("<Motion>", self._show_trend_hover)
        self.trend_canvas.bind("<Leave>", lambda _e: self._clear_trend_hover())

    def _render_alert_cards(self):
        """Update the alert button and the optional seven-day history popup."""
        cutoff = datetime.now() - timedelta(hours=self.alert_history_hours)
        recent = []
        for event in self.events:
            try:
                occurred_at = datetime.fromisoformat(str(event.get("time", "")))
            except ValueError:
                occurred_at = datetime.now()
            if occurred_at >= cutoff:
                recent.append(event)

        pending = [event for event in recent if event["action"] == "확인 필요"]
        if hasattr(self, "alert_history_button"):
            self.alert_history_button.configure(text=f"미확인 알림  {len(pending)}건")

        tree = self.alert_tree
        if tree is None:
            return

        for item in tree.get_children():
            tree.delete(item)
        selected_filter = (
            self.alert_filter_var.get()
            if self.alert_filter_var is not None
            else "미확인"
        )
        if selected_filter == "미확인":
            visible = pending
        elif selected_filter == "확인 완료":
            visible = [event for event in recent if event["action"] == "확인 완료"]
        else:
            visible = recent

        for event in visible:
            state = event.get("state", "")
            korean = "위험" if state in ("Critical", "위험") else "경고"
            processing = event.get("ack_pending") or event.get("backend_pending")
            action = "처리 중" if processing else event.get("action", "")
            tree.insert(
                "",
                "end",
                iid=str(event["id"]),
                values=(
                    event.get("time", ""),
                    event.get("asset", "ROI"),
                    f"{float(event.get('temp', 0)):.1f}°C",
                    korean,
                    action,
                ),
                tags=("critical" if korean == "위험" else "warning",),
            )

    def open_alert_history(self):
        """Open a non-modal popup with a selectable one-hour to seven-day range."""
        if self.alert_window:
            try:
                if self.alert_window.winfo_exists():
                    self.alert_window.deiconify()
                    self.alert_window.lift()
                    self.alert_window.focus_force()
                    self._refresh_alert_history()
                    return
            except tk.TclError:
                pass
            self.alert_window = None
            self.alert_tree = None

        win = tk.Toplevel(self.root, name="alert_history")
        win.title("알림 이력")
        win.geometry("900x560")
        win.minsize(760, 420)
        win.transient(self.root)
        self.alert_window = win
        self.alert_history_button.configure(
            bg=COLORS["blue"], fg="white", relief="sunken",
        )

        def close_window():
            self.alert_window = None
            self.alert_tree = None
            self.alert_filter_var = None
            self.alert_period_var = None
            self.alert_range_label = None
            self.alert_history_button.configure(
                bg=COLORS["panel"], fg=COLORS["muted"], relief="flat",
            )
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close_window)

        toolbar = ttk.Frame(win, padding=(12, 12, 12, 6))
        toolbar.pack(fill="x")
        ttk.Label(
            toolbar,
            text="알림 이력",
            font=("맑은 고딕", 14, "bold"),
        ).pack(side="left")
        ttk.Label(toolbar, text="기간").pack(side="left", padx=(24, 6))
        current_period = next(
            (
                label for label, hours in self.HISTORY_PERIODS.items()
                if hours == self.alert_history_hours
            ),
            "7일",
        )
        self.alert_period_var = tk.StringVar(value=current_period)
        period_box = ttk.Combobox(
            toolbar,
            textvariable=self.alert_period_var,
            values=tuple(self.HISTORY_PERIODS),
            state="readonly",
            width=8,
        )
        period_box.pack(side="left")
        period_box.bind("<<ComboboxSelected>>", self._on_alert_period_changed)
        ttk.Label(toolbar, text="상태").pack(side="left", padx=(24, 6))
        self.alert_filter_var = tk.StringVar(value="미확인")
        filter_box = ttk.Combobox(
            toolbar,
            textvariable=self.alert_filter_var,
            values=("미확인", "확인 완료", "전체"),
            state="readonly",
            width=12,
        )
        filter_box.pack(side="left")
        filter_box.bind("<<ComboboxSelected>>", lambda _event: self._render_alert_cards())
        ttk.Button(
            toolbar,
            text="새로고침",
            command=self._refresh_alert_history,
        ).pack(side="right")

        table_frame = ttk.Frame(win, padding=(12, 6))
        table_frame.pack(fill="both", expand=True)
        columns = ("time", "roi", "temp", "severity", "action")
        tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        for key, label, width in (
            ("time", "발생 시각", 180),
            ("roi", "ROI", 150),
            ("temp", "최대 온도", 110),
            ("severity", "상태", 90),
            ("action", "확인 상태", 120),
        ):
            tree.heading(key, text=label)
            tree.column(key, width=width, anchor="center")
        tree.tag_configure("critical", foreground=COLORS["red"])
        tree.tag_configure("warning", foreground=COLORS["orange"])
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.alert_tree = tree

        footer = ttk.Frame(win, padding=(12, 6, 12, 12))
        footer.pack(fill="x")
        self.alert_range_label = ttk.Label(
            footer,
            text=f"조회 범위: 현재 시각 기준 최근 {current_period}",
            foreground=COLORS["muted"],
        )
        self.alert_range_label.pack(side="left")
        ttk.Button(
            footer,
            text="선택 알림 확인 처리",
            style="Action.TButton",
            command=self._acknowledge_selected_alert,
        ).pack(side="right")

        self._render_alert_cards()
        self._refresh_alert_history()

    def _on_alert_period_changed(self, _event=None):
        if self.alert_period_var is None:
            return
        label = self.alert_period_var.get()
        self.alert_history_hours = self.HISTORY_PERIODS.get(label, 168)
        if self.alert_range_label is not None:
            self.alert_range_label.configure(
                text=f"조회 범위: 현재 시각 기준 최근 {label}"
            )
        self._render_alert_cards()
        self._refresh_alert_history()

    def _refresh_alert_history(self):
        if self.cfg.backend.enabled:
            self._maintenance_executor.submit(self._sync_events_from_backend)
        else:
            self._render_alert_cards()

    def _acknowledge_selected_alert(self):
        tree = self.alert_tree
        if tree is None or not tree.selection():
            messagebox.showinfo(
                "알림 확인",
                "확인 처리할 알림을 선택하세요.",
                parent=self.alert_window or self.root,
            )
            return
        self._acknowledge_event(tree.selection()[0])

    def _acknowledge_event(self, event_id: str):
        event = next((e for e in self.events if e["id"] == event_id), None)
        if event is None:
            return
        if event.get("ack_pending") or event.get("backend_pending"):
            return
        if event.get("backend_id") and self.cfg.backend.enabled:
            event["ack_pending"] = True
            self._add_operating_log(
                "알림",
                "처리 중",
                f"{event['asset']} · {event['temp']:.1f}°C",
            )
            self._render_alert_cards()
            self._acknowledge_event_backend(event)
            return
        self._mark_event_acknowledged(event)
        self._add_operating_log("알림", "성공", f"{event['asset']} · {event['temp']:.1f}°C")
        self._render_alert_cards()

    @staticmethod
    def _mark_event_acknowledged(event: dict, acknowledged_at=None):
        event["action"] = "확인 완료"
        event["ack_pending"] = False
        event["acknowledged_at"] = (
            acknowledged_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

    def _draw_temperature_trend(self):
        canvas = self.trend_canvas
        canvas.delete("all")
        self._trend_hover_points = []
        cutoff = datetime.now() - timedelta(hours=self.trend_history_hours)
        selected_history = [
            point for point in self.temperature_history
            if point[0] >= cutoff
        ]
        display_history = self._downsample_temperature_history(
            selected_history,
            self.TREND_DRAW_POINTS,
        )
        width = max(canvas.winfo_width(), 480)
        height = max(canvas.winfo_height(), 220)
        left, top, right, bottom = 54, 18, width - 18, height - 36
        baseline = self.cfg.roi.baseline_temp
        warning = baseline + self.cfg.roi.warning_delta
        critical = baseline + self.cfg.roi.critical_delta
        values = [value for _, value in display_history]
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
        if len(display_history) < 2:
            if display_history:
                captured_at, value = display_history[0]
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
        count = len(display_history)
        for index, (captured_at, value) in enumerate(display_history):
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
        span = display_history[-1][0] - display_history[0][0]
        tick_format = "%m-%d %H:%M" if span >= timedelta(days=1) else "%H:%M:%S"
        for tick_index in tick_indexes:
            captured_at, _ = display_history[tick_index]
            x = left + tick_index / max(1, count - 1) * (right - left)
            canvas.create_line(x, bottom, x, bottom + 5, fill="#6f7b84")
            anchor = "w" if tick_index == 0 else "e" if tick_index == count - 1 else "center"
            canvas.create_text(x, bottom + 18, anchor=anchor,
                               text=captured_at.strftime(tick_format),
                               fill=COLORS["muted"], font=("맑은 고딕", 8))

    @staticmethod
    def _downsample_temperature_history(history, max_points):
        """Keep graph drawing light while preserving the hottest point per bucket."""
        if len(history) <= max_points:
            return list(history)
        bucket_size = math.ceil(len(history) / max_points)
        reduced = []
        for start in range(0, len(history), bucket_size):
            bucket = history[start:start + bucket_size]
            reduced.append(max(bucket, key=lambda point: point[1]))
        reduced.sort(key=lambda point: point[0])
        return reduced

    def _draw_status_gauge(self):
        """Draw the latest NORMAL/WARNING/CRITICAL state as a semicircle gauge."""
        canvas = self.status_gauge_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 260)
        height = max(canvas.winfo_height(), 210)
        center_x = width / 2
        center_y = min(height - 48, 155)
        radius = min(width * 0.39, center_y - 28)
        bbox = (
            center_x - radius,
            center_y - radius,
            center_x + radius,
            center_y + radius,
        )

        canvas.create_text(
            center_x,
            16,
            text="현재 온도 상태",
            fill=COLORS["text"],
            font=("맑은 고딕", 11, "bold"),
        )
        # Tk의 0도는 오른쪽이므로 위험→경고→정상 순서로 반원을 그린다.
        for start, extent, color in (
            (4, 54, COLORS["red"]),
            (63, 54, COLORS["orange"]),
            (122, 54, COLORS["green"]),
        ):
            canvas.create_arc(
                *bbox,
                start=start,
                extent=extent,
                style="arc",
                outline=color,
                width=22,
            )

        state_angles = {
            Status.NORMAL: 149,
            Status.WARNING: 90,
            Status.CRITICAL: 31,
        }
        state_labels = {
            Status.NORMAL: ("정상", COLORS["green"]),
            Status.WARNING: ("경고", COLORS["orange"]),
            Status.CRITICAL: ("위험", COLORS["red"]),
        }
        angle = math.radians(state_angles.get(self.latest_status, 149))
        needle_length = radius * 0.68
        needle_x = center_x + math.cos(angle) * needle_length
        needle_y = center_y - math.sin(angle) * needle_length
        canvas.create_line(
            center_x,
            center_y,
            needle_x,
            needle_y,
            fill="white",
            width=6,
            capstyle="round",
        )
        canvas.create_oval(
            center_x - 10,
            center_y - 10,
            center_x + 10,
            center_y + 10,
            fill="white",
            outline=COLORS["muted"],
            width=2,
        )

        label, state_color = state_labels.get(
            self.latest_status,
            ("확인 중", COLORS["muted"]),
        )
        latest_temp = (
            self.temperature_history[-1][1]
            if self.temperature_history
            else None
        )
        temperature_text = (
            f"{latest_temp:.1f} °C"
            if latest_temp is not None
            else "측정 대기"
        )
        label_y = center_y + 26
        canvas.create_text(
            center_x,
            label_y,
            text=f"{label}  {temperature_text}",
            fill=state_color,
            font=("맑은 고딕", 14, "bold"),
        )
        canvas.create_text(
            center_x - radius * 0.76,
            center_y + 3,
            text="정상",
            fill=COLORS["green"],
            font=("맑은 고딕", 9, "bold"),
        )
        canvas.create_text(
            center_x,
            center_y - radius - 15,
            text="경고",
            fill=COLORS["orange"],
            font=("맑은 고딕", 9, "bold"),
        )
        canvas.create_text(
            center_x + radius * 0.76,
            center_y + 3,
            text="위험",
            fill=COLORS["red"],
            font=("맑은 고딕", 9, "bold"),
        )

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
        capture = getattr(self, "capture", None)
        if capture is not None and capture.running:
            # CaptureSession owns every camera request while active.  A second
            # dashboard GET here amplifies a camera-busy failure into a storm.
            return
        self._resume_after_connection_check |= bool(resume_on_success)
        if self._connection_check_running or self.lifecycle != "running":
            return
        self._connection_check_running = True
        self.metrics.connection_attempts += 1
        def work():
            result = {"ok": False, "status_code": None, "error_kind": None}
            try:
                response = requests.get(camera_image_url(self.cfg.camera.ip), timeout=5)
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
            self._post_to_ui(lambda: self._connection_result(result))
        threading.Thread(target=work, daemon=True).start()

    def _schedule_connection_retry(self) -> None:
        """Retry an unavailable camera with bounded backoff unless user-paused."""
        if (
            self.lifecycle != "running"
            or self.capture_paused_by_user
            or self._connection_retry_timer is not None
        ):
            return
        self._connection_retry_attempt = min(self._connection_retry_attempt + 1, 6)
        delay_ms = min(60_000, 1_000 * (2 ** self._connection_retry_attempt))
        self._connection_retry_timer = self.root.after(
            delay_ms,
            self._run_connection_retry,
        )

    def _run_connection_retry(self) -> None:
        self._connection_retry_timer = None
        if self.lifecycle == "running" and not self.capture_paused_by_user:
            self._check_connection_async()

    def _cancel_connection_retry(self) -> None:
        if self._connection_retry_timer:
            try:
                self.root.after_cancel(self._connection_retry_timer)
            except tk.TclError:
                pass
            self._connection_retry_timer = None

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
            self._cancel_connection_retry()
            self._connection_retry_attempt = 0
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
            self.capture_toggle_button.configure(text="▶  촬영 시작", state="normal")
            self._schedule_connection_retry()
        self._update_connection_stability_display()
        self._update_metric_text()

    def start_monitoring(self):
        if self.monitoring or self.lifecycle != "running":
            return
        if self._stopping_capture is not None:
            self._restart_after_capture_stop = True
            self.capture_toggle_button.configure(
                text="이전 촬영 종료 대기...",
                state="disabled",
            )
            return
        self.capture_paused_by_user = False
        self._restart_after_capture_stop = False
        self._cancel_connection_retry()

        self._start_capture_session()

    def _start_capture_session(self) -> None:
        """Start HTTP capture only after every required safety input is ready."""

        if self.monitoring or self.lifecycle != "running":
            return
        self.monitoring = True
        self.capture_toggle_button.configure(text="■  촬영 정지", state="normal")

        probe_state = {"elevated": False}

        def _probe_callback(max_temp: float) -> bool:
            threshold = (
                float(self.cfg.roi.baseline_temp)
                + float(self.cfg.roi.warning_delta)
            )
            elevated = max_temp >= threshold
            if elevated and not probe_state["elevated"]:
                self._post_to_ui(
                    lambda temp=max_temp, limit=threshold: (
                        self._add_operating_log(
                            "HTTP 프로브",
                            "경고",
                            f"{temp:.1f}°C (threshold {limit:.1f}°C) - 즉시 전체 촬영",
                        ),
                        self._schedule_refresh(100),
                    )
                )
            probe_state["elevated"] = elevated
            return elevated

        def _status_callback(state: str, detail: str) -> None:
            self._post_to_ui(
                lambda current=state, message=detail: self._handle_capture_status(
                    current,
                    message,
                )
            )

        self.capture = CaptureSession(
            cam_ip=self.cfg.camera.ip, mode=self.cfg.tools.mode,
            interval=float(self.cfg.camera.capture_interval_sec),
            probe_interval=float(self.cfg.camera.warning_interval_sec),
            save_dir=self.cfg.paths.dataset_dir, log_callback=self._capture_log,
            probe_callback=_probe_callback,
            status_callback=_status_callback,
            cfg=self.cfg,
        )
        self.capture.start()
        self._capture_started_at = datetime.now()
        self._capture_stale_announced = False

    def toggle_capture(self):
        """현장 사용자가 촬영만 정지하거나 다시 시작할 수 있게 한다."""
        if self.monitoring:
            self.stop_monitoring()
        else:
            self._set_system_state("연결 확인 중", COLORS["orange"])
            self.capture_toggle_button.configure(text="연결 확인 중...", state="disabled")
            self._add_operating_log("캡처", "시작", "촬영 시작 전 카메라 응답 확인")
            self._check_connection_async(resume_on_success=True)

    def stop_monitoring(self):
        if (
            not self.monitoring
            and self._stopping_capture is None
            and self._gige_reader is None
            and self._stopping_gige_reader is None
            and self._gige_ready_timer is None
        ):
            return
        self.capture_paused_by_user = True
        self._cancel_connection_retry()
        self.monitoring = False
        self._stop_gige_probe()
        capture = self.capture
        self.capture = None
        if capture:
            capture.request_stop()
            self._stopping_capture = capture
            self._wait_for_capture_stop(capture, restart=False)
        self.capture_toggle_button.configure(text="▶  촬영 시작")
        self._set_system_state("촬영 정지", COLORS["orange"])
        self._add_operating_log("캡처", "성공", "사용자가 촬영을 정지함")

    def _wait_for_capture_stop(self, capture: CaptureSession, *, restart: bool) -> None:
        """Poll a stop request without blocking Tk, then optionally restart.

        Camera HTTP requests can take up to ten seconds.  Starting a replacement
        before the old thread leaves would create concurrent camera traffic.
        """
        if self.lifecycle != "running":
            return
        if capture.wait_stopped(timeout=0):
            if self._stopping_capture is capture:
                self._stopping_capture = None
            should_restart = restart or self._restart_after_capture_stop
            self._restart_after_capture_stop = False
            if should_restart and not self.capture_paused_by_user:
                self.capture_toggle_button.configure(text="▶  촬영 시작", state="normal")
                self.start_monitoring()
            elif self.lifecycle == "running":
                self.capture_toggle_button.configure(text="▶  촬영 시작", state="normal")
            return
        try:
            self.root.after(100, lambda: self._wait_for_capture_stop(capture, restart=restart))
        except tk.TclError:
            pass

    def _capture_log(self, message: str):
        self._post_to_ui(lambda: self._handle_capture_log(message))

    def _handle_capture_log(self, message: str):
        """Handle capture-thread messages from the Tk main thread only."""
        if self.lifecycle != "running":
            return
        if "saved" in message:
            self.metrics.capture_attempts += 1; self.metrics.capture_successes += 1
            self._last_successful_capture_at = datetime.now()
            self._capture_stale_announced = False
            self._add_operating_log("캡처", "성공", message)
            self._record_api_result(True)
        elif any(word in message.lower() for word in ("error", "timeout", "http", "connection")):
            self.metrics.capture_attempts += 1; self.metrics.exception_count += 1
            self._add_operating_log("캡처", "실패", message)
            self._record_api_message(message)
        self._update_connection_stability_display()
        self._update_metric_text()

    def _handle_capture_status(self, state: str, detail: str) -> None:
        """Reflect CaptureSession state without issuing another camera GET."""
        if self.lifecycle != "running":
            return
        if state == "disconnected":
            self._connection_ok = False
            self._set_system_state("연결 없음", COLORS["red"])
            self._add_operating_log("카메라", "실패", detail)
        elif state in {"degraded", "backoff"}:
            self._set_system_state("카메라 응답 대기", COLORS["orange"])
            self._add_operating_log("카메라", "경고", detail)
        elif state == "recovered":
            self._connection_ok = True
            self._set_system_state("정상 운영 중", COLORS["green"])
            self._add_operating_log("카메라", "복구", detail)

    # ── GigE 5초 프로브 ─────────────────────────────────────

    def _start_gige_probe(self) -> bool:
        """Legacy opt-in PySpin reader; not called by the dashboard runtime."""
        if not self.cfg.camera.gige_enabled:
            return True
        if self._gige_reader is not None:
            return True
        if self._stopping_gige_reader is not None:
            # Never create another PySpin session until the previous worker
            # has released its SDK resources.
            self._wait_for_gige_stop(self._stopping_gige_reader)
            return False
        try:
            reader_factory = GigeTemperatureReader
            if reader_factory is None:
                from ..capture.gige_backend import GigeTemperatureReader as reader_factory
            reader = reader_factory(device_index=self.cfg.camera.gige_device_index)
            if not reader.start():
                return False
        except Exception as exc:
            _file_log.error("required GigE reader initialization failed (%s)", type(exc).__name__)
            return False
        self._gige_reader = reader
        self._add_operating_log("GigE", "시작", "온도 프레임 수신 대기")
        return True

    def _wait_for_required_gige_ready(self) -> None:
        """Poll GigE readiness without blocking Tk or starting HTTP capture."""

        self._gige_ready_timer = None
        if self.lifecycle != "running":
            return
        reader = self._gige_reader
        if reader is None or reader.stopped:
            self._fail_closed_gige("GigE 온도 프로브가 준비 전에 종료되었습니다.")
            return
        temperature = reader.read_temperature()
        if reader.connected and temperature is not None:
            self._gige_ready_deadline = None
            self._gige_failure_announced = False
            self._add_operating_log(
                "GigE", "성공", f"필수 온도 프로브 준비 완료 ({temperature:.1f}°C)"
            )
            self._start_capture_session()
            return
        deadline = self._gige_ready_deadline or time.monotonic()
        if time.monotonic() >= deadline:
            self._fail_closed_gige(
                f"{self.GIGE_READY_TIMEOUT_SEC:.0f}초 안에 GigE 온도 프레임을 받지 못했습니다."
            )
            return
        try:
            self._gige_ready_timer = self.root.after(
                100,
                self._wait_for_required_gige_ready,
            )
        except tk.TclError:
            pass

    def _fail_closed_gige(self, reason: str) -> None:
        """Stop all collection when the configured factory GigE input is lost."""

        self._gige_ready_deadline = None
        if getattr(self, "_gige_ready_timer", None):
            try:
                self.root.after_cancel(self._gige_ready_timer)
            except tk.TclError:
                pass
            self._gige_ready_timer = None
        self.capture_paused_by_user = True
        self.monitoring = False
        self._restart_after_capture_stop = False
        self._cancel_connection_retry()
        self._stop_gige_probe()
        capture, self.capture = self.capture, None
        if capture is not None:
            capture.request_stop()
            self._stopping_capture = capture
            self._wait_for_capture_stop(capture, restart=False)
        self.capture_toggle_button.configure(text="GigE 점검 필요", state="normal")
        self._set_system_state("GigE 필수 프로브 장애 · 수집 정지", COLORS["red"])
        self._add_operating_log("GigE", "차단", reason)
        if not self._gige_failure_announced:
            self._gige_failure_announced = True
            messagebox.showerror(
                "GigE 필수 프로브 장애",
                f"{reason}\n\nHTTP 촬영을 포함한 모든 수집을 안전 정지했습니다.",
                parent=self.root,
            )

    def _start_gige_timer(self):
        """5초 GigE 프로브 타이머를 시작한다. 이미 실행 중이면 무시."""
        if self._gige_probe_timer is not None:
            return
        self._schedule_gige_probe()

    def _stop_gige_timer(self):
        """5초 GigE 프로브 타이머만 중지한다. 리더는 유지."""
        if self._gige_probe_timer:
            self.root.after_cancel(self._gige_probe_timer)
            self._gige_probe_timer = None

    def _schedule_gige_probe(self):
        """5000ms 후 다음 GigE 프로브를 예약한다."""
        if self.lifecycle != "running":
            return
        self._gige_probe_timer = self.root.after(5000, self._run_gige_probe)

    def _run_gige_probe(self):
        """5초마다 GigE 온도를 확인하고 임계 초과 시 경고 모드로 전환한다."""
        if not self.monitoring or self._gige_reader is None:
            self._gige_probe_timer = None
            return
        reader = self._gige_reader
        if factory_mode_enabled() and (
            reader.stopped or not reader.connected
        ):
            self._gige_probe_timer = None
            self._fail_closed_gige("운영 중 GigE 온도 프레임이 중단되었습니다.")
            return
        temp = reader.read_temperature()
        if factory_mode_enabled() and temp is None:
            self._gige_probe_timer = None
            self._fail_closed_gige("운영 중 GigE 온도값을 읽을 수 없습니다.")
            return
        if temp is not None:
            threshold = self.cfg.roi.baseline_temp + self.cfg.roi.warning_delta
            capture = self.capture
            if temp >= threshold:
                if capture:
                    capture.set_warning_mode(True)
                self._schedule_refresh(100)
                self._add_operating_log(
                    "GigE", "성공", f"{temp:.1f}°C (threshold {threshold:.1f}°C) - 분석 가속"
                )
            else:
                if capture:
                    capture.set_warning_mode(False)
        if self.monitoring:
            self._schedule_gige_probe()

    def _stop_gige_probe(self):
        """Request GigE shutdown without releasing SDK objects from Tk's thread."""
        if getattr(self, "_gige_ready_timer", None):
            try:
                self.root.after_cancel(self._gige_ready_timer)
            except tk.TclError:
                pass
            self._gige_ready_timer = None
        self._gige_ready_deadline = None
        self._stop_gige_timer()
        reader, self._gige_reader = self._gige_reader, None
        if reader is not None:
            reader.request_stop()
            self._stopping_gige_reader = reader
            self._wait_for_gige_stop(reader)

    def _wait_for_gige_stop(self, reader: GigeTemperatureReader) -> None:
        """Poll a GigE stop without freezing the dashboard UI.

        ``GigeTemperatureReader`` performs PySpin cleanup in its acquisition
        worker's finally block.  Holding the reader here prevents an immediate
        replacement reader from racing that teardown after a timeout.
        """
        self._gige_stop_wait_timer = None
        if reader.wait_stopped(timeout=0):
            if self._stopping_gige_reader is reader:
                self._stopping_gige_reader = None
            if self.lifecycle == "running":
                self._add_operating_log("GigE", "성공", "온도 프로브 정지")
                if self.monitoring and self._gige_reader is None:
                    self._start_gige_probe()
            return
        if self.lifecycle != "running":
            return
        try:
            self._gige_stop_wait_timer = self.root.after(
                100,
                lambda: self._wait_for_gige_stop(reader),
            )
        except tk.TclError:
            pass

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
        self._run_maintenance()
        self._schedule_analysis()
        self._update_capture_freshness()
        self._update_metric_text()
        # 환경설정에서 지정한 화면 갱신 주기를 상태와 관계없이 적용한다.
        self._schedule_refresh(self.REFRESH_SECONDS * 1000)

    def _update_capture_freshness(self) -> None:
        """Do not display an old Normal frame as live production data."""
        if not self.monitoring or self.capture_paused_by_user:
            return
        reference = self._last_successful_capture_at or self._capture_started_at
        if reference is None:
            return
        stale_after = max(
            self.REFRESH_SECONDS * 2,
            float(self.cfg.camera.capture_interval_sec) * 2,
        ) + 10.0
        age = (datetime.now() - reference).total_seconds()
        if age > stale_after:
            self._set_system_state("데이터 지연", COLORS["red"])
            if not self._capture_stale_announced:
                self._capture_stale_announced = True
                self._add_operating_log(
                    "캡처",
                    "경고",
                    f"마지막 유효 캡처 후 {age:.0f}초 경과 · 데이터가 최신이 아닙니다",
                )

    def _run_maintenance(self):
        """Run only non-destructive, best-effort backend synchronisation.

        Dataset repair, metadata rebuilding and retention deletion are manual
        maintenance tasks.  They must never be queued from an unattended GUI
        refresh cycle on a factory line.
        """
        now = time.time()

        if now - self._last_backend_sync >= 30 and self.cfg.backend.enabled:
            self._last_backend_sync = now
            self._maintenance_executor.submit(self._sync_events_from_backend)
            self._maintenance_executor.submit(self._sync_temperature_history)

    def _queue_metadata_update(self) -> None:
        """Append metadata for newly radiometrically decoded captures.

        This is intentionally separate from broad integrity repair and all
        retention deletion.  The single maintenance worker serializes CSV
        appends without delaying camera I/O or live analysis.
        """
        self._maintenance_executor.submit(
            self._run_metadata_update,
            self.cfg.paths.dataset_dir,
        )

    @staticmethod
    def _run_metadata_update(save_dir: str) -> None:
        try:
            result = run_metadata(save_dir=save_dir, log_callback=None)
            if result.new > 0:
                _file_log.info("dashboard metadata: %d new records", result.new)
        except Exception as exc:
            _file_log.warning("dashboard metadata update error: %s", exc)

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
        self._add_operating_log("캡처", "시작", "새로고침 버튼으로 즉시 촬영 요청")
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
            npy = pairs.ensure_npy(thermal)
            pair = {"base": thermal.stem, "thermal": thermal, "visual": visual, "npy": npy}
            result = self._process_pair_to_dict(pair)
            self._post_to_ui(lambda: self._apply_capture_refresh_result(result))
        except Exception as exc:
            message = str(exc)
            self._post_to_ui(lambda msg=message: self._handle_capture_refresh_error(msg))

    def _apply_capture_refresh_result(self, result: dict):
        try:
            self._add_operating_log(
                "캡처", "완료", f"{result['base']} 촬영 및 화면 갱신 완료"
            )
            self._apply_analysis_result(result, self._analysis_generation)
        finally:
            self._finish_capture_refresh()

    def _handle_capture_refresh_error(self, message: str):
        try:
            self._add_operating_log("캡처", "실패", message)
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
                self._post_to_ui(lambda: self._finish_analysis(generation))
                return
            result = self._process_pair_to_dict(pair)
            self._post_to_ui(lambda: self._apply_analysis_result(result, generation))
        except Exception as exc:
            message = str(exc)
            self._post_to_ui(
                lambda msg=message: self._handle_analysis_error(msg, generation),
            )

    def _handle_analysis_error(self, message: str, generation: int):
        """Worker 오류를 Tk 메인 스레드에서 로그와 화면에 반영한다."""
        if self.lifecycle != "running":
            self._finish_analysis(generation)
            return
        self.metrics.exception_count += 1
        self._add_operating_log("분석", "실패", message)
        self._append_event("Error", 0.0, f"분석 예외: {message}")
        self._update_metric_text()
        self._finish_analysis(generation)

    def _latest_pair(self):
        """Return the newest thermal, visual and NPY paths for analysis."""
        capture = self.capture
        thermal_only_warning = bool(
            capture is not None and capture.warning_mode
        )
        return latest_analysis_pair(
            self.cfg.paths.dataset_dir,
            visual_mode=(
                self.cfg.tools.mode == "both"
                and not thermal_only_warning
            ),
        )

    def _visual_required_for_quality(self, visual_img) -> bool:
        """Require visual frames except during the intentional thermal-only warning mode."""
        if self.cfg.tools.mode != "both":
            return False
        if visual_img is not None:
            return True
        capture = self.capture
        return not (capture is not None and capture.warning_mode)

    def _process_pair_to_dict(self, pair: dict) -> dict:
        base = pair["base"]
        thermal = pair["thermal"]
        visual = pair["visual"]
        npy = pair["npy"]
        captured_at = capture_time_from_file(base, thermal)
        thermal_img = cv2.imread(str(thermal))
        visual_img = None
        if visual and visual.exists():
            visual_img = cv2.imread(str(visual))
        visual_required = self._visual_required_for_quality(visual_img)
        image_quality_ok, image_quality_reason = assess_image_quality(
            thermal_img, visual_img,
            visual_mode=visual_required,
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
        # 알람 판정용 상태: 클러스터 게이트·히스테리시스를 반영한 API 결과.
        # 텔레그램/경고 전이는 이 값을 쓴다(표시용 status와 분리).
        alarm_status = worst["status"]
        roi_result = worst["roi"]
        overall_max_roi = max(roi_results, key=lambda rr: rr.max_temp)
        overall_max_temp = float(overall_max_roi.max_temp)
        # 화면 표시용 상태: raw 최대온도 기준(클러스터 무시). 작은 핫스팟도
        # 온도가 높으면 위험/경고로 표시하되, 실제 알람은 alarm_status로 판단.
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

        merged_hotspots = merge_roi_hotspot_centroids(roi_results)

        visual_display_img = visual_img
        visual_projection_warning = None
        if visual_img is not None and thermal_img is not None:
            calibration_path = resolve_runtime_path(self.cfg.paths.homography_path)
            visual_display_img, visual_projection_warning = create_visual_roi_overlay(
                visual_img,
                [result.roi_bounds for result in roi_results],
                [result.roi_name or f"ROI-{index + 1}" for index, result in enumerate(roi_results)],
                [item["status"] for item in per_roi_statuses],
                calibration_path=str(calibration_path),
                thermal_size=(thermal_img.shape[1], thermal_img.shape[0]),
            )

        overlay = create_overlay(
            # This panel is explicitly the Thermal view. Passing the visual
            # path would make create_overlay use RGB as its background when a
            # Homography exists, duplicating the visible-image panel.
            str(thermal), "", roi_result.roi_bounds,
            roi_result.max_temp, roi_result.mean_temp, roi_result.hot_temp_95,
            status.value, hotspot_centroids=merged_hotspots,
            roi_bounds_list=_get_roi_bounds_list(roi_cfg),
            roi_names=[r.roi_name for r in roi_results] if len(roi_results) > 1 else None,
        )

        overlay_path = None
        if status != Status.NORMAL:
            overlay_dir = Path(self.cfg.paths.overlay_dir)
            overlay_dir.mkdir(parents=True, exist_ok=True)
            candidate = overlay_dir / f"{base}_overlay.jpg"
            if cv2.imwrite(str(candidate), overlay):
                overlay_path = candidate

        mean_difference = None
        if thermal_img is not None and visual_img is not None:
            thermal_small = cv2.resize(thermal_img, (160, 120))
            visual_small = cv2.resize(visual_img, (160, 120))
            mean_difference = float(cv2.absdiff(thermal_small, visual_small).mean())

        return {
            "base": base, "overlay": overlay, "thermal_img": thermal_img,
            "visual_img": visual_display_img,
            "visual_raw_img": visual_img,
            "visual_projection_warning": visual_projection_warning,
            "max_temp": roi_result.max_temp, "mean_temp": roi_result.mean_temp,
            "min_temp": getattr(roi_result, 'min_temp', roi_result.max_temp),
            "hot_temp_95": roi_result.hot_temp_95,
            "over_temp_pixels": getattr(roi_result, 'over_temp_pixels', 0),
            "max_hotspot_size": getattr(roi_result, 'max_hotspot_size', 0),
            "hotspot_count": len(merged_hotspots),
            "status": status, "alarm_status": alarm_status, "alarm": alarm,
            "measurement_status": alarm_status,
            "measurement_roi": roi_result,
            "overall_max_temp": overall_max_temp,
            "overall_max_roi_name": overall_max_roi.roi_name or "ROI-01",
            "overall_max_roi": overall_max_roi,
            "roi_bounds": roi_result.roi_bounds,
            "roi_results": roi_results,
            "roi_name": roi_result.roi_name,
            "captured_at": captured_at,
            "image_quality_ok": image_quality_ok,
            "image_quality_reason": image_quality_reason,
            "image_quality_mean_difference": mean_difference,
            "thermal_path": thermal,
            "visual_path": visual,
            "npy_path": npy,
            "overlay_path": overlay_path,
            "hotspots": merged_hotspots,
            "thermal_only_mode": (
                self.cfg.tools.mode == "both"
                and visual_img is None
                and not visual_required
            ),
        }

    def _apply_analysis_result(self, result: dict, generation: int):
        if self.lifecycle != "running":
            self._finish_analysis(generation)
            return
        if generation < self._analysis_generation:
            return

        status = result["status"]
        alarm_status = result.get("alarm_status", status)
        previous = self.latest_status
        captured_at = result.get("captured_at") or datetime.now()
        quality_ok = bool(result.get("image_quality_ok", False))
        if quality_ok:
            if self._should_show_critical_popup(self.latest_alarm_status, alarm_status):
                self._show_critical_popup(result, captured_at)
            self.latest_alarm_status = alarm_status
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
            local_event = self._append_event(
                status.value,
                result.get("overall_max_temp", result["max_temp"]),
                "확인 필요",
                result.get("overall_max_roi_name", "ROI-01"),
                event_time=captured_at,
            )
            result["_local_event_id"] = local_event["id"]
            self._last_alert_capture = captured_at
        elif status == Status.NORMAL and previous != Status.NORMAL:
            self._add_operating_log("분석", "성공", "정상 복귀")

        # 새로 촬영된 이미지일 때만 백엔드 DB에 측정값을 기록한다.
        if (
            is_new_capture
            and self._latest_pair_fresh
            and quality_ok
            and self.cfg.backend.enabled
        ):
            result["_backend_posted_event"] = threading.Event()
            local_event_id = result.get("_local_event_id")
            if local_event_id:
                local_event = next(
                    (e for e in self.events if e.get("id") == local_event_id),
                    None,
                )
                if local_event is not None:
                    local_event["backend_pending"] = True
                    self._render_alert_cards()
            threading.Thread(
                target=self.telegram.post_measurement, args=(result,), daemon=True
            ).start()

        if is_new_capture and self._latest_pair_fresh:
            self._queue_metadata_update()

        self.telegram.maybe_dispatch(
            result,
            quality_ok,
            captured_at,
        )

        # 두 영상은 검증을 통과한 한 쌍일 때만 동시에 교체한다.
        # 갱신하면 캡처 저장 시차나 잘못된 파일 쌍 때문에 좌우 영상이
        # 뒤바뀌어 보일 수 있으므로, 이상 프레임은 표시하지 않고 직전의
        # 정상 화면을 유지한다.
        if quality_ok:
            if result["visual_img"] is not None:
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
                    "분석", "실패", result.get("image_quality_reason", "영상 확인 필요")
                )
            self._image_quality_window.append(quality_ok)
            del self._image_quality_window[:-20]
            projection_warning = result.get("visual_projection_warning")
            if quality_ok and projection_warning:
                self._add_operating_log(
                    "캘리브레이션", "경고", f"가시광 ROI 숨김 · {projection_warning}",
                )
        elif is_new_capture:
            self._add_operating_log(
                "분석", "실패",
                f"{capture_id} · 촬영 후 {capture_age:.0f}초 경과",
            )
        self.latest_status = status
        self.last_update = datetime.now()
        self.metrics.analysis_ok += 1
        self._add_operating_log("분석", "성공",
                                f"{result['base']} · {status.value} · Max {result['max_temp']:.1f}°C")
        self._update_values_with_result(result)
        self._finish_analysis(generation)

    @staticmethod
    def _should_show_critical_popup(previous: Status, current: Status) -> bool:
        """위험 상태로 새로 진입할 때만 팝업을 허용한다."""
        return previous != Status.CRITICAL and current == Status.CRITICAL

    def _show_critical_popup(self, result: dict, captured_at: datetime) -> None:
        """Show one non-blocking local popup for a new Critical transition."""
        if self.lifecycle != "running":
            return
        try:
            if self.critical_popup is not None and self.critical_popup.winfo_exists():
                self.critical_popup.lift()
                self.critical_popup.focus_force()
                return
        except tk.TclError:
            self.critical_popup = None

        current_temp = float(result.get("overall_max_temp", result.get("max_temp", 0.0)))
        critical_temp = float(self.cfg.roi.baseline_temp + self.cfg.roi.critical_delta)
        roi_name = result.get("overall_max_roi_name") or result.get("roi_name") or "ROI"

        win = tk.Toplevel(self.root)
        self.critical_popup = win
        win.title("위험 온도 감지")
        win.configure(bg="#2a1010")
        win.resizable(False, False)
        win.transient(self.root)
        win.attributes("-topmost", True)

        def close_popup():
            self.critical_popup = None
            try:
                win.destroy()
            except tk.TclError:
                pass

        win.protocol("WM_DELETE_WINDOW", close_popup)
        container = tk.Frame(win, bg="#2a1010", padx=28, pady=24)
        container.pack(fill="both", expand=True)
        tk.Label(
            container,
            text="⚠  위험 온도 감지",
            bg="#2a1010", fg="#ff5a5a",
            font=("맑은 고딕", 18, "bold"),
        ).pack(anchor="w")
        tk.Label(
            container,
            text=f"{roi_name}의 온도가 위험 수준에 도달했습니다.",
            bg="#2a1010", fg="white",
            font=("맑은 고딕", 12, "bold"),
        ).pack(anchor="w", pady=(14, 12))
        details = (
            f"현재 최고온도   {current_temp:.1f}°C\n"
            f"위험 기준온도   {critical_temp:.1f}°C\n"
            f"감지 시각       {captured_at:%Y-%m-%d %H:%M:%S}"
        )
        tk.Label(
            container,
            text=details,
            justify="left", anchor="w",
            bg="#2a1010", fg="#f3dddd",
            font=("맑은 고딕", 11),
        ).pack(fill="x", pady=(0, 18))
        ttk.Button(container, text="확인", command=close_popup).pack(fill="x")

        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.lift()
        win.focus_force()
        try:
            self.root.bell()
        except tk.TclError:
            pass
        self._add_operating_log(
            "위험 팝업", "표시", f"{roi_name} · {current_temp:.1f}°C",
        )

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
            cutoff = datetime.now() - timedelta(days=self.TREND_HISTORY_DAYS)
            self.temperature_history = [
                point for point in self.temperature_history
                if point[0] >= cutoff
            ]
            self._last_history_capture = captured_at
        self._draw_status_gauge()
        self._draw_temperature_trend()
        if result.get("image_quality_ok", False):
            stamp = captured_at.strftime("촬영 시각 %Y-%m-%d %H:%M:%S")
            self.thermal_stamp.configure(text=stamp)
            if result.get("thermal_only_mode", False):
                self.visual_stamp.configure(text="과열 모드 · 가시광 촬영 생략")
            elif result.get("visual_projection_warning"):
                self.visual_stamp.configure(
                    text=f"ROI 숨김 · {result['visual_projection_warning']}",
                    fg=COLORS["orange"],
                )
            else:
                self.visual_stamp.configure(text=f"{stamp} · ROI 투영", fg=COLORS["muted"])
        else:
            issue = result.get("image_quality_reason", "영상 종류 확인 필요")
            hold_text = f"갱신 보류 · {issue}"
            self.visual_stamp.configure(text=hold_text)
            self.thermal_stamp.configure(text=hold_text)
        self.header_time.configure(text=self.last_update.strftime("마지막 갱신 %H:%M:%S"))

    def _finish_analysis(self, generation: int):
        self._analysis_running = False
        if self._analysis_pending:
            self._schedule_analysis()

    def _draw_visible_roi(self, img, roi):
        x1, y1, x2, y2 = roi; h, w = img.shape[:2]
        sx, sy = w / 640.0, h / 480.0
        cv2.rectangle(img, (int(x1*sx), int(y1*sy)), (int(x2*sx), int(y2*sy)), (0, 255, 0), max(2, w//700))
        cv2.putText(img, "ROI-01", (int(x1*sx), max(25, int(y1*sy)-8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,0), 2)

    def _show_image(self, _canvas, image, kind):
        source_name = "_thermal_source" if kind == "thermal" else "_visual_source"
        setattr(self, source_name, image.copy() if image is not None else None)
        self._schedule_dashboard_image_render(kind)

    def _schedule_dashboard_image_render(self, kind):
        previous = self._image_render_ids.get(kind)
        if previous:
            try:
                self.root.after_cancel(previous)
            except tk.TclError:
                pass
        self._image_render_ids[kind] = self.root.after(
            40,
            lambda selected=kind: self._render_dashboard_image(selected),
        )

    def _render_dashboard_image(self, kind):
        self._image_render_ids[kind] = None
        canvas = self.thermal_label if kind == "thermal" else self.visual_label
        source = self._thermal_source if kind == "thermal" else self._visual_source
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        canvas.delete("all")
        if source is None:
            waiting_text = (
                "열화상 이미지 수신 대기 중"
                if kind == "thermal"
                else "가시광 이미지 수신 대기 중"
            )
            canvas.create_text(
                width // 2,
                height // 2,
                text=waiting_text,
                fill="#9aa9b6",
                font=("맑은 고딕", 11),
            )
            if kind == "thermal":
                self.thermal_photo = None
            else:
                self.visual_photo = None
            return

        rgb = cv2.cvtColor(source, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb)
        pil.thumbnail((width, height), RESAMPLE_LANCZOS)
        photo = ImageTk.PhotoImage(pil)
        canvas.create_image(width // 2, height // 2, image=photo, anchor="center")
        if kind == "thermal":
            self.thermal_photo = photo
        else:
            self.visual_photo = photo

    def _append_event(self, state, temp, action, roi_name="ROI-01", event_time=None):
        now = event_time or datetime.now()
        event = {
            "id": now.strftime("%Y%m%d%H%M%S_%f"),
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "asset": roi_name,
            "state": state,
            "temp": float(temp) if temp is not None else 0.0,
            "action": action,
            "acknowledged_at": None,
        }
        self.events.insert(0, event)
        self._render_alert_cards()
        return event

    def _link_backend_alert(self, local_event_id: str, alert_id) -> None:
        """Finish backend linking and attach an alert ID when one was created."""
        if not local_event_id:
            return
        event = next((e for e in self.events if e.get("id") == local_event_id), None)
        if event is None:
            return
        event["backend_pending"] = False
        if alert_id is None:
            self._render_alert_cards()
            return
        backend_id = str(alert_id)
        event["backend_id"] = backend_id
        duplicates = [
            candidate
            for candidate in self.events
            if candidate is not event and candidate.get("backend_id") == backend_id
        ]
        for duplicate in duplicates:
            if duplicate.get("action") == "확인 완료":
                self._mark_event_acknowledged(
                    event,
                    duplicate.get("acknowledged_at"),
                )
            elif duplicate.get("ack_pending"):
                event["ack_pending"] = True
            self.events.remove(duplicate)
        self._render_alert_cards()

    def acknowledge_selected(self):
        self.open_alert_history()

    def show_all_events(self):
        self.open_alert_history()

    def _update_metric_text_async(self):
        self._post_to_ui(self._update_metric_text)

    def _update_metric_text(self):
        if not hasattr(self, "metric_label"):
            return
        m = self.metrics
        self.metric_label.configure(text=(
            f"카메라 연결 {m.rate(m.connection_successes,m.connection_attempts):.1f}%   ·   "
            f"캡처 성공 {m.rate(m.capture_successes,m.capture_attempts):.1f}%   ·   "
            f"분석 정상 완료 {m.analysis_ok}회   ·   예외 처리 {m.exception_count}회"))

    def _operating_log_summary_text(self) -> str:
        m = self.metrics
        return (
            f"연결 성공률 {m.rate(m.connection_successes, m.connection_attempts):.1f}%   |   "
            f"캡처 성공률 {m.rate(m.capture_successes, m.capture_attempts):.1f}%   |   "
            f"분석 정상 완료 {m.analysis_ok}회   |   예외 처리 {m.exception_count}회   |   "
            f"상태 {self.lifecycle}"
        )

    def _clear_operating_log_references(self, window=None) -> None:
        if window is not None and getattr(self, "operating_log_window", None) is not window:
            return
        self.operating_log_window = None
        self.operating_log_summary_label = None
        self.operating_log_tree = None

    def _close_operating_log(self) -> None:
        win = getattr(self, "operating_log_window", None)
        self._clear_operating_log_references(win)
        try:
            self.operating_log_button.configure(style="Action.TButton")
        except (AttributeError, tk.TclError):
            pass
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass

    def _refresh_operating_log_ui(
        self,
        row: tuple[str, str, str, str] | None = None,
    ) -> None:
        """Push one persisted log row and current metrics into the open popup."""
        if threading.current_thread() is not threading.main_thread():
            self._post_to_ui(lambda current=row: self._refresh_operating_log_ui(current))
            return
        win = getattr(self, "operating_log_window", None)
        label = getattr(self, "operating_log_summary_label", None)
        tree = getattr(self, "operating_log_tree", None)
        if win is None or label is None or tree is None:
            return
        try:
            if not win.winfo_exists():
                self._clear_operating_log_references(win)
                return
            label.configure(text=self._operating_log_summary_text())
            if row is None:
                children = tree.get_children()
                if children:
                    tree.delete(*children)
                for existing_row in self.operating_logs:
                    tree.insert("", "end", values=existing_row)
            else:
                tree.insert("", 0, values=row)
            children = tree.get_children()
            if len(children) > 1000:
                tree.delete(*children[1000:])
        except tk.TclError:
            self._clear_operating_log_references(win)

    def open_operating_log(self):
        if self._operating_log_opening:
            return

        if self.operating_log_window:
            try:
                if self.operating_log_window.winfo_exists():
                    self.operating_log_button.configure(style="Active.Action.TButton")
                    self.operating_log_window.deiconify()
                    self.operating_log_window.lift()
                    self.operating_log_window.focus_force()
                    return
            except tk.TclError:
                pass
            self._clear_operating_log_references(self.operating_log_window)

        # 참조가 유실되더라도 같은 이름의 Tk 창이 남아 있으면 새로 만들지 않는다.
        try:
            existing = self.root.nametowidget(".operating_log")
            if existing.winfo_exists():
                self.operating_log_window = existing
                self.operating_log_button.configure(style="Active.Action.TButton")
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
            self.operating_log_button.configure(style="Active.Action.TButton")
        finally:
            self._operating_log_opening = False

        win.protocol("WM_DELETE_WINDOW", self._close_operating_log)
        summary = ttk.LabelFrame(win, text="운영 지표", padding=10); summary.pack(fill="x", padx=12, pady=(12,6))
        summary_label = ttk.Label(
            summary,
            text=self._operating_log_summary_text(),
            font=("맑은 고딕",10,"bold"),
        )
        summary_label.pack(anchor="w")
        self.operating_log_summary_label = summary_label
        frame = ttk.LabelFrame(win, text="시간순 기록", padding=8); frame.pack(fill="both", expand=True, padx=12, pady=(6,12))
        columns = ("time", "category", "result", "detail")
        tree = ttk.Treeview(frame, columns=columns, show="headings")
        for key, label, width in (("time","시각",155),("category","구분",90),("result","결과",100),("detail","상세 내용",510)):
            tree.heading(key,text=label); tree.column(key,width=width,anchor="w" if key=="detail" else "center")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview); tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left",fill="both",expand=True); scroll.pack(side="right",fill="y")
        self.operating_log_tree = tree
        self._refresh_operating_log_ui()

    def _add_operating_log(self, category: str, result: str, detail: str):
        row = (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), category, result, detail)
        self.operating_logs.insert(0, row)
        del self.operating_logs[1000:]
        _file_log.info("[%s] %s | %s", category, result, detail)
        if threading.current_thread() is threading.main_thread():
            self._refresh_operating_log_ui(row)
        else:
            self._post_to_ui(
                lambda current=row: self._refresh_operating_log_ui(current)
            )
        if self.cfg.backend.enabled:
            payload = {
                "category": category,
                "action": category,
                "result": result,
                "detail": {"message": detail},
            }

            def persist_operation_log():
                try:
                    requests.post(
                        f"{self.cfg.backend.url}/api/operation-logs",
                        json=payload,
                        timeout=bounded_backend_timeout(self.cfg.backend.timeout_sec),
                    )
                except requests.RequestException:
                    _file_log.debug("operation_logs API unavailable", exc_info=True)

            threading.Thread(target=persist_operation_log, daemon=True).start()

    def _sync_temperature_history(self):
        """Load the selected period and join it to live in-memory readings."""
        if not self.cfg.backend.enabled:
            return
        try:
            resp = requests.get(
                f"{self.cfg.backend.url}/api/temperature-trend",
                params={
                    "hours": self.trend_history_hours,
                    "limit": self.TREND_API_LIMIT,
                },
                timeout=bounded_backend_timeout(self.cfg.backend.timeout_sec),
            )
            if resp.status_code != 200:
                return
            points = resp.json().get("points", [])
            if not isinstance(points, list):
                return
            if threading.current_thread() is threading.main_thread():
                self._merge_temperature_history(points)
            else:
                self._post_to_ui(
                    lambda rows=points: self._merge_temperature_history(rows),
                )
        except Exception as exc:
            _file_log.warning("backend temperature trend sync failed: %s", exc)

    def _merge_temperature_history(self, points: list[dict]) -> None:
        cutoff = datetime.now() - timedelta(days=self.TREND_HISTORY_DAYS)
        merged = {
            captured_at: float(temperature)
            for captured_at, temperature in self.temperature_history
            if captured_at >= cutoff
        }
        for point in points:
            try:
                measured_at = datetime.fromisoformat(
                    str(point.get("measured_at", "")).replace("Z", "+00:00")
                )
                if measured_at.tzinfo is not None:
                    measured_at = measured_at.astimezone().replace(tzinfo=None)
                if measured_at < cutoff:
                    continue
                temperature = float(point["max_temp"])
            except (KeyError, TypeError, ValueError):
                continue
            merged[measured_at] = temperature

        self.temperature_history = sorted(merged.items())
        if self.temperature_history:
            self._last_history_capture = self.temperature_history[-1][0]
        self._draw_status_gauge()
        self._draw_temperature_trend()

    def _sync_events_from_backend(self):
        """GET /api/alerts 로 영구 알람 이벤트 목록을 가져와 self.events와 병합."""
        if not self.cfg.backend.enabled:
            return
        try:
            resp = requests.get(
                f"{self.cfg.backend.url}/api/alerts",
                params={
                    "limit": 5000,
                    "hours": self.alert_history_hours,
                },
                timeout=bounded_backend_timeout(self.cfg.backend.timeout_sec),
            )
            if resp.status_code != 200:
                return
            data = resp.json()
            alerts = data.get("alerts", [])
            if not isinstance(alerts, list):
                return
            if threading.current_thread() is threading.main_thread():
                self._merge_backend_alerts(alerts)
            else:
                self._post_to_ui(lambda rows=alerts: self._merge_backend_alerts(rows))
        except Exception as exc:
            _file_log.warning("backend alert sync failed: %s", exc)

    def _merge_backend_alerts(self, alerts: list[dict]) -> None:
        for alert in alerts:
            alert_id = str(alert.get("alert_id", ""))
            if not alert_id:
                continue
            event_status = alert.get("event_status", "open")
            action = (
                "확인 완료"
                if event_status in ("acknowledged", "resolved")
                else "확인 필요"
            )
            event = next(
                (e for e in self.events if e.get("backend_id") == alert_id),
                None,
            )
            ack_pending = bool(
                event is not None
                and event.get("ack_pending")
                and event_status == "open"
            )
            fields = {
                "backend_id": alert_id,
                "time": alert.get("occurred_at", "")[:19].replace("T", " "),
                "asset": alert.get("roi_name", "ROI"),
                "state": alert.get("severity", "normal").capitalize(),
                "temp": float(alert.get("max_temp", 0)),
                "action": action,
                "ack_pending": ack_pending,
                "acknowledged_at": alert.get("acknowledged_at"),
            }
            if event is None:
                self.events.insert(0, {"id": f"be_{alert_id}", **fields})
            else:
                event.update(fields)
        self.events.sort(key=lambda event: event.get("time", ""), reverse=True)
        del self.events[5000:]
        self._render_alert_cards()

    def _acknowledge_event_backend(self, event: dict):
        """이벤트 확인 처리 → PATCH /api/alerts/{id} 호출."""
        backend_id = event.get("backend_id")
        if not backend_id or not self.cfg.backend.enabled:
            return
        event_id = event["id"]

        def work():
            success = False
            detail = ""
            acknowledged_at = None
            try:
                resp = requests.patch(
                    f"{self.cfg.backend.url}/api/alerts/{backend_id}",
                    json={"event_status": "acknowledged"},
                    timeout=bounded_backend_timeout(self.cfg.backend.timeout_sec),
                )
                if resp.status_code == 200:
                    data = resp.json()
                    success = (
                        data.get("status") == "updated"
                        and data.get("event_status") == "acknowledged"
                    )
                    if success:
                        acknowledged_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        detail = str(data.get("error") or data)
                else:
                    detail = f"HTTP {resp.status_code}"
            except Exception as exc:
                detail = str(exc)
            self._post_to_ui(
                lambda: self._finish_backend_ack(
                    event_id,
                    success,
                    acknowledged_at,
                    detail,
                ),
            )
        threading.Thread(target=work, daemon=True).start()

    def _finish_backend_ack(
        self,
        event_id: str,
        success: bool,
        acknowledged_at,
        detail: str,
    ) -> None:
        event = next((e for e in self.events if e.get("id") == event_id), None)
        if event is None:
            return
        event["ack_pending"] = False
        if success:
            self._mark_event_acknowledged(event, acknowledged_at)
            self._add_operating_log(
                "알림",
                "성공",
                f"{event['asset']} · {event['temp']:.1f}°C",
            )
        else:
            self._add_operating_log(
                "알림",
                "실패",
                detail or f"alert_id={event.get('backend_id')}",
            )
        self._render_alert_cards()

    # ── Shutdown ─────────────────────────────────────────────────

    def open_settings(self):
        if self.settings_dialog:
            try:
                if self.settings_dialog.win.winfo_exists():
                    self.settings_button.configure(style="Active.Action.TButton")
                    self.settings_dialog.win.deiconify()
                    self.settings_dialog.win.lift()
                    self.settings_dialog.win.focus_force()
                    return
            except tk.TclError:
                pass
            self.settings_dialog = None
        self.settings_dialog = SettingsDialog(self)
        self.settings_button.configure(style="Active.Action.TButton")

    def apply_saved_settings_immediately(self):
        """Apply saved thresholds and runtime settings without waiting for the timer."""
        self._draw_status_gauge()
        self._draw_temperature_trend()
        self._schedule_refresh(self.REFRESH_SECONDS * 1000)
        self._schedule_analysis()
        self._check_connection_async()

    def on_close(self):
        if self.lifecycle != "running":
            return
        self.lifecycle = "closing"
        self._add_operating_log("프로그램", "시작", "running → closing")
        self._set_system_state("종료 중", COLORS["orange"])
        if self.timer_id:
            self.root.after_cancel(self.timer_id)
            self.timer_id = None
        self._cancel_connection_retry()
        if self._ui_dispatch_timer:
            try:
                self.root.after_cancel(self._ui_dispatch_timer)
            except tk.TclError:
                pass
            self._ui_dispatch_timer = None
        self._discard_ui_callbacks()
        self._stop_gige_probe()
        if self._gige_stop_wait_timer:
            try:
                self.root.after_cancel(self._gige_stop_wait_timer)
            except tk.TclError:
                pass
            self._gige_stop_wait_timer = None
        gige_reader = self._stopping_gige_reader
        capture = self.capture or self._stopping_capture
        self.capture = None
        self.monitoring = False
        if capture is not None:
            capture.request_stop()
        # Cancel queued disk/backend work.  Active requests are daemon workers,
        # but no longer get access to the Tk interpreter through the UI queue.
        self._analysis_executor.shutdown(wait=False, cancel_futures=True)
        self._maintenance_executor.shutdown(wait=False, cancel_futures=True)

        def finish_close() -> None:
            if capture is not None and not capture.wait_stopped(timeout=0):
                try:
                    self.root.after(100, finish_close)
                except tk.TclError:
                    pass
                return
            if gige_reader is not None and not gige_reader.wait_stopped(timeout=0):
                try:
                    self.root.after(100, finish_close)
                except tk.TclError:
                    pass
                return
            if self._stopping_gige_reader is gige_reader:
                self._stopping_gige_reader = None
            self.lifecycle = "closed"
            _file_log.info("dashboard lifecycle closing → closed")
            try:
                self.root.destroy()
            except tk.TclError:
                pass

        finish_close()


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
        general = ttk.Frame(notebook, padding=16)
        roi = ttk.Frame(notebook, padding=16)
        advanced = ttk.Frame(notebook, padding=16)
        notifications = ttk.Frame(notebook, padding=16)
        notebook.add(general, text="일반")
        notebook.add(roi, text="감시 영역")
        notebook.add(advanced, text="고급 설정")
        notebook.add(notifications, text="알림 전송 설정")
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
        self._build_notification_settings(notifications)
        buttons = ttk.Frame(self.win); buttons.pack(fill="x", padx=14, pady=(0,14))
        ttk.Button(buttons, text="취소", command=self.close).pack(side="right", padx=4)
        ttk.Button(buttons, text="저장", style="Action.TButton", command=self.save).pack(side="right", padx=4)

    def _build_notification_settings(self, parent):
        from ..analysis import notifier

        settings = notifier.get_settings()
        self._notification_login_running = False
        self.telegram_login_window: Optional[tk.Toplevel] = None
        self.telegram_token = tk.StringVar(value=settings["bot_token"])
        self.telegram_chat_id = tk.StringVar(value=settings["chat_id"])

        ttk.Label(
            parent,
            text="Telegram 알림",
            font=("맑은 고딕", 13, "bold"),
        ).pack(anchor="w", pady=(0, 5))
        ttk.Label(
            parent,
            text="위험(Critical) 알람 발생 시 등록된 Telegram 채팅으로 알림을 전송합니다.",
            foreground="#59636d",
        ).pack(anchor="w", pady=(0, 14))

        state_box = ttk.LabelFrame(parent, text="연결 상태", padding=16)
        state_box.pack(fill="x")
        self.telegram_status_label = ttk.Label(
            state_box,
            font=("맑은 고딕", 11, "bold"),
        )
        self.telegram_status_label.pack(anchor="w")

        controls = ttk.Frame(state_box)
        controls.pack(fill="x", pady=(14, 0))
        self.telegram_login_button = ttk.Button(
            controls,
            text="로그인",
            style="Action.TButton",
            command=self._open_telegram_login_window,
        )
        self.telegram_login_button.pack(side="left", padx=(0, 6))
        self.telegram_logout_button = ttk.Button(
            controls,
            text="로그아웃",
            command=self._logout_telegram,
        )
        self.telegram_logout_button.pack(side="left")
        self.telegram_toggle_button = tk.Button(
            controls,
            command=self._toggle_telegram_delivery,
            relief="flat",
            bd=0,
            padx=18,
            pady=9,
            cursor="hand2",
            font=("맑은 고딕", 10, "bold"),
        )
        self.telegram_toggle_button.pack(side="right")
        ttk.Label(
            parent,
            text="로그인을 누르면 별도 창에서 Bot Token과 Chat ID를 입력할 수 있습니다.\n"
                 "로그인 정보는 이 PC의 보호된 dashboard 환경 파일에만 저장되며 Git에는 업로드되지 않습니다.",
            foreground="#59636d",
            justify="left",
        ).pack(anchor="w", pady=(12, 0))
        self._refresh_telegram_controls()

    def _open_telegram_login_window(self):
        if self.telegram_login_window:
            try:
                if self.telegram_login_window.winfo_exists():
                    self.telegram_login_window.deiconify()
                    self.telegram_login_window.lift()
                    self.telegram_login_window.focus_force()
                    return
            except tk.TclError:
                pass
            self.telegram_login_window = None

        win = tk.Toplevel(self.win, name="telegram_login")
        win.title("Telegram 로그인")
        win.geometry("500x285")
        win.resizable(False, False)
        win.transient(self.win)
        win.grab_set()
        win.protocol("WM_DELETE_WINDOW", self._close_telegram_login_window)
        self.telegram_login_window = win
        self.telegram_login_button.configure(style="Active.Action.TButton")

        content = ttk.Frame(win, padding=20)
        content.pack(fill="both", expand=True)
        ttk.Label(
            content,
            text="Telegram Bot 연결",
            font=("맑은 고딕", 13, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(
            content,
            text="BotFather에서 발급받은 Token과 알림을 받을 Chat ID를 입력하세요.",
            foreground="#59636d",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 14))
        ttk.Label(content, text="Bot Token", width=12).grid(
            row=2, column=0, sticky="w", padx=(0, 10), pady=7,
        )
        self.telegram_token_entry = ttk.Entry(
            content,
            textvariable=self.telegram_token,
            show="●",
        )
        self.telegram_token_entry.grid(row=2, column=1, sticky="ew", pady=7)
        ttk.Label(content, text="Chat ID", width=12).grid(
            row=3, column=0, sticky="w", padx=(0, 10), pady=7,
        )
        ttk.Entry(content, textvariable=self.telegram_chat_id).grid(
            row=3, column=1, sticky="ew", pady=7,
        )
        content.columnconfigure(1, weight=1)

        self.telegram_login_status_label = ttk.Label(
            content,
            text="",
            foreground=COLORS["orange"],
        )
        self.telegram_login_status_label.grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 4),
        )
        buttons = ttk.Frame(content)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(6, 0))
        ttk.Button(
            buttons,
            text="취소",
            command=self._close_telegram_login_window,
        ).pack(side="right", padx=(6, 0))
        self.telegram_login_submit_button = ttk.Button(
            buttons,
            text="로그인",
            style="Action.TButton",
            command=self._login_telegram,
        )
        self.telegram_login_submit_button.pack(side="right")
        self.telegram_token_entry.focus_set()

    def _close_telegram_login_window(self):
        if self._notification_login_running:
            return
        win = self.telegram_login_window
        self.telegram_login_window = None
        self.telegram_login_button.configure(style="Action.TButton")
        if win:
            try:
                if win.winfo_exists():
                    win.grab_release()
                    win.destroy()
            except tk.TclError:
                pass

    def _refresh_telegram_controls(self):
        from ..analysis import notifier

        settings = notifier.get_settings()
        configured = settings["configured"]
        enabled = settings["enabled"] and configured
        if not configured:
            status_text, status_color = "● 미로그인", COLORS["red"]
        elif enabled:
            status_text, status_color = "● 연결됨 · 알림 전송 활성화", COLORS["green"]
        else:
            status_text, status_color = "● 연결됨 · 알림 전송 비활성화", COLORS["orange"]
        self.telegram_status_label.configure(text=status_text, foreground=status_color)
        self.telegram_toggle_button.configure(
            text="알림 전송 비활성화" if enabled else "알림 전송 활성화",
            bg=COLORS["green"] if enabled else "#c9ced3",
            fg="white" if enabled else "#20252a",
            activebackground=COLORS["green"] if enabled else "#b8bec4",
            activeforeground="white" if enabled else "#20252a",
            state="normal" if configured else "disabled",
        )
        self.telegram_logout_button.configure(
            state="normal" if configured else "disabled",
        )

    def _login_telegram(self):
        if self._notification_login_running:
            return
        token = self.telegram_token.get().strip()
        chat_id = self.telegram_chat_id.get().strip()
        if not token or not chat_id:
            messagebox.showwarning(
                "Telegram 로그인",
                "Bot Token과 Chat ID를 모두 입력하세요.",
                parent=self.telegram_login_window or self.win,
            )
            return

        self._notification_login_running = True
        self.telegram_login_submit_button.configure(
            text="연결 확인 중...",
            state="disabled",
        )
        self.telegram_login_status_label.configure(
            text="Telegram 서버와 연결을 확인하고 있습니다.",
            foreground=COLORS["orange"],
        )

        def work():
            from ..analysis import notifier
            result = notifier.test_connection(token, chat_id)
            # Worker threads must not inspect a Tk widget or call ``after``:
            # the dashboard may be closing while a network request returns.
            self.d._post_to_ui(
                lambda: self._finish_telegram_login(token, chat_id, result),
            )

        threading.Thread(target=work, daemon=True).start()

    def _finish_telegram_login(self, token, chat_id, result):
        from ..analysis import notifier

        self._notification_login_running = False
        if self.telegram_login_window and self.telegram_login_window.winfo_exists():
            self.telegram_login_submit_button.configure(text="로그인", state="normal")
        connected, detail = result
        if connected:
            notifier.configure(token, chat_id, enabled=False, persist=True)
            self.d._add_operating_log("Telegram", "성공", detail)
            self._refresh_telegram_controls()
            self._close_telegram_login_window()
            messagebox.showinfo(
                "Telegram 로그인",
                f"{detail}\n\n알림 전송 버튼을 활성화하면 위험(Critical) 알림을 받을 수 있습니다.",
                parent=self.win,
            )
        else:
            if self.telegram_login_window and self.telegram_login_window.winfo_exists():
                self.telegram_login_status_label.configure(
                    text=detail,
                    foreground=COLORS["red"],
                )
            self.d._add_operating_log("Telegram", "실패", detail)

    def _logout_telegram(self):
        from ..analysis import notifier

        if not messagebox.askyesno(
            "Telegram 로그아웃",
            "저장된 Bot Token과 Chat ID를 이 PC에서 삭제하시겠습니까?",
            parent=self.win,
        ):
            return
        notifier.logout(persist=True)
        self.telegram_token.set("")
        self.telegram_chat_id.set("")
        self.d._add_operating_log("Telegram", "성공", "로컬 로그인 정보 삭제")
        self._refresh_telegram_controls()

    def _toggle_telegram_delivery(self):
        from ..analysis import notifier

        settings = notifier.get_settings()
        try:
            notifier.set_enabled(not settings["enabled"], persist=True)
        except RuntimeError as exc:
            messagebox.showwarning("알림 전송 설정", str(exc), parent=self.win)
            return
        updated = notifier.get_settings()["enabled"]
        self.d._add_operating_log(
            "Telegram",
            "성공",
            "자동 위험(Critical) 알림 전송",
        )
        self._refresh_telegram_controls()

    def close(self):
        if self._tool_running:
            return
        if self.telegram_login_window:
            if self._notification_login_running:
                return
            self._close_telegram_login_window()
        self.d.settings_dialog = None
        self.d.settings_button.configure(style="Action.TButton")
        if self.win.winfo_exists():
            self.win.destroy()

    def _focus_running_tool(self):
        """실행 중인 OpenCV 도구 창을 새로 만들지 않고 앞으로 가져온다."""
        for title in self._tool_window_titles:
            try:
                if cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) >= 1:
                    cv2.setWindowProperty(title, cv2.WND_PROP_TOPMOST, 1)
            except cv2.error:
                continue

    def _show_tool_guard(self):
        """OpenCV 도구 실행 중 대시보드 입력이 대기열에 쌓이지 않게 한다."""
        guard = tk.Toplevel(self.win)
        guard.title("작업 진행 중")
        guard.transient(self.win)
        guard.resizable(False, False)
        # 실제 캘리브레이션 작업창과는 별도인 안내창이다. 고정 320×130은
        # 고해상도·고DPI 화면에서 지나치게 작으므로 화면 비율 안에서 제한한다.
        screen_width = self.win.winfo_screenwidth()
        screen_height = self.win.winfo_screenheight()
        guard_width = max(380, min(460, int(screen_width * 0.28)))
        guard_height = max(160, min(190, int(screen_height * 0.18)))
        guard.geometry(f"{guard_width}x{guard_height}")
        guard.protocol("WM_DELETE_WINDOW", lambda: None)

        body = ttk.Frame(guard, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="캘리브레이션 창에서 작업을 완료하세요.",
            font=("맑은 고딕", 11, "bold"),
        ).pack(pady=(0, 16))
        ttk.Button(
            body,
            text="캘리브레이션 창 보기",
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
        """OpenCV 루프 중 모달 가드의 Tk 이벤트를 처리한다."""
        try:
            if self._tool_guard_window and self._tool_guard_window.winfo_exists():
                self._tool_guard_window.update()
        except tk.TclError:
            # 종료 경합으로 가드가 먼저 파괴돼도 OpenCV 도구는 계속 종료할 수 있다.
            pass

    def _begin_tool(self, tool_name: str) -> bool:
        """ROI/캘리브레이션 도구는 프로세스에서 한 번에 하나만 실행한다."""
        if self._tool_running:
            self.d._add_operating_log(
                "설정", "실패", f"{self._tool_running} 작업이 이미 실행 중"
            )
            messagebox.showinfo(
                "작업 진행 중",
                f"{self._tool_running} 작업이 이미 실행 중입니다.",
                parent=self.win,
            ) 
            return False
        self._tool_running = tool_name
        self._roi_editor_running = tool_name == "ROI 설정"
        self._calibration_running = tool_name == "캘리브레이션"
        self.roi_button.configure(
            style="Active.TButton" if self._roi_editor_running else "TButton"
        )
        self.calibration_button.configure(
            style="Active.TButton" if self._calibration_running else "TButton"
        )
        self.win.update_idletasks()
        self.win.grab_release()
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
        self.roi_button.configure(style="TButton")
        self.calibration_button.configure(style="TButton")
        if self.win.winfo_exists():
            self.win.grab_set()

    def _tool_display_bounds(self):
        """현재 Tk 가상 화면 영역을 OpenCV 도구에 전달한다."""
        self.win.update_idletasks()
        return (
            self.win.winfo_vrootx(),
            self.win.winfo_vrooty(),
            self.win.winfo_vrootwidth(),
            self.win.winfo_vrootheight(),
        )

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

    @staticmethod
    def _latest_complete_image_pair(dataset: Path):
        """가장 최신의 Thermal/Visual 완성 쌍을 반환한다 (공용 pairs 모듈 위임)."""
        return pairs.latest_complete_pair(dataset)
    def open_roi_editor(self):
        if not self._require_factory_capture_quiescent("ROI를 변경"):
            return
        dataset = Path(self.d.cfg.paths.dataset_dir)
        if not dataset.exists():
            messagebox.showwarning("ROI 설정", "데이터셋 폴더가 없습니다.", parent=self.win); return
        pair = self._latest_complete_image_pair(dataset)
        if pair is None:
            messagebox.showwarning(
                "ROI 설정",
                "완성된 열화상·가시광 이미지 쌍이 없습니다.\n"
                "이미지 수집이 완료된 후 다시 시도하세요.",
                parent=self.win,
            )
            return
        thermal, visual = pair
        calibration_path = resolve_runtime_path(self.d.cfg.paths.homography_path)
        if not calibration_path.exists():
            messagebox.showwarning(
                "ROI 설정",
                "캘리브레이션 정보가 없습니다.\n캘리브레이션을 먼저 실행하세요.",
                parent=self.win,
            )
            return
        if not self._begin_tool("ROI 설정"):
            return
        self.d._add_operating_log("설정", "시작", str(visual))
        try:
            from .tk_image_dialogs import show_roi_dialog
            from .roi_api_client import sync_rois

            def save_rois_to_db(entries):
                if not self.d.cfg.backend.enabled:
                    raise RuntimeError(
                        "Backend API 연동이 비활성화되어 있습니다. "
                        "환경설정의 Backend 설정을 확인하세요."
                    )
                result, roi_id_map = sync_rois(
                    self.d.cfg.backend.url,
                    self.d.cfg.identity.camera_id,
                    self.d.cfg.camera.ip,
                    entries,
                    timeout=bounded_backend_timeout(self.d.cfg.backend.timeout_sec),
                    database_camera_id=self.d.cfg.identity.db_camera_id,
                )
                self.d.cfg.identity.db_camera_id = result.camera_id
                # 저장된 ROI ID를 config에 반영
                for entry in entries:
                    name = entry.name
                    if name in roi_id_map:
                        entry.db_roi_id = roi_id_map[name]
                threshold_result = self._sync_thresholds_to_backend(entries)
                self.d._add_operating_log(
                    "DB",
                    "성공",
                    f"camera_id={result.camera_id}, "
                    f"신규 버전 {result.created}개, 변경 없음 {result.unchanged}개 · "
                    f"threshold 생성 {threshold_result.created}개, "
                    f"갱신 {threshold_result.updated}개",
                )

            saved = show_roi_dialog(
                self.win,
                str(thermal),
                str(visual),
                save_handler=save_rois_to_db,
            )
            self.d.cfg = load_config(force_reload=True)
            result_text = "성공"
            self.d._add_operating_log(
                "설정", result_text,
                f"{len(self.d.cfg.roi.rois)}개 영역 저장됨" if saved else "저장 없이 종료",
            )
        except Exception as exc:
            self.d._add_operating_log("설정", "예외 처리", str(exc))
            messagebox.showerror("ROI 설정", str(exc), parent=self.win)
        finally:
            self._end_tool()

    def open_calibration(self):
        dataset = Path(self.d.cfg.paths.dataset_dir)
        pair = self._latest_complete_image_pair(dataset) if dataset.exists() else None
        if pair is None:
            messagebox.showwarning(
                "캘리브레이션",
                "완성된 열화상·가시광 이미지 쌍이 없습니다.\n"
                "이미지 수집이 완료된 후 다시 시도하세요.",
                parent=self.win,
            )
            return
        thermal, visual = pair
        if not self._begin_tool("캘리브레이션"):
            return
        self.d._add_operating_log("캘리브레이션", "시작", thermal.name)
        saved = False
        calibration_window_title = None
        try:
            from .calibration import CALIBRATION_WINDOW_TITLE, run_calibration
            calibration_window_title = CALIBRATION_WINDOW_TITLE
            self._tool_window_titles = (CALIBRATION_WINDOW_TITLE,)
            self._show_tool_guard()
            def save_calibration_to_db(calibration_data):
                if not self.d.cfg.backend.enabled:
                    return
                camera_id = self.d.cfg.identity.db_camera_id
                if camera_id is None:
                    raise RuntimeError("캘리브레이션을 저장할 DB camera_id가 없습니다.")
                response = requests.post(
                    f"{self.d.cfg.backend.url}/api/calibrations",
                    json={"camera_id": camera_id, **calibration_data},
                    timeout=bounded_backend_timeout(self.d.cfg.backend.timeout_sec),
                )
                response.raise_for_status()
                body = response.json()
                if body.get("status") != "created":
                    raise RuntimeError(body.get("error", "캘리브레이션 DB 저장 실패"))

            saved = bool(run_calibration(
                str(thermal),
                str(visual),
                event_pump=self._pump_tool_events,
                display_bounds=self._tool_display_bounds(),
                result_callback=save_calibration_to_db,
            ))
            if saved:
                calibration_path = resolve_runtime_path(self.d.cfg.paths.homography_path)
                self.d._add_operating_log("캘리브레이션", "완료", str(calibration_path))
            else:
                self.d._add_operating_log("캘리브레이션", "종료", "저장 없이 종료")
        except Exception as exc:
            self.d.metrics.exception_count += 1
            self.d._add_operating_log("캘리브레이션", "예외 처리", str(exc))
            messagebox.showerror("캘리브레이션", str(exc), parent=self.win)
            saved = False
        finally:
            if calibration_window_title:
                try:
                    if cv2.getWindowProperty(
                        calibration_window_title,
                        cv2.WND_PROP_VISIBLE,
                    ) >= 0:
                        cv2.destroyWindow(calibration_window_title)
                except cv2.error:
                    pass
            self._end_tool()
        if saved and self.win.winfo_exists() and messagebox.askyesno(
            "ROI 설정",
            "캘리브레이션이 완료되었습니다.\n\n"
            "가시광 이미지에서 ROI 영역을 설정하시겠습니까?\n"
            "(가시광에서 지정한 ROI는 열화상 좌표로 자동 변환됩니다.)",
            parent=self.win,
        ):
            self.win.after_idle(self.open_roi_editor)

    def _require_factory_capture_quiescent(self, action: str) -> bool:
        """Require stopped HTTP/GigE owners before remote policy mutation."""

        active = factory_mode_enabled() and (
            getattr(self.d, "monitoring", False)
            or getattr(self.d, "capture", None) is not None
            or getattr(self.d, "_stopping_capture", None) is not None
            or getattr(self.d, "_gige_reader", None) is not None
            or getattr(self.d, "_stopping_gige_reader", None) is not None
        )
        if not active:
            return True
        # Asset/ROI/threshold writes and local atomic replacement span process
        # boundaries and cannot be one transaction.  Quiescence prevents live
        # analysis from observing a partially changed remote policy.
        messagebox.showerror(
            "촬영 정지 필요",
            f"현장에서 {action} 전에 촬영을 정지하고 이전 CaptureSession과 "
            "GigE reader의 종료가 완료될 때까지 기다리세요.",
            parent=self.win,
        )
        return False

    def save(self):
        try:
            if not self._require_factory_capture_quiescent("설정을 저장하기"):
                return

            camera_ip = self.ip.get().strip()
            dataset_value = self.dataset_dir.get().strip()
            if not dataset_value:
                messagebox.showerror("입력 오류", "데이터 저장 폴더를 선택하세요.", parent=self.win)
                return

            raw_dataset_path = Path(
                os.path.expandvars(os.path.expanduser(dataset_value))
            )
            if not raw_dataset_path.is_absolute():
                raw_dataset_path = PROJECT_ROOT / raw_dataset_path
            dataset_path = str(raw_dataset_path.resolve(strict=False))
            overlay_path = str(Path(dataset_path) / "overlay")

            # Validate a complete candidate before creating folders or writing
            # anything to the backend.  Invalid threshold ordering or a mount
            # root selection must be rejected without side effects.
            candidate = deepcopy(self.d.cfg)
            candidate.camera.ip = camera_ip
            candidate.paths.dataset_dir = dataset_path
            candidate.paths.overlay_dir = overlay_path
            candidate.roi.baseline_temp = float(self.baseline.get())
            candidate.roi.warning_delta = float(self.warning.get())
            candidate.roi.critical_delta = float(self.critical.get())
            # A generated factory config deliberately starts with Backend
            # persistence disabled and no DB identity.  Commissioning is an
            # explicit operator decision in this dialog: first validate the
            # locally safe candidate with persistence disabled, then register
            # the hierarchy, and only persist enabled=True after the API
            # returns an authoritative camera ID.
            bootstrap_backend = bool(
                factory_mode_enabled()
                and
                not candidate.backend.enabled
                and candidate.identity.db_camera_id is None
            )
            if bootstrap_backend and not messagebox.askyesno(
                "현장 Backend 등록",
                "이 설비는 아직 Backend에 등록되지 않았습니다.\n\n"
                "계속하면 factory·line·robot·camera 정보를 Backend에 등록하고 "
                "측정값 DB 저장을 활성화합니다. 승인된 현장 정보와 연결 상태를 "
                "확인한 뒤 계속하시겠습니까?",
                parent=self.win,
            ):
                return

            # ``validate_config`` correctly rejects enabled=True without a
            # DB camera ID.  Keep it false through local validation and turn
            # it on only after successful registration below.
            if bootstrap_backend:
                candidate.backend.enabled = False
            validate_config(candidate, collection_mode=True)

            current_dataset_path = Path(
                os.path.expandvars(os.path.expanduser(self.d.cfg.paths.dataset_dir))
            )
            if not current_dataset_path.is_absolute():
                current_dataset_path = PROJECT_ROOT / current_dataset_path
            capture_runtime_changed = (
                camera_ip != self.d.cfg.camera.ip
                or dataset_path != str(current_dataset_path.resolve(strict=False))
            )

            registration = None
            if candidate.backend.enabled or bootstrap_backend:
                from .asset_api_client import register_asset_hierarchy

                identity = candidate.identity
                # DB 스키마의 factory → line → robot → camera 외래키 연결은
                # 측정·알림 저장에 필요하다. 사용자가 입력하거나 화면에서 보는
                # 항목은 제거하고, 기존 값 또는 내부 기본값으로만 유지한다.
                factory_name = identity.factory_name.strip() or "ThermoGuard"
                line_name = identity.line_name.strip() or "기본 라인"
                robot_name = identity.robot_name.strip() or identity.robot_id
                registration = register_asset_hierarchy(
                    base_url=candidate.backend.url,
                    timeout=bounded_backend_timeout(candidate.backend.timeout_sec),
                    factory_name=factory_name,
                    line_name=line_name,
                    robot_code=identity.robot_id,
                    robot_name=robot_name,
                    camera_code=identity.camera_id,
                    camera_ip=camera_ip,
                    capture_mode=candidate.tools.mode,
                    normal_interval_sec=candidate.camera.capture_interval_sec,
                    warning_interval_sec=candidate.camera.warning_interval_sec,
                    factory_id=identity.factory_id,
                    line_id=identity.line_id,
                    robot_id=identity.db_robot_id,
                    camera_id=identity.db_camera_id,
                )

                identity.factory_id = registration.factory_id
                identity.line_id = registration.line_id
                identity.db_robot_id = registration.robot_id
                identity.db_camera_id = registration.camera_id
                if bootstrap_backend:
                    candidate.backend.enabled = True
                # Prove the now-complete persisted form satisfies strict
                # startup validation before any local state changes.
                validate_config(candidate, collection_mode=True)

                # This is intentionally before save_config/self.d.cfg/capture
                # restart.  If threshold sync fails, the running capture stays
                # on its prior coherent configuration rather than splitting
                # camera, ROI and DB identities across two settings versions.
                self._sync_thresholds_to_backend(cfg=candidate)

            # In development this still restarts capture when its source path
            # changes.  Factory save is accepted only after capture has become
            # quiescent, so the next operator start necessarily uses the fully
            # persisted candidate.
            os.makedirs(dataset_path, exist_ok=True)
            os.makedirs(overlay_path, exist_ok=True)
            save_collection_config(candidate)
            self.d.cfg = candidate
            if bootstrap_backend:
                # The factory start gate intentionally marked monitoring as
                # paused.  A successful, explicitly approved registration is
                # the only path that clears that commissioning pause.
                self.d.capture_paused_by_user = False
                self.d._commissioning_block_announced = False
            if registration is not None:
                self.d._add_operating_log(
                    "DB",
                    "저장 완료",
                    f"카메라 연결 정보 저장 (camera_id={registration.camera_id})",
                )
            else:
                self.d._add_operating_log(
                    "DB",
                    "보류",
                    "Backend 연동이 비활성화되어 로컬 설정만 저장함",
                )
            self.d._add_operating_log("설정", "성공", dataset_path)
            self.d._add_operating_log(
                "설정",
                "성공",
                (
                    f"정상 {self.d.cfg.roi.baseline_temp:.1f}°C · "
                    f"경고 +{self.d.cfg.roi.warning_delta:.1f}°C · "
                    f"위험 +{self.d.cfg.roi.critical_delta:.1f}°C"
                ),
            )

            if capture_runtime_changed and self.d.capture:
                old_capture = self.d.capture
                self.d.monitoring = False
                self.d.capture = None
                self.d._stop_gige_probe()
                old_capture.request_stop()
                self.d._stopping_capture = old_capture
                self.d.capture_paused_by_user = False
                self.d._wait_for_capture_stop(old_capture, restart=True)

            self.d.apply_saved_settings_immediately()
            self.close()
        except ConfigValidationError as exc:
            messagebox.showerror(
                "안전 검증 실패",
                f"설정이 현장 운영 조건을 만족하지 않습니다.\n\n{exc}",
                parent=self.win,
            )
        except OSError as exc:
            messagebox.showerror("저장 경로 오류", f"폴더를 만들거나 사용할 수 없습니다.\n{exc}", parent=self.win)
        except ValueError:
            messagebox.showerror("입력 오류", "숫자 설정값을 확인하세요.", parent=self.win)
        except Exception as exc:
            self.d._add_operating_log("DB", "실패", str(exc))
            messagebox.showerror(
                "카메라 정보 DB 저장 실패",
                f"카메라 연결 정보를 저장하지 못했습니다.\n\n{exc}",
                parent=self.win,
            )

    def _sync_thresholds_to_backend(self, roi_entries=None, *, cfg=None):
        """Synchronize one validated config before making it live locally.

        ``cfg`` allows Settings save to prove backend threshold persistence
        before it writes ``config.json`` or replaces ``self.d.cfg``.  ROI
        editing keeps the default current dashboard config.
        """

        target_cfg = self.d.cfg if cfg is None else cfg
        if not target_cfg.backend.enabled or not target_cfg.identity.db_camera_id:
            raise RuntimeError(
                "Backend 또는 카메라 DB ID가 없어 threshold를 저장할 수 없습니다."
            )

        from .threshold_api_client import sync_threshold_profiles

        entries = target_cfg.roi.rois if roi_entries is None else roi_entries
        roi_ids = [
            (
                entry.get("db_roi_id")
                if isinstance(entry, dict)
                else getattr(entry, "db_roi_id", None)
            )
            for entry in entries
        ]
        roi_ids = [roi_id for roi_id in roi_ids if roi_id is not None]
        if not roi_ids:
            self.d._add_operating_log(
                "DB",
                "시작",
                "저장된 DB ROI ID가 없어 camera-wide threshold를 동기화합니다.",
            )

        result = sync_threshold_profiles(
            base_url=target_cfg.backend.url,
            timeout=bounded_backend_timeout(target_cfg.backend.timeout_sec),
            camera_id=target_cfg.identity.db_camera_id,
            roi_ids=roi_ids,
            baseline_temp=target_cfg.roi.baseline_temp,
            warning_delta=target_cfg.roi.warning_delta,
            critical_delta=target_cfg.roi.critical_delta,
            min_hotspot_size=target_cfg.hotspot.min_size,
            min_hotspot_size_max=target_cfg.hotspot.min_size_max,
            alarm_cooldown_sec=target_cfg.monitoring.alarm_cooldown_sec,
        )
        _file_log.info(
            "backend ROI threshold sync success: camera_id=%s roi_ids=%s "
            "created=%s updated=%s",
            result.camera_id,
            result.roi_ids,
            result.created,
            result.updated,
        )
        self.d._add_operating_log(
            "DB",
            "저장 완료",
            f"ROI {len(result.roi_ids)}개 · 생성 {result.created}개 · "
            f"갱신 {result.updated}개",
        )
        return result


def main() -> int:
    """Run the supervised data-collection dashboard without factory gates."""
    # Keep the runtime scope solely so CaptureSession can identify this as the
    # supported dashboard path when THERMOGUARD_FACTORY_MODE happens to remain
    # set.  It does not acquire or require a host lock.
    with dashboard_runtime_scope():
        root = tk.Tk()
        app = ProductDashboard(root)
        root.protocol("WM_DELETE_WINDOW", app.on_close)
        try:
            root.mainloop()
        finally:
            if getattr(app, "lifecycle", "closed") == "running":
                app.on_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

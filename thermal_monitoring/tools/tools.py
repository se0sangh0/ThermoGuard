"""
tools.py - 통합 모니터링 대시보드 GUI

환경 설정, 실시간 감지 화면, 로그 테이블을 하나의 GUI에서 제공합니다.
캡처 시작부터 분석, 알림까지 통합 수행합니다.

사용법:
    python tools.py

[GUI-UPDATE 변경 요약]
    - 계획서 기능: Mode, Check Dataset, Generate Metadata 추가
    - System Status: Not Ready / Ready / Monitoring / Error 추가
    - ExifTool 경로 선택과 비동기 카메라 연결 검사 추가
    - Detection Log / Activity Log 분리 및 백그라운드 로그 연동
    - thermal-only 수집·분석과 사용자 오류 피드백 보완
"""

import os
import sys
import glob
import shutil
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog
from datetime import datetime
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
from PIL import Image, ImageTk
import requests

from ..config import load_config, save_config
from ..capture.capture import CaptureSession
# GUI-UPDATE: 계획서의 데이터 검사·메타데이터 기능을 버튼에서 호출한다.
from ..data.checking import run_check, CheckResult
from ..data.metadata import run_metadata, MetadataResult
from ..data.cleanup import run_cleanup, CleanupResult
from ..analysis.roi import load_roi_config, extract_roi_from_npy, RoiResult, extract_all_rois_from_npy, _get_roi_bounds_list
from ..analysis.threshold import (
    Status, MonitorState, evaluate_with_state,
)
from ..analysis.overlay import create_overlay, _load_homography
from ..analysis.notifier import send_alarm
from ..logger import get_logger

_log = get_logger("tools")

DATASET_DIR = load_config().paths.dataset_dir
HOMOGRAPHY_PATH = load_config().paths.homography_path


class MonitoringDashboard:
    """통합 모니터링 대시보드 — 3섹션 레이아웃"""

    MAX_LOG_ROWS = 500
    DISPLAY_IMAGE_WIDTH = 560

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Robot Thermal Monitoring Dashboard")
        self.root.geometry("1060x820")
        self.root.minsize(900, 650)
        self.root.resizable(True, True)

        self._config = load_config()
        self._monitor_state = MonitorState()
        self._capture_session: Optional[CaptureSession] = None
        self._running = False
        self._tick_count = 0
        self._scan_timer_id: Optional[str] = None

        # 분석 전용 작업 스레드 (UI 블로킹 방지)
        self._analysis_executor = ThreadPoolExecutor(max_workers=1)
        self._analysis_running = False
        self._analysis_pending = False
        self._analysis_generation = 0

        self._current_view = "thermal"
        self._current_overlay: Optional[np.ndarray] = None
        self._current_visual_overlay: Optional[np.ndarray] = None
        self._current_status: str = "Normal"
        self._current_hotspot_count: int = 0
        self._current_thermal_jpg: Optional[str] = None
        self._current_visual_jpg: Optional[str] = None
        # display-only data (백그라운드 작업에서 UI로 전달)
        self._display_max: float = 0.0
        self._display_mean: float = 0.0
        self._display_hot_95: float = 0.0

        self._processed_bases: set = set()
        self._processed_bases_lock = threading.Lock()
        self._capture_mode = "both"  # tkinter var 대신 캐시 (백그라운드 스레드 안전)
        self._camera_connected = False
        # GUI-UPDATE: 장시간 작업의 중복 실행을 막는 상태 플래그.
        self._connection_check_running = False
        self._system_ready = False
        self._roi_running = False
        self._calib_running = False
        self._check_running = False
        self._metadata_running = False
        self._cleanup_running = False
        self._photo_ref: Optional[ImageTk.PhotoImage] = None

        self._build_ui()
        self._check_camera_connection()
        self._prime_processed_cache()

    # ════════════════════════════════════════════════════════════
    # UI 구성
    # ════════════════════════════════════════════════════════════
    def _build_ui(self):
        # ── Section 1: 환경 설정 ──────────────────────────────
        self._build_env_section()

        # ── Section 2: 감지 화면 (좌: 이미지, 우: 제어) ──────
        det_frame = ttk.LabelFrame(self.root, text="감지 화면", padding=10)
        det_frame.pack(fill="both", expand=True, padx=10, pady=(0, 5))

        det_paned = ttk.PanedWindow(det_frame, orient="horizontal")
        det_paned.pack(fill="both", expand=True)

        # Left panel: image
        img_frame = ttk.Frame(det_paned, width=580)
        det_paned.add(img_frame, weight=1)

        self._image_label = ttk.Label(img_frame, anchor="center", background="#1a1a1a")
        self._image_label.pack(fill="both", expand=True)

        # Right panel: controls
        ctrl_frame = ttk.Frame(det_paned, width=220)
        det_paned.add(ctrl_frame, weight=0)

        # View toggle
        view_frame = ttk.LabelFrame(ctrl_frame, text="View Mode", padding=8)
        view_frame.pack(fill="x", padx=5, pady=(5, 10))

        self._view_var = tk.StringVar(value="thermal")
        self._thermal_radio = ttk.Radiobutton(
            view_frame, text="Thermal", variable=self._view_var,
            value="thermal", command=self._on_view_changed,
        )
        self._thermal_radio.pack(anchor="w")
        self._visual_radio = ttk.Radiobutton(
            view_frame, text="Visual", variable=self._view_var,
            value="visual", command=self._on_view_changed,
        )
        self._visual_radio.pack(anchor="w")

        # Detection info
        info_frame = ttk.LabelFrame(ctrl_frame, text="Detection Info", padding=8)
        info_frame.pack(fill="x", padx=5, pady=(0, 10))

        self._status_label = ttk.Label(info_frame, text="Status: Normal",
                                       font=("", 11, "bold"))
        self._status_label.pack(anchor="w", pady=(0, 4))

        self._max_temp_label = ttk.Label(info_frame, text="Max: -- °C", font=("", 10))
        self._max_temp_label.pack(anchor="w")

        self._mean_temp_label = ttk.Label(info_frame, text="Mean: -- °C", font=("", 10))
        self._mean_temp_label.pack(anchor="w")

        self._hot_temp_label = ttk.Label(info_frame, text="95th: -- °C", font=("", 10))
        self._hot_temp_label.pack(anchor="w")

        self._hotspot_label = ttk.Label(info_frame, text="Hotspots: 0", font=("", 10))
        self._hotspot_label.pack(anchor="w")

        # Tool buttons
        tool_frame = ttk.LabelFrame(ctrl_frame, text="Tools", padding=8)
        tool_frame.pack(fill="x", padx=5)

        # GUI-UPDATE: 계획서에 명시된 데이터 운영 버튼 2종.
        self._check_btn = ttk.Button(tool_frame, text="Check Dataset",
                                     command=self._run_dataset_check)
        self._check_btn.pack(fill="x", pady=(0, 6))

        self._metadata_btn = ttk.Button(tool_frame, text="Generate Metadata",
                                        command=self._run_metadata_generation)
        self._metadata_btn.pack(fill="x", pady=(0, 6))

        self._cleanup_btn = ttk.Button(tool_frame, text="Cleanup Dataset",
                                       command=self._run_cleanup)
        self._cleanup_btn.pack(fill="x", pady=(0, 10))

        self._roi_btn = ttk.Button(tool_frame, text="Set ROI",
                                   command=self._launch_roi_selector)
        self._roi_btn.pack(fill="x", pady=(0, 6))

        self._calib_btn = ttk.Button(tool_frame, text="Calibrate",
                                     command=self._launch_calibration)
        self._calib_btn.pack(fill="x")

        # ── Section 3: 로그 화면 ──────────────────────────────
        self._build_log_section()

    def _build_env_section(self):
        env_frame = ttk.LabelFrame(self.root, text="환경 설정", padding=10)
        env_frame.pack(fill="x", padx=10, pady=(10, 5))

        # Row 0: Camera IP + connection status
        ttk.Label(env_frame, text="Camera IP:").grid(row=0, column=0, sticky="w", padx=(0, 5))
        self._cam_ip_var = tk.StringVar(value=self._config.camera.ip)
        cam_ip_entry = ttk.Entry(env_frame, textvariable=self._cam_ip_var, width=18)
        cam_ip_entry.grid(row=0, column=1, sticky="w")
        cam_ip_entry.bind("<Return>", lambda _event: self._check_camera_connection())

        self._cam_status_label = ttk.Label(env_frame, text="○ Disconnected",
                                           foreground="#888888")
        self._cam_status_label.grid(row=0, column=2, sticky="w", padx=(10, 0))

        self._connection_btn = ttk.Button(
            env_frame, text="Check Connection",
            command=self._check_camera_connection,
        )
        self._connection_btn.grid(row=0, column=3, sticky="w", padx=(10, 0))

        # Row 1: Dataset dir + Browse
        ttk.Label(env_frame, text="Dataset:").grid(row=1, column=0, sticky="w",
                                                    padx=(0, 5), pady=(6, 0))
        self._dir_var = tk.StringVar(value=self._config.paths.dataset_dir)
        dir_entry = ttk.Entry(env_frame, textvariable=self._dir_var, width=40, state="readonly")
        dir_entry.grid(row=1, column=1, sticky="w", pady=(6, 0))

        browse_btn = ttk.Button(env_frame, text="Browse...", command=self._change_dataset_dir)
        browse_btn.grid(row=1, column=2, sticky="w", padx=(10, 0), pady=(6, 0))

        # GUI-UPDATE: 발표자료 입력 설정과 맞추기 위해 Mode를 추가했다.
        # Row 2: Interval + Mode + Start/Stop
        ttk.Label(env_frame, text="Interval (s):").grid(row=2, column=0, sticky="w",
                                                         padx=(0, 5), pady=(6, 0))
        self._interval_var = tk.StringVar(value=str(self._config.camera.capture_interval_sec))
        interval_entry = ttk.Entry(env_frame, textvariable=self._interval_var, width=6)
        interval_entry.grid(row=2, column=1, sticky="w", pady=(6, 0))

        ttk.Label(env_frame, text="Mode:").grid(
            row=2, column=2, sticky="e", padx=(10, 5), pady=(6, 0))
        self._mode_var = tk.StringVar(value=self._config.tools.mode)
        self._mode_combo = ttk.Combobox(
            env_frame, textvariable=self._mode_var,
            values=("both", "thermal"), state="readonly", width=9,
        )
        self._mode_combo.grid(row=2, column=3, sticky="w", pady=(6, 0))

        self._monitor_btn = ttk.Button(env_frame, text="Start Monitoring",
                                       command=self._toggle_monitoring)
        self._monitor_btn.grid(row=2, column=4, sticky="w", padx=(10, 0), pady=(6, 0))

        # GUI-UPDATE: NPY 복구에 필요한 ExifTool을 GUI에서 직접 지정한다.
        # Row 3: ExifTool path
        ttk.Label(env_frame, text="ExifTool:").grid(
            row=3, column=0, sticky="w", padx=(0, 5), pady=(6, 0))
        self._exiftool_var = tk.StringVar(
            value=self._config.tools.exiftool_path or "Auto-detect from PATH")
        exiftool_entry = ttk.Entry(
            env_frame, textvariable=self._exiftool_var,
            width=40, state="readonly",
        )
        exiftool_entry.grid(
            row=3, column=1, columnspan=2, sticky="we", pady=(6, 0))
        ttk.Button(
            env_frame, text="Browse...", command=self._change_exiftool_path,
        ).grid(row=3, column=3, sticky="w", padx=(10, 0), pady=(6, 0))

        # GUI-UPDATE: 시스템 준비 상태와 준비되지 않은 이유를 분리 표시한다.
        # Row 4: System readiness
        ttk.Label(env_frame, text="System Status:").grid(
            row=4, column=0, sticky="w", padx=(0, 5), pady=(8, 0))
        self._system_status_label = ttk.Label(
            env_frame, text="● Not Ready", foreground="#888888",
            font=("", 10, "bold"),
        )
        self._system_status_label.grid(
            row=4, column=1, sticky="w", pady=(8, 0))

        self._readiness_reason_var = tk.StringVar(value="Checking system requirements...")
        self._readiness_reason_label = ttk.Label(
            env_frame, textvariable=self._readiness_reason_var,
            foreground="#666666",
        )
        self._readiness_reason_label.grid(
            row=4, column=2, columnspan=3, sticky="w", padx=(10, 0), pady=(8, 0))

        # Status bar
        self._status_bar_var = tk.StringVar(value="Checking system readiness...")
        status_bar = ttk.Label(self.root, textvariable=self._status_bar_var,
                               relief="sunken", anchor="w", padding=3)
        status_bar.pack(fill="x", padx=10, pady=(0, 5))

    def _build_log_section(self):
        log_frame = ttk.LabelFrame(self.root, text="로그 화면", padding=5)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # GUI-UPDATE: 감지 결과와 운영·오류 로그가 섞이지 않도록 탭을 분리한다.
        notebook = ttk.Notebook(log_frame)
        notebook.pack(fill="both", expand=True)

        detection_tab = ttk.Frame(notebook)
        activity_tab = ttk.Frame(notebook)
        notebook.add(detection_tab, text="Detection Log")
        notebook.add(activity_tab, text="Activity Log")

        columns = ("time", "location", "temperature", "alert", "notified")
        self._log_tree = ttk.Treeview(
            detection_tab, columns=columns, show="headings", height=12)

        self._log_tree.heading("time", text="Detection Time")
        self._log_tree.heading("location", text="Location")
        self._log_tree.heading("temperature", text="Temperature")
        self._log_tree.heading("alert", text="Alert Level")
        self._log_tree.heading("notified", text="Notified")

        self._log_tree.column("time", width=140, minwidth=100)
        self._log_tree.column("location", width=130, minwidth=80)
        self._log_tree.column("temperature", width=100, minwidth=80)
        self._log_tree.column("alert", width=100, minwidth=80)
        self._log_tree.column("notified", width=80, minwidth=60)

        self._log_tree.tag_configure("Critical", foreground="#ff0000",
                                     font=("Consolas", 9, "bold"))
        self._log_tree.tag_configure("Warning", foreground="#ff8800")
        self._log_tree.tag_configure("Normal", foreground="#888888")

        scrollbar = ttk.Scrollbar(detection_tab, orient="vertical",
                                  command=self._log_tree.yview)
        self._log_tree.configure(yscrollcommand=scrollbar.set)

        self._log_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._activity_text = tk.Text(
            activity_tab, height=12, wrap="word", state="disabled",
            font=("Consolas", 9), background="#111111", foreground="#dddddd",
        )
        activity_scrollbar = ttk.Scrollbar(
            activity_tab, orient="vertical", command=self._activity_text.yview)
        self._activity_text.configure(yscrollcommand=activity_scrollbar.set)
        self._activity_text.pack(side="left", fill="both", expand=True)
        activity_scrollbar.pack(side="right", fill="y")

    # ════════════════════════════════════════════════════════════
    # 환경 설정 메서드
    # ════════════════════════════════════════════════════════════
    def _check_camera_connection(self):
        # GUI-UPDATE: HTTP timeout 동안 GUI가 멈추지 않도록 별도 스레드에서 검사한다.
        ip = self._cam_ip_var.get().strip()
        if self._connection_check_running:
            return
        if not ip:
            self._finish_camera_connection_check(False)
            return

        self._connection_check_running = True
        self._cam_status_label.configure(text="◌ Checking...", foreground="#666666")
        self._connection_btn.configure(state="disabled")

        def _run_check():
            connected = False
            try:
                r = requests.get(f"http://{ip}/api/image/current?imgformat=JPEG",
                                 timeout=5)
                connected = r.status_code == 200
            except Exception:
                connected = False
            self.root.after(
                0, lambda: self._finish_camera_connection_check(connected))

        threading.Thread(target=_run_check, daemon=True).start()

    def _finish_camera_connection_check(self, connected: bool):
        self._connection_check_running = False
        self._camera_connected = connected
        self._connection_btn.configure(state="normal")
        if self._camera_connected:
            self._cam_status_label.configure(text="● Connected", foreground="#00aa00")
        else:
            self._cam_status_label.configure(text="○ Disconnected", foreground="#888888")
        self._append_activity_log(
            f"Camera {self._cam_ip_var.get().strip()}: "
            f"{'connected' if connected else 'disconnected'}")
        self._refresh_readiness()

    def _dataset_is_ready(self) -> bool:
        # GUI-UPDATE: Dataset이 실제로 생성·기록 가능한지 Ready 조건으로 확인한다.
        dataset_dir = self._config.paths.dataset_dir
        if not dataset_dir:
            return False
        try:
            os.makedirs(dataset_dir, exist_ok=True)
            return os.path.isdir(dataset_dir) and os.access(dataset_dir, os.W_OK)
        except OSError:
            return False

    def _exiftool_is_available(self) -> bool:
        # GUI-UPDATE: 설정 경로 → PATH → 번들 실행 파일 순서로 확인한다.
        configured = self._config.tools.exiftool_path.strip()
        if configured:
            return os.path.isfile(configured)

        if shutil.which("exiftool"):
            return True

        try:
            from ..capture.thermal_utils import EXIFTOOL
            return os.path.isabs(EXIFTOOL) and os.path.isfile(EXIFTOOL)
        except Exception:
            return False

    def _roi_is_valid(self) -> bool:
        roi = self._config.roi
        values = (roi.x1, roi.y1, roi.x2, roi.y2)
        return None not in values and roi.x1 < roi.x2 and roi.y1 < roi.y2

    def _has_calibration_pair(self) -> bool:
        dataset_dir = self._config.paths.dataset_dir
        if not os.path.isdir(dataset_dir):
            return False
        for thermal_jpg in glob.glob(os.path.join(dataset_dir, "*.jpg")):
            if "_visual" in os.path.basename(thermal_jpg):
                continue
            visual_jpg = thermal_jpg.replace(".jpg", "_visual.jpg")
            if os.path.isfile(visual_jpg):
                return True
        return False

    def _refresh_readiness(self) -> bool:
        # GUI-UPDATE: 운영 준비 조건을 한 곳에서 판정하고 상태 문구를 갱신한다.
        reasons = []
        if not self._camera_connected:
            reasons.append("Camera disconnected")
        if not self._dataset_is_ready():
            reasons.append("Dataset unavailable")
        if not self._exiftool_is_available():
            reasons.append("ExifTool not found")
        if not self._roi_is_valid():
            reasons.append("ROI not configured")

        self._system_ready = not reasons

        if self._running and self._system_ready:
            self._system_status_label.configure(
                text="● Monitoring", foreground="#0066cc")
            self._readiness_reason_var.set("Capture and analysis are running")
            self._monitor_btn.configure(state="normal")
        elif self._running:
            self._system_status_label.configure(text="● Error", foreground="#cc0000")
            self._readiness_reason_var.set(" / ".join(reasons))
            self._monitor_btn.configure(state="normal")
        elif self._system_ready:
            self._system_status_label.configure(text="● Ready", foreground="#00aa00")
            self._readiness_reason_var.set("All required checks passed")
            self._monitor_btn.configure(state="normal")
            if self._status_bar_var.get() == "Checking system readiness...":
                self._status_bar_var.set("System ready.")
        else:
            self._system_status_label.configure(text="● Not Ready", foreground="#888888")
            self._readiness_reason_var.set(" / ".join(reasons))
            self._monitor_btn.configure(state="normal")
            if self._status_bar_var.get() == "Checking system readiness...":
                self._status_bar_var.set(f"Not ready: {' / '.join(reasons)}")

        return self._system_ready

    def _change_dataset_dir(self):
        new_dir = filedialog.askdirectory(
            initialdir=os.path.abspath(self._config.paths.dataset_dir),
            title="Select Dataset Directory"
        )
        if new_dir:
            self._config.paths.dataset_dir = new_dir
            self._config.paths.overlay_dir = os.path.join(new_dir, "overlay")
            save_config(self._config)
            self._dir_var.set(new_dir)
            with self._processed_bases_lock:
                self._processed_bases.clear()
            self._prime_processed_cache()
            self._log_to_status(f"Dataset directory changed: {new_dir}")
            self._refresh_readiness()

    def _change_exiftool_path(self):
        # GUI-UPDATE: 선택 경로를 config.json에 저장해 다음 실행에도 재사용한다.
        new_path = filedialog.askopenfilename(
            title="Select ExifTool executable",
            filetypes=(
                ("Executable", "*.exe" if sys.platform == "win32" else "*"),
                ("All files", "*.*"),
            ),
        )
        if not new_path:
            return
        self._config.tools.exiftool_path = new_path
        save_config(self._config)
        self._exiftool_var.set(new_path)
        self._append_activity_log(f"ExifTool path changed: {new_path}")
        self._log_to_status("ExifTool path saved.")
        self._refresh_readiness()

    def _log_to_status(self, msg: str):
        self.root.after(0, lambda: self._status_bar_var.set(msg))

    def _append_activity_log(self, msg: str):
        """GUI-UPDATE: 백그라운드 작업 로그를 tkinter 메인 스레드에서 표시."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {msg.rstrip()}\n"

        def _insert():
            if not self.root.winfo_exists():
                return
            self._activity_text.configure(state="normal")
            self._activity_text.insert("end", line)
            line_count = int(self._activity_text.index("end-1c").split(".")[0])
            if line_count > 1000:
                self._activity_text.delete("1.0", f"{line_count - 1000}.0")
            self._activity_text.configure(state="disabled")
            self._activity_text.see("end")

        self.root.after(0, _insert)

    def _run_dataset_check(self):
        # GUI-UPDATE: 무결성 검사는 파일 작업이므로 GUI 밖의 스레드에서 수행한다.
        if self._check_running:
            self._log_to_status("Dataset check is already running.")
            return

        self._check_running = True
        self._check_btn.configure(state="disabled")
        self._log_to_status("Checking dataset integrity...")
        self._append_activity_log("=== Dataset check started ===")
        save_dir = self._config.paths.dataset_dir

        def _run():
            try:
                result = run_check(
                    save_dir=save_dir,
                    log_callback=self._append_activity_log,
                )
                self.root.after(0, lambda: self._finish_dataset_check(result, None))
            except Exception as exc:
                self.root.after(0, lambda exc=exc: self._finish_dataset_check(None, exc))

        threading.Thread(target=_run, daemon=True).start()

    def _finish_dataset_check(
        self,
        result: Optional[CheckResult],
        error: Optional[Exception],
    ):
        self._check_running = False
        self._check_btn.configure(state="normal")
        if error is not None:
            message = f"Dataset check failed: {error}"
            self._append_activity_log(message)
            self._log_to_status(message)
            return

        message = (
            f"Dataset check complete — JPG {result.total_jpg}, "
            f"NPY {result.total_npy}, Pairs {result.paired}, "
            f"Fixed {result.fixed}, Failed {result.failed}, "
            f"Removed {result.removed}"
        )
        self._append_activity_log(message)
        self._log_to_status(message)

    def _run_metadata_generation(self):
        # GUI-UPDATE: metadata.csv 생성 중에도 영상·버튼 이벤트를 유지한다.
        if self._metadata_running:
            self._log_to_status("Metadata generation is already running.")
            return

        self._metadata_running = True
        self._metadata_btn.configure(state="disabled")
        self._log_to_status("Generating metadata.csv...")
        self._append_activity_log("=== Metadata generation started ===")
        save_dir = self._config.paths.dataset_dir

        def _run():
            try:
                result = run_metadata(
                    save_dir=save_dir,
                    log_callback=self._append_activity_log,
                )
                self.root.after(0, lambda: self._finish_metadata_generation(result, None))
            except Exception as exc:
                self.root.after(
                    0, lambda exc=exc: self._finish_metadata_generation(None, exc))

        threading.Thread(target=_run, daemon=True).start()

    def _finish_metadata_generation(
        self,
        result: Optional[MetadataResult],
        error: Optional[Exception],
    ):
        self._metadata_running = False
        self._metadata_btn.configure(state="normal")
        if error is not None:
            message = f"Metadata generation failed: {error}"
            self._append_activity_log(message)
            self._log_to_status(message)
            return

        message = (
            f"Metadata complete — Pairs {result.total_pairs}, "
            f"Existing {result.existing}, Added {result.new}"
        )
        self._append_activity_log(message)
        self._log_to_status(message)

    def _maybe_run_cleanup_in_background(self):
        """주기적 정리를 조용히 백그라운드에서 수행 (버튼과 무관)."""
        if self._cleanup_running:
            return
        self._cleanup_running = True
        save_dir = self._config.paths.dataset_dir

        def _run():
            try:
                result = run_cleanup(save_dir=save_dir, log_callback=self._append_activity_log)
                if result.removed_pairs > 0 or result.freed_bytes > 0:
                    freed_mb = result.freed_bytes / (1024 * 1024)
                    self._append_activity_log(
                        f"Background cleanup — removed {result.removed_pairs} pairs, "
                        f"freed {freed_mb:.1f} MB")
            except Exception:
                pass
            finally:
                self._cleanup_running = False

        threading.Thread(target=_run, daemon=True).start()

    def _run_cleanup(self):
        """GUI-UPDATE: 불필요 데이터 정리를 백그라운드에서 실행한다."""
        if self._cleanup_running:
            self._log_to_status("Cleanup is already running.")
            return

        self._cleanup_running = True
        self._cleanup_btn.configure(state="disabled")
        self._log_to_status("Cleaning up old datasets...")
        self._append_activity_log("=== Dataset cleanup started ===")
        save_dir = self._config.paths.dataset_dir

        def _run():
            try:
                result = run_cleanup(
                    save_dir=save_dir,
                    log_callback=self._append_activity_log,
                )
                self.root.after(0, lambda: self._finish_cleanup(result, None))
            except Exception as exc:
                self.root.after(0, lambda exc=exc: self._finish_cleanup(None, exc))

        threading.Thread(target=_run, daemon=True).start()

    def _finish_cleanup(
        self,
        result: Optional[CleanupResult],
        error: Optional[Exception],
    ):
        self._cleanup_running = False
        self._cleanup_btn.configure(state="normal")
        if error is not None:
            message = f"Cleanup failed: {error}"
            self._append_activity_log(message)
            self._log_to_status(message)
            return

        freed_mb = result.freed_bytes / (1024 * 1024)
        message = (
            f"Cleanup complete — Normal pairs removed: {result.removed_pairs}, "
            f"Alarm history preserved: {result.preserved_alarms}, "
            f"Orphan NPY: {result.removed_orphan_npy}, "
            f"Orphan JPG: {result.removed_orphan_jpg}, "
            f"Orphan overlay: {result.removed_overlay}, "
            f"Freed: {freed_mb:.1f} MB"
        )
        self._append_activity_log(message)
        self._log_to_status(message)

    # ════════════════════════════════════════════════════════════
    # 모니터링 시작/중지
    # ════════════════════════════════════════════════════════════
    def _toggle_monitoring(self):
        if self._running:
            self._stop_monitoring()
        else:
            self._start_monitoring()

    def _start_monitoring(self):
        # GUI-UPDATE: Ready 검사 후 화면에서 선택한 Mode를 캡처 세션에 전달한다.
        if not self._refresh_readiness():
            self._log_to_status(
                f"Not ready: {self._readiness_reason_var.get()}")
            return

        cam_ip = self._cam_ip_var.get().strip()
        mode = self._mode_var.get().strip()
        if mode not in ("both", "thermal"):
            self._log_to_status("Invalid capture mode.")
            return
        try:
            interval = float(self._interval_var.get())
            if interval <= 0:
                raise ValueError
        except ValueError:
            self._log_to_status("Invalid interval value. Must be > 0.")
            return

        self._config.camera.ip = cam_ip
        self._config.camera.capture_interval_sec = interval
        self._config.tools.mode = mode
        self._capture_mode = mode
        save_config(self._config)

        self._monitor_state = MonitorState()
        with self._processed_bases_lock:
            self._processed_bases.clear()
        self._prime_processed_cache()
        self._running = True
        self._tick_count = 0
        self._analysis_generation = 0

        # 프로브 콜백: 캡처 대기 중 1초마다 경량 Thermal 체크
        roi = self._config.roi
        baseline = roi.baseline_temp
        warning_delta = roi.warning_delta

        def _probe_callback(max_temp: float) -> bool:
            if max_temp >= baseline + warning_delta:
                self._capture_session.set_warning_mode(True)
                return True
            return False

        self._capture_session = CaptureSession(
            cam_ip=cam_ip,
            mode=mode,
            interval=interval,
            save_dir=self._config.paths.dataset_dir,
            # GUI-UPDATE: 캡처 성공·실패를 콘솔 대신 Activity Log에도 표시한다.
            log_callback=self._append_activity_log,
            probe_callback=_probe_callback,
        )
        self._capture_session.start()

        self._monitor_btn.configure(text="Stop Monitoring")
        self._log_to_status(
            f"Monitoring started — {cam_ip}, mode={mode}, interval={interval}s")
        self._append_activity_log(
            f"Monitoring started: camera={cam_ip}, mode={mode}, interval={interval}s")
        self._refresh_readiness()
        self._monitoring_tick()

    def _stop_monitoring(self):
        self._running = False
        if self._scan_timer_id:
            self.root.after_cancel(self._scan_timer_id)
            self._scan_timer_id = None
        if self._capture_session:
            self._capture_session.request_stop()
        self._monitor_btn.configure(text="Start Monitoring")
        self._log_to_status("Monitoring stop requested...")
        self._append_activity_log("Monitoring stop requested")
        self._refresh_readiness()

    # ════════════════════════════════════════════════════════════
    # 모니터링 루프 (root.after)
    # ════════════════════════════════════════════════════════════
    def _monitoring_tick(self):
        if not self._running:
            return
        try:
            if self._tick_count % 10 == 0:
                self._check_camera_connection()

            if self._tick_count > 0 and self._tick_count % 1800 == 0:
                self._maybe_run_cleanup_in_background()

            # 분석 작업 스케줄링 (UI 스레드에서는 요청만)
            self._schedule_analysis()

            self._refresh_display()
            self._refresh_readiness()
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._log_to_status(f"Tick error: {e}")

        self._tick_count += 1
        interval_ms = int(self._config.monitoring.process_interval_sec * 1000)
        self._scan_timer_id = self.root.after(interval_ms, self._monitoring_tick)

    # ════════════════════════════════════════════════════════════
    # 분석 작업 스케줄링 (백그라운드)
    # ════════════════════════════════════════════════════════════
    def _schedule_analysis(self):
        """UI 스레드에서 분석 요청만 제출하고 즉시 반환."""
        if not self._running:
            return
        if self._analysis_running:
            self._analysis_pending = True
            return

        self._analysis_running = True
        self._analysis_pending = False
        gen = self._analysis_generation + 1
        self._analysis_generation = gen

        self._analysis_executor.submit(self._run_analysis_worker, gen)

    def _run_analysis_worker(self, generation: int):
        """백그라운드 스레드에서 실행: 파일 스캔 + NPY 생성 + 분석 + 오버레이."""
        try:
            new_pairs = self._scan_new_pairs()
            results = []
            for pair in new_pairs:
                try:
                    result = self._process_pair_to_dict(pair)
                    results.append((result, pair["base"]))
                except Exception:
                    import traceback
                    traceback.print_exc()
                    self._append_activity_log(
                        f"Analysis failed for {pair.get('base', 'unknown')}: "
                        f"{traceback.format_exc()[-200:]}")

            if results:
                self.root.after(0, lambda: self._apply_analysis_result(results, generation))
            else:
                self.root.after(0, lambda: self._finish_analysis_worker(generation))
        except Exception:
            import traceback
            traceback.print_exc()
            self.root.after(0, lambda: self._finish_analysis_worker(generation))

    def _process_pair_to_dict(self, pair: dict) -> dict:
        """_process_pair 결과를 UI-safe dict로 변환 (백그라운드 스레드용)."""
        roi_config = load_roi_config()
        roi_results = extract_all_rois_from_npy(pair["npy"], roi_config)
        roi_result = max(roi_results, key=lambda r: r.hot_temp_95
                         if r.hot_temp_95 is not None else -1)

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

        new_status, do_alarm = evaluate_with_state(
            hot_temp=roi_result.hot_temp_95,
            max_temp=roi_result.max_temp,
            mean_temp=roi_result.mean_temp,
            baseline=roi_config.baseline_temp,
            warning_delta=roi_config.warning_delta,
            critical_delta=roi_config.critical_delta,
            state=self._monitor_state,
            over_temp_pixels=roi_result.over_temp_pixels,
            max_hotspot_size=roi_result.max_hotspot_size,
        )

        overlay = create_overlay(
            thermal_jpg_path=pair["thermal_jpg"],
            visual_jpg_path=pair["visual_jpg"],
            roi_bounds=roi_result.roi_bounds,
            max_temp=roi_result.max_temp,
            mean_temp=roi_result.mean_temp,
            hot_temp=roi_result.hot_temp_95,
            status=new_status.value,
            hotspot_centroids=roi_result.hotspot_centroids,
            roi_bounds_list=_get_roi_bounds_list(roi_config),
            roi_names=[r.roi_name for r in roi_results] if len(roi_results) > 1 else None,
        )

        visual_overlay = None
        if pair["visual_jpg"] and os.path.isfile(pair["visual_jpg"]):
            visual_overlay = cv2.imread(pair["visual_jpg"])
            if visual_overlay is not None:
                visual_overlay = cv2.resize(
                    visual_overlay, (overlay.shape[1], overlay.shape[0]))

        if roi_result.hotspot_centroids:
            cx, cy, _ = roi_result.hotspot_centroids[0]
            loc_str = f"({cx}, {cy})"
        else:
            x1, y1, x2, y2 = roi_result.roi_bounds
            loc_str = f"ROI({(x1 + x2) // 2}, {(y1 + y2) // 2})"

        return {
            "overlay": overlay,
            "visual_overlay": visual_overlay,
            "thermal_jpg": pair["thermal_jpg"],
            "visual_jpg": pair["visual_jpg"],
            "max_temp": roi_result.max_temp,
            "mean_temp": roi_result.mean_temp,
            "hot_temp_95": roi_result.hot_temp_95,
            "status": new_status.value,
            "hotspot_count": len(roi_result.hotspot_centroids),
            "loc_str": loc_str,
            "do_alarm": do_alarm,
            "hotspot_centroids": roi_result.hotspot_centroids,
            "roi_bounds": roi_result.roi_bounds,
            "base": pair["base"],
        }

    def _apply_analysis_result(self, results: list, generation: int):
        """UI 스레드에서 실행: 분석 결과를 화면에 반영."""
        if not self._running:
            return
        if generation < self._analysis_generation:
            return  # 오래된 결과는 폐기

        for result_dict, base in results:
            self._mark_processed(base)

            self._monitor_state.status = Status(result_dict["status"])

            self._current_overlay = result_dict["overlay"]
            self._current_visual_overlay = result_dict["visual_overlay"]
            self._current_thermal_jpg = result_dict["thermal_jpg"]
            self._current_visual_jpg = result_dict["visual_jpg"]
            self._current_status = result_dict["status"]
            self._current_hotspot_count = result_dict["hotspot_count"]
            self._display_max = result_dict["max_temp"]
            self._display_mean = result_dict["mean_temp"]
            self._display_hot_95 = result_dict["hot_temp_95"]

            was_notified = False
            if result_dict["do_alarm"]:
                was_notified = True  # 알림 발송 시도 즉시 로그에는 Yes로 표시
                threading.Thread(
                    target=self._try_notify_from_result, args=(result_dict,),
                    daemon=True,
                ).start()

            self._add_log_row(
                datetime.now().strftime("%H:%M:%S"),
                result_dict["loc_str"],
                result_dict["max_temp"],
                result_dict["status"],
                was_notified,
            )

            if result_dict["do_alarm"]:
                self._log_to_status(
                    f"Alert: {result_dict['status']} | Max: {result_dict['max_temp']:.1f}°C")

        self._refresh_display()
        self._finish_analysis_worker(generation)

    def _finish_analysis_worker(self, generation: int):
        self._analysis_running = False
        if self._analysis_pending:
            self.root.after(0, self._schedule_analysis)

    def _try_notify_from_result(self, result_dict: dict) -> bool:
        try:
            overlay = result_dict["overlay"]
            if overlay is None:
                return False
            overlay_dir = self._config.paths.overlay_dir
            os.makedirs(overlay_dir, exist_ok=True)
            overlay_path = os.path.join(
                overlay_dir,
                f"{datetime.now().strftime('%Y%m%d%H%M%S')}_overlay.jpg")
            cv2.imwrite(overlay_path, overlay)

            success = send_alarm(
                image_path=overlay_path,
                temp=result_dict["hot_temp_95"],
                status=result_dict["status"],
                robot_id=self._config.identity.robot_id,
            )
            if success:
                self._monitor_state.last_alarm_time = time.time()
            return success
        except RuntimeError:
            self._append_activity_log("Telegram skipped: not configured")
            return False
        except Exception as exc:
            self._append_activity_log(f"Telegram error: {exc}")
            return False

    def _scan_all_paired_bases(self) -> set:
        dataset_dir = self._config.paths.dataset_dir
        if not os.path.isdir(dataset_dir):
            return set()
        try:
            files = os.listdir(dataset_dir)
        except OSError:
            return set()
        thermal_jpgs = {f.replace(".jpg", ""): f for f in files
                        if f.endswith(".jpg") and "_visual" not in f}
        visual_jpgs = {f.replace("_visual.jpg", ""): f for f in files
                       if f.endswith("_visual.jpg")}
        # GUI-UPDATE: thermal 모드는 RGB 파일 없이 Thermal JPG만으로 처리한다.
        if self._mode_var.get() == "thermal":
            return set(thermal_jpgs.keys())
        return set(thermal_jpgs.keys()) & set(visual_jpgs.keys())

    def _prime_processed_cache(self):
        existing = self._scan_all_paired_bases()
        with self._processed_bases_lock:
            self._processed_bases = existing

    def _scan_new_pairs(self) -> list[dict]:
        dataset_dir = self._config.paths.dataset_dir
        if not os.path.isdir(dataset_dir):
            return []
        try:
            files = os.listdir(dataset_dir)
        except OSError:
            return []

        thermal_jpgs = {f.replace(".jpg", ""): f for f in files
                        if f.endswith(".jpg") and "_visual" not in f}
        npys = {f.replace("_thermal.npy", ""): f for f in files
                if f.endswith("_thermal.npy")}
        visual_jpgs = {f.replace("_visual.jpg", ""): f for f in files
                       if f.endswith("_visual.jpg")}

        mode = self._capture_mode
        if mode == "thermal":
            bases = sorted(thermal_jpgs.keys())
        else:
            bases = sorted(set(thermal_jpgs.keys()) & set(visual_jpgs.keys()))
        new_pairs = []
        for base in bases:
            with self._processed_bases_lock:
                if base in self._processed_bases:
                    continue
            npy_path = os.path.join(dataset_dir, base + "_thermal.npy")
            if base not in npys:
                try:
                    from ..capture.thermal_utils import extract_from_jpeg
                    jpg_path = os.path.join(dataset_dir, thermal_jpgs[base])
                    thermal, _ = extract_from_jpeg(jpg_path)
                    np.save(npy_path, thermal)
                except Exception as exc:
                    self._append_activity_log(
                        f"NPY extraction failed for {base}: {exc}")
                    continue
            visual_name = visual_jpgs.get(base)
            new_pairs.append({
                "base": base,
                "thermal_jpg": os.path.join(dataset_dir, thermal_jpgs[base]),
                "visual_jpg": (
                    os.path.join(dataset_dir, visual_name) if visual_name else ""),
                "npy": npy_path,
            })
        return new_pairs

    def _mark_processed(self, base: str):
        with self._processed_bases_lock:
            self._processed_bases.add(base)
            max_cache = self._config.monitoring.max_processed_cache
            if len(self._processed_bases) > max_cache:
                retain = max_cache // 2
                self._processed_bases = set(sorted(self._processed_bases)[-retain:])

    # ════════════════════════════════════════════════════════════
    # [DEAD CODE] 기존 쌍 처리 (→ _process_pair_to_dict + _apply_analysis_result 로 대체됨)
    # ════════════════════════════════════════════════════════════
    # def _process_pair(self, pair: dict):
    #     try:
    #         roi_config = load_roi_config()
    #         roi_results = extract_all_rois_from_npy(pair["npy"], roi_config)
    #         roi_result = max(roi_results, key=lambda r: r.hot_temp_95)
    #         all_hotspots = []
    #         for rr in roi_results:
    #             all_hotspots.extend(rr.hotspot_centroids)
    #         unique_hotspots = []
    #         for spot in all_hotspots:
    #             is_dup = False
    #             for u in unique_hotspots:
    #                 if abs(spot[0] - u[0]) < 5 and abs(spot[1] - u[1]) < 5:
    #                     if spot[2] > u[2]:
    #                         unique_hotspots[unique_hotspots.index(u)] = spot
    #                     is_dup = True
    #                     break
    #             if not is_dup:
    #                 unique_hotspots.append(spot)
    #         roi_result.hotspot_centroids = unique_hotspots
    #         new_status, do_alarm = evaluate_with_state(
    #             hot_temp=roi_result.hot_temp_95,
    #             max_temp=roi_result.max_temp,
    #             mean_temp=roi_result.mean_temp,
    #             baseline=roi_config.baseline_temp,
    #             warning_delta=roi_config.warning_delta,
    #             critical_delta=roi_config.critical_delta,
    #             state=self._monitor_state,
    #             over_temp_pixels=roi_result.over_temp_pixels,
    #             max_hotspot_size=roi_result.max_hotspot_size,
    #         )
    #         self._monitor_state.status = new_status
    #         overlay = create_overlay(
    #             thermal_jpg_path=pair["thermal_jpg"],
    #             visual_jpg_path=pair["visual_jpg"],
    #             roi_bounds=roi_result.roi_bounds,
    #             max_temp=roi_result.max_temp,
    #             mean_temp=roi_result.mean_temp,
    #             hot_temp=roi_result.hot_temp_95,
    #             status=new_status.value,
    #             hotspot_centroids=roi_result.hotspot_centroids,
    #             roi_bounds_list=_get_roi_bounds_list(roi_config),
    #             roi_names=[r.roi_name for r in roi_results] if len(roi_results) > 1 else None,
    #         )
    #         self._current_overlay = overlay
    #         self._current_visual_overlay = (
    #             cv2.imread(pair["visual_jpg"]) if pair["visual_jpg"] else None)
    #         if self._current_visual_overlay is not None:
    #             self._current_visual_overlay = cv2.resize(
    #                 self._current_visual_overlay, (overlay.shape[1], overlay.shape[0]))
    #         self._current_thermal_jpg = pair["thermal_jpg"]
    #         self._current_visual_jpg = pair["visual_jpg"]
    #         self._current_roi_result = roi_result
    #         self._current_status = new_status.value
    #         if roi_result.hotspot_centroids:
    #             cx, cy, _ = roi_result.hotspot_centroids[0]
    #             loc_str = f"({cx}, {cy})"
    #         else:
    #             x1, y1, x2, y2 = roi_result.roi_bounds
    #             loc_str = f"ROI({(x1 + x2) // 2}, {(y1 + y2) // 2})"
    #         was_notified = False
    #         if do_alarm:
    #             was_notified = self._try_notify(roi_result, new_status)
    #         self._add_log_row(
    #             datetime.now().strftime("%H:%M:%S"),
    #             loc_str,
    #             roi_result.max_temp,
    #             new_status.value,
    #             was_notified,
    #         )
    #         if do_alarm:
    #             self._log_to_status(
    #                 f"Alert: {new_status.value} | Max: {roi_result.max_temp:.1f}°C")
    #     except Exception as e:
    #         import traceback
    #         traceback.print_exc()
    #         self._append_activity_log(
    #             f"Analysis failed for {pair.get('base', 'unknown')}: {e}")
    #         self._log_to_status(f"Analysis error: {e}")

    # ════════════════════════════════════════════════════════════
    # [DEAD CODE] 기존 알림 (→ _try_notify_from_result 로 대체됨)
    # ════════════════════════════════════════════════════════════
    # def _try_notify(self, roi_result: RoiResult, new_status: Status) -> bool:
    #     try:
    #         if self._current_overlay is None:
    #             return False
    #         overlay_dir = self._config.paths.overlay_dir
    #         os.makedirs(overlay_dir, exist_ok=True)
    #         overlay_path = os.path.join(overlay_dir,
    #             f"{datetime.now().strftime('%Y%m%d%H%M%S')}_overlay.jpg")
    #         cv2.imwrite(overlay_path, self._current_overlay)
    #         success = send_alarm(
    #             image_path=overlay_path,
    #             temp=roi_result.hot_temp_95,
    #             status=new_status.value,
    #             robot_id=self._config.identity.robot_id,
    #         )
    #         if success:
    #             self._monitor_state.last_alarm_time = time.time()
    #         return success
    #     except RuntimeError:
    #         self._append_activity_log("Telegram skipped: not configured")
    #         return False
    #     except Exception as exc:
    #         self._append_activity_log(f"Telegram error: {exc}")
    #         return False

    # ════════════════════════════════════════════════════════════
    # 이미지 표시
    # ════════════════════════════════════════════════════════════
    def _refresh_display(self):
        if self._current_view == "visual" and self._current_visual_overlay is not None:
            display_img = self._current_visual_overlay
        elif self._current_overlay is not None:
            display_img = self._current_overlay
        else:
            return

        self._photo_ref = self._cv2_to_tk(display_img, self.DISPLAY_IMAGE_WIDTH)
        self._image_label.configure(image=self._photo_ref)

        if self._display_max > 0 or self._current_status != "Normal":
            status = self._current_status
            color = {"Critical": "#ff0000", "Warning": "#ff8800"}.get(status, "#00aa00")
            self._status_label.configure(text=f"Status: {status}", foreground=color)
            self._max_temp_label.configure(
                text=f"Max: {self._display_max:.1f} °C")
            self._mean_temp_label.configure(
                text=f"Mean: {self._display_mean:.1f} °C")
            self._hot_temp_label.configure(
                text=f"95th: {self._display_hot_95:.1f} °C")
            self._hotspot_label.configure(
                text=f"Hotspots: {self._current_hotspot_count}")
        self._refresh_readiness()

    def _on_view_changed(self):
        self._current_view = self._view_var.get()
        self._refresh_display()

    @staticmethod
    def _cv2_to_tk(cv_img: np.ndarray, max_width: int) -> ImageTk.PhotoImage:
        h, w = cv_img.shape[:2]
        scale = max_width / w
        new_w, new_h = int(w * scale), int(h * scale)
        img = cv2.resize(cv_img, (new_w, new_h))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(img)
        return ImageTk.PhotoImage(pil_img)

    # ════════════════════════════════════════════════════════════
    # 로그 테이블
    # ════════════════════════════════════════════════════════════
    def _add_log_row(self, timestamp: str, location: str, temp: float,
                     alert_level: str, was_notified: bool):
        def _insert():
            item_id = self._log_tree.insert("", 0, values=(
                timestamp, location, f"{temp:.1f}°C",
                alert_level, "Yes" if was_notified else "—",
            ), tags=(alert_level,))
            self._trim_log()
        self.root.after(0, _insert)

    def _trim_log(self):
        children = self._log_tree.get_children()
        if len(children) > self.MAX_LOG_ROWS:
            for item in children[self.MAX_LOG_ROWS:]:
                self._log_tree.delete(item)

    # ════════════════════════════════════════════════════════════
    # 외부 도구 실행 (ROI Selector / Calibration)
    # ════════════════════════════════════════════════════════════
    def _find_latest_thermal_jpg(self) -> str:
        dataset_dir = self._config.paths.dataset_dir
        if not os.path.isdir(dataset_dir):
            return ""
        jpgs = sorted(glob.glob(os.path.join(dataset_dir, "*.jpg")))
        thermal_jpgs = [j for j in jpgs if "_visual" not in j]
        return thermal_jpgs[-1] if thermal_jpgs else ""

    def _launch_roi_selector(self):
        if self._roi_running:
            return
        img_path = self._find_latest_thermal_jpg()
        if not img_path:
            self._log_to_status("No thermal image found for ROI selection.")
            return
        self._roi_running = True
        self._log_to_status("Launching ROI Selector...")

        def _open():
            old_argv = sys.argv
            sys.argv = ["roi_selector", img_path]
            try:
                from ..tools.roi_selector import main as roi_main
                roi_main()
            finally:
                sys.argv = old_argv
                self._roi_running = False
                self._config = load_config(force_reload=True)
                self.root.after(0, self._update_env_display)
                self._log_to_status("ROI Selector closed.")

        self.root.after(100, _open)

    def _pick_calibration_image(self) -> str:
        """가장 hotspot이 많은 thermal 이미지, 없으면 가장 최근 이미지."""
        dataset_dir = self._config.paths.dataset_dir
        if not os.path.isdir(dataset_dir):
            return ""
        npy_files = sorted(glob.glob(os.path.join(dataset_dir, "*_thermal.npy")))
        if not npy_files:
            return self._find_latest_thermal_jpg()

        best_path = ""
        best_count = 0
        roi_config = load_roi_config()
        for npy_path in npy_files:
            try:
                result = extract_roi_from_npy(npy_path, roi_config)
                count = len(result.hotspot_centroids)
                if count > best_count:
                    best_count = count
                    best_path = npy_path.replace("_thermal.npy", ".jpg")
            except Exception:
                pass

        if best_count == 0 and npy_files:
            best_path = npy_files[-1].replace("_thermal.npy", ".jpg")

        return best_path

    def _launch_calibration(self):
        if self._calib_running:
            return
        thermal_jpg = self._pick_calibration_image()
        if not thermal_jpg or not os.path.isfile(thermal_jpg):
            self._log_to_status("No image found for calibration.")
            return
        visual_jpg = thermal_jpg.replace(".jpg", "_visual.jpg")
        if not os.path.isfile(visual_jpg):
            self._log_to_status(f"Visual image not found: {visual_jpg}")
            return

        self._calib_running = True
        self._log_to_status("Launching Calibration...")

        def _open():
            try:
                from ..tools.calibration import run_calibration
                run_calibration(thermal_jpg, visual_jpg)
            finally:
                self._calib_running = False
                self._config = load_config(force_reload=True)
                self.root.after(0, self._update_env_display)
                self._log_to_status("Calibration closed.")

        self.root.after(100, _open)

    def _update_env_display(self):
        self._cam_ip_var.set(self._config.camera.ip)
        self._dir_var.set(self._config.paths.dataset_dir)
        self._interval_var.set(str(self._config.camera.capture_interval_sec))
        self._mode_var.set(self._config.tools.mode)
        self._exiftool_var.set(
            self._config.tools.exiftool_path or "Auto-detect from PATH")
        self._check_camera_connection()
        self._refresh_readiness()

    # ════════════════════════════════════════════════════════════
    # 종료 처리
    # ════════════════════════════════════════════════════════════
    def on_close(self):
        self._running = False
        if self._scan_timer_id:
            self.root.after_cancel(self._scan_timer_id)
        if self._capture_session:
            self._capture_session.request_stop()
            self._capture_session = None
        self._analysis_executor.shutdown(wait=False)
        self.root.destroy()


def main():
    root = tk.Tk()
    app = MonitoringDashboard(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()

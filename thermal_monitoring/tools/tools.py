"""
tools.py - 통합 운영 도구 GUI

데이터 수집, 무결성 검사, 메타데이터 생성을 하나의 GUI에서 실행합니다.
tkinter 기반 (Python 내장, 추가 설치 불필요).

사용법:
    python tools.py
"""

import sys
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext
from datetime import datetime

from capture import CaptureSession
from checking import run_check
from metadata import run_metadata
from config import load_config


class ToolApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Robot Thermal Monitoring - Tools")
        self.root.geometry("700x550")
        self.root.resizable(True, True)

        self._spin_chars = ["|", "/", "-", "\\"]
        self._spin_idx = 0
        self._spinning = False

        self._build_ui()

        # Capture 세션
        self._capture_session = None
        self._capturing = False

    # --------------------------------------------------------
    # UI 구성
    # --------------------------------------------------------
    def _build_ui(self):
        cfg = load_config()
        # 상단 - 캡처 설정
        config_frame = ttk.LabelFrame(self.root, text="Capture Settings", padding=10)
        config_frame.pack(fill="x", padx=10, pady=(10, 5))

        ttk.Label(config_frame, text="Camera IP:").grid(row=0, column=0, sticky="w", padx=(0, 5))
        self.cam_ip_var = tk.StringVar(value=cfg.camera.ip)
        ttk.Entry(config_frame, textvariable=self.cam_ip_var, width=20).grid(row=0, column=1, sticky="w")

        ttk.Label(config_frame, text="Interval (s):").grid(row=0, column=2, sticky="w", padx=(15, 5))
        self.interval_var = tk.StringVar(value=str(cfg.camera.capture_interval_sec))
        ttk.Entry(config_frame, textvariable=self.interval_var, width=6).grid(row=0, column=3, sticky="w")

        ttk.Label(config_frame, text="Mode:").grid(row=0, column=4, sticky="w", padx=(15, 5))
        self.mode_var = tk.StringVar(value=cfg.tools.mode)
        mode_cb = ttk.Combobox(config_frame, textvariable=self.mode_var,
                               values=["both", "thermal"], state="readonly", width=8)
        mode_cb.grid(row=0, column=5, sticky="w")

        # 버튼 행
        btn_frame = ttk.Frame(self.root, padding=10)
        btn_frame.pack(fill="x", padx=10)

        self.capture_btn = ttk.Button(
            btn_frame, text="Start Capture", command=self._toggle_capture
        )
        self.capture_btn.pack(side="left", padx=(0, 10))

        self.check_btn = ttk.Button(
            btn_frame, text="Check Dataset", command=self._run_check
        )
        self.check_btn.pack(side="left", padx=(0, 10))

        self.metadata_btn = ttk.Button(
            btn_frame, text="Generate Metadata", command=self._run_metadata
        )
        self.metadata_btn.pack(side="left")

        # 상태 표시줄
        self.status_var = tk.StringVar(value="Ready")
        status_label = ttk.Label(self.root, textvariable=self.status_var, relief="sunken", anchor="w", padding=5)
        status_label.pack(fill="x", padx=10, pady=(0, 5))

        # 로그 출력
        log_frame = ttk.LabelFrame(self.root, text="Log", padding=5)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.log_area = scrolledtext.ScrolledText(
            log_frame, wrap="word", state="disabled",
            font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="#d4d4d4",
        )
        self.log_area.pack(fill="both", expand=True)

    # --------------------------------------------------------
    # 로그 출력
    # --------------------------------------------------------
    def _log(self, msg: str):
        def _write():
            self.log_area.configure(state="normal")
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_area.insert("end", f"[{ts}] {msg}\n")
            self.log_area.see("end")
            self.log_area.configure(state="disabled")
        self.root.after(0, _write)

    def _set_status(self, text: str):
        self.root.after(0, lambda: self.status_var.set(text))

    # --------------------------------------------------------
    # 스피너 (작업 중 표시)
    # --------------------------------------------------------
    def _start_spin(self, label: str):
        self._spinning = True
        self._spin_label = label

        def _spin():
            if not self._spinning:
                return
            self._spin_idx = (self._spin_idx + 1) % len(self._spin_chars)
            self._set_status(f"{self._spin_chars[self._spin_idx]} {self._spin_label}")
            self.root.after(200, _spin)

        self.root.after(0, _spin)

    def _stop_spin(self, final: str):
        self._spinning = False
        self._set_status(final)

    # --------------------------------------------------------
    # Capture Start / Stop
    # --------------------------------------------------------
    def _toggle_capture(self):
        if self._capturing:
            self._stop_capture()
        else:
            self._start_capture()

    def _start_capture(self):
        cam_ip = self.cam_ip_var.get().strip()
        try:
            interval = float(self.interval_var.get())
        except ValueError:
            self._log("Invalid interval value.")
            return
        mode = self.mode_var.get()

        self._capture_session = CaptureSession(
            cam_ip=cam_ip,
            mode=mode,
            interval=interval,
            log_callback=self._log,
        )
        self._capture_session.start()
        self._capturing = True
        self.capture_btn.configure(text="Stop Capture")
        self._set_status(f"Capturing ({mode}, every {interval}s)")
        self._log(f"Capture started - {cam_ip}")

    def _stop_capture(self):
        if self._capture_session:
            self._capture_session.stop()
        self._capturing = False
        self.capture_btn.configure(text="Start Capture")
        self._set_status("Ready")

    # --------------------------------------------------------
    # Check Dataset (백그라운드 스레드)
    # --------------------------------------------------------
    def _run_check(self):
        self.check_btn.configure(state="disabled")
        self._start_spin("Checking dataset integrity...")

        def _task():
            try:
                run_check(log_callback=self._log)
                self._stop_spin("Check complete.")
            except Exception as e:
                self._log(f"Check error: {e}")
                self._stop_spin("Check failed.")
            finally:
                self.root.after(0, lambda: self.check_btn.configure(state="normal"))

        threading.Thread(target=_task, daemon=True).start()

    # --------------------------------------------------------
    # Generate Metadata (백그라운드 스레드)
    # --------------------------------------------------------
    def _run_metadata(self):
        self.metadata_btn.configure(state="disabled")
        self._start_spin("Generating metadata...")

        def _task():
            try:
                run_metadata(log_callback=self._log)
                self._stop_spin("Metadata complete.")
            except Exception as e:
                self._log(f"Metadata error: {e}")
                self._stop_spin("Metadata failed.")
            finally:
                self.root.after(0, lambda: self.metadata_btn.configure(state="normal"))

        threading.Thread(target=_task, daemon=True).start()

    # --------------------------------------------------------
    # 종료 처리
    # --------------------------------------------------------
    def on_close(self):
        if self._capturing:
            self._stop_capture()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = ToolApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()

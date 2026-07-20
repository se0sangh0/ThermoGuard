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
import threading
import time
from datetime import datetime

import requests

from ..config import load_config


class CaptureSession:
    def __init__(
        self,
        cam_ip: str | None = None,
        mode: str | None = None,
        interval: float | None = None,
        save_dir: str | None = None,
        log_callback=None,
    ):
        cfg = load_config()
        self.cam_ip = cam_ip or cfg.camera.ip
        self.mode = mode or cfg.tools.mode      # "thermal" or "both"
        self.interval = interval or cfg.camera.capture_interval_sec
        self.save_dir = save_dir or cfg.paths.dataset_dir
        self.log_callback = log_callback  # callable(str) for GUI output
        self._running = False
        self._thread = None
        # GUI-UPDATE: cam_ip 인자가 None이어도 config에서 확정된 self.cam_ip를 사용한다.
        self._urls = {
            "thermal": f"http://{self.cam_ip}/api/image/current?imgformat=JPEG",
            "visual": f"http://{self.cam_ip}/api/image/current?imgformat=JPEG_visual",
        }

    def _log(self, msg: str):
        if self.log_callback:
            self.log_callback(msg)
        else:
            print(msg)

    def start(self):
        if self._running:
            self._log("[capture] Already running.")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._log(f"[capture] Started (interval={self.interval}s, mode={self.mode})")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=self.interval + 5)
        self._log("[capture] Stopped.")

    @property
    def running(self) -> bool:
        return self._running

    def _run(self):
        os.makedirs(self.save_dir, exist_ok=True)
        img_types = ["thermal", "visual"] if self.mode == "both" else ["thermal"]

        while self._running:
            try:
                filenametime = datetime.now().strftime("%Y%m%d%H%M%S_%f")
                for img_type in img_types:
                    if not self._running:
                        break
                    r = requests.get(self._urls[img_type], timeout=10)
                    if r.status_code == 200:
                        content_type = r.headers.get("Content-Type", "")
                        if "image" not in content_type.lower() and content_type != "octet-stream":
                            self._log(f"[{img_type}] Not an image. Content-Type: {content_type}")
                            continue
                        suffix = "_visual" if img_type == "visual" else ""
                        jpg_path = os.path.join(self.save_dir, f"{filenametime}{suffix}.jpg")
                        with open(jpg_path, "wb") as f:
                            f.write(r.content)
                        self._log(f"[{datetime.now().strftime('%H:%M:%S')}] [{img_type}] saved "
                                  f"({len(r.content)} bytes)")
                    else:
                        self._log(f"[{img_type}] HTTP {r.status_code}")
            except requests.exceptions.Timeout:
                self._log("[capture] Timeout")
            except requests.exceptions.ConnectionError:
                self._log("[capture] Connection error - check camera IP")
            except Exception as e:
                self._log(f"[capture] Error: {e}")

            # GUI-UPDATE: 소수점 촬영 주기를 보존하면서 stop 요청에도 빠르게 반응한다.
            deadline = time.monotonic() + self.interval
            while self._running:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.2, remaining))


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

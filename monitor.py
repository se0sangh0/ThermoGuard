"""
monitor.py - 실시간 열화상 감시 시퀀서 (Real-time Thermal Monitoring Sequencer)

캡처는 백그라운드 스레드에서 논스톱으로 돌아가고, 메인 루프에서는:
  1. 데이터 무결성 검사 (누락 NPY 복구, 고아 NPY 제거)
  2. 신규 이미지 쌍 스캔
  3. ROI 온도 통계 추출
  4. Threshold + 상태 머신 판정 (Normal → Warning → Critical)
  5. 과열 시 오버레이 이미지 생성
  6. 상태 변화 시 Telegram 알림 전송
  7. CSV 메타데이터 주기적 업데이트

모든 단계에서 예외가 발생해도 로그만 남기고 시퀀스는 계속 동작합니다.

사용법:
    python monitor.py
"""

import os
import sys
import threading
import time
from datetime import datetime
from typing import Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import load_config
from capture import CaptureSession
from checking import run_check, CheckResult
from roi import load_roi_config, extract_roi_from_npy
from threshold import (
    Status,
    MonitorState,
    evaluate_with_state,
)
from overlay import create_overlay, save_overlay
from notifier import send_alarm
from metadata import run_metadata

_cfg = load_config()
DATASET_DIR = _cfg.paths.dataset_dir
OVERLAY_DIR = _cfg.paths.overlay_dir

# 처리 루프가 새 파일을 확인하는 주기 (초)
PROCESS_INTERVAL = _cfg.monitoring.process_interval_sec
# 무결성 검사 주기 (초)
INTEGRITY_INTERVAL = _cfg.monitoring.integrity_interval_sec
# 메타데이터 업데이트 주기 (초)
METADATA_INTERVAL = _cfg.monitoring.metadata_interval_sec
# 처리 완료된 파일 캐시 최대 개수 (메모리 관리)
MAX_PROCESSED_CACHE = _cfg.monitoring.max_processed_cache


class MonitorSequencer:
    """실시간 열화상 감시 시퀀서"""

    def __init__(
        self,
        cam_ip: str = "192.168.0.51",
        capture_interval: float = 1.0,
        process_interval: float = PROCESS_INTERVAL,
    ):
        self.cam_ip = cam_ip
        self.capture_interval = capture_interval
        self.process_interval = process_interval
        self._running = False
        self._lock = threading.Lock()

        self.capture: Optional[CaptureSession] = None
        self.state = MonitorState()
        self.roi_config = None
        self.processed_bases: set = set()
        self._alarm_count = 0
        self._status_counts = {s.value: 0 for s in Status}

    # ── 로깅 ────────────────────────────────────────────────
    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {msg}")

    # ── 파일 스캔 ─────────────────────────────────────────────
    def _scan_all_paired_bases(self) -> set:
        """thermal JPG + visual JPG 둘 다 존재하는 모든 base를 반환"""
        if not os.path.isdir(DATASET_DIR):
            return set()
        try:
            files = os.listdir(DATASET_DIR)
        except OSError:
            return set()
        thermal_jpgs = {
            f.replace(".jpg", ""): f
            for f in files
            if f.endswith(".jpg") and "_visual" not in f
        }
        visual_jpgs = {
            f.replace("_visual.jpg", ""): f
            for f in files
            if f.endswith("_visual.jpg")
        }
        return set(thermal_jpgs.keys()) & set(visual_jpgs.keys())

    def _prime_processed_cache(self):
        """시작 시점에 이미 존재하는 모든 쌍을 processed_bases에 추가 (기존 데이터 알람 방지)"""
        existing = self._scan_all_paired_bases()
        self.processed_bases = existing
        if existing:
            self._log(f"Seeded processed cache with {len(existing)} existing pair(s) — will not re-analyze.")

    def _scan_new_pairs(self) -> list[dict]:
        """아직 처리되지 않은 이미지 쌍을 찾아 반환"""
        if not os.path.isdir(DATASET_DIR):
            return []

        try:
            files = os.listdir(DATASET_DIR)
        except OSError:
            return []

        thermal_jpgs = {
            f.replace(".jpg", ""): f
            for f in files
            if f.endswith(".jpg") and "_visual" not in f
        }
        npys = {
            f.replace("_thermal.npy", ""): f
            for f in files
            if f.endswith("_thermal.npy")
        }
        visual_jpgs = {
            f.replace("_visual.jpg", ""): f
            for f in files
            if f.endswith("_visual.jpg")
        }

        # thermal JPG + visual JPG 둘 다 있는 모든 쌍을 대상으로 함
        bases = sorted(
            set(thermal_jpgs.keys()) & set(visual_jpgs.keys())
        )

        new_pairs = []
        for base in bases:
            if base in self.processed_bases:
                continue
            npy_path = os.path.join(DATASET_DIR, base + "_thermal.npy")
            # NPY가 없으면 JPEG에서 즉시 추출
            if base not in npys:
                try:
                    from thermal_utils import extract_from_jpeg
                    jpg_path = os.path.join(DATASET_DIR, thermal_jpgs[base])
                    thermal, _ = extract_from_jpeg(jpg_path)
                    np.save(npy_path, thermal)
                except Exception as e:
                    self._log(f"  Failed to extract NPY for {base}: {e}")
                    continue
            new_pairs.append({
                "base": base,
                "thermal_jpg": os.path.join(DATASET_DIR, thermal_jpgs[base]),
                "visual_jpg": os.path.join(DATASET_DIR, visual_jpgs[base]),
                "npy": npy_path,
            })

        return new_pairs

    def _mark_processed(self, base: str):
        """처리 완료 목록에 추가. 캐시가 너무 커지면 오래된 항목 제거"""
        self.processed_bases.add(base)
        if len(self.processed_bases) > MAX_PROCESSED_CACHE:
            # 절반만 남기고 오래된 항목 제거
            retain = MAX_PROCESSED_CACHE // 2
            self.processed_bases = set(sorted(self.processed_bases)[-retain:])

    # ── 무결성 검사 ─────────────────────────────────────────
    def _run_integrity_check(self):
        """데이터셋 무결성 검사: 누락 NPY 복구, 고아 NPY 제거"""
        try:
            result = run_check(save_dir=DATASET_DIR, log_callback=None)
            if result.missing_npy > 0 or result.orphan_npy > 0:
                self._log(
                    f"Integrity: {result.missing_npy} NPY recovered, "
                    f"{result.orphan_npy} orphans removed"
                )
        except Exception as e:
            self._log(f"Integrity check error (recovering): {e}")

    # ── 메타데이터 업데이트 ─────────────────────────────────
    def _run_metadata_update(self):
        """CSV 메타데이터 업데이트"""
        try:
            result = run_metadata(save_dir=DATASET_DIR, log_callback=None)
            if result.new > 0:
                self._log(f"Metadata: {result.new} new records")
        except Exception as e:
            self._log(f"Metadata update error (recovering): {e}")

    # ── 단일 쌍 처리 ─────────────────────────────────────────
    def _process_pair(self, pair: dict) -> bool:
        """
        이미지 한 쌍을 전체 파이프라인으로 처리.

        Returns:
            True: 정상 처리
            False: 오류 발생 (시퀀스는 계속됨)
        """
        base = pair["base"]
        try:
            if self.roi_config is None:
                self._log(f"[{base}] ROI config not loaded — skipping")
                return False

            # 1. ROI 추출
            roi_result = extract_roi_from_npy(pair["npy"], self.roi_config)

            # 2. Threshold + 상태 판정
            new_status, do_alarm = evaluate_with_state(
                hot_temp=roi_result.hot_temp_95,
                max_temp=roi_result.max_temp,
                mean_temp=roi_result.mean_temp,
                baseline=self.roi_config.baseline_temp,
                warning_delta=self.roi_config.warning_delta,
                critical_delta=self.roi_config.critical_delta,
                state=self.state,
                over_temp_pixels=roi_result.over_temp_pixels,
                max_hotspot_size=roi_result.max_hotspot_size,
            )

            prev_status = self.state.status
            self.state.status = new_status

            with self._lock:
                self._status_counts[new_status.value] += 1

            # 상태 변화 표시
            transition = ""
            if do_alarm:
                transition = f"  >>> {prev_status.value} → {new_status.value} <<<"

            self._log(
                f"[{base}] 95th={roi_result.hot_temp_95:.1f}°C "
                f"max={roi_result.max_temp:.1f}°C "
                f"mean={roi_result.mean_temp:.1f}°C "
                f"| {new_status.value}{transition}"
            )

            # 3. 오버레이 생성 (Warning, Critical일 때만)
            overlay_path = None
            if new_status != Status.NORMAL:
                try:
                    self._log(f"Hotspots: {roi_result.hotspot_centroids}")
                    self._log(f"Count: {len(roi_result.hotspot_centroids)}")
                    overlay = create_overlay(
                        thermal_jpg_path=pair["thermal_jpg"],
                        visual_jpg_path=pair["visual_jpg"],
                        roi_bounds=roi_result.roi_bounds,
                        max_temp=roi_result.max_temp,
                        mean_temp=roi_result.mean_temp,
                        hot_temp=roi_result.hot_temp_95,
                        status=new_status.value,
                        hotspot_centroids=roi_result.hotspot_centroids,
                    )
                    overlay_path = save_overlay(base, overlay)
                except Exception as e:
                    self._log(f"  Overlay error: {e}")

            # 4. 알림 전송 (상태 변화 시)
            if do_alarm:
                try:
                    success = send_alarm(
                        image_path=overlay_path or pair["thermal_jpg"],
                        temp=roi_result.hot_temp_95,
                        status=new_status.value,
                    )
                    if success:
                        self.state.last_alarm_time = time.time()
                        with self._lock:
                            self._alarm_count += 1
                        self._log(f"  ▲ Alarm sent: {new_status.value}")
                    else:
                        self._log(f"  ▲ Alarm failed to send")
                except RuntimeError:
                    self._log("  ▲ Alarm skipped (Telegram not configured)")
                except Exception as e:
                    self._log(f"  ▲ Alarm error: {e}")

            return True

        except FileNotFoundError as e:
            self._log(f"[{base}] File missing: {e}")
            return False
        except ValueError as e:
            self._log(f"[{base}] Data error: {e}")
            return False
        except Exception as e:
            self._log(f"[{base}] Unexpected error: {e}")
            import traceback
            traceback.print_exc()
            return False

    # ── 메인 감시 루프 ───────────────────────────────────────
    def _monitoring_loop(self):
        """메인 처리 루프: 스캔 → 처리 → 대기. 예외 발생 시에도 재개"""
        self._log("=" * 50)
        self._log("  Robot Thermal Monitor — Sequencer Started")
        self._log(f"  Camera : {self.cam_ip}")
        self._log(f"  Capture interval : {self.capture_interval}s")
        self._log(f"  Process interval : {self.process_interval}s")
        self._log("=" * 50)
        self._log("")

        integrity_timer = time.time()
        metadata_timer = time.time()

        while self._running:
            try:
                now = time.time()

                # 주기적 무결성 검사
                if now - integrity_timer >= INTEGRITY_INTERVAL:
                    self._run_integrity_check()
                    integrity_timer = now

                # 주기적 메타데이터 업데이트
                if now - metadata_timer >= METADATA_INTERVAL:
                    self._run_metadata_update()
                    metadata_timer = now

                # 신규 쌍 스캔
                new_pairs = self._scan_new_pairs()
                if new_pairs:
                    self._log(f"→ {len(new_pairs)} new pair(s) detected")
                    for pair in new_pairs:
                        if not self._running:
                            break
                        self._process_pair(pair)
                        self._mark_processed(pair["base"])

            except Exception as e:
                self._log(f"Loop error (recovering): {e}")
                import traceback
                traceback.print_exc()

            # 다음 스캔까지 대기 (stop 체크를 위해 1초씩 분할)
            for _ in range(int(self.process_interval)):
                if not self._running:
                    break
                time.sleep(1)

    # ── 공개 API ─────────────────────────────────────────────
    def start(self):
        """캡처 시작 + 감시 루프 진입"""
        if self._running:
            self._log("Already running.")
            return

        # ROI 설정 로드 (캡처 시작 전 필수)
        try:
            self.roi_config = load_roi_config()
        except Exception as e:
            self._log(f"Failed to load ROI config: {e}")
            self._log("Using default ROI (full frame)")
            from roi import RoiConfig
            self.roi_config = RoiConfig()

        self._log(
            f"ROI: ({self.roi_config.x1},{self.roi_config.y1})"
            f"-({self.roi_config.x2},{self.roi_config.y2})"
        )
        self._log(
            f"Threshold: baseline={self.roi_config.baseline_temp}°C, "
            f"warning=+{self.roi_config.warning_delta}°C, "
            f"critical=+{self.roi_config.critical_delta}°C"
        )
        self._log(f"Cooldown: {self.state.alarm_cooldown / 60:.0f} min")

        # 시작 시점에 기존 데이터 캐시 (이미 존재하는 파일은 재분석하지 않음)
        self._prime_processed_cache()

        self._running = True

        # 백그라운드 캡처 시작 (논스톱, 독립 스레드)
        self.capture = CaptureSession(
            cam_ip=self.cam_ip,
            mode="both",
            interval=self.capture_interval,
            save_dir=DATASET_DIR,
            log_callback=self._log,
        )
        self.capture.start()

        # 메인 스레드에서 감시 루프 실행
        try:
            self._monitoring_loop()
        except KeyboardInterrupt:
            self._log("Keyboard interrupt received.")
        finally:
            self.stop()

    def stop(self):
        """캡처 + 감시 루프 종료"""
        self._running = False
        if self.capture:
            self.capture.stop()

        # 요약 출력
        self._log("=" * 50)
        self._log("  Sequencer Stopped")
        self._log(f"  Alarms sent: {self._alarm_count}")
        for s in Status:
            self._log(f"  {s.value}: {self._status_counts[s.value]}")
        self._log("=" * 50)


# ── 진입점 ──────────────────────────────────────────────────
if __name__ == "__main__":
    from _encoding import setup_encoding
    setup_encoding()

    cfg = load_config()

    monitor = MonitorSequencer(
        cam_ip=os.environ.get("CAM_IP", cfg.camera.ip),
        capture_interval=float(os.environ.get("CAPTURE_INTERVAL", str(cfg.camera.capture_interval_sec))),
    )

    monitor.start()

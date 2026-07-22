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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

import numpy as np

from ..config import load_config
from ..capture.capture import CaptureSession
from ..data.checking import run_check
from ..data.metadata import run_metadata
from ..data.cleanup import run_cleanup_if_due
from ..analysis.roi import load_roi_config, extract_roi_from_npy, extract_all_rois_from_npy, _get_roi_bounds_list
from ..analysis.threshold import (
    Status,
    MonitorState,
    evaluate_threshold,
    evaluate_with_state,
    _STATUS_RANK,
)
from ..analysis.overlay import create_overlay, save_overlay
from ..analysis.notifier import send_alarm
from ..logger import get_logger

_cfg = load_config()
DATASET_DIR = _cfg.paths.dataset_dir
OVERLAY_DIR = _cfg.paths.overlay_dir
MAX_PARALLEL_PAIRS = max(2, (os.cpu_count() or 4) // 2)
_log_monitor = get_logger("pipeline.monitor")

# 처리 루프가 새 파일을 확인하는 주기 (초)
PROCESS_INTERVAL = _cfg.monitoring.process_interval_sec
# 무결성 검사 주기 (초)
INTEGRITY_INTERVAL = _cfg.monitoring.integrity_interval_sec
# 메타데이터 업데이트 주기 (초)
METADATA_INTERVAL = _cfg.monitoring.metadata_interval_sec
# 처리 완료된 파일 캐시 최대 개수 (메모리 관리)
MAX_PROCESSED_CACHE = _cfg.monitoring.max_processed_cache

# 전송 실패한 CRITICAL 알람의 재시도 최소 간격 (초). CRITICAL이 지속되는 동안
# 성공할 때까지 재시도하되, 네트워크 장애 시 과도한 요청을 막기 위한 백오프.
ALARM_RETRY_BACKOFF_SEC = 60.0


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
    def _scan_all_existing_bases(self) -> set:
        """시작 시점에 이미 존재하는 모든 thermal base를 반환.

        _scan_new_pairs와 동일하게 thermal JPG 기준(visual 유무 무관)으로 스캔한다.
        visual까지 요구하면, 이전 실행의 thermal-only(경고 모드) 잔여 파일이
        프라임에서 누락되어 과거 데이터로 재분석/오알람될 수 있다.
        """
        if not os.path.isdir(DATASET_DIR):
            return set()
        bases: set = set()
        try:
            with os.scandir(DATASET_DIR) as entries:
                for entry in entries:
                    if not entry.is_file():
                        continue
                    name = entry.name
                    if (name.endswith(".jpg")
                            and "_visual" not in name
                            and "_overlay" not in name):
                        bases.add(name.replace(".jpg", ""))
        except OSError:
            return set()
        return bases

    def _prime_processed_cache(self):
        """시작 시점에 이미 존재하는 모든 thermal base를 processed_bases에 추가 (기존 데이터 알람 방지)"""
        existing = self._scan_all_existing_bases()
        self.processed_bases = existing
        if existing:
            self._log(f"Seeded processed cache with {len(existing)} existing base(s) — will not re-analyze.")

    def _scan_new_pairs(self) -> list[dict]:
        """아직 처리되지 않은 이미지 쌍을 찾아 반환"""
        if not os.path.isdir(DATASET_DIR):
            return []

        thermal_jpgs: dict[str, str] = {}
        npys: dict[str, str] = {}
        visual_jpgs: dict[str, str] = {}
        try:
            with os.scandir(DATASET_DIR) as entries:
                for entry in entries:
                    if not entry.is_file():
                        continue
                    name = entry.name
                    if name.endswith("_thermal.npy"):
                        npys[name.replace("_thermal.npy", "")] = name
                    elif name.endswith("_visual.jpg"):
                        visual_jpgs[name.replace("_visual.jpg", "")] = name
                    elif name.endswith(".jpg") and "_overlay" not in name:
                        thermal_jpgs[name.replace(".jpg", "")] = name
        except OSError:
            return []

        # thermal JPG + visual JPG 둘 다 있는 모든 쌍을 대상으로 함
        # visual이 없는 thermal만 있어도 분석 진행 (경고 모드 등)
        bases = sorted(set(thermal_jpgs.keys()))

        new_pairs = []
        for base in bases:
            if base in self.processed_bases:
                continue
            npy_path = os.path.join(DATASET_DIR, base + "_thermal.npy")
            # NPY가 없으면 JPEG에서 즉시 추출
            if base not in npys:
                try:
                    from ..capture.thermal_utils import extract_from_jpeg
                    jpg_path = os.path.join(DATASET_DIR, thermal_jpgs[base])
                    thermal, _ = extract_from_jpeg(jpg_path)
                    np.save(npy_path, thermal)
                except Exception as e:
                    self._log(f"  Failed to extract NPY for {base}: {e}")
                    continue
            visual_jpg = visual_jpgs.get(base)
            new_pairs.append({
                "base": base,
                "thermal_jpg": os.path.join(DATASET_DIR, thermal_jpgs[base]),
                "visual_jpg": os.path.join(DATASET_DIR, visual_jpg) if visual_jpg else "",
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

    # ── 오래된 데이터 정리 ───────────────────────────────────
    def _run_cleanup(self):
        """오래된 데이터셋 정리 (1시간마다 자동 실행)"""
        try:
            result = run_cleanup_if_due(
                save_dir=DATASET_DIR,
                log_callback=self._log,
            )
            if result is not None:
                self._log(f"Cleanup: {result.removed_pairs} pairs removed, "
                          f"{result.freed_bytes / (1024 * 1024):.1f} MB freed")
        except Exception as e:
            self._log(f"Cleanup error (recovering): {e}")

    # ── 단일 쌍 분석 (순수/스레드 안전, 상태 변경 없음) ──────────
    def _analyze_pair(self, pair: dict) -> Optional[dict]:
        """
        이미지 한 쌍의 ROI 통계를 추출하고 대표 ROI를 선정한다.

        상태 머신(self.state)을 건드리지 않는 순수 연산이므로 여러 쌍을
        스레드 풀로 병렬 실행해도 안전하다. 상태 판정/알람은 이후
        _evaluate_and_act에서 timestamp 순으로 순차 처리한다.

        Returns:
            분석 결과 dict, 또는 오류/스킵 시 None
        """
        base = pair["base"]
        try:
            if self.roi_config is None:
                self._log(f"[{base}] ROI config not loaded — skipping")
                return None

            # 1. ROI 추출 (다중 ROI 지원)
            roi_results = extract_all_rois_from_npy(pair["npy"], self.roi_config)

            # 대표 ROI 선정: 각 ROI를 개별 판정하여 가장 심각한(동률이면 95th가 높은)
            #  ROI를 대표로 사용. 95th만으로 뽑으면 max·클러스터가 큰 국소 발열
            #  ROI를 놓칠 수 있어 상태 심각도 기준으로 보완한다.
            def _severity(rr) -> tuple:
                st = evaluate_threshold(
                    rr.hot_temp_95, rr.max_temp,
                    self.roi_config.baseline_temp,
                    self.roi_config.warning_delta,
                    self.roi_config.critical_delta,
                    max_hotspot_size=rr.max_hotspot_size,
                )
                return (_STATUS_RANK[st], rr.hot_temp_95)

            roi_result = max(roi_results, key=_severity)

            # 모든 ROI의 핫스팟 통합 + 5px 이내 중복 제거
            all_hotspots = []
            for rr in roi_results:
                all_hotspots.extend(rr.hotspot_centroids)
            unique_hotspots = []
            for spot in all_hotspots:
                is_dup = False
                for idx, u in enumerate(unique_hotspots):
                    if abs(spot[0] - u[0]) < 5 and abs(spot[1] - u[1]) < 5:
                        if spot[2] > u[2]:
                            unique_hotspots[idx] = spot
                        is_dup = True
                        break
                if not is_dup:
                    unique_hotspots.append(spot)
            roi_result.hotspot_centroids = unique_hotspots

            return {
                "pair": pair,
                "roi_results": roi_results,
                "roi_result": roi_result,
            }

        except FileNotFoundError as e:
            _log_monitor.warning("[%s] File missing: %s", base, e)
            self._log(f"[{base}] File missing: {e}")
            return None
        except ValueError as e:
            _log_monitor.warning("[%s] Data error: %s", base, e)
            self._log(f"[{base}] Data error: {e}")
            return None
        except Exception as e:
            _log_monitor.error("[%s] Unexpected analysis error: %s", base, e, exc_info=True)
            self._log(f"[{base}] Unexpected error: {e}")
            return None

    # ── 상태 판정 + 전이 + 알람 (메인 스레드 전용, 순차 실행) ────
    def _evaluate_and_act(self, analysis: dict) -> None:
        """
        분석 결과에 상태 머신을 적용하고 오버레이/알람을 처리한다.

        self.state를 변경하므로 반드시 메인 스레드에서 timestamp 순서대로
        순차 호출해야 한다(병렬 호출 시 상태 경쟁으로 중복 알람/비결정적
        상태 전이가 발생한다).
        """
        pair = analysis["pair"]
        roi_results = analysis["roi_results"]
        roi_result = analysis["roi_result"]
        base = pair["base"]
        try:
            # 2. Threshold + 상태 판정 — 모든 ROI 개별 평가
            per_roi_statuses: list[dict] = []
            for rr in roi_results:
                s, a = evaluate_with_state(
                    hot_temp=rr.hot_temp_95,
                    max_temp=rr.max_temp,
                    mean_temp=rr.mean_temp,
                    baseline=self.roi_config.baseline_temp,
                    warning_delta=self.roi_config.warning_delta,
                    critical_delta=self.roi_config.critical_delta,
                    state=self.state,
                    over_temp_pixels=rr.over_temp_pixels,
                    max_hotspot_size=rr.max_hotspot_size,
                    roi_name=rr.roi_name if rr.roi_name else None,
                )
                per_roi_statuses.append({"roi_name": rr.roi_name, "status": s, "alarm": a, "roi": rr})

            # 최악 ROI로 대표 상태 선정
            worst = max(per_roi_statuses, key=lambda x: (_STATUS_RANK[x["status"]], x["roi"].hot_temp_95))
            new_status = worst["status"]
            do_alarm = any(ps["alarm"] for ps in per_roi_statuses)

            # per-ROI 상태 갱신
            for ps in per_roi_statuses:
                rn = ps["roi_name"]
                if rn:
                    rs = self.state.roi_state(rn)
                    rs.status = ps["status"]
                    if ps["alarm"]:
                        rs.last_alarm_time = time.time()
                        rs.alarm_pending = False

            prev_status = self.state.status
            self.state.status = new_status

            # 상태 변화 시 캡처 주기 전환
            if prev_status == Status.NORMAL and new_status != Status.NORMAL:
                if self.capture:
                    self.capture.set_warning_mode(True)
                _log_monitor.info("Capture interval switched to warning mode (%.1fs) — status: %s",
                                  _cfg.camera.warning_interval_sec, new_status.value)
            elif prev_status != Status.NORMAL and new_status == Status.NORMAL:
                if self.capture:
                    self.capture.set_warning_mode(False)
                _log_monitor.info("Capture interval restored to normal (%.1fs) — status: Normal",
                                  _cfg.camera.capture_interval_sec)

            self._status_counts[new_status.value] += 1

            # ── 알람 대기(pending) 상태 갱신 ──
            # 새 CRITICAL 전이면 알람을 '보내야 함'으로 표시. CRITICAL을 벗어나면 종료.
            if do_alarm:
                self.state.alarm_pending = True
            if new_status != Status.CRITICAL:
                self.state.alarm_pending = False

            # 이번 사이클에 전송을 시도할지: 신규 전이거나, 대기 중이며 백오프가 경과했을 때.
            # (전송 실패 시 status는 이미 CRITICAL이라 do_alarm은 False가 되므로,
            #  alarm_pending 기반으로 CRITICAL이 지속되는 동안 재시도한다.)
            now = time.time()
            attempt_send = self.state.alarm_pending and (
                do_alarm or (now - self.state.last_alarm_attempt) >= ALARM_RETRY_BACKOFF_SEC
            )

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

                    # 카메라 2중 접근 방지: 배경 캡처가 방금 저장한 최신 쌍을 재사용.
                    # (경고 모드면 visual이 없어 thermal-only 오버레이로 폴백)
                    overlay_thermal = pair["thermal_jpg"]
                    overlay_visual = pair["visual_jpg"]
                    if self.capture:
                        last_t, last_v = self.capture.last_saved_pair
                        if last_t:
                            overlay_thermal = last_t
                            overlay_visual = last_v or ""
                            self._log(f"  Using latest captured frame: {os.path.basename(last_t)}")

                    overlay = create_overlay(
                        thermal_jpg_path=overlay_thermal,
                        visual_jpg_path=overlay_visual,
                        roi_bounds=roi_result.roi_bounds,
                        max_temp=roi_result.max_temp,
                        mean_temp=roi_result.mean_temp,
                        hot_temp=roi_result.hot_temp_95,
                        status=new_status.value,
                        hotspot_centroids=roi_result.hotspot_centroids,
                        roi_bounds_list=_get_roi_bounds_list(self.roi_config),
                        roi_names=[r.roi_name for r in roi_results] if len(roi_results) > 1 else None,
                    )
                    overlay_path = save_overlay(base, overlay)
                except Exception as e:
                    self._log(f"  Overlay error: {e}")

            # 4. 알림 전송 (신규 전이 또는 실패분 재시도)
            if attempt_send:
                self.state.last_alarm_attempt = now
                is_retry = not do_alarm
                try:
                    success = send_alarm(
                        image_path=overlay_path or pair["thermal_jpg"],
                        temp=roi_result.hot_temp_95,
                        status=new_status.value,
                    )
                    if success:
                        self.state.last_alarm_time = time.time()
                        self.state.alarm_pending = False
                        self._alarm_count += 1
                        self._log(f"  ▲ Alarm {'re-sent' if is_retry else 'sent'}: {new_status.value}")
                    else:
                        self._log("  ▲ Alarm send failed — pending, will retry")
                except RuntimeError:
                    # 텔레그램 미설정: 재시도해도 소용없으므로 대기 해제
                    self.state.alarm_pending = False
                    self._log("  ▲ Alarm skipped (Telegram not configured)")
                except Exception as e:
                    self._log(f"  ▲ Alarm error: {e} — pending, will retry")

        except Exception as e:
            _log_monitor.error("[%s] Unexpected evaluate/act error: %s", base, e, exc_info=True)
            self._log(f"[{base}] Unexpected error: {e}")
            import traceback
            traceback.print_exc()

    # ── 메인 감시 루프 ───────────────────────────────────────
    def _monitoring_loop(self):
        """메인 처리 루프: 스캔 → 처리 → 대기. 예외 발생 시에도 재개"""
        _log_monitor.info("=" * 50)
        _log_monitor.info("Sequencer started: camera=%s capture_interval=%.1fs process_interval=%.1fs",
                          self.cam_ip, self.capture_interval, self.process_interval)
        self._log("=" * 50)
        self._log("  Robot Thermal Monitor — Sequencer Started")
        self._log(f"  Camera : {self.cam_ip}")
        self._log(f"  Capture interval : {self.capture_interval}s")
        self._log(f"  Process interval : {self.process_interval}s")
        self._log("=" * 50)
        self._log("")

        integrity_timer = time.time()
        metadata_timer = time.time()
        cleanup_timer = time.time()

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

                # 주기적 오래된 데이터 정리 (1시간마다)
                if now - cleanup_timer >= 3600:
                    self._run_cleanup()
                    cleanup_timer = now

                # 신규 쌍 스캔
                new_pairs = self._scan_new_pairs()
                if new_pairs:
                    self._log(f"→ {len(new_pairs)} new pair(s) detected")
                    # 1) ROI 추출은 순수/스레드 안전 → 스레드 풀로 병렬 처리.
                    #    executor.map은 입력 순서(=timestamp 정렬)를 그대로 보존한다.
                    if len(new_pairs) > 1:
                        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_PAIRS) as executor:
                            analyses = list(executor.map(self._analyze_pair, new_pairs))
                    else:
                        analyses = [self._analyze_pair(new_pairs[0])]

                    # 2) 상태 판정/알람은 timestamp 순서로 메인 스레드에서 순차 실행.
                    #    (상태 경쟁 방지 + 시계열 상태 전이 정합성 보장)
                    for pair, analysis in zip(new_pairs, analyses):
                        if not self._running:
                            break
                        if analysis is not None:
                            self._evaluate_and_act(analysis)
                        self._mark_processed(pair["base"])

            except Exception as e:
                _log_monitor.error("Monitoring loop error (recovering): %s", e, exc_info=True)
                self._log(f"Loop error (recovering): {e}")
                import traceback
                traceback.print_exc()

            # 다음 스캔까지 대기 (monotonic 데드라인 — 1초 미만/소수 값도 안전, busy-spin 방지)
            deadline = time.monotonic() + self.process_interval
            while self._running:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(1.0, remaining))

    # ── 공개 API ─────────────────────────────────────────────
    def start(self):
        """캡처 시작 + 감시 루프 진입"""
        if self._running:
            _log_monitor.warning("start() called but already running")
            self._log("Already running.")
            return

        # ROI 설정 로드 (캡처 시작 전 필수)
        try:
            self.roi_config = load_roi_config()
        except Exception as e:
            _log_monitor.error("Failed to load ROI config: %s — using full frame default", e)
            self._log(f"Failed to load ROI config: {e}")
            self._log("Using default ROI (full frame)")
            from ..analysis.roi import RoiConfig
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

        # 프로브 콜백: 캡처 대기 중 1초마다 경량 Thermal 체크
        baseline = self.roi_config.baseline_temp
        warning = self.roi_config.warning_delta

        def _probe_callback(max_temp: float) -> bool:
            if max_temp >= baseline + warning:
                _log_monitor.info("Probe triggered: %.1f°C >= %.1f°C (baseline %.0f + %d)",
                                  max_temp, baseline + warning, baseline, warning)
                self.capture.set_warning_mode(True)
                return True
            return False

        # 백그라운드 캡처 시작 (논스톱, 독립 스레드)
        self.capture = CaptureSession(
            cam_ip=self.cam_ip,
            mode="both",
            interval=self.capture_interval,
            save_dir=DATASET_DIR,
            log_callback=self._log,
            probe_callback=_probe_callback,
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
        _log_monitor.info("Sequencer stop requested (alarms=%d)", self._alarm_count)
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
def main():
    from .._encoding import setup_encoding
    setup_encoding()

    cfg = load_config()

    monitor = MonitorSequencer(
        cam_ip=os.environ.get("CAM_IP", cfg.camera.ip),
        capture_interval=float(os.environ.get("CAPTURE_INTERVAL", str(cfg.camera.capture_interval_sec))),
    )

    monitor.start()


if __name__ == "__main__":
    main()

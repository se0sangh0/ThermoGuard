"""
pipeline.py - 통합 분석 파이프라인

thermal_dataset의 모든 파일쌍을 순회하며:
  1. roi.py 로 ROI 온도 통계 추출
  2. threshold.py 로 상태 판정 + 알림 여부 결정
  3. overlay.py 로 오버레이 이미지 생성
  4. notifier.py 로 Telegram 알림 전송 (상태 변화 시)

사용법:
    python pipeline.py
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import Lock

import numpy as np

from ..config import load_config
from ..analysis.roi import load_roi_config, extract_roi_from_npy, extract_all_rois_from_npy, _get_roi_bounds_list
from ..analysis.threshold import (
    Status,
    MonitorState,
    evaluate_with_state,
)
from ..analysis.overlay import create_overlay, save_overlay
from ..analysis.notifier import send_alarm as send_telegram

_cfg = load_config()
DATASET_DIR = _cfg.paths.dataset_dir
OVERLAY_DIR = _cfg.paths.overlay_dir
MAX_WORKERS = os.cpu_count() or 4


def scan_pairs() -> list[dict]:
    """
    thermal_dataset에서 JPG + NPY + Visual JPG 파일쌍을 타임스탬프 순으로 반환.
    """
    if not os.path.isdir(DATASET_DIR):
        print(f"[pipeline] '{DATASET_DIR}' not found")
        return []

    files = os.listdir(DATASET_DIR)
    thermal_jpgs = {f.replace(".jpg", ""): f
                    for f in files if f.endswith(".jpg") and "_visual" not in f}
    npys = {f.replace("_thermal.npy", ""): f
            for f in files if f.endswith("_thermal.npy")}
    visual_jpgs = {f.replace("_visual.jpg", ""): f
                   for f in files if f.endswith("_visual.jpg")}

    bases = sorted(
        set(thermal_jpgs.keys()) & set(npys.keys()) & set(visual_jpgs.keys())
    )

    pairs = []
    for base in bases:
        pairs.append({
            "base": base,
            "thermal_jpg": os.path.join(DATASET_DIR, thermal_jpgs[base]),
            "visual_jpg": os.path.join(DATASET_DIR, visual_jpgs[base]),
            "npy": os.path.join(DATASET_DIR, npys[base]),
        })
    return pairs


def _process_single_pair(
    pair: dict,
    config,
    state: MonitorState,
    lock: Lock,
    idx: int,
    total: int,
) -> dict:
    """단일 쌍 처리 — ThreadPoolExecutor에서 병렬 호출됨."""
    base = pair["base"]
    try:
        roi_results = extract_all_rois_from_npy(pair["npy"], config)
        result = max(roi_results, key=lambda r: r.hot_temp_95)
        # 핫스팟 통합
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
        result.hotspot_centroids = unique_hotspots
        new_status, do_alarm = evaluate_with_state(
            hot_temp=result.hot_temp_95,
            max_temp=result.max_temp,
            mean_temp=result.mean_temp,
            baseline=config.baseline_temp,
            warning_delta=config.warning_delta,
            critical_delta=config.critical_delta,
            state=state,
            over_temp_pixels=result.over_temp_pixels,
            max_hotspot_size=result.max_hotspot_size,
        )

        prev_status = state.status
        with lock:
            state.status = new_status

        overlay_path = None
        if new_status != Status.NORMAL:
            overlay = create_overlay(
                thermal_jpg_path=pair["thermal_jpg"],
                visual_jpg_path=pair["visual_jpg"],
                roi_bounds=result.roi_bounds,
                max_temp=result.max_temp,
                mean_temp=result.mean_temp,
                hot_temp=result.hot_temp_95,
                status=new_status.value,
                hotspot_centroids=result.hotspot_centroids,
                roi_bounds_list=_get_roi_bounds_list(config),
                roi_names=[r.roi_name for r in roi_results] if len(roi_results) > 1 else None,
            )
            overlay_path = save_overlay(base, overlay)

        alarm_sent = False
        if do_alarm:
            try:
                sent = send_telegram(
                    image_path=overlay_path or pair["thermal_jpg"],
                    temp=result.hot_temp_95,
                    status=new_status.value,
                )
                if sent:
                    state.last_alarm_time = time.time()
                    alarm_sent = True
            except RuntimeError:
                pass
            except Exception:
                pass

        return {
            "idx": idx,
            "base": base,
            "hot_temp": result.hot_temp_95,
            "max_temp": result.max_temp,
            "status": new_status,
            "prev_status": prev_status,
            "do_alarm": do_alarm,
            "alarm_sent": alarm_sent,
        }
    except Exception as e:
        return {
            "idx": idx,
            "base": base,
            "error": str(e),
        }


def run_pipeline():
    """전체 파이프라인 실행 (병렬 처리)"""
    from .._encoding import setup_encoding
    setup_encoding()

    print("=" * 60)
    print("  Robot Thermal Monitoring - Pipeline")
    print(f"  Started  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Workers  : {MAX_WORKERS}")
    print("=" * 60)

    config = load_roi_config()
    state = MonitorState()
    pairs = scan_pairs()

    print(f"\n  Found {len(pairs)} image pairs")
    print(f"  ROI: ({config.x1},{config.y1})-({config.x2},{config.y2})")
    print(f"  Baseline: {config.baseline_temp}C, "
          f"Warning: +{config.warning_delta}C, "
          f"Critical: +{config.critical_delta}C")
    print(f"  Alarm cooldown: {state.alarm_cooldown // 60} min")
    print(f"  Overlay output: {OVERLAY_DIR}/")
    print()

    alarm_count = 0
    status_counts = {s.value: 0 for s in Status}
    lock = Lock()
    completed = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                _process_single_pair, pair, config, state, lock, i, len(pairs)
            ): i
            for i, pair in enumerate(pairs)
        }

        results = []
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            completed += 1

            if "error" in r:
                print(f"[{completed}/{len(pairs)}] {r['base']} | ERROR: {r['error']}")
            else:
                status_counts[r["status"].value] += 1
                transition = ""
                if r["do_alarm"]:
                    transition = f"  <<< {r['prev_status'].value} -> {r['status'].value} >>>"
                    alarm_count += 1
                print(f"[{completed}/{len(pairs)}] {r['base']} "
                      f"| 95th={r['hot_temp']:.1f}C max={r['max_temp']:.1f}C "
                      f"| {r['status'].value}{transition}")

    # 요약
    print()
    print("=" * 60)
    print("  Pipeline Complete")
    print(f"  Total pairs processed : {len(pairs)}")
    print(f"  Normal   : {status_counts['Normal']}")
    print(f"  Warning  : {status_counts['Warning']}")
    print(f"  Critical : {status_counts['Critical']}")
    print(f"  Alarms sent : {alarm_count}")
    print("=" * 60)


def main():
    run_pipeline()


if __name__ == "__main__":
    main()

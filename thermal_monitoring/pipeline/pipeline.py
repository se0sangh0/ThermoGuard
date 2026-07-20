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
from datetime import datetime

import numpy as np

from ..config import load_config
from ..analysis.roi import load_roi_config, extract_roi_from_npy
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


def run_pipeline():
    """전체 파이프라인 실행"""
    from .._encoding import setup_encoding
    setup_encoding()

    print("=" * 60)
    print("  Robot Thermal Monitoring - Pipeline")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
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

    for i, pair in enumerate(pairs):
        base = pair["base"]

        # 1. ROI 추출
        result = extract_roi_from_npy(pair["npy"], config)

        # 2. Threshold + 상태 판정
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
        state.status = new_status
        status_counts[new_status.value] += 1

        # 3. Overlay 생성 (Warning, Critical일 때만)
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
            )
            overlay_path = save_overlay(base, overlay)

        # 진행 상황 출력
        progress = f"[{i + 1}/{len(pairs)}]"
        transition = ""
        if do_alarm:
            transition = f"  <<< {prev_status.value} -> {new_status.value} >>>"
            alarm_count += 1

        print(f"{progress} {base} | 95th={result.hot_temp_95:.1f}C "
              f"max={result.max_temp:.1f}C | {new_status.value}{transition}")

        # 4. 알림 전송
        if do_alarm:
            try:
                send_telegram(
                    image_path=overlay_path or pair["thermal_jpg"],
                    temp=result.hot_temp_95,
                    status=new_status.value,
                )
                state.last_alarm_time = time.time()
            except RuntimeError:
                print("  ▲ Alarm skipped (Telegram not configured)")
            except Exception as e:
                print(f"  ▲ Alarm error: {e}")

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

"""test_overlay.py - 단일 이미지 오버레이 생성 테스트 (최신 데이터 사용)"""

import os

from _encoding import setup_encoding
setup_encoding()

from roi import load_roi_config, extract_roi_from_npy
from threshold import evaluate_threshold
from overlay import create_overlay, save_overlay

DATASET_DIR = "thermal_dataset"

# 최신 .npy 파일 찾기
npy_files = sorted(
    [f for f in os.listdir(DATASET_DIR) if f.endswith("_thermal.npy")]
)
if not npy_files:
    print("No .npy files found in thermal_dataset/")
    exit(1)

latest_npy = npy_files[-1]
base = latest_npy.replace("_thermal.npy", "")
thermal_jpg = os.path.join(DATASET_DIR, f"{base}.jpg")
visual_jpg = os.path.join(DATASET_DIR, f"{base}_visual.jpg")
npy_path = os.path.join(DATASET_DIR, latest_npy)

config = load_roi_config()
result = extract_roi_from_npy(npy_path, config)

status = evaluate_threshold(
    result.hot_temp_95,
    result.max_temp,
    config.baseline_temp,
    config.warning_delta,
    config.critical_delta,
    max_hotspot_size=result.max_hotspot_size,
)

print(f"File: {base}")
print(f"ROI bounds (thermal 640x480): {result.roi_bounds}")
print(f"Max: {result.max_temp:.1f}C  Mean: {result.mean_temp:.1f}C  95th: {result.hot_temp_95:.1f}C")
print(f"Status: {status.value}")
print(f"Hotspots: {len(result.hotspot_centroids)}")

overlay = create_overlay(
    thermal_jpg_path=thermal_jpg,
    visual_jpg_path=visual_jpg,
    roi_bounds=result.roi_bounds,
    max_temp=result.max_temp,
    mean_temp=result.mean_temp,
    hot_temp=result.hot_temp_95,
    status=status.value,
    hotspot_centroids=result.hotspot_centroids,
)

out = save_overlay(base, overlay)
print(f"Overlay saved: {out}")

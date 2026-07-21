"""
metadata.py - CSV 메타데이터 생성 및 업데이트

정상 JPG-NPY 파일쌍을 기준으로 metadata.csv를 생성/업데이트합니다.
ROI 분석 + Threshold 판정 결과를 포함합니다.
이미 CSV에 있는 레코드는 건너뜁니다.

사용법 (import):
    from metadata import run_metadata
    result = run_metadata(log_callback=print)
"""

import csv
import os

import numpy as np

from ..analysis.roi import load_roi_config, extract_roi_from_npy
from ..analysis.threshold import evaluate_threshold
from ..config import load_config
from ..logger import get_logger

_logger = get_logger("data.metadata")

SAVE_DIR = load_config().paths.dataset_dir

CSV_HEADER = [
    "image_id",
    "timestamp",
    "camera_id",
    "robot_id",
    "image_path",
    "thermal_path",
    "min_temp",
    "max_temp",
    "mean_temp",
    "hotspot_temp",
    "ambient_temp",
    "delta_temp",
    "alarm_level",
]


class MetadataResult:
    def __init__(self):
        self.total_pairs = 0
        self.existing = 0
        self.new = 0
        self.messages: list[str] = []


def _log(msg: str, log_callback=None, messages: list[str] | None = None):
    if log_callback:
        log_callback(msg)
    else:
        print(msg)
    if messages is not None:
        messages.append(msg)


def run_metadata(
    save_dir: str | None = None,
    log_callback=None,
) -> MetadataResult:
    result = MetadataResult()

    if save_dir is None:
        save_dir = load_config().paths.dataset_dir

    if not os.path.isdir(save_dir):
        _log(f"'{save_dir}' folder not found.", log_callback, result.messages)
        return result

    cfg = load_config()

    roi_config = load_roi_config()

    files = os.listdir(save_dir)
    jpgs = {f.replace(".jpg", ""): f
            for f in files if f.endswith(".jpg") and "_visual" not in f}
    npys = {f.replace("_thermal.npy", ""): f
            for f in files if f.endswith("_thermal.npy")}
    paired = sorted(set(jpgs.keys()) & set(npys.keys()))

    csv_path = os.path.join(save_dir, "metadata.csv")
    existing_ids: set[str] = set()
    if os.path.exists(csv_path):
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)
            existing_ids = {row[0] for row in reader if row}

    new_ids = sorted(set(paired) - existing_ids)

    result.total_pairs = len(paired)
    result.existing = len(existing_ids)
    result.new = len(new_ids)

    _log(f"Pairs: {result.total_pairs}  Existing: {result.existing}  "
         f"New: {result.new}", log_callback, result.messages)

    if not new_ids:
        _log("No new records to add.", log_callback, result.messages)
        return result

    write_header = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0

    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(CSV_HEADER)

        count = 0
        for base in new_ids:
            jpg_path = os.path.join(save_dir, jpgs[base])
            npy_path = os.path.join(save_dir, npys[base])
            thermal = np.load(npy_path)

            # ROI 분석 + Threshold 판정
            roi_result = extract_roi_from_npy(npy_path, roi_config)
            alarm_status = evaluate_threshold(
                roi_result.hot_temp_95,
                roi_result.max_temp,
                roi_config.baseline_temp,
                roi_config.warning_delta,
                roi_config.critical_delta,
                max_hotspot_size=roi_result.max_hotspot_size,
            )

            # hotspot_temp: centroids 중 최고 온도 (실제 발열원), 없으면 ROI max
            hotspot_temp = round(roi_result.max_temp, 2)
            if roi_result.hotspot_centroids:
                hotspot_temp = round(max(c[2] for c in roi_result.hotspot_centroids), 2)
            # 대안: ROI 95th percentile
            # hotspot_temp = round(float(roi_result.hot_temp_95), 2)

            # ambient: 전체 프레임 10th percentile (배경 온도 추정 — hotspot 영향 최소화)
            ambient = round(float(np.nanpercentile(thermal, 10)), 2)
            delta_temp = round(hotspot_temp - ambient, 2)

            row = [
                base,
                base[:14],
                cfg.identity.camera_id,
                cfg.identity.robot_id,
                jpgs[base],
                npys[base],
                round(float(np.nanmin(roi_result.roi_thermal)), 2),
                round(float(roi_result.max_temp), 2),                # ROI max
                round(float(roi_result.mean_temp), 2),               # ROI mean
                hotspot_temp,                                         # ROI 95th
                ambient,                                              # full frame 10th
                delta_temp,
                alarm_status.value,
            ]
            writer.writerow(row)
            count += 1

        _log(f"Added {count} records.", log_callback, result.messages)

    return result


if __name__ == "__main__":
    run_metadata()

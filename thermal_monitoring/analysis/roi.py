"""
roi.py - ROI 설정 및 온도 통계 추출

config.json에서 ROI 좌표를 불러와 .npy 온도 행렬에서
해당 영역의 온도 통계값을 계산합니다.
"""

import os
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

from config import load_config, RoiConfig as AppRoiConfig

# Thermal 이미지 vs .npy 해상도 차이 보정
DISPLAY_W = load_config().display.roi_display_width
DISPLAY_H = load_config().display.roi_display_height

# 하위 호환을 위한 wrapper
RoiConfig = AppRoiConfig


@dataclass
class RoiResult:
    roi_thermal: np.ndarray
    max_temp: float
    mean_temp: float
    hot_temp_95: float
    roi_bounds: tuple  # (x1, y1, x2, y2) - thermal image 기준
    over_temp_pixels: int = 0       # 기준 온도 초과 픽셀 수
    max_hotspot_size: int = 0       # 가장 큰 초과 클러스터 크기 (connected component)
    hotspot_centroids: list = field(default_factory=list)  # [(x, y, temp), ...] in 640x480 좌표계


def load_roi_config() -> RoiConfig:
    """config.json 에서 ROI 설정 불러오기 (하위 호환 wrapper)"""
    cfg = load_config()
    return RoiConfig(
        x1=cfg.roi.x1,
        y1=cfg.roi.y1,
        x2=cfg.roi.x2,
        y2=cfg.roi.y2,
        baseline_temp=cfg.roi.baseline_temp,
        warning_delta=cfg.roi.warning_delta,
        critical_delta=cfg.roi.critical_delta,
    )


def _scale_roi_to_npy(
    roi: RoiConfig, npy_shape: tuple
) -> tuple:
    """
    Thermal 이미지 좌표(640x480)를 .npy 행렬 좌표로 변환.
    .npy shape = (H, W) 이므로 (height, width) 순서에 주의.
    """
    npy_h, npy_w = npy_shape
    scale_x = npy_w / DISPLAY_W
    scale_y = npy_h / DISPLAY_H

    nx1 = int(roi.x1 * scale_x)
    ny1 = int(roi.y1 * scale_y)
    nx2 = int(roi.x2 * scale_x)
    ny2 = int(roi.y2 * scale_y)

    nx1 = max(0, min(nx1, npy_w))
    ny1 = max(0, min(ny1, npy_h))
    nx2 = max(0, min(nx2, npy_w))
    ny2 = max(0, min(ny2, npy_h))

    return ny1, ny2, nx1, nx2  # numpy 슬라이싱 순서: y1:y2, x1:x2


def extract_roi_from_npy(npy_path: str, config: Optional[RoiConfig] = None) -> RoiResult:
    """
    .npy 파일에서 ROI 영역 온도 통계 추출.

    Args:
        npy_path: .npy 파일 경로
        config: ROI 설정 (None이면 roi_config.json 자동 로드)

    Returns:
        RoiResult (roi_thermal, max_temp, mean_temp, hot_temp_95, roi_bounds)
    """
    if config is None:
        config = load_roi_config()

    thermal = np.load(npy_path).astype(np.float64)

    if thermal.ndim != 2:
        raise ValueError(f"Expected 2D thermal array, got shape {thermal.shape}")

    y1, y2, x1, x2 = _scale_roi_to_npy(config, thermal.shape)

    # 유효성 검사 - ROI가 너무 작으면 전체 프레임 사용
    if y2 <= y1 or x2 <= x1:
        print(f"[roi] WARNING: ROI too small ({config.x1},{config.y1})-({config.x2},{config.y2}), using full frame")
        roi = thermal
    else:
        roi = thermal[y1:y2, x1:x2]

    # NaN 제거
    valid = roi[~np.isnan(roi)]
    if len(valid) == 0:
        return RoiResult(
            roi_thermal=roi,
            max_temp=0.0,
            mean_temp=0.0,
            hot_temp_95=0.0,
            roi_bounds=(config.x1, config.y1, config.x2, config.y2),
        )

    # 국소 발열 클러스터 분석
    # baseline + warning_delta 기준 초과 픽셀을 connected components로 그룹화
    over_threshold = config.baseline_temp + config.warning_delta
    hotspot_mask = roi > over_threshold
    over_pixels = int(np.sum(hotspot_mask))

    MIN_HOTSPOT = 3  # 노이즈 필터링: 3픽셀 이상만 실제 발열로 인정
    max_cluster = 0
    centroids = []

    if over_pixels > 0:
        hotspot_uint8 = hotspot_mask.astype(np.uint8)
        num_labels, labels, stats, centroids_raw = cv2.connectedComponentsWithStats(
            hotspot_uint8, connectivity=8
        )
        # stats[0]은 배경(label 0) 전체 영역이므로 제외
        if num_labels > 1:
            max_cluster = int(stats[1:, cv2.CC_STAT_AREA].max())

        # ROI 내부 좌표 -> thermal 이미지(640x480) 좌표로 변환
        scale_back_x = DISPLAY_W / thermal.shape[1]
        scale_back_y = DISPLAY_H / thermal.shape[0]

        for label_id in range(1, num_labels):
            area = int(stats[label_id, cv2.CC_STAT_AREA])
            if area < MIN_HOTSPOT:
                continue
            # 클러스터별 마스크 및 온도
            cluster_mask = labels == label_id
            cluster_temps = roi[cluster_mask]
            cluster_max_temp = float(np.nanmax(cluster_temps))

            cx, cy = centroids_raw[label_id]
            # ROI 오프셋 적용 후 thermal 이미지 좌표계로 변환
            if roi is not thermal:
                cx += x1
                cy += y1
            tx = round(cx * scale_back_x)
            ty = round(cy * scale_back_y)
            centroids.append((tx, ty, cluster_max_temp))

    return RoiResult(
        roi_thermal=roi,
        max_temp=float(np.nanmax(valid)),
        mean_temp=float(np.nanmean(valid)),
        hot_temp_95=float(np.nanpercentile(valid, 95)),
        roi_bounds=(config.x1, config.y1, config.x2, config.y2),
        over_temp_pixels=over_pixels,
        max_hotspot_size=max_cluster,
        hotspot_centroids=centroids,
    )


# ------------------------------------------------------------
# 테스트
# ------------------------------------------------------------
if __name__ == "__main__":
    from _encoding import setup_encoding
    setup_encoding()

    cfg = load_config()
    dataset_dir = cfg.paths.dataset_dir

    print("=== ROI Test ===\n")
    config = load_roi_config()
    print(f"ROI bounds: ({config.x1}, {config.y1}) - ({config.x2}, {config.y2})")
    print(f"Baseline: {config.baseline_temp}C")
    print(f"Warning delta: {config.warning_delta}C")
    print(f"Critical delta: {config.critical_delta}C")

    if os.path.isdir(dataset_dir):
        npy_files = sorted(
            [f for f in os.listdir(dataset_dir) if f.endswith("_thermal.npy")]
        )
        if npy_files:
            npy_path = os.path.join(dataset_dir, npy_files[-1])
            print(f"\nTesting with: {npy_path}")
            result = extract_roi_from_npy(npy_path, config)
            print(f"  Max temp: {result.max_temp:.1f}C")
            print(f"  Mean temp: {result.mean_temp:.1f}C")
            print(f"  95th percentile: {result.hot_temp_95:.1f}C")
            print(f"  ROI shape: {result.roi_thermal.shape}")
            print(f"  Over-threshold pixels: {result.over_temp_pixels}")
            print(f"  Max hotspot cluster size: {result.max_hotspot_size}")
        else:
            print("\nNo .npy files found")
    else:
        print(f"\n'{dataset_dir}' directory not found")

"""thermal_dataset의 thermal/visual/npy 파일 쌍을 스캔·해석하는 공용 유틸.

대시보드·monitor·도구가 각자 dataset.glob(...)로 재구현하던 "쌍 찾기" 로직을
한곳으로 모은다. 파일명 규약:
    {base}.jpg            → thermal
    {base}_visual.jpg     → visual (정상 모드에서만 저장; 과열 모드는 thermal-only)
    {base}_thermal.npy    → 온도 행렬 (JPEG에서 지연 추출)
"""

import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

from ..capture.thermal_utils import extract_from_jpeg


def thermal_jpgs(dataset_dir) -> list[Path]:
    """정렬된 thermal JPG 경로 목록 (visual·overlay 제외). 폴더 없으면 빈 목록."""
    d = Path(dataset_dir)
    if not d.is_dir():
        return []
    return sorted(
        p for p in d.glob("*.jpg")
        if "_visual" not in p.name and "_overlay" not in p.name
    )


def visual_for(thermal: Path) -> Path:
    """thermal 경로에 대응하는 visual 경로 (존재 여부는 확인하지 않음)."""
    return thermal.with_name(f"{thermal.stem}_visual.jpg")


def npy_for(thermal: Path) -> Path:
    """thermal 경로에 대응하는 _thermal.npy 경로 (존재 여부는 확인하지 않음)."""
    return thermal.with_name(f"{thermal.stem}_thermal.npy")


def latest_complete_pair(dataset_dir) -> Optional[tuple[Path, Path]]:
    """visual이 존재하는 가장 최근 (thermal, visual) 쌍. 없으면 None.

    과열(경고) 모드에서는 최신 thermal에 visual이 없을 수 있으므로,
    visual이 있는 가장 최근 thermal까지 거슬러 찾는다. ROI·캘리브레이션
    도구가 과열 중에도 마지막 완성 쌍으로 동작할 수 있게 한다.
    """
    for thermal in reversed(thermal_jpgs(dataset_dir)):
        visual = visual_for(thermal)
        if visual.exists():
            return thermal, visual
    return None


def ensure_npy(thermal: Path) -> Path:
    """thermal에 대응하는 _thermal.npy가 없으면 exiftool 추출로 생성 후 경로 반환."""
    npy = npy_for(thermal)
    if not npy.exists():
        matrix, _ = extract_from_jpeg(str(thermal))
        np.save(npy, matrix)
    return npy


def capture_time_from_file(base: str, thermal: Path) -> datetime:
    """저장 파일명에 기록된 캡처 요청 시각을 읽는다.

    CaptureSession은 촬영 직전에 YYYYmmddHHMMSS_ffffff 형식으로
    파일명을 생성한다. 이전 형식의 파일도 표시할 수 있게 초 단위
    형식을 함께 지원하고, 형식이 다른 외부 파일은 수정 시각을 쓴다.
    """
    for timestamp_format in ("%Y%m%d%H%M%S_%f", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(base, timestamp_format)
        except ValueError:
            continue
    return datetime.fromtimestamp(thermal.stat().st_mtime)


def latest_analysis_pair(
    dataset_dir,
    *,
    visual_mode: bool = True,
    visual_grace_sec: float = 5.0,
) -> Optional[dict]:
    """가장 최근의 분석 대상 쌍(thermal, visual, npy)을 반환.

    Thermal과 Visual은 병렬 요청 후 각각 저장되므로 아주 짧은 시간
    동안 Thermal 파일만 존재할 수 있다. visual_mode일 때 visual_grace_sec
    동안은 직전 완성 쌍을 사용하고, 유예 시간이 경과하면 최신 쌍을 반환한다.
    (visual이 없으면 None으로 표시)

    Returns:
        {"base": stem, "thermal": Path, "visual": Path|None, "npy": Path}
        또는 대상 쌍이 없으면 None.
    """
    thermal_files = thermal_jpgs(dataset_dir)
    if not thermal_files:
        return None
    if visual_mode:
        newest = thermal_files[-1]
        newest_age = max(0.0, time.time() - newest.stat().st_mtime)
        if visual_for(newest).exists() or newest_age >= visual_grace_sec:
            thermal = newest
        else:
            thermal = next(
                (t for t in reversed(thermal_files[:-1]) if visual_for(t).exists()),
                None,
            )
        if thermal is None:
            return None
    else:
        thermal = thermal_files[-1]
    visual = visual_for(thermal)
    npy = ensure_npy(thermal)
    return {
        "base": thermal.stem,
        "thermal": thermal,
        "visual": visual if visual.exists() else None,
        "npy": npy,
    }

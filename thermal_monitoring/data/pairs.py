"""thermal_dataset의 thermal/visual/npy 파일 쌍을 스캔·해석하는 공용 유틸.

대시보드·monitor·도구가 각자 dataset.glob(...)로 재구현하던 "쌍 찾기" 로직을
한곳으로 모은다. 파일명 규약:
    {base}.jpg            → thermal
    {base}_visual.jpg     → visual (정상 모드에서만 저장; 과열 모드는 thermal-only)
    {base}_thermal.npy    → 온도 행렬 (JPEG에서 지연 추출)
"""

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

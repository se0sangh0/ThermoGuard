"""
checking.py - 데이터셋 무결성 검사 및 복구

- NPY가 누락된 JPG → 온도 행렬 자동 추출
- JPG가 없는 고아 NPY → 삭제

사용법 (import):
    from checking import run_check, CheckResult
    result = run_check(log_callback=print)
"""

import os

import numpy as np

from ..capture.thermal_utils import extract_from_jpeg
from ..config import load_config

SAVE_DIR = load_config().paths.dataset_dir


class CheckResult:
    def __init__(self):
        self.total_jpg = 0
        self.total_npy = 0
        self.paired = 0
        self.missing_npy = 0
        self.orphan_npy = 0
        self.fixed = 0
        self.failed = 0
        self.removed = 0
        self.messages: list[str] = []


# GUI-UPDATE: Browse로 선택한 Dataset을 검사하도록 전역 SAVE_DIR 의존을 제거했다.
def _scan(save_dir: str):
    files = os.listdir(save_dir)
    jpgs = {f.replace(".jpg", ""): f
            for f in files if f.endswith(".jpg") and "_visual" not in f}
    npys = {f.replace("_thermal.npy", ""): f
            for f in files if f.endswith("_thermal.npy")}
    return jpgs, npys


def _log(msg: str, log_callback=None, messages: list[str] | None = None):
    if log_callback:
        log_callback(msg)
    else:
        print(msg)
    if messages is not None:
        messages.append(msg)


def run_check(
    save_dir: str = SAVE_DIR,
    log_callback=None,
) -> CheckResult:
    result = CheckResult()

    if not os.path.isdir(save_dir):
        _log(f"'{save_dir}' folder not found.", log_callback, result.messages)
        return result

    jpg_bases, npy_bases = _scan(save_dir)
    paired = set(jpg_bases.keys()) & set(npy_bases.keys())
    missing = set(jpg_bases.keys()) - set(npy_bases.keys())
    orphan = set(npy_bases.keys()) - set(jpg_bases.keys())

    result.total_jpg = len(jpg_bases)
    result.total_npy = len(npy_bases)
    result.paired = len(paired)
    result.missing_npy = len(missing)
    result.orphan_npy = len(orphan)

    _log(f"=== Dataset Integrity Check ===", log_callback, result.messages)
    _log(f"JPG: {result.total_jpg}  NPY: {result.total_npy}  "
         f"Pairs: {result.paired}  Missing NPY: {result.missing_npy}  "
         f"Orphan NPY: {result.orphan_npy}", log_callback, result.messages)

    # 1. NPY 누락 복구
    if missing:
        _log(f"\n[Recovering {len(missing)} missing NPY files...]", log_callback, result.messages)
        for base in sorted(missing):
            jpg_path = os.path.join(save_dir, jpg_bases[base])
            npy_path = os.path.join(save_dir, base + "_thermal.npy")
            try:
                thermal, _ = extract_from_jpeg(jpg_path)
                np.save(npy_path, thermal)
                _log(f"  OK {npy_path} "
                     f"(min={np.nanmin(thermal):.1f}C, max={np.nanmax(thermal):.1f}C)",
                     log_callback, result.messages)
                result.fixed += 1
            except Exception as e:
                _log(f"  FAIL {jpg_bases[base]} - {e}", log_callback, result.messages)
                result.failed += 1

    # 2. 고아 NPY 삭제
    if orphan:
        _log(f"\n[Removing {len(orphan)} orphan NPY files...]", log_callback, result.messages)
        for base in sorted(orphan):
            npy_path = os.path.join(save_dir, npy_bases[base])
            os.remove(npy_path)
            _log(f"  REMOVED {npy_path}", log_callback, result.messages)
            result.removed += 1

    # GUI-UPDATE: 결과 요약은 복구·삭제가 반영된 최종 파일 상태로 표시한다.
    result.total_npy += result.fixed - result.removed
    result.paired += result.fixed

    _log(f"\nDone. Fixed: {result.fixed}, Failed: {result.failed}, "
         f"Removed: {result.removed}", log_callback, result.messages)

    return result


if __name__ == "__main__":
    run_check()

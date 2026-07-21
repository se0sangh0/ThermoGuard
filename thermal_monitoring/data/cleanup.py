"""
cleanup.py - 불필요 데이터셋 정리 모듈

오래된 JPG/NPY 파일쌍, 고아 오버레이 이미지, 실패 복구 흔적을 삭제합니다.
monitor.py 실시간 감시나 tools.py GUI에서 백그라운드로 동작합니다.

사용법 (import):
    from cleanup import run_cleanup, CleanupResult
    result = run_cleanup(retention_days=7, log_callback=print)

    # 백그라운드 모드 (monitor.py / tools.py):
    from cleanup import run_cleanup_if_due
    run_cleanup_if_due(save_dir=..., retention_days=7)

설정:
    config.json의 monitoring.cleanup_retention_days (기본 7일)
"""

import csv
import os
import time
import glob
from datetime import datetime, timedelta
from dataclasses import dataclass, field

from ..logger import get_logger

_logger = get_logger("data.cleanup")

_RELATIVE_SAVE_DIR = "thermal_dataset"
_DEFAULT_RETENTION_DAYS = 7


@dataclass
class CleanupResult:
    removed_pairs: int = 0          # 삭제된 Normal JPG+NPY 쌍
    preserved_alarms: int = 0       # Warning/Critical 이력으로 보존된 쌍
    removed_orphan_npy: int = 0     # JPG 없는 NPY
    removed_orphan_jpg: int = 0     # NPY 없는 JPG
    removed_overlay: int = 0        # 대응 쌍 없는 오버레이
    freed_bytes: int = 0            # 확보된 디스크 공간
    errors: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


def _log(msg: str, log_callback=None, messages: list[str] | None = None):
    if log_callback:
        log_callback(msg)
    else:
        print(msg)
    if messages is not None:
        messages.append(msg)


def _parse_timestamp_from_filename(filename: str) -> datetime | None:
    """파일명에서 14자리 타임스탬프 파싱 (YYYYMMDDHHMMSS)"""
    base = os.path.basename(filename)
    name = os.path.splitext(base)[0]
    name = name.replace("_thermal", "").replace("_visual", "").replace("_overlay", "")
    timestamp = name[:14]
    if len(timestamp) == 14 and timestamp.isdigit():
        try:
            return datetime.strptime(timestamp, "%Y%m%d%H%M%S")
        except ValueError:
            pass
    return None


def _load_alarm_bases(save_dir: str) -> set[str]:
    """metadata.csv에서 Warning/Critical 이력이 있는 image_id 집합을 반환."""
    csv_path = os.path.join(save_dir, "metadata.csv")
    if not os.path.isfile(csv_path):
        return set()
    alarm_bases = set()
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            headers = next(reader, None)
            if headers is None:
                return set()
            try:
                idx_id = headers.index("image_id")
                idx_alarm = headers.index("alarm_level")
            except ValueError:
                return set()
            for row in reader:
                if len(row) <= max(idx_id, idx_alarm):
                    continue
                if row[idx_alarm] in ("Warning", "Critical"):
                    alarm_bases.add(row[idx_id])
    except Exception:
        pass
    return alarm_bases


def _get_file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def run_cleanup(
    save_dir: str | None = None,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
    log_callback=None,
) -> CleanupResult:
    """
    오래된 파일과 고아 데이터를 정리합니다.

    삭제 대상:
      1. retention_days보다 오래된 JPG+NPY 쌍
      2. JPG가 없는 고아 NPY
      3. NPY가 없는 고아 JPG
      4. 대응하는 원본 쌍이 없는 오버레이 이미지
    """
    result = CleanupResult()

    if save_dir is None:
        try:
            from ..config import load_config
            save_dir = load_config().paths.dataset_dir
        except Exception:
            save_dir = _RELATIVE_SAVE_DIR

    if not os.path.isdir(save_dir):
        _logger.info("Skip cleanup: '%s' not found", save_dir)
        _log(f"[cleanup] '{save_dir}' not found — skipping.", log_callback, result.messages)
        return result

    cutoff = datetime.now() - timedelta(days=retention_days)

    _log(f"[cleanup] Retention: {retention_days} days (cutoff: {cutoff.strftime('%Y-%m-%d %H:%M')})",
         log_callback, result.messages)
    _log(f"[cleanup] Scanning: {save_dir}", log_callback, result.messages)

    files = os.listdir(save_dir)

    # 분류
    thermal_jpgs = {f.replace(".jpg", ""): f for f in files
                    if f.endswith(".jpg") and "_visual" not in f and "_overlay" not in f}
    npys = {f.replace("_thermal.npy", ""): f for f in files if f.endswith("_thermal.npy")}
    visual_jpgs = {f.replace("_visual.jpg", ""): f for f in files if f.endswith("_visual.jpg")}
    overlays = [f for f in files if f.endswith("_overlay.jpg")]

    # 1. 오래된 Normal 쌍만 삭제 (Warning/Critical 이력 보존)
    alarm_bases = _load_alarm_bases(save_dir)
    paired = set(thermal_jpgs.keys()) & set(npys.keys())
    old_normal = []
    for base in paired:
        ts = _parse_timestamp_from_filename(base)
        if ts and ts < cutoff and base not in alarm_bases:
            old_normal.append(base)

    old_alarm_skipped = 0
    for base in paired:
        ts = _parse_timestamp_from_filename(base)
        if ts and ts < cutoff and base in alarm_bases:
            old_alarm_skipped += 1

    if old_alarm_skipped > 0:
        result.preserved_alarms = old_alarm_skipped
        _log(f"[cleanup] Skipped {old_alarm_skipped} expired pair(s) with Warning/Critical history",
             log_callback, result.messages)
        _logger.info("cleanup: skipped %d expired pair(s) with alarm history", old_alarm_skipped)

    if old_normal:
        _log(f"[cleanup] Removing {len(old_normal)} expired Normal pair(s)...", log_callback, result.messages)
        for base in old_normal:
            paths = [
                os.path.join(save_dir, thermal_jpgs[base]),
                os.path.join(save_dir, npys[base]),
            ]
            if base in visual_jpgs:
                paths.append(os.path.join(save_dir, visual_jpgs[base]))
            for p in paths:
                try:
                    result.freed_bytes += _get_file_size(p)
                    os.remove(p)
                    result.removed_pairs += 1
                except OSError as e:
                    result.errors.append(f"Failed to remove {p}: {e}")
            _log(f"  REMOVED {base}", log_callback, result.messages)

    # 2. 고아 NPY (JPG 없는)
    orphan_npy = set(npys.keys()) - set(thermal_jpgs.keys())
    for base in orphan_npy:
        p = os.path.join(save_dir, npys[base])
        try:
            result.freed_bytes += _get_file_size(p)
            os.remove(p)
            result.removed_orphan_npy += 1
        except OSError as e:
            result.errors.append(f"Failed to remove orphan NPY {p}: {e}")
    if orphan_npy:
        _log(f"[cleanup] Removed {len(orphan_npy)} orphan NPY(s)", log_callback, result.messages)

    # 3. 고아 JPG (NPY 없는)
    orphan_jpg = set(thermal_jpgs.keys()) - set(npys.keys())
    for base in orphan_jpg:
        p = os.path.join(save_dir, thermal_jpgs[base])
        try:
            result.freed_bytes += _get_file_size(p)
            os.remove(p)
            result.removed_orphan_jpg += 1
        except OSError as e:
            result.errors.append(f"Failed to remove orphan JPG {p}: {e}")
    if orphan_jpg:
        _log(f"[cleanup] Removed {len(orphan_jpg)} orphan JPG(s)", log_callback, result.messages)

    # 4. 대응 쌍 없는 오버레이
    overlay_dir = os.path.join(save_dir, "overlay")
    if os.path.isdir(overlay_dir):
        for f in os.listdir(overlay_dir):
            if not f.endswith("_overlay.jpg"):
                continue
            base = f.replace("_overlay.jpg", "")
            if base not in paired and base not in thermal_jpgs:
                p = os.path.join(overlay_dir, f)
                try:
                    result.freed_bytes += _get_file_size(p)
                    os.remove(p)
                    result.removed_overlay += 1
                except OSError as e:
                    result.errors.append(f"Failed to remove overlay {p}: {e}")
        if result.removed_overlay > 0:
            _log(f"[cleanup] Removed {result.removed_overlay} orphan overlay(s)", log_callback, result.messages)

    # 오류 로그
    for err in result.errors:
        _log(f"[cleanup] ERROR: {err}", log_callback, result.messages)

    freed_mb = result.freed_bytes / (1024 * 1024)
    summary = (
        f"[cleanup] Done — Normal pairs removed: {result.removed_pairs}, "
        f"alarm history preserved: {result.preserved_alarms}, "
        f"orphan NPY: {result.removed_orphan_npy}, "
        f"orphan JPG: {result.removed_orphan_jpg}, "
        f"orphan overlay: {result.removed_overlay}, "
        f"freed: {freed_mb:.1f} MB"
    )
    _log(summary, log_callback, result.messages)

    return result


# ════════════════════════════════════════════════════════════
# 백그라운드 모드 — 호출 시점 기준으로 주기적 정리
# ════════════════════════════════════════════════════════════

_last_cleanup_time: float = 0.0
_CLEANUP_INTERVAL_SEC = 3600.0  # 1시간


def run_cleanup_if_due(
    save_dir: str | None = None,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
    log_callback=None,
) -> CleanupResult | None:
    """
    마지막 정리로부터 CLEANUP_INTERVAL_SEC 이상 지났으면 정리 실행.
    그렇지 않으면 None 반환 (건너뜀).
    """
    global _last_cleanup_time
    now = time.time()
    if (now - _last_cleanup_time) < _CLEANUP_INTERVAL_SEC:
        return None
    _last_cleanup_time = now
    return run_cleanup(save_dir=save_dir, retention_days=retention_days, log_callback=log_callback)

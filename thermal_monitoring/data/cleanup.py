"""
cleanup.py - 불필요 데이터셋 정리 모듈

오래된 JPG/NPY 파일쌍, 고아 오버레이 이미지, 실패 복구 흔적을 삭제합니다.
공장 대시보드의 자동 타이머에서는 호출하지 않습니다. 백업 확인과 명시적인
dataset marker 생성 뒤 승인된 유지보수 절차에서만 사용합니다.

사용법 (import):
    from cleanup import run_cleanup, CleanupResult
    result = run_cleanup(retention_days=7, log_callback=print)

    # 승인된 유지보수에서 주기 판단이 필요한 경우:
    from cleanup import run_cleanup_if_due
    run_cleanup_if_due(save_dir=..., retention_days=7)

    # 승인된 유지보수에서 Normal 쌍 제거가 필요한 경우:
    from cleanup import remove_normal_pairs_if_due
    remove_normal_pairs_if_due(save_dir=...)

설정:
    config.json의 monitoring.cleanup_retention_days (기본 2일)
"""

import csv
import os
import time
import stat
import tempfile
from datetime import datetime, timedelta
from dataclasses import dataclass, field

from ..logger import get_logger

_logger = get_logger("data.cleanup")

_RELATIVE_SAVE_DIR = "thermal_dataset"
_DEFAULT_RETENTION_DAYS = 2
_DATASET_MARKER = ".thermoguard-dataset"
_REPOSITORY_ROOT = os.path.realpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
)
_HOME_DIRECTORY = os.path.realpath(os.path.expanduser("~"))


class DatasetSafetyError(ValueError):
    """Raised when a directory is not safe to mark as a ThermoGuard dataset."""


def _resolve_dataset_path(path: str | os.PathLike[str]) -> str:
    """Resolve a user-supplied dataset path without following it during scans."""
    try:
        raw_path = os.fspath(path)
        if not isinstance(raw_path, str):
            raise TypeError("dataset path must be text")
        if not raw_path:
            raise ValueError("empty dataset path")
        return os.path.realpath(os.path.abspath(os.path.expanduser(raw_path)))
    except (TypeError, ValueError) as exc:
        raise DatasetSafetyError("dataset path must be a non-empty filesystem path") from exc


def _unsafe_dataset_reason(path: str) -> str | None:
    """Return the reason ``path`` must never be used as a cleanup target."""
    if path == os.path.sep:
        return "filesystem root is never a valid dataset directory"
    if path == _HOME_DIRECTORY:
        return "the current user's home directory is never a valid dataset directory"
    if path == _REPOSITORY_ROOT:
        return "the ThermoGuard repository root is never a valid dataset directory"
    if os.path.ismount(path):
        return "a filesystem mount root is never a valid dataset directory"
    if not os.path.isdir(path):
        return "target directory does not exist"
    return None


def _marker_reason(path: str) -> str | None:
    """Return a refusal reason unless ``path`` has a regular dataset marker file."""
    marker_path = os.path.join(path, _DATASET_MARKER)
    try:
        marker_stat = os.lstat(marker_path)
    except FileNotFoundError:
        return f"missing explicit dataset marker '{_DATASET_MARKER}'"
    except OSError as exc:
        return f"cannot verify dataset marker '{_DATASET_MARKER}': {exc}"

    if not stat.S_ISREG(marker_stat.st_mode):
        return f"dataset marker '{_DATASET_MARKER}' must be a regular file"
    return None


def _approved_dataset_dir(save_dir: str | os.PathLike[str]) -> tuple[str | None, str | None]:
    """Resolve and validate a directory before any destructive dataset operation."""
    try:
        resolved = _resolve_dataset_path(save_dir)
    except DatasetSafetyError as exc:
        return None, str(exc)

    reason = _unsafe_dataset_reason(resolved)
    if reason:
        return None, reason
    reason = _marker_reason(resolved)
    if reason:
        return None, reason
    return resolved, None


def initialize_dataset_marker(path: str | os.PathLike[str]) -> str:
    """Explicitly mark an existing safe directory as eligible for dataset cleanup.

    This helper is deliberately never called by normal capture or cleanup flows.
    An operator must run it for the intended dataset directory before destructive
    retention jobs are allowed to remove files.
    """
    resolved = _resolve_dataset_path(path)
    reason = _unsafe_dataset_reason(resolved)
    if reason:
        raise DatasetSafetyError(f"Refused to mark '{resolved}': {reason}")

    marker_path = os.path.join(resolved, _DATASET_MARKER)
    marker_reason = _marker_reason(resolved)
    if marker_reason is None:
        return marker_path
    if not marker_reason.startswith("missing explicit dataset marker"):
        raise DatasetSafetyError(f"Refused to mark '{resolved}': {marker_reason}")

    fd, temporary_marker = tempfile.mkstemp(
        prefix=f".{_DATASET_MARKER}.",
        dir=resolved,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("ThermoGuard dataset cleanup marker\n")
            stream.flush()
            os.fsync(stream.fileno())
        # os.replace makes the completed marker visible atomically.
        os.replace(temporary_marker, marker_path)
    except Exception:
        try:
            os.unlink(temporary_marker)
        except FileNotFoundError:
            pass
        raise
    return marker_path


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
    refused: bool = False           # 안전 대상 검증 실패로 작업 자체를 거부했는지


def _log(msg: str, log_callback=None, messages: list[str] | None = None):
    if log_callback:
        log_callback(msg)
    else:
        print(msg)
    if messages is not None:
        messages.append(msg)


def _refuse_cleanup(
    result: CleanupResult,
    operation: str,
    save_dir: str | os.PathLike[str],
    reason: str,
    log_callback=None,
) -> CleanupResult:
    """Report a refusal before a cleanup operation can scan or remove files."""
    message = (
        f"[cleanup] REFUSED {operation} for '{save_dir}': {reason}. "
        f"Initialize the intended dataset explicitly with "
        f"initialize_dataset_marker(path) before enabling destructive cleanup."
    )
    result.refused = True
    result.errors.append(message)
    _logger.error("%s", message)
    _log(message, log_callback, result.messages)
    return result


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


def _scan_recursive(save_dir: str):
    """재귀적으로 데이터셋 디렉토리를 스캔. dict 값은 전체 경로."""
    thermal_jpgs: dict[str, str] = {}
    npys: dict[str, str] = {}
    visual_jpgs: dict[str, str] = {}
    overlays: list[str] = []
    try:
        for root, _dirs, files in os.walk(save_dir):
            for name in files:
                full = os.path.join(root, name)
                if name.endswith("_thermal.npy"):
                    npys[name.replace("_thermal.npy", "")] = full
                elif name.endswith("_visual.jpg"):
                    visual_jpgs[name.replace("_visual.jpg", "")] = full
                elif name.endswith("_overlay.jpg"):
                    overlays.append(full)
                elif name.endswith(".jpg"):
                    thermal_jpgs[name.replace(".jpg", "")] = full
    except OSError:
        pass
    return thermal_jpgs, npys, visual_jpgs, overlays


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

    approved_save_dir, refusal_reason = _approved_dataset_dir(save_dir)
    if refusal_reason:
        return _refuse_cleanup(result, "retention cleanup", save_dir, refusal_reason, log_callback)
    save_dir = approved_save_dir

    if not os.path.isdir(save_dir):
        _logger.info("Skip cleanup: '%s' not found", save_dir)
        _log(f"[cleanup] '{save_dir}' not found — skipping.", log_callback, result.messages)
        return result

    cutoff = datetime.now() - timedelta(days=retention_days)

    _log(f"[cleanup] Retention: {retention_days} days (cutoff: {cutoff.strftime('%Y-%m-%d %H:%M')})",
         log_callback, result.messages)
    _log(f"[cleanup] Scanning: {save_dir}", log_callback, result.messages)

    thermal_jpgs, npys, visual_jpgs, overlays = _scan_recursive(save_dir)

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
                thermal_jpgs[base],
                npys[base],
            ]
            if base in visual_jpgs:
                paths.append(visual_jpgs[base])
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
        p = npys[base]
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
        p = thermal_jpgs[base]
        try:
            result.freed_bytes += _get_file_size(p)
            os.remove(p)
            result.removed_orphan_jpg += 1
        except OSError as e:
            result.errors.append(f"Failed to remove orphan JPG {p}: {e}")
    if orphan_jpg:
        _log(f"[cleanup] Removed {len(orphan_jpg)} orphan JPG(s)", log_callback, result.messages)

    # 4. 대응 쌍 없는 오버레이
    for p in overlays:
        base = os.path.basename(p).replace("_overlay.jpg", "")
        if base not in paired and base not in thermal_jpgs:
            try:
                result.freed_bytes += _get_file_size(p)
                os.remove(p)
                result.removed_overlay += 1
            except OSError as e:
                result.errors.append(f"Failed to remove overlay {p}: {e}")
    if result.removed_overlay > 0:
        _log(f"[cleanup] Removed {result.removed_overlay} orphan overlay(s)", log_callback, result.messages)

    # 빈 서브디렉토리 정리 (날짜/블록 폴더)
    _remove_empty_subdirs(save_dir, log_callback, result.messages)

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
# 12시간 Normal 쌍 제거 프로브
# ════════════════════════════════════════════════════════════

_last_normal_removal_time: float = 0.0
_NORMAL_REMOVAL_INTERVAL_SEC = 12 * 3600  # 12시간


def run_remove_normal_pairs(
    save_dir: str | None = None,
    log_callback=None,
) -> CleanupResult:
    """Normal 상태인 모든 쌍(thermal JPG + NPY + visual JPG)을 삭제.
    
    Warning/Critical 이력이 있는 image_id만 보존하고,
    나머지 Normal 쌍은 모두 제거한다.
    """
    result = CleanupResult()

    if save_dir is None:
        try:
            from ..config import load_config
            save_dir = load_config().paths.dataset_dir
        except Exception:
            save_dir = _RELATIVE_SAVE_DIR

    approved_save_dir, refusal_reason = _approved_dataset_dir(save_dir)
    if refusal_reason:
        return _refuse_cleanup(result, "Normal-pair cleanup", save_dir, refusal_reason, log_callback)
    save_dir = approved_save_dir

    if not os.path.isdir(save_dir):
        _logger.info("Skip normal-pair removal: '%s' not found", save_dir)
        return result

    alarm_bases = _load_alarm_bases(save_dir)
    thermal_jpgs, npys, visual_jpgs, _overlays = _scan_recursive(save_dir)
    paired = set(thermal_jpgs.keys()) & set(npys.keys())

    normal_bases = sorted(paired - alarm_bases)
    if not normal_bases:
        _log("[cleanup] No Normal pairs to remove.", log_callback, result.messages)
        return result

    _log(f"[cleanup] Normal-pair cleanup: removing {len(normal_bases)} Normal pair(s), "
         f"preserving {len(alarm_bases & paired)} alarm pair(s).",
         log_callback, result.messages)
    _logger.info("normal-pair cleanup: %d Normal pairs to remove, %d alarm pairs preserved",
                 len(normal_bases), len(alarm_bases & paired))

    for base in normal_bases:
        paths = [
            thermal_jpgs[base],
            npys[base],
        ]
        if base in visual_jpgs:
            paths.append(visual_jpgs[base])
        for p in paths:
            try:
                result.freed_bytes += _get_file_size(p)
                os.remove(p)
                result.removed_pairs += 1
            except OSError as e:
                result.errors.append(f"Failed to remove {p}: {e}")

    for err in result.errors:
        _log(f"[cleanup] ERROR: {err}", log_callback, result.messages)

    # 빈 서브디렉토리 정리
    _remove_empty_subdirs(save_dir, log_callback, result.messages)

    freed_mb = result.freed_bytes / (1024 * 1024)
    _log(f"[cleanup] Normal-pair removal done — {len(normal_bases)} pairs, "
         f"freed: {freed_mb:.1f} MB",
         log_callback, result.messages)

    return result


def remove_normal_pairs_if_due(
    save_dir: str | None = None,
    log_callback=None,
) -> CleanupResult | None:
    """마지막 Normal 쌍 제거로부터 12시간 이상 지났으면 실행. 그렇지 않으면 None."""
    global _last_normal_removal_time
    now = time.time()
    if (now - _last_normal_removal_time) < _NORMAL_REMOVAL_INTERVAL_SEC:
        return None
    result = run_remove_normal_pairs(save_dir=save_dir, log_callback=log_callback)
    if not result.refused:
        _last_normal_removal_time = now
    return result


# ════════════════════════════════════════════════════════════
# 빈 서브디렉토리 정리 헬퍼
# ════════════════════════════════════════════════════════════

def _remove_empty_subdirs(save_dir: str, log_callback=None, messages: list[str] | None = None):
    """날짜/블록 서브디렉토리 중 빈 폴더를 삭제 (root save_dir과 overlay 제외)."""
    try:
        removed = 0
        for root, dirs, files in os.walk(save_dir, topdown=False):
            if root == save_dir or os.path.basename(root) == "overlay":
                continue
            if not files and not dirs:
                try:
                    os.rmdir(root)
                    removed += 1
                except OSError:
                    pass
        if removed > 0:
            _log(f"[cleanup] Removed {removed} empty subdirector{'y' if removed == 1 else 'ies'}",
                 log_callback, messages)
    except OSError:
        pass


# ════════════════════════════════════════════════════════════
# 백그라운드 모드 — 호출 시점 기준으로 주기적 정리
# ════════════════════════════════════════════════════════════

_last_cleanup_time: float = 0.0
_CLEANUP_INTERVAL_SEC = 900.0  # 15분


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
    result = run_cleanup(save_dir=save_dir, retention_days=retention_days, log_callback=log_callback)
    if not result.refused:
        _last_cleanup_time = now
    return result

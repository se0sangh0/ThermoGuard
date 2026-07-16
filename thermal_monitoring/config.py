"""
config.py - 통합 설정 모듈 (Unified Config Module)

모든 설정을 config.json 하나로 관리합니다.
최초 실행 시 roi_config.json, experiment_config.json에서 자동 이관(migration)합니다.

사용법:
    from config import load_config

    cfg = load_config()
    print(cfg.roi.baseline_temp)   # 23.0
    print(cfg.camera.ip)             # 192.168.0.51
"""

import json
import os
import shutil
from dataclasses import dataclass, field, asdict
from typing import Optional

CONFIG_PATH = "config.json"
OLD_ROI_CONFIG = "roi_config.json"
OLD_EXP_CONFIG = "experiment_config.json"


# ════════════════════════════════════════════════════════════
# Dataclass definitions
# ════════════════════════════════════════════════════════════

@dataclass
class CameraConfig:
    ip: str = "192.168.0.51"
    capture_interval_sec: float = 1.0


@dataclass
class IdentityConfig:
    camera_id: str = "CAM-01"
    robot_id: str = "Robot-01"


@dataclass
class RoiConfig:
    """ROI 설정 — roi.py의 RoiConfig와 호환되는 필드명 유지"""
    x1: int = 0
    y1: int = 0
    x2: int = 640
    y2: int = 480
    baseline_temp: float = 35.0
    warning_delta: float = 15.0
    critical_delta: float = 25.0


@dataclass
class MonitoringConfig:
    process_interval_sec: float = 2.0
    integrity_interval_sec: float = 60.0
    metadata_interval_sec: float = 120.0
    max_processed_cache: int = 10000
    alarm_cooldown_sec: float = 600.0


@dataclass
class HotspotConfig:
    min_size: int = 3
    min_size_max: int = 10


@dataclass
class PathsConfig:
    dataset_dir: str = "thermal_dataset"
    overlay_dir: str = "thermal_dataset/overlay"
    homography_path: str = "thermal_to_rgb.npy"


@dataclass
class DisplayConfig:
    roi_display_width: int = 640
    roi_display_height: int = 480
    display_width: int = 800


@dataclass
class ToolsConfig:
    exiftool_path: str = ""
    mode: str = "both"


@dataclass
class AppConfig:
    camera: CameraConfig = field(default_factory=CameraConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    roi: RoiConfig = field(default_factory=RoiConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    hotspot: HotspotConfig = field(default_factory=HotspotConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)


# ════════════════════════════════════════════════════════════
# Internal helpers
# ════════════════════════════════════════════════════════════

def _dict_to_dataclass(d: dict, dc: type):
    """중첩 dict를 dataclass 인스턴스로 변환"""
    field_names = {f.name for f in dc.__dataclass_fields__.values()}
    kwargs = {}
    for k, v in d.items():
        if k in field_names:
            kwargs[k] = v
    return dc(**kwargs)


def _from_dict(raw: dict) -> AppConfig:
    return AppConfig(
        camera=_dict_to_dataclass(raw.get("camera", {}), CameraConfig),
        identity=_dict_to_dataclass(raw.get("identity", {}), IdentityConfig),
        roi=_dict_to_dataclass(raw.get("roi", {}), RoiConfig),
        monitoring=_dict_to_dataclass(raw.get("monitoring", {}), MonitoringConfig),
        hotspot=_dict_to_dataclass(raw.get("hotspot", {}), HotspotConfig),
        paths=_dict_to_dataclass(raw.get("paths", {}), PathsConfig),
        display=_dict_to_dataclass(raw.get("display", {}), DisplayConfig),
        tools=_dict_to_dataclass(raw.get("tools", {}), ToolsConfig),
    )


def _backup_and_remove(filepath: str) -> None:
    """파일을 .bak으로 백업"""
    if os.path.isfile(filepath):
        bak_path = filepath + ".bak"
        try:
            shutil.move(filepath, bak_path)
            print(f"[config] Migrated: {filepath} → {bak_path}")
        except OSError:
            pass


# ════════════════════════════════════════════════════════════
# Load / Save
# ════════════════════════════════════════════════════════════

_cached_config: Optional[AppConfig] = None


def load_config(config_path: str = CONFIG_PATH, force_reload: bool = False) -> AppConfig:
    """
    통합 설정을 로드. config.json이 없으면 기존 파일에서 자동 이관.

    Args:
        config_path: 설정 파일 경로
        force_reload: True이면 캐시를 무시하고 다시 읽음
    """
    global _cached_config

    if _cached_config is not None and not force_reload:
        return _cached_config

    # 이미 config.json이 있으면 바로 로드
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            _cached_config = _from_dict(raw)
            return _cached_config
        except (json.JSONDecodeError, TypeError) as e:
            print(f"[config] WARNING: Failed to parse {config_path}: {e}")
            print("[config] Falling back to defaults + migration")

    # config.json 없음 → 기본값 + 이전 파일에서 이관
    cfg = AppConfig()

    migrated = False

    # roi_config.json → AppConfig.roi
    if os.path.isfile(OLD_ROI_CONFIG):
        try:
            with open(OLD_ROI_CONFIG, "r", encoding="utf-8") as f:
                old = json.load(f)
            roi = old.get("thermal_roi", {})
            cfg.roi.x1 = int(roi.get("x1", cfg.roi.x1))
            cfg.roi.y1 = int(roi.get("y1", cfg.roi.y1))
            cfg.roi.x2 = int(roi.get("x2", cfg.roi.x2))
            cfg.roi.y2 = int(roi.get("y2", cfg.roi.y2))
            cfg.roi.baseline_temp = float(old.get("baseline_temp", cfg.roi.baseline_temp))
            cfg.roi.warning_delta = float(old.get("warning_delta", cfg.roi.warning_delta))
            cfg.roi.critical_delta = float(old.get("critical_delta", cfg.roi.critical_delta))
            print(f"[config] Migrated settings from {OLD_ROI_CONFIG}")
            migrated = True
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"[config] WARNING: Failed to migrate {OLD_ROI_CONFIG}: {e}")

    # experiment_config.json → AppConfig.identity
    if os.path.isfile(OLD_EXP_CONFIG):
        try:
            with open(OLD_EXP_CONFIG, "r", encoding="utf-8") as f:
                old = json.load(f)
            cfg.identity.camera_id = str(old.get("camera_id", cfg.identity.camera_id))
            cfg.identity.robot_id = str(old.get("robot_id", cfg.identity.robot_id))
            print(f"[config] Migrated settings from {OLD_EXP_CONFIG}")
            migrated = True
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"[config] WARNING: Failed to migrate {OLD_EXP_CONFIG}: {e}")

    # 새 config.json 저장
    save_config(cfg, config_path)

    # 이관 완료된 파일 → .bak 백업
    if migrated:
        _backup_and_remove(OLD_ROI_CONFIG)
        _backup_and_remove(OLD_EXP_CONFIG)

    _cached_config = cfg
    return _cached_config


def save_config(cfg: AppConfig, config_path: str = CONFIG_PATH) -> None:
    """현재 설정을 config.json에 저장"""
    raw = asdict(cfg)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2, ensure_ascii=False)


def reset_cache() -> None:
    """캐시 초기화 (테스트용)"""
    global _cached_config
    _cached_config = None

"""ThermoGuard 로컬 경로 설정과 런타임 기본값.

``config.json``은 장비마다 달라지는 로컬 경로만 영속화합니다. 카메라,
설비 식별자, ROI, Threshold 같은 운영값은 DB에서 주입하는 런타임 값이며,
인터벌과 기타 비경로 값은 이 모듈의 명명된 기본 상수에서 시작합니다.

기존 호출자의 단계적 전환을 위해 ``AppConfig``의 공개 필드 형상은 유지합니다.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Optional


CONFIG_PATH = "config.json"

# 비경로 런타임 기본값. DB hydration 전의 안전한 시작값이며 JSON에서 읽지 않습니다.
NORMAL_CAPTURE_INTERVAL_SEC: float = 30.0
TEMP_MONITOR_INTERVAL_SEC: float = 5.0
PROCESS_INTERVAL_SEC: float = 10.0
INTEGRITY_INTERVAL_SEC: float = 60.0
METADATA_INTERVAL_SEC: float = 120.0
MAX_PROCESSED_CACHE: int = 10000
ALARM_COOLDOWN_SEC: float = 600.0
CLEANUP_RETENTION_DAYS: int = 2

DEFAULT_CAMERA_IP = "192.168.0.51"
DEFAULT_CAMERA_ID = "CAM-01"
DEFAULT_ROBOT_ID = "Robot-01"
DEFAULT_FACTORY_NAME = ""
DEFAULT_LINE_NAME = ""
DEFAULT_ROBOT_NAME = ""

DEFAULT_ROI_X1 = 0
DEFAULT_ROI_Y1 = 0
DEFAULT_ROI_X2 = 640
DEFAULT_ROI_Y2 = 480
DEFAULT_BASELINE_TEMP = 35.0
DEFAULT_WARNING_DELTA = 15.0
DEFAULT_CRITICAL_DELTA = 25.0

DEFAULT_HOTSPOT_MIN_SIZE = 3
DEFAULT_HOTSPOT_MIN_SIZE_MAX = 10
DEFAULT_ROI_DISPLAY_WIDTH = 640
DEFAULT_ROI_DISPLAY_HEIGHT = 480
DEFAULT_DISPLAY_WIDTH = 800
DEFAULT_TOOLS_MODE = "both"
DEFAULT_LOG_DIR = "logs"

DEFAULT_BACKEND_URL = "http://127.0.0.1:8000"
DEFAULT_BACKEND_ENABLED = True
DEFAULT_BACKEND_TIMEOUT_SEC = 5.0

DEFAULT_DATASET_DIR = "thermal_dataset"
DEFAULT_OVERLAY_DIR = "thermal_dataset/overlay"
DEFAULT_HOMOGRAPHY_PATH = "thermal_to_rgb.npy"
DEFAULT_EXIFTOOL_PATH = ""


@dataclass
class CameraConfig:
    ip: str = DEFAULT_CAMERA_IP
    capture_interval_sec: float = NORMAL_CAPTURE_INTERVAL_SEC
    # 호환 필드입니다. GigE 감시/REST 캡처 분리 전까지 기존 호출자가 사용합니다.
    warning_interval_sec: float = TEMP_MONITOR_INTERVAL_SEC
    gige_enabled: bool = True
    gige_device_index: int = 0


@dataclass
class IdentityConfig:
    camera_id: str = DEFAULT_CAMERA_ID
    robot_id: str = DEFAULT_ROBOT_ID
    factory_name: str = DEFAULT_FACTORY_NAME
    line_name: str = DEFAULT_LINE_NAME
    robot_name: str = DEFAULT_ROBOT_NAME
    factory_id: int | None = None
    line_id: int | None = None
    db_robot_id: int | None = None
    db_camera_id: int | None = None


@dataclass
class RoiEntry:
    """개별 ROI 영역 정의."""

    name: str = "ROI-1"
    x1: int = DEFAULT_ROI_X1
    y1: int = DEFAULT_ROI_Y1
    x2: int = DEFAULT_ROI_X2
    y2: int = DEFAULT_ROI_Y2
    db_roi_id: int | None = None


@dataclass
class RoiConfig:
    """ROI 설정 — DB hydration 전에는 코드 기본값을 사용합니다."""

    x1: int = DEFAULT_ROI_X1
    y1: int = DEFAULT_ROI_Y1
    x2: int = DEFAULT_ROI_X2
    y2: int = DEFAULT_ROI_Y2
    baseline_temp: float = DEFAULT_BASELINE_TEMP
    warning_delta: float = DEFAULT_WARNING_DELTA
    critical_delta: float = DEFAULT_CRITICAL_DELTA
    rois: list[RoiEntry] = field(default_factory=list)


@dataclass
class MonitoringConfig:
    process_interval_sec: float = PROCESS_INTERVAL_SEC
    integrity_interval_sec: float = INTEGRITY_INTERVAL_SEC
    metadata_interval_sec: float = METADATA_INTERVAL_SEC
    max_processed_cache: int = MAX_PROCESSED_CACHE
    alarm_cooldown_sec: float = ALARM_COOLDOWN_SEC
    cleanup_retention_days: int = CLEANUP_RETENTION_DAYS


@dataclass
class HotspotConfig:
    min_size: int = DEFAULT_HOTSPOT_MIN_SIZE
    min_size_max: int = DEFAULT_HOTSPOT_MIN_SIZE_MAX


@dataclass
class PathsConfig:
    dataset_dir: str = DEFAULT_DATASET_DIR
    overlay_dir: str = DEFAULT_OVERLAY_DIR
    homography_path: str = DEFAULT_HOMOGRAPHY_PATH


@dataclass
class DisplayConfig:
    roi_display_width: int = DEFAULT_ROI_DISPLAY_WIDTH
    roi_display_height: int = DEFAULT_ROI_DISPLAY_HEIGHT
    display_width: int = DEFAULT_DISPLAY_WIDTH


@dataclass
class ToolsConfig:
    exiftool_path: str = DEFAULT_EXIFTOOL_PATH
    mode: str = DEFAULT_TOOLS_MODE


@dataclass
class BackendConfig:
    """thermal_monitoring ↔ Backend API 런타임 연결 기본값."""

    url: str = DEFAULT_BACKEND_URL
    enabled: bool = DEFAULT_BACKEND_ENABLED
    timeout_sec: float = DEFAULT_BACKEND_TIMEOUT_SEC


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
    backend: BackendConfig = field(default_factory=BackendConfig)


_cached_config: Optional[AppConfig] = None
_cached_config_path: Optional[str] = None


def _path_config_from_raw(
    raw: object,
    runtime_config: Optional[AppConfig] = None,
) -> AppConfig:
    """경로 전용 JSON을 AppConfig 호환 객체에 병합합니다."""

    cfg = runtime_config or AppConfig()
    # force_reload 시 파일/키가 삭제된 경우에도 이전 로컬 경로가 캐시에
    # 잔류하지 않도록, 영속 대상 필드만 먼저 기본값으로 되돌립니다.
    cfg.paths = PathsConfig()
    cfg.tools.exiftool_path = DEFAULT_EXIFTOOL_PATH
    if not isinstance(raw, dict):
        return cfg

    paths_raw = raw.get("paths")
    if isinstance(paths_raw, dict):
        allowed = PathsConfig.__dataclass_fields__
        path_values = {
            key: str(value)
            for key, value in paths_raw.items()
            if key in allowed and value is not None
        }
        cfg.paths = PathsConfig(**path_values)

    tools_raw = raw.get("tools")
    if isinstance(tools_raw, dict) and tools_raw.get("exiftool_path") is not None:
        cfg.tools.exiftool_path = str(tools_raw["exiftool_path"])
    return cfg


def load_config(config_path: str = CONFIG_PATH, force_reload: bool = False) -> AppConfig:
    """로컬 경로를 읽고 비경로 필드는 코드 기본값으로 구성합니다.

    레거시 JSON에 비경로 키가 남아 있어도 읽지 않습니다. DB 관리값의 실제
    hydration은 호출 계층이 반환된 ``AppConfig``에 적용해야 합니다.
    """

    global _cached_config, _cached_config_path

    normalized_path = os.path.abspath(config_path)
    if (
        _cached_config is not None
        and _cached_config_path == normalized_path
        and not force_reload
    ):
        return _cached_config

    cfg = (
        _cached_config
        if _cached_config is not None and _cached_config_path == normalized_path
        else AppConfig()
    )
    # 파일이 사라진 경우에도 로컬 영속 필드는 기본값으로 복원하되,
    # 같은 객체에 DB에서 주입된 비경로 런타임 값은 유지합니다.
    cfg = _path_config_from_raw({}, cfg)
    if os.path.isfile(normalized_path):
        try:
            with open(normalized_path, "r", encoding="utf-8") as stream:
                cfg = _path_config_from_raw(json.load(stream), cfg)
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            print(f"[config] WARNING: Failed to read {normalized_path}: {exc}")

    _cached_config = cfg
    _cached_config_path = normalized_path
    return cfg


def save_config(cfg: AppConfig, config_path: str = CONFIG_PATH) -> None:
    """로컬 경로만 같은 디렉터리의 임시 파일을 거쳐 원자 저장합니다."""

    global _cached_config, _cached_config_path

    normalized_path = os.path.abspath(config_path)
    parent_dir = os.path.dirname(normalized_path)
    os.makedirs(parent_dir, exist_ok=True)
    payload = {
        "paths": asdict(cfg.paths),
        "tools": {"exiftool_path": str(cfg.tools.exiftool_path)},
    }

    temporary_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent_dir,
            prefix=f".{os.path.basename(normalized_path)}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = stream.name
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(temporary_path, normalized_path)
        temporary_path = None
        _cached_config = cfg
        _cached_config_path = normalized_path
    finally:
        if temporary_path and os.path.exists(temporary_path):
            try:
                os.remove(temporary_path)
            except OSError:
                pass


def reset_cache() -> None:
    """런타임 설정 캐시를 초기화합니다(주로 테스트용)."""

    global _cached_config, _cached_config_path
    _cached_config = None
    _cached_config_path = None

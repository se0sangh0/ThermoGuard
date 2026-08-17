"""Unified configuration loading, validation, and durable persistence.

Development uses ``config.json`` from the repository root by default.  A
factory launcher sets ``THERMOGUARD_CONFIG`` to an explicitly managed
configuration file outside the checkout.

``load_config()`` intentionally remains backwards compatible: it can migrate
legacy settings and create a default file on first use.  Installation and
automation checks should use ``load_config(strict=True)`` so a missing,
malformed, or unsafe configuration is never silently replaced.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_ENV_VAR = "THERMOGUARD_CONFIG"
FACTORY_MODE_ENV_VAR = "THERMOGUARD_FACTORY_MODE"
FROZEN_STATE_ROOT = Path("/var/lib/thermoguard")
# A commissioned release is installed below this root.  Detecting the location
# in addition to the launcher environment prevents a direct ``python
# dashboard.py`` invocation from disabling factory path policy by unsetting an
# environment variable.
FACTORY_INSTALL_ROOT = Path("/opt/thermoguard")
FACTORY_CONFIG_PATH = Path("/var/lib/thermoguard/config.json")
# Keep this public string constant for callers that imported it previously.
CONFIG_PATH = str(
    FROZEN_STATE_ROOT / "config.json"
    if getattr(sys, "frozen", False)
    else PROJECT_ROOT / "config.json"
)
OLD_ROI_CONFIG = str(PROJECT_ROOT / "roi_config.json")
OLD_EXP_CONFIG = str(PROJECT_ROOT / "experiment_config.json")


class ConfigValidationError(ValueError):
    """Raised when a configuration is unsafe or unsuitable for production."""


# ════════════════════════════════════════════════════════════
# Dataclass definitions
# ════════════════════════════════════════════════════════════


@dataclass
class CameraConfig:
    ip: str = "192.168.0.51"
    capture_interval_sec: float = 30.0
    warning_interval_sec: float = 5.0
    # Legacy PySpin option retained for backward-compatible config loading.
    # The supported factory dashboard reaches the GigE camera through its HTTP
    # REST image endpoint and ExifTool, regardless of this legacy flag.
    gige_enabled: bool = False
    gige_device_index: int = 0


@dataclass
class IdentityConfig:
    camera_id: str = "CAM-01"
    robot_id: str = "Robot-01"
    factory_name: str = ""
    line_name: str = ""
    robot_name: str = ""
    factory_id: int | None = None
    line_id: int | None = None
    db_robot_id: int | None = None
    db_camera_id: int | None = None


@dataclass
class RoiEntry:
    """Individual ROI definition in thermal-image coordinates."""

    name: str = "ROI-1"
    x1: int = 0
    y1: int = 0
    x2: int = 640
    y2: int = 480
    db_roi_id: int | None = None


@dataclass
class RoiConfig:
    """ROI settings. ``rois`` is preferred; the four coordinates are a fallback."""

    x1: int = 0
    y1: int = 0
    x2: int = 640
    y2: int = 480
    baseline_temp: float = 35.0
    warning_delta: float = 15.0
    critical_delta: float = 25.0
    rois: list = field(default_factory=list)  # list[RoiEntry]


@dataclass
class MonitoringConfig:
    process_interval_sec: float = 10.0
    integrity_interval_sec: float = 60.0
    metadata_interval_sec: float = 120.0
    max_processed_cache: int = 10000
    alarm_cooldown_sec: float = 600.0
    cleanup_retention_days: int = 2


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
class BackendConfig:
    """thermal_monitoring ↔ Backend API connection settings."""

    url: str = "http://127.0.0.1:8000"
    # A generated configuration must never start posting to a production
    # backend until the site has explicitly supplied its database identity.
    enabled: bool = False
    timeout_sec: float = 5.0


# FastAPI persistence must never hold the capture lifecycle hostage.  The
# configurable timeout remains useful for normal DB synchronisation, but
# capture status, measurement and GUI operational logs are safety-adjacent
# best-effort calls and use this separate bounded deadline.
BACKEND_IO_TIMEOUT_MAX_SEC = 10.0


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


# ════════════════════════════════════════════════════════════
# Path and conversion helpers
# ════════════════════════════════════════════════════════════


def _raw_config_path(
    config_path: str | os.PathLike[str] | None,
) -> Path:
    """Return the requested path without resolving a final symlink.

    Strict production loading must be able to reject a symlinked config file;
    resolving it first would erase that security-relevant information.
    """

    if config_path is None:
        override = os.environ.get(CONFIG_ENV_VAR, "").strip()
        if not override:
            return Path(CONFIG_PATH)
        expanded = os.path.expandvars(os.path.expanduser(override))
    else:
        expanded = os.path.expandvars(os.path.expanduser(os.fspath(config_path)))
    return Path(expanded)


def default_config_path() -> Path:
    """Return the configured config path without performing I/O.

    A relative ``THERMOGUARD_CONFIG`` remains relative to the caller's current
    directory, which makes command-line overrides predictable.  The normal
    default is always anchored to the repository instead of the launch CWD.
    """

    return _raw_config_path(None).resolve(strict=False)


def _resolve_config_path(config_path: str | os.PathLike[str] | None) -> Path:
    return _raw_config_path(config_path).resolve(strict=False)


def resolve_runtime_path(
    value: str | os.PathLike[str],
    *,
    config_path: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve mutable paths beside the selected config, never from launch CWD."""

    expanded = Path(os.path.expandvars(os.path.expanduser(os.fspath(value))))
    if expanded.is_absolute():
        return expanded.resolve(strict=False)
    return (_resolve_config_path(config_path).parent / expanded).resolve(strict=False)


def _legacy_paths(config_path: Path) -> tuple[Path, Path]:
    """Locate migration candidates alongside the selected unified config."""

    return (
        config_path.parent / Path(OLD_ROI_CONFIG).name,
        config_path.parent / Path(OLD_EXP_CONFIG).name,
    )


def _dict_to_dataclass(raw: dict, dc: type):
    """Convert one JSON object to a dataclass, ignoring unknown keys."""

    if not isinstance(raw, dict):
        raise TypeError(f"Expected an object for {dc.__name__}")
    field_names = {field_.name for field_ in dc.__dataclass_fields__.values()}
    return dc(**{key: value for key, value in raw.items() if key in field_names})


def _dataclass_field_names(dc: type) -> set[str]:
    return set(dc.__dataclass_fields__)


def _validate_complete_mapping(
    raw: object,
    name: str,
    expected_keys: set[str],
    *,
    optional_keys: set[str] | None = None,
) -> dict:
    """Require an exact JSON object shape for a production configuration.

    Dataclass defaults are intentionally convenient for development and legacy
    migration, but they are unsafe at a factory boundary: a misspelled key or
    omitted setting could otherwise silently become a default camera, timeout,
    or threshold.  Strict mode therefore accepts only the explicit schema
    emitted by :func:`save_config` / ``config.example.json``.
    """

    if not isinstance(raw, dict):
        raise ConfigValidationError(f"{name} must be a JSON object")
    optional_keys = optional_keys or set()
    actual_keys = set(raw)
    unknown = sorted(actual_keys - expected_keys)
    missing = sorted((expected_keys - optional_keys) - actual_keys)
    if unknown:
        raise ConfigValidationError(
            f"{name} contains unknown key(s): {', '.join(unknown)}"
        )
    if missing:
        raise ConfigValidationError(
            f"{name} is missing required key(s): {', '.join(missing)}"
        )
    return raw


def _validate_strict_raw_schema(raw: object) -> dict:
    """Validate complete, typo-free JSON structure before conversion.

    This is deliberately separate from ``validate_config``.  The latter
    validates values after they have become dataclasses; this function ensures
    no dataclass default was substituted for production input.
    """

    top_level = _validate_complete_mapping(
        raw,
        "configuration",
        _dataclass_field_names(AppConfig),
    )
    section_types: dict[str, type] = {
        "camera": CameraConfig,
        "identity": IdentityConfig,
        "roi": RoiConfig,
        "monitoring": MonitoringConfig,
        "hotspot": HotspotConfig,
        "paths": PathsConfig,
        "display": DisplayConfig,
        "tools": ToolsConfig,
        "backend": BackendConfig,
    }
    for section, dc in section_types.items():
        _validate_complete_mapping(
            top_level[section],
            section,
            _dataclass_field_names(dc),
        )

    roi_entries = top_level["roi"]["rois"]
    if not isinstance(roi_entries, list):
        raise ConfigValidationError("roi.rois must be a JSON array")
    roi_keys = _dataclass_field_names(RoiEntry)
    for index, entry in enumerate(roi_entries):
        _validate_complete_mapping(
            entry,
            f"roi.rois[{index}]",
            roi_keys,
        )
    return top_level


def _reject_duplicate_json_keys(pairs: list[tuple[object, object]]) -> dict:
    """JSON decoder hook that rejects ambiguous duplicate object members."""

    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ConfigValidationError(f"configuration contains duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_strict_config_file_security(
    requested_path: Path,
    resolved_path: Path,
) -> None:
    """Reject mutable indirection and group/other-writable approved config.

    The dashboard owns camera, ROI and threshold behaviour.  Accepting a file
    that another local account can modify would make the reviewed deployment
    configuration non-authoritative at the next restart.
    """

    if requested_path.is_symlink():
        raise ConfigValidationError(
            f"Strict configuration must not be a symbolic link: {requested_path}"
        )
    try:
        mode = stat.S_IMODE(resolved_path.stat().st_mode)
    except OSError as exc:
        raise ConfigValidationError(
            f"Cannot inspect strict configuration permissions: {resolved_path}"
        ) from exc
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigValidationError(
            "Strict configuration must not be writable by group or others "
            f"(mode {mode:04o}): {resolved_path}"
        )


def _from_dict(raw: dict, *, strict: bool = False) -> AppConfig:
    if not isinstance(raw, dict):
        raise TypeError("Top-level configuration must be a JSON object")

    if strict:
        raw = _validate_strict_raw_schema(raw)

    roi_raw = raw.get("roi", {})
    if not isinstance(roi_raw, dict):
        raise TypeError("Expected an object for RoiConfig")

    roi_entries = roi_raw.get("rois", [])
    if not isinstance(roi_entries, list):
        raise TypeError("roi.rois must be a JSON array")

    rois_list: list[RoiEntry] = []
    for entry in roi_entries:
        if not isinstance(entry, dict):
            raise TypeError("Each roi.rois entry must be a JSON object")
        # Preserve the historic conversion in normal application loads, but do
        # not coerce strict preflight input: it must prove that the JSON itself
        # contains valid integer coordinates.
        if strict:
            x1 = entry.get("x1", 0)
            y1 = entry.get("y1", 0)
            x2 = entry.get("x2", 640)
            y2 = entry.get("y2", 480)
        else:
            x1 = int(entry.get("x1", 0))
            y1 = int(entry.get("y1", 0))
            x2 = int(entry.get("x2", 640))
            y2 = int(entry.get("y2", 480))
        rois_list.append(
            RoiEntry(
                name=entry.get("name", "ROI"),
                x1=x1,
                y1=y1,
                x2=x2,
                y2=y2,
                db_roi_id=entry.get("db_roi_id"),
            )
        )

    roi_config = _dict_to_dataclass(roi_raw, RoiConfig)
    roi_config.rois = rois_list

    return AppConfig(
        camera=_dict_to_dataclass(raw.get("camera", {}), CameraConfig),
        identity=_dict_to_dataclass(raw.get("identity", {}), IdentityConfig),
        roi=roi_config,
        monitoring=_dict_to_dataclass(
            raw.get("monitoring", {}), MonitoringConfig
        ),
        hotspot=_dict_to_dataclass(raw.get("hotspot", {}), HotspotConfig),
        paths=_dict_to_dataclass(raw.get("paths", {}), PathsConfig),
        display=_dict_to_dataclass(raw.get("display", {}), DisplayConfig),
        tools=_dict_to_dataclass(raw.get("tools", {}), ToolsConfig),
        backend=_dict_to_dataclass(raw.get("backend", {}), BackendConfig),
    )


def _backup_and_remove(filepath: str | os.PathLike[str]) -> None:
    """Move a migrated legacy file aside without deleting its contents."""

    path = Path(filepath)
    if path.is_file():
        backup_path = path.with_name(f"{path.name}.bak")
        try:
            shutil.move(str(path), str(backup_path))
            print(f"[config] Migrated: {path} → {backup_path}")
        except OSError:
            pass


# ════════════════════════════════════════════════════════════
# Strict production validation
# ════════════════════════════════════════════════════════════


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigValidationError(f"{name} must be a finite number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ConfigValidationError(f"{name} must be finite")
    return numeric


def _positive_number(value: object, name: str, *, maximum: float) -> float:
    numeric = _finite_number(value, name)
    if numeric <= 0 or numeric > maximum:
        raise ConfigValidationError(f"{name} must be greater than 0 and at most {maximum}")
    return numeric


def _nonnegative_number(value: object, name: str, *, maximum: float) -> float:
    numeric = _finite_number(value, name)
    if numeric < 0 or numeric > maximum:
        raise ConfigValidationError(f"{name} must be between 0 and {maximum}")
    return numeric


def _positive_int(value: object, name: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigValidationError(f"{name} must be an integer")
    if value <= 0 or value > maximum:
        raise ConfigValidationError(f"{name} must be between 1 and {maximum}")
    return value


def _nonnegative_int(value: object, name: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigValidationError(f"{name} must be an integer")
    if value < 0 or value > maximum:
        raise ConfigValidationError(f"{name} must be between 0 and {maximum}")
    return value


def _optional_positive_int(value: object, name: str, *, maximum: int) -> None:
    if value is not None:
        _positive_int(value, name, maximum=maximum)


def _require_nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _validate_camera_host(value: object) -> None:
    host = _require_nonempty_string(value, "camera.ip")
    if any(character in host for character in ":/@\\") or any(character.isspace() for character in host):
        raise ConfigValidationError("camera.ip must be an IPv4 address or hostname without a URL/path")
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass
    if len(host) > 253 or not re.fullmatch(
        r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
        host,
    ):
        raise ConfigValidationError("camera.ip must be a valid IPv4 address or hostname")


def _validate_roi_bounds(
    roi: RoiConfig | RoiEntry,
    name: str,
    *,
    width: int,
    height: int,
) -> None:
    x1 = _nonnegative_int(roi.x1, f"{name}.x1", maximum=width)
    y1 = _nonnegative_int(roi.y1, f"{name}.y1", maximum=height)
    x2 = _nonnegative_int(roi.x2, f"{name}.x2", maximum=width)
    y2 = _nonnegative_int(roi.y2, f"{name}.y2", maximum=height)
    if x1 >= x2:
        raise ConfigValidationError(f"{name} must satisfy x1 < x2")
    if y1 >= y2:
        raise ConfigValidationError(f"{name} must satisfy y1 < y2")


def _resolve_dataset_target(value: object, name: str) -> Path:
    candidate = _configured_path(value, name)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    target = candidate.resolve(strict=False)

    protected_roots = {
        Path("/").resolve(),
        Path.home().resolve(),
        PROJECT_ROOT.resolve(),
    }
    if target in protected_roots:
        raise ConfigValidationError(
            f"{name} must be a dedicated data directory, not {target}"
        )
    if os.path.ismount(target):
        raise ConfigValidationError(
            f"{name} must be below a mount point, not the mount root {target}"
        )
    return target


def _configured_path(value: object, name: str) -> Path:
    """Return an expanded configured path without anchoring relative values."""

    raw_path = _require_nonempty_string(value, name)
    return Path(os.path.expandvars(os.path.expanduser(raw_path)))


def _is_installed_factory_release() -> bool:
    """Return whether this source tree is running from the fixed factory root."""

    try:
        return PROJECT_ROOT.resolve().is_relative_to(FACTORY_INSTALL_ROOT.resolve())
    except OSError:
        return False


def factory_mode_enabled() -> bool:
    """Whether factory path policy is required for this process.

    The launcher enables this policy explicitly, while a source tree installed
    below ``/opt/thermoguard`` enforces it even if a caller invokes the Python
    entry point directly.  Development checkouts keep their normal behaviour
    unless the flag is deliberately set for a test or dry run.
    """

    return (
        _is_installed_factory_release()
        or os.environ.get(FACTORY_MODE_ENV_VAR, "").strip() == "1"
    )


def bounded_backend_timeout(value: object) -> float:
    """Return a finite backend timeout suitable for non-blocking line work."""

    return min(
        BACKEND_IO_TIMEOUT_MAX_SEC,
        max(1.0, _positive_number(value, "backend.timeout_sec", maximum=600)),
    )


def _validate_factory_writable_paths(cfg: AppConfig) -> None:
    """Keep factory-generated data out of the immutable release directory.

    The production launcher is intentionally fixed and exports
    ``THERMOGUARD_FACTORY_MODE=1``.  Relative output paths in that mode would
    otherwise resolve below the release checkout and make a release mutable or
    fail later during capture/calibration.  Development keeps relative paths
    for convenience, so this guard is scoped to the launcher contract.
    """

    if not factory_mode_enabled():
        return

    release_root = PROJECT_ROOT.resolve()
    writable_paths = {
        "paths.dataset_dir": cfg.paths.dataset_dir,
        "paths.overlay_dir": cfg.paths.overlay_dir,
        "paths.homography_path": cfg.paths.homography_path,
    }
    for name, value in writable_paths.items():
        configured = _configured_path(value, name)
        if not configured.is_absolute():
            raise ConfigValidationError(
                f"{name} must be an absolute path in factory mode"
            )
        target = configured.resolve(strict=False)
        if target.is_relative_to(release_root):
            raise ConfigValidationError(
                f"{name} must be outside the immutable release directory in factory mode"
            )


def validate_config(
    cfg: AppConfig,
    *,
    collection_mode: bool = False,
    enforce_threshold_order: bool = True,
) -> None:
    """Validate the invariants required before a factory deployment writes config.

    This function has no I/O and deliberately does not coerce unsafe values.
    Callers can use it before a planned deployment check, while ``save_config``
    always invokes it before changing a file.
    """

    if not isinstance(cfg, AppConfig):
        raise ConfigValidationError("cfg must be an AppConfig instance")

    _validate_camera_host(cfg.camera.ip)
    _positive_number(
        cfg.camera.capture_interval_sec,
        "camera.capture_interval_sec",
        maximum=86_400,
    )
    _positive_number(
        cfg.camera.warning_interval_sec,
        "camera.warning_interval_sec",
        maximum=86_400,
    )
    if not isinstance(cfg.camera.gige_enabled, bool):
        raise ConfigValidationError("camera.gige_enabled must be a boolean")
    _nonnegative_int(
        cfg.camera.gige_device_index,
        "camera.gige_device_index",
        maximum=1_000_000,
    )

    width = _positive_int(
        cfg.display.roi_display_width,
        "display.roi_display_width",
        maximum=10_000,
    )
    height = _positive_int(
        cfg.display.roi_display_height,
        "display.roi_display_height",
        maximum=10_000,
    )
    _positive_int(cfg.display.display_width, "display.display_width", maximum=20_000)

    baseline_temp = _finite_number(cfg.roi.baseline_temp, "roi.baseline_temp")
    if not -100 <= baseline_temp <= 1_000:
        raise ConfigValidationError("roi.baseline_temp must be between -100 and 1000")
    warning_delta = _positive_number(
        cfg.roi.warning_delta,
        "roi.warning_delta",
        maximum=1_000,
    )
    critical_delta = _positive_number(
        cfg.roi.critical_delta,
        "roi.critical_delta",
        maximum=1_000,
    )
    if enforce_threshold_order and warning_delta >= critical_delta:
        raise ConfigValidationError(
            "roi must satisfy 0 < warning_delta < critical_delta"
        )
    _validate_roi_bounds(cfg.roi, "roi", width=width, height=height)

    if not isinstance(cfg.roi.rois, list):
        raise ConfigValidationError("roi.rois must be a list")
    for index, roi_entry in enumerate(cfg.roi.rois):
        if not isinstance(roi_entry, RoiEntry):
            raise ConfigValidationError(f"roi.rois[{index}] must be a RoiEntry")
        _require_nonempty_string(roi_entry.name, f"roi.rois[{index}].name")
        _validate_roi_bounds(
            roi_entry,
            f"roi.rois[{index}]",
            width=width,
            height=height,
        )
        _optional_positive_int(
            roi_entry.db_roi_id,
            f"roi.rois[{index}].db_roi_id",
            maximum=2_147_483_647,
        )

    _require_nonempty_string(cfg.identity.camera_id, "identity.camera_id")
    _require_nonempty_string(cfg.identity.robot_id, "identity.robot_id")
    for field_name in ("factory_name", "line_name", "robot_name"):
        value = getattr(cfg.identity, field_name)
        if not isinstance(value, str):
            raise ConfigValidationError(f"identity.{field_name} must be a string")
    _optional_positive_int(cfg.identity.factory_id, "identity.factory_id", maximum=2_147_483_647)
    _optional_positive_int(cfg.identity.line_id, "identity.line_id", maximum=2_147_483_647)
    _optional_positive_int(
        cfg.identity.db_robot_id,
        "identity.db_robot_id",
        maximum=2_147_483_647,
    )
    _optional_positive_int(
        cfg.identity.db_camera_id,
        "identity.db_camera_id",
        maximum=2_147_483_647,
    )

    _positive_number(
        cfg.monitoring.process_interval_sec,
        "monitoring.process_interval_sec",
        maximum=604_800,
    )
    _positive_number(
        cfg.monitoring.integrity_interval_sec,
        "monitoring.integrity_interval_sec",
        maximum=604_800,
    )
    _positive_number(
        cfg.monitoring.metadata_interval_sec,
        "monitoring.metadata_interval_sec",
        maximum=604_800,
    )
    _positive_int(
        cfg.monitoring.max_processed_cache,
        "monitoring.max_processed_cache",
        maximum=10_000_000,
    )
    _nonnegative_number(
        cfg.monitoring.alarm_cooldown_sec,
        "monitoring.alarm_cooldown_sec",
        maximum=604_800,
    )
    _nonnegative_int(
        cfg.monitoring.cleanup_retention_days,
        "monitoring.cleanup_retention_days",
        maximum=3_650,
    )

    min_size = _positive_int(cfg.hotspot.min_size, "hotspot.min_size", maximum=1_000_000)
    min_size_max = _positive_int(
        cfg.hotspot.min_size_max,
        "hotspot.min_size_max",
        maximum=1_000_000,
    )
    if min_size > min_size_max:
        raise ConfigValidationError(
            "hotspot must satisfy min_size <= min_size_max"
        )

    if collection_mode:
        dataset_target = _configured_path(
            cfg.paths.dataset_dir, "paths.dataset_dir"
        )
        overlay_target = _configured_path(
            cfg.paths.overlay_dir, "paths.overlay_dir"
        )
        if not dataset_target.is_absolute():
            dataset_target = PROJECT_ROOT / dataset_target
        if not overlay_target.is_absolute():
            overlay_target = PROJECT_ROOT / overlay_target
        dataset_target = dataset_target.resolve(strict=False)
        overlay_target = overlay_target.resolve(strict=False)
    else:
        dataset_target = _resolve_dataset_target(
            cfg.paths.dataset_dir, "paths.dataset_dir"
        )
        overlay_target = _resolve_dataset_target(
            cfg.paths.overlay_dir, "paths.overlay_dir"
        )
    if not overlay_target.is_relative_to(dataset_target):
        raise ConfigValidationError("paths.overlay_dir must be inside paths.dataset_dir")
    _require_nonempty_string(cfg.paths.homography_path, "paths.homography_path")
    if not collection_mode:
        _validate_factory_writable_paths(cfg)

    if not isinstance(cfg.tools.mode, str) or cfg.tools.mode not in {"thermal", "both"}:
        raise ConfigValidationError("tools.mode must be either 'thermal' or 'both'")
    if not isinstance(cfg.tools.exiftool_path, str):
        raise ConfigValidationError("tools.exiftool_path must be a string")

    if not isinstance(cfg.backend.enabled, bool):
        raise ConfigValidationError("backend.enabled must be a boolean")
    _positive_number(cfg.backend.timeout_sec, "backend.timeout_sec", maximum=600)
    backend_url = _require_nonempty_string(cfg.backend.url, "backend.url")
    try:
        parsed_backend_url = urlparse(backend_url)
        backend_hostname = parsed_backend_url.hostname
        parsed_backend_url.port  # Validate an optional explicit port.
    except ValueError as exc:
        raise ConfigValidationError("backend.url is malformed") from exc
    if parsed_backend_url.scheme not in {"http", "https"} or not parsed_backend_url.netloc:
        raise ConfigValidationError("backend.url must be an absolute http(s) URL")
    if (
        not backend_hostname
        or parsed_backend_url.username is not None
        or parsed_backend_url.password is not None
        or parsed_backend_url.query
        or parsed_backend_url.fragment
    ):
        raise ConfigValidationError(
            "backend.url must not contain credentials, query parameters, or fragments"
        )
    if cfg.backend.enabled:
        _positive_int(
            cfg.identity.db_camera_id,
            "identity.db_camera_id",
            maximum=2_147_483_647,
        )


# ════════════════════════════════════════════════════════════
# Load / Save
# ════════════════════════════════════════════════════════════


_cached_config: Optional[AppConfig] = None
_cached_config_path: Optional[Path] = None


def load_config(
    config_path: str | os.PathLike[str] | None = None,
    force_reload: bool = False,
    *,
    strict: bool = False,
) -> AppConfig:
    """Load unified settings, with optional fail-closed production validation.

    ``strict=False`` retains the historical migration/default behavior for
    existing application callers.  ``strict=True`` never writes a replacement
    file: malformed, missing, or unsafe config raises ``ConfigValidationError``
    and the original file remains untouched.
    """

    global _cached_config, _cached_config_path
    requested_path = _raw_config_path(config_path)
    resolved_path = _resolve_config_path(config_path)

    # A release below /opt/thermoguard is an installed factory deployment.
    # Its reviewed site policy must come from the fixed persistent path, not a
    # caller-controlled config override.  The restriction is strict-load only
    # so legacy migration behaviour remains available to development helpers.
    if (
        strict
        and _is_installed_factory_release()
        and resolved_path != FACTORY_CONFIG_PATH.resolve(strict=False)
    ):
        raise ConfigValidationError(
            "Installed factory releases must use the approved configuration "
            f"path: {FACTORY_CONFIG_PATH}"
        )

    if (
        not strict
        and _cached_config is not None
        and _cached_config_path == resolved_path
        and not force_reload
    ):
        return _cached_config

    if resolved_path.is_file():
        try:
            if strict:
                _validate_strict_config_file_security(requested_path, resolved_path)
            with resolved_path.open("r", encoding="utf-8") as config_file:
                raw = (
                    json.load(config_file, object_pairs_hook=_reject_duplicate_json_keys)
                    if strict
                    else json.load(config_file)
                )
            cfg = _from_dict(raw, strict=strict)
            if strict:
                validate_config(cfg)
            _cached_config = cfg
            _cached_config_path = resolved_path
            return cfg
        except ConfigValidationError:
            # A strict load must report an unsafe file without changing it.
            raise
        except (OSError, json.JSONDecodeError, TypeError, ValueError, AttributeError) as exc:
            if strict:
                raise ConfigValidationError(
                    f"Failed to read strict configuration {resolved_path}: {exc}"
                ) from exc
            print(f"[config] WARNING: Failed to parse {resolved_path}: {exc}")
            print("[config] Falling back to defaults + migration")
    elif strict:
        raise ConfigValidationError(
            f"Strict configuration file does not exist: {resolved_path}"
        )

    # Missing or non-strict malformed config: retain historic defaults/migration.
    cfg = AppConfig()
    old_roi_path, old_exp_path = _legacy_paths(resolved_path)
    migrated = False

    if old_roi_path.is_file():
        try:
            with old_roi_path.open("r", encoding="utf-8") as legacy_file:
                old = json.load(legacy_file)
            roi = old.get("thermal_roi", {})
            cfg.roi.x1 = int(roi.get("x1", cfg.roi.x1))
            cfg.roi.y1 = int(roi.get("y1", cfg.roi.y1))
            cfg.roi.x2 = int(roi.get("x2", cfg.roi.x2))
            cfg.roi.y2 = int(roi.get("y2", cfg.roi.y2))
            cfg.roi.baseline_temp = float(old.get("baseline_temp", cfg.roi.baseline_temp))
            cfg.roi.warning_delta = float(old.get("warning_delta", cfg.roi.warning_delta))
            cfg.roi.critical_delta = float(old.get("critical_delta", cfg.roi.critical_delta))
            print(f"[config] Migrated settings from {old_roi_path}")
            migrated = True
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            print(f"[config] WARNING: Failed to migrate {old_roi_path}: {exc}")

    if old_exp_path.is_file():
        try:
            with old_exp_path.open("r", encoding="utf-8") as legacy_file:
                old = json.load(legacy_file)
            cfg.identity.camera_id = str(old.get("camera_id", cfg.identity.camera_id))
            cfg.identity.robot_id = str(old.get("robot_id", cfg.identity.robot_id))
            print(f"[config] Migrated settings from {old_exp_path}")
            migrated = True
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            print(f"[config] WARNING: Failed to migrate {old_exp_path}: {exc}")

    save_config(cfg, resolved_path)

    if migrated:
        _backup_and_remove(old_roi_path)
        _backup_and_remove(old_exp_path)

    _cached_config = cfg
    _cached_config_path = resolved_path
    return cfg


def _fsync_directory(directory: Path) -> None:
    """Persist the name replacement where the platform permits directory fsync."""

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        directory_fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        # Some filesystem implementations do not support directory fsync.
        # The data itself was already fsynced before the atomic replacement.
        pass
    finally:
        os.close(directory_fd)


def _save_config_atomic(
    cfg: AppConfig,
    config_path: str | os.PathLike[str] | None = None,
) -> None:
    global _cached_config, _cached_config_path
    target = _resolve_config_path(config_path)
    target_parent = target.parent

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target_parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
            # Configuration is operationally sensitive (camera endpoint,
            # thresholds, dataset path and DB IDs).  Always harden it instead
            # of inheriting a loose umask or an old group-writable mode.
            os.fchmod(config_file.fileno(), 0o600)
            json.dump(asdict(cfg), config_file, indent=2, ensure_ascii=False, allow_nan=False)
            config_file.write("\n")
            config_file.flush()
            os.fsync(config_file.fileno())
        os.replace(temporary_path, target)
        _fsync_directory(target_parent)
        # Keep all lazy analysis helpers on the same configuration object as
        # the dashboard after an approved Settings save.  Without this, those
        # helpers can keep a stale cached threshold/path while the UI reports
        # that the new settings have been applied.
        _cached_config = cfg
        _cached_config_path = target
    except BaseException:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def save_config(
    cfg: AppConfig,
    config_path: str | os.PathLike[str] | None = None,
) -> None:
    """Strictly validate and atomically persist a factory configuration."""

    validate_config(cfg)
    _save_config_atomic(cfg, config_path)


def save_collection_config(
    cfg: AppConfig,
    config_path: str | os.PathLike[str] | None = None,
) -> None:
    """Atomically persist supervised collection settings as mode 0600.

    Collection tools may update ROI/calibration metadata while preserving an
    older site's equal warning/critical deltas or mount-root dataset.  Validate
    all numeric/type/ROI invariants, but leave threshold ordering to the
    Settings dialog that actually edits those values.
    """

    if not isinstance(cfg, AppConfig):
        raise ConfigValidationError("cfg must be an AppConfig instance")
    width = _positive_int(
        cfg.display.roi_display_width,
        "display.roi_display_width",
        maximum=10_000,
    )
    height = _positive_int(
        cfg.display.roi_display_height,
        "display.roi_display_height",
        maximum=10_000,
    )
    _finite_number(cfg.roi.baseline_temp, "roi.baseline_temp")
    _positive_number(cfg.roi.warning_delta, "roi.warning_delta", maximum=1_000)
    _positive_number(cfg.roi.critical_delta, "roi.critical_delta", maximum=1_000)
    _validate_roi_bounds(cfg.roi, "roi", width=width, height=height)
    if not isinstance(cfg.roi.rois, list):
        raise ConfigValidationError("roi.rois must be a list")
    for index, roi_entry in enumerate(cfg.roi.rois):
        if not isinstance(roi_entry, RoiEntry):
            raise ConfigValidationError(f"roi.rois[{index}] must be a RoiEntry")
        _require_nonempty_string(roi_entry.name, f"roi.rois[{index}].name")
        _validate_roi_bounds(
            roi_entry,
            f"roi.rois[{index}]",
            width=width,
            height=height,
        )
    try:
        json.dumps(asdict(cfg), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ConfigValidationError(
            f"collection configuration is not safely serializable: {exc}"
        ) from exc
    _save_config_atomic(cfg, config_path)


def reset_cache() -> None:
    """Reset the in-process config cache (primarily for tests)."""

    global _cached_config, _cached_config_path
    _cached_config = None
    _cached_config_path = None

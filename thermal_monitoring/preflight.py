"""Read-only production-readiness checks for a ThermoGuard factory install.

Run this before a commissioning window and again after any config/dependency
change.  It deliberately never creates configuration, dataset, markers, or
database records.  Use ``--online`` only when the camera and backend are in the
approved test window.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

import requests

from .config import (
    AppConfig,
    ConfigValidationError,
    PROJECT_ROOT,
    default_config_path,
    factory_mode_enabled,
    load_config,
    resolve_runtime_path,
)
from .runtime_lock import inspect_default_lock_provisioning


EXIT_OK = 0
EXIT_WARNING = 1
EXIT_ERROR = 2


@dataclass(frozen=True)
class Check:
    name: str
    level: str  # ok | warning | error
    detail: str


def _check(name: str, condition: bool, detail: str, *, error: bool = True) -> Check:
    if condition:
        return Check(name, "ok", detail)
    return Check(name, "error" if error else "warning", detail)


def _dataset_path(cfg: AppConfig) -> Path:
    value = Path(os.path.expanduser(os.path.expandvars(cfg.paths.dataset_dir)))
    return (PROJECT_ROOT / value).resolve() if not value.is_absolute() else value.resolve()


def _homography_path(cfg: AppConfig) -> Path:
    return resolve_runtime_path(cfg.paths.homography_path)


def _overlay_path(cfg: AppConfig) -> Path:
    value = Path(os.path.expanduser(os.path.expandvars(cfg.paths.overlay_dir)))
    return (PROJECT_ROOT / value).resolve() if not value.is_absolute() else value.resolve()


def _camera_image_url(camera_ip: str) -> str:
    return f"http://{camera_ip}/api/image/current?imgformat=JPEG"


def _check_runtime_dependencies(cfg: AppConfig) -> list[Check]:
    checks: list[Check] = []
    checks.append(
        _check(
            "python",
            (3, 10) <= sys.version_info[:2] <= (3, 12),
            f"Python {sys.version.split()[0]} (supported factory baseline: 3.10–3.12)",
        )
    )
    for module in ("requests", "numpy", "PIL", "cv2", "tkinter"):
        try:
            importlib.import_module(module)
        except Exception as exc:
            checks.append(Check(f"python:{module}", "error", f"import failed: {type(exc).__name__}"))
        else:
            checks.append(Check(f"python:{module}", "ok", "available"))

    configured_exiftool = cfg.tools.exiftool_path.strip()
    if configured_exiftool:
        expanded = os.path.expanduser(os.path.expandvars(configured_exiftool))
        if os.path.sep in expanded:
            candidate = Path(expanded)
            exiftool = (
                str(candidate)
                if candidate.is_file() and os.access(candidate, os.X_OK)
                else None
            )
        else:
            exiftool = shutil.which(expanded)
    else:
        exiftool = shutil.which("exiftool")
    checks.append(
        _check(
            "exiftool",
            bool(exiftool),
            "available" if exiftool else "configured executable is unavailable" if configured_exiftool else "not found in PATH/config",
        )
    )
    return checks


def _check_legacy_collector_service() -> Check:
    """Read-only guard against the old systemd collector owning the camera.

    The retired source entrypoints cannot protect a stale unit from a previous
    installation.  A live legacy service is therefore a commissioning error,
    while hosts without systemd are reported as a warning rather than guessed.
    """

    systemctl = shutil.which("systemctl")
    if not systemctl:
        return Check(
            "legacy-collector-service",
            "warning",
            "systemctl unavailable; verify legacy collector is not running",
        )
    try:
        active = subprocess.run(
            [systemctl, "is-active", "--quiet", "hotspot-flir-collector.service"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(
            "legacy-collector-service",
            "warning",
            f"could not determine legacy collector state ({type(exc).__name__})",
        )
    if active.returncode == 0:
        return Check(
            "legacy-collector-service",
            "error",
            "hotspot-flir-collector.service is active; stop and mask it before dashboard operation",
        )
    if active.returncode == 4:
        return Check("legacy-collector-service", "ok", "not installed")
    if active.returncode != 3:
        return Check(
            "legacy-collector-service",
            "error",
            f"could not establish inactive collector state (systemctl exit {active.returncode})",
        )

    # An inactive but enabled unit can be started automatically at boot or by
    # a dependency later in the shift.  It is just as unsafe as a running
    # collector because it owns the same camera outside the dashboard lock.
    # The commissioning contract therefore accepts only a masked old unit (or
    # a unit absent from the host), not merely an inactive/disabled one.
    try:
        enabled = subprocess.run(
            [systemctl, "is-enabled", "hotspot-flir-collector.service"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(
            "legacy-collector-service",
            "error",
            f"could not verify collector mask state ({type(exc).__name__})",
        )
    state = enabled.stdout.strip().lower()
    if state == "masked":
        return Check("legacy-collector-service", "ok", "inactive and masked")
    if enabled.returncode == 4 or state in {"not-found", ""} and enabled.returncode == 4:
        return Check("legacy-collector-service", "ok", "not installed")
    return Check(
        "legacy-collector-service",
        "error",
        "hotspot-flir-collector.service is inactive but not masked "
        f"(state: {state or enabled.returncode}); mask it before dashboard operation",
    )


def _check_storage(cfg: AppConfig) -> list[Check]:
    dataset = _dataset_path(cfg)
    parent = dataset.parent
    overlay = _overlay_path(cfg)
    overlay_parent = overlay.parent
    homography = _homography_path(cfg)
    homography_parent = homography.parent
    overlay_ready = overlay.is_dir() and os.access(overlay, os.W_OK | os.X_OK)
    overlay_creatable = (
        not overlay.exists()
        and overlay_parent.is_dir()
        and os.access(overlay_parent, os.W_OK | os.X_OK)
    )
    return [
        _check("dataset:directory", dataset.is_dir(), str(dataset)),
        _check(
            "dataset:writable",
            dataset.is_dir() and os.access(dataset, os.W_OK | os.X_OK),
            "writable" if dataset.is_dir() and os.access(dataset, os.W_OK | os.X_OK) else str(dataset),
        ),
        _check(
            "dataset:parent",
            parent.is_dir() and os.access(parent, os.W_OK | os.X_OK),
            "parent writable" if parent.is_dir() and os.access(parent, os.W_OK | os.X_OK) else str(parent),
        ),
        _check(
            "overlay:writable",
            overlay_ready or overlay_creatable,
            "writable" if overlay_ready else "will be created by dashboard" if overlay_creatable else str(overlay),
        ),
        _check(
            "calibration:parent",
            homography_parent.is_dir()
            and os.access(homography_parent, os.W_OK | os.X_OK),
            "writable" if homography_parent.is_dir()
            and os.access(homography_parent, os.W_OK | os.X_OK) else str(homography_parent),
        ),
    ]


def _check_atomic_replace_parent(name: str, path: Path) -> Check:
    """Verify the directory rights required by atomic config/env persistence."""

    parent = path.parent
    writable = parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
    return _check(
        name,
        writable,
        "atomic replace supported" if writable else f"parent is not writable/searchable: {parent}",
    )


def _check_dashboard_runtime_paths() -> list[Check]:
    """Verify launcher-provided writable paths without creating anything."""

    configured_log_dir = os.environ.get("THERMOGUARD_LOG_DIR", "").strip()
    log_dir = (
        Path(os.path.expanduser(os.path.expandvars(configured_log_dir)))
        if configured_log_dir
        else PROJECT_ROOT / "logs"
    )
    log_dir = log_dir.resolve(strict=False)
    checks = [
        _check(
            "dashboard:log-directory",
            log_dir.is_dir() and os.access(log_dir, os.W_OK | os.X_OK),
            "writable" if log_dir.is_dir() and os.access(log_dir, os.W_OK | os.X_OK) else str(log_dir),
        )
    ]

    configured_env = os.environ.get("THERMOGUARD_DASHBOARD_ENV", "").strip()
    if not configured_env:
        checks.append(
            Check(
                "dashboard:environment-file",
                "warning",
                "not set; factory launcher must provide THERMOGUARD_DASHBOARD_ENV",
            )
        )
        return checks

    env_path = Path(
        os.path.expanduser(os.path.expandvars(configured_env))
    )
    try:
        mode = env_path.stat().st_mode & 0o777
        env_ok = env_path.is_file() and not env_path.is_symlink() and mode == 0o600
    except OSError:
        env_ok = False
    checks.append(
        _check(
            "dashboard:environment-file",
            env_ok and os.access(env_path, os.R_OK | os.W_OK),
            "protected 0600 file" if env_ok and os.access(env_path, os.R_OK | os.W_OK) else str(env_path),
        )
    )
    checks.append(
        _check_atomic_replace_parent("dashboard:environment-parent", env_path)
    )
    return checks


def _check_dashboard_lock() -> Check:
    """Verify the root-provisioned host-wide lock for a factory launch."""

    if not factory_mode_enabled():
        return Check(
            "dashboard:host-lock",
            "ok",
            "not enforced outside factory mode",
        )
    problem = inspect_default_lock_provisioning()
    return _check(
        "dashboard:host-lock",
        problem is None,
        "root-provisioned host-wide lock is ready" if problem is None else problem,
    )


def _check_online(cfg: AppConfig) -> list[Check]:
    timeout = min(10.0, max(1.0, float(cfg.backend.timeout_sec)))
    checks: list[Check] = []
    try:
        camera = requests.get(_camera_image_url(cfg.camera.ip), timeout=timeout)
        checks.append(
            _check(
                "camera:http",
                camera.status_code == 200,
                f"HTTP {camera.status_code}",
            )
        )
    except requests.RequestException as exc:
        checks.append(Check("camera:http", "error", f"{type(exc).__name__}"))

    try:
        readiness = requests.get(f"{cfg.backend.url.rstrip('/')}/api/ready", timeout=timeout)
        checks.append(
            _check(
                "backend:ready",
                readiness.status_code == 200,
                f"HTTP {readiness.status_code}",
            )
        )
    except requests.RequestException as exc:
        checks.append(Check("backend:ready", "error", f"{type(exc).__name__}"))
    return checks


def run_preflight(
    *,
    config_path: str | os.PathLike[str] | None = None,
    online: bool = False,
) -> list[Check]:
    """Return all read-only readiness results without terminating the process."""
    selected_config_path = (
        default_config_path()
        if config_path is None
        else Path(os.path.expanduser(os.path.expandvars(os.fspath(config_path)))).resolve(
            strict=False
        )
    )
    try:
        cfg = load_config(config_path, force_reload=True, strict=True)
    except ConfigValidationError as exc:
        return [Check("configuration", "error", str(exc))]

    checks = [Check("configuration", "ok", "strict validation passed")]
    checks.append(_check_atomic_replace_parent("configuration:parent", selected_config_path))
    checks.extend(_check_runtime_dependencies(cfg))
    checks.extend(_check_storage(cfg))
    checks.extend(_check_dashboard_runtime_paths())
    checks.append(_check_dashboard_lock())
    checks.append(_check_legacy_collector_service())
    if online:
        checks.extend(_check_online(cfg))
    else:
        checks.append(Check("online", "warning", "skipped; rerun with --online during an approved test window"))
    return checks


def exit_code(checks: Iterable[Check]) -> int:
    levels = {check.level for check in checks}
    if "error" in levels:
        return EXIT_ERROR
    if "warning" in levels:
        return EXIT_WARNING
    return EXIT_OK


def _print_human(checks: Iterable[Check]) -> None:
    labels = {"ok": "OK", "warning": "WARN", "error": "ERROR"}
    for check in checks:
        print(f"[{labels[check.level]}] {check.name}: {check.detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ThermoGuard read-only factory preflight")
    parser.add_argument("--config", help="approved config.json path")
    parser.add_argument(
        "--online",
        action="store_true",
        help="also probe the configured camera and backend readiness endpoint",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable results")
    args = parser.parse_args(argv)
    checks = run_preflight(config_path=args.config, online=args.online)
    if args.json:
        print(json.dumps([asdict(check) for check in checks], ensure_ascii=False))
    else:
        _print_human(checks)
    return exit_code(checks)


if __name__ == "__main__":
    raise SystemExit(main())

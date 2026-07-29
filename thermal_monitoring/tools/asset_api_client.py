"""Backend API client for factory, production-line, robot and camera identity."""

from __future__ import annotations

from dataclasses import dataclass

import requests


class AssetApiError(RuntimeError):
    """Raised when the asset hierarchy cannot be persisted by the backend."""


@dataclass(frozen=True)
class AssetRegistration:
    factory_id: int | None
    line_id: int | None
    robot_id: int
    camera_id: int


def _request(method: str, url: str, *, timeout: float, **kwargs) -> dict:
    try:
        response = requests.request(method, url, timeout=timeout, **kwargs)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise AssetApiError(f"Backend API 연결 실패: {exc}") from exc
    except ValueError as exc:
        raise AssetApiError("Backend가 올바른 JSON을 반환하지 않았습니다.") from exc
    if not isinstance(payload, dict):
        raise AssetApiError("Backend 응답 형식이 올바르지 않습니다.")
    if payload.get("status") == "error":
        raise AssetApiError(str(payload.get("error") or "Backend DB 저장 오류"))
    return payload


def _create(base_url: str, path: str, body: dict, id_key: str, timeout: float) -> int:
    payload = _request(
        "POST",
        f"{base_url.rstrip('/')}{path}",
        json=body,
        timeout=timeout,
    )
    if payload.get("status") != "created" or payload.get(id_key) is None:
        raise AssetApiError(f"{path} 등록 응답에 {id_key}가 없습니다: {payload}")
    return int(payload[id_key])


def _find_asset_hierarchy(
    base_url: str,
    camera_code: str,
    camera_ip: str,
    timeout: float,
) -> AssetRegistration | None:
    payload = _request(
        "GET",
        f"{base_url.rstrip('/')}/api/cameras",
        timeout=timeout,
    )
    cameras = [
        camera
        for camera in payload.get("cameras", [])
        if isinstance(camera, dict)
    ]
    matches = []
    for camera in cameras:
        if (
            str(camera.get("camera_code", "")).strip() == camera_code
            or str(camera.get("ip_address", "")).strip() == camera_ip
        ):
            matches.append(camera)
    if not matches and len(cameras) == 1:
        matches = cameras
    if not matches:
        return None

    camera = matches[0]
    required = ("robot_id", "camera_id")
    if any(camera.get(key) is None for key in required):
        return None
    return AssetRegistration(
        factory_id=(
            int(camera["factory_id"])
            if camera.get("factory_id") is not None
            else None
        ),
        line_id=(
            int(camera["line_id"])
            if camera.get("line_id") is not None
            else None
        ),
        robot_id=int(camera["robot_id"]),
        camera_id=int(camera["camera_id"]),
    )


def register_asset_hierarchy(
    *,
    base_url: str,
    timeout: float,
    factory_name: str,
    line_name: str,
    robot_code: str,
    robot_name: str,
    camera_code: str,
    camera_ip: str,
    factory_id: int | None = None,
    line_id: int | None = None,
    robot_id: int | None = None,
    camera_id: int | None = None,
) -> AssetRegistration:
    """Persist missing hierarchy levels using the existing FastAPI routes."""
    if not all((factory_id, line_id, robot_id, camera_id)):
        existing = _find_asset_hierarchy(
            base_url, camera_code, camera_ip, timeout,
        )
        if existing is not None:
            return AssetRegistration(
                factory_id=(
                    int(factory_id)
                    if factory_id
                    else existing.factory_id
                ),
                line_id=int(line_id) if line_id else existing.line_id,
                robot_id=existing.robot_id,
                camera_id=existing.camera_id,
            )

    if not factory_id:
        factory_id = _create(
            base_url, "/api/factories",
            {"factory_name": factory_name, "timezone": "Asia/Seoul"},
            "factory_id", timeout,
        )
    if not line_id:
        line_id = _create(
            base_url, "/api/production-lines",
            {
                "factory_id": factory_id,
                "line_name": line_name,
                "description": None,
            },
            "line_id", timeout,
        )
    if not robot_id:
        robot_id = _create(
            base_url, "/api/robots",
            {
                "line_id": line_id,
                "robot_code": robot_code,
                "robot_name": robot_name,
                "location_x": None,
                "location_y": None,
                "location_label": None,
                "enabled": True,
            },
            "robot_id", timeout,
        )
    if not camera_id:
        camera_id = _create(
            base_url, "/api/cameras",
            {
                "robot_id": robot_id,
                "camera_code": camera_code,
                "ip_address": camera_ip,
                "model_name": None,
                "capture_mode": "both",
                "normal_interval_sec": 30.0,
                "warning_interval_sec": 5.0,
                "enabled": True,
            },
            "camera_id", timeout,
        )
    return AssetRegistration(
        factory_id=int(factory_id),
        line_id=int(line_id),
        robot_id=int(robot_id),
        camera_id=int(camera_id),
    )

"""REST API client used by the ROI settings UI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import requests


class RoiApiError(RuntimeError):
    """Raised when an ROI cannot be synchronized through the backend API."""


@dataclass(frozen=True)
class RoiSyncResult:
    camera_id: int
    created: int
    unchanged: int


def _json(response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RoiApiError("백엔드가 올바른 JSON 응답을 반환하지 않았습니다.") from exc
    if isinstance(payload, dict) and payload.get("status") == "error":
        raise RoiApiError(str(payload.get("error") or "백엔드 처리 오류"))
    if not isinstance(payload, dict):
        raise RoiApiError("백엔드 응답 형식이 올바르지 않습니다.")
    return payload


def _get(base_url: str, path: str, timeout: float) -> dict:
    try:
        response = requests.get(f"{base_url.rstrip('/')}{path}", timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RoiApiError(f"백엔드 API 연결 실패: {exc}") from exc
    return _json(response)


def _post(base_url: str, path: str, payload: dict, timeout: float) -> dict:
    try:
        response = requests.post(
            f"{base_url.rstrip('/')}{path}",
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RoiApiError(f"ROI DB 저장 요청 실패: {exc}") from exc
    return _json(response)


def resolve_camera_id(
    base_url: str,
    camera_code: str,
    camera_ip: str,
    *,
    timeout: float = 5.0,
) -> int:
    """Resolve the DB integer camera_id from the GUI code or camera IP."""
    payload = _get(base_url, "/api/cameras", timeout)
    cameras = payload.get("cameras", [])
    if not isinstance(cameras, list):
        raise RoiApiError("카메라 조회 응답에 cameras 목록이 없습니다.")

    wanted_code = str(camera_code).strip()
    wanted_ip = str(camera_ip).strip()
    for camera in cameras:
        if not isinstance(camera, dict):
            continue
        if wanted_code and str(camera.get("camera_code", "")).strip() == wanted_code:
            return int(camera["camera_id"])
        if wanted_ip and str(camera.get("ip_address", "")).strip() == wanted_ip:
            return int(camera["camera_id"])

    raise RoiApiError(
        f"DB에서 카메라를 찾지 못했습니다. camera_code={wanted_code}, ip={wanted_ip}"
    )


def sync_rois(
    base_url: str,
    camera_code: str,
    camera_ip: str,
    rois: Iterable,
    *,
    timeout: float = 5.0,
    database_camera_id: int | None = None,
) -> RoiSyncResult:
    """Send changed thermal-coordinate ROIs through existing FastAPI routes."""
    camera_id = (
        int(database_camera_id)
        if database_camera_id is not None
        else resolve_camera_id(
            base_url, camera_code, camera_ip, timeout=timeout,
        )
    )
    payload = _get(base_url, "/api/rois", timeout)
    rows = payload.get("rois", [])
    if not isinstance(rows, list):
        raise RoiApiError("ROI 조회 응답에 rois 목록이 없습니다.")

    latest_by_name: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or int(row.get("camera_id", -1)) != camera_id:
            continue
        name = str(row.get("roi_name", ""))
        if name not in latest_by_name or int(row.get("version", 1)) > int(
            latest_by_name[name].get("version", 1)
        ):
            latest_by_name[name] = row

    created = 0
    unchanged = 0
    for roi in rois:
        name = str(roi.name)
        coordinates = (int(roi.x1), int(roi.y1), int(roi.x2), int(roi.y2))
        previous = latest_by_name.get(name)
        if previous is not None:
            old_coordinates = tuple(
                int(previous[key]) for key in ("x1", "y1", "x2", "y2")
            )
            if old_coordinates == coordinates and bool(previous.get("enabled", True)):
                unchanged += 1
                continue
            version = int(previous.get("version", 1)) + 1
        else:
            version = 1

        result = _post(
            base_url,
            "/api/rois",
            {
                "camera_id": camera_id,
                "roi_name": name,
                "x1": coordinates[0],
                "y1": coordinates[1],
                "x2": coordinates[2],
                "y2": coordinates[3],
                "version": version,
                "enabled": True,
            },
            timeout,
        )
        if result.get("status") != "created" or result.get("roi_id") is None:
            raise RoiApiError(f"ROI 저장 응답을 확인할 수 없습니다: {result}")
        created += 1

    return RoiSyncResult(camera_id=camera_id, created=created, unchanged=unchanged)

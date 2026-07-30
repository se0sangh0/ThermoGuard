"""REST API client for keeping ROI threshold profiles ready for measurements."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import requests


class ThresholdApiError(RuntimeError):
    """Raised when threshold profiles cannot be synchronized."""


@dataclass(frozen=True)
class ThresholdSyncResult:
    camera_id: int
    roi_ids: tuple[int, ...]
    created: int
    updated: int


def _request(
    method: str,
    url: str,
    *,
    timeout: float,
    **kwargs,
) -> dict:
    try:
        response = requests.request(method, url, timeout=timeout, **kwargs)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise ThresholdApiError(f"Threshold API 연결 실패: {exc}") from exc
    except ValueError as exc:
        raise ThresholdApiError(
            "Threshold API가 올바른 JSON을 반환하지 않았습니다."
        ) from exc
    if not isinstance(payload, dict):
        raise ThresholdApiError("Threshold API 응답 형식이 올바르지 않습니다.")
    if payload.get("status") == "error":
        raise ThresholdApiError(
            str(payload.get("error") or "Threshold DB 저장 오류")
        )
    return payload


def _optional_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sync_threshold_profiles(
    *,
    base_url: str,
    timeout: float,
    camera_id: int,
    roi_ids: Iterable[int],
    baseline_temp: float,
    warning_delta: float,
    critical_delta: float,
    min_hotspot_size: int,
    min_hotspot_size_max: int,
    alarm_cooldown_sec: float,
) -> ThresholdSyncResult:
    """Create or update the active exact-match profile for every DB ROI."""
    normalized_roi_ids = tuple(
        sorted({
            normalized
            for roi_id in roi_ids
            if (normalized := _optional_int(roi_id)) is not None
        })
    )
    normalized_camera_id = int(camera_id)
    if not normalized_roi_ids:
        return ThresholdSyncResult(
            camera_id=normalized_camera_id,
            roi_ids=(),
            created=0,
            updated=0,
        )

    root = base_url.rstrip("/")
    payload = _request(
        "GET",
        f"{root}/api/thresholds",
        timeout=timeout,
    )
    thresholds = payload.get("thresholds", [])
    if not isinstance(thresholds, list):
        raise ThresholdApiError(
            "Threshold 조회 응답에 thresholds 목록이 없습니다."
        )

    active_by_roi: dict[int, list[dict]] = {}
    for threshold in thresholds:
        if not isinstance(threshold, dict):
            continue
        threshold_camera_id = _optional_int(threshold.get("camera_id"))
        threshold_roi_id = _optional_int(threshold.get("roi_id"))
        if (
            threshold_camera_id != normalized_camera_id
            or threshold_roi_id not in normalized_roi_ids
            or threshold.get("valid_to") is not None
        ):
            continue
        active_by_roi.setdefault(threshold_roi_id, []).append(threshold)

    values = {
        "baseline_temp": float(baseline_temp),
        "warning_delta": float(warning_delta),
        "critical_delta": float(critical_delta),
        "min_hotspot_size": int(min_hotspot_size),
        "min_hotspot_size_max": int(min_hotspot_size_max),
        "alarm_cooldown_sec": int(alarm_cooldown_sec),
    }
    created = 0
    updated = 0
    for roi_id in normalized_roi_ids:
        matching = active_by_roi.get(roi_id, [])
        if matching:
            latest = max(
                matching,
                key=lambda row: int(row.get("threshold_id", 0)),
            )
            threshold_id = _optional_int(latest.get("threshold_id"))
            if threshold_id is None:
                raise ThresholdApiError(
                    f"ROI {roi_id}의 threshold_id를 확인할 수 없습니다."
                )
            update = _request(
                "PATCH",
                f"{root}/api/thresholds/{threshold_id}",
                timeout=timeout,
                json=values,
            )
            if update.get("status") != "updated":
                raise ThresholdApiError(
                    f"ROI {roi_id} threshold 수정 응답이 올바르지 않습니다: "
                    f"{update}"
                )
            updated += 1
            continue

        create = _request(
            "POST",
            f"{root}/api/thresholds",
            timeout=timeout,
            json={
                "camera_id": normalized_camera_id,
                "roi_id": roi_id,
                **values,
            },
        )
        if create.get("status") != "created" or create.get("threshold_id") is None:
            raise ThresholdApiError(
                f"ROI {roi_id} threshold 생성 응답이 올바르지 않습니다: "
                f"{create}"
            )
        created += 1

    return ThresholdSyncResult(
        camera_id=normalized_camera_id,
        roi_ids=normalized_roi_ids,
        created=created,
        updated=updated,
    )

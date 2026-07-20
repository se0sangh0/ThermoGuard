"""
threshold.py - Threshold 판단 및 상태 머신

ROI 온도 통계값을 받아 Normal / Warning / Critical 상태를 판정하고
상태 변화 시 알림 여부를 결정합니다.

이중 판정 경로:
  1. 95th percentile 기준: 95th >= baseline + delta AND cluster >= 3px
  2. max 온도 기준:    max >= baseline + critical_delta AND cluster >= 10px
     (ROI 대비 소수 픽셀만 과열되어 95th가 낮게 나오는 경우를 보완)
"""

import time
from dataclasses import dataclass, field
from enum import Enum

from ..config import load_config

_cfg = load_config()
MIN_HOTSPOT_SIZE = _cfg.hotspot.min_size       # 95th percentile 경로 최소 클러스터 크기
MIN_HOTSPOT_SIZE_MAX = _cfg.hotspot.min_size_max  # max 온도 경로 최소 클러스터 크기 (노이즈 방지용 상향)


class Status(Enum):
    NORMAL = "Normal"
    WARNING = "Warning"
    CRITICAL = "Critical"


@dataclass
class MonitorState:
    status: Status = Status.NORMAL
    last_alarm_time: float = 0.0
    alarm_cooldown: float = _cfg.monitoring.alarm_cooldown_sec  # from config.json


def evaluate_threshold(
    hot_temp: float,
    max_temp: float,
    baseline: float = 35.0,
    warning_delta: float = 15.0,
    critical_delta: float = 25.0,
    max_hotspot_size: int = 0,
) -> Status:
    """
    이중 경로 온도 상태 판정.

    경로 1 (95th percentile): 넓은 영역이 서서히 과열될 때 감지.
        cluster >= 3px 필수 (1~2px 노이즈 제거).

    경로 2 (max 온도): ROI 대비 소수 픽셀만 국소 과열되어
        95th가 묻히는 경우를 보완. cluster >= 10px로 노이즈 방지.
    """
    cluster_95 = max_hotspot_size >= MIN_HOTSPOT_SIZE
    cluster_max = max_hotspot_size >= MIN_HOTSPOT_SIZE_MAX

    # 경로 1: 95th percentile 기반
    critical_95 = hot_temp >= baseline + critical_delta and cluster_95
    warning_95 = hot_temp >= baseline + warning_delta and cluster_95

    # 경로 2: max 온도 기반 (국소 고온 보완)
    hot_max = max_temp >= baseline + critical_delta and cluster_max

    if critical_95 or (hot_max and warning_95):
        return Status.CRITICAL
    elif warning_95 or hot_max:
        return Status.WARNING
    else:
        return Status.NORMAL


def should_alarm(new_status: Status, state: MonitorState) -> bool:
    """Critical 상태 변화일 때만 알림. Warning은 인터벌만 전환."""
    if new_status != Status.CRITICAL:
        return False
    if new_status == state.status:
        return False
    now = time.time()
    if state.last_alarm_time > 0 and (now - state.last_alarm_time) < state.alarm_cooldown:
        return False
    return True


def evaluate_with_state(
    hot_temp: float,
    max_temp: float,
    mean_temp: float,
    baseline: float,
    warning_delta: float,
    critical_delta: float,
    state: MonitorState,
    over_temp_pixels: int = 0,
    max_hotspot_size: int = 0,
) -> tuple[Status, bool]:
    new_status = evaluate_threshold(
        hot_temp, max_temp, baseline, warning_delta, critical_delta,
        max_hotspot_size,
    )
    do_alarm = should_alarm(new_status, state)
    return new_status, do_alarm

"""
threshold.py - Threshold 판단 및 상태 머신

ROI 온도 통계값을 받아 Normal / Warning / Critical 상태를 판정하고
상태 변화 시 알림 여부를 결정합니다.

이중 판정 경로:
  1. 95th percentile 기준: 95th >= baseline + delta AND cluster >= 3px
  2. max 온도 기준:    max >= baseline + critical_delta AND cluster >= 10px
     (ROI 대비 소수 픽셀만 과열되어 95th가 낮게 나오는 경우를 보완)

다중 ROI 지원:
  - state.status 는 모든 ROI 중 최악 상태를 집계 (capture interval 전환용)
  - should_alarm / evaluate_with_state 에 roi_name 을 전달하면
    ROI마다 독립적인 알람 쿨다운을 적용한다.
  - roi_name=None 이면 기존 단일 ROI 하위 호환 동작.
"""

import time
from dataclasses import dataclass, field
from enum import Enum

from ..config import load_config
from ..logger import get_logger

_log = get_logger("analysis.threshold")

_cfg = load_config()
MIN_HOTSPOT_SIZE = _cfg.hotspot.min_size       # 95th percentile 경로 최소 클러스터 크기
MIN_HOTSPOT_SIZE_MAX = _cfg.hotspot.min_size_max  # max 온도 경로 최소 클러스터 크기 (노이즈 방지용 상향)


class Status(Enum):
    NORMAL = "Normal"
    WARNING = "Warning"
    CRITICAL = "Critical"

# 상태 심각도 순위 (다중 ROI 최악 선정용: 높을수록 심각)
_STATUS_RANK = {Status.NORMAL: 0, Status.WARNING: 1, Status.CRITICAL: 2}


@dataclass
class _RoiState:
    """ROI별 독립 알람 상태."""
    status: Status = Status.NORMAL
    last_alarm_time: float = 0.0
    last_alarm_attempt: float = 0.0
    alarm_pending: bool = False


@dataclass
class MonitorState:
    # ── 하위 호환: 단일 ROI 또는 최악-집계용 필드 ──
    status: Status = Status.NORMAL
    last_alarm_time: float = 0.0
    alarm_cooldown: float = _cfg.monitoring.alarm_cooldown_sec  # from config.json
    alarm_pending: bool = False        # 전송 실패한 CRITICAL 알람이 재시도 대기 중인지
    last_alarm_attempt: float = 0.0    # 마지막 전송 시도 시각 (재시도 백오프용)

    # ── 다중 ROI: 이름 → _RoiState (lazy 초기화) ──
    _roi_states: dict = field(default_factory=dict)

    def roi_state(self, roi_name: str) -> _RoiState:
        if roi_name not in self._roi_states:
            self._roi_states[roi_name] = _RoiState()
        return self._roi_states[roi_name]


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


def should_alarm(new_status: Status, state: MonitorState, *, roi_name: str | None = None) -> bool:
    """Critical 상태 변화일 때만 알림. Warning은 인터벌만 전환.

    roi_name이 주어지면 해당 ROI별 독립 쿨다운을 적용하고,
    None이면 state.last_alarm_time 기준 전역 쿨다운을 적용한다 (하위 호환).
    """
    if new_status != Status.CRITICAL:
        return False

    if roi_name is not None:
        rs = state.roi_state(roi_name)
        if new_status == rs.status:
            return False
        now = time.time()
        if rs.last_alarm_time > 0 and (now - rs.last_alarm_time) < state.alarm_cooldown:
            return False
        return True
    else:
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
    *,
    roi_name: str | None = None,
) -> tuple[Status, bool]:
    new_status = evaluate_threshold(
        hot_temp, max_temp, baseline, warning_delta, critical_delta,
        max_hotspot_size,
    )
    do_alarm = should_alarm(new_status, state, roi_name=roi_name)

    # 상태 전환 로그 (파일 로그)
    prev_status = state.status
    if roi_name is not None:
        prev_status = state.roi_state(roi_name).status
        context = f"[{roi_name}] "
    else:
        context = ""

    if new_status != prev_status:
        _log.info(
            "STATE CHANGE: %s%s → %s | hot=%.1f°C max=%.1f°C mean=%.1f°C "
            "baseline=%.1f°C warn_delta=%.1f°C crit_delta=%.1f°C "
            "hotspot_size=%d over_pixels=%d",
            context, prev_status.value, new_status.value,
            hot_temp, max_temp, mean_temp,
            baseline, warning_delta, critical_delta,
            max_hotspot_size, over_temp_pixels,
        )
    if do_alarm:
        _log.info("ALARM TRIGGERED: %s%s | hot=%.1f°C max=%.1f°C", context, new_status.value, hot_temp, max_temp)
    elif new_status == Status.CRITICAL and not do_alarm:
        cooldown_remaining = state.alarm_cooldown
        if roi_name is not None:
            rs = state.roi_state(roi_name)
            cooldown_remaining = state.alarm_cooldown - (time.time() - rs.last_alarm_time)
        else:
            cooldown_remaining = state.alarm_cooldown - (time.time() - state.last_alarm_time)
        _log.info("ALARM SUPPRESSED: %scooldown active (%.0fs remaining)", context, cooldown_remaining)

    return new_status, do_alarm


def evaluate_rois_with_state(
    roi_results: list,
    *,
    baseline: float,
    warning_delta: float,
    critical_delta: float,
    state: MonitorState,
) -> tuple[list[dict], dict, bool]:
    """다중 ROI를 개별 판정하고 최악 ROI/알람 여부를 집계한다."""
    per_roi_statuses: list[dict] = []
    for rr in roi_results:
        s, a = evaluate_with_state(
            hot_temp=rr.hot_temp_95,
            max_temp=rr.max_temp,
            mean_temp=rr.mean_temp,
            baseline=baseline,
            warning_delta=warning_delta,
            critical_delta=critical_delta,
            state=state,
            over_temp_pixels=rr.over_temp_pixels,
            max_hotspot_size=rr.max_hotspot_size,
            roi_name=rr.roi_name if rr.roi_name else None,
        )
        per_roi_statuses.append({"roi_name": rr.roi_name, "status": s, "alarm": a, "roi": rr})

    worst = max(per_roi_statuses, key=lambda x: (_STATUS_RANK[x["status"]], x["roi"].hot_temp_95))
    do_alarm = any(ps["alarm"] for ps in per_roi_statuses)
    return per_roi_statuses, worst, do_alarm


def apply_roi_state_updates(state: MonitorState, per_roi_statuses: list[dict]) -> None:
    """ROI별 상태/알람 시각을 MonitorState에 반영한다."""
    for ps in per_roi_statuses:
        roi_name = ps["roi_name"]
        if roi_name:
            roi_state = state.roi_state(roi_name)
            roi_state.status = ps["status"]
            if ps["alarm"]:
                roi_state.last_alarm_time = time.time()
                roi_state.alarm_pending = False

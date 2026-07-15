"""
test.py - Threshold 판단 및 상태 머신 시뮬레이션 테스트

※ 임의의 온도 시나리오로 threshold 모듈의 동작을 확인합니다.
   실제 데이터 연동은 pipeline.py를 사용하세요.
"""

import time
from collections import deque

from _encoding import setup_encoding
from config import load_config
from threshold import Status, MonitorState, evaluate_threshold, should_alarm

setup_encoding()

cfg = load_config()

# ============================================================
# 테스트 설정 (config.json 기준)
# ============================================================
BASELINE_TEMP = cfg.roi.baseline_temp
WARNING_DELTA = cfg.roi.warning_delta
CRITICAL_DELTA = cfg.roi.critical_delta
ALARM_COOLDOWN = cfg.monitoring.alarm_cooldown_sec

# ------------------------------------------------------------
# 임시 온도 시나리오
# ------------------------------------------------------------
SIMULATED_READINGS = [
    # (시점, 95th, max, mean, cluster_size)
    ("13:00", 36.0, 37.2, 34.5, 0),   # Normal (cluster 부족)
    ("13:10", 36.5, 38.1, 34.8, 0),   # Normal
    ("13:20", 50.2, 55.3, 42.1, 15),  # 95th 경로 → Warning
    ("13:30", 52.1, 57.8, 43.5, 18),
    ("13:40", 37.0, 66.1, 42.0, 30),  # max 경로 → Warning (max 높지만 95th 낮음)
    ("13:50", 60.5, 66.1, 48.3, 30),  # 95th 경로 → Critical
    ("14:00", 61.2, 67.5, 49.1, 35),  # 쿨다운 중
    ("14:10", 38.0, 40.1, 35.2, 0),   # 정상 복귀
]


# ============================================================
# 이력 관리
# ============================================================
_history: deque = deque(maxlen=100)


# ============================================================
# 메인 시뮬레이션 루프
# ============================================================
def main():
    print("=" * 50)
    print("  Robot Thermal Monitoring - Threshold Sim")
    print("=" * 50)
    print(f"  Baseline: {BASELINE_TEMP}C  Warning: +{WARNING_DELTA}C  Critical: +{CRITICAL_DELTA}C")
    print(f"  Cooldown: {ALARM_COOLDOWN // 60} min")
    print("=" * 50)

    state = MonitorState()
    global _history
    _history.clear()
    alarm_count = 0

    for timestamp, hot_temp, max_temp, mean_temp, cluster_size in SIMULATED_READINGS:
        new_status = evaluate_threshold(
            hot_temp, max_temp, BASELINE_TEMP, WARNING_DELTA, CRITICAL_DELTA,
            max_hotspot_size=cluster_size,
        )
        do_alarm = should_alarm(new_status, state)
        prev_status = state.status
        state.status = new_status

        _history.append({
            "time": timestamp, "95th": hot_temp, "max": max_temp,
            "mean": mean_temp, "status": new_status.value,
        })

        transition = ""
        if do_alarm:
            transition = f"  <<< {prev_status.value} -> {new_status.value} >>>"
            alarm_count += 1

        print(f"[{timestamp}] 95th={hot_temp:.1f}C max={max_temp:.1f}C "
              f"mean={mean_temp:.1f}C -> {new_status.value}{transition}")

        if do_alarm:
            state.last_alarm_time = time.time()

        time.sleep(0.5)

    print("\n[History]")
    print(f"{'Time':<8} {'95th':<8} {'max':<8} {'mean':<8} {'Status':<10}")
    print("-" * 45)
    for r in _history:
        print(f"{r['time']:<8} {r['95th']:<8.1f} {r['max']:<8.1f} "
              f"{r['mean']:<8.1f} {r['status']:<10}")
    print(f"\nTotal alarms: {alarm_count}")


if __name__ == "__main__":
    main()

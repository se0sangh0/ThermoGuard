import subprocess
import time
import signal
import sys
from datetime import datetime


# ============================================================
# Hotspot Guard FLIR Collector
# ============================================================

PYTHON_PATH = "/usr/bin/python3"

GRAB_SCRIPT = (
    "/home/autoit/Project_hotspot/backend/"
    "collector/grab_flir_temperature.py"
)

# 정상 상태 측정 주기
INTERVAL_SEC = 30

# 오류 발생 시 재시도 대기시간
ERROR_RETRY_SEC = 5

running = True


# ============================================================
# 종료 신호 처리
# systemctl stop 시 정상 종료하기 위함
# ============================================================

def stop_collector(signum, frame):
    global running

    print()
    print("========================================")
    print(" FLIR Collector 종료 요청")
    print("========================================")

    running = False


signal.signal(
    signal.SIGTERM,
    stop_collector
)

signal.signal(
    signal.SIGINT,
    stop_collector
)


# ============================================================
# FLIR 측정 1회 실행
# ============================================================

def run_measurement():
    print()
    print("========================================")
    print(" FLIR A50 자동 측정 시작")
    print("========================================")

    print(
        "측정 시각:",
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

    try:
        result = subprocess.run(
            [
                PYTHON_PATH,
                GRAB_SCRIPT
            ],
            check=False,
            timeout=20
        )

        if result.returncode == 0:
            print()
            print("FLIR 측정 및 FastAPI 전송 성공")

            return True

        print()
        print(
            "FLIR 측정 실패"
            f" (return code: {result.returncode})"
        )

        return False

    except subprocess.TimeoutExpired:

        print()
        print("FLIR 측정 프로그램 실행 시간 초과")

        return False

    except Exception as e:

        print()
        print(
            f"Collector 오류: {e}"
        )

        return False


# ============================================================
# Collector Main
# ============================================================

def main():
    print()
    print("========================================")
    print(" Hotspot Guard FLIR Collector")
    print("========================================")

    print(
        f"측정 주기: {INTERVAL_SEC}초"
    )

    print(
        f"측정 프로그램: {GRAB_SCRIPT}"
    )

    print()
    print("Collector 시작")

    while running:

        success = run_measurement()

        # 오류가 났으면 5초 후 다시 시도
        if not success:

            print(
                f"{ERROR_RETRY_SEC}초 후 재시도합니다."
            )

            for _ in range(ERROR_RETRY_SEC):

                if not running:
                    break

                time.sleep(1)

            continue

        # 정상 측정 후 30초 대기
        print()
        print(
            f"다음 측정까지 {INTERVAL_SEC}초 대기"
        )

        for _ in range(INTERVAL_SEC):

            if not running:
                break

            time.sleep(1)

    print()
    print("========================================")
    print(" FLIR Collector 정상 종료")
    print("========================================")


if __name__ == "__main__":
    main()

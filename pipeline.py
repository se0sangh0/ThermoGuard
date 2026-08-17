#!/usr/bin/env python3
"""Retired runtime entry point.

배치 분석도 운영 데이터·알림을 중복 처리할 수 있어 직접 실행하지 않는다.
"""

from thermal_monitoring.operational_mode import exit_legacy_operation


def main() -> None:
    exit_legacy_operation("python pipeline.py")

if __name__ == "__main__":
    main()

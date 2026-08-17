#!/usr/bin/env python3
"""Retired runtime entry point.

실시간 수집과 분석은 Product Dashboard에서만 시작한다.
"""

from thermal_monitoring.operational_mode import exit_legacy_operation


def main() -> None:
    exit_legacy_operation("python monitor.py")

if __name__ == "__main__":
    main()

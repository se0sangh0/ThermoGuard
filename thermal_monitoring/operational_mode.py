"""ThermoGuard의 지원 운영 경로를 한 곳으로 고정한다.

카메라 수집, 상태 전이, 알림, DB 기록은 서로 독립적으로 실행되면 안 된다.
따라서 운영 환경에서는 Product Dashboard만 이 흐름을 시작할 수 있다.
"""

from __future__ import annotations

import sys
from typing import NoReturn


DASHBOARD_COMMAND = "python dashboard.py"


class LegacyOperationalPathError(RuntimeError):
    """Retired runtime path를 실행하려 할 때 발생한다."""


def legacy_operation_message(entrypoint: str) -> str:
    """운영자가 바로 따라 할 수 있는 단일 경로 안내를 반환한다."""
    return (
        f"{entrypoint}은(는) 더 이상 ThermoGuard 운영 진입점이 아닙니다. "
        f"프로젝트 루트에서 `{DASHBOARD_COMMAND}`만 사용하세요. "
        "FastAPI 백엔드는 대시보드의 DB·알림 연동을 위한 지원 서비스로 유지해야 합니다."
    )


def reject_legacy_operation(entrypoint: str) -> NoReturn:
    """라이브러리 호출에서 비운영 경로 사용을 명시적으로 차단한다."""
    raise LegacyOperationalPathError(legacy_operation_message(entrypoint))


def exit_legacy_operation(entrypoint: str) -> NoReturn:
    """스크립트 실행 시 stack trace 없이 안전하게 종료한다."""
    print(legacy_operation_message(entrypoint), file=sys.stderr)
    raise SystemExit(2)

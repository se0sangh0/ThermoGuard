#!/usr/bin/env python3
"""알림 전송 이력 API 계약을 점검하는 무의존성 프로브."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DELIVERIES_PATH = "/api/notification-deliveries"
DIAGNOSTIC_MARKER = "ThermoGuard notification API diagnostic probe"

EXIT_OK = 0
EXIT_ROUTE_MISSING = 2
EXIT_HTTP_ERROR = 3
EXIT_NON_JSON = 4
EXIT_APPLICATION_ERROR = 5
EXIT_CONNECTION_ERROR = 6
EXIT_TIMEOUT = 7
EXIT_CONTRACT_ERROR = 8

Transport = Callable[
    [str, str, Optional[bytes], float],
    Tuple[int, bytes],
]


@dataclass
class ProbeError(Exception):
    exit_code: int
    message: str


def urllib_transport(
    method: str,
    url: str,
    body: Optional[bytes],
    timeout: float,
) -> Tuple[int, bytes]:
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url=url,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def positive_timeout(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "timeout은 양수여야 합니다."
        ) from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("timeout은 양수여야 합니다.")
    return parsed


def normalize_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized:
        raise argparse.ArgumentTypeError(
            "base URL은 비어 있을 수 없습니다."
        )
    parsed = urllib.parse.urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError(
            "base URL은 http:// 또는 https:// 주소여야 합니다."
        )
    return normalized


def parse_json_response(status: int, body: bytes, context: str) -> Any:
    if status == 404:
        raise ProbeError(
            EXIT_ROUTE_MISSING,
            (
                f"{context}: 경로를 찾을 수 없습니다(HTTP 404). "
                "구버전 backend 또는 다른 app/process 가능성이 있습니다."
            ),
        )
    if not 200 <= status < 300:
        raise ProbeError(
            EXIT_HTTP_ERROR,
            f"{context}: HTTP 오류가 발생했습니다(HTTP {status}).",
        )

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProbeError(
            EXIT_NON_JSON,
            f"{context}: 2xx 응답이 JSON 형식이 아닙니다.",
        ) from error

    if isinstance(payload, dict) and payload.get("status") == "error":
        raise ProbeError(
            EXIT_APPLICATION_ERROR,
            f"{context}: application error 응답입니다: {payload!r}",
        )
    return payload


def request_json(
    transport: Transport,
    method: str,
    url: str,
    body: Optional[bytes],
    timeout: float,
    context: str,
) -> Any:
    try:
        status, response_body = transport(method, url, body, timeout)
    except (TimeoutError, socket.timeout) as error:
        raise ProbeError(
            EXIT_TIMEOUT,
            f"{context}: 요청 시간이 초과되었습니다.",
        ) from error
    except (urllib.error.URLError, ConnectionError, OSError) as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise ProbeError(
                EXIT_TIMEOUT,
                f"{context}: 요청 시간이 초과되었습니다.",
            ) from error
        raise ProbeError(
            EXIT_CONNECTION_ERROR,
            f"{context}: backend 연결에 실패했습니다: {reason}",
        ) from error

    return parse_json_response(status, response_body, context)


def validate_openapi(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ProbeError(
            EXIT_CONTRACT_ERROR,
            "OpenAPI 응답이 객체가 아닙니다.",
        )

    paths = payload.get("paths")
    route = paths.get(DELIVERIES_PATH) if isinstance(paths, dict) else None
    if not isinstance(route, dict):
        raise ProbeError(
            EXIT_ROUTE_MISSING,
            (
                f"OpenAPI paths에 정확한 {DELIVERIES_PATH} 경로가 없습니다. "
                "구버전 backend 또는 다른 app/process 가능성이 있습니다."
            ),
        )
    missing_methods = [
        method for method in ("get", "post") if method not in route
    ]
    if missing_methods:
        raise ProbeError(
            EXIT_ROUTE_MISSING,
            (
                f"{DELIVERIES_PATH} 경로에 "
                f"{', '.join(missing_methods).upper()} 정의가 없습니다. "
                "구버전 backend 또는 다른 app/process 가능성이 있습니다."
            ),
        )


def validate_collection(payload: Any, context: str) -> list[Any]:
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("count"), int)
        or isinstance(payload.get("count"), bool)
        or not isinstance(payload.get("deliveries"), list)
    ):
        raise ProbeError(
            EXIT_CONTRACT_ERROR,
            (
                f"{context}: 예상 계약 "
                "{count: int, deliveries: list}과 다릅니다."
            ),
        )
    return payload["deliveries"]


def run_probe(
    base_url: str,
    timeout: float,
    alert_id: Optional[int],
    transport: Transport = urllib_transport,
) -> int:
    openapi = request_json(
        transport,
        "GET",
        f"{base_url}/openapi.json",
        None,
        timeout,
        "OpenAPI 조회",
    )
    validate_openapi(openapi)

    collection_url = f"{base_url}{DELIVERIES_PATH}?limit=1"
    initial = request_json(
        transport,
        "GET",
        collection_url,
        None,
        timeout,
        "전송 이력 조회",
    )
    validate_collection(initial, "전송 이력 조회")

    if alert_id is None:
        print(
            "성공: OpenAPI와 전송 이력 조회 계약을 "
            "확인했습니다(읽기 전용)."
        )
        return EXIT_OK

    print(
        "경고: POST 진단을 실행하면 pending 전송 이력 행이 "
        "영구적으로 남습니다. 이 프로브는 삭제하지 않습니다.",
        file=sys.stderr,
    )
    post_payload = {
        "alert_id": alert_id,
        "delivery_status": "pending",
        "http_status": None,
        "retry_count": 0,
        "error_message": DIAGNOSTIC_MARKER,
    }
    encoded = json.dumps(
        post_payload,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    created = request_json(
        transport,
        "POST",
        f"{base_url}{DELIVERIES_PATH}",
        encoded,
        timeout,
        "전송 이력 생성",
    )
    if (
        not isinstance(created, dict)
        or created.get("status") != "created"
        or "delivery_id" not in created
        or not isinstance(created.get("delivery_id"), int)
        or isinstance(created.get("delivery_id"), bool)
        or created.get("alert_id") != alert_id
        or created.get("delivery_status") != "pending"
    ):
        raise ProbeError(
            EXIT_CONTRACT_ERROR,
            "전송 이력 생성 응답 계약이 올바르지 않습니다.",
        )

    delivery_id = created["delivery_id"]
    verify_url = f"{base_url}{DELIVERIES_PATH}?limit=100"
    verified = request_json(
        transport,
        "GET",
        verify_url,
        None,
        timeout,
        "생성 행 검증 조회",
    )
    deliveries = validate_collection(verified, "생성 행 검증 조회")
    if not any(
        isinstance(item, dict) and item.get("delivery_id") == delivery_id
        for item in deliveries
    ):
        raise ProbeError(
            EXIT_CONTRACT_ERROR,
            (
                f"생성된 delivery_id={delivery_id}를 GET ?limit=100에서 "
                "확인하지 못했습니다."
            ),
        )

    print(
        f"성공: delivery_id={delivery_id} 생성 및 조회를 확인했습니다."
    )
    print(
        "알림: 성공 여부와 관계없이 POST 진단 행은 영구적으로 "
        "남을 수 있으며 이 프로브는 삭제하지 않습니다.",
        file=sys.stderr,
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        type=normalize_base_url,
    )
    parser.add_argument("--timeout", default=5.0, type=positive_timeout)
    parser.add_argument("--alert-id", type=int)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_probe(args.base_url, args.timeout, args.alert_id)
    except ProbeError as error:
        print(f"오류: {error.message}", file=sys.stderr)
        if args.alert_id is not None:
            print(
                "알림: 검증 실패 후에도 POST 진단 행이 영구적으로 "
                "남을 수 있으며 이 프로브는 삭제하지 않습니다.",
                file=sys.stderr,
            )
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import importlib.util
import io
import json
import socket
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Optional


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "Project_hotspot"
    / "backend"
    / "check_notification_delivery_api.py"
)
SPEC = importlib.util.spec_from_file_location(
    "check_notification_delivery_api",
    MODULE_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"모듈을 불러올 수 없습니다: {MODULE_PATH}")
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


def json_bytes(value):
    return json.dumps(value).encode("utf-8")


OPENAPI = {
    "paths": {
        "/api/notification-deliveries": {
            "get": {},
            "post": {},
        }
    }
}


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(
        self,
        method: str,
        url: str,
        body: Optional[bytes],
        timeout: float,
    ):
        self.calls.append((method, url, body, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class NotificationDeliveryApiProbeTests(unittest.TestCase):
    def test_openapi_missing_route_is_route_missing(self):
        transport = FakeTransport(
            [(200, json_bytes({"paths": {}}))]
        )

        with self.assertRaises(probe.ProbeError) as captured:
            probe.run_probe(
                "http://127.0.0.1:8000",
                1.0,
                None,
                transport,
            )

        self.assertEqual(
            probe.EXIT_ROUTE_MISSING,
            captured.exception.exit_code,
        )
        self.assertIn("구버전 backend", captured.exception.message)
        self.assertEqual(1, len(transport.calls))

    def test_html_404_is_classified_before_json_parsing(self):
        transport = FakeTransport(
            [(404, b"<html><body>Not Found</body></html>")]
        )

        with self.assertRaises(probe.ProbeError) as captured:
            probe.run_probe(
                "http://127.0.0.1:8000",
                1.0,
                None,
                transport,
            )

        self.assertEqual(
            probe.EXIT_ROUTE_MISSING,
            captured.exception.exit_code,
        )
        self.assertIn(
            "다른 app/process 가능성",
            captured.exception.message,
        )

    def test_read_only_success_never_posts(self):
        transport = FakeTransport(
            [
                (200, json_bytes(OPENAPI)),
                (200, json_bytes({"count": 0, "deliveries": []})),
            ]
        )

        result = probe.run_probe(
            "http://127.0.0.1:8000",
            2.0,
            None,
            transport,
        )

        self.assertEqual(probe.EXIT_OK, result)
        self.assertEqual(
            ["GET", "GET"],
            [call[0] for call in transport.calls],
        )
        self.assertEqual(
            "http://127.0.0.1:8000/openapi.json",
            transport.calls[0][1],
        )
        self.assertEqual(
            (
                "http://127.0.0.1:8000"
                "/api/notification-deliveries?limit=1"
            ),
            transport.calls[1][1],
        )

    def test_post_contract_and_follow_up_get_verification(self):
        transport = FakeTransport(
            [
                (200, json_bytes(OPENAPI)),
                (200, json_bytes({"count": 0, "deliveries": []})),
                (
                    200,
                    json_bytes(
                        {
                            "status": "created",
                            "delivery_id": 71,
                            "alert_id": 42,
                            "delivery_status": "pending",
                        }
                    ),
                ),
                (
                    200,
                    json_bytes(
                        {
                            "count": 1,
                            "deliveries": [{"delivery_id": 71}],
                        }
                    ),
                ),
            ]
        )
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            result = probe.run_probe(
                "http://127.0.0.1:8000",
                3.0,
                42,
                transport,
            )

        self.assertEqual(probe.EXIT_OK, result)
        self.assertEqual(
            ["GET", "GET", "POST", "GET"],
            [call[0] for call in transport.calls],
        )
        post_call = transport.calls[2]
        self.assertEqual(
            (
                "http://127.0.0.1:8000"
                "/api/notification-deliveries"
            ),
            post_call[1],
        )
        self.assertEqual(
            {
                "alert_id": 42,
                "delivery_status": "pending",
                "http_status": None,
                "retry_count": 0,
                "error_message": probe.DIAGNOSTIC_MARKER,
            },
            json.loads(post_call[2].decode("utf-8")),
        )
        self.assertEqual(
            (
                "http://127.0.0.1:8000"
                "/api/notification-deliveries?limit=100"
            ),
            transport.calls[3][1],
        )
        self.assertIn("영구적으로 남", stderr.getvalue())
        self.assertIn("삭제하지 않습니다", stderr.getvalue())

    def test_http_200_status_error_is_application_error(self):
        transport = FakeTransport(
            [
                (200, json_bytes(OPENAPI)),
                (
                    200,
                    json_bytes(
                        {
                            "status": "error",
                            "error": "database unavailable",
                        }
                    ),
                ),
            ]
        )

        with self.assertRaises(probe.ProbeError) as captured:
            probe.run_probe(
                "http://127.0.0.1:8000",
                1.0,
                None,
                transport,
            )

        self.assertEqual(
            probe.EXIT_APPLICATION_ERROR,
            captured.exception.exit_code,
        )
        self.assertEqual(
            ["GET", "GET"],
            [call[0] for call in transport.calls],
        )

    def test_non_json_2xx_is_non_json_error(self):
        transport = FakeTransport([(200, b"not-json")])

        with self.assertRaises(probe.ProbeError) as captured:
            probe.run_probe(
                "http://127.0.0.1:8000",
                1.0,
                None,
                transport,
            )

        self.assertEqual(
            probe.EXIT_NON_JSON,
            captured.exception.exit_code,
        )

    def test_connection_failure_has_distinct_exit_code(self):
        transport = FakeTransport(
            [ConnectionRefusedError("connection refused")]
        )

        with self.assertRaises(probe.ProbeError) as captured:
            probe.run_probe(
                "http://127.0.0.1:8000",
                1.0,
                None,
                transport,
            )

        self.assertEqual(
            probe.EXIT_CONNECTION_ERROR,
            captured.exception.exit_code,
        )
        self.assertIn("연결에 실패", captured.exception.message)

    def test_timeout_has_distinct_exit_code(self):
        transport = FakeTransport([socket.timeout("timed out")])

        with self.assertRaises(probe.ProbeError) as captured:
            probe.run_probe(
                "http://127.0.0.1:8000",
                1.0,
                None,
                transport,
            )

        self.assertEqual(
            probe.EXIT_TIMEOUT,
            captured.exception.exit_code,
        )
        self.assertIn("시간이 초과", captured.exception.message)

    def test_non_2xx_is_classified_before_json_parsing(self):
        transport = FakeTransport(
            [(500, b"<html>failure</html>")]
        )

        with self.assertRaises(probe.ProbeError) as captured:
            probe.run_probe(
                "http://127.0.0.1:8000",
                1.0,
                None,
                transport,
            )

        self.assertEqual(
            probe.EXIT_HTTP_ERROR,
            captured.exception.exit_code,
        )

    def test_base_url_normalization_and_alert_id_type(self):
        parser = probe.build_parser()
        args = parser.parse_args(
            [
                "--base-url",
                "  http://localhost:8000///  ",
                "--alert-id",
                "12",
            ]
        )

        self.assertEqual("http://localhost:8000", args.base_url)
        self.assertEqual(12, args.alert_id)
        self.assertIs(type(args.alert_id), int)

    def test_timeout_must_be_positive(self):
        parser = probe.build_parser()
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["--timeout", "0"])

    def test_base_url_requires_http_scheme_and_host(self):
        parser = probe.build_parser()
        invalid_urls = [
            "httpx://127.0.0.1:8000",
            "127.0.0.1:8000",
        ]

        for invalid_url in invalid_urls:
            with self.subTest(invalid_url=invalid_url):
                with redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args(
                            ["--base-url", invalid_url]
                        )


if __name__ == "__main__":
    unittest.main()

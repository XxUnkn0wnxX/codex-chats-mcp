import json
import io
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, call, patch

import codex_chats_mcp as server


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class HTTPTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.error_log = patch.object(server, "_append_error_log")
        self.mock_error_log = self.error_log.start()
        self.addCleanup(self.error_log.stop)

    def _auth_path(self) -> tuple[tempfile.TemporaryDirectory, Path]:
        temp_dir = tempfile.TemporaryDirectory()
        auth_path = Path(temp_dir.name) / "auth.json"
        auth_path.write_text(
            '{"tokens":{"access_token":"access-token","account_id":"account-id"}}'
        )
        return temp_dir, auth_path

    def test_auth_headers_default_user_agent_and_credentials(self) -> None:
        temp_dir, auth_path = self._auth_path()
        self.addCleanup(temp_dir.cleanup)

        with patch.object(server, "AUTH_PATH", auth_path), patch.dict(
            os.environ, {server.USER_AGENT_ENV: ""}
        ):
            headers = server._auth_headers()

        self.assertEqual(
            headers,
            {
                "Authorization": "Bearer access-token",
                "ChatGPT-Account-ID": "account-id",
                "User-Agent": server.USER_AGENT,
                "Accept": "application/json",
            },
        )

    def test_user_agent_override_is_trimmed_and_whitespace_falls_back(self) -> None:
        temp_dir, auth_path = self._auth_path()
        self.addCleanup(temp_dir.cleanup)

        with patch.object(server, "AUTH_PATH", auth_path), patch.dict(
            os.environ, {server.USER_AGENT_ENV: "  Browser/1.0  "}
        ):
            self.assertEqual(server._auth_headers()["User-Agent"], "Browser/1.0")

        with patch.object(server, "AUTH_PATH", auth_path), patch.dict(
            os.environ, {server.USER_AGENT_ENV: " \t\n "}
        ):
            self.assertEqual(server._auth_headers()["User-Agent"], server.USER_AGENT)

    def test_request_success_decodes_json(self) -> None:
        response = FakeResponse(200, b'{"ok":true,"items":[1]}')
        with patch.object(
            server, "_auth_headers", return_value={"Authorization": "test"}
        ), patch.object(server.urllib.request, "urlopen", return_value=response) as urlopen:
            status, payload = server._request("GET", "/test")

        self.assertEqual((status, payload), (200, {"ok": True, "items": [1]}))
        urlopen.assert_called_once()
        self.mock_error_log.assert_not_called()

    def test_request_cloudflare_html_success_is_sanitized_and_not_success(self) -> None:
        raw_marker = "RAW_200_HTML_BODY_MARKER"
        response = FakeResponse(
            200,
            f"<!doctype html><body>{raw_marker}</body>".encode(),
            {
                "Content-Type": "text/html; charset=UTF-8",
                "Server": "cloudflare",
                "CF-Ray": "ray-200-test-123",
                "Retry-After": "120",
                "CF-Mitigated": "challenge",
                "CF-Error-Type": "managed_challenge",
                "CF-Error-Origin": "waf",
                "X-Internal-Diagnostic": "do-not-expose",
            },
        )
        with patch.object(
            server, "_auth_headers", return_value={"Authorization": "test"}
        ), patch.object(
            server.urllib.request, "urlopen", return_value=response
        ) as urlopen, patch.object(server.time, "sleep") as sleep:
            status, payload = server._request("GET", "/interstitial")

        self.assertEqual(status, 502)
        self.assertEqual(
            payload,
            {
                "detail": "Cloudflare returned HTML instead of the expected JSON response.",
                "response_type": "html",
                "content_type": "text/html; charset=UTF-8",
                "server": "cloudflare",
                "cf_ray": "ray-200-test-123",
                "upstream_status": 200,
                "attempts": 3,
                "retry_after": "120",
                "cf_mitigated": "challenge",
                "cf_error_type": "managed_challenge",
                "cf_error_origin": "waf",
            },
        )
        self.assertNotIn(raw_marker, repr(payload))
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(0.5), call(1.0)])
        self.assertEqual(self.mock_error_log.call_count, 3)
        events = [call.args[0] for call in self.mock_error_log.call_args_list]
        self.assertEqual(
            [event["event"] for event in events],
            ["retry_error", "retry_error", "terminal_error"],
        )
        self.assertEqual(
            [event["retry_delay_seconds"] for event in events[:2]], [0.5, 1.0]
        )
        self.assertEqual(events[-1]["status"], 200)
        self.assertEqual(events[-1]["attempt"], 3)
        self.assertEqual(events[-1]["max_attempts"], 3)
        self.assertNotIn(raw_marker, repr(events))

    def test_request_cloudflare_html_403_then_json_success_retries_once(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.invalid/private/task-id",
            403,
            "forbidden",
            {"Content-Type": "text/html", "Server": "CloudFlare"},
            io.BytesIO(b"<!doctype html><body>challenge</body>"),
        )
        response = FakeResponse(200, b'{"ok":true}')
        with patch.object(
            server, "_auth_headers", return_value={"Authorization": "test"}
        ), patch.object(
            server.urllib.request, "urlopen", side_effect=[error, response]
        ) as urlopen, patch.object(server.time, "sleep") as sleep:
            status, payload = server._request("GET", "/private/task-id")

        self.assertEqual((status, payload), (200, {"ok": True}))
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.5)
        self.assertEqual(self.mock_error_log.call_count, 1)
        event = self.mock_error_log.call_args.args[0]
        self.assertEqual(event["event"], "retry_error")
        self.assertEqual(event["status"], 403)
        self.assertEqual(event["attempt"], 1)
        self.assertEqual(event["max_attempts"], 3)
        self.assertEqual(event["retry_delay_seconds"], 0.5)
        self.assertEqual(event["endpoint"], "/{resource}")
        self.assertNotIn("task-id", repr(event))

    def test_request_cloudflare_html_503_then_json_success_retries_once(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.invalid/private/task-id",
            503,
            "service unavailable",
            {"Content-Type": "text/html", "Server": "cloudflare"},
            io.BytesIO(b"<!doctype html><body>temporary challenge</body>"),
        )
        response = FakeResponse(200, b'{"ok":true}')
        with patch.object(
            server, "_auth_headers", return_value={"Authorization": "test"}
        ), patch.object(
            server.urllib.request, "urlopen", side_effect=[error, response]
        ) as urlopen, patch.object(server.time, "sleep") as sleep:
            status, payload = server._request("GET", "/private/task-id")

        self.assertEqual((status, payload), (200, {"ok": True}))
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.5)
        self.assertEqual(self.mock_error_log.call_count, 1)
        self.assertEqual(
            self.mock_error_log.call_args.args[0]["event"], "retry_error"
        )

    def test_request_json_http_error_decodes_json(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.invalid/test",
            400,
            "bad request",
            {"Content-Type": "application/json", "Retry-After": "120"},
            io.BytesIO(b'{"detail":"invalid request","retry_after":"api-value"}'),
        )
        with patch.object(
            server, "_auth_headers", return_value={"Authorization": "test"}
        ), patch.object(server.urllib.request, "urlopen", side_effect=error) as urlopen:
            status, payload = server._request("GET", "/test")

        self.assertEqual(
            (status, payload),
            (400, {"detail": "invalid request", "retry_after": "api-value"}),
        )
        urlopen.assert_called_once()
        self.assertEqual(self.mock_error_log.call_count, 1)
        event = self.mock_error_log.call_args.args[0]
        self.assertEqual(event["event"], "terminal_error")
        self.assertEqual(event["status"], 400)
        self.assertEqual(event["max_attempts"], 1)
        self.assertNotIn("invalid request", repr(event))

    def test_request_cloudflare_html_error_is_sanitized(self) -> None:
        raw_marker = "RAW_HTML_BODY_MARKER"

        def fresh_error(*args: object, **kwargs: object) -> None:
            raise urllib.error.HTTPError(
                "https://example.invalid/private/task-id",
                404,
                "not found",
                {
                    "Content-Type": "text/html; charset=UTF-8",
                    "Server": "cloudflare",
                    "CF-Ray": "ray-test-123",
                },
                io.BytesIO(f"<!doctype html><body>{raw_marker}</body>".encode()),
            )

        with patch.object(
            server, "_auth_headers", return_value={"Authorization": "test"}
        ), patch.object(
            server.urllib.request, "urlopen", side_effect=fresh_error
        ) as urlopen, patch.object(server.time, "sleep") as sleep:
            status, payload = server._request("GET", "/private/task-id")

        self.assertEqual(status, 404)
        self.assertEqual(
            payload,
            {
                "detail": "Cloudflare returned HTML instead of the expected JSON response.",
                "response_type": "html",
                "content_type": "text/html; charset=UTF-8",
                "server": "cloudflare",
                "cf_ray": "ray-test-123",
                "attempts": 3,
            },
        )
        self.assertNotIn(raw_marker, repr(payload))
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(0.5), call(1.0)])
        self.assertEqual(self.mock_error_log.call_count, 3)
        events = [call.args[0] for call in self.mock_error_log.call_args_list]
        self.assertEqual(
            [event["event"] for event in events],
            ["retry_error", "retry_error", "terminal_error"],
        )
        self.assertEqual(events[-1]["status"], 404)
        self.assertEqual(events[-1]["attempt"], 3)
        self.assertEqual(events[-1]["max_attempts"], 3)
        self.assertEqual(events[-1]["cf_ray"], "ray-test-123")
        self.assertNotIn(raw_marker, repr(events))

    def test_request_cloudflare_html_patch_does_not_retry(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.invalid/private/task-id",
            403,
            "forbidden",
            {
                "Content-Type": "text/html; charset=UTF-8",
                "Server": "cloudflare",
                "CF-Ray": "ray-patch-test-123",
            },
            io.BytesIO(b"<!doctype html><body>patch challenge</body>"),
        )
        with patch.object(
            server, "_auth_headers", return_value={"Authorization": "test"}
        ), patch.object(
            server.urllib.request, "urlopen", side_effect=error
        ) as urlopen, patch.object(server.time, "sleep") as sleep:
            status, payload = server._request(
                "PATCH", "/private/task-id", body={"title": "updated"}
            )

        self.assertEqual(status, 403)
        self.assertEqual(payload["attempts"], 1)
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(self.mock_error_log.call_count, 1)
        event = self.mock_error_log.call_args.args[0]
        self.assertEqual(event["event"], "terminal_error")
        self.assertEqual(event["max_attempts"], 1)
        self.assertEqual(event["error_type"], "CloudflareHTML")
        self.assertEqual(event["cf_ray"], "ray-patch-test-123")

    def test_request_cloudflare_html_429_does_not_retry(self) -> None:
        raw_marker = "RAW_429_HTML_BODY_MARKER"
        error = urllib.error.HTTPError(
            "https://example.invalid/private/task-id",
            429,
            "too many requests",
            {
                "Content-Type": "text/html; charset=UTF-8",
                "Server": "cloudflare",
                "CF-Ray": "ray-429-test-123",
                "Retry-After": "60",
            },
            io.BytesIO(f"<!doctype html><body>{raw_marker}</body>".encode()),
        )
        with patch.object(
            server, "_auth_headers", return_value={"Authorization": "test"}
        ), patch.object(
            server.urllib.request, "urlopen", side_effect=error
        ) as urlopen, patch.object(server.time, "sleep") as sleep:
            status, payload = server._request("GET", "/private/task-id")

        self.assertEqual(
            payload,
            {
                "detail": "Cloudflare returned HTML instead of the expected JSON response.",
                "response_type": "html",
                "content_type": "text/html; charset=UTF-8",
                "server": "cloudflare",
                "cf_ray": "ray-429-test-123",
                "retry_after": "60",
                "attempts": 1,
            },
        )
        self.assertEqual(status, 429)
        self.assertNotIn(raw_marker, repr(payload))
        urlopen.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(self.mock_error_log.call_count, 1)
        event = self.mock_error_log.call_args.args[0]
        self.assertEqual(event["event"], "terminal_error")
        self.assertEqual(event["status"], 429)
        self.assertEqual(event["max_attempts"], 1)
        self.assertEqual(event["cf_ray"], "ray-429-test-123")
        self.assertNotIn(raw_marker, repr(event))

    def test_request_generic_html_does_not_retry(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.invalid/private/task-id",
            403,
            "forbidden",
            {"Content-Type": "text/html", "Server": "nginx"},
            io.BytesIO(b"<!doctype html><body>generic error</body>"),
        )
        with patch.object(
            server, "_auth_headers", return_value={"Authorization": "test"}
        ), patch.object(
            server.urllib.request, "urlopen", side_effect=error
        ) as urlopen, patch.object(server.time, "sleep") as sleep:
            status, payload = server._request("GET", "/private/task-id")

        self.assertEqual(status, 403)
        self.assertEqual(
            payload["detail"],
            "The server returned HTML instead of the expected JSON response.",
        )
        self.assertEqual(payload["attempts"], 1)
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(self.mock_error_log.call_count, 1)
        event = self.mock_error_log.call_args.args[0]
        self.assertEqual(event["event"], "terminal_error")
        self.assertEqual(event["error_type"], "HTMLResponse")
        self.assertEqual(event["max_attempts"], 1)
        self.assertNotIn("generic error", repr(event))

    def test_request_json_http_errors_are_not_retried(self) -> None:
        for error_status in (401, 403, 500):
            with self.subTest(status=error_status):
                error = urllib.error.HTTPError(
                    "https://example.invalid/test",
                    error_status,
                    "request failed",
                    {"Content-Type": "application/json"},
                    io.BytesIO(b'{"detail":"request failed"}'),
                )
                with patch.object(
                    server, "_auth_headers", return_value={"Authorization": "test"}
                ), patch.object(
                    server.urllib.request, "urlopen", side_effect=error
                ) as urlopen, patch.object(server.time, "sleep") as sleep:
                    status, payload = server._request("GET", "/test")

                self.assertEqual(
                    (status, payload), (error_status, {"detail": "request failed"})
                )
                urlopen.assert_called_once()
                sleep.assert_not_called()
                self.assertEqual(self.mock_error_log.call_count, 1)
                event = self.mock_error_log.call_args.args[0]
                self.assertEqual(event["event"], "terminal_error")
                self.assertEqual(event["status"], error_status)
                self.assertEqual(event["error_type"], "HTTPError")
                self.assertEqual(event["max_attempts"], 1)
                self.assertNotIn("request failed", repr(event))
                self.mock_error_log.reset_mock()

    def test_request_json_429_retry_after_is_not_retried(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.invalid/test",
            429,
            "too many requests",
            {"Content-Type": "application/json", "Retry-After": "120"},
            io.BytesIO(b'{"detail":"rate limited"}'),
        )
        with patch.object(
            server, "_auth_headers", return_value={"Authorization": "test"}
        ), patch.object(
            server.urllib.request, "urlopen", side_effect=error
        ) as urlopen, patch.object(server.time, "sleep") as sleep:
            status, payload = server._request("GET", "/test")

        self.assertEqual(
            (status, payload),
            (429, {"detail": "rate limited", "retry_after": "120"}),
        )
        urlopen.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(self.mock_error_log.call_count, 1)
        event = self.mock_error_log.call_args.args[0]
        self.assertEqual(event["event"], "terminal_error")
        self.assertEqual(event["status"], 429)
        self.assertEqual(event["error_type"], "HTTPError")
        self.assertEqual(event["max_attempts"], 1)
        self.assertNotIn("rate limited", repr(event))

    def test_request_transport_errors_are_structured_without_retries(self) -> None:
        for error in (urllib.error.URLError("private URL"), TimeoutError("private timeout")):
            with self.subTest(error=type(error).__name__):
                with patch.object(
                    server, "_auth_headers", return_value={"Authorization": "test"}
                ), patch.object(
                    server.urllib.request, "urlopen", side_effect=error
                ) as urlopen:
                    status, payload = server._request("GET", "/private/task-id")

                self.assertEqual(status, 0)
                self.assertEqual(
                    payload,
                    {
                        "detail": "Request failed before receiving an HTTP response.",
                        "error_type": type(error).__name__,
                    },
                )
                urlopen.assert_called_once()
                self.assertEqual(self.mock_error_log.call_count, 1)
                event = self.mock_error_log.call_args.args[0]
                self.assertEqual(event["event"], "terminal_error")
                self.assertEqual(event["error_type"], type(error).__name__)
                self.assertEqual(event["max_attempts"], 1)
                self.assertNotIn("private", repr(event))
                self.mock_error_log.reset_mock()


class ErrorLogUtilityTest(unittest.TestCase):
    def test_debug_gate_and_error_log_path_override_off_and_venv_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            explicit = Path(temp_dir) / "errors.jsonl"
            with patch.dict(
                os.environ,
                {
                    server.CODEX_CHATS_DEBUG_LOG: "1",
                    server.CODEX_CHATS_ERROR_LOG: str(explicit),
                },
            ):
                self.assertEqual(server._resolve_error_log_path(), explicit)

        with patch.dict(
            os.environ,
            {
                server.CODEX_CHATS_DEBUG_LOG: "true",
                server.CODEX_CHATS_ERROR_LOG: "off",
            },
        ):
            self.assertIsNone(server._resolve_error_log_path())

        with patch.dict(
            os.environ,
            {
                server.CODEX_CHATS_DEBUG_LOG: "on",
                server.CODEX_CHATS_ERROR_LOG: "",
            },
        ), patch.object(server.sys, "prefix", "/private/venv"), patch.object(
            server.sys, "base_prefix", "/usr/local"
        ):
            self.assertEqual(
                server._resolve_error_log_path(),
                Path("/private/venv") / server.ERROR_LOG_FILENAME,
            )

        with patch.dict(
            os.environ,
            {
                server.CODEX_CHATS_DEBUG_LOG: "1",
                server.CODEX_CHATS_ERROR_LOG: "",
            },
        ), patch.object(server.sys, "prefix", "/usr/local"), patch.object(
            server.sys, "base_prefix", "/usr/local"
        ):
            self.assertIsNone(server._resolve_error_log_path())

        for disabled in ("", "0", "false", "no", "off"):
            with self.subTest(debug=disabled), patch.dict(
                os.environ,
                {
                    server.CODEX_CHATS_DEBUG_LOG: disabled,
                    server.CODEX_CHATS_ERROR_LOG: "/tmp/should-not-be-used.log",
                },
            ):
                self.assertFalse(server._debug_logging_enabled())
                self.assertIsNone(server._resolve_error_log_path())

        with patch.dict(
            os.environ,
            {
                server.CODEX_CHATS_DEBUG_LOG: "TRUE ",
                server.CODEX_CHATS_ERROR_LOG: "/tmp/explicit-error.log",
            },
        ):
            self.assertTrue(server._debug_logging_enabled())

    def test_safe_context_normalizes_route_redacts_id_and_strips_query(self) -> None:
        full_id = "conversation-ID-SECRET"
        context = server._safe_error_context(
            "PATCH",
            f"/conversations/{full_id}?cursor=CURSOR_SECRET&offset=12",
            {
                "title": "TITLE_SECRET",
                "Authorization": "AUTH_SECRET",
            },
        )

        self.assertEqual(context["operation"], "rename_conversation")
        self.assertEqual(context["endpoint"], "/conversations/{id}")
        self.assertEqual(context["resource_id"], server._redact_resource_id(full_id))
        self.assertNotIn(full_id, repr(context))
        self.assertNotIn("CURSOR_SECRET", repr(context))
        self.assertNotIn("TITLE_SECRET", repr(context))
        self.assertNotIn("AUTH_SECRET", repr(context))

    def test_append_error_log_is_private_jsonl_and_has_no_sensitive_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "errors.jsonl"
            full_id = "conversation-ID-SECRET"
            fields = {
                "event": "terminal_error",
                **server._safe_error_context(
                    "GET",
                    f"/conversation/{full_id}?cursor=CURSOR_SECRET",
                    {"title": "TITLE_SECRET"},
                ),
                "method": "GET",
                "status": 403,
                "error_type": "HTTPError",
                "cf_ray": "ray-test",
                "body": "BODY_SECRET",
                "url": "https://example.invalid/conversation/URL_SECRET",
                "Authorization": "AUTH_SECRET",
            }
            with patch.dict(os.environ, {server.CODEX_CHATS_DEBUG_LOG: "yes"}):
                server._append_error_log(fields, log_path)

            raw = log_path.read_text()
            record = json.loads(raw)
            self.assertTrue(record["timestamp"].endswith("Z"))
            self.assertEqual(record["endpoint"], "/conversation/{id}")
            self.assertEqual(record["resource_id"], server._redact_resource_id(full_id))
            for marker in (
                full_id,
                "CURSOR_SECRET",
                "TITLE_SECRET",
                "BODY_SECRET",
                "URL_SECRET",
                "AUTH_SECRET",
            ):
                self.assertNotIn(marker, raw)
            self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)

    def test_append_error_log_trims_oldest_complete_lines_and_stays_under_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "errors.jsonl"
            with patch.object(server, "ERROR_LOG_MAX_BYTES", 700), patch.object(
                server, "ERROR_LOG_RETAIN_BYTES", 300
            ), patch.dict(os.environ, {server.CODEX_CHATS_DEBUG_LOG: "1"}):
                for index in range(8):
                    server._append_error_log(
                        {
                            "event": "terminal_error",
                            "operation": "list_conversations",
                            "method": "GET",
                            "endpoint": "/conversations",
                            "status": 500,
                            "error_type": "HTTPError",
                            "cf_ray": f"ray-{index}",
                        },
                        log_path,
                    )

            data = log_path.read_bytes()
            self.assertLessEqual(len(data), 700)
            self.assertTrue(data.endswith(b"\n"))
            records = [json.loads(line) for line in data.splitlines()]
            self.assertGreaterEqual(len(records), 1)
            self.assertNotIn("ray-0", {record.get("cf_ray") for record in records})
            self.assertIn("ray-7", {record.get("cf_ray") for record in records})

    def test_msvcrt_lock_unlock_fallback_preserves_file_position(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "errors.jsonl"
            with log_path.open("w+b") as handle:
                handle.write(b"lock target")
                handle.flush()
                fd = handle.fileno()
                os.lseek(fd, 3, os.SEEK_SET)
                fake_msvcrt = Mock(LK_LOCK=1, LK_UNLCK=2)
                with patch.object(server, "fcntl", None), patch.object(
                    server, "msvcrt", fake_msvcrt
                ):
                    lock_kind = server._lock_error_log(fd)
                    self.assertEqual(lock_kind, "msvcrt")
                    self.assertEqual(os.lseek(fd, 0, os.SEEK_CUR), 3)
                    server._unlock_error_log(fd, lock_kind)
                    self.assertEqual(os.lseek(fd, 0, os.SEEK_CUR), 3)

                self.assertEqual(
                    fake_msvcrt.locking.call_args_list,
                    [call(fd, 1, 1), call(fd, 2, 1)],
                )

    def test_append_error_log_does_not_write_without_a_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "errors.jsonl"
            original = b'{"existing":true}\n'
            log_path.write_bytes(original)
            fields = {
                "event": "terminal_error",
                "operation": "list_conversations",
                "method": "GET",
                "endpoint": "/conversations",
                "status": 500,
                "error_type": "HTTPError",
            }
            with patch.dict(os.environ, {server.CODEX_CHATS_DEBUG_LOG: "1"}), patch.object(
                server, "fcntl", None
            ), patch.object(server, "msvcrt", None):
                server._append_error_log(fields, log_path)

            self.assertEqual(log_path.read_bytes(), original)

    def test_append_error_log_uses_portable_open_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "errors.jsonl"
            fields = {
                "event": "terminal_error",
                "operation": "list_conversations",
                "method": "GET",
                "endpoint": "/conversations",
                "status": 500,
                "error_type": "HTTPError",
            }
            original_open = os.open
            with patch.dict(os.environ, {server.CODEX_CHATS_DEBUG_LOG: "1"}), patch.object(
                server.os,
                "open",
                side_effect=lambda path, flags, mode: original_open(path, flags, mode),
            ) as open_mock:
                server._append_error_log(fields, log_path)

            flags = open_mock.call_args.args[1]
            for flag_name in ("O_BINARY", "O_NOFOLLOW"):
                flag = getattr(os, flag_name, 0)
                self.assertEqual(flags & flag, flag)

    def test_append_error_log_rejects_non_regular_target_before_lock_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "errors.jsonl"
            target_fd = os.open(Path(temp_dir) / "target", os.O_RDWR | os.O_CREAT, 0o600)
            fields = {
                "event": "terminal_error",
                "operation": "list_conversations",
                "method": "GET",
                "endpoint": "/conversations",
                "status": 500,
                "error_type": "HTTPError",
            }
            with patch.dict(os.environ, {server.CODEX_CHATS_DEBUG_LOG: "1"}), patch.object(
                server.os, "open", return_value=target_fd
            ), patch.object(
                server.os, "fstat", return_value=Mock(st_mode=stat.S_IFDIR | 0o700)
            ), patch.object(server, "_lock_error_log") as lock, patch.object(
                server, "_write_all"
            ) as write:
                server._append_error_log(fields, log_path)

            lock.assert_not_called()
            write.assert_not_called()
            self.assertFalse(log_path.exists())

    @unittest.skipUnless(
        server.fcntl is not None or server.msvcrt is not None,
        "inter-process file locking unavailable",
    )
    def test_concurrent_writers_keep_complete_jsonl_under_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "errors.jsonl"
            child_script = textwrap.dedent(
                """
                import os
                from pathlib import Path
                import codex_chats_mcp as server

                server.ERROR_LOG_MAX_BYTES = 1200
                server.ERROR_LOG_RETAIN_BYTES = 500
                destination = Path(os.environ["CODEX_CHATS_ERROR_LOG"])
                for index in range(16):
                    server._append_error_log(
                        {
                            "event": "terminal_error",
                            "operation": "list_conversations",
                            "method": "GET",
                            "endpoint": "/conversations",
                            "status": 500,
                            "error_type": "HTTPError",
                            "cf_ray": f"child-{index}",
                        },
                        destination,
                    )
                """
            )
            child_env = os.environ.copy()
            child_env[server.CODEX_CHATS_DEBUG_LOG] = "1"
            child_env[server.CODEX_CHATS_ERROR_LOG] = str(log_path)
            children = [
                subprocess.Popen(
                    [sys.executable, "-c", child_script],
                    cwd=temp_dir,
                    env=child_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(3)
            ]
            results = [child.communicate(timeout=20) for child in children]
            for stdout, stderr in results:
                self.assertEqual((stdout, stderr), ("", ""))
            self.assertTrue(all(child.returncode == 0 for child in children))

            data = log_path.read_bytes()
            self.assertLessEqual(len(data), 1200)
            self.assertTrue(data.endswith(b"\n"))
            records = [json.loads(line) for line in data.splitlines()]
            self.assertGreaterEqual(len(records), 1)
            self.assertTrue(all(record["event"] == "terminal_error" for record in records))

    def test_append_error_log_uses_path_chmod_when_fchmod_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "errors.jsonl"
            fields = {
                "event": "terminal_error",
                "operation": "list_conversations",
                "method": "GET",
                "endpoint": "/conversations",
                "status": 500,
                "error_type": "HTTPError",
            }
            with patch.dict(os.environ, {server.CODEX_CHATS_DEBUG_LOG: "1"}), patch.object(
                server.os, "fchmod", None
            ), patch.object(server.os, "chmod", wraps=os.chmod) as chmod:
                server._append_error_log(fields, log_path)

            chmod.assert_called_with(log_path, stat.S_IRUSR | stat.S_IWUSR)
            self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()

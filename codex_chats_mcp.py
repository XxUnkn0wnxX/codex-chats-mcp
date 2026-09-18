#!/usr/bin/env python3
"""MCP server: list and delete Codex Cloud chats (tasks) and regular ChatGPT conversations.

Codex itself only exposes archive/unarchive in the UI. This wraps the
internal ChatGPT `/backend-api/wham/tasks/*` endpoints so archived tasks
can actually be deleted (`DELETE /wham/tasks/{id}`), and also wraps
`/backend-api/conversations/*` so the regular ChatGPT chat list can be
managed the same way.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from _codex_chats_build import DEBUG_BUILD

try:
    import fcntl
except ImportError:  # pragma: no cover - unavailable on some platforms
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - unavailable on POSIX platforms
    msvcrt = None  # type: ignore[assignment]

from mcp.server.mcpserver import MCPServer

AUTH_PATH = Path.home() / ".codex" / "auth.json"
BASE_URL = "https://chatgpt.com/backend-api"
USER_AGENT = "codex-chats-mcp/0.2.0"
USER_AGENT_ENV = "CODEX_CHATS_USER_AGENT"
MAX_CLOUDFLARE_ATTEMPTS = 3
CLOUDFLARE_RETRY_DELAYS = (0.5, 1.0)
RETRYABLE_CLOUDFLARE_STATUSES = frozenset({403, 404})
RESPONSE_DIAGNOSTIC_HEADERS = (
    ("Retry-After", "retry_after"),
    ("CF-Mitigated", "cf_mitigated"),
    ("CF-Error-Type", "cf_error_type"),
    ("CF-Error-Origin", "cf_error_origin"),
)
CODEX_CHATS_ERROR_LOG = "CODEX_CHATS_ERROR_LOG"
CODEX_CHATS_DEBUG_LOG = "CODEX_CHATS_DEBUG_LOG"
ERROR_LOG_FILENAME = "codex-chats-mcp-errors.log"
ERROR_LOG_MAX_BYTES = 8 * 1024 * 1024
ERROR_LOG_RETAIN_BYTES = 6 * 1024 * 1024
SERVER_INSTANCE_ID = secrets.token_hex(16)
_MCP_REQUEST_ID_HASH_KEY = secrets.token_bytes(32)
_MCP_REQUEST_ID_MAX_LENGTH = 64
_UNKNOWN_CORRELATION = "unknown"
_SAFE_CORRELATION_TOKEN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$", re.ASCII
)
_SAFE_NUMERIC_REQUEST_ID = re.compile(r"^-?[0-9]{1,64}$", re.ASCII)

_ERROR_LOG_EVENTS = frozenset({"retry_error", "terminal_error"})
_ERROR_LOG_FIELDS = frozenset(
    {
        "event",
        "operation",
        "method",
        "endpoint",
        "resource_id",
        "status",
        "error_type",
        "attempt",
        "max_attempts",
        "retry_delay_seconds",
        "cf_ray",
        "server_instance_id",
        "mcp_request_id",
        "tool_call",
    }
)
_ERROR_LOG_STRING_LIMITS = {
    "operation": 64,
    "method": 16,
    "endpoint": 128,
    "resource_id": 32,
    "error_type": 64,
    "cf_ray": 128,
    "server_instance_id": 64,
    "mcp_request_id": 64,
    "tool_call": 64,
}

_MCP_ERROR_CONTEXT: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "codex_chats_mcp_error_context",
    default=(_UNKNOWN_CORRELATION, _UNKNOWN_CORRELATION),
)


class _MCPRequestIdDigest(str):
    """Marker for a digest produced by this process, not peer input."""


def _safe_correlation_token(value: object) -> str | None:
    """Accept only short, ASCII correlation tokens from the MCP peer."""
    if type(value) is int:
        try:
            token = str(value)
        except (OverflowError, ValueError):
            return None
    elif type(value) is str:
        token = value
    else:
        return None
    if len(token) > 64 or _SAFE_CORRELATION_TOKEN.fullmatch(token) is None:
        return None
    return token


def _safe_mcp_request_id(value: object) -> str:
    if isinstance(value, _MCPRequestIdDigest):
        return value
    if type(value) is int:
        try:
            token = str(value)
        except (OverflowError, ValueError):
            return _UNKNOWN_CORRELATION
        if _SAFE_NUMERIC_REQUEST_ID.fullmatch(token) is None:
            return _UNKNOWN_CORRELATION
    elif type(value) is str:
        token = value
    else:
        return _UNKNOWN_CORRELATION
    if not token or len(token) > _MCP_REQUEST_ID_MAX_LENGTH:
        return _UNKNOWN_CORRELATION
    try:
        encoded = token.encode("utf-8")
    except UnicodeError:
        return _UNKNOWN_CORRELATION
    digest = hmac.new(_MCP_REQUEST_ID_HASH_KEY, encoded, hashlib.sha256).hexdigest()
    return _MCPRequestIdDigest(f"hash:{digest}")


def _safe_tool_call(value: object) -> str:
    if type(value) is not str:
        return _UNKNOWN_CORRELATION
    return _safe_correlation_token(value) or _UNKNOWN_CORRELATION


async def _capture_mcp_error_context(ctx: Any, call_next: Any) -> Any:
    """Capture safe tools/call metadata for synchronous downstream requests."""
    request_id = _UNKNOWN_CORRELATION
    tool_call = _UNKNOWN_CORRELATION
    if getattr(ctx, "method", None) == "tools/call":
        params = getattr(ctx, "params", None)
        name = params.get("name") if isinstance(params, Mapping) else None
        request_id = _safe_mcp_request_id(getattr(ctx, "request_id", None))
        tool_call = _safe_tool_call(name)

    token = _MCP_ERROR_CONTEXT.set((request_id, tool_call))
    try:
        return await call_next(ctx)
    finally:
        _MCP_ERROR_CONTEXT.reset(token)


def _error_correlation_fields() -> dict[str, str]:
    request_id, tool_call = _MCP_ERROR_CONTEXT.get()
    return {
        "server_instance_id": SERVER_INSTANCE_ID,
        "mcp_request_id": request_id,
        "tool_call": tool_call,
    }


mcp = MCPServer("codex-chats", middleware=[_capture_mcp_error_context])


def _resolve_error_log_path() -> Path | None:
    """Return the private error-log path, or None when logging is disabled."""
    if not _debug_logging_enabled():
        return None
    override = os.environ.get(CODEX_CHATS_ERROR_LOG, "").strip()
    if override:
        if override.lower() == "off":
            return None
        return Path(override).expanduser()
    if sys.prefix != sys.base_prefix:
        return Path(sys.prefix) / ERROR_LOG_FILENAME
    return None


def _debug_logging_enabled() -> bool:
    return DEBUG_BUILD and os.environ.get(CODEX_CHATS_DEBUG_LOG, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _redact_resource_id(resource_id: object) -> str:
    """Keep only small first/last fragments of a resource identifier."""
    value = str(resource_id)
    if len(value) <= 4:
        return "*" * len(value)
    if len(value) <= 8:
        return f"{value[:2]}...{value[-2:]}"
    return f"{value[:4]}...{value[-4:]}"


def _safe_error_context(
    method: str, path: str, body: dict | None = None
) -> dict[str, str | None]:
    """Infer safe operation metadata without retaining query or body values."""
    try:
        route = urllib.parse.urlsplit(path).path
        segments = [urllib.parse.unquote(part) for part in route.split("/") if part]
    except (TypeError, ValueError):
        segments = []

    verb = method.upper()
    operation = "request"
    endpoint = "/{resource}"
    resource_id: str | None = None

    if segments[:3] == ["wham", "tasks", "list"] and len(segments) == 3:
        endpoint = "/wham/tasks/list"
        operation = "list_chats"
    elif len(segments) >= 3 and segments[:2] == ["wham", "tasks"]:
        resource_id = _redact_resource_id(segments[2])
        endpoint = "/wham/tasks/{id}"
        action = segments[3] if len(segments) == 4 else ""
        if action in {"archive", "unarchive"}:
            endpoint += f"/{action}"
            operation = f"{action}_chat"
        elif verb == "GET":
            operation = "get_chat"
        elif verb == "DELETE":
            operation = "delete_chat"
    elif segments and segments[0] == "conversations":
        if len(segments) == 1:
            endpoint = "/conversations"
            if verb == "GET":
                operation = "list_conversations"
            elif verb == "PATCH" and isinstance(body, dict):
                keys = set(body)
                if "is_visible" in keys:
                    operation = "delete_all_conversations"
        else:
            endpoint = "/conversations/{id}"
            resource_id = _redact_resource_id(segments[1])
            if verb == "GET":
                operation = "get_conversation"
            elif verb == "PATCH" and isinstance(body, dict):
                keys = set(body)
                if "title" in keys:
                    operation = "rename_conversation"
                elif "is_visible" in keys:
                    operation = "delete_conversation"
                elif "is_archived" in keys:
                    operation = (
                        "archive_conversation"
                        if body.get("is_archived")
                        else "unarchive_conversation"
                    )
    elif len(segments) >= 2 and segments[0] == "conversation":
        endpoint = "/conversation/{id}"
        resource_id = _redact_resource_id(segments[1])
        if verb == "GET":
            operation = "get_conversation"
    elif segments and segments[0] in {"wham", "conversation"}:
        endpoint = f"/{segments[0]}"

    return {
        "operation": operation,
        "endpoint": endpoint,
        "resource_id": resource_id,
    }


def _record_error_event(
    event: str,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    status: int | None = None,
    error_type: str,
    attempt: int,
    max_attempts: int,
    retry_delay_seconds: float | None = None,
    cf_ray: str | None = None,
) -> None:
    """Build a safe failure event and send it to the private error log."""
    fields: dict[str, Any] = {
        "event": event,
        **_safe_error_context(method, path, body),
        "method": method.upper(),
        "error_type": error_type,
        "attempt": attempt,
        "max_attempts": max_attempts,
        **_error_correlation_fields(),
    }
    if status is not None:
        fields["status"] = status
    if retry_delay_seconds is not None:
        fields["retry_delay_seconds"] = retry_delay_seconds
    if cf_ray is not None:
        fields["cf_ray"] = cf_ray or "unknown"
    _append_error_log(fields, warn_on_failure=True)


def _error_log_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _generic_error_log_warning() -> None:
    try:
        print(
            "codex-chats-mcp: unable to write the private error log; "
            "failure details were not recorded",
            file=sys.stderr,
        )
    except Exception:
        pass


def _safe_error_fields(fields: dict[str, Any]) -> dict[str, Any] | None:
    """Copy only primitive, bounded, non-sensitive event fields."""
    if not isinstance(fields, dict):
        return None
    event = fields.get("event")
    if event not in _ERROR_LOG_EVENTS:
        return None

    safe: dict[str, Any] = {"event": event}
    for key in _ERROR_LOG_FIELDS - {"event"}:
        value = fields.get(key)
        if value is None:
            continue
        if key == "server_instance_id":
            safe[key] = SERVER_INSTANCE_ID
            continue
        if key == "mcp_request_id":
            safe[key] = _safe_mcp_request_id(value)
            continue
        if key == "tool_call":
            safe[key] = _safe_correlation_token(value) or _UNKNOWN_CORRELATION
            continue
        if key in _ERROR_LOG_STRING_LIMITS:
            if not isinstance(value, str):
                continue
            if key == "endpoint":
                try:
                    value = urllib.parse.urlsplit(value).path or "/{resource}"
                except (TypeError, ValueError):
                    value = "/{resource}"
                value = value.split("?", 1)[0]
            elif key == "resource_id":
                value = _redact_resource_id(value)
            safe[key] = value[: _ERROR_LOG_STRING_LIMITS[key]]
        elif key in {"status", "attempt", "max_attempts"}:
            if isinstance(value, int) and not isinstance(value, bool):
                safe[key] = value
        elif key == "retry_delay_seconds":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                safe[key] = value
    safe.setdefault("server_instance_id", SERVER_INSTANCE_ID)
    safe.setdefault("mcp_request_id", _UNKNOWN_CORRELATION)
    safe.setdefault("tool_call", _UNKNOWN_CORRELATION)
    return safe


def _complete_jsonl_suffix(data: bytes, target_bytes: int) -> bytes:
    """Return a suffix of complete lines no larger than target_bytes."""
    if target_bytes <= 0:
        return b""
    lines = [line for line in data.splitlines(keepends=True) if line.endswith(b"\n")]
    retained: list[bytes] = []
    retained_bytes = 0
    for line in reversed(lines):
        if retained_bytes + len(line) > target_bytes:
            break
        retained.append(line)
        retained_bytes += len(line)
    retained.reverse()
    return b"".join(retained)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short error-log write")
        view = view[written:]


def _lock_error_log(fd: int) -> str | None:
    """Acquire an advisory lock with a portable file-position-preserving API."""
    if fcntl is not None:
        position: int | None = None
        acquired = False
        try:
            position = os.lseek(fd, 0, os.SEEK_CUR)
            fcntl.flock(fd, fcntl.LOCK_EX)
            acquired = True
            os.lseek(fd, position, os.SEEK_SET)
            return "fcntl"
        except OSError:
            if acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                if position is not None:
                    os.lseek(fd, position, os.SEEK_SET)
            except OSError:
                pass
    if msvcrt is not None:
        position = None
        acquired = False
        try:
            position = os.lseek(fd, 0, os.SEEK_CUR)
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            acquired = True
            os.lseek(fd, position, os.SEEK_SET)
            return "msvcrt"
        except OSError:
            if acquired:
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            try:
                if position is not None:
                    os.lseek(fd, position, os.SEEK_SET)
            except OSError:
                pass
    return None


def _unlock_error_log(fd: int, lock_kind: str | None) -> None:
    if lock_kind == "fcntl" and fcntl is not None:
        position: int | None = None
        try:
            position = os.lseek(fd, 0, os.SEEK_CUR)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            if position is not None:
                try:
                    os.lseek(fd, position, os.SEEK_SET)
                except OSError:
                    pass
    elif lock_kind == "msvcrt" and msvcrt is not None:
        position = None
        try:
            position = os.lseek(fd, 0, os.SEEK_CUR)
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            if position is not None:
                try:
                    os.lseek(fd, position, os.SEEK_SET)
                except OSError:
                    pass


def _set_error_log_permissions(fd: int, path: Path) -> None:
    fchmod = getattr(os, "fchmod", None)
    if callable(fchmod):
        try:
            fchmod(fd, 0o600)
            return
        except OSError:
            pass
    chmod = getattr(os, "chmod", None)
    if callable(chmod):
        try:
            chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass


def _append_error_log(
    fields: dict[str, Any],
    path: str | Path | None = None,
    *,
    warn_on_failure: bool = False,
) -> None:
    """Append one safe failure event as JSONL without ever raising."""
    try:
        if not _debug_logging_enabled():
            return
        safe = _safe_error_fields(fields)
        if safe is None:
            return
        log_path = Path(path).expanduser() if path is not None else _resolve_error_log_path()
        if log_path is None:
            return
        payload = {"timestamp": _error_log_timestamp(), **safe}
        line = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode(
            "utf-8"
        )
        max_bytes = ERROR_LOG_MAX_BYTES
        retain_bytes = ERROR_LOG_RETAIN_BYTES
        if len(line) > max_bytes:
            if warn_on_failure:
                _generic_error_log_warning()
            return

        log_path.parent.mkdir(parents=True, exist_ok=True)
        flags = (
            os.O_RDWR
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        fd = os.open(log_path, flags, 0o600)
        lock_kind: str | None = None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("private error-log target is not a regular file")
            _set_error_log_permissions(fd, log_path)
            lock_kind = _lock_error_log(fd)
            if lock_kind is None:
                raise OSError("private error-log lock unavailable")
            current_size = os.fstat(fd).st_size
            if current_size + len(line) > max_bytes:
                os.lseek(fd, 0, os.SEEK_SET)
                existing = os.read(fd, current_size)
                retained = _complete_jsonl_suffix(existing, retain_bytes)
                while len(retained) + len(line) > max_bytes and retained:
                    retained = _complete_jsonl_suffix(retained, len(retained) - 1)
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                _write_all(fd, retained + line)
            else:
                _write_all(fd, line)
        finally:
            _unlock_error_log(fd, lock_kind)
            os.close(fd)
    except Exception:
        if warn_on_failure:
            _generic_error_log_warning()


def _auth_headers() -> dict[str, str]:
    data = json.loads(AUTH_PATH.read_text())
    tokens = data["tokens"]
    user_agent = os.environ.get(USER_AGENT_ENV, "").strip() or USER_AGENT
    return {
        "Authorization": f"Bearer {tokens['access_token']}",
        "ChatGPT-Account-ID": tokens["account_id"],
        "User-Agent": user_agent,
        "Accept": "application/json",
    }


def _header_value(headers: object, name: str) -> str:
    if headers is None:
        return ""
    get = getattr(headers, "get", None)
    value = get(name) if callable(get) else None
    if value is None:
        items = getattr(headers, "items", lambda: ())()
        value = next((item for key, item in items if str(key).lower() == name.lower()), "")
    return str(value).strip()


def _is_html_response(content_type: str, raw: str) -> bool:
    leading = raw.lstrip().lower()
    return "html" in content_type.lower() or leading.startswith(
        ("<!doctype html", "<html", "<head", "<body")
    )


def _is_cloudflare_response(server: str, cf_ray: str) -> bool:
    return server.lower() == "cloudflare" or bool(cf_ray)


def _response_diagnostics(headers: object) -> dict[str, str]:
    diagnostics: dict[str, str] = {}
    for header_name, payload_key in RESPONSE_DIAGNOSTIC_HEADERS:
        value = _header_value(headers, header_name)
        if value:
            diagnostics[payload_key] = value
    return diagnostics


def _html_error_payload(
    content_type: str, server: str, cf_ray: str, headers: object
) -> dict[str, str | int]:
    cloudflare = _is_cloudflare_response(server, cf_ray)
    payload: dict[str, str | int] = {
        "detail": (
            "Cloudflare returned HTML instead of the expected JSON response."
            if cloudflare
            else "The server returned HTML instead of the expected JSON response."
        ),
        "response_type": "html",
        "content_type": content_type or "unknown",
        "server": server or "unknown",
        "cf_ray": cf_ray or "unknown",
    }
    payload.update(_response_diagnostics(headers))
    return payload


def _transport_error(error: Exception) -> dict[str, str]:
    return {
        "detail": "Request failed before receiving an HTTP response.",
        "error_type": type(error).__name__,
    }


def _should_retry_cloudflare_html(
    method: str,
    status: int,
    content_type: str,
    raw: str,
    server: str,
    cf_ray: str,
) -> bool:
    retryable_status = status in RETRYABLE_CLOUDFLARE_STATUSES or 500 <= status <= 599
    return (
        method.upper() == "GET"
        and retryable_status
        and _is_html_response(content_type, raw)
        and _is_cloudflare_response(server, cf_ray)
    )


def _request(method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = _auth_headers()
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    for attempt in range(1, MAX_CLOUDFLARE_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode(errors="replace")
                content_type = _header_value(resp.headers, "Content-Type")
                server = _header_value(resp.headers, "Server")
                cf_ray = _header_value(resp.headers, "CF-Ray")
                if _is_html_response(content_type, raw):
                    html_payload = _html_error_payload(
                        content_type, server, cf_ray, resp.headers
                    )
                    if resp.status == 200:
                        html_payload["upstream_status"] = resp.status
                    # Normalize HTML interstitials to 502 so nominal 200 responses can retry.
                    effective_status = 502
                    retryable = _should_retry_cloudflare_html(
                        method, effective_status, content_type, raw, server, cf_ray
                    )
                    if retryable and attempt < MAX_CLOUDFLARE_ATTEMPTS:
                        delay = CLOUDFLARE_RETRY_DELAYS[attempt - 1]
                        _record_error_event(
                            "retry_error",
                            method,
                            path,
                            body,
                            status=resp.status,
                            error_type="CloudflareHTML",
                            attempt=attempt,
                            max_attempts=MAX_CLOUDFLARE_ATTEMPTS,
                            retry_delay_seconds=delay,
                            cf_ray=cf_ray or "unknown",
                        )
                        time.sleep(delay)
                        continue
                    html_payload["attempts"] = attempt
                    _record_error_event(
                        "terminal_error",
                        method,
                        path,
                        body,
                        status=resp.status,
                        error_type="CloudflareHTML"
                        if _is_cloudflare_response(server, cf_ray)
                        else "HTMLResponse",
                        attempt=attempt,
                        max_attempts=MAX_CLOUDFLARE_ATTEMPTS if retryable else 1,
                        cf_ray=(cf_ray or "unknown")
                        if _is_cloudflare_response(server, cf_ray)
                        else None,
                    )
                    return effective_status, html_payload
                try:
                    return resp.status, json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    return resp.status, raw
        except urllib.error.HTTPError as e:
            status = e.code
            response_headers = e.headers
            error_detail = str(e)
            try:
                raw = e.read().decode(errors="replace")
            finally:
                e.close()
            content_type = _header_value(response_headers, "Content-Type")
            server = _header_value(response_headers, "Server")
            cf_ray = _header_value(response_headers, "CF-Ray")
            if _is_html_response(content_type, raw):
                html_payload = _html_error_payload(
                    content_type, server, cf_ray, response_headers
                )
                retryable = _should_retry_cloudflare_html(
                    method, status, content_type, raw, server, cf_ray
                )
                if retryable and attempt < MAX_CLOUDFLARE_ATTEMPTS:
                    delay = CLOUDFLARE_RETRY_DELAYS[attempt - 1]
                    _record_error_event(
                        "retry_error",
                        method,
                        path,
                        body,
                        status=status,
                        error_type="CloudflareHTML",
                        attempt=attempt,
                        max_attempts=MAX_CLOUDFLARE_ATTEMPTS,
                        retry_delay_seconds=delay,
                        cf_ray=cf_ray or "unknown",
                    )
                    time.sleep(delay)
                    continue
                html_payload["attempts"] = attempt
                _record_error_event(
                    "terminal_error",
                    method,
                    path,
                    body,
                    status=status,
                    error_type="CloudflareHTML"
                    if _is_cloudflare_response(server, cf_ray)
                    else "HTMLResponse",
                    attempt=attempt,
                    max_attempts=MAX_CLOUDFLARE_ATTEMPTS if retryable else 1,
                    cf_ray=(cf_ray or "unknown")
                    if _is_cloudflare_response(server, cf_ray)
                    else None,
                )
                return status, html_payload
            _record_error_event(
                "terminal_error",
                method,
                path,
                body,
                status=status,
                error_type="HTTPError",
                attempt=attempt,
                max_attempts=1,
                cf_ray=cf_ray or None,
            )
            try:
                payload = json.loads(raw) if raw else {"detail": error_detail}
            except json.JSONDecodeError:
                return status, raw
            if isinstance(payload, dict):
                for key, value in _response_diagnostics(response_headers).items():
                    payload.setdefault(key, value)
            return status, payload
        except TimeoutError as e:
            _record_error_event(
                "terminal_error",
                method,
                path,
                body,
                error_type=type(e).__name__,
                attempt=attempt,
                max_attempts=1,
            )
            return 0, _transport_error(e)
        except urllib.error.URLError as e:
            _record_error_event(
                "terminal_error",
                method,
                path,
                body,
                error_type=type(e).__name__,
                attempt=attempt,
                max_attempts=1,
            )
            return 0, _transport_error(e)


PAGE_LIMIT = 20  # server-side cap on /wham/tasks/list


def _summarize(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "archived": item.get("archived"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "environment_label": (item.get("task_status_display") or {}).get("environment_label"),
    }


def _list_page(task_filter: str, cursor: str | None) -> tuple[int, dict | str]:
    qs = f"limit={PAGE_LIMIT}&task_filter={task_filter}"
    if cursor:
        qs += f"&cursor={urllib.parse.quote(cursor)}"
    return _request("GET", f"/wham/tasks/list?{qs}")


@mcp.tool()
def list_chats(
    task_filter: Literal["all", "current", "archived"] = "all",
    max_results: int = 100,
) -> dict:
    """List Codex Cloud chats.

    Args:
        task_filter: 'archived' to see archived chats, 'current' for active,
            'all' for everything.
        max_results: stop after collecting this many items (paginates internally;
            server caps each page at 20).
    """
    items: list[dict] = []
    cursor: str | None = None
    while len(items) < max_results:
        status, payload = _list_page(task_filter, cursor)
        if status != 200 or not isinstance(payload, dict):
            return {"ok": False, "status": status, "error": payload, "items": items}
        page = payload.get("items") or []
        items.extend(_summarize(i) for i in page)
        cursor = payload.get("cursor")
        if not cursor or not page:
            break
    items = items[:max_results]
    return {"ok": True, "count": len(items), "items": items}


@mcp.tool()
def get_chat(chat_id: str) -> dict:
    """Fetch a single Codex Cloud chat by ID (e.g. task_e_...)."""
    status, payload = _request("GET", f"/wham/tasks/{chat_id}")
    if status != 200:
        return {"ok": False, "status": status, "error": payload}
    task = payload.get("task", {}) if isinstance(payload, dict) else {}
    return {
        "ok": True,
        "id": task.get("id"),
        "title": task.get("title"),
        "archived": task.get("archived"),
        "task_status_display": task.get("task_status_display"),
    }


@mcp.tool()
def get_chat_raw(chat_id: str) -> dict:
    """Fetch the full raw Codex Cloud task payload (includes turns/messages if any)."""
    status, payload = _request("GET", f"/wham/tasks/{chat_id}")
    return {"ok": status == 200, "status": status, "task": payload}


@mcp.tool()
def delete_chat(chat_id: str) -> dict:
    """Permanently delete a Codex Cloud chat by ID.

    Works on archived OR active chats. There is no undo.
    """
    status, payload = _request("DELETE", f"/wham/tasks/{chat_id}")
    return {"ok": status == 200, "status": status, "response": payload}


@mcp.tool()
def archive_chat(chat_id: str) -> dict:
    """Archive a Codex Cloud chat (move it out of the active list)."""
    status, payload = _request("POST", f"/wham/tasks/{chat_id}/archive", body={})
    return {"ok": status == 200, "status": status, "response": payload}


@mcp.tool()
def unarchive_chat(chat_id: str) -> dict:
    """Unarchive a Codex Cloud chat (return it to the active list)."""
    status, payload = _request("POST", f"/wham/tasks/{chat_id}/unarchive", body={})
    return {"ok": status == 200, "status": status, "response": payload}


@mcp.tool()
def delete_all_archived(confirm: bool = False) -> dict:
    """Delete every archived Codex Cloud chat. Requires confirm=True.

    Repeatedly fetches the first page of archived tasks and DELETEs each.
    The next page is fetched only after the current one is gone — so we don't
    need cursor handling and we won't get stuck if a delete fails (a failed
    item stays on the page and we'd loop on it, so we bail in that case).
    """
    if not confirm:
        return {
            "ok": False,
            "error": "Pass confirm=true to actually delete. Call list_chats(task_filter='archived') first to preview.",
        }

    deleted: list[dict] = []
    failed: list[dict] = []
    while True:
        status, payload = _list_page("archived", cursor=None)
        if status != 200 or not isinstance(payload, dict):
            return {"ok": False, "status": status, "error": payload,
                    "deleted": deleted, "failed": failed}
        items = payload.get("items") or []
        if not items:
            break
        page_success = 0
        for item in items:
            cid = item.get("id")
            if not cid:
                continue
            ds, _ = _request("DELETE", f"/wham/tasks/{cid}")
            entry = {"id": cid, "title": item.get("title"), "status": ds}
            if ds == 200:
                deleted.append(entry); page_success += 1
            else:
                failed.append(entry)
        if page_success == 0:
            break  # nothing progressed; avoid infinite loop
    return {"ok": not failed, "deleted_count": len(deleted),
            "failed_count": len(failed), "deleted": deleted, "failed": failed}


CONVERSATIONS_PAGE_LIMIT = 28  # ChatGPT web default for /conversations


def _summarize_conversation(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "create_time": item.get("create_time"),
        "update_time": item.get("update_time"),
        "is_archived": item.get("is_archived"),
    }


@mcp.tool()
def list_conversations(max_results: int = 100, include_archived: bool = False) -> dict:
    """List regular ChatGPT conversations (the 'Recents' list, not Codex tasks).

    Args:
        max_results: stop after collecting this many items (paginates internally).
        include_archived: if False (default), filter out archived conversations.
    """
    items: list[dict] = []
    offset = 0
    while len(items) < max_results:
        qs = f"offset={offset}&limit={CONVERSATIONS_PAGE_LIMIT}&order=updated"
        status, payload = _request("GET", f"/conversations?{qs}")
        if status != 200 or not isinstance(payload, dict):
            return {"ok": False, "status": status, "error": payload, "items": items}
        page = payload.get("items") or []
        if not page:
            break
        for c in page:
            if not include_archived and c.get("is_archived"):
                continue
            items.append(_summarize_conversation(c))
        offset += len(page)
        total = payload.get("total")
        if isinstance(total, int) and offset >= total:
            break
    items = items[:max_results]
    return {"ok": True, "count": len(items), "items": items}


@mcp.tool()
def delete_conversation(conversation_id: str) -> dict:
    """Permanently delete a regular ChatGPT conversation by ID.

    Uses PATCH /conversations/{id} with is_visible=false, which is what the
    web UI's 'Delete chat' action does. There is no undo.
    """
    status, payload = _request(
        "PATCH", f"/conversations/{conversation_id}", body={"is_visible": False}
    )
    return {"ok": status == 200, "status": status, "response": payload}


@mcp.tool()
def delete_all_conversations(confirm: bool = False) -> dict:
    """Permanently delete EVERY regular ChatGPT conversation. Requires confirm=True.

    Mirrors the 'Delete all chats' button in ChatGPT settings: a single
    PATCH /conversations with is_visible=false flips every chat at once.
    There is no undo. Call list_conversations() first to preview.
    """
    if not confirm:
        return {
            "ok": False,
            "error": "Pass confirm=true to actually delete every conversation. Call list_conversations() first to preview.",
        }
    status, payload = _request("PATCH", "/conversations", body={"is_visible": False})
    return {"ok": status == 200, "status": status, "response": payload}


@mcp.tool()
def get_conversation(conversation_id: str) -> dict:
    """Fetch the full payload for a regular ChatGPT conversation, including its message tree."""
    status, payload = _request("GET", f"/conversation/{conversation_id}")
    return {"ok": status == 200, "status": status, "conversation": payload}


@mcp.tool()
def archive_conversation(conversation_id: str) -> dict:
    """Archive a regular ChatGPT conversation (hide from Recents, keep around)."""
    status, payload = _request(
        "PATCH", f"/conversations/{conversation_id}", body={"is_archived": True}
    )
    return {"ok": status == 200, "status": status, "response": payload}


@mcp.tool()
def unarchive_conversation(conversation_id: str) -> dict:
    """Unarchive a regular ChatGPT conversation (return to Recents)."""
    status, payload = _request(
        "PATCH", f"/conversations/{conversation_id}", body={"is_archived": False}
    )
    return {"ok": status == 200, "status": status, "response": payload}


@mcp.tool()
def rename_conversation(conversation_id: str, title: str) -> dict:
    """Rename a regular ChatGPT conversation."""
    status, payload = _request(
        "PATCH", f"/conversations/{conversation_id}", body={"title": title}
    )
    return {"ok": status == 200, "status": status, "response": payload}


@mcp.tool()
def search_conversations(
    query: str,
    max_results: int = 100,
    case_sensitive: bool = False,
    include_archived: bool = False,
) -> dict:
    """Find ChatGPT conversations whose title contains `query` (substring match).

    Client-side filter over list_conversations — no dedicated search endpoint
    is used, so this is exact-substring only (no semantic search).
    """
    listing = list_conversations(max_results=max_results, include_archived=include_archived)
    if not listing.get("ok"):
        return listing
    needle = query if case_sensitive else query.lower()
    matches = []
    for item in listing["items"]:
        title = item.get("title") or ""
        hay = title if case_sensitive else title.lower()
        if needle in hay:
            matches.append(item)
    return {"ok": True, "count": len(matches), "query": query, "items": matches}


@mcp.tool()
def delete_conversations_matching(
    query: str,
    confirm: bool = False,
    case_sensitive: bool = False,
    max_scan: int = 500,
) -> dict:
    """Delete every conversation whose title contains `query`. Requires confirm=True.

    Always call search_conversations(query) first to preview the hit list.
    `max_scan` caps how many conversations we scan before deleting.
    """
    matches = search_conversations(
        query=query, max_results=max_scan, case_sensitive=case_sensitive
    )
    if not matches.get("ok"):
        return matches
    items = matches["items"]
    if not confirm:
        return {
            "ok": False,
            "would_delete_count": len(items),
            "items": items,
            "error": "Pass confirm=true to actually delete. Above is the preview.",
        }
    deleted: list[dict] = []
    failed: list[dict] = []
    for item in items:
        cid = item.get("id")
        if not cid:
            continue
        ds, _ = _request("PATCH", f"/conversations/{cid}", body={"is_visible": False})
        entry = {"id": cid, "title": item.get("title"), "status": ds}
        (deleted if ds == 200 else failed).append(entry)
    return {
        "ok": not failed,
        "deleted_count": len(deleted),
        "failed_count": len(failed),
        "deleted": deleted,
        "failed": failed,
    }


@mcp.tool()
def export_conversations(
    output_path: str,
    max_results: int = 1000,
    include_archived: bool = True,
    include_messages: bool = False,
) -> dict:
    """Dump conversations to a JSON file at `output_path`.

    `include_messages=True` fetches the full message tree for each conversation
    (one API call per chat — slow for large accounts but produces a complete backup).
    `include_messages=False` (default) writes only titles/timestamps/IDs.
    """
    listing = list_conversations(max_results=max_results, include_archived=include_archived)
    if not listing.get("ok"):
        return listing
    items = listing["items"]
    if include_messages:
        for item in items:
            cid = item.get("id")
            if not cid:
                continue
            full = get_conversation(cid)
            if full.get("ok"):
                item["full"] = full["conversation"]
    out = Path(output_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"count": len(items), "items": items}, indent=2))
    return {"ok": True, "count": len(items), "path": str(out), "with_messages": include_messages}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

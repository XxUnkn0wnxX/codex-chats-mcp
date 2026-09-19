"""Fail-closed release eligibility checks for the GitHub publish workflow.

The command deliberately treats the GitHub event and checked-out commit as
untrusted input until their fixed repository/ref/SHA contract has been
verified.  Project metadata is read from immutable Git objects and parsed as
TOML; it is never imported or otherwise executed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

import tomllib
from packaging.version import InvalidVersion, Version


REPOSITORY = "XxUnkn0wnxX/codex-chats-mcp"
PROJECT = "codex-chats-mcp-v2"
REF = "refs/heads/main"
PYPI_URL = f"https://pypi.org/pypi/{PROJECT}/json"
USER_AGENT = "codex-chats-mcp-release-gate/1"
PYPROJECT_PATH = "pyproject.toml"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ZERO_SHA = "0" * 40


class GateError(ValueError):
    """A safe, concise reason that the release gate cannot proceed."""


@dataclass(frozen=True)
class GateResult:
    should_publish: bool
    version: str
    reason: str


@dataclass(frozen=True)
class TrustedContext:
    event_name: str
    event: Mapping[str, Any]
    sha: str
    output_path: Path


def _fail(message: str) -> None:
    raise GateError(message)


def _strict_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        _fail(f"Invalid {label} commit SHA")
    if value == ZERO_SHA:
        _fail(f"Invalid {label} commit SHA")
    return value


def _required_env(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not isinstance(value, str) or not value:
        _fail(f"Missing GitHub environment value: {name}")
    return value


def load_event(path: str | os.PathLike[str]) -> Mapping[str, Any]:
    """Read the event JSON without ever echoing its contents in an error."""

    try:
        with Path(path).open("rb") as event_file:
            event = json.load(event_file)
    except (OSError, ValueError, UnicodeError):
        _fail("Unable to read GitHub event payload")
    if not isinstance(event, dict):
        _fail("GitHub event payload must be an object")
    return event


def trusted_context(
    environ: Mapping[str, str],
    event: Mapping[str, Any],
) -> TrustedContext:
    """Validate fixed GitHub identity/ref/SHA values before Git access."""

    event_name = _required_env(environ, "GITHUB_EVENT_NAME")
    if event_name not in {"push", "workflow_dispatch"}:
        _fail("Unsupported GitHub event")
    if _required_env(environ, "GITHUB_REPOSITORY") != REPOSITORY:
        _fail("Untrusted GitHub repository")
    if _required_env(environ, "GITHUB_REF") != REF:
        _fail("Untrusted GitHub ref")
    sha = _strict_sha(_required_env(environ, "GITHUB_SHA"), "GitHub")
    output_path = Path(_required_env(environ, "GITHUB_OUTPUT"))

    if event_name == "push":
        _validate_push_event(event, sha)
    else:
        _validate_dispatch_event(event)

    return TrustedContext(event_name, event, sha, output_path)


def _validate_push_event(event: Mapping[str, Any], after: str) -> None:
    if event.get("ref") != REF:
        _fail("Push event is not for the main ref")
    payload_after = _strict_sha(event.get("after"), "push after")
    if payload_after != after:
        _fail("Push event SHA does not match GitHub SHA")
    for key in ("created", "deleted", "forced"):
        if event.get(key) is not False:
            _fail(f"Push event has unsupported {key} state")
    _strict_sha(event.get("before"), "push before")


def _validate_dispatch_event(event: Mapping[str, Any]) -> None:
    inputs = event.get("inputs")
    if not isinstance(inputs, dict):
        _fail("Manual event inputs are missing")
    confirm = inputs.get("confirm_publish")
    if confirm is not True and confirm != "true":
        _fail("Manual publish confirmation is required")
    if not isinstance(inputs.get("expected_version"), str):
        _fail("Manual expected version is missing")


def _run_git(args: Sequence[str], *, cwd: str | os.PathLike[str] = ".") -> bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        _fail("Git is unavailable")
    if completed.returncode != 0:
        _fail("Git could not verify the release commit")
    return completed.stdout


def checkout_head_sha(*, cwd: str | os.PathLike[str] = ".") -> str:
    output = _run_git(["rev-parse", "--verify", "HEAD"], cwd=cwd)
    try:
        head = output.decode("ascii").strip()
    except UnicodeDecodeError:
        _fail("Git returned an invalid checkout SHA")
    return _strict_sha(head, "checkout")


def verify_checkout(expected_sha: str, *, cwd: str | os.PathLike[str] = ".") -> None:
    expected = _strict_sha(expected_sha, "GitHub")
    if checkout_head_sha(cwd=cwd) != expected:
        _fail("Checked-out HEAD does not match GitHub SHA")


def _verify_commit_available(sha: str, *, cwd: str | os.PathLike[str] = ".") -> None:
    _strict_sha(sha, "commit")
    try:
        completed = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        _fail("Git is unavailable")
    if completed.returncode != 0:
        _fail("Required release commit is unavailable")


def _verify_ancestor(before: str, after: str, *, cwd: str | os.PathLike[str] = ".") -> None:
    try:
        completed = subprocess.run(
            ["git", "merge-base", "--is-ancestor", before, after],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        _fail("Git is unavailable")
    if completed.returncode != 0:
        _fail("Push base is not an ancestor of the release commit")


def git_show_pyproject(sha: str, *, cwd: str | os.PathLike[str] = ".") -> bytes:
    """Read only the requested immutable Git object; never execute its code."""

    _strict_sha(sha, "commit")
    return _run_git(["show", "--no-ext-diff", "--format=", f"{sha}:{PYPROJECT_PATH}"], cwd=cwd)


def parse_project_version(source: bytes, *, label: str) -> Version:
    try:
        text = source.decode("utf-8")
        document = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        _fail(f"Invalid {label} pyproject metadata")
    if not isinstance(document, dict):
        _fail(f"Invalid {label} pyproject metadata")
    project = document.get("project")
    if not isinstance(project, dict) or project.get("name") != PROJECT:
        _fail(f"Invalid {label} project name")
    dynamic = project.get("dynamic", [])
    if not isinstance(dynamic, list) or any(not isinstance(item, str) for item in dynamic):
        _fail(f"Invalid {label} project metadata")
    if "version" in dynamic:
        _fail(f"{label.capitalize()} project version must be static")
    raw_version = project.get("version")
    if not isinstance(raw_version, str) or not raw_version or raw_version != raw_version.strip():
        _fail(f"Invalid {label} project version")
    try:
        parsed = Version(raw_version)
    except InvalidVersion:
        _fail(f"Invalid {label} project version")
    if not str(parsed) or any(ord(character) < 0x20 for character in str(parsed)):
        _fail(f"Invalid {label} project version")
    return parsed


def _stable(version: Version, label: str) -> None:
    if version.is_prerelease or version.is_devrelease or version.local:
        _fail(f"{label.capitalize()} version is not stable")


def evaluate_push(
    context: TrustedContext,
    *,
    cwd: str | os.PathLike[str] = ".",
) -> GateResult:
    before = _strict_sha(context.event["before"], "push before")
    after = context.sha
    _verify_commit_available(before, cwd=cwd)
    _verify_commit_available(after, cwd=cwd)
    _verify_ancestor(before, after, cwd=cwd)

    before_version = parse_project_version(git_show_pyproject(before, cwd=cwd), label="base")
    current_version = parse_project_version(git_show_pyproject(after, cwd=cwd), label="current")
    normalized = str(current_version)
    if current_version == before_version:
        return GateResult(False, normalized, "version is unchanged; publish skipped")
    if current_version < before_version:
        _fail("Current version is lower than the push base")
    _stable(current_version, "current")
    return GateResult(True, normalized, "stable version increased; publish enabled")


class _RejectRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request: Request, *args: Any, **kwargs: Any) -> Request:
        _fail("PyPI lookup redirected")


def _safe_urlopen(request: Request, *, timeout: int) -> Any:
    opener = build_opener(_RejectRedirectHandler)
    return opener.open(request, timeout=timeout)


def _pypi_project_is_missing(
    *,
    opener: Callable[..., Any],
) -> bool:
    request = Request(PYPI_URL, method="GET", headers={"User-Agent": USER_AGENT})
    try:
        response = opener(request, timeout=15)
    except HTTPError as error:
        error_url = error.geturl() if hasattr(error, "geturl") else getattr(error, "url", None)
        try:
            if error.code == 404 and error_url == PYPI_URL:
                return True
            _fail("PyPI lookup returned an unexpected HTTP status")
        finally:
            close = getattr(error, "close", None)
            if callable(close):
                close()
    except Exception:
        _fail("PyPI lookup failed")

    try:
        status = getattr(response, "status", None)
        if status is None and hasattr(response, "getcode"):
            status = response.getcode()
        final_url = response.geturl() if hasattr(response, "geturl") else None
        if final_url != PYPI_URL:
            _fail("PyPI lookup returned an unexpected response")
        if status == 404:
            return True
        if status != 200:
            _fail("PyPI lookup returned an unexpected HTTP status")
        return False
    except GateError:
        raise
    except Exception:
        _fail("PyPI lookup failed")
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    return False


def evaluate_manual(
    context: TrustedContext,
    *,
    cwd: str | os.PathLike[str] = ".",
    opener: Callable[..., Any],
) -> GateResult:
    verify_checkout(context.sha, cwd=cwd)
    current_version = parse_project_version(git_show_pyproject(context.sha, cwd=cwd), label="current")
    expected_raw = context.event["inputs"]["expected_version"]
    if expected_raw != expected_raw.strip():
        _fail("Manual expected version is not strictly formatted")
    try:
        expected_version = Version(expected_raw)
    except InvalidVersion:
        _fail("Invalid manual expected version")
    if expected_version != current_version:
        _fail("Manual expected version does not match the current version")
    _stable(current_version, "current")
    if not _pypi_project_is_missing(opener=opener):
        _fail("PyPI project already exists")
    return GateResult(True, str(current_version), "first PyPI upload is authorized")


def evaluate(
    environ: Mapping[str, str] | None = None,
    *,
    cwd: str | os.PathLike[str] = ".",
    opener: Callable[..., Any] | None = None,
) -> tuple[TrustedContext, GateResult]:
    values = os.environ if environ is None else environ
    event = load_event(_required_env(values, "GITHUB_EVENT_PATH"))
    context = trusted_context(values, event)
    if context.event_name == "push":
        verify_checkout(context.sha, cwd=cwd)
        result = evaluate_push(context, cwd=cwd)
    else:
        result = evaluate_manual(context, cwd=cwd, opener=opener or _safe_urlopen)
    return context, result


def write_output(path: Path, result: GateResult) -> None:
    try:
        with path.open("a", encoding="utf-8") as output:
            output.write(f"should_publish={'true' if result.should_publish else 'false'}\n")
            output.write(f"version={result.version}\n")
    except OSError:
        _fail("Unable to write GitHub output")


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] = ".",
    opener: Callable[..., Any] | None = None,
) -> int:
    del argv
    try:
        context, result = evaluate(environ, cwd=cwd, opener=opener)
        write_output(context.output_path, result)
    except GateError as error:
        print(f"Release gate failed: {error}", file=sys.stderr)
        return 1
    print(f"Release gate passed: {result.reason} ({result.version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

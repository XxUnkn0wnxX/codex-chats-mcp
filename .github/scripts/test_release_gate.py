import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from release_gate import (
    PROJECT,
    PYPI_URL,
    REF,
    REPOSITORY,
    USER_AGENT,
    GateError,
    GateResult,
    evaluate,
    main,
    parse_project_version,
)


class GitFixture:
    def __init__(self, initial_version: str = "0.2.0") -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self._git("init", "-q")
        self._git("config", "user.name", "Test Author")
        self._git("config", "user.email", "test@example.invalid")
        self.commit_version(initial_version)

    def close(self) -> None:
        self.temporary.cleanup()

    def _git(self, *args: str) -> str:
        command = [
            "git",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ]
        completed = subprocess.run(
            command,
            cwd=self.root,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin"},
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def commit_version(self, version: str, *, extra: str = "") -> str:
        (self.root / "pyproject.toml").write_text(
            f"[project]\nname = \"{PROJECT}\"\nversion = \"{version}\"\n{extra}",
            encoding="utf-8",
        )
        self._git("add", "pyproject.toml")
        self._git("commit", "-q", "-m", "Update package metadata")
        return self.head

    @property
    def head(self) -> str:
        return self._git("rev-parse", "HEAD")

    @property
    def base(self) -> str:
        return self._git("rev-list", "--max-parents=0", "HEAD").splitlines()[0]

    def event_file(self, event: dict[str, object]) -> Path:
        path = self.root / "event.json"
        path.write_text(json.dumps(event), encoding="utf-8")
        return path

    def environment(
        self,
        event: dict[str, object],
        *,
        event_name: str = "push",
        sha: str | None = None,
    ) -> dict[str, str]:
        return {
            "GITHUB_EVENT_NAME": event_name,
            "GITHUB_EVENT_PATH": str(self.event_file(event)),
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_REF": REF,
            "GITHUB_SHA": sha or self.head,
            "GITHUB_OUTPUT": str(self.root / "github-output"),
        }

    def push_event(self, before: str | None = None, *, after: str | None = None, **changes: object) -> dict[str, object]:
        return {
            "ref": REF,
            "before": before or self.base,
            "after": after or self.head,
            "created": False,
            "deleted": False,
            "forced": False,
            **changes,
        }


class FakeResponse:
    def __init__(self, status: int = 200, url: str = PYPI_URL) -> None:
        self.status = status
        self.url = url
        self.closed = False

    def geturl(self) -> str:
        return self.url

    def close(self) -> None:
        self.closed = True

    def read(self) -> bytes:
        raise AssertionError("the release gate must not parse the PyPI response body")


class ReleaseGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.close)

    def push_result(self, before: str | None = None, **changes: object) -> GateResult:
        event = self.fixture.push_event(before, **changes)
        _, result = evaluate(self.fixture.environment(event), cwd=self.fixture.root, opener=self.no_network)
        return result

    @staticmethod
    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("push evaluation must not use the network")

    @staticmethod
    def missing_project(request: object, timeout: object) -> object:
        raise HTTPError(PYPI_URL, 404, "not found", {}, io.BytesIO())

    def test_normal_version_bump_publishes(self) -> None:
        before = self.fixture.base
        self.fixture.commit_version("0.3.0")
        result = self.push_result(before)
        self.assertEqual(result, GateResult(True, "0.3.0", "stable version increased; publish enabled"))

    def test_normalized_equal_versions_skip(self) -> None:
        before = self.fixture.base
        self.fixture.commit_version("0.2.0.0")
        result = self.push_result(before)
        self.assertFalse(result.should_publish)
        self.assertEqual(result.version, "0.2.0.0")

    def test_equal_version_docs_only_push_skips(self) -> None:
        before = self.fixture.base
        self.fixture.commit_version("0.2.0", extra='description = "Documentation-only commit"\n')
        result = self.push_result(before)
        self.assertFalse(result.should_publish)
        self.assertEqual(result.version, "0.2.0")

    def test_numerical_version_ordering_uses_packaging(self) -> None:
        before = self.fixture.base
        self.fixture.commit_version("0.10.0")
        self.assertTrue(self.push_result(before).should_publish)

        fixture = GitFixture("0.10.0")
        self.addCleanup(fixture.close)
        old = fixture.base
        fixture.commit_version("0.9.0")
        with self.assertRaisesRegex(GateError, "lower"):
            evaluate(fixture.environment(fixture.push_event(old)), cwd=fixture.root)

    def test_development_prerelease_and_local_candidates_fail(self) -> None:
        for version in ("0.3.0.dev1", "0.3.0rc1", "0.3.0+local"):
            with self.subTest(version=version):
                fixture = GitFixture()
                self.addCleanup(fixture.close)
                before = fixture.base
                fixture.commit_version(version)
                with self.assertRaisesRegex(GateError, "stable"):
                    evaluate(fixture.environment(fixture.push_event(before)), cwd=fixture.root)

    def test_pep440_version_is_normalized_in_output(self) -> None:
        before = self.fixture.base
        self.fixture.commit_version("v0.3")
        result = self.push_result(before)
        self.assertEqual(result.version, "0.3")

    def test_invalid_toml_name_dynamic_and_version_are_rejected(self) -> None:
        cases = (
            b"[project\nname = 'x'",
            b"[project]\nname = 'different'\nversion = '0.3.0'",
            b"[project]\nname = 'codex-chats-mcp-v2'\ndynamic = ['version']",
            b"[project]\nname = 'codex-chats-mcp-v2'\nversion = 'not a version'",
        )
        for source in cases:
            with self.subTest(source=source):
                with self.assertRaises(GateError):
                    parse_project_version(source, label="current")

    def test_strict_sha_and_context_checks_happen_before_git(self) -> None:
        event = self.fixture.push_event()
        environment = self.fixture.environment(event, sha="not-a-commit")
        with self.assertRaisesRegex(GateError, "Invalid GitHub commit SHA"):
            evaluate(environment, cwd=self.root_that_does_not_exist())

        for key, value in (
            ("GITHUB_REPOSITORY", "attacker/example"),
            ("GITHUB_REF", "refs/heads/develop"),
        ):
            with self.subTest(key=key):
                bad = self.fixture.environment(event)
                bad[key] = value
                with self.assertRaises(GateError):
                    evaluate(bad, cwd=self.root_that_does_not_exist())

    def root_that_does_not_exist(self) -> Path:
        return self.fixture.root / "no-checkout"

    def test_push_after_must_match_github_sha(self) -> None:
        event = self.fixture.push_event(after="1" * 40)
        with self.assertRaisesRegex(GateError, "does not match"):
            evaluate(self.fixture.environment(event), cwd=self.fixture.root)

    def test_push_rejects_missing_zero_created_deleted_and_forced_before(self) -> None:
        for field, value in (
            ("before", None),
            ("before", "0" * 40),
            ("created", True),
            ("deleted", True),
            ("forced", True),
        ):
            with self.subTest(field=field, value=value):
                event = self.fixture.push_event()
                if value is None:
                    del event[field]
                else:
                    event[field] = value
                with self.assertRaises(GateError):
                    evaluate(self.fixture.environment(event), cwd=self.fixture.root)

    def test_push_rejects_unavailable_and_nonancestor_base(self) -> None:
        event = self.fixture.push_event(before="1" * 40)
        with self.assertRaisesRegex(GateError, "unavailable"):
            evaluate(self.fixture.environment(event), cwd=self.fixture.root)

        branch = self.fixture._git("branch", "--show-current")
        self.fixture._git("checkout", "-q", "-b", "side")
        sibling = self.fixture.commit_version("0.1.0")
        self.fixture._git("checkout", "-q", branch)
        self.fixture.commit_version("0.3.0")
        event = self.fixture.push_event(before=sibling)
        with self.assertRaisesRegex(GateError, "ancestor"):
            evaluate(self.fixture.environment(event), cwd=self.fixture.root)

    def test_multicommit_push_compares_event_before_not_head_parent(self) -> None:
        before = self.fixture.base
        self.fixture.commit_version("0.3.0")
        self.fixture.commit_version("0.3.0", extra='description = "Documentation-only follow-up"\n')
        self.assertTrue(self.push_result(before).should_publish)

    def test_checkout_head_must_match_github_sha(self) -> None:
        event = self.fixture.push_event(after="1" * 40)
        environment = self.fixture.environment(event, sha="1" * 40)
        with self.assertRaisesRegex(GateError, "HEAD"):
            evaluate(environment, cwd=self.fixture.root)

    def test_manual_requires_exact_confirmation_and_matching_version(self) -> None:
        for confirm in (False, "True", " true", 1, None):
            with self.subTest(confirm=confirm):
                event = {
                    "inputs": {"confirm_publish": confirm, "expected_version": "0.2.0"}
                }
                with self.assertRaisesRegex(GateError, "confirmation"):
                    evaluate(
                        self.fixture.environment(event, event_name="workflow_dispatch"),
                        cwd=self.fixture.root,
                        opener=self.missing_project,
                    )

        for expected in ("0.2.1", "not-a-version", " 0.2.0"):
            with self.subTest(expected=expected):
                event = {"inputs": {"confirm_publish": True, "expected_version": expected}}
                with self.assertRaises(GateError):
                    evaluate(
                        self.fixture.environment(event, event_name="workflow_dispatch"),
                        cwd=self.fixture.root,
                        opener=self.missing_project,
                    )

    def test_manual_boolean_and_string_confirmation_authorize_missing_project(self) -> None:
        for confirm in (True, "true"):
            with self.subTest(confirm=confirm):
                event = {"inputs": {"confirm_publish": confirm, "expected_version": "0.2.0"}}
                _, result = evaluate(
                    self.fixture.environment(event, event_name="workflow_dispatch"),
                    cwd=self.fixture.root,
                    opener=self.missing_project,
                )
                self.assertTrue(result.should_publish)

    def test_manual_rejects_prerelease_dev_and_local_version(self) -> None:
        for version in ("0.2.1rc1", "0.2.1.dev1", "0.2.1+local"):
            with self.subTest(version=version):
                fixture = GitFixture(version)
                self.addCleanup(fixture.close)
                event = {"inputs": {"confirm_publish": True, "expected_version": version}}
                with self.assertRaisesRegex(GateError, "stable"):
                    evaluate(
                        fixture.environment(event, event_name="workflow_dispatch"),
                        cwd=fixture.root,
                        opener=self.missing_project,
                    )

    def test_manual_existing_project_rejects_without_reading_body(self) -> None:
        response = FakeResponse()
        seen: list[tuple[object, object]] = []

        def opener(request: object, timeout: object) -> FakeResponse:
            seen.append((request, timeout))
            return response

        event = {"inputs": {"confirm_publish": True, "expected_version": "0.2.0"}}
        with self.assertRaisesRegex(GateError, "already exists"):
            evaluate(
                self.fixture.environment(event, event_name="workflow_dispatch"),
                cwd=self.fixture.root,
                opener=opener,
            )
        self.assertEqual(len(seen), 1)
        request, timeout = seen[0]
        self.assertEqual(timeout, 15)
        self.assertEqual(request.full_url, PYPI_URL)
        self.assertEqual(dict(request.header_items())["User-agent"], USER_AGENT)
        self.assertFalse(any(key.lower() == "authorization" for key, _ in request.header_items()))
        self.assertTrue(response.closed)

    def test_manual_only_404_authorizes_missing_project(self) -> None:
        def missing(request: object, timeout: object) -> object:
            raise HTTPError(PYPI_URL, 404, "not found", {}, io.BytesIO(b"untrusted"))

        event = {"inputs": {"confirm_publish": True, "expected_version": "0.2.0"}}
        _, result = evaluate(
            self.fixture.environment(event, event_name="workflow_dispatch"),
            cwd=self.fixture.root,
            opener=missing,
        )
        self.assertTrue(result.should_publish)

        response = FakeResponse(404)
        _, result = evaluate(
            self.fixture.environment(event, event_name="workflow_dispatch"),
            cwd=self.fixture.root,
            opener=lambda request, timeout: response,
        )
        self.assertTrue(result.should_publish)
        self.assertTrue(response.closed)

        def redirected_missing(request: object, timeout: object) -> object:
            raise HTTPError("https://example.invalid/redirect", 404, "not found", {}, io.BytesIO())

        with self.assertRaises(GateError):
            evaluate(
                self.fixture.environment(event, event_name="workflow_dispatch"),
                cwd=self.fixture.root,
                opener=redirected_missing,
            )

        for status in (301, 403, 500):
            with self.subTest(status=status):
                def unexpected(request: object, timeout: object, code: int = status) -> object:
                    raise HTTPError(PYPI_URL, code, "unexpected", {}, io.BytesIO())

                with self.assertRaises(GateError):
                    evaluate(
                        self.fixture.environment(event, event_name="workflow_dispatch"),
                        cwd=self.fixture.root,
                        opener=unexpected,
                    )

    def test_manual_transport_and_redirect_fail_closed(self) -> None:
        event = {"inputs": {"confirm_publish": True, "expected_version": "0.2.0"}}
        with self.assertRaisesRegex(GateError, "lookup failed"):
            evaluate(
                self.fixture.environment(event, event_name="workflow_dispatch"),
                cwd=self.fixture.root,
                opener=lambda request, timeout: (_ for _ in ()).throw(URLError("offline")),
            )
        with self.assertRaises(GateError):
            evaluate(
                self.fixture.environment(event, event_name="workflow_dispatch"),
                cwd=self.fixture.root,
                opener=lambda request, timeout: FakeResponse(200, "https://example.invalid/redirect"),
            )

    def test_push_has_no_network_and_manual_is_only_network_path(self) -> None:
        before = self.fixture.base
        self.fixture.commit_version("0.3.0")
        with patch("release_gate._safe_urlopen", side_effect=AssertionError("network")):
            result = self.push_result(before)
        self.assertTrue(result.should_publish)

    def test_cli_writes_outputs_only_after_success(self) -> None:
        before = self.fixture.base
        self.fixture.commit_version("0.3.0")
        environment = self.fixture.environment(self.fixture.push_event(before))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            self.assertEqual(main(environ=environment, cwd=self.fixture.root), 0)
        self.assertEqual(stdout.getvalue().count("Release gate passed"), 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            Path(environment["GITHUB_OUTPUT"]).read_text(encoding="utf-8"),
            "should_publish=true\nversion=0.3.0\n",
        )

        failed = dict(environment)
        failed["GITHUB_SHA"] = "1" * 40
        failed_event = self.fixture.push_event(after="1" * 40)
        failed["GITHUB_EVENT_PATH"] = str(self.fixture.event_file(failed_event))
        failed_output = self.fixture.root / "failed-output"
        failed["GITHUB_OUTPUT"] = str(failed_output)
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", stderr):
            self.assertEqual(main(environ=failed, cwd=self.fixture.root), 1)
        self.assertFalse(failed_output.exists())
        self.assertIn("Release gate failed:", stderr.getvalue())
        self.assertNotIn("1" * 40, stderr.getvalue())

    def test_cli_failure_does_not_dump_event_payload(self) -> None:
        secret = "do-not-print-this-payload"
        event = self.fixture.push_event(ref=secret)
        environment = self.fixture.environment(event)
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            self.assertEqual(main(environ=environment, cwd=self.fixture.root), 1)
        self.assertNotIn(secret, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest
from importlib.metadata import distribution
from pathlib import Path
from unittest.mock import patch

import codex_chats_mcp as server


class DistributionTest(unittest.TestCase):
    def test_fork_distribution_and_user_agent_version_match(self) -> None:
        installed = distribution("codex-chats-mcp-v2")
        self.assertEqual(installed.metadata["Name"], "codex-chats-mcp-v2")
        self.assertEqual(server.USER_AGENT, f"codex-chats-mcp/{installed.version}")

    def test_console_command_remains_compatible(self) -> None:
        installed = distribution("codex-chats-mcp-v2")
        console_scripts = {
            entry.name: entry.value
            for entry in installed.entry_points
            if entry.group == "console_scripts"
        }
        self.assertEqual(console_scripts, {"codex-chats-mcp": "codex_chats_mcp:main"})

    def test_pypi_description_contains_markdown_readme(self) -> None:
        metadata = distribution("codex-chats-mcp-v2").metadata
        self.assertTrue(metadata["Summary"])
        self.assertEqual(metadata["Description-Content-Type"], "text/markdown")
        readme = metadata.get_payload()
        self.assertTrue(readme.startswith("# codex-chats-mcp\n"))
        self.assertIn("codex-chats-mcp-v2", readme)

    def test_project_links_identify_fork_and_upstream(self) -> None:
        metadata = distribution("codex-chats-mcp-v2").metadata
        links = dict(link.split(", ", 1) for link in metadata.get_all("Project-URL", []))
        fork = "https://github.com/XxUnkn0wnxX/codex-chats-mcp"
        self.assertEqual(links.get("Homepage"), fork)
        self.assertEqual(links.get("Repository"), fork)
        self.assertEqual(links.get("Issues"), f"{fork}/issues")
        self.assertEqual(links.get("Upstream"), "https://github.com/shoyu-ramen/codex-chats-mcp")

    def test_default_install_does_not_enable_debug_logging(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(server._debug_logging_enabled())
            self.assertIsNone(server._resolve_error_log_path())

    @unittest.skipIf(server.DEBUG_BUILD, "requires a release wheel")
    def test_release_build_rejects_runtime_only_debug_logging(self) -> None:
        self.assertFalse(server.DEBUG_BUILD)
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "errors.jsonl"
            with patch.dict(
                os.environ,
                {
                    server.CODEX_CHATS_DEBUG_LOG: "1",
                    server.CODEX_CHATS_ERROR_LOG: str(log_path),
                },
            ):
                self.assertFalse(server._debug_logging_enabled())
                self.assertIsNone(server._resolve_error_log_path())
                server._append_error_log(
                    {
                        "event": "terminal_error",
                        "operation": "list_conversations",
                        "method": "GET",
                        "endpoint": "/conversations",
                        "status": 500,
                        "error_type": "HTTPError",
                    },
                    log_path,
                )

            self.assertFalse(log_path.exists())

    def test_debug_capability_requires_build_and_runtime_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "errors.jsonl"
            with patch.object(server, "DEBUG_BUILD", True), patch.dict(
                os.environ,
                {
                    server.CODEX_CHATS_DEBUG_LOG: "1",
                    server.CODEX_CHATS_ERROR_LOG: str(log_path),
                },
            ):
                self.assertTrue(server._debug_logging_enabled())
                self.assertEqual(server._resolve_error_log_path(), log_path)
                server._append_error_log(
                    {
                        "event": "terminal_error",
                        "operation": "list_conversations",
                        "method": "GET",
                        "endpoint": "/conversations",
                        "status": 500,
                        "error_type": "HTTPError",
                    },
                    log_path,
                )

            self.assertTrue(log_path.is_file())


if __name__ == "__main__":
    unittest.main()

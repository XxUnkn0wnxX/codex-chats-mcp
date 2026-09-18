import os
import unittest
from importlib.metadata import distribution
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

    def test_default_install_does_not_enable_debug_logging(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(server._debug_logging_enabled())
            self.assertIsNone(server._resolve_error_log_path())


if __name__ == "__main__":
    unittest.main()

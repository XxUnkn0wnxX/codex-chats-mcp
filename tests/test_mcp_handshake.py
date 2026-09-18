import json
import sys
import textwrap
import unittest
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client


class MCPHandshakeTest(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_and_list_tools(self) -> None:
        console_name = "codex-chats-mcp.exe" if sys.platform == "win32" else "codex-chats-mcp"
        executable = Path(sys.executable).parent / console_name
        self.assertTrue(executable.is_file(), executable)
        server_parameters = StdioServerParameters(
            command=str(executable),
        )

        async with stdio_client(server_parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialize_result = await session.initialize()
                list_result = await session.list_tools()

        self.assertEqual(initialize_result.server_info.name, "codex-chats")
        tool_names = [tool.name for tool in list_result.tools]
        self.assertEqual(len(tool_names), 17)
        self.assertTrue(
            {
                "list_conversations",
                "search_conversations",
                "get_conversation",
            }.issubset(tool_names)
        )

    async def test_stdio_read_tools_with_mocked_backend(self) -> None:
        # Exercise the installed SDK's dispatch, validation, serialization, and
        # middleware without reading credentials or contacting ChatGPT.
        child_code = textwrap.dedent(
            """
            import re
            from unittest.mock import patch

            import codex_chats_mcp as server

            def fake_request(method, path, body=None):
                assert method == "GET" and body is None
                context = server._error_correlation_fields()
                assert context["tool_call"] in {
                    "list_conversations", "search_conversations", "get_conversation"
                }
                assert re.fullmatch(r"hash:[0-9a-f]{64}", context["mcp_request_id"])
                if path.startswith("/conversations?"):
                    return 200, {"total": 3, "items": [
                        {"id": "fixture-1", "title": "SDK compatibility", "is_archived": False},
                        {"id": "fixture-2", "title": "Other fixture", "is_archived": False},
                        {"id": "fixture-3", "title": "Archived fixture", "is_archived": True},
                    ]}
                if path == "/conversation/fixture-1":
                    return 200, {"id": "fixture-1", "mapping": {
                        "node-1": {"message": {"content": {"parts": ["Synthetic fixture"]}}}
                    }}
                if path == "/conversation/missing-fixture":
                    return 404, {"detail": "Synthetic not-found response"}
                raise AssertionError("Unexpected backend request")

            with patch.object(server, "_request", side_effect=fake_request), \\
                 patch.object(server, "_auth_headers", side_effect=AssertionError("No auth allowed")), \\
                 patch.object(server.urllib.request, "urlopen", side_effect=AssertionError("No network allowed")):
                server.main()
            """
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-I", "-c", child_code],
        )

        def payload(result: types.CallToolResult) -> dict:
            self.assertFalse(result.is_error)
            self.assertEqual(len(result.content), 1)
            self.assertEqual(result.content[0].type, "text")
            decoded = json.loads(result.content[0].text)
            if result.structured_content is not None:
                self.assertEqual(result.structured_content, decoded)
            return decoded

        with anyio.fail_after(30):
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    await session.list_tools()

                    listed = payload(
                        await session.call_tool("list_conversations", {"max_results": 1})
                    )
                    self.assertEqual(listed["count"], 1)
                    self.assertEqual(listed["items"][0]["id"], "fixture-1")

                    searched = payload(
                        await session.call_tool("search_conversations", {"query": "sdk"})
                    )
                    self.assertEqual(searched["count"], 1)
                    self.assertEqual(searched["items"][0]["id"], "fixture-1")

                    fetched = payload(
                        await session.call_tool("get_conversation", {"conversation_id": "fixture-1"})
                    )
                    self.assertTrue(fetched["ok"])
                    message = fetched["conversation"]["mapping"]["node-1"]["message"]
                    self.assertEqual(message["content"]["parts"], ["Synthetic fixture"])

                    missing = payload(
                        await session.call_tool(
                            "get_conversation", {"conversation_id": "missing-fixture"}
                        )
                    )
                    self.assertFalse(missing["ok"])
                    self.assertEqual(missing["status"], 404)

                    invalid = await session.call_tool("get_conversation", {})
                    self.assertTrue(invalid.is_error)


if __name__ == "__main__":
    unittest.main()

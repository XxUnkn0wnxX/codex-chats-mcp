import sys
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
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


if __name__ == "__main__":
    unittest.main()

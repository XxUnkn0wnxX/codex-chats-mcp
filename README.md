# codex-chats-mcp

[![PyPI version](https://img.shields.io/pypi/v/codex-chats-mcp.svg)](https://pypi.org/project/codex-chats-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/codex-chats-mcp.svg)](https://pypi.org/project/codex-chats-mcp/)
[![License: MIT](https://img.shields.io/pypi/l/codex-chats-mcp.svg)](https://github.com/XxUnkn0wnxX/codex-chats-mcp/blob/main/LICENSE)

An unofficial MCP server for listing, searching, archiving, renaming, exporting, and deleting ChatGPT conversations and Codex Cloud tasks from MCP-compatible clients.

This is the maintained [XxUnkn0wnxX/codex-chats-mcp](https://github.com/XxUnkn0wnxX/codex-chats-mcp) fork of [shoyu-ramen/codex-chats-mcp](https://github.com/shoyu-ramen/codex-chats-mcp). It wraps undocumented `chatgpt.com/backend-api` endpoints, which can change without notice.

The current source line requires the MCP Python SDK v2: `mcp[cli]>=2.1.1,<3`.

## Release status

- The latest published PyPI release is `0.1.1`.
- The maintained fork source build is `0.1.2.dev1` and is not published to PyPI.
- The PyPI workflow is retained for a future tested release but is disabled by default behind a repository-variable gate and manual confirmation. The normal `Test` workflow never publishes.

## Install

### Published PyPI release (`0.1.1`)

```zsh
python3 -m pip install codex-chats-mcp
```

Or with `uv`:

```zsh
uv tool install codex-chats-mcp
```

### Maintained fork source (`0.1.2.dev1`)

```zsh
git clone --branch develop https://github.com/XxUnkn0wnxX/codex-chats-mcp
cd codex-chats-mcp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

## Authentication

After you sign in through Codex's normal login flow, the server internally reads `~/.codex/auth.json`, the file maintained by Codex. Never print, copy, or edit this file; it contains credentials for your ChatGPT account and must be treated as a secret.

## Wire it up

### Codex CLI (`~/.codex/config.toml`)

Codex stores local MCP server configuration in this file; see the [official Codex MCP setup documentation](https://learn.chatgpt.com/docs/extend/mcp#connect-codex-to-an-mcp-server).

For an installed command:

```toml
[mcp_servers.codex-chats]
command = "codex-chats-mcp"
```

For a source build, use its absolute executable path:

```toml
[mcp_servers.codex-chats]
command = "/absolute/path/to/venv/bin/codex-chats-mcp"
```

Replace the path with the executable installed on your system. Codex also
supports an `enabled_tools` allowlist if you want to expose only selected
tools; choose it for your own workflow rather than copying another user's
policy.

### Claude Code

```zsh
claude mcp add codex-chats codex-chats-mcp
```

### Other MCP clients

Point the client at the `codex-chats-mcp` executable. It speaks MCP over stdio.

## Tools

### ChatGPT conversations (the “Recents” list)

| Tool | What it does |
|---|---|
| `list_conversations` | Paginates through conversations; archived conversations are excluded by default. |
| `get_conversation` | Fetches one full conversation payload, including its message tree. |
| `search_conversations` | Performs a client-side substring match on conversation titles. |
| `rename_conversation` | Changes a conversation title. |
| `archive_conversation` / `unarchive_conversation` | Toggles the archive flag. |
| `delete_conversation` | Permanently deletes one conversation (`is_visible=false`). No undo. |
| `delete_conversations_matching` | Deletes every conversation whose title matches a substring; requires `confirm=True`. |
| `delete_all_conversations` | Permanently deletes every visible conversation, like ChatGPT's “Delete all chats”; requires `confirm=True`. |
| `export_conversations` | Writes conversation titles/IDs, and optionally full message trees, to a JSON file. |

### Codex Cloud tasks

| Tool | What it does |
|---|---|
| `list_chats` | Paginates Codex tasks, filterable by `all`, `current`, or `archived`. |
| `get_chat` | Fetches a summary of one task. |
| `get_chat_raw` | Fetches the full raw task payload. |
| `archive_chat` / `unarchive_chat` | Toggles archive state. |
| `delete_chat` | Permanently deletes a task, active or archived. No undo. |
| `delete_all_archived` | Permanently deletes every archived task; requires `confirm=True`. |

## Safety

Every destructive bulk action (`delete_all_conversations`, `delete_all_archived`, and `delete_conversations_matching`) requires `confirm=True`. Without confirmation, the tool returns a preview of what would be deleted. Deletions are permanent and have no ChatGPT-side undo.

## Troubleshooting

### HTTP and Cloudflare responses

The default User-Agent is the honest `codex-chats-mcp/0.1.2.dev1`. `CODEX_CHATS_USER_AGENT` can override it for diagnostics only; it does not bypass Cloudflare.

Only Cloudflare-identified HTML on `GET` requests is retried: status 403, 404, or any 5xx, plus nominal HTTP 200 HTML, for three total attempts with 0.5s then 1.0s backoff. Cloudflare identification requires `Server: cloudflare` or a nonempty `CF-Ray` header. Mutations, 429 rate limits, JSON/auth errors, generic HTML, and transport errors are not retried.

Terminal HTML errors are sanitized: raw pages are not returned. The outer tool response contains `status`; its payload uses `content_type`, `server`, `cf_ray`, and `attempts`, plus allowlisted retry-diagnostic fields where present.

### Opt-in debug error logging

Enable it explicitly in the MCP environment:

```toml
[mcp_servers.codex-chats.env]
CODEX_CHATS_DEBUG_LOG = "1"
```

Without `CODEX_CHATS_DEBUG_LOG=1`, no log file is created. With an active virtual environment, the default path is `<active-venv>/codex-chats-mcp-errors.log`. Set `CODEX_CHATS_ERROR_LOG` to a trusted private regular-file path, or set it to `off` to disable the file.

Only retry and terminal-error events are logged: never successes, message content, authentication, request/response bodies, queries, or full resource IDs. Entries contain a locally generated `server_instance_id`, a per-process keyed digest of the `mcp_request_id`, the bounded `tool_call`, normalized endpoint/status/error/attempt/CF-Ray fields, and redacted resource IDs. Raw peer request IDs are never written. The server-instance value groups records from one connector process; it is not a Codex conversation/session ID, because the stdio MCP transport does not expose one. JSONL logs use POSIX mode `0600`, have a hard 8 MiB cap, and trim the oldest complete records to about 6 MiB when necessary. Writes are cross-process locked for concurrent connector sessions and fail closed if locking is unavailable. Custom paths must remain trusted private regular files.

### MCP stdio startup

Running `codex-chats-mcp` manually waits for an MCP client on stdio; it is not an interactive smoke test. Use an MCP client handshake and tool-list check instead.

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for detailed source setup, tests, clean-wheel validation, and live injection guidance.

## License

MIT

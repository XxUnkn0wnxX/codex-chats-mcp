# codex-chats-mcp

[![PyPI version](https://img.shields.io/pypi/v/codex-chats-mcp.svg)](https://pypi.org/project/codex-chats-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/codex-chats-mcp.svg)](https://pypi.org/project/codex-chats-mcp/)
[![License: MIT](https://img.shields.io/pypi/l/codex-chats-mcp.svg)](https://github.com/shoyu-ramen/codex-chats-mcp/blob/main/LICENSE)
[![Publish to PyPI](https://github.com/shoyu-ramen/codex-chats-mcp/actions/workflows/publish.yml/badge.svg)](https://github.com/shoyu-ramen/codex-chats-mcp/actions/workflows/publish.yml)

An MCP server for managing ChatGPT conversations and Codex Cloud tasks (chats) from any MCP-compatible client — Claude Code, Codex, Cline, etc.

ChatGPT's web UI lets you archive chats but the "Delete all" button only wipes visible ones, and Codex Cloud has no per-task delete at all. This server wraps the internal `chatgpt.com/backend-api` endpoints so you can list, search, rename, archive, export, and **permanently delete** both kinds of chats from your agent.

> **Unofficial.** This uses undocumented internal endpoints (`/conversations/*`, `/wham/tasks/*`). They can change without notice and require a valid ChatGPT session. Use at your own risk.

The current development line targets the MCP Python SDK `>=2.1.1,<3` and is v2-only.

## Install

```bash
pip install codex-chats-mcp
```

Or with `uv`:

```bash
uv tool install codex-chats-mcp
```

This installs a `codex-chats-mcp` executable.

## Authentication

The server reads `~/.codex/auth.json` — the same file the Codex CLI maintains after `codex login`. If you don't have Codex installed, log in once with `npx @openai/codex login` (or sign in via the Codex desktop app) to produce the file.

The token has full access to your ChatGPT account. Treat the auth file as a secret.

## Wire it up

### Codex CLI (`~/.codex/config.toml`)

```toml
[mcp_servers.codex-chats]
command = "codex-chats-mcp"
```

### Claude Code

```bash
claude mcp add codex-chats codex-chats-mcp
```

### Anything else

Point your MCP client at the `codex-chats-mcp` executable. It speaks MCP over stdio.

## Tools

### ChatGPT conversations (the "Recents" list)

| Tool | What it does |
|---|---|
| `list_conversations` | Paginates through your conversations. Filters out archived by default. |
| `get_conversation` | Full payload for one conversation, including the message tree. |
| `search_conversations` | Substring match on titles (client-side). |
| `rename_conversation` | Change a conversation's title. |
| `archive_conversation` / `unarchive_conversation` | Toggle the archive flag. |
| `delete_conversation` | Permanently delete one chat (`is_visible=false`). No undo. |
| `delete_conversations_matching` | Delete every chat whose title matches a substring. Requires `confirm=True`. |
| `delete_all_conversations` | Nuke every visible chat — same as ChatGPT's "Delete all chats" button. Requires `confirm=True`. |
| `export_conversations` | Dump titles/IDs (and optionally full message trees) to a JSON file. |

### Codex Cloud tasks

| Tool | What it does |
|---|---|
| `list_chats` | Paginate Codex tasks, filterable by `all` / `current` / `archived`. |
| `get_chat` | Summary of one task. |
| `get_chat_raw` | Full raw task payload. |
| `archive_chat` / `unarchive_chat` | Toggle archive state. |
| `delete_chat` | Permanently delete a task. Works on active OR archived. |
| `delete_all_archived` | Bulk-delete every archived task. Requires `confirm=True`. |

## Safety

Every destructive bulk action (`delete_all_conversations`, `delete_all_archived`, `delete_conversations_matching`) requires `confirm=True`. Without it the tool returns a preview of what *would* be deleted. There is no undo on the ChatGPT side.

## Troubleshooting

The default User-Agent is the honest `codex-chats-mcp/0.1.2.dev1`. To opt in to a custom value, set `CODEX_CHATS_USER_AGENT` in your Codex configuration:

```toml
[mcp_servers.codex-chats.env]
CODEX_CHATS_USER_AGENT = "your preferred User-Agent string"
```

This does not bypass Cloudflare: these internal endpoints can change. Cloudflare-identified HTML (`Server: cloudflare` or a nonempty `CF-Ray`) on read-only GET responses with status 403, 404, or 5xx is retried for three total attempts with 0.5s then 1.0s backoff; an unexpected nominal HTTP 200 HTML response is normalized to synthetic 502 and follows the same policy. Mutations, JSON/auth/API errors, generic HTML, transport errors, and 429 rate limits are not retried. Terminal HTML responses are sanitized and include existing `Content-Type`, `Server`, and `CF-Ray` metadata plus `attempts` and, when present, only the allowlisted `Retry-After`, `CF-Mitigated`, `CF-Error-Type`, and `CF-Error-Origin` headers instead of raw pages.

## Development

```bash
git clone https://github.com/shoyu-ramen/codex-chats-mcp
cd codex-chats-mcp
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
codex-chats-mcp
```

## License

MIT

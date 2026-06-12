# Claudot Bridge Daemon

The bridge daemon connects the Godot editor to an AI chat backend. Three
backends are supported, selected at runtime by the Godot plugin via the
`chat/configure` message:

| Backend | Module | Auth | Tools |
|---|---|---|---|
| `claude-code` (default) | Claude Agent SDK → Claude Code CLI | CLI OAuth login, or `ANTHROPIC_API_KEY` if provided | Full Claude Code (files, bash, MCP) |
| `anthropic` | `providers.py` → Anthropic Messages API | API key (required) | Godot scene/docs tools via HTTP bridge |
| `openai` / `custom` | `providers.py` → any OpenAI-compatible `/chat/completions` | API key (and base URL for `custom`) | Godot scene/docs tools via HTTP bridge |

The `anthropic` backend fully supports **Claude Fable 5** (always-on thinking,
refusal stop reason, 1M context) as well as Opus 4.8/4.7/4.6, Sonnet 4.6, and
Haiku 4.5, with prompt caching enabled for cost efficiency.

## Architecture

```
Godot Editor (chat panel)
    ↓ TCP (per-project port, chat/* JSON-RPC)
Bridge Daemon (agent_bridge.py)
    ├─→ Claude Agent SDK (claude-code backend, persistent session)
    └─→ Direct providers (providers.py)
            ├─→ Anthropic Messages API  /  OpenAI-compatible endpoint
            └─→ Godot scene tools (godot_tools.py → HTTP :7778 → editor)
```

## Features

- **Persistent Conversations**: One Claude session per Godot connection, maintains context across messages
- **MCP Tools**: Claude can read/write Godot scenes using 7 tools:
  - `get_node_property` - Read node properties
  - `set_node_property` - Modify properties (undoable)
  - `get_scene_state` - Snapshot entire scene tree
  - `get_editor_context` - Get current scene/selection
  - `create_node` - Create new nodes (undoable)
  - `delete_node` - Delete nodes (undoable)
  - `reparent_node` - Move nodes (undoable)
- **Real-time Streaming**: Responses stream back to Godot as Claude generates them
- **Context Injection**: Scene path and selected nodes automatically included in prompts

## Setup

### 1. Install Dependencies

```bash
cd addons/claudot/bridge
pip install -r requirements.txt
```

This installs:
- `claude-agent-sdk` - Claude Agent SDK for Python
- `fastmcp` - MCP server framework (used by Agent SDK)

### 2. Configure Provider & API Key

No manual configuration is required for the default `claude-code` backend —
it uses Claude Code's own OAuth login (`~/.claude`).

For bring-your-own-key providers, use **Settings** in the Godot chat panel.
The plugin stores keys in Godot's editor settings (outside the project) and
pushes the configuration to the bridge over loopback TCP via `chat/configure`
each time it connects. Keys are never written to the project directory and
never passed on the command line.

Get an Anthropic key from https://console.anthropic.com/ — required for the
`anthropic` backend, optional for `claude-code` (and will be required for
Claude Fable 5 once it moves to API-key-only access).

## Usage

### Start the Bridge

**New Agent SDK Bridge (recommended):**

```bash
python addons/claudot/bridge/agent_bridge.py --port 7777 --log-level DEBUG
```

Options:
- `--host` - TCP server host (default: 127.0.0.1)
- `--port` - TCP server port (default: 7777)
- `--model` - Default Claude model (default: claude-opus-4-8; overridden at runtime by `chat/configure`)
- `--log-level` - Logging level (DEBUG, INFO, WARNING, ERROR)

**Legacy MCP Bridge (tools only, no chat):**

```bash
python addons/claudot/bridge/main.py --mode mcp --port 7777 --log-level DEBUG
```

### Connect from Godot

1. Open your project in Godot 4.x
2. Enable the Claudot plugin (Project > Project Settings > Plugins)
3. Open the chat panel (right dock)
4. Click "Connect"
5. Start chatting!

### Example Conversations

```
You: What scene am I working on?

Claude: You're currently working on "res://scenes/main_menu.tscn" with a
Control node as the root. I can see you have a Button selected at path
"/root/Control/StartButton".

You: Change the button text to "Begin Adventure"

Claude: I'll set that property for you now.
[Uses set_node_property MCP tool]
Done! The button text is now "Begin Adventure". You can undo this with Ctrl+Z
if needed.
```

## Architecture Comparison

### Agent SDK Bridge (agent_bridge.py)

**Use for:** Chat with Claude from Godot

✅ Persistent conversation (maintains context)
✅ Programmatic control (no subprocess spawning)
✅ Real-time streaming responses
✅ MCP tools work seamlessly
✅ Full Claude Code capabilities
✅ Context injection (scene/selection)

**Connection:** Godot → TCP → Agent SDK → Claude API

### Legacy MCP Bridge (main.py --mode mcp)

**Use for:** MCP tools only (no chat)

✅ MCP tools exposed
❌ No chat (messages just logged)
❌ Requires separate Claude Code process
⚠️  Tools work, but conversation doesn't

**Connection:** Claude Code (MCP client) → Bridge (MCP server) → Godot

## Development

### File Structure

```
addons/claudot/bridge/
├── agent_bridge.py          # Chat bridge: TCP server, backend dispatch, Agent SDK path
├── providers.py             # Direct-API providers (Anthropic Messages API, OpenAI-compatible)
├── godot_tools.py           # Godot tool schemas + executor for direct providers
├── godot_mcp_server.py      # Standalone MCP server for Claude Code CLI
├── godot_docs.py            # Godot class-reference search/cache
├── test_providers_smoke.py  # Provider smoke tests (uv run test_providers_smoke.py)
├── test_bridge_smoke.py     # Bridge TCP integration test (uv run test_bridge_smoke.py)
├── main.py                  # Legacy bridge (MCP + subprocess modes)
├── mcp_server.py            # Legacy MCP server with tool definitions
├── tcp_server.py            # Legacy TCP server helper
├── claude_subprocess.py     # Legacy Claude CLI subprocess management
├── requirements.txt         # Python dependencies
└── README.md                # This file
```

### Protocol

**Configuration (Godot → Bridge, sent on connect and on settings change):**

```json
{
  "jsonrpc": "2.0",
  "method": "chat/configure",
  "params": {
    "provider": "anthropic",
    "model": "claude-fable-5",
    "api_key": "sk-ant-...",
    "base_url": ""
  }
}
```

The bridge acknowledges with `chat/configured` `{provider, model}`. Changing
provider, model, key, or base URL tears down the active session; the next
message starts a fresh one. API keys are redacted from bridge logs and the
Godot console tab.

**Other bridge → Godot messages:**

- `chat/refusal` `{category, message}` — the model's safety classifiers
  declined the request (Claude Fable 5). The final `chat/response` still
  follows to reset the UI state.
- `chat/error` `{error}` — provider/backend failure (bad key, unreachable
  endpoint, rate limit). Clears the working indicator.

**Godot → Bridge (TCP, newline-delimited JSON-RPC):**

```json
{
  "jsonrpc": "2.0",
  "method": "chat/send",
  "params": {
    "content": "User message here",
    "context": {
      "scene_path": "res://main.tscn",
      "selected_nodes": [...]
    }
  }
}
```

**Bridge → Godot (TCP, newline-delimited JSON-RPC):**

```json
{
  "jsonrpc": "2.0",
  "method": "chat/response",
  "params": {
    "content": "Claude's response here",
    "cost_usd": 0.0234,
    "duration_ms": 1523,
    "num_turns": 2
  }
}
```

**MCP Tool Calls (Bridge → Godot):**

```json
{
  "jsonrpc": "2.0",
  "method": "mcp/get_scene_state",
  "params": {"max_depth": 5},
  "id": 1234567890
}
```

**MCP Tool Responses (Godot → Bridge):**

```json
{
  "jsonrpc": "2.0",
  "id": 1234567890,
  "success": true,
  "scene_path": "res://main.tscn",
  "node_count": 42,
  "tree": {...},
  "timestamp": 1234567890.123
}
```

## Troubleshooting

### "Error: Not connected to Godot"

Claude tried to use an MCP tool but bridge isn't connected to Godot.
- Check that Godot plugin is enabled
- Click "Connect" in Godot chat panel
- Verify bridge is running on same port (7777)

### "ModuleNotFoundError: No module named 'claude_agent_sdk'"

Agent SDK not installed.
```bash
pip install claude-agent-sdk
```

### "ANTHROPIC_API_KEY not found"

API key not configured.
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

### Chat messages not appearing

Using wrong bridge mode.
- For chat: Use `agent_bridge.py` (Agent SDK)
- For MCP only: Use `main.py --mode mcp`

### Tool calls timing out

Godot MCP handler not responding.
- Check Godot console for errors
- Verify `addons/claudot/mcp/mcp_handler.gd` is loaded
- Check that plugin is in editor mode (@tool annotations present)

## Packaging / Distribution

When creating a distribution zip, include **all** files under `addons/claudot/` except `__pycache__/` directories and `.env`. Critically, this includes `.gd.uid` files — Godot 4.x uses these for resource UID tracking. Without them, `preload()` calls in the plugin script fail to resolve and the plugin won't appear in Project Settings > Plugins.

**Exclude:**
- `bridge/__pycache__/` — Python bytecode cache
- `bridge/.env` — user-specific API key

**Must include:**
- All `.gd.uid` files (one per `.gd` script)
- `ui/chat_panel.tscn`
- `plugin.cfg`

## License

See main project LICENSE file.

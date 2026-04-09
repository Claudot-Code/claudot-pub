# Claudot

A Godot 4.x editor plugin that connects Claude Code to your game engine -- chat, scene tools, and autonomous testing from inside the editor.

Claudot gives Claude Code a live window into your Godot project: a dockable chat panel in the editor, 15 MCP tools for reading and modifying your scene tree, and the ability to run GDScript tests autonomously so Claude can verify its own work without leaving your workflow.

## Prerequisites

| Requirement | Purpose | Install |
|------------|---------|---------|
| **Godot 4.x** | The editor this plugin extends | (already installed) |
| **Claude Code** | AI chat and MCP tool communication | [claude.ai/code](https://claude.ai/code) |
| **uv** | Runs the Python bridge daemon | [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/) |
| **Git for Windows** (Windows only) | Required by Claude Code on Windows | [git-scm.com/download/win](https://git-scm.com/download/win) |

**uv on Windows:** `winget install --id=astral-sh.uv -e` also works.

After installing Claude Code, run `claude` once and complete the browser authentication to authorize your account. This sets up the OAuth token that Claudot uses -- no API key configuration needed.

## Installation

1. **Download** the latest release zip from the [GitHub Releases](../../releases) page.

2. **Copy two directories** into your Godot project root:
   - `addons/claudot/` → `YourProject/addons/claudot/`
   - `bridge/` → `YourProject/bridge/`

3. **Enable the plugin** in Godot: Project → Project Settings → Plugins → enable **Claudot**.

That's it. No terminal commands, no pip install, no manual configuration.

## First Use

When you enable the plugin, the following happens automatically:

1. The Python bridge daemon launches in the background (via uv)
2. `.mcp.json` is created at your project root (MCP server config for Claude Code)
3. `CLAUDE.md` is created at your project root (context file for Claude Code -- skip-if-exists)
4. `.gitignore` is updated to exclude generated files

A one-time notice appears in the Claudot panel confirming `.mcp.json` was written and telling you which directory to run `claude` from.

### Using Claude Code (recommended)

Claude Code is the primary workflow. Claude can call all 15 MCP tools to inspect and modify your project.

1. Open a terminal in your Godot project directory
2. Run `claude`
3. Claude Code discovers the MCP tools automatically via `.mcp.json`
4. Ask Claude to read your scene tree, create nodes, set properties, run tests, capture screenshots, and more

### Using the Claudot Panel

The in-editor chat panel is available for quick questions without leaving the editor.

1. Open the **Claudot** tab at the bottom of the Godot editor (next to Output and Debugger)
2. Click **Connect** to connect to the bridge daemon
3. Type a message and press Enter or click Send

## How It Works

Claudot has three layers. The Godot plugin runs an HTTP server (port 7778) that exposes your scene tree as callable tools. A standalone MCP server (Python) translates between Claude Code's MCP protocol and those HTTP endpoints. Claude Code discovers the MCP server via the `.mcp.json` config file and calls tools through it. The bridge daemon also handles chat relay for the in-editor panel via a separate TCP connection.

## MCP Tools

All 15 tools are available when running `claude` from your project directory.

| Tool | Description |
|------|-------------|
| `get_scene_state` | Read the current scene tree structure |
| `get_node_property` | Get a property value from any node |
| `set_node_property` | Set a property value on any node |
| `get_editor_context` | Get active scene and selected nodes |
| `create_node` | Add a new node to the scene |
| `delete_node` | Remove a node from the scene |
| `reparent_node` | Move a node to a new parent |
| `search_files` | Search project files by pattern or extension |
| `capture_screenshot` | Capture editor or game viewport screenshot |
| `get_debugger_output` | Read `print()` output from game execution |
| `get_debugger_errors` | Read error output from game execution |
| `get_node_script` | Read GDScript source code from a node |
| `run_tests` | Run GDScript tests (GUT framework) |
| `request_user_input` | Ask the developer a question from within Claude's workflow |
| `get_pending_input_answer` | Retrieve the developer's answer |

## Troubleshooting

**"claude" not found**
Claude Code is not installed or not in PATH. Install from [claude.ai/code](https://claude.ai/code) and restart your terminal.

**"uv" not found**
uv is not installed or not in PATH. Install from [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/) and restart Godot.

**Bridge won't start**
The bridge launches automatically on plugin enable. Check the Godot Output panel for error messages -- it will tell you if uv (or Python) is missing from PATH.

**MCP tools not discovered by Claude Code**
Make sure `.mcp.json` exists in your project root -- it is auto-generated when the plugin enables. If it is missing, disable and re-enable the Claudot plugin. Make sure you run `claude` from the same directory that contains `.mcp.json`.

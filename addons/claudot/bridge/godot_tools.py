"""
Godot tool definitions and executor for direct-API chat providers.

The Claude Code path gets Godot tools through MCP (godot_mcp_server.py).
Direct-API providers (Anthropic Messages API, OpenAI-compatible) run their own
tool loop inside the bridge process instead, so this module provides:

- TOOL_DEFS: provider-neutral tool schemas (name, description, JSON Schema input)
- as_anthropic_tools() / as_openai_tools(): schema converters per wire format
- execute_tool(): dispatches a call either to the Godot HTTP bridge
  (http://127.0.0.1:7778/mcp/invoke — same endpoint godot_mcp_server.py uses)
  or to the local godot_docs module for documentation lookups.

capture_screenshot is intentionally excluded: image tool results are not
portable across OpenAI-compatible providers.
"""

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

GODOT_BRIDGE_URL = os.environ.get("GODOT_BRIDGE_URL", "http://127.0.0.1:7778")

# Tools that block on user interaction need a longer HTTP timeout
_LONG_TIMEOUT_TOOLS = {"request_user_input", "run_tests"}
_DEFAULT_TIMEOUT = 15.0
_LONG_TIMEOUT = 120.0

# Tools handled in-process via godot_docs instead of the HTTP bridge
_DOCS_TOOLS = {"godot_search_docs", "godot_get_class_docs"}


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


TOOL_DEFS: list[dict] = [
    {
        "name": "get_editor_context",
        "description": (
            "Get the current Godot editor context: active scene path and selected nodes. "
            "Call this FIRST before any scene work to orient yourself."
        ),
        "input_schema": _schema({}),
    },
    {
        "name": "get_scene_state",
        "description": (
            "Read the full scene tree of the currently open Godot scene, including node "
            "names, types, and paths. Use before modifying nodes."
        ),
        "input_schema": _schema({
            "max_depth": {"type": "integer", "description": "Maximum tree depth to return (default 5)"},
        }),
    },
    {
        "name": "get_node_property",
        "description": "Read a property value from a node in the open scene.",
        "input_schema": _schema({
            "node_path": {"type": "string", "description": "Node path, e.g. /root/Main/Player"},
            "property_name": {"type": "string", "description": "Property name, e.g. position"},
        }, ["node_path", "property_name"]),
    },
    {
        "name": "set_node_property",
        "description": (
            "Set a property on a node in the open scene. Undoable with Ctrl+Z. "
            "Read the current value first with get_node_property or get_scene_state."
        ),
        "input_schema": _schema({
            "node_path": {"type": "string", "description": "Node path, e.g. /root/Main/Player"},
            "property_name": {"type": "string", "description": "Property name, e.g. text"},
            "value": {"type": "string", "description": "New value as a string, e.g. \"Begin Adventure\" or \"Vector2(100, 50)\""},
        }, ["node_path", "property_name", "value"]),
    },
    {
        "name": "create_node",
        "description": "Add a new node to the open scene tree. Undoable.",
        "input_schema": _schema({
            "parent_path": {"type": "string", "description": "Path of the parent node, e.g. /root/Main"},
            "node_type": {"type": "string", "description": "Godot class name, e.g. Sprite2D"},
            "node_name": {"type": "string", "description": "Name for the new node"},
        }, ["parent_path", "node_type", "node_name"]),
    },
    {
        "name": "delete_node",
        "description": "Remove a node from the open scene. Undoable.",
        "input_schema": _schema({
            "node_path": {"type": "string", "description": "Path of the node to delete"},
        }, ["node_path"]),
    },
    {
        "name": "reparent_node",
        "description": "Move a node to a different parent in the open scene. Undoable.",
        "input_schema": _schema({
            "node_path": {"type": "string", "description": "Path of the node to move"},
            "new_parent_path": {"type": "string", "description": "Path of the new parent node"},
        }, ["node_path", "new_parent_path"]),
    },
    {
        "name": "search_files",
        "description": (
            "Search Godot project files by wildcard pattern and/or extension. Returns res:// "
            "paths. Use this to find .gd scripts and scenes — never guess file paths."
        ),
        "input_schema": _schema({
            "pattern": {"type": "string", "description": "Wildcard filename pattern, e.g. player* (case-insensitive)"},
            "extensions": {"type": "array", "items": {"type": "string"}, "description": "Extensions to filter, e.g. [\".gd\", \".tscn\"]"},
            "max_results": {"type": "integer", "description": "Maximum files to return (default 100)"},
        }),
    },
    {
        "name": "get_node_script",
        "description": "Read the GDScript source attached to a node in the open scene.",
        "input_schema": _schema({
            "node_path": {"type": "string", "description": "Path of the node whose script to read"},
        }, ["node_path"]),
    },
    {
        "name": "run_scene",
        "description": "Launch the game (like pressing F5). Optionally run a specific scene.",
        "input_schema": _schema({
            "scene_path": {"type": "string", "description": "Optional res:// scene path; default is the main scene"},
        }),
    },
    {
        "name": "stop_scene",
        "description": "Stop the running game (like pressing F8).",
        "input_schema": _schema({}),
    },
    {
        "name": "get_debugger_output",
        "description": "Read print() output from the running or last-run game.",
        "input_schema": _schema({
            "max_lines": {"type": "integer", "description": "Maximum lines to return (default 100)"},
        }),
    },
    {
        "name": "get_debugger_errors",
        "description": "Read error output from the running or last-run game. Check after code or scene changes.",
        "input_schema": _schema({
            "max_lines": {"type": "integer", "description": "Maximum lines to return (default 100)"},
        }),
    },
    {
        "name": "run_tests",
        "description": "Run GDScript tests via the GUT framework and return results.",
        "input_schema": _schema({
            "test_directory": {"type": "string", "description": "Test directory (default test/unit)"},
            "test_file": {"type": "string", "description": "Optional: run a single test file"},
            "test_name": {"type": "string", "description": "Optional: run a single test method"},
        }),
    },
    {
        "name": "godot_search_docs",
        "description": (
            "Search the Godot 4 class reference for accurate method signatures, property "
            "names, and signal names. Use BEFORE writing GDScript involving a built-in "
            "class you are not certain about — prevents hallucinated API calls."
        ),
        "input_schema": _schema({
            "query": {"type": "string", "description": "Class, method, property, or keyword, e.g. move_and_slide"},
            "kind": {"type": "string", "description": "Optional filter: class, method, property, signal, constant"},
        }, ["query"]),
    },
    {
        "name": "godot_get_class_docs",
        "description": (
            "Get complete documentation for a Godot 4 class: inheritance, methods with full "
            "signatures, properties, signals, constants. Use when you need exact signatures."
        ),
        "input_schema": _schema({
            "class_name": {"type": "string", "description": "Godot class name, e.g. CharacterBody2D"},
            "section": {"type": "string", "description": "all, methods, properties, signals, constants, or description"},
        }, ["class_name"]),
    },
]

TOOL_NAMES = {t["name"] for t in TOOL_DEFS}


def as_anthropic_tools() -> list[dict]:
    """Tool list in Anthropic Messages API format."""
    return [
        {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
        for t in TOOL_DEFS
    ]


def as_openai_tools() -> list[dict]:
    """Tool list in OpenAI chat-completions function format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in TOOL_DEFS
    ]


async def execute_tool(tool_name: str, tool_args: dict) -> str:
    """
    Execute a Godot tool call and return the result as a string.

    Documentation tools run in-process via godot_docs; everything else is
    forwarded to the Godot editor's HTTP bridge. Errors are returned as
    strings (prefixed with "ERROR:") rather than raised, so the model can
    read them and adapt.
    """
    if tool_name not in TOOL_NAMES:
        return f"ERROR: Unknown tool '{tool_name}'."

    if tool_name in _DOCS_TOOLS:
        return await _execute_docs_tool(tool_name, tool_args)

    timeout = _LONG_TIMEOUT if tool_name in _LONG_TIMEOUT_TOOLS else _DEFAULT_TIMEOUT
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{GODOT_BRIDGE_URL}/mcp/invoke",
                json={"tool_name": tool_name, "tool_args": tool_args},
            )
            response.raise_for_status()
            result = response.json()

        if result.get("is_error", False):
            return f"ERROR: Tool '{tool_name}' failed: {result.get('tool_call_result', 'Unknown error')}"

        tool_result = result.get("tool_call_result", "{}")
        if isinstance(tool_result, str):
            return tool_result
        return json.dumps(tool_result)

    except httpx.ConnectError:
        return "ERROR: Godot bridge not running. The Godot editor must be open with the Claudot plugin enabled."
    except httpx.TimeoutException:
        return f"ERROR: Godot bridge timed out after {timeout:.0f}s. Check that the Godot editor is responsive."
    except httpx.HTTPStatusError as e:
        return f"ERROR: Godot bridge HTTP error {e.response.status_code}: {e.response.text}"
    except Exception as e:
        return f"ERROR: Godot bridge communication error: {e}"


async def _execute_docs_tool(tool_name: str, tool_args: dict) -> str:
    """Run godot_search_docs / godot_get_class_docs via the local godot_docs module."""
    try:
        from godot_docs import search_docs, get_class_docs
    except ImportError:
        return "ERROR: godot_docs module not available."

    try:
        if tool_name == "godot_search_docs":
            query = tool_args.get("query", "")
            kind = tool_args.get("kind", "")
            results = await search_docs(query, kind)
            if not results:
                return f"No results found for '{query}'" + (f" (kind={kind})" if kind else "")
            if len(results) == 1 and "error" in results[0]:
                return f"ERROR: {results[0]['error']}"
            lines = [f"Search results for '{query}':"]
            for entry in results:
                kind_str = entry.get("kind", "")
                class_name = entry.get("class_name", "")
                name = entry.get("name", "")
                brief = entry.get("brief", "")
                symbol = class_name if kind_str == "class" else f"{class_name}.{name}"
                line = f"  [{kind_str}] {symbol}"
                if brief and brief != "(not yet fetched)":
                    line += f" — {brief}"
                lines.append(line)
            return "\n".join(lines)

        # godot_get_class_docs
        class_name = tool_args.get("class_name", "")
        section = tool_args.get("section", "all") or "all"
        data = await get_class_docs(class_name, section)
        if "error" in data:
            return f"ERROR: {data['error']}"
        return json.dumps(data, indent=2)

    except Exception as e:
        return f"ERROR: Documentation lookup failed: {e}"

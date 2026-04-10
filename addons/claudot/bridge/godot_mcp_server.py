#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "fastmcp>=3.0.0",
#   "httpx>=0.27.0",
# ]
# ///

"""
Godot MCP Server - Standalone FastMCP server for Claude Code integration.

Provides 20 MCP tools for Godot scene manipulation, test execution, and docs lookup:
- Scene state inspection (get_scene_state, get_editor_context)
- Node property access (get_node_property, set_node_property)
- Node tree manipulation (create_node, delete_node, reparent_node)
- File search (search_files)
- Screenshot capture (capture_screenshot)
- Debugger output (get_debugger_output)
- Debugger errors (get_debugger_errors)
- Script reading (get_node_script)
- Test execution (run_tests - fully functional, runs godot --headless)
- Interactive input (request_user_input, get_pending_input_answer)
- Game control (run_scene, stop_scene)
- Godot 4 class reference (godot_search_docs, godot_get_class_docs, godot_refresh_docs)

Architecture:
- Standalone MCP server with stdio transport (discoverable by Claude Code)
- Scene tools (1-12) communicate via HTTP bridge using /mcp/invoke endpoint
- Test tool (13) executes directly via subprocess (no bridge needed)
- Docs tools (18-20) fetch from GitHub and cache locally (no bridge needed)
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# CRITICAL: Configure logging to stderr BEFORE any other imports
# stdout is reserved exclusively for MCP JSON-RPC protocol
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stderr
)

logger = logging.getLogger(__name__)

# Now import FastMCP after logging is configured
try:
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError
    from fastmcp.utilities.types import Image
    import httpx
except ImportError as e:
    logger.error(f"Failed to import dependencies: {e}")
    logger.error("Install with: pip install fastmcp>=3.0.0 httpx>=0.27.0")
    sys.exit(1)

# Godot docs module (same directory — no bridge dependency)
try:
    from godot_docs import search_docs, get_class_docs, refresh_docs
except ImportError as e:
    logger.warning(f"godot_docs module not available: {e}")
    search_docs = None
    get_class_docs = None
    refresh_docs = None

# Initialize FastMCP server
mcp = FastMCP("godot")


async def _forward_to_godot(tool_name: str, params: dict) -> dict:
    """
    Forward MCP tool call to Godot HTTP bridge.

    Posts to /mcp/invoke with {"tool_name": name, "tool_args": args} body.
    Parses response format: {"is_error": bool, "tool_call_result": str}

    Args:
        tool_name: MCP tool name (e.g., "get_node_property")
        params: Tool parameters dict

    Returns:
        Parsed tool result dict

    Raises:
        ToolError: If bridge is not running, times out, or returns error
    """
    bridge_url = os.environ.get("GODOT_BRIDGE_URL", "http://127.0.0.1:7778")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{bridge_url}/mcp/invoke",
                json={
                    "tool_name": tool_name,
                    "tool_args": params
                }
            )
            response.raise_for_status()
            result = response.json()

            # Parse GDAI response format
            if result.get("is_error", False):
                raise ToolError(
                    f"Tool '{tool_name}' failed: {result.get('tool_call_result', 'Unknown error')}"
                )

            # Parse the tool_call_result (it's a JSON string from Godot)
            tool_result = result.get("tool_call_result", "{}")
            if isinstance(tool_result, str):
                try:
                    return json.loads(tool_result)
                except json.JSONDecodeError:
                    return {"result": tool_result}
            return tool_result

    except httpx.ConnectError:
        raise ToolError(
            "Godot bridge not running. Start Godot editor with Claudot plugin enabled."
        )
    except httpx.TimeoutException:
        raise ToolError(
            "Godot bridge timeout. Check if Godot editor is responsive."
        )
    except httpx.HTTPStatusError as e:
        raise ToolError(
            f"Godot bridge HTTP error {e.response.status_code}: {e.response.text}"
        )
    except ToolError:
        raise  # Re-raise ToolErrors from GDAI response parsing
    except Exception as e:
        raise ToolError(f"Godot bridge communication error: {str(e)}")


async def _forward_to_godot_long(tool_name: str, params: dict) -> dict:
    """
    Forward MCP tool call to Godot HTTP bridge with extended 120-second timeout.

    Identical to _forward_to_godot() but uses timeout=120.0 instead of 10.0.
    Used for tools that block waiting for user interaction (e.g. request_user_input).

    Args:
        tool_name: MCP tool name (e.g., "request_user_input")
        params: Tool parameters dict

    Returns:
        Parsed tool result dict

    Raises:
        ToolError: If bridge is not running, times out after 120s, or returns error
    """
    bridge_url = os.environ.get("GODOT_BRIDGE_URL", "http://127.0.0.1:7778")

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{bridge_url}/mcp/invoke",
                json={
                    "tool_name": tool_name,
                    "tool_args": params
                }
            )
            response.raise_for_status()
            result = response.json()

            # Parse GDAI response format
            if result.get("is_error", False):
                raise ToolError(
                    f"Tool '{tool_name}' failed: {result.get('tool_call_result', 'Unknown error')}"
                )

            # Parse the tool_call_result (it's a JSON string from Godot)
            tool_result = result.get("tool_call_result", "{}")
            if isinstance(tool_result, str):
                try:
                    return json.loads(tool_result)
                except json.JSONDecodeError:
                    return {"result": tool_result}
            return tool_result

    except httpx.ConnectError:
        raise ToolError(
            "Godot bridge not running. Start Godot editor with Claudot plugin enabled."
        )
    except httpx.TimeoutException:
        raise ToolError(
            "Godot bridge timeout after 120 seconds. Developer did not respond in time. "
            "Call get_pending_input_answer() to retrieve the answer if the developer responded after the timeout."
        )
    except httpx.HTTPStatusError as e:
        raise ToolError(
            f"Godot bridge HTTP error {e.response.status_code}: {e.response.text}"
        )
    except ToolError:
        raise  # Re-raise ToolErrors from GDAI response parsing
    except Exception as e:
        raise ToolError(f"Godot bridge communication error: {str(e)}")


# Tool 1: Get scene state
@mcp.tool()
async def get_scene_state(max_depth: int = 5) -> dict:
    """
    Get a snapshot of the Godot scene tree structure.

    Returns hierarchical representation of all nodes in the current scene,
    including node types, paths, and key properties.

    Args:
        max_depth: Maximum tree depth to traverse (default: 5)

    Returns:
        Scene tree snapshot with node hierarchy
    """
    logger.info(f"get_scene_state called with max_depth={max_depth}")
    result = await _forward_to_godot("get_scene_state", {"max_depth": max_depth})
    return result


# Tool 2: Get node property
@mcp.tool()
async def get_node_property(node_path: str, property_name: str) -> dict:
    """
    Read a property value from a node in the Godot scene tree.

    Args:
        node_path: Path to node (e.g., '/root/Player' or 'Player')
        property_name: Property name (e.g., 'position', 'visible', 'modulate')

    Returns:
        Property value with type information
    """
    logger.info(f"get_node_property called: {node_path}.{property_name}")
    result = await _forward_to_godot("get_node_property", {
        "node_path": node_path,
        "property_name": property_name
    })
    return result


# Tool 3: Set node property
@mcp.tool()
async def set_node_property(node_path: str, property_name: str, value: str) -> dict:
    """
    Set a property value on a node in the Godot scene tree.

    This operation is undoable with Ctrl+Z in the Godot editor.

    Args:
        node_path: Path to node (e.g., '/root/Player')
        property_name: Property name (e.g., 'position', 'visible')
        value: New value (will be converted to appropriate Godot type)

    Returns:
        Confirmation with old and new values
    """
    logger.info(f"set_node_property called: {node_path}.{property_name} = {value}")
    result = await _forward_to_godot("set_node_property", {
        "node_path": node_path,
        "property_name": property_name,
        "value": value
    })
    return result


# Tool 4: Get editor context
@mcp.tool()
async def get_editor_context() -> dict:
    """
    Get current Godot editor context.

    Returns information about the active scene, selected nodes, and editor state.
    Useful for understanding what the user is currently working on.

    Returns:
        Editor context including scene path, root node, selected nodes
    """
    logger.info("get_editor_context called")
    result = await _forward_to_godot("get_editor_context", {})
    return result


# Tool 5: Create node
@mcp.tool()
async def create_node(parent_path: str, node_type: str, node_name: str) -> dict:
    """
    Create a new node in the Godot scene tree.

    This operation is undoable with Ctrl+Z in the Godot editor.

    Args:
        parent_path: Path to parent node (e.g., '/root' or 'Player')
        node_type: Node type (e.g., 'Node2D', 'Sprite2D', 'Area2D', 'RigidBody2D')
        node_name: Name for the new node

    Returns:
        Created node path and confirmation
    """
    logger.info(f"create_node called: {node_type} '{node_name}' under {parent_path}")
    result = await _forward_to_godot("create_node", {
        "parent_path": parent_path,
        "node_type": node_type,
        "node_name": node_name
    })
    return result


# Tool 6: Delete node
@mcp.tool()
async def delete_node(node_path: str) -> dict:
    """
    Delete a node from the Godot scene tree.

    This operation is undoable with Ctrl+Z in the Godot editor.

    Args:
        node_path: Path to node to delete (e.g., '/root/Player/Enemy')

    Returns:
        Confirmation of deletion
    """
    logger.info(f"delete_node called: {node_path}")
    result = await _forward_to_godot("delete_node", {
        "node_path": node_path
    })
    return result


# Tool 7: Reparent node
@mcp.tool()
async def reparent_node(node_path: str, new_parent_path: str) -> dict:
    """
    Move a node to a different parent in the Godot scene tree.

    This operation is undoable with Ctrl+Z in the Godot editor.

    Args:
        node_path: Path to node to move (e.g., '/root/Player/Weapon')
        new_parent_path: Path to new parent node (e.g., '/root/Items')

    Returns:
        New path and confirmation
    """
    logger.info(f"reparent_node called: {node_path} -> {new_parent_path}")
    result = await _forward_to_godot("reparent_node", {
        "node_path": node_path,
        "new_parent_path": new_parent_path
    })
    return result


# Tool 8: Search files
@mcp.tool()
async def search_files(
    pattern: str = "",
    extensions: list[str] | None = None,
    max_results: int = 100
) -> dict:
    """
    Search Godot project files by pattern and extension.

    Searches the res:// filesystem recursively for files matching the given
    pattern and/or extensions. Returns res:// paths usable with other tools.

    Args:
        pattern: Wildcard pattern to match filenames (e.g., "player*", "test_*", "*.gd")
                 Uses wildcards: * (any characters), ? (one character)
                 Case-insensitive. Empty string matches all files.
        extensions: List of extensions to filter (e.g., [".gd", ".tscn", ".png"])
                    Can be specified with or without leading dot.
                    Empty list matches all extensions.
        max_results: Maximum files to return (default: 100, max: 1000)
                     Prevents token overflow in large projects.

    Returns:
        {
            "files": [
                {
                    "path": "res://scripts/player.gd",
                    "type": "GDScript",
                    "name": "player.gd",
                    "directory": "res://scripts"
                },
                ...
            ],
            "count": 42,
            "truncated": false
        }

    Examples:
        # Find all GDScript files
        search_files(extensions=[".gd"])

        # Find player-related files
        search_files(pattern="player*")

        # Find test scenes
        search_files(pattern="test_*", extensions=[".tscn"])

        # Find all scripts (limit results)
        search_files(pattern="*.gd", max_results=50)
    """
    logger.info(f"search_files called: pattern={pattern}, extensions={extensions}, max={max_results}")

    # Forward to Godot HTTP bridge
    result = await _forward_to_godot("search_files", {
        "pattern": pattern,
        "extensions": extensions or [],
        "max_results": max_results
    })

    return result


# Tool 9: Capture screenshot
@mcp.tool()
async def capture_screenshot(viewport_type: str = "2d_editor") -> Image:
    """
    Capture screenshot of Godot editor or game viewport.

    Returns visual snapshot of the specified viewport for inspecting scene
    layout, UI elements, node positions, and visual state. Screenshots are
    automatically sized to 800x600 JPEG for token efficiency.

    Args:
        viewport_type: Which viewport to capture:
            - "2d_editor": 2D editor viewport (default)
            - "3d_editor": 3D editor viewport (camera view)
            - "game": Running game viewport (requires game to be running - press F5)

    Returns:
        Screenshot image rendered inline in conversation (800x600 JPEG)

    Examples:
        # Capture 2D editor to see node layout
        capture_screenshot(viewport_type="2d_editor")

        # Capture 3D viewport to see camera angle
        capture_screenshot(viewport_type="3d_editor")

        # Capture running game (only works when game is playing)
        capture_screenshot(viewport_type="game")
    """
    logger.info(f"capture_screenshot called: viewport_type={viewport_type}")

    # Validate viewport type
    valid_types = ["2d_editor", "3d_editor", "game"]
    if viewport_type not in valid_types:
        raise ToolError(f"Invalid viewport_type '{viewport_type}'. Must be one of: {valid_types}")

    # Forward to Godot HTTP bridge
    result = await _forward_to_godot("capture_screenshot", {
        "viewport_type": viewport_type
    })

    # Extract base64 image data
    image_data_base64 = result.get("image_data", "")
    if not image_data_base64:
        raise ToolError("No image data returned from Godot")

    # Decode base64 to bytes
    import base64
    image_bytes = base64.b64decode(image_data_base64)

    # Log metadata for debugging
    logger.info(f"Screenshot captured: {result.get('encoded_size', {})}, format: {result.get('format', 'unknown')}")

    # Return FastMCP Image helper (auto-converts to MCP ImageContent)
    return Image(data=image_bytes, format="jpeg")


# Tool 10: Get debugger output
@mcp.tool()
async def get_debugger_output(max_lines: int = 100) -> dict:
    """
    Retrieve recent print() output captured during game/test execution.

    Returns messages from the debugger output ring buffer. Use this after
    run_tests to see what the game printed during test execution, helping
    debug test failures and verify behavior.

    The buffer stores the most recent 1000 messages. Messages are captured
    when game code calls OutputCapture.capture_print() instead of print().

    Args:
        max_lines: Maximum messages to return (default: 100, max: 1000).
                   Returns the most recent messages first.

    Returns:
        {
            "messages": [
                {"timestamp": 1709312345.67, "text": "Player spawned at (0, 0)"},
                {"timestamp": 1709312345.89, "text": "Health set to 100"}
            ],
            "count": 2,
            "buffer_total": 2
        }

    Examples:
        # Get last 100 messages (default)
        get_debugger_output()

        # Get last 10 messages
        get_debugger_output(max_lines=10)

        # Typical workflow: run tests then check output
        # 1. run_tests(test_file="test_player.gd")
        # 2. get_debugger_output(max_lines=50)
    """
    logger.info(f"get_debugger_output called: max_lines={max_lines}")

    result = await _forward_to_godot("get_debugger_output", {
        "max_lines": max_lines
    })

    return result


# Tool 11: Get debugger errors
@mcp.tool()
async def get_debugger_errors(max_lines: int = 100) -> dict:
    """
    Retrieve recent errors captured during game/test execution.

    Returns messages from the debugger error ring buffer. Use this after
    run_tests to see errors explicitly captured via OutputCapture.capture_error(),
    separate from print() output. Errors and print output are kept in distinct
    buffers so each signal remains focused.

    The buffer stores the most recent 1000 errors. Errors are captured
    when game code calls OutputCapture.capture_error() instead of push_error().

    Args:
        max_lines: Maximum errors to return (default: 100, max: 1000).
                   Returns the most recent errors first.

    Returns:
        {
            "errors": [
                {"timestamp": 1709312345.67, "text": "Player health cannot be negative"},
                {"timestamp": 1709312345.89, "text": "Failed to load resource: res://missing.tres"}
            ],
            "count": 2,
            "buffer_total": 2
        }

    Examples:
        # Get last 100 errors (default)
        get_debugger_errors()

        # Get last 10 errors
        get_debugger_errors(max_lines=10)

        # Typical workflow: run tests then check errors
        # 1. run_tests(test_file="test_player.gd")
        # 2. get_debugger_errors(max_lines=50)
    """
    logger.info(f"get_debugger_errors called: max_lines={max_lines}")

    result = await _forward_to_godot("get_debugger_errors", {
        "max_lines": max_lines
    })

    return result


# Tool 12: Get node script
@mcp.tool()
async def get_node_script(node_path: str) -> dict:
    """
    Read GDScript source code attached to a node in the scene tree.

    Returns the full script source code and file path for the given node.
    Useful for understanding node behavior, reviewing code, and knowing
    which file to edit. Handles both external .gd files and built-in
    scripts embedded in .tscn files.

    Args:
        node_path: Path to node (e.g., '/root/Player' or 'Player')

    Returns:
        {
            "has_script": true,
            "script_path": "res://scripts/player.gd",
            "is_built_in": false,
            "source_code": "extends CharacterBody2D\\n...",
            "line_count": 42
        }

        If no script attached: {"has_script": false}
        If C# script: raises ToolError

    Examples:
        # Read the player's script
        get_node_script(node_path="/root/Player")

        # Check if a UI element has a script
        get_node_script(node_path="/root/UI/HealthBar")
    """
    logger.info(f"get_node_script called: {node_path}")
    result = await _forward_to_godot("get_node_script", {
        "node_path": node_path
    })
    return result


# Tool 13: Run tests
@mcp.tool()
async def run_tests(test_directory: str = "test/unit", test_file: str = "", test_name: str = "") -> dict:
    """
    Execute GUT tests headlessly and return structured results.

    This tool runs Godot in headless mode to execute GDScript tests via the GUT framework.
    It can run all tests or target specific test files/names.

    Args:
        test_directory: Test directory path (e.g., 'test/unit'). Defaults to 'test/unit'.
        test_file: Specific test file to run (e.g., 'test_example.gd'). If omitted, runs all tests in directory.
        test_name: Filter tests by name prefix (e.g., 'test_signal' runs only test_signal_* functions). If omitted, runs all tests in file.

    Returns:
        Structured test results with pass/fail counts and failure details
    """
    logger.info(f"run_tests called: directory={test_directory}, file={test_file}, name={test_name}")

    # Locate Godot project root (parent of bridge directory)
    project_root = Path(__file__).parent.parent.resolve()

    # Verify Godot executable is accessible
    godot_cmd = "godot"
    try:
        result = subprocess.run(
            [godot_cmd, "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0:
            raise ToolError(
                "Godot not found or not on PATH. Try adding Godot to system PATH."
            )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise ToolError(
            f"Cannot execute Godot. Ensure Godot is installed and on PATH.\n{str(e)}"
        )

    # Step 1: Pre-import to avoid class registration failures
    logger.info("Running pre-import step...")
    try:
        preimport_result = subprocess.run(
            [godot_cmd, "--headless", "--import", "--quit"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=30
        )
        logger.debug(f"Pre-import exit code: {preimport_result.returncode}")
    except subprocess.TimeoutExpired:
        logger.warning("Pre-import timed out (continuing anyway)")

    # Step 2: Build GUT command
    gut_args = [
        godot_cmd,
        "--headless",
        "-s", "addons/gut/gut_cmdln.gd"
    ]

    # Add directory filter
    if test_directory:
        gut_args.extend([f"-gdir=res://{test_directory}"])

    # Add file filter if specified
    if test_file:
        test_path = f"res://{test_directory}/{test_file}"
        gut_args.extend([f"-gtest={test_path}"])

    # Add name filter if specified
    if test_name:
        gut_args.extend([f"-gprefix={test_name}"])

    # Always exit after tests complete
    gut_args.append("-gexit")

    logger.info(f"Executing: {' '.join(gut_args)}")

    # Step 3: Execute tests
    try:
        test_result = subprocess.run(
            gut_args,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=60
        )

        stdout = test_result.stdout
        stderr = test_result.stderr
        exit_code = test_result.returncode

        logger.debug(f"Test exit code: {exit_code}")
        logger.debug(f"Test stdout length: {len(stdout)} chars")

    except subprocess.TimeoutExpired:
        raise ToolError(
            "Test execution timed out (>60s). Check for infinite loops or hanging tests."
        )
    except Exception as e:
        raise ToolError(f"Error executing tests: {str(e)}")

    # Step 4: Parse GUT output
    parsed = _parse_gut_output(stdout)

    # Build result message
    result_text = "**Test Results**\n\n"
    result_text += "**Summary:**\n"
    result_text += f"- Passed: {parsed['passed']}\n"
    result_text += f"- Failed: {parsed['failed']}\n"
    result_text += f"- Pending: {parsed['pending']}\n"
    result_text += f"- Total: {parsed['passed'] + parsed['failed'] + parsed['pending']}\n"
    result_text += f"\n**Status:** {'✓ ALL TESTS PASSED' if parsed['failed'] == 0 else '✗ SOME TESTS FAILED'}\n"

    if parsed["failures"]:
        result_text += "\n**Failures:**\n"
        for failure in parsed["failures"]:
            result_text += f"\n- {failure['test']}\n"
            result_text += f"  File: {failure['file']}\n"
            result_text += f"  Line: {failure['line']}\n"
            result_text += f"  Error: {failure['message']}\n"

    # Append full stdout for debugging
    result_text += f"\n**Full Output:**\n```\n{stdout}\n```\n"

    if stderr:
        result_text += f"\n**Stderr:**\n```\n{stderr}\n```\n"

    # Return structured result
    return {
        "summary": {
            "passed": parsed["passed"],
            "failed": parsed["failed"],
            "pending": parsed["pending"],
            "total": parsed["passed"] + parsed["failed"] + parsed["pending"]
        },
        "failures": parsed["failures"],
        "output": result_text,
        "all_passed": parsed["failed"] == 0
    }


def _parse_gut_output(output: str) -> dict:
    """
    Parse GUT test output to extract pass/fail counts and failure details.

    GUT output format:
    - Summary line: "passed:6 failed:0 pending:0"
    - Failure format: "res://test/unit/test_example.gd(32) - Expected [2] to equal [3]: test_failing"

    Args:
        output: Raw stdout from GUT execution

    Returns:
        Dictionary with keys: passed, failed, pending, failures (list of dicts)
    """
    result = {
        "passed": 0,
        "failed": 0,
        "pending": 0,
        "failures": []
    }

    # Extract summary counts from "passed:6 failed:0 pending:0" line
    summary_match = re.search(r'passed:(\d+)\s+failed:(\d+)\s+pending:(\d+)', output)
    if summary_match:
        result["passed"] = int(summary_match.group(1))
        result["failed"] = int(summary_match.group(2))
        result["pending"] = int(summary_match.group(3))

    # Extract failure details
    # Format: res://test/unit/test_example.gd(32) - Expected [2] to equal [3]: test_failing
    failure_pattern = r'(res://[^(]+)\((\d+)\)\s*-\s*([^:]+):\s*(\w+)'
    for match in re.finditer(failure_pattern, output):
        result["failures"].append({
            "file": match.group(1),
            "line": int(match.group(2)),
            "message": match.group(3).strip(),
            "test": match.group(4)
        })

    return result


# Tool 14: Request user input
@mcp.tool()
async def request_user_input(
    prompt: str,
    type: str,
    options: list[str] | None = None,
    labels: list[str] | None = None
) -> dict:
    """
    Request structured input from the developer via an overlay widget in the Claudot panel.

    This tool BLOCKS until the developer responds (up to ~60s real time, 120s HTTP timeout).
    An input widget appears in the Godot editor panel showing the prompt and input controls.

    Supported input types:
        - "radio":    Single-choice from a list (options required)
        - "checkbox": Multi-select from a list (options required)
        - "confirm":  Yes/no confirmation, optionally with custom button labels
        - "text":     Free-form text entry

    If the HTTP request times out before the developer responds, call
    get_pending_input_answer() to retrieve the buffered response.

    Args:
        prompt:  Question or instruction to display to the developer (required, non-empty)
        type:    Input type: "radio", "checkbox", "confirm", or "text"
        options: List of choices for radio/checkbox types (required for those types)
        labels:  Custom button labels for confirm type (e.g., ["Yes, do it", "No, cancel"])

    Returns:
        {
            "answer": {<developer response dict>},
            "timestamp": <unix time>
        }

    Examples:
        # Radio choice
        request_user_input(prompt="Which difficulty?", type="radio", options=["Easy","Normal","Hard"])

        # Confirmation
        request_user_input(prompt="Delete all enemies?", type="confirm")

        # Free text
        request_user_input(prompt="Enter the player name:", type="text")
    """
    logger.info(f"request_user_input called: type={type}, prompt={prompt[:50]!r}")
    result = await _forward_to_godot_long("request_user_input", {
        "prompt": prompt,
        "type": type,
        "options": options or [],
        "labels": labels or []
    })
    return result


# Tool 15: Get pending input answer
@mcp.tool()
async def get_pending_input_answer() -> dict:
    """
    Retrieve a buffered developer answer from a previous request_user_input call.

    Use this when a previous request_user_input call timed out (HTTP 120s timeout
    elapsed) before the developer responded. The answer is buffered in Godot so it
    can be retrieved here.

    Returns:
        {
            "has_answer": true,
            "answer": {<developer response dict>},
            "timestamp": <unix time>
        }

        or if no buffered answer:
        {
            "has_answer": false,
            "timestamp": <unix time>
        }
    """
    logger.info("get_pending_input_answer called")
    result = await _forward_to_godot("get_pending_input_answer", {})
    return result


# Tool 16: Run scene
@mcp.tool()
async def run_scene(scene_path: str = "") -> dict:
    """
    Run a game scene in the Godot editor.

    Launches the game inside the editor (equivalent to pressing F5 for main scene
    or F6 for current scene). After starting, use capture_screenshot(viewport_type="game")
    to see the running game, and get_debugger_output() / get_debugger_errors() to
    read game output.

    Args:
        scene_path: Path to scene file (e.g., "res://scenes/level1.tscn").
                    If empty, runs the project's main scene (F5 equivalent).

    Returns:
        Confirmation that scene was launched
    """
    logger.info(f"run_scene called: scene_path={scene_path!r}")
    result = await _forward_to_godot("run_scene", {"scene_path": scene_path})
    return result


# Tool 17: Stop scene
@mcp.tool()
async def stop_scene() -> dict:
    """
    Stop the currently running game scene in the Godot editor.

    Equivalent to pressing the Stop button (F8) in the editor toolbar.

    Returns:
        Confirmation that scene was stopped
    """
    logger.info("stop_scene called")
    result = await _forward_to_godot("stop_scene", {})
    return result


# Tool 18: Search Godot docs
@mcp.tool()
async def godot_search_docs(query: str, kind: str = "") -> str:
    """
    Search the Godot 4 class reference for accurate method signatures, property names,
    and signal names. Use this BEFORE writing GDScript that involves a Godot built-in
    class you are not certain about — especially for physics bodies, UI controls, and
    any class added or changed in Godot 4. Prevents hallucinated API calls.

    Args:
        query: Class name, method, property, or keyword to search for (e.g. "move_and_slide", "CharacterBody2D", "tween")
        kind: Optional filter — "class", "method", "property", "signal", "constant"

    Returns top 10 matches with class name, symbol type, and description snippet.
    """
    logger.info(f"godot_search_docs called: query={query!r}, kind={kind!r}")

    if search_docs is None:
        return "ERROR: godot_docs module not available. Check that godot_docs.py is in the same directory as godot_mcp_server.py."

    results = await search_docs(query, kind)

    if not results:
        return f"No results found for '{query}'" + (f" (kind={kind})" if kind else "")

    # Check for error result
    if len(results) == 1 and "error" in results[0]:
        return f"ERROR: {results[0]['error']}"

    lines = [f"Search results for '{query}'" + (f" [kind={kind}]" if kind else "") + ":\n"]
    for entry in results:
        kind_str = entry.get("kind", "")
        class_name = entry.get("class_name", "")
        name = entry.get("name", "")
        brief = entry.get("brief", "")

        if kind_str == "class":
            symbol = class_name
        else:
            symbol = f"{class_name}.{name}"

        line = f"  [{kind_str}] {symbol}"
        if brief and brief != "(not yet fetched)":
            line += f" — {brief}"
        lines.append(line)

    return "\n".join(lines)


# Tool 19: Get full class docs
@mcp.tool()
async def godot_get_class_docs(class_name: str, section: str = "all") -> str:
    """
    Get complete documentation for a Godot 4 class including inheritance chain, methods
    with full signatures, properties with types and defaults, signals, and constants.
    Use when you need exact method signatures, parameter types, return types, or property
    defaults. Always prefer this over guessing when implementing features involving
    built-in Godot classes.

    Args:
        class_name: The Godot class name (e.g. "Node2D", "CharacterBody2D", "Timer")
        section: Which section — "all", "methods", "properties", "signals", "constants", "description"
    """
    logger.info(f"godot_get_class_docs called: class_name={class_name!r}, section={section!r}")

    if get_class_docs is None:
        return "ERROR: godot_docs module not available. Check that godot_docs.py is in the same directory as godot_mcp_server.py."

    data = await get_class_docs(class_name, section)

    if "error" in data:
        return f"ERROR: {data['error']}"

    lines: list[str] = []

    # Header: name + inheritance chain
    chain = data.get("inheritance_chain", [])
    if chain:
        lines.append(f"# {class_name}")
        lines.append(f"Inherits: {' < '.join(chain[1:])}" if len(chain) > 1 else "Inherits: (none)")
    else:
        lines.append(f"# {class_name}")

    version = data.get("version", "")
    if version:
        lines.append(f"Godot version: {version}")

    lines.append("")

    # Brief description
    brief = data.get("brief_description", "")
    if brief:
        lines.append(brief)
        lines.append("")

    # Description section
    if section in ("all", "description"):
        desc = data.get("description", "")
        if desc:
            lines.append("## Description")
            lines.append(desc)
            lines.append("")

    # Methods section
    if section in ("all", "methods"):
        methods = data.get("methods", [])
        if methods:
            lines.append("## Methods")
            for m in methods:
                params_str = ", ".join(
                    f"{p['type']} {p['name']}" + (f" = {p['default']}" if p.get("default") else "")
                    for p in m.get("params", [])
                )
                qualifiers = m.get("qualifiers", "")
                sig = f"{m['return_type']} {m['name']}({params_str})"
                if qualifiers:
                    sig += f" {qualifiers}"
                lines.append(f"  {sig}")
                if m.get("description"):
                    lines.append(f"    {m['description'][:120]}")
            lines.append("")

    # Properties section
    if section in ("all", "properties"):
        members = data.get("members", [])
        if members:
            lines.append("## Properties")
            for mem in members:
                default = mem.get("default", "")
                prop_line = f"  {mem['type']} {mem['name']}"
                if default:
                    prop_line += f" = {default}"
                lines.append(prop_line)
                if mem.get("description"):
                    lines.append(f"    {mem['description'][:120]}")
            lines.append("")

    # Signals section
    if section in ("all", "signals"):
        signals = data.get("signals", [])
        if signals:
            lines.append("## Signals")
            for sig in signals:
                params_str = ", ".join(
                    f"{p['type']} {p['name']}" for p in sig.get("params", [])
                )
                lines.append(f"  {sig['name']}({params_str})")
                if sig.get("description"):
                    lines.append(f"    {sig['description'][:120]}")
            lines.append("")

    # Constants section
    if section in ("all", "constants"):
        constants = data.get("constants", [])
        if constants:
            lines.append("## Constants")
            enums = data.get("enums", {})
            shown_in_enum: set[str] = set()

            # Show enums grouped
            for enum_name, enum_consts in enums.items():
                lines.append(f"  enum {enum_name}:")
                for c in enum_consts:
                    lines.append(f"    {c['name']} = {c['value']}")
                    shown_in_enum.add(c["name"])

            # Remaining standalone constants
            for c in constants:
                if c["name"] not in shown_in_enum:
                    lines.append(f"  {c['name']} = {c['value']}")
            lines.append("")

    return "\n".join(lines)


# Tool 20: Refresh docs cache
@mcp.tool()
async def godot_refresh_docs(version: str = "") -> str:
    """
    Force refresh of cached Godot 4 documentation. Use if docs seem outdated or if you
    need docs for a specific Godot version. Clears cache and re-downloads from GitHub.

    Args:
        version: Godot version tag (e.g. "4.4-stable"). Leave empty for latest stable.
    """
    logger.info(f"godot_refresh_docs called: version={version!r}")

    if refresh_docs is None:
        return "ERROR: godot_docs module not available. Check that godot_docs.py is in the same directory as godot_mcp_server.py."

    result = await refresh_docs(version)

    if result.get("status") == "error":
        return f"ERROR: {result.get('message', 'Unknown error')}"

    return result.get("message", f"Docs refreshed for Godot {result.get('version', 'unknown')}")


# Entry point
if __name__ == "__main__":
    logger.info("Starting Godot MCP Server")
    logger.info("Transport: stdio (discoverable by Claude Code)")
    logger.info("Tools: 20 (scene state, properties, node manipulation, file search, screenshots, debugger output, debugger errors, script reading, tests, interactive input, game control, godot docs)")
    mcp.run()

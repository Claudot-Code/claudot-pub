#!/usr/bin/env python3
"""
HTTP Bridge Validation Script

Tests all 11 HTTP bridge tools end-to-end against a running Godot editor
with the Claudot plugin enabled. This script validates that the HTTP bridge
actually works, not just that tools are registered.

Prerequisites:
- Godot editor running with Claudot plugin enabled
- HTTP server shows "listening on 127.0.0.1:7778" in Godot console
- At least one scene open in the editor (needs a root node)

Usage:
    python addons/claudot/bridge/validate_mcp_tools.py

Exit codes:
    0 - All tests passed
    1 - One or more tests failed
"""

import base64
import json
import sys
import urllib.request
import urllib.error
from typing import Dict, Any, Optional, Tuple


# Configuration
BRIDGE_URL = "http://127.0.0.1:7778"
TIMEOUT = 5  # seconds


class TestResult:
    """Track test execution results."""

    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def record_pass(self, test_name: str):
        """Record a test pass."""
        self.passed += 1
        print(f"[PASS] {test_name}")

    def record_fail(self, test_name: str, error: str):
        """Record a test failure."""
        self.failed += 1
        self.failures.append((test_name, error))
        print(f"[FAIL] {test_name}")
        print(f"  Error: {error}")

    def summary(self) -> Tuple[int, int]:
        """Print summary and return (passed, failed) counts."""
        print("\n" + "="*60)
        print(f"SUMMARY: {self.passed}/{self.passed + self.failed} tests passed")
        print("="*60)

        if self.failures:
            print("\nFailures:")
            for test_name, error in self.failures:
                print(f"  - {test_name}: {error}")

        return self.passed, self.failed


def call_tool(tool_name: str, tool_args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Call an HTTP bridge tool directly.

    Args:
        tool_name: Name of the tool to call
        tool_args: Dictionary of tool arguments

    Returns:
        Response dictionary with is_error and tool_call_result

    Raises:
        urllib.error.URLError: If connection fails
        ValueError: If response is invalid JSON
    """
    request_data = {
        "tool_name": tool_name,
        "tool_args": tool_args
    }

    request_body = json.dumps(request_data).encode('utf-8')

    req = urllib.request.Request(
        f"{BRIDGE_URL}/mcp/invoke",
        data=request_body,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )

    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        response_data = response.read().decode('utf-8')
        return json.loads(response_data)


def get_tools_list() -> Dict[str, Any]:
    """
    Get list of available tools from HTTP bridge.

    Returns:
        Response dictionary with mcp_tools array
    """
    req = urllib.request.Request(f"{BRIDGE_URL}/tools", method='GET')

    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        response_data = response.read().decode('utf-8')
        return json.loads(response_data)


def test_connectivity(result: TestResult) -> bool:
    """Test 1: HTTP bridge connectivity."""
    try:
        tools = get_tools_list()

        if "mcp_tools" not in tools:
            result.record_fail("HTTP bridge connectivity", "Response missing 'mcp_tools' key")
            return False

        tool_count = len(tools["mcp_tools"])
        if tool_count != 11:
            result.record_fail("HTTP bridge connectivity", f"Expected 11 tools, got {tool_count}")
            return False

        result.record_pass("HTTP bridge connectivity (11 tools registered)")
        return True

    except Exception as e:
        result.record_fail("HTTP bridge connectivity", str(e))
        return False


def test_get_scene_state(result: TestResult) -> Optional[str]:
    """Test 2: get_scene_state tool - also returns scene root path for later tests."""
    try:
        response = call_tool("get_scene_state", {"max_depth": 3})

        if response.get("is_error", False):
            result.record_fail("get_scene_state", response.get("tool_call_result", "Unknown error"))
            return None

        # Parse tool_call_result (JSON string from Godot)
        tool_result_str = response.get("tool_call_result", "{}")
        tool_result = json.loads(tool_result_str)

        # Verify response structure
        if not tool_result.get("success", False):
            result.record_fail("get_scene_state", f"Tool returned success=false: {tool_result.get('error', 'Unknown')}")
            return None

        if "tree" not in tool_result:
            result.record_fail("get_scene_state", "Response missing 'tree' key")
            return None

        if "node_count" not in tool_result:
            result.record_fail("get_scene_state", "Response missing 'node_count' key")
            return None

        # Extract scene root path from tree
        tree = tool_result["tree"]
        root_path = None

        # DEBUG: Print tree structure to understand what we're getting
        print(f"  DEBUG: Tree keys: {list(tree.keys())}")

        # Tree is a dict where keys are node paths like "/root/NodeName"
        # Filter out editor internal nodes (paths containing @ symbols like @EditorNode@)
        # Then find the shortest path (root node)
        if tree and isinstance(tree, dict):
            # Filter out paths containing @ (editor internals)
            scene_paths = [path for path in tree.keys() if '@' not in path]
            print(f"  DEBUG: Scene paths (after filtering @): {scene_paths}")

            if scene_paths:
                # Sort by length to get root first
                scene_paths.sort(key=len)
                root_path = scene_paths[0]
                print(f"  DEBUG: Root path: {root_path}")

        if not root_path:
            result.record_fail("get_scene_state", "Could not extract scene root path from tree")
            return None

        result.record_pass(f"get_scene_state (found root: {root_path})")
        return root_path

    except Exception as e:
        result.record_fail("get_scene_state", str(e))
        return None


def test_get_editor_context(result: TestResult):
    """Test 3: get_editor_context tool."""
    try:
        response = call_tool("get_editor_context", {})

        if response.get("is_error", False):
            result.record_fail("get_editor_context", response.get("tool_call_result", "Unknown error"))
            return

        tool_result = json.loads(response.get("tool_call_result", "{}"))

        if not tool_result.get("success", False):
            result.record_fail("get_editor_context", f"Tool returned success=false: {tool_result.get('error', 'Unknown')}")
            return

        if "scene_path" not in tool_result:
            result.record_fail("get_editor_context", "Response missing 'scene_path' key")
            return

        if "selection" not in tool_result:
            result.record_fail("get_editor_context", "Response missing 'selection' key")
            return

        result.record_pass("get_editor_context")

    except Exception as e:
        result.record_fail("get_editor_context", str(e))


def test_create_node(result: TestResult, scene_root_path: str) -> Optional[str]:
    """Test 4: create_node tool - returns created node path if successful."""
    try:
        # Use scene root path directly (already in correct format from tree)
        parent_path = scene_root_path
        print(f"  DEBUG: scene_root_path='{scene_root_path}', parent_path='{parent_path}'")

        response = call_tool("create_node", {
            "node_type": "Node2D",
            "node_name": "ValidationTestNode",
            "parent_path": parent_path
        })

        if response.get("is_error", False):
            result.record_fail("create_node", response.get("tool_call_result", "Unknown error"))
            return None

        tool_result = json.loads(response.get("tool_call_result", "{}"))

        if not tool_result.get("success", False):
            result.record_fail("create_node", f"Tool returned success=false: {tool_result.get('error', 'Unknown')}")
            return None

        if "node_path" not in tool_result:
            result.record_fail("create_node", "Response missing 'node_path' key")
            return None

        if "node_type" not in tool_result:
            result.record_fail("create_node", "Response missing 'node_type' key")
            return None

        node_path = tool_result["node_path"]
        result.record_pass(f"create_node (created: {node_path})")
        return node_path

    except Exception as e:
        result.record_fail("create_node", str(e))
        return None


def test_get_node_property(result: TestResult, node_path: str) -> Optional[Dict[str, Any]]:
    """Test 5: get_node_property tool - returns property value if successful."""
    try:
        response = call_tool("get_node_property", {
            "node_path": node_path,
            "property_name": "position"
        })

        if response.get("is_error", False):
            result.record_fail("get_node_property (initial)", response.get("tool_call_result", "Unknown error"))
            return None

        tool_result = json.loads(response.get("tool_call_result", "{}"))

        if not tool_result.get("success", False):
            result.record_fail("get_node_property (initial)", f"Tool returned success=false: {tool_result.get('error', 'Unknown')}")
            return None

        if "value" not in tool_result:
            result.record_fail("get_node_property (initial)", "Response missing 'value' key")
            return None

        value = tool_result["value"]
        result.record_pass(f"get_node_property (initial position: {value})")
        return value

    except Exception as e:
        result.record_fail("get_node_property (initial)", str(e))
        return None


def test_set_node_property(result: TestResult, node_path: str):
    """Test 6: set_node_property tool."""
    try:
        response = call_tool("set_node_property", {
            "node_path": node_path,
            "property_name": "position",
            "value": {"_type": "Vector2", "x": 100, "y": 200}
        })

        if response.get("is_error", False):
            result.record_fail("set_node_property", response.get("tool_call_result", "Unknown error"))
            return

        tool_result = json.loads(response.get("tool_call_result", "{}"))

        if not tool_result.get("success", False):
            result.record_fail("set_node_property", f"Tool returned success=false: {tool_result.get('error', 'Unknown')}")
            return

        if "old_value" not in tool_result:
            result.record_fail("set_node_property", "Response missing 'old_value' key")
            return

        if "new_value" not in tool_result:
            result.record_fail("set_node_property", "Response missing 'new_value' key")
            return

        result.record_pass(f"set_node_property (old: {tool_result['old_value']}, new: {tool_result['new_value']})")

    except Exception as e:
        result.record_fail("set_node_property", str(e))


def test_property_persistence(result: TestResult, node_path: str):
    """Test 7: Verify property change persisted."""
    try:
        response = call_tool("get_node_property", {
            "node_path": node_path,
            "property_name": "position"
        })

        if response.get("is_error", False):
            result.record_fail("property persistence", response.get("tool_call_result", "Unknown error"))
            return

        tool_result = json.loads(response.get("tool_call_result", "{}"))

        if not tool_result.get("success", False):
            result.record_fail("property persistence", f"Tool returned success=false: {tool_result.get('error', 'Unknown')}")
            return

        value = tool_result.get("value", {})

        # Check if position is (100, 200)
        if value.get("x") != 100 or value.get("y") != 200:
            result.record_fail("property persistence", f"Expected (100, 200), got {value}")
            return

        result.record_pass(f"property persistence (confirmed: {value})")

    except Exception as e:
        result.record_fail("property persistence", str(e))


def test_reparent_node(result: TestResult, scene_root_path: str) -> Optional[str]:
    """Test 8: reparent_node tool - creates target parent first."""
    try:
        # Create a target parent node using scene root path directly
        parent_path = scene_root_path

        response = call_tool("create_node", {
            "node_type": "Node2D",
            "node_name": "ReparentTarget",
            "parent_path": parent_path
        })

        if response.get("is_error", False):
            result.record_fail("reparent_node (create target)", response.get("tool_call_result", "Unknown error"))
            return None

        tool_result = json.loads(response.get("tool_call_result", "{}"))
        target_path = tool_result.get("node_path")

        if not target_path:
            result.record_fail("reparent_node (create target)", "Failed to get target node path")
            return None

        # Now reparent ValidationTestNode under ReparentTarget
        response = call_tool("reparent_node", {
            "node_path": f"{parent_path}/ValidationTestNode",
            "new_parent_path": target_path
        })

        if response.get("is_error", False):
            result.record_fail("reparent_node", response.get("tool_call_result", "Unknown error"))
            return None

        tool_result = json.loads(response.get("tool_call_result", "{}"))

        if not tool_result.get("success", False):
            result.record_fail("reparent_node", f"Tool returned success=false: {tool_result.get('error', 'Unknown')}")
            return None

        if "new_parent" not in tool_result:
            result.record_fail("reparent_node", "Response missing 'new_parent' key")
            return None

        result.record_pass(f"reparent_node (new parent: {tool_result['new_parent']})")
        return target_path

    except Exception as e:
        result.record_fail("reparent_node", str(e))
        return None


def test_delete_node(result: TestResult, node_path: str, test_name_suffix: str = ""):
    """Test 9: delete_node tool."""
    try:
        response = call_tool("delete_node", {
            "node_path": node_path
        })

        if response.get("is_error", False):
            result.record_fail(f"delete_node{test_name_suffix}", response.get("tool_call_result", "Unknown error"))
            return

        tool_result = json.loads(response.get("tool_call_result", "{}"))

        if not tool_result.get("success", False):
            result.record_fail(f"delete_node{test_name_suffix}", f"Tool returned success=false: {tool_result.get('error', 'Unknown')}")
            return

        if "deleted_path" not in tool_result:
            result.record_fail(f"delete_node{test_name_suffix}", "Response missing 'deleted_path' key")
            return

        result.record_pass(f"delete_node{test_name_suffix} (deleted: {tool_result['deleted_path']})")

    except Exception as e:
        result.record_fail(f"delete_node{test_name_suffix}", str(e))


def test_error_unknown_tool(result: TestResult):
    """Test 10: Error handling for unknown tool."""
    try:
        response = call_tool("nonexistent_tool", {})

        if not response.get("is_error", False):
            result.record_fail("error handling (unknown tool)", "Expected is_error=true for unknown tool")
            return

        result.record_pass("error handling (unknown tool)")

    except Exception as e:
        result.record_fail("error handling (unknown tool)", str(e))


def test_error_invalid_node_type(result: TestResult, scene_root_path: str):
    """Test 11: Error handling for invalid node type."""
    try:
        # Use scene root path directly
        parent_path = scene_root_path

        response = call_tool("create_node", {
            "node_type": "NonExistentNodeType",
            "node_name": "BadNode",
            "parent_path": parent_path
        })

        if not response.get("is_error", False):
            result.record_fail("error handling (invalid node type)", "Expected is_error=true for invalid node type")
            return

        result.record_pass("error handling (invalid node type)")

    except Exception as e:
        result.record_fail("error handling (invalid node type)", str(e))


def test_search_files_by_extension(result: TestResult):
    """Test 12: search_files by extension filter."""
    try:
        response = call_tool("search_files", {
            "extensions": [".gd"],
            "max_results": 100
        })

        if response.get("is_error", False):
            result.record_fail("search_files (by extension)", response.get("tool_call_result", "Unknown error"))
            return

        tool_result = json.loads(response.get("tool_call_result", "{}"))

        if not tool_result.get("success", False):
            result.record_fail("search_files (by extension)", f"Tool returned success=false: {tool_result.get('error', 'Unknown')}")
            return

        if "files" not in tool_result:
            result.record_fail("search_files (by extension)", "Response missing 'files' key")
            return

        if "count" not in tool_result:
            result.record_fail("search_files (by extension)", "Response missing 'count' key")
            return

        count = tool_result.get("count", 0)
        if count == 0:
            result.record_fail("search_files (by extension)", "Expected at least one .gd file in project")
            return

        # Verify first result has required fields
        files = tool_result.get("files", [])
        if len(files) > 0:
            first_file = files[0]
            if "path" not in first_file:
                result.record_fail("search_files (by extension)", "File missing 'path' field")
                return
            if "type" not in first_file:
                result.record_fail("search_files (by extension)", "File missing 'type' field")
                return
            if "name" not in first_file:
                result.record_fail("search_files (by extension)", "File missing 'name' field")
                return
            if "directory" not in first_file:
                result.record_fail("search_files (by extension)", "File missing 'directory' field")
                return

            path = first_file.get("path", "")
            if not path.startswith("res://"):
                result.record_fail("search_files (by extension)", f"Path should start with 'res://', got: {path}")
                return

            file_type = first_file.get("type", "")
            if file_type != "GDScript":
                result.record_fail("search_files (by extension)", f"Expected type 'GDScript' for .gd file, got: {file_type}")
                return

        result.record_pass(f"search_files (by extension) - found {count} .gd files")

    except Exception as e:
        result.record_fail("search_files (by extension)", str(e))


def test_search_files_by_pattern(result: TestResult):
    """Test 13: search_files by pattern filter."""
    try:
        response = call_tool("search_files", {
            "pattern": "test_*",
            "max_results": 100
        })

        if response.get("is_error", False):
            result.record_fail("search_files (by pattern)", response.get("tool_call_result", "Unknown error"))
            return

        tool_result = json.loads(response.get("tool_call_result", "{}"))

        if not tool_result.get("success", False):
            result.record_fail("search_files (by pattern)", f"Tool returned success=false: {tool_result.get('error', 'Unknown')}")
            return

        # Verify all returned files match pattern
        files = tool_result.get("files", [])
        for file_info in files:
            name = file_info.get("name", "")
            # Pattern is case-insensitive, so check lowercase
            if not name.lower().startswith("test_"):
                result.record_fail("search_files (by pattern)", f"File '{name}' doesn't match pattern 'test_*'")
                return

        result.record_pass(f"search_files (by pattern) - found {len(files)} test_* files")

    except Exception as e:
        result.record_fail("search_files (by pattern)", str(e))


def test_search_files_no_filters(result: TestResult):
    """Test 14: search_files with no filters (all files)."""
    try:
        response = call_tool("search_files", {
            "max_results": 5
        })

        if response.get("is_error", False):
            result.record_fail("search_files (no filters)", response.get("tool_call_result", "Unknown error"))
            return

        tool_result = json.loads(response.get("tool_call_result", "{}"))

        if not tool_result.get("success", False):
            result.record_fail("search_files (no filters)", f"Tool returned success=false: {tool_result.get('error', 'Unknown')}")
            return

        count = tool_result.get("count", 0)
        if count == 0:
            result.record_fail("search_files (no filters)", "Expected at least one file in project")
            return

        result.record_pass(f"search_files (no filters) - found {count} files")

    except Exception as e:
        result.record_fail("search_files (no filters)", str(e))


def test_search_files_result_limit(result: TestResult):
    """Test 15: search_files respects max_results limit."""
    try:
        response = call_tool("search_files", {
            "max_results": 3
        })

        if response.get("is_error", False):
            result.record_fail("search_files (result limit)", response.get("tool_call_result", "Unknown error"))
            return

        tool_result = json.loads(response.get("tool_call_result", "{}"))

        if not tool_result.get("success", False):
            result.record_fail("search_files (result limit)", f"Tool returned success=false: {tool_result.get('error', 'Unknown')}")
            return

        count = tool_result.get("count", 0)
        if count > 3:
            result.record_fail("search_files (result limit)", f"Expected max 3 results, got {count}")
            return

        result.record_pass(f"search_files (result limit) - correctly limited to {count} results")

    except Exception as e:
        result.record_fail("search_files (result limit)", str(e))


def test_capture_2d_editor_screenshot(result: TestResult):
    """Test 16: Capture 2D editor screenshot."""
    try:
        response = call_tool("capture_screenshot", {
            "viewport_type": "2d_editor"
        })

        if response.get("is_error", False):
            result.record_fail("capture_screenshot (2D editor)", response.get("tool_call_result", "Unknown error"))
            return

        tool_result = json.loads(response.get("tool_call_result", "{}"))

        if not tool_result.get("success", False):
            result.record_fail("capture_screenshot (2D editor)", f"Tool returned success=false: {tool_result.get('error', 'Unknown')}")
            return

        if "image_data" not in tool_result:
            result.record_fail("capture_screenshot (2D editor)", "Response missing 'image_data' key")
            return

        image_data = tool_result.get("image_data", "")
        if not image_data:
            result.record_fail("capture_screenshot (2D editor)", "image_data is empty")
            return

        # Verify base64 is valid
        try:
            base64.b64decode(image_data)
        except Exception as decode_err:
            result.record_fail("capture_screenshot (2D editor)", f"Invalid base64 data: {decode_err}")
            return

        if tool_result.get("format") != "jpeg":
            result.record_fail("capture_screenshot (2D editor)", f"Expected format 'jpeg', got '{tool_result.get('format')}'")
            return

        # Verify dimensions are within limits
        encoded_size = tool_result.get("encoded_size", {})
        width = encoded_size.get("width", 0)
        height = encoded_size.get("height", 0)

        if width > 800 or height > 600:
            result.record_fail("capture_screenshot (2D editor)", f"Image exceeds 800x600 limit: {width}x{height}")
            return

        result.record_pass(f"capture_screenshot (2D editor) - captured {width}x{height} JPEG")

    except Exception as e:
        result.record_fail("capture_screenshot (2D editor)", str(e))


def test_capture_invalid_viewport_type(result: TestResult):
    """Test 17: Capture with invalid viewport type."""
    try:
        response = call_tool("capture_screenshot", {
            "viewport_type": "invalid_type"
        })

        # Should return error
        if not response.get("is_error", False):
            # If not is_error, check if tool_result has success=false
            tool_result = json.loads(response.get("tool_call_result", "{}"))
            if tool_result.get("success", True):
                result.record_fail("capture_screenshot (invalid type)", "Expected error for invalid viewport type")
                return

        result.record_pass("capture_screenshot (invalid type) - correctly rejected invalid type")

    except Exception as e:
        result.record_fail("capture_screenshot (invalid type)", str(e))


def test_capture_game_viewport_not_running(result: TestResult):
    """Test 18: Capture game viewport when not running."""
    try:
        response = call_tool("capture_screenshot", {
            "viewport_type": "game"
        })

        # Should error if game not running
        tool_result = json.loads(response.get("tool_call_result", "{}"))

        if tool_result.get("success", False):
            # Game was running - test inconclusive but not a failure
            result.record_pass("capture_screenshot (game not running) - game was running, test inconclusive")
        else:
            # Should have error message about game not running
            error_msg = tool_result.get("error", "").lower()
            if "not running" in error_msg or "game" in error_msg:
                result.record_pass("capture_screenshot (game not running) - correct error message")
            else:
                result.record_fail("capture_screenshot (game not running)", f"Expected 'not running' error, got: {error_msg}")

    except Exception as e:
        result.record_fail("capture_screenshot (game not running)", str(e))


def test_get_debugger_output_empty(result: TestResult):
    """Test 19: get_debugger_output with empty buffer."""
    try:
        response = call_tool("get_debugger_output", {"max_lines": 100})

        if response.get("is_error", False):
            result.record_fail("get_debugger_output (empty buffer)", response.get("tool_call_result", "Unknown error"))
            return

        tool_result = json.loads(response.get("tool_call_result", "{}"))

        if not tool_result.get("success", False):
            result.record_fail("get_debugger_output (empty buffer)", f"Tool returned success=false: {tool_result.get('error', 'Unknown')}")
            return

        if "messages" not in tool_result:
            result.record_fail("get_debugger_output (empty buffer)", "Response missing 'messages' key")
            return

        if "count" not in tool_result:
            result.record_fail("get_debugger_output (empty buffer)", "Response missing 'count' key")
            return

        if "buffer_total" not in tool_result:
            result.record_fail("get_debugger_output (empty buffer)", "Response missing 'buffer_total' key")
            return

        # Buffer should be empty or have some messages (we don't know state)
        # But the response structure must be correct
        messages = tool_result.get("messages", [])
        count = tool_result.get("count", -1)

        if count != len(messages):
            result.record_fail("get_debugger_output (empty buffer)", f"count ({count}) != len(messages) ({len(messages)})")
            return

        result.record_pass(f"get_debugger_output (buffer has {count} messages)")

    except Exception as e:
        result.record_fail("get_debugger_output (empty buffer)", str(e))


def test_get_debugger_output_max_lines(result: TestResult):
    """Test 20: get_debugger_output respects max_lines parameter."""
    try:
        response = call_tool("get_debugger_output", {"max_lines": 5})

        if response.get("is_error", False):
            result.record_fail("get_debugger_output (max_lines)", response.get("tool_call_result", "Unknown error"))
            return

        tool_result = json.loads(response.get("tool_call_result", "{}"))

        if not tool_result.get("success", False):
            result.record_fail("get_debugger_output (max_lines)", f"Tool returned success=false: {tool_result.get('error', 'Unknown')}")
            return

        count = tool_result.get("count", -1)
        if count > 5:
            result.record_fail("get_debugger_output (max_lines)", f"Expected max 5 results, got {count}")
            return

        result.record_pass(f"get_debugger_output (max_lines=5, got {count} messages)")

    except Exception as e:
        result.record_fail("get_debugger_output (max_lines)", str(e))


def main():
    """Run all validation tests."""
    print("="*60)
    print("HTTP Bridge Validation Script")
    print("="*60)
    print(f"Target: {BRIDGE_URL}")
    print(f"Timeout: {TIMEOUT}s")
    print("")

    result = TestResult()

    # Test 1: Connectivity
    if not test_connectivity(result):
        print("\nERROR: Cannot connect to HTTP bridge.")
        print("Make sure:")
        print("  1. Godot editor is running")
        print("  2. Claudot plugin is enabled")
        print("  3. Console shows 'listening on 127.0.0.1:7778'")
        print("  4. At least one scene is open")
        return 1

    # Test 2: get_scene_state (also gets scene root path)
    scene_root_path = test_get_scene_state(result)
    if not scene_root_path:
        print("\nERROR: Could not get scene state or scene root path.")
        print("Make sure at least one scene is open in the editor.")
        return 1

    # Test 3: get_editor_context
    test_get_editor_context(result)

    # Test 4: create_node
    node_path = test_create_node(result, scene_root_path)
    if not node_path:
        print("\nERROR: Could not create test node. Skipping remaining tests.")
        return 1

    # Test 5: get_node_property (initial value)
    test_get_node_property(result, node_path)

    # Test 6: set_node_property
    test_set_node_property(result, node_path)

    # Test 7: get_node_property again (verify persistence)
    test_property_persistence(result, node_path)

    # Test 8: reparent_node (creates ReparentTarget parent first)
    target_path = test_reparent_node(result, scene_root_path)

    # Test 9a: delete_node (ValidationTestNode - now under ReparentTarget)
    if target_path:
        test_delete_node(result, f"{target_path}/ValidationTestNode", " (ValidationTestNode)")
        # Test 9b: delete_node (ReparentTarget)
        test_delete_node(result, target_path, " (ReparentTarget)")

    # Test 10: Error handling - unknown tool
    test_error_unknown_tool(result)

    # Test 11: Error handling - invalid node type
    test_error_invalid_node_type(result, scene_root_path)

    # Test 12: search_files by extension
    test_search_files_by_extension(result)

    # Test 13: search_files by pattern
    test_search_files_by_pattern(result)

    # Test 14: search_files with no filters
    test_search_files_no_filters(result)

    # Test 15: search_files result limit
    test_search_files_result_limit(result)

    # Test 16: capture_screenshot 2D editor
    test_capture_2d_editor_screenshot(result)

    # Test 17: capture_screenshot invalid viewport type
    test_capture_invalid_viewport_type(result)

    # Test 18: capture_screenshot game not running
    test_capture_game_viewport_not_running(result)

    # Test 19: get_debugger_output (empty/current buffer)
    test_get_debugger_output_empty(result)

    # Test 20: get_debugger_output (max_lines parameter)
    test_get_debugger_output_max_lines(result)

    # Print summary
    passed, failed = result.summary()

    return 0 if failed == 0 else 1


def show_help():
    """Display help message."""
    print(__doc__)
    print("\nPrerequisites:")
    print("  1. Start Godot editor")
    print("  2. Enable Claudot plugin (Project > Project Settings > Plugins)")
    print("  3. Verify console shows: 'listening on 127.0.0.1:7778'")
    print("  4. Open or create any scene (needs at least a root node)")
    print("\nThen run:")
    print("  python addons/claudot/bridge/validate_mcp_tools.py")
    print("\nThe script will:")
    print("  - Test all 11 HTTP bridge tools")
    print("  - Create temporary nodes for testing (deleted at end)")
    print("  - Verify error handling")
    print("  - Print PASS/FAIL for each test")
    print("\nExit codes:")
    print("  0 - All tests passed")
    print("  1 - One or more tests failed")


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        show_help()
        sys.exit(0)

    sys.exit(main())

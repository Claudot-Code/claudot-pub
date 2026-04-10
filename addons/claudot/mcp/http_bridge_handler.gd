@tool
extends Node

## HttpBridgeHandler - Routes HTTP requests to MCP scene tools
##
## Implements the HTTP bridge layer between the standalone MCP server and
## the Godot plugin's scene tools. Provides /mcp/invoke and /tools endpoints
## for MCP tool execution.
##
## Request format: {"tool_name": String, "tool_args": Dictionary}
## Response format: {"is_error": bool, "tool_call_result": String}

# Dependencies
const SceneTools = preload("res://addons/claudot/mcp/scene_tools.gd")
const HttpServer = preload("res://addons/claudot/network/http_server.gd")

# Scene tools instance
var scene_tools: Node = null
var plugin: EditorPlugin = null


func setup(editor_plugin: EditorPlugin) -> void:
	## Initialize bridge handler with plugin reference and create scene tools.
	##
	## @param editor_plugin: EditorPlugin instance for EditorInterface access

	plugin = editor_plugin

	# Create scene tools instance
	scene_tools = SceneTools.new()
	scene_tools.name = "SceneTools"
	scene_tools.setup(editor_plugin)
	add_child(scene_tools)


func handle_request(method: String, path: String, body: String, peer: StreamPeerTCP) -> void:
	## Route HTTP request to appropriate handler.
	##
	## Endpoints:
	## - POST /mcp/invoke: Execute a tool operation
	## - GET /tools: List available tools
	## - POST /client_initialized: Acknowledge client connection
	## - Everything else: 404 Not Found

	# Broad error catch to prevent HTTP server crashes
	var result: Dictionary

	match [method, path]:
		["POST", "/mcp/invoke"]:
			result = await _handle_call_tool(body, peer)
		["GET", "/tools"]:
			result = _handle_list_tools(peer)
		["POST", "/client_initialized"]:
			# Acknowledge client initialization (no action needed)
			HttpServer.send_json_response(peer, {"status": "ok"})
		_:
			HttpServer.send_error_response(peer, "Not found: %s %s" % [method, path], 404)


func _handle_call_tool(body: String, peer: StreamPeerTCP) -> Dictionary:
	## Handle POST /mcp/invoke endpoint.
	##
	## Parses request body, routes to scene_tools method, and returns result.

	# Parse request body
	var json_result = JSON.parse_string(body)

	if json_result == null:
		HttpServer.send_error_response(peer, "Invalid JSON in request body", 400)
		return {"is_error": true, "error": "Invalid JSON"}

	# Extract tool_name and tool_args
	var tool_name = json_result.get("tool_name", "")
	var tool_args = json_result.get("tool_args", {})

	if tool_name.is_empty():
		HttpServer.send_error_response(peer, "Missing 'tool_name' in request", 400)
		return {"is_error": true, "error": "Missing tool_name"}

	if not tool_args is Dictionary:
		HttpServer.send_error_response(peer, "'tool_args' must be a Dictionary", 400)
		return {"is_error": true, "error": "Invalid tool_args"}

	# Route to scene tools method
	var result: Dictionary

	match tool_name:
		"get_scene_state":
			result = scene_tools.get_scene_state(tool_args)

		"get_node_property":
			result = scene_tools.get_node_property(tool_args)

		"set_node_property":
			result = scene_tools.set_node_property(tool_args)

		"get_editor_context":
			result = scene_tools.get_editor_context(tool_args)

		"create_node":
			result = scene_tools.create_node(tool_args)

		"delete_node":
			result = scene_tools.delete_node(tool_args)

		"reparent_node":
			result = scene_tools.reparent_node(tool_args)

		"search_files":
			result = scene_tools.search_files(tool_args)

		"capture_screenshot":
			result = await scene_tools.capture_screenshot(tool_args)

		"get_debugger_output":
			result = scene_tools.get_debugger_output(tool_args)

		"get_debugger_errors":
			result = scene_tools.get_debugger_errors(tool_args)

		"get_node_script":
			result = scene_tools.get_node_script(tool_args)

		"run_tests":
			# run_tests is handled by the MCP server directly via subprocess
			# If it somehow gets routed here, return error
			result = {
				"success": false,
				"error": "run_tests must be executed via MCP server subprocess, not HTTP bridge"
			}

		"request_user_input":
			result = await scene_tools.request_user_input(tool_args)

		"get_pending_input_answer":
			result = scene_tools.get_pending_input_answer(tool_args)

		"run_scene":
			result = scene_tools.run_scene(tool_args)

		"stop_scene":
			result = scene_tools.stop_scene(tool_args)

		"get_classdb_class_list":
			result = scene_tools.get_classdb_class_list(tool_args)

		"get_classdb_class_docs":
			result = scene_tools.get_classdb_class_docs(tool_args)

		_:
			result = {
				"success": false,
				"error": "Unknown tool: %s" % tool_name
			}

	# Build GDAI-compatible response
	if result.get("success", false):
		# Success - return tool result as JSON string
		HttpServer.send_json_response(peer, {
			"is_error": false,
			"tool_call_result": JSON.stringify(result)
		})
	else:
		# Error - return error message
		HttpServer.send_json_response(peer, {
			"is_error": true,
			"tool_call_result": result.get("error", "Unknown error")
		})

	return result


func _handle_list_tools(peer: StreamPeerTCP) -> Dictionary:
	## Handle GET /tools endpoint.
	##
	## Returns static list of available tool definitions for discovery/debugging.
	## The MCP server has hardcoded tool definitions via @mcp.tool() decorators,
	## so this is supplementary.

	var tools_list = {
		"mcp_tools": [
			{
				"name": "get_scene_state",
				"description": "Get a snapshot of the Godot scene tree structure"
			},
			{
				"name": "get_node_property",
				"description": "Read a property value from a node in the scene tree"
			},
			{
				"name": "set_node_property",
				"description": "Set a property value on a node in the scene tree"
			},
			{
				"name": "get_editor_context",
				"description": "Get current editor state: scene path, selection, recent errors"
			},
			{
				"name": "create_node",
				"description": "Create a new node in the scene tree"
			},
			{
				"name": "delete_node",
				"description": "Delete a node from the scene tree"
			},
			{
				"name": "reparent_node",
				"description": "Move a node to a different parent in the scene tree"
			},
			{
				"name": "search_files",
				"description": "Search res:// filesystem for files by pattern and extension"
			},
			{
				"name": "capture_screenshot",
				"description": "Capture editor or game viewport screenshot as JPEG image"
			},
			{
				"name": "get_debugger_output",
				"description": "Retrieve recent print() output from game/test execution debugger buffer"
			},
			{
				"name": "get_debugger_errors",
				"description": "Retrieve recent captured errors from game/test execution debugger error buffer"
			},
			{
				"name": "get_node_script",
				"description": "Read GDScript source code attached to a node in the scene tree"
			},
			{
				"name": "run_tests",
				"description": "Execute GUT tests headlessly and return structured results"
			},
			{
				"name": "request_user_input",
				"description": "Request structured input from the developer via an overlay widget in the Claudot panel"
			},
			{
				"name": "get_pending_input_answer",
				"description": "Retrieve a buffered developer answer from a previous timed-out request_user_input call"
			},
			{
				"name": "run_scene",
				"description": "Run a game scene in the Godot editor (main scene or specific scene path)"
			},
			{
				"name": "stop_scene",
				"description": "Stop the currently running game scene in the Godot editor"
			},
			{
				"name": "get_classdb_class_list",
				"description": "Get all class names from Godot's built-in ClassDB (no network needed)"
			},
			{
				"name": "get_classdb_class_docs",
				"description": "Get structured docs (methods, properties, signals, constants) for a class from ClassDB"
			}
		]
	}

	HttpServer.send_json_response(peer, tools_list)
	return tools_list

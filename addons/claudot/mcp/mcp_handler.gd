@tool
extends Node

## MCPHandler - Receives and dispatches MCP commands from the bridge
##
## Listens on TCP message_received signal for JSON-RPC requests with
## method names starting with "mcp/". Dispatches to registered tool
## handlers and sends results back over TCP.

# Dependencies
const MessageProtocol = preload("res://addons/claudot/network/message_protocol.gd")
const SceneTools = preload("res://addons/claudot/mcp/scene_tools.gd")

# References
var plugin: EditorPlugin = null
var tcp_client: Node = null
var scene_tools: Node = null


func setup(editor_plugin: EditorPlugin, client: Node) -> void:
	## Initialize handler with plugin and TCP client references.
	plugin = editor_plugin
	tcp_client = client

	# Create scene tools instance
	scene_tools = SceneTools.new()
	scene_tools.name = "SceneTools"
	scene_tools.setup(editor_plugin)
	add_child(scene_tools)

	# Connect to TCP message stream
	tcp_client.message_received.connect(_on_message_received)

	_debug_log("MCP handler initialized")


func _on_message_received(message: Dictionary) -> void:
	## Handle incoming JSON-RPC messages from TCP client.
	##
	## Filters for MCP commands (method starts with "mcp/"),
	## dispatches to appropriate handler, and sends response back.

	# Only handle JSON-RPC requests (have "method" field)
	if not message.has("method"):
		return

	var method = message.get("method", "")

	# Only handle MCP commands
	if not method.begins_with("mcp/"):
		return

	var params = message.get("params", {})
	var request_id = message.get("id")

	_debug_log("Handling MCP command: %s" % method)

	# Dispatch to handler
	var result = _dispatch_command(method, params)

	# Send response back via TCP
	_send_response(result, request_id)


func _dispatch_command(method: String, params: Dictionary) -> Dictionary:
	## Route MCP method to appropriate handler function.
	##
	## @param method: MCP method name (e.g., "mcp/get_node_property")
	## @param params: Method parameters
	## @return: Result dictionary with success flag and data or error

	match method:
		# Read tools
		"mcp/get_node_property":
			return scene_tools.get_node_property(params)

		"mcp/get_scene_state":
			return scene_tools.get_scene_state(params)

		"mcp/get_editor_context":
			return scene_tools.get_editor_context(params)

		# Write tools
		"mcp/set_node_property":
			return scene_tools.set_node_property(params)

		"mcp/create_node":
			return scene_tools.create_node(params)

		"mcp/delete_node":
			return scene_tools.delete_node(params)

		"mcp/reparent_node":
			return scene_tools.reparent_node(params)

		"mcp/get_debugger_errors":
			return scene_tools.get_debugger_errors(params)

		_:
			return {
				"success": false,
				"error": "Unknown MCP method: %s" % method,
				"timestamp": Time.get_unix_time_from_system()
			}


func _send_response(result: Dictionary, request_id) -> void:
	## Send JSON-RPC response back over TCP connection.
	##
	## Accesses tcp_peer directly since send_message() only supports requests.

	if tcp_client == null:
		return

	# Check connection state using the enum from tcp_client's script
	const TCP_CLIENT_SCRIPT = preload("res://addons/claudot/network/tcp_client.gd")
	if tcp_client.current_state != TCP_CLIENT_SCRIPT.ConnectionState.CONNECTED:
		return

	# Format JSON-RPC response
	var response_str = MessageProtocol.format_response(result, request_id)
	var bytes = response_str.to_utf8_buffer()

	# Send directly via tcp_peer (tcp_client.send_message only does requests)
	var error = tcp_client.tcp_peer.put_data(bytes)

	if error != OK:
		push_error("[MCPHandler] Failed to send response: %d" % error)
	else:
		_debug_log("Response sent for request %s" % str(request_id))


func _debug_log(_message: String) -> void:
	pass

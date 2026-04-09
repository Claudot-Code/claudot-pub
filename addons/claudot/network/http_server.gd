@tool
extends Node

## HttpServer - Non-blocking HTTP server for MCP bridge communication
##
## Implements a lightweight HTTP server using TCPServer that runs on the editor
## main thread via _process() polling. Listens on localhost:7778 for incoming
## MCP tool calls from the standalone Python MCP server.
##
## Architecture:
## - Non-blocking polling loop in _process()
## - Request handler callback pattern for routing
## - JSON-RPC style responses
## - Localhost-only binding for security

# Server state
var tcp_server: TCPServer = null
var peers: Array = []  # Array of StreamPeerTCP

# Configuration
const PORT = 7778
const HOST = "127.0.0.1"  # Localhost only - NEVER bind to 0.0.0.0

# Request handler callback (set by plugin)
var request_handler: Callable


func start_server() -> bool:
	## Start the HTTP server and begin listening for connections.
	##
	## @return: true if server started successfully, false otherwise

	if tcp_server:
		return true

	tcp_server = TCPServer.new()
	var err = tcp_server.listen(PORT, HOST)

	if err != OK:
		push_error("[Claudot HTTP] Failed to start server on %s:%d - Error code: %d" % [HOST, PORT, err])
		tcp_server = null
		return false

	return true


func stop_server() -> void:
	## Stop the HTTP server and disconnect all active connections.

	if not tcp_server:
		return

	# Disconnect all peers
	for peer in peers:
		if peer is StreamPeerTCP:
			peer.disconnect_from_host()

	peers.clear()

	# Stop listening
	tcp_server.stop()
	tcp_server = null


func set_request_handler(handler: Callable) -> void:
	## Set the callable that will handle parsed HTTP requests.
	##
	## Handler signature: func(method: String, path: String, body: String, peer: StreamPeerTCP) -> void

	request_handler = handler


func _process(delta: float) -> void:
	## Non-blocking polling loop for HTTP connections.
	##
	## Called every frame to check for new connections and process active peers.

	if not tcp_server:
		return

	# Accept new connections (non-blocking)
	if tcp_server.is_connection_available():
		var peer = tcp_server.take_connection()
		peers.append(peer)

	# Process active connections
	var disconnected_indices = []

	for i in range(peers.size()):
		var peer = peers[i]

		# CRITICAL: Must poll before checking status
		peer.poll()

		var status = peer.get_status()

		if status == StreamPeerTCP.STATUS_CONNECTED:
			# Check if data is available
			if peer.get_available_bytes() > 0:
				_handle_http_request(peer)
		else:
			# Connection closed or error - mark for removal
			disconnected_indices.append(i)

	# Remove disconnected peers (reverse order to preserve indices)
	for i in range(disconnected_indices.size() - 1, -1, -1):
		var idx = disconnected_indices[i]
		peers.remove_at(idx)


func _handle_http_request(peer: StreamPeerTCP) -> void:
	## Parse incoming HTTP request and route to handler.
	##
	## Extracts method, path, and body from raw HTTP request, then calls
	## the registered request_handler callback.

	# Read all available data
	var raw_data = peer.get_utf8_string(peer.get_available_bytes())

	if raw_data.is_empty():
		return

	# Parse HTTP request line (first line before \r\n)
	var lines = raw_data.split("\r\n")
	if lines.size() == 0:
		send_error_response(peer, "Invalid HTTP request - no lines", 400)
		return

	var request_line = lines[0].split(" ")
	if request_line.size() < 2:
		send_error_response(peer, "Invalid HTTP request line", 400)
		return

	var method = request_line[0]
	var path = request_line[1]

	# Extract body (after \r\n\r\n separator)
	var body_start = raw_data.find("\r\n\r\n")
	var body = ""
	if body_start != -1:
		body = raw_data.substr(body_start + 4)

	# Route to handler
	if request_handler.is_valid():
		request_handler.call(method, path, body, peer)
	else:
		send_error_response(peer, "Service unavailable - no request handler configured", 503)


static func send_json_response(peer: StreamPeerTCP, data: Dictionary, status_code: int = 200) -> void:
	## Send an HTTP JSON response to the client.
	##
	## @param peer: TCP connection to send response on
	## @param data: Dictionary to serialize as JSON body
	## @param status_code: HTTP status code (200, 400, 404, 500, etc.)

	var body = JSON.stringify(data)

	# Map status codes to text
	var status_text = "OK"
	match status_code:
		200: status_text = "OK"
		400: status_text = "Bad Request"
		404: status_text = "Not Found"
		500: status_text = "Internal Server Error"
		503: status_text = "Service Unavailable"

	# Build HTTP response
	var response = "HTTP/1.1 %d %s\r\n" % [status_code, status_text]
	response += "Content-Type: application/json\r\n"
	response += "Content-Length: %d\r\n" % body.length()
	response += "Access-Control-Allow-Origin: *\r\n"
	response += "Connection: close\r\n"
	response += "\r\n"
	response += body

	# Send response
	var send_err = peer.put_data(response.to_utf8_buffer())
	if send_err != OK:
		push_error("[Claudot HTTP] Failed to send response: %d" % send_err)


static func send_error_response(peer: StreamPeerTCP, error_message: String, status_code: int = 500) -> void:
	## Send an error response in GDAI format.
	##
	## @param peer: TCP connection
	## @param error_message: Error description
	## @param status_code: HTTP status code (default: 500)

	send_json_response(peer, {
		"is_error": true,
		"error": error_message
	}, status_code)

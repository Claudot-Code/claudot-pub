@tool
extends Node

## ClaudotTCP - Non-blocking TCP client for bridge communication
##
## Manages TCP connection to the bridge daemon with state machine,
## exponential backoff retry, circuit breaker, and message framing.

# Signals
signal connection_state_changed(new_state: ConnectionState)
signal message_received(message: Dictionary)
signal connection_error(error_message: String)

# Connection state enum
enum ConnectionState {
	DISCONNECTED,  ## Not connected, not trying to connect
	CONNECTING,    ## Attempting to establish connection
	CONNECTED,     ## Successfully connected
	ERROR,         ## Connection error occurred
	CIRCUIT_OPEN   ## Circuit breaker tripped after max retries
}

# Constants
const DEFAULT_HOST = "127.0.0.1"
const DEFAULT_PORT = 7777
const CONNECTION_TIMEOUT = 5.0
const MAX_RETRIES = 5
const BASE_RETRY_DELAY = 1.0
const MAX_RETRY_DELAY = 16.0

# Export configuration
@export var auto_connect: bool = false  ## Auto-connect to bridge on ready (for testing)

# State
var current_state: ConnectionState = ConnectionState.DISCONNECTED
var tcp_peer: StreamPeerTCP
var host: String = DEFAULT_HOST
var port: int = DEFAULT_PORT

# Retry logic
var retry_count: int = 0
var retry_timer: float = 0.0
var retry_delay: float = 0.0

# Connection timing
var connection_start_time: float = 0.0

# Message buffering
var receive_buffer: String = ""
var next_message_id: int = 0

# Preload message protocol
const MessageProtocol = preload("res://addons/claudot/network/message_protocol.gd")


func _ready() -> void:
	tcp_peer = StreamPeerTCP.new()
	set_process(true)
	_debug_log("TCP client autoload ready")

	# Auto-connect if enabled
	if auto_connect:
		_debug_log("Auto-connect enabled, attempting connection...")
		connect_to_bridge()


func _process(delta: float) -> void:
	if tcp_peer == null:
		return

	# Handle retry timer
	if retry_timer > 0.0:
		retry_timer -= delta
		if retry_timer <= 0.0:
			_attempt_connection()
		return

	# Poll the TCP connection (CRITICAL: must be called before get_status())
	tcp_peer.poll()

	# State machine
	match current_state:
		ConnectionState.CONNECTING:
			_process_connecting(delta)

		ConnectionState.CONNECTED:
			_process_connected()

		ConnectionState.DISCONNECTED, ConnectionState.ERROR, ConnectionState.CIRCUIT_OPEN:
			# Idle states - waiting for manual action or retry timer
			pass


func _process_connecting(delta: float) -> void:
	var status = tcp_peer.get_status()

	if status == StreamPeerTCP.STATUS_CONNECTED:
		# Connection established
		_set_state(ConnectionState.CONNECTED)
		retry_count = 0
		retry_delay = 0.0
		_debug_log("Connected to bridge at %s:%d" % [host, port])

	elif status == StreamPeerTCP.STATUS_ERROR:
		# Connection failed
		var error_msg = "Failed to connect to bridge at %s:%d" % [host, port]
		_handle_connection_failure(error_msg)

	else:
		# Still connecting - check timeout
		var elapsed = Time.get_ticks_msec() / 1000.0 - connection_start_time
		if elapsed > CONNECTION_TIMEOUT:
			var error_msg = "Connection timeout after %.1fs" % CONNECTION_TIMEOUT
			_handle_connection_failure(error_msg)


func _process_connected() -> void:
	var status = tcp_peer.get_status()

	if status != StreamPeerTCP.STATUS_CONNECTED:
		# Lost connection
		_handle_connection_failure("Connection lost")
		return

	# Read available data
	var available = tcp_peer.get_available_bytes()
	if available > 0:
		var result = tcp_peer.get_data(available)
		var error = result[0]
		var data = result[1]

		if error == OK:
			var text = data.get_string_from_utf8()
			receive_buffer += text

			# Parse complete messages
			var parsed = MessageProtocol.parse_buffer(receive_buffer)
			receive_buffer = parsed["remainder"]

			# Emit received messages (full Dictionary with all JSON-RPC fields)
			for msg in parsed["messages"]:
				_debug_log("Message received: %s" % JSON.stringify(msg))
				message_received.emit(msg)
		else:
			_handle_connection_failure("Read error: %d" % error)


func set_port(p: int) -> void:
	## Override the TCP port before connection. Called by the plugin to use the
	## per-project derived port from BridgeLauncher instead of DEFAULT_PORT.
	port = p


func connect_to_bridge(custom_host: String = "", custom_port: int = 0) -> void:
	if custom_host != "":
		host = custom_host
	if custom_port > 0:
		port = custom_port

	if current_state == ConnectionState.CIRCUIT_OPEN:
		return

	if current_state == ConnectionState.CONNECTING or current_state == ConnectionState.CONNECTED:
		return

	_attempt_connection()


func attempt_connect() -> void:
	## Public method to initiate connection (called from plugin or UI).
	connect_to_bridge()


func disconnect_from_bridge() -> void:
	if tcp_peer:
		tcp_peer.disconnect_from_host()

	receive_buffer = ""
	_set_state(ConnectionState.DISCONNECTED)
	_debug_log("Disconnected from bridge")


func send_message(method: String, params: Dictionary = {}) -> int:
	if current_state != ConnectionState.CONNECTED:
		return -1

	var msg_id = next_message_id
	next_message_id += 1

	var message = MessageProtocol.format_request(method, params, msg_id)
	_debug_log("Sending message: %s" % message)
	var bytes = message.to_utf8_buffer()
	var error = tcp_peer.put_data(bytes)

	if error != OK:
		push_error("[ClaudotTCP] Failed to send message: %d" % error)
		_handle_connection_failure("Send error: %d" % error)
		return -1

	return msg_id


func reset_circuit_breaker() -> void:
	if current_state == ConnectionState.CIRCUIT_OPEN:
		retry_count = 0
		retry_delay = 0.0
		retry_timer = 0.0
		_set_state(ConnectionState.DISCONNECTED)
		_debug_log("Circuit breaker reset")


func get_status_text() -> String:
	match current_state:
		ConnectionState.DISCONNECTED:
			return "Disconnected"
		ConnectionState.CONNECTING:
			return "Connecting..."
		ConnectionState.CONNECTED:
			return "Connected"
		ConnectionState.ERROR:
			return "Error (will retry in %.1fs)" % retry_timer
		ConnectionState.CIRCUIT_OPEN:
			return "Circuit breaker open (max retries exceeded)"
		_:
			return "Unknown"


func cleanup() -> void:
	disconnect_from_bridge()
	_debug_log("Cleanup complete")


# Private methods

func _attempt_connection() -> void:
	# Always create a fresh StreamPeerTCP instance.
	# Old peer in STATUS_ERROR causes Error 22 (ERR_ALREADY_IN_USE) on reconnect
	# because Godot's connect_to_host() enforces status == STATUS_NONE.
	tcp_peer = StreamPeerTCP.new()

	# Reset retry state for manual reconnect attempts
	retry_timer = 0.0

	_set_state(ConnectionState.CONNECTING)
	connection_start_time = Time.get_ticks_msec() / 1000.0

	var error = tcp_peer.connect_to_host(host, port)
	if error != OK:
		_handle_connection_failure("Failed to initiate connection: %d" % error)


func _handle_connection_failure(error_msg: String) -> void:
	_debug_log("Connection failure: %s" % error_msg)
	connection_error.emit(error_msg)

	retry_count += 1

	if retry_count >= MAX_RETRIES:
		# Trip circuit breaker
		_set_state(ConnectionState.CIRCUIT_OPEN)
	else:
		# Schedule retry with exponential backoff
		retry_delay = min(BASE_RETRY_DELAY * pow(2, retry_count - 1), MAX_RETRY_DELAY)
		retry_timer = retry_delay
		_set_state(ConnectionState.ERROR)
		_debug_log("Retry %d/%d in %.1fs" % [retry_count, MAX_RETRIES, retry_delay])


func _set_state(new_state: ConnectionState) -> void:
	if current_state != new_state:
		var old_state_name = ConnectionState.keys()[current_state]
		var new_state_name = ConnectionState.keys()[new_state]
		_debug_log("State transition: %s -> %s" % [old_state_name, new_state_name])
		current_state = new_state
		connection_state_changed.emit(new_state)


func _debug_log(_message: String) -> void:
	pass

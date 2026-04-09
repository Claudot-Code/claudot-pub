@tool
class_name ClaudotDebuggerPlugin extends EditorDebuggerPlugin

## Editor-side debugger plugin for capturing print() output from game execution.
## Receives messages via EngineDebugger message protocol and stores in ring buffer.

const MESSAGE_PREFIX = "claudot_output"

# Ring buffer storage (print)
var _buffer: Array = []
var _buffer_size: int = 1000
var _write_index: int = 0
var _message_count: int = 0

# Ring buffer storage (error)
var _error_buffer: Array = []
var _error_buffer_size: int = 1000
var _error_write_index: int = 0
var _error_message_count: int = 0

# Session tracking
var _active_sessions: Array[int] = []


func _has_capture(prefix: String) -> bool:
	return prefix == MESSAGE_PREFIX


func _capture(message: String, data: Array, session_id: int) -> bool:
	if message == "claudot_output:print":
		if data.size() >= 2:
			_append_to_buffer(data[0], str(data[1]))
		return true
	if message == "claudot_output:error":
		if data.size() >= 2:
			_append_to_error_buffer(data[0], str(data[1]))
		return true
	return false


func _setup_session(session_id: int) -> void:
	var session = get_session(session_id)
	if session:
		session.started.connect(_on_session_started.bind(session_id))
		session.stopped.connect(_on_session_stopped.bind(session_id))


func _on_session_started(session_id: int) -> void:
	if session_id not in _active_sessions:
		_active_sessions.append(session_id)


func _on_session_stopped(session_id: int) -> void:
	_active_sessions.erase(session_id)


func _append_to_buffer(timestamp: float, text: String) -> void:
	var entry = {"timestamp": timestamp, "text": text}

	if _message_count < _buffer_size:
		_buffer.append(entry)
		_message_count += 1
	else:
		_buffer[_write_index] = entry

	_write_index = (_write_index + 1) % _buffer_size


func get_recent_output(max_lines: int = 100) -> Array:
	"""Return the most recent max_lines messages from buffer."""
	if _message_count == 0:
		return []

	var count = mini(_message_count, max_lines)
	var result: Array = []
	result.resize(count)

	var read_start: int
	if _message_count < _buffer_size:
		read_start = _message_count - count
	else:
		read_start = (_write_index - count + _buffer_size) % _buffer_size

	for i in range(count):
		var index = (read_start + i) % _buffer_size
		result[i] = _buffer[index]

	return result


func get_buffer_size() -> int:
	return _message_count


func clear_buffer() -> void:
	_buffer.clear()
	_write_index = 0
	_message_count = 0


func _append_to_error_buffer(timestamp: float, text: String) -> void:
	var entry = {"timestamp": timestamp, "text": text}

	if _error_message_count < _error_buffer_size:
		_error_buffer.append(entry)
		_error_message_count += 1
	else:
		_error_buffer[_error_write_index] = entry

	_error_write_index = (_error_write_index + 1) % _error_buffer_size


func get_error_output(max_lines: int = 100) -> Array:
	"""Return the most recent max_lines error messages from error buffer."""
	if _error_message_count == 0:
		return []

	var count = mini(_error_message_count, max_lines)
	var result: Array = []
	result.resize(count)

	var read_start: int
	if _error_message_count < _error_buffer_size:
		read_start = _error_message_count - count
	else:
		read_start = (_error_write_index - count + _error_buffer_size) % _error_buffer_size

	for i in range(count):
		var index = (read_start + i) % _error_buffer_size
		result[i] = _error_buffer[index]

	return result


func get_error_buffer_size() -> int:
	return _error_message_count

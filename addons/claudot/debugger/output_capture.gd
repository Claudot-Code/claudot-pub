extends Node

## OutputCapture - Game-side autoload for capturing print statements.
## Sends print output to editor via debugger messages.
## Must be registered as autoload to be available during test execution.

const MESSAGE_PREFIX = "claudot_output"

var _debugger_active: bool = false


func _ready() -> void:
	if not EngineDebugger.is_active():
		return

	_debugger_active = true
	EngineDebugger.register_message_capture(MESSAGE_PREFIX, _on_message_received)


func capture_print(text: String) -> void:
	## Send print output to editor debugger buffer.
	## Call this instead of (or in addition to) print() to capture output.
	if not _debugger_active:
		return

	var timestamp = Time.get_unix_time_from_system()
	EngineDebugger.send_message(MESSAGE_PREFIX + ":print", [timestamp, text])


func capture_error(text: String) -> void:
	## Send error output to editor debugger buffer.
	## Call this to capture errors separately from print output.
	if not _debugger_active:
		return

	var timestamp = Time.get_unix_time_from_system()
	EngineDebugger.send_message(MESSAGE_PREFIX + ":error", [timestamp, text])


func _on_message_received(message: String, data: Array) -> bool:
	# Required callback for register_message_capture, even if only sending
	return true

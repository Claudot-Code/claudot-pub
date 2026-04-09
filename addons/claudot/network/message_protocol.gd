extends RefCounted

## MessageProtocol - JSON-RPC 2.0 message framing
##
## Handles formatting and parsing of newline-delimited JSON-RPC 2.0 messages
## for TCP communication with the bridge daemon.

## Format a JSON-RPC 2.0 request
## @param method: The method name to call
## @param params: Dictionary of parameters
## @param id: Request ID (integer)
## @return: JSON string with newline terminator
static func format_request(method: String, params: Dictionary, id: int) -> String:
	var request = {
		"jsonrpc": "2.0",
		"method": method,
		"params": params,
		"id": id
	}
	return JSON.stringify(request) + "\n"


## Format a JSON-RPC 2.0 success response
## @param result: The result value (any JSON-serializable type)
## @param id: Request ID (integer or null)
## @return: JSON string with newline terminator
static func format_response(result, id) -> String:
	var response = {
		"jsonrpc": "2.0",
		"result": result,
		"id": id
	}
	return JSON.stringify(response) + "\n"


## Format a JSON-RPC 2.0 error response
## @param code: Error code (integer)
## @param message: Error message (string)
## @param id: Request ID (integer or null)
## @return: JSON string with newline terminator
static func format_error(code: int, message: String, id) -> String:
	var response = {
		"jsonrpc": "2.0",
		"error": {
			"code": code,
			"message": message
		},
		"id": id
	}
	return JSON.stringify(response) + "\n"


## Parse accumulated buffer and extract complete messages
## @param buffer: Accumulated string buffer
## @return: Dictionary with "messages" (Array[Dictionary]) and "remainder" (String)
static func parse_buffer(buffer: String) -> Dictionary:
	var messages: Array[Dictionary] = []
	var lines = buffer.split("\n", false)  # false = don't include empty strings
	var remainder = ""

	# Check if buffer ends with newline (complete message) or not (partial)
	var ends_with_newline = buffer.ends_with("\n")

	# Process complete lines (all but potentially the last)
	var line_count = lines.size()
	var complete_line_count = line_count if ends_with_newline else max(0, line_count - 1)

	for i in range(complete_line_count):
		var line = lines[i].strip_edges()
		if line.is_empty():
			continue

		var json = JSON.new()
		var error = json.parse(line)

		if error == OK:
			var data = json.get_data()
			if typeof(data) == TYPE_DICTIONARY:
				messages.append(data)

	# Store incomplete line as remainder
	if not ends_with_newline and line_count > 0:
		remainder = lines[-1]

	return {
		"messages": messages,
		"remainder": remainder
	}

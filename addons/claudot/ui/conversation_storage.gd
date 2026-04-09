extends RefCounted

## ConversationStorage - Static utility for persisting chat conversation history
##
## Saves/loads conversation to user:// directory using ConfigFile.
## Each Godot project gets its own conversation file, scoped by a hash of the
## project root path. This prevents conversation history from bleeding across
## projects when the same Godot editor switches between them.
##
## Caps stored messages to prevent unbounded file growth.

# Constants
# DEPRECATED: Legacy single-file path — no longer used. Retained for reference only.
const SAVE_PATH = "user://claudot_conversation.cfg"
const MAX_MESSAGES = 200


static func _get_save_path() -> String:
	## Return a project-scoped save path derived from the project root directory.
	## Uses hash() of the absolute project root path as a short unique ID.
	## Collision risk is negligible for the number of projects a developer would have.
	var project_root: String = ProjectSettings.globalize_path("res://")
	var project_hash: int = hash(project_root)
	# Use %x for lowercase hex — more readable than decimal in a filename
	return "user://claudot_conversations/project_%x.cfg" % project_hash


static func save_conversation(messages: Array) -> void:
	## Save conversation to ConfigFile, capped at MAX_MESSAGES.
	var save_path := _get_save_path()

	# Ensure the conversations directory exists before writing
	var dir_path := save_path.get_base_dir()
	DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path(dir_path))

	var config = ConfigFile.new()

	# Determine how many messages to save (keep only the last MAX_MESSAGES)
	var message_count = min(messages.size(), MAX_MESSAGES)
	var start_index = messages.size() - message_count

	# Store metadata
	config.set_value("meta", "count", message_count)

	# Store each message
	for i in range(message_count):
		var msg_index = start_index + i
		var msg = messages[msg_index]
		config.set_value("messages", "msg_%d_sender" % i, msg.sender)
		config.set_value("messages", "msg_%d_content" % i, msg.content)

	# Save to disk
	config.save(save_path)


static func load_conversation() -> Array:
	## Load conversation from ConfigFile. Returns empty array if file doesn't exist.
	var config = ConfigFile.new()

	# Try to load the file
	var error = config.load(_get_save_path())
	if error != OK:
		# File doesn't exist yet (first run) or read error
		return []

	# Read message count
	var count = config.get_value("meta", "count", 0)

	# Load each message
	var messages: Array = []
	for i in range(count):
		var sender = config.get_value("messages", "msg_%d_sender" % i, "")
		var content = config.get_value("messages", "msg_%d_content" % i, "")

		if sender != "" and content != "":
			messages.append({"sender": sender, "content": content})

	return messages


static func clear_conversation() -> void:
	## Delete the conversation file for the current project.
	DirAccess.remove_absolute(_get_save_path())
	# Silently ignore if file doesn't exist

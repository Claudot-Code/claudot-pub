@tool
extends Node

## SceneTools - Implements MCP scene inspection tools
##
## Provides read-only tools for inspecting editor state and scene tree.
## All tools return structured Dictionary with success flag and timestamp.

# Dependencies
const PropertyConverter = preload("res://addons/claudot/mcp/property_converter.gd")

# References
var plugin: EditorPlugin = null

# Screenshot encoding state (shared with WorkerThreadPool tasks)
var _jpeg_encode_result: String = ""

# Interactive input state
signal user_input_requested(prompt: String, input_type: String, options: Array, labels: Array)
signal user_input_response_received(answer: Dictionary)

var _pending_input_request: bool = false
var _buffered_answer: Dictionary = {}


func setup(editor_plugin: EditorPlugin) -> void:
	## Initialize with plugin reference for EditorInterface access.
	plugin = editor_plugin


func get_node_property(params: Dictionary) -> Dictionary:
	## Read a property value from a node in the scene tree.
	##
	## @param params: {"node_path": String, "property_name": String}
	## @return: {"success": bool, "value": Variant, ...} or error

	var node_path = params.get("node_path", "")
	var property_name = params.get("property_name", "")

	# Validate parameters
	if node_path.is_empty():
		return _error("node_path parameter is required")

	if property_name.is_empty():
		return _error("property_name parameter is required")

	# Resolve node
	var node = _resolve_node(node_path)
	if node == null:
		return _error("Node not found: %s" % node_path)

	# Check property exists
	if not property_name in node:
		return _error("Property '%s' does not exist on node %s" % [property_name, node_path])

	# Read property value
	var value = node.get(property_name)

	# Convert to JSON-safe representation
	var json_value = PropertyConverter.variant_to_json(value)

	return {
		"success": true,
		"value": json_value,
		"node_path": str(node.get_path()),
		"property_name": property_name,
		"timestamp": Time.get_unix_time_from_system()
	}


func get_scene_state(params: Dictionary) -> Dictionary:
	## Snapshot the entire scene tree with node properties.
	##
	## @param params: {"max_depth": int} (default 5)
	## @return: {"success": bool, "tree": Dictionary, ...} or error

	var max_depth = params.get("max_depth", 5)

	# Get scene root
	var scene_root = _get_scene_root()
	if scene_root == null:
		return _error("No scene is currently being edited")

	# Traverse scene tree (pass scene_root as reference for relative paths)
	var tree = {}
	var node_count = _traverse_scene(scene_root, tree, 0, max_depth, scene_root)

	return {
		"success": true,
		"scene_path": scene_root.scene_file_path if scene_root.scene_file_path else "Untitled",
		"node_count": node_count,
		"tree": tree,
		"timestamp": Time.get_unix_time_from_system()
	}


func get_editor_context(params: Dictionary) -> Dictionary:
	## Get current editor state: scene path, selection, recent errors.
	##
	## @param params: {} (no required params)
	## @return: {"success": bool, "scene_path": String, "selection": Array, ...}

	var editor_interface = plugin.get_editor_interface()
	var context = {
		"success": true,
		"timestamp": Time.get_unix_time_from_system()
	}

	# Current scene path
	var scene_root = editor_interface.get_edited_scene_root()
	if scene_root:
		context["scene_path"] = scene_root.scene_file_path if scene_root.scene_file_path else "Untitled"
	else:
		context["scene_path"] = null

	# Selected nodes
	var selection = editor_interface.get_selection()
	var selected_nodes = selection.get_selected_nodes()
	context["selection"] = []

	for node in selected_nodes:
		context["selection"].append({
			"path": str(node.get_path()),
			"type": node.get_class(),
			"name": node.name
		})

	# Recent errors - placeholder (LOW CONFIDENCE per research)
	# No built-in API for console error access from EditorPlugin
	context["recent_errors"] = []

	return context


# Private helper methods

func _resolve_node(node_path: String) -> Node:
	## Resolve node path to Node reference.
	##
	## Handles both absolute ("/root/NodeName") and relative ("NodeName") paths.

	var scene_root = _get_scene_root()
	if not scene_root:
		return null

	# Handle root references
	if node_path == "/root" or node_path.is_empty():
		return scene_root

	# Strip "/root/" prefix if present
	if node_path.begins_with("/root/"):
		node_path = node_path.substr(6)
	elif node_path.begins_with("/"):
		node_path = node_path.substr(1)

	# Resolve relative to scene root
	return scene_root.get_node_or_null(node_path)


func _get_scene_root() -> Node:
	## Get the currently edited scene root.
	if plugin == null:
		return null

	var editor_interface = plugin.get_editor_interface()
	return editor_interface.get_edited_scene_root()


func _traverse_scene(node: Node, snapshot: Dictionary, depth: int, max_depth: int, scene_root: Node = null) -> int:
	## Recursively traverse scene tree and collect node data.
	##
	## @param scene_root: Reference node for calculating relative paths
	## @return: Total node count traversed

	if depth > max_depth:
		return 0

	# Use scene-relative path if scene_root provided, otherwise absolute path
	var node_path: String
	if scene_root != null:
		if node == scene_root:
			node_path = "/root"
		else:
			# Get path relative to scene root
			node_path = "/root/" + str(scene_root.get_path_to(node))
	else:
		node_path = str(node.get_path())

	var node_count = 1

	# Collect node info
	snapshot[node_path] = {
		"type": node.get_class(),
		"name": node.name,
		"properties": _extract_key_properties(node)
	}

	# Add script info if present
	var script = node.get_script()
	if script:
		snapshot[node_path]["script"] = script.resource_path

	# Recursively traverse children
	for child in node.get_children():
		node_count += _traverse_scene(child, snapshot, depth + 1, max_depth, scene_root)

	return node_count


func _extract_key_properties(node: Node) -> Dictionary:
	## Extract commonly useful properties from a node.
	##
	## Focuses on transform, visibility, and other frequently accessed properties.

	var props = {}

	# Position, rotation, scale for 2D nodes
	if node is Node2D:
		props["position"] = PropertyConverter.variant_to_json(node.position)
		props["rotation"] = node.rotation
		props["scale"] = PropertyConverter.variant_to_json(node.scale)

	# Position, rotation, scale for 3D nodes
	if node is Node3D:
		props["position"] = PropertyConverter.variant_to_json(node.position)
		props["rotation"] = PropertyConverter.variant_to_json(node.rotation)
		props["scale"] = PropertyConverter.variant_to_json(node.scale)

	# Visibility for canvas items
	if node is CanvasItem:
		props["visible"] = node.visible
		props["modulate"] = PropertyConverter.variant_to_json(node.modulate)

	# Visibility for 3D nodes
	if node is Node3D:
		props["visible"] = node.visible

	return props


func set_node_property(params: Dictionary) -> Dictionary:
	## Set a property value on a node in the scene tree.
	##
	## @param params: {"node_path": String, "property_name": String, "value": Variant}
	## @return: {"success": bool, "old_value": Variant, "new_value": Variant, ...} or error

	var node_path = params.get("node_path", "")
	var property_name = params.get("property_name", "")
	var value = params.get("value")

	# Validate parameters
	if node_path.is_empty():
		return _error("node_path parameter is required")

	if property_name.is_empty():
		return _error("property_name parameter is required")

	# Resolve node
	var node = _resolve_node(node_path)
	if node == null:
		return _error("Node not found: %s" % node_path)

	# Check property exists
	if not property_name in node:
		return _error("Property '%s' does not exist on node %s" % [property_name, node_path])

	# Get property type from node's property list
	var target_type = -1
	for prop in node.get_property_list():
		if prop.name == property_name:
			target_type = prop.type
			break

	# Convert incoming value to appropriate Godot type
	var new_value = PropertyConverter.json_to_variant(value, target_type)

	# Get old value for response
	var old_value = node.get(property_name)

	# Apply change directly (no undo integration)
	node.set(property_name, new_value)

	return {
		"success": true,
		"old_value": PropertyConverter.variant_to_json(old_value),
		"new_value": PropertyConverter.variant_to_json(new_value),
		"node_path": str(node.get_path()),
		"property_name": property_name,
		"timestamp": Time.get_unix_time_from_system()
	}


func create_node(params: Dictionary) -> Dictionary:
	## Create a new node in the scene tree.
	##
	## @param params: {"parent_path": String, "node_type": String, "node_name": String}
	## @return: {"success": bool, "node_path": String, ...} or error

	var parent_path = params.get("parent_path", "")
	var node_type = params.get("node_type", "")
	var node_name = params.get("node_name", "")

	# Validate parameters
	if parent_path.is_empty():
		return _error("parent_path parameter is required")

	if node_type.is_empty():
		return _error("node_type parameter is required")

	if node_name.is_empty():
		return _error("node_name parameter is required")

	# Validate node type exists and can be instantiated
	if not ClassDB.class_exists(node_type):
		return _error("Invalid node type: %s" % node_type)

	if not ClassDB.can_instantiate(node_type):
		return _error("Cannot instantiate node type: %s" % node_type)

	# Resolve parent node
	var parent = _resolve_node(parent_path)
	if parent == null:
		return _error("Parent node not found: %s" % parent_path)

	# Get scene root
	var scene_root = _get_scene_root()
	if scene_root == null:
		return _error("No scene is currently being edited")

	# Create the node
	var node = ClassDB.instantiate(node_type)
	if node == null:
		return _error("Failed to instantiate node of type: %s" % node_type)

	node.name = node_name

	# Add node directly (no undo integration)
	parent.add_child(node)
	node.set_owner(scene_root)

	# Return scene-relative path
	var absolute_path = str(node.get_path())
	var scene_root_path = str(scene_root.get_path())
	var relative_path = absolute_path
	if absolute_path.begins_with(scene_root_path):
		relative_path = "/root" + absolute_path.substr(scene_root_path.length())

	return {
		"success": true,
		"node_path": relative_path,
		"node_type": node_type,
		"node_name": node_name,
		"parent_path": str(parent.get_path()),
		"timestamp": Time.get_unix_time_from_system()
	}


func delete_node(params: Dictionary) -> Dictionary:
	## Delete a node from the scene tree.
	##
	## @param params: {"node_path": String}
	## @return: {"success": bool, "deleted_path": String, ...} or error

	var node_path = params.get("node_path", "")

	# Validate parameters
	if node_path.is_empty():
		return _error("node_path parameter is required")

	# Resolve node
	var node = _resolve_node(node_path)
	if node == null:
		return _error("Node not found: %s" % node_path)

	# Get scene root and validate not deleting root
	var scene_root = _get_scene_root()
	if scene_root == null:
		return _error("No scene is currently being edited")

	if node == scene_root:
		return _error("Cannot delete the scene root node")

	# Get parent
	var parent = node.get_parent()
	if parent == null:
		return _error("Node has no parent: %s" % node_path)

	# Get scene-relative path before deletion
	var absolute_path = str(node.get_path())
	var scene_root_path = str(scene_root.get_path())
	var deleted_path = absolute_path
	if absolute_path.begins_with(scene_root_path):
		deleted_path = "/root" + absolute_path.substr(scene_root_path.length())

	# Delete node directly (no undo integration)
	parent.remove_child(node)
	node.queue_free()

	return {
		"success": true,
		"deleted_path": deleted_path,
		"timestamp": Time.get_unix_time_from_system()
	}


func reparent_node(params: Dictionary) -> Dictionary:
	## Move a node to a different parent.
	##
	## @param params: {"node_path": String, "new_parent_path": String}
	## @return: {"success": bool, "node_path": String, ...} or error

	var node_path = params.get("node_path", "")
	var new_parent_path = params.get("new_parent_path", "")

	# Validate parameters
	if node_path.is_empty():
		return _error("node_path parameter is required")

	if new_parent_path.is_empty():
		return _error("new_parent_path parameter is required")

	# Resolve nodes
	var node = _resolve_node(node_path)
	if node == null:
		return _error("Node not found: %s" % node_path)

	var new_parent = _resolve_node(new_parent_path)
	if new_parent == null:
		return _error("New parent node not found: %s" % new_parent_path)

	# Get scene root
	var scene_root = _get_scene_root()
	if scene_root == null:
		return _error("No scene is currently being edited")

	# Validate not creating a cycle
	if _is_ancestor_of(node, new_parent):
		return _error("Cannot reparent: would create a cycle (new parent is descendant of node)")

	# Get current parent
	var old_parent = node.get_parent()
	if old_parent == null:
		return _error("Node has no parent: %s" % node_path)

	# Reparent directly (no undo integration)
	old_parent.remove_child(node)
	new_parent.add_child(node)
	node.set_owner(scene_root)

	# Return scene-relative paths
	var scene_root_path = str(scene_root.get_path())
	var absolute_node_path = str(node.get_path())
	var absolute_new_parent_path = str(new_parent.get_path())
	var absolute_old_parent_path = str(old_parent.get_path())

	var relative_node_path = absolute_node_path
	if absolute_node_path.begins_with(scene_root_path):
		relative_node_path = "/root" + absolute_node_path.substr(scene_root_path.length())

	var relative_new_parent = absolute_new_parent_path
	if absolute_new_parent_path.begins_with(scene_root_path):
		relative_new_parent = "/root" + absolute_new_parent_path.substr(scene_root_path.length())

	var relative_old_parent = absolute_old_parent_path
	if absolute_old_parent_path.begins_with(scene_root_path):
		relative_old_parent = "/root" + absolute_old_parent_path.substr(scene_root_path.length())

	return {
		"success": true,
		"node_path": relative_node_path,
		"old_parent": relative_old_parent,
		"new_parent": relative_new_parent,
		"timestamp": Time.get_unix_time_from_system()
	}


func _is_ancestor_of(potential_ancestor: Node, node: Node) -> bool:
	## Check if potential_ancestor is an ancestor of node (prevents cycles).
	var current = node.get_parent()
	while current != null:
		if current == potential_ancestor:
			return true
		current = current.get_parent()
	return false


func _notify_error(_message: String) -> void:
	pass


func search_files(params: Dictionary) -> Dictionary:
	## Search res:// filesystem for files matching extension/pattern filters.
	##
	## @param params: {
	##   "pattern": String (optional) - Wildcard pattern like "*.gd" or "player*"
	##   "extensions": Array[String] (optional) - Extensions like [".gd", ".tscn"]
	##   "max_results": int (optional) - Limit results (default: 100, max: 1000)
	## }
	## @return: {"success": bool, "files": Array[Dictionary], ...} or error
	##
	## Each file entry contains:
	##   {
	##     "path": "res://scripts/player.gd",
	##     "type": "GDScript",
	##     "name": "player.gd",
	##     "directory": "res://scripts"
	##   }

	# Extract parameters with defaults
	var pattern = params.get("pattern", "")
	var extensions = params.get("extensions", [])
	var max_results = params.get("max_results", 100)

	# Validate and clamp max_results
	if max_results <= 0:
		max_results = 100
	max_results = mini(max_results, 1000)  # Hard cap

	# Validate extensions array
	if not extensions is Array:
		return _error("extensions must be an array of strings")

	# Get EditorFileSystem singleton
	var editor_interface = plugin.get_editor_interface()
	var filesystem = editor_interface.get_resource_file_system()

	if not filesystem:
		return _error("EditorFileSystem not available - ensure running in Godot editor")

	# Get root directory
	var root_dir = filesystem.get_filesystem()
	if not root_dir:
		return _error("Could not access filesystem root")

	# Collect matching files
	var results = []
	_traverse_for_search(root_dir, pattern, extensions, results, max_results)

	# Build response
	var response = {
		"success": true,
		"files": results,
		"count": results.size(),
		"truncated": results.size() >= max_results,
		"timestamp": Time.get_unix_time_from_system()
	}

	if response["truncated"]:
		response["message"] = "Results limited to %d files. Use more specific pattern or extensions to narrow search." % max_results

	return response


func _traverse_for_search(
	dir: EditorFileSystemDirectory,
	pattern: String,
	extensions: Array,
	results: Array,
	max_results: int
) -> void:
	## Recursively traverse directory collecting matching files.
	##
	## Applies filters in order:
	##   1. Extension filter (cheap, eliminates most non-matches)
	##   2. Pattern filter (more expensive wildcard matching)

	# Early exit if result limit reached
	if results.size() >= max_results:
		return

	# Process files in current directory
	var file_count = dir.get_file_count()
	for i in range(file_count):
		if results.size() >= max_results:
			break

		var file_path = dir.get_file_path(i)
		var file_type = dir.get_file_type(i)

		# Filter 1: Extension (fast check first)
		if extensions.size() > 0:
			var file_ext = file_path.get_extension()
			var matches_ext = false
			for ext in extensions:
				# Normalize: handle both ".gd" and "gd"
				var clean_ext = ext.trim_prefix(".")
				if file_ext == clean_ext:
					matches_ext = true
					break
			if not matches_ext:
				continue

		# Filter 2: Pattern (slower, but fewer files reach here)
		if not pattern.is_empty():
			var file_name = file_path.get_file()
			# Use matchn for case-insensitive matching
			if not file_name.matchn(pattern):
				continue

		# File matches all filters - add to results
		results.append({
			"path": file_path,
			"type": file_type,
			"name": file_path.get_file(),
			"directory": file_path.get_base_dir()
		})

	# Recurse into subdirectories
	var subdir_count = dir.get_subdir_count()
	for i in range(subdir_count):
		if results.size() >= max_results:
			break
		var subdir = dir.get_subdir(i)
		_traverse_for_search(subdir, pattern, extensions, results, max_results)


func capture_screenshot(params: Dictionary) -> Dictionary:
	## Capture screenshot from editor or game viewport.
	##
	## @param params: {
	##   "viewport_type": String - "2d_editor", "3d_editor", or "game"
	## }
	## @return: {"success": bool, "image_data": String (base64 JPEG)} or error
	##
	## Returns 800x600 JPEG screenshot with letterbox/pillarbox aspect ratio
	## preservation. Uses async JPEG encoding to prevent UI freeze.

	var viewport_type = params.get("viewport_type", "2d_editor")

	# Validate viewport type
	var valid_types = ["2d_editor", "3d_editor", "game"]
	if not viewport_type in valid_types:
		return _error("Invalid viewport_type '%s'. Must be one of: %s" % [viewport_type, str(valid_types)])

	# Get editor interface
	var editor_interface = plugin.get_editor_interface()
	if not editor_interface:
		return _error("EditorInterface not available")

	# Get viewport reference based on type
	var viewport: Viewport = null

	match viewport_type:
		"2d_editor":
			viewport = editor_interface.get_editor_viewport_2d()
			if not viewport:
				return _error("2D editor viewport not available")

		"3d_editor":
			# Get first 3D viewport (index 0)
			viewport = editor_interface.get_editor_viewport_3d(0)
			if not viewport:
				return _error("3D editor viewport not available. Ensure a 3D scene is open or use '2d_editor' viewport.")

		"game":
			# Validate game is running
			if not editor_interface.is_playing_scene():
				return _error("Game viewport not available - game is not running. Start game with F5 first.")

			# Get running game viewport
			var scene_tree = get_tree()
			if not scene_tree:
				return _error("SceneTree not available")

			viewport = scene_tree.root
			if not viewport:
				return _error("Could not access game viewport")

	# Wait one frame for viewport to finish rendering
	await get_tree().process_frame

	# Capture viewport texture
	var viewport_texture = viewport.get_texture()
	if not viewport_texture:
		return _error("Failed to get viewport texture")

	var image = viewport_texture.get_image()
	if not image:
		return _error("Failed to capture viewport image")

	# Store original dimensions
	var orig_width = image.get_width()
	var orig_height = image.get_height()

	# Resize to 800x600 letterbox
	var resized = _resize_letterbox(image, 800, 600)

	# Encode as JPEG async (prevents UI freeze)
	var base64_data = await _encode_jpeg_async(resized, 0.80)

	return {
		"success": true,
		"image_data": base64_data,
		"viewport_type": viewport_type,
		"original_size": {"width": orig_width, "height": orig_height},
		"encoded_size": {"width": resized.get_width(), "height": resized.get_height()},
		"format": "jpeg",
		"quality": 0.80
	}


func _resize_letterbox(image: Image, max_width: int, max_height: int) -> Image:
	## Resize image to fit within max dimensions, preserving aspect ratio.
	## Adds black letterbox/pillarbox bars if needed.

	var orig_width = image.get_width()
	var orig_height = image.get_height()

	# Check if resize needed
	if orig_width <= max_width and orig_height <= max_height:
		return image

	# Calculate aspect ratios
	var aspect_ratio = float(orig_width) / float(orig_height)
	var target_aspect = float(max_width) / float(max_height)

	var new_width: int
	var new_height: int

	# Determine letterbox vs pillarbox
	if aspect_ratio > target_aspect:
		# Wider - fit to width
		new_width = max_width
		new_height = int(max_width / aspect_ratio)
	else:
		# Taller - fit to height
		new_height = max_height
		new_width = int(max_height * aspect_ratio)

	# Create resized image
	var resized = image.duplicate()
	resized.resize(new_width, new_height, Image.INTERPOLATE_LANCZOS)

	# Create letterboxed canvas if needed
	if new_width < max_width or new_height < max_height:
		var canvas = Image.create(max_width, max_height, false, Image.FORMAT_RGB8)
		canvas.fill(Color.BLACK)

		# Center the resized image
		var x_offset = (max_width - new_width) / 2
		var y_offset = (max_height - new_height) / 2
		canvas.blit_rect(resized, Rect2i(0, 0, new_width, new_height), Vector2i(x_offset, y_offset))

		return canvas

	return resized


func _encode_jpeg_async(image: Image, quality: float) -> String:
	## Encode image as JPEG on background thread, return base64 string.
	## Uses WorkerThreadPool to prevent UI freeze during encoding.

	# Clamp quality to valid range
	quality = clamp(quality, 0.01, 1.0)
	_jpeg_encode_result = ""

	# Create task callable that writes result to member var
	var encode_task = func():
		var jpeg_buffer = image.save_jpg_to_buffer(quality)
		_jpeg_encode_result = Marshalls.raw_to_base64(jpeg_buffer)

	# Submit to worker thread pool (offloads CPU-intensive JPEG encoding)
	var task_id = WorkerThreadPool.add_task(encode_task)

	# Poll for completion -- await yields to keep editor UI responsive
	while not WorkerThreadPool.is_task_completed(task_id):
		await get_tree().process_frame

	# Wait for task cleanup (returns Error, not the result)
	WorkerThreadPool.wait_for_task_completion(task_id)

	return _jpeg_encode_result


func get_debugger_output(params: Dictionary) -> Dictionary:
	## Retrieve recent print() output from debugger output buffer.
	##
	## @param params: {
	##   "max_lines": int (optional) - Maximum messages to return (default: 100, max: 1000)
	## }
	## @return: {"success": bool, "messages": Array, "count": int, ...} or error

	var max_lines = params.get("max_lines", 100)
	max_lines = clampi(max_lines, 1, 1000)

	# Get debugger plugin reference from parent plugin
	var debugger_plugin = plugin.debugger_plugin
	if debugger_plugin == null:
		return _error("Debugger output capture not available")

	var messages = debugger_plugin.get_recent_output(max_lines)

	return {
		"success": true,
		"messages": messages,
		"count": messages.size(),
		"buffer_total": debugger_plugin.get_buffer_size(),
		"timestamp": Time.get_unix_time_from_system()
	}


func get_debugger_errors(params: Dictionary) -> Dictionary:
	## Retrieve recent captured errors from debugger error buffer.
	##
	## @param params: {
	##   "max_lines": int (optional) - Maximum errors to return (default: 100, max: 1000)
	## }
	## @return: {"success": bool, "errors": Array, "count": int, ...} or error

	var max_lines = params.get("max_lines", 100)
	max_lines = clampi(max_lines, 1, 1000)

	# Get debugger plugin reference from parent plugin
	var debugger_plugin = plugin.debugger_plugin
	if debugger_plugin == null:
		return _error("Debugger error capture not available")

	var errors = debugger_plugin.get_error_output(max_lines)

	return {
		"success": true,
		"errors": errors,
		"count": errors.size(),
		"buffer_total": debugger_plugin.get_error_buffer_size(),
		"timestamp": Time.get_unix_time_from_system()
	}


func get_node_script(params: Dictionary) -> Dictionary:
	## Read GDScript source code attached to a node in the scene tree.
	##
	## @param params: {"node_path": String}
	## @return: {"success": bool, "has_script": bool, "source_code": String, ...} or error

	var node_path = params.get("node_path", "")

	# Validate parameters
	if node_path.is_empty():
		return _error("node_path parameter is required")

	# Resolve node
	var node = _resolve_node(node_path)
	if node == null:
		return _error("Node not found: %s" % node_path)

	# Check if a script is attached
	var script = node.get_script()
	if script == null:
		return {
			"success": true,
			"has_script": false,
			"node_path": str(node.get_path()),
			"timestamp": Time.get_unix_time_from_system()
		}

	# Only GDScript is supported
	if not script is GDScript:
		return _error("Script is not GDScript (C# scripts not supported)")

	# Determine script path and whether it is built-in (embedded in .tscn)
	var path: String = script.resource_path
	var is_built_in: bool = path == "" or "::" in path

	# Read source code (works for both external .gd files and built-in scripts)
	var source: String = script.source_code

	return {
		"success": true,
		"has_script": true,
		"node_path": str(node.get_path()),
		"script_path": path,
		"is_built_in": is_built_in,
		"source_code": source,
		"line_count": source.count("\n") + 1,
		"timestamp": Time.get_unix_time_from_system()
	}


func request_user_input(params: Dictionary) -> Dictionary:
	## Request structured input from the developer via an overlay widget in the Claudot panel.
	##
	## Blocks until the developer responds (emits user_input_requested signal, then awaits
	## user_input_response_received). Plan 02 wires the UI widget to emit the response signal.
	##
	## @param params: {
	##   "type": String   - Input type: "radio", "checkbox", "confirm", or "text"
	##   "prompt": String - Question to display to the developer
	##   "options": Array (optional) - Choices for radio/checkbox types
	##   "labels": Array  (optional) - Custom button labels for confirm type
	## }
	## @return: {"success": true, "answer": Dictionary, "timestamp": float} or error

	var input_type: String = params.get("type", "")
	var prompt: String = params.get("prompt", "")
	var options: Array = params.get("options", [])
	var labels: Array = params.get("labels", [])

	# Validate type
	var valid_types = ["radio", "checkbox", "confirm", "text"]
	if not input_type in valid_types:
		return _error("Invalid input type '%s'. Must be one of: %s" % [input_type, str(valid_types)])

	# Validate prompt
	if prompt.is_empty():
		return _error("prompt parameter is required and must not be empty")

	# Validate options required for radio/checkbox
	if input_type in ["radio", "checkbox"] and options.is_empty():
		return _error("options array is required for input type '%s'" % input_type)

	# Concurrency guard
	if _pending_input_request:
		return _error("Another input request is already pending")

	_pending_input_request = true
	_buffered_answer = {}

	# Signal the UI widget (wired in Plan 02)
	user_input_requested.emit(prompt, input_type, options, labels)

	# Await developer response
	var answer: Dictionary = await user_input_response_received

	_pending_input_request = false
	_buffered_answer = answer  # Buffer in case HTTP already timed out

	return {
		"success": true,
		"answer": answer,
		"timestamp": Time.get_unix_time_from_system()
	}


func _on_user_input_received(answer: Dictionary) -> void:
	## Called by conversation_tab (wired in claudot_plugin.gd in Plan 02) when the
	## developer submits an answer through the interactive input widget.
	##
	## @param answer: Dictionary with the developer's response
	user_input_response_received.emit(answer)


func get_pending_input_answer(params: Dictionary) -> Dictionary:
	## Retrieve a buffered developer answer from a previous request_user_input call.
	##
	## Used when the original HTTP request timed out before the developer responded.
	## Call this after the developer has had time to answer.
	##
	## @param params: {} (no required params)
	## @return: {"success": true, "has_answer": bool, "answer": Dictionary (if has_answer), ...}

	if not _buffered_answer.is_empty():
		var answer = _buffered_answer.duplicate()
		_buffered_answer = {}
		return {
			"success": true,
			"has_answer": true,
			"answer": answer,
			"timestamp": Time.get_unix_time_from_system()
		}

	return {
		"success": true,
		"has_answer": false,
		"timestamp": Time.get_unix_time_from_system()
	}


func run_scene(params: Dictionary) -> Dictionary:
	## Launch a game scene in the editor (equivalent to pressing F5 or F6).
	##
	## @param params: {"scene_path": String} (optional — empty means main scene)
	## @return: {"success": bool, "scene": String, "timestamp": float} or error

	var scene_path: String = params.get("scene_path", "")
	var editor_interface = plugin.get_editor_interface()

	# Check if game is already running
	if editor_interface.is_playing_scene():
		return _error("Game is already running. Call stop_scene first.")

	if scene_path.is_empty():
		# Play main scene (F5 equivalent)
		editor_interface.play_main_scene()
		return {
			"success": true,
			"scene": "main",
			"timestamp": Time.get_unix_time_from_system()
		}
	else:
		# Validate scene path extension
		var ext = scene_path.get_extension()
		if ext != "tscn" and ext != "scn":
			return _error("scene_path must end with .tscn or .scn, got: %s" % scene_path)

		# Play specific scene (F6 equivalent)
		editor_interface.play_custom_scene(scene_path)
		return {
			"success": true,
			"scene": scene_path,
			"timestamp": Time.get_unix_time_from_system()
		}


func stop_scene(params: Dictionary) -> Dictionary:
	## Stop the currently running game scene in the editor (equivalent to pressing F8).
	##
	## @param params: {} (no required params)
	## @return: {"success": bool, "timestamp": float} or error

	var editor_interface = plugin.get_editor_interface()

	# Check if game is running
	if not editor_interface.is_playing_scene():
		return _error("Game is not running")

	editor_interface.stop_playing_scene()

	return {
		"success": true,
		"timestamp": Time.get_unix_time_from_system()
	}


func get_classdb_class_list(params: Dictionary) -> Dictionary:
	## Return all class names registered in ClassDB.
	##
	## Uses the running Godot editor's ClassDB — always matches the exact engine version.
	## No network required.
	##
	## @param params: {} (no required params)
	## @return: {"success": bool, "classes": Array[String], "count": int, "godot_version": String}

	var class_list: PackedStringArray = ClassDB.get_class_list()
	var sorted_list: Array[String] = []
	for cls in class_list:
		sorted_list.append(str(cls))
	sorted_list.sort()

	return {
		"success": true,
		"classes": sorted_list,
		"count": sorted_list.size(),
		"godot_version": Engine.get_version_info().get("string", "unknown"),
		"timestamp": Time.get_unix_time_from_system()
	}


func get_classdb_class_docs(params: Dictionary) -> Dictionary:
	## Get structured documentation for a single class from ClassDB.
	##
	## Returns method signatures, properties, signals, and constants in the
	## same format as the XML doc parser so the Python cache layer can use it
	## interchangeably with GitHub-sourced docs.
	##
	## @param params: {"class_name": String}
	## @return: Structured class docs dict or error

	var cls: String = params.get("class_name", "")

	if cls.is_empty():
		return _error("class_name parameter is required")

	if not ClassDB.class_exists(cls):
		return _error("Class '%s' not found in ClassDB" % cls)

	var result: Dictionary = {
		"success": true,
		"name": cls,
		"inherits": str(ClassDB.get_parent_class(cls)),
		"brief_description": "",
		"description": "",
		"methods": [],
		"members": [],
		"signals": [],
		"constants": [],
		"enums": {},
		"source": "classdb",
		"godot_version": Engine.get_version_info().get("string", "unknown"),
		"timestamp": Time.get_unix_time_from_system()
	}

	# --- Methods (own class only, no inheritance) ---
	var methods: Array = ClassDB.class_get_method_list(cls, true)
	for method_info: Dictionary in methods:
		var method_name: String = str(method_info.get("name", ""))

		var return_info: Dictionary = method_info.get("return", {})
		var return_type: String = _get_type_name_from_info(return_info)

		var params_list: Array = []
		var args: Array = method_info.get("args", [])
		var default_args: Array = method_info.get("default_args", [])
		var default_start: int = args.size() - default_args.size()

		for j in range(args.size()):
			var arg: Dictionary = args[j]
			var param_entry: Dictionary = {
				"name": str(arg.get("name", "")),
				"type": _get_type_name_from_info(arg),
				"default": ""
			}
			if j >= default_start and j - default_start < default_args.size():
				param_entry["default"] = str(default_args[j - default_start])
			params_list.append(param_entry)

		result["methods"].append({
			"name": method_name,
			"qualifiers": "",
			"return_type": return_type,
			"params": params_list,
			"description": ""
		})

	# --- Properties (members) — own class only ---
	var properties: Array = ClassDB.class_get_property_list(cls, true)
	for prop_info: Dictionary in properties:
		var prop_name: String = str(prop_info.get("name", ""))
		var usage: int = prop_info.get("usage", 0)

		# Include only real properties (storage or editor visible)
		if not (usage & (PROPERTY_USAGE_STORAGE | PROPERTY_USAGE_EDITOR)):
			continue

		# Skip category/group/subgroup markers
		if usage & PROPERTY_USAGE_CATEGORY or usage & PROPERTY_USAGE_GROUP or usage & PROPERTY_USAGE_SUBGROUP:
			continue

		if prop_name.is_empty():
			continue

		result["members"].append({
			"name": prop_name,
			"type": _get_type_name_from_info(prop_info),
			"default": "",
			"setter": "",
			"getter": "",
			"description": ""
		})

	# --- Signals — own class only ---
	var signals_list: Array = ClassDB.class_get_signal_list(cls, true)
	for signal_info: Dictionary in signals_list:
		var signal_name: String = str(signal_info.get("name", ""))
		var signal_params: Array = []
		for arg: Dictionary in signal_info.get("args", []):
			signal_params.append({
				"name": str(arg.get("name", "")),
				"type": _get_type_name_from_info(arg)
			})
		result["signals"].append({
			"name": signal_name,
			"params": signal_params,
			"description": ""
		})

	# --- Enums — own class only ---
	var enum_list: PackedStringArray = ClassDB.class_get_enum_list(cls, true)
	for enum_name: String in enum_list:
		var enum_constants: PackedStringArray = ClassDB.class_get_enum_constants(cls, enum_name, true)
		result["enums"][str(enum_name)] = []
		for const_name: String in enum_constants:
			var value: int = ClassDB.class_get_integer_constant(cls, const_name)
			var const_entry: Dictionary = {
				"name": str(const_name),
				"value": str(value),
				"enum": str(enum_name),
				"description": ""
			}
			result["constants"].append(const_entry)
			result["enums"][str(enum_name)].append(const_entry)

	# --- Non-enum constants — own class only ---
	var all_constants: PackedStringArray = ClassDB.class_get_integer_constant_list(cls, true)
	var enum_const_names: Dictionary = {}
	for ec: Dictionary in result["constants"]:
		enum_const_names[ec["name"]] = true

	for const_name: String in all_constants:
		if str(const_name) in enum_const_names:
			continue
		var value: int = ClassDB.class_get_integer_constant(cls, const_name)
		result["constants"].append({
			"name": str(const_name),
			"value": str(value),
			"enum": "",
			"description": ""
		})

	return result


func _get_type_name_from_info(info: Dictionary) -> String:
	## Convert a ClassDB type info dict to a human-readable type string.
	##
	## Handles Object types (returns class_name) and Variant types (returns type_string).
	var type_int: int = info.get("type", TYPE_NIL)
	var class_name_str: String = str(info.get("class_name", ""))

	if type_int == TYPE_OBJECT and not class_name_str.is_empty():
		return class_name_str

	if type_int == TYPE_NIL:
		# For return types, NIL with no usage typically means void
		var usage: int = info.get("usage", 0)
		if usage == 0:
			return "void"
		return "Variant"

	return type_string(type_int)


func _error(message: String) -> Dictionary:
	## Create an error result dictionary and notify.
	_notify_error(message)
	return {
		"success": false,
		"error": message,
		"timestamp": Time.get_unix_time_from_system()
	}

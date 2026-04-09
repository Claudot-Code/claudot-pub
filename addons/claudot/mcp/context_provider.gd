@tool
extends RefCounted

## ContextProvider - Gathers current editor context for auto-injection into messages
##
## Called by chat_panel before sending messages. Returns a dictionary
## of context that gets appended to the outgoing message params.
##
## Note: Error log access (CTXT-03) is not implemented. Godot does not expose
## a built-in EditorPlugin API for reading console errors. Research confirmed
## LOW confidence for any workaround. This is deferred to a future phase if
## a viable approach is discovered.

var plugin: EditorPlugin


func _init(editor_plugin: EditorPlugin = null) -> void:
	plugin = editor_plugin


func get_context(include_scene: bool = true, include_selection: bool = true) -> Dictionary:
	## Gather current editor context based on toggle flags.
	## Returns a dictionary suitable for attaching to outgoing messages.
	var context = {}

	if plugin == null:
		return context

	var editor = plugin.get_editor_interface()

	# Scene context
	if include_scene:
		var scene_root = editor.get_edited_scene_root()
		if scene_root:
			context["scene_path"] = scene_root.scene_file_path if scene_root.scene_file_path else "Untitled"
			context["scene_root_type"] = scene_root.get_class()
			context["scene_root_name"] = scene_root.name

	# Selection context
	if include_selection:
		var selection = editor.get_selection()
		if selection:
			var selected_nodes = selection.get_selected_nodes()
			if selected_nodes.size() > 0:
				var selection_info = []
				for node in selected_nodes:
					var info = {
						"path": str(node.get_path()),
						"type": node.get_class(),
						"name": node.name
					}
					# Include script path if node has a script
					var script = node.get_script()
					if script:
						info["script"] = script.resource_path
					selection_info.append(info)
				context["selected_nodes"] = selection_info

	return context

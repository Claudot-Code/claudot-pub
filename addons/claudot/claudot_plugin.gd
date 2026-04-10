@tool
extends EditorPlugin

## Claudot EditorPlugin
## Manages the TCP client as a direct child node so it runs in the editor.

const TCP_CLIENT_SCRIPT = preload("res://addons/claudot/network/tcp_client.gd")
const MCP_HANDLER_SCRIPT = preload("res://addons/claudot/mcp/mcp_handler.gd")
const CHAT_PANEL_SCENE = preload("res://addons/claudot/ui/chat_panel.tscn")
const HTTP_SERVER_SCRIPT = preload("res://addons/claudot/network/http_server.gd")
const HTTP_BRIDGE_HANDLER_SCRIPT = preload("res://addons/claudot/mcp/http_bridge_handler.gd")
const DEBUGGER_PLUGIN_SCRIPT = preload("res://addons/claudot/debugger/debugger_capture_plugin.gd")
const BRIDGE_LAUNCHER_SCRIPT = preload("res://addons/claudot/network/bridge_launcher.gd")
const FIRST_RUN_SETTING = "claudot/first_run_shown"

var tcp_client: Node = null
var mcp_handler: Node = null
var chat_panel: Control = null
var http_server: Node = null
var bridge_handler: Node = null
var debugger_plugin: EditorDebuggerPlugin = null
var bottom_panel_button: Button = null
var bridge_launcher: Node = null


func _enter_tree() -> void:
	# Step 0: Register debugger plugin for output capture
	debugger_plugin = DEBUGGER_PLUGIN_SCRIPT.new()
	add_debugger_plugin(debugger_plugin)

	# Step 1: Create TCP client as a direct child of the plugin (runs in editor)
	tcp_client = TCP_CLIENT_SCRIPT.new()
	tcp_client.name = "ClaudotTCP"
	add_child(tcp_client)

	# Step 2: Create MCP handler as child (runs in editor, processes MCP commands)
	mcp_handler = MCP_HANDLER_SCRIPT.new()
	mcp_handler.name = "ClaudotMCP"
	add_child(mcp_handler)

	# Step 3: Create chat panel, register in bottom dock
	chat_panel = CHAT_PANEL_SCENE.instantiate()
	bottom_panel_button = add_control_to_bottom_panel(chat_panel, "Claudot")

	# Step 4: Create HTTP bridge handler (routes tool calls to scene tools)
	bridge_handler = HTTP_BRIDGE_HANDLER_SCRIPT.new()
	bridge_handler.name = "ClaudotHTTPBridge"
	add_child(bridge_handler)
	bridge_handler.setup(self)

	# Step 5: Create and start HTTP server (receives calls from MCP server)
	http_server = HTTP_SERVER_SCRIPT.new()
	http_server.name = "ClaudotHTTPServer"
	add_child(http_server)
	http_server.set_request_handler(Callable(bridge_handler, "handle_request"))
	http_server.start_server()

	# Step 6: Register output capture autoload for game-side print capture
	add_autoload_singleton("OutputCapture", "res://addons/claudot/debugger/output_capture.gd")

	# Step 7: Wire TCP signals via deferred call
	call_deferred("_setup_connections")

	# Step 8: Create bridge launcher and auto-launch bridge process
	bridge_launcher = BRIDGE_LAUNCHER_SCRIPT.new()
	bridge_launcher.name = "ClaudotBridgeLauncher"
	add_child(bridge_launcher)
	# Connect error signal before auto_launch so any immediate error is caught
	# Signal wiring to chat_panel must be deferred (chat_panel may not be ready yet)
	call_deferred("_setup_bridge_launcher")

	print("[Claudot] Ready")


func _setup_connections() -> void:
	## Wire TCP client connections after _ready() completes.
	## Connects both MCP handler and chat panel to TCP message stream.

	if tcp_client and mcp_handler:
		# Wire MCP handler (processes "mcp/*" method calls)
		mcp_handler.setup(self, tcp_client)

	if tcp_client and chat_panel:
		# Wire chat panel (processes non-MCP messages for UI)
		chat_panel.setup_tcp_signals(tcp_client)
		chat_panel.setup_context(self)
		if bridge_launcher:
			chat_panel.setup_bridge_launcher(bridge_launcher)

	# Wire interactive input signals (Phase 25: INPT-01 through INPT-06)
	if bridge_handler and bridge_handler.scene_tools and chat_panel and chat_panel.conversation_tab:
		bridge_handler.scene_tools.user_input_requested.connect(
			chat_panel.conversation_tab._show_input_widget
		)
		chat_panel.conversation_tab.user_input_submitted.connect(
			bridge_handler.scene_tools._on_user_input_received
		)


func _setup_bridge_launcher() -> void:
	## Wire bridge_launcher signals to chat_panel and conditionally auto-launch.
	## Called deferred so chat_panel is fully ready when signals connect.
	if not bridge_launcher:
		return

	# Wire launcher_error → conversation tab (existing behavior from Phase 17)
	if chat_panel:
		bridge_launcher.launcher_error.connect(
			func(message: String) -> void:
				if chat_panel and chat_panel.conversation_tab:
					chat_panel.conversation_tab.append_system_message(message)
		)

	# Wire crash signal → conversation tab error message
	if chat_panel:
		bridge_launcher.bridge_process_exited.connect(
			func() -> void:
				if chat_panel == null or chat_panel.conversation_tab == null:
					return
				# Suppress false alarm when uv launcher exits after delegating to Python —
				# TCP still active means the actual bridge subprocess is alive.
				if chat_panel.tcp_client != null and \
						chat_panel.tcp_client.current_state == TCP_CLIENT_SCRIPT.ConnectionState.CONNECTED:
					return
				chat_panel.conversation_tab.append_system_message(
					"[b]Bridge stopped.[/b] Click [b]Connect[/b] to restart it."
				)
		)

	# Auto-launch bridge immediately (uses claude CLI auth — no API key required)
	bridge_launcher.auto_launch()

	# Wire derived port to TCP client so it connects to this project's bridge instance.
	# Each project gets a unique port via _get_project_port() — prevents cross-connection
	# when two Godot projects are open simultaneously.
	var project_port = bridge_launcher.bridge_port
	if tcp_client and project_port > 0:
		tcp_client.set_port(project_port)
		print("[Claudot] TCP client port set to %d" % project_port)

	# Step 9: Generate .mcp.json for Claude Code CLI discovery (Phase 19: MCPC-01, MCPC-02)
	_generate_mcp_json()

	# Step 10: Ensure .mcp.json and addons/claudot/bridge/.env are gitignored (Phase 19: MCPC-03)
	_ensure_gitignore_entries()

	# Step 10.5: Pre-approve Claudot MCP tools in .claude/settings.local.json
	_ensure_claude_code_permissions()

	# Step 11: Generate CLAUDE.md for Claude Code context (Phase 23: CLMD-01, CLMD-02)
	_generate_claude_md()

	# Step 12: Check for claude CLI and show error if not found (Phase 27: ONBR-01)
	var cli_missing = _check_claude_cli()

	# Step 13: Show welcome message on first run (Phase 27: ONBR-02)
	# Skipped if CLI error was already shown (mutual exclusion — error IS the first-run message)
	if not cli_missing:
		_check_first_run()


func _generate_mcp_json() -> void:
	## Generate .mcp.json at project root for Claude Code CLI MCP server discovery.
	## Merges with existing file to preserve other MCP server entries.
	## Skips generation if no Python launcher is detected.
	if not bridge_launcher:
		return

	var launcher = bridge_launcher.detect_launcher()
	if launcher.is_empty():
		return  # No launcher found — skip; conversation tab already shows install instructions

	var script_abs = ProjectSettings.globalize_path("res://addons/claudot/bridge/godot_mcp_server.py")
	var project_root = ProjectSettings.globalize_path("res://")

	# Build args based on launcher (uv needs "run" subcommand)
	var args: Array
	if launcher == "uv":
		args = ["run", script_abs]
	else:
		args = [script_abs]

	var our_entry = {
		"command": launcher,
		"args": args,
		"env": {"GODOT_BRIDGE_URL": "http://127.0.0.1:7778"}
	}

	# Read and merge existing file, or start fresh
	var mcp_path = project_root + ".mcp.json"
	var existing: Dictionary = {}
	if FileAccess.file_exists(mcp_path):
		var f = FileAccess.open(mcp_path, FileAccess.READ)
		if f:
			var parsed = JSON.parse_string(f.get_as_text())
			f.close()
			if parsed is Dictionary:
				existing = parsed

	# Ensure mcpServers key exists and is a Dictionary
	if not existing.has("mcpServers") or not existing["mcpServers"] is Dictionary:
		existing["mcpServers"] = {}

	# Remove stale server keys from previous Claudot versions (e.g. "godot")
	for key in existing["mcpServers"].keys():
		if key != "claudot":
			existing["mcpServers"].erase(key)

	existing["mcpServers"]["claudot"] = our_entry

	# Write back with readable 2-space indentation
	var fw = FileAccess.open(mcp_path, FileAccess.WRITE)
	if fw == null:
		push_error("[Claudot] Failed to write .mcp.json: %s" % mcp_path)
		return
	fw.store_string(JSON.stringify(existing, "  ") + "\n")
	fw.close()


func _ensure_gitignore_entries() -> void:
	## Ensure .mcp.json and bridge/.env are in .gitignore at project root.
	## Idempotent: skips entries that already exist. Creates .gitignore if missing.
	var project_root = ProjectSettings.globalize_path("res://")
	var gitignore_path = project_root + ".gitignore"

	var existing_content = ""
	if FileAccess.file_exists(gitignore_path):
		var f = FileAccess.open(gitignore_path, FileAccess.READ)
		if f:
			existing_content = f.get_as_text()
			f.close()

	# Check each entry individually with exact line comparison
	var entries_to_add: PackedStringArray = []
	for entry in [".mcp.json", "addons/claudot/bridge/.env", ".claude/settings.local.json"]:
		var found = false
		for line in existing_content.split("\n"):
			if line.strip_edges() == entry:
				found = true
				break
		if not found:
			entries_to_add.append(entry)

	if entries_to_add.is_empty():
		return  # All entries already present

	# Build append text with section header
	var append_text = "\n# Claudot\n"
	for entry in entries_to_add:
		append_text += entry + "\n"

	if FileAccess.file_exists(gitignore_path):
		# Append to existing file using READ_WRITE + seek_end (not WRITE which truncates)
		var f = FileAccess.open(gitignore_path, FileAccess.READ_WRITE)
		if f:
			f.seek_end(0)
			f.store_string(append_text)
			f.close()
	else:
		# Create new file (no leading newline needed)
		var f = FileAccess.open(gitignore_path, FileAccess.WRITE)
		if f:
			f.store_string(append_text.lstrip("\n"))
			f.close()


func _ensure_claude_code_permissions() -> void:
	## Create or update .claude/settings.local.json to pre-approve Claudot MCP tools.
	## Idempotent: merges with existing permissions, never overwrites user's other settings.
	var project_root = ProjectSettings.globalize_path("res://")
	var dot_claude_dir = project_root + ".claude/"
	var settings_path = dot_claude_dir + "settings.local.json"

	var claudot_tools = [
		"mcp__claudot__get_scene_state",
		"mcp__claudot__get_node_property",
		"mcp__claudot__set_node_property",
		"mcp__claudot__get_editor_context",
		"mcp__claudot__create_node",
		"mcp__claudot__delete_node",
		"mcp__claudot__reparent_node",
		"mcp__claudot__search_files",
		"mcp__claudot__capture_screenshot",
		"mcp__claudot__get_debugger_output",
		"mcp__claudot__get_debugger_errors",
		"mcp__claudot__get_node_script",
		"mcp__claudot__run_tests",
		"mcp__claudot__request_user_input",
		"mcp__claudot__get_pending_input_answer",
		"mcp__claudot__run_scene",
		"mcp__claudot__stop_scene",
		"mcp__claudot__godot_search_docs",
		"mcp__claudot__godot_get_class_docs",
		"mcp__claudot__godot_refresh_docs",
	]

	# Read existing settings or start fresh
	var settings: Dictionary = {}
	if FileAccess.file_exists(settings_path):
		var f = FileAccess.open(settings_path, FileAccess.READ)
		if f:
			var parsed = JSON.parse_string(f.get_as_text())
			f.close()
			if parsed is Dictionary:
				settings = parsed

	# Ensure permissions.allow array exists
	if not settings.has("permissions") or not settings["permissions"] is Dictionary:
		settings["permissions"] = {}
	if not settings["permissions"].has("allow") or not settings["permissions"]["allow"] is Array:
		settings["permissions"]["allow"] = []

	# Add missing tools
	var added = false
	for tool_name in claudot_tools:
		if not settings["permissions"]["allow"].has(tool_name):
			settings["permissions"]["allow"].append(tool_name)
			added = true

	if not added:
		return  # All tools already present

	# Ensure .claude/ directory exists
	if not DirAccess.dir_exists_absolute(dot_claude_dir):
		DirAccess.make_dir_recursive_absolute(dot_claude_dir)

	# Write back with readable indentation
	var fw = FileAccess.open(settings_path, FileAccess.WRITE)
	if fw == null:
		push_error("[Claudot] Failed to write .claude/settings.local.json")
		return
	fw.store_string(JSON.stringify(settings, "  ") + "\n")
	fw.close()


func _generate_claude_md() -> void:
	## Generate .claude/CLAUDE.md to teach Claude Code about Claudot's MCP tools.
	## Skips generation if .claude/CLAUDE.md already exists (preserves user edits).
	var project_root = ProjectSettings.globalize_path("res://")
	var claude_dir = project_root + ".claude"
	var claude_md_path = claude_dir + "/CLAUDE.md"

	if FileAccess.file_exists(claude_md_path):
		return

	DirAccess.make_dir_recursive_absolute(claude_dir)

	var structure = _get_top_level_structure()
	var content = _build_claude_md_content(structure)

	var f = FileAccess.open(claude_md_path, FileAccess.WRITE)
	if f == null:
		push_error("[Claudot] Failed to write .claude/CLAUDE.md: %s" % claude_md_path)
		return
	f.store_string(content)
	f.close()


func _get_top_level_structure() -> String:
	## Return a markdown list of top-level dirs and files under res://.
	## Dirs are listed first (with trailing /), files second, both sorted.
	## Hidden entries (starting with ".") are skipped.
	var dir = DirAccess.open("res://")
	if dir == null:
		return "(could not read project directory)"

	var dirs: PackedStringArray = []
	var files: PackedStringArray = []

	dir.list_dir_begin()
	var entry = dir.get_next()
	while entry != "":
		if not entry.begins_with("."):
			if dir.current_is_dir():
				dirs.append(entry + "/")
			else:
				files.append(entry)
		entry = dir.get_next()
	dir.list_dir_end()

	dirs.sort()
	files.sort()

	var lines: PackedStringArray = []
	for d in dirs:
		lines.append("- " + d)
	for fi in files:
		lines.append("- " + fi)

	return "\n".join(lines)


func _build_claude_md_content(structure: String) -> String:
	## Build the full CLAUDE.md content with all 8 required sections.
	var c = ""

	# Section 1 — Project Context
	c += "# Claudot — Godot AI Assistant\n\n"
	c += "This project uses the Claudot plugin. You have MCP tools to inspect and modify the Godot scene tree. "
	c += "The Godot editor must be open and the plugin enabled for tools to work.\n\n"

	# Section 2 — Project Scope (safety boundary)
	c += "## Project Scope\n\n"
	c += "Only create or modify files within this Godot project (the directory containing this CLAUDE.md). "
	c += "Before writing or running commands that affect files outside this directory, "
	c += "ask the user for explicit confirmation.\n\n"

	# Section 3 — MCP Tool Inventory
	c += "## MCP Tools\n\n"
	c += "| Tool | Required Params | Notes |\n"
	c += "|------|----------------|-------|\n"
	c += "| `get_scene_state` | — | Optional: `max_depth` (int, default 5) |\n"
	c += "| `get_node_property` | `node_path`, `property_name` | |\n"
	c += "| `set_node_property` | `node_path`, `property_name`, `value` | |\n"
	c += "| `get_editor_context` | — | Active scene, selection |\n"
	c += "| `create_node` | `parent_path`, `node_type`, `node_name` | |\n"
	c += "| `delete_node` | `node_path` | |\n"
	c += "| `reparent_node` | `node_path`, `new_parent_path` | |\n"
	c += "| `search_files` | — | Optional: `pattern`, `extensions`, `max_results` |\n"
	c += "| `capture_screenshot` | — | Optional: `viewport_type` (2d_editor/3d_editor/game) |\n"
	c += "| `get_debugger_output` | — | Optional: `max_lines` (default 100) |\n"
	c += "| `get_debugger_errors` | — | Optional: `max_lines` (default 100) |\n"
	c += "| `get_node_script` | `node_path` | GDScript source only, not C# |\n"
	c += "| `run_tests` | — | Optional: `test_directory`, `test_file`, `test_name` |\n"
	c += "| `run_scene` | — | Optional: `scene_path` (String, default: main scene) |\n"
	c += "| `stop_scene` | — | Stops the running game |\n\n"

	# Section 4 — Node Path Format
	c += "## Node Path Format\n\n"
	c += "Node paths use `/root/NodeName` format. The scene root is `/root`. "
	c += "Children use `/root/Parent/Child`.\n\n"
	c += "Example: `/root/Main/Player/Sprite2D`\n\n"

	# Section 5 — GDScript Best Practices
	c += "## GDScript Best Practices\n\n"
	c += "**Prefer built-in nodes over custom scripts.**\n"
	c += "Before writing a script to do something, check if a built-in Godot node already handles it. "
	c += "Use `CharacterBody2D` instead of a plain `Node2D` + custom movement script. "
	c += "Use `AnimationPlayer` instead of scripting tweens manually. "
	c += "Use `Timer` instead of a counter in `_process()`. "
	c += "Reach for the node first; add a script only when the built-in behaviour needs extending.\n\n"
	c += "**Expose settings with @export.**\n"
	c += "Any value that a designer or developer might tune should be an `@export` variable, not a hardcoded constant. "
	c += "This makes the value visible and editable in the Godot Inspector without touching code.\n"
	c += "```gdscript\n"
	c += "@export var speed: float = 200.0\n"
	c += "@export var max_health: int = 100\n"
	c += "@export var jump_force: float = 400.0\n"
	c += "```\n\n"
	c += "**Always type variables and return values.**\n"
	c += "```gdscript\n"
	c += "var speed: float = 200.0\n"
	c += "func move(delta: float) -> void:\n"
	c += "    pass\n"
	c += "```\n\n"
	c += "**Signal-first architecture** — decouple nodes with signals. "
	c += "Child nodes emit; parents/managers listen. Default to signals over direct method calls.\n"
	c += "```gdscript\n"
	c += "signal health_changed(new_health: int)\n"
	c += "health_changed.emit(health)\n"
	c += "player.health_changed.connect(_on_health_changed)\n"
	c += "```\n\n"
	c += "**Use `@onready` for node references:**\n"
	c += "```gdscript\n"
	c += "@onready var sprite: Sprite2D = $Sprite2D\n"
	c += "```\n\n"

	# Section 6 — Workflow Rules
	c += "## Workflow Rules\n\n"
	c += "1. **Orient first** — call `get_editor_context()` before making changes.\n"
	c += "2. **Inspect before mutate** — read state with `get_scene_state()` or `get_node_property()` before `set_node_property()`.\n"
	c += "3. **Read scripts before editing files** — use `get_node_script()` to read a node's GDScript before editing the `.gd` file on disk.\n"
	c += "4. **Test workflow** — after code changes: `run_tests()` then `get_debugger_output()` for print output and `get_debugger_errors()` for error checking.\n"
	c += "5. **Run game** — use `run_scene()` to launch the game, then `capture_screenshot(viewport_type=\"game\")` to see it, and `get_debugger_output()` / `get_debugger_errors()` for logs. Use `stop_scene()` when done.\n"
	c += "6. **Discover files** — use `search_files(extensions=[\".gd\"])` rather than guessing file paths.\n"
	c += "7. **Visual verification** — use `capture_screenshot()` after visual scene changes to verify layout.\n\n"

	# Section 7 — Testing Guide
	c += "## Testing\n\n"
	c += "Tests use the GUT framework. Test files go in `res://test/`. Use `run_tests()` to execute.\n\n"
	c += "`test_directory` defaults to `\"test/unit\"`. "
	c += "Use `test_file` to target a specific file, `test_name` to run a single test method.\n\n"

	# Section 8 — Project Structure (dynamic)
	c += "## Project Structure\n\n"
	c += "<!-- Auto-generated when Claudot was first enabled. Update manually if structure changes. -->\n\n"
	c += "```\n"
	c += "res://\n"
	c += structure + "\n"
	c += "```\n"

	return c


func _check_claude_cli() -> bool:
	## Check if claude CLI is available. Shows error in conversation tab if not found.
	## Returns true if CLI is MISSING (error shown), false if found.
	if not bridge_launcher:
		return true
	if bridge_launcher.probe_executable("claude") != 0:
		if chat_panel and chat_panel.conversation_tab:
			chat_panel.conversation_tab.append_system_message(
				"[b]Claude Code not found.[/b]\n\n" +
				"Install it from [url=https://claude.ai/download]claude.ai/download[/url] " +
				"and restart Godot."
			)
		return true
	return false


func _check_first_run() -> void:
	## Show welcome message on first plugin enable for this project.
	## Uses ProjectSettings to persist the flag per-project (stored in project.godot).
	## Skipped if _check_claude_cli() already showed an error (mutual exclusion).
	var already_shown = ProjectSettings.has_setting(FIRST_RUN_SETTING) and \
		ProjectSettings.get_setting(FIRST_RUN_SETTING)
	if already_shown:
		return
	if chat_panel and chat_panel.conversation_tab:
		chat_panel.conversation_tab.append_system_message(
			"[b]Welcome to Claudot![/b]\n\n" +
			"Click [b]Connect[/b] to start chatting with Claude.\n\n" +
			"Run [code]claude[/code] in this project's directory to enable MCP tools."
		)
	ProjectSettings.set_setting(FIRST_RUN_SETTING, true)
	ProjectSettings.save()


func _exit_tree() -> void:
	# Clean up bridge launcher (Step 8 reverse — kill process before other cleanup)
	if bridge_launcher:
		bridge_launcher.stop()
		bridge_launcher.queue_free()
		bridge_launcher = null

	# Remove output capture autoload (registered in Step 6 -- remove first, reverse order)
	remove_autoload_singleton("OutputCapture")

	# Clean up debugger plugin (registered in Step 0 -- remove second, reverse order)
	if debugger_plugin:
		remove_debugger_plugin(debugger_plugin)
		debugger_plugin = null

	# Clean up HTTP server
	if http_server:
		http_server.stop_server()
		http_server.queue_free()
		http_server = null

	# Clean up bridge handler
	if bridge_handler:
		bridge_handler.queue_free()
		bridge_handler = null

	# Remove bottom panel (MUST happen before queue_free)
	if chat_panel:
		remove_control_from_bottom_panel(chat_panel)
		chat_panel.queue_free()
		chat_panel = null
	bottom_panel_button = null  # Button auto-freed by editor

	# Clean up MCP handler
	if mcp_handler:
		mcp_handler.queue_free()
		mcp_handler = null

	# Clean up TCP client
	if tcp_client:
		if tcp_client.has_method("cleanup"):
			tcp_client.cleanup()
		tcp_client.queue_free()
		tcp_client = null


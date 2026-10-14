@tool
extends ConfirmationDialog

## UpdateDialog - Shows an available Claudot release and installs it in place.
##
## open_for(info, installed) shows the release notes with "Update and restart",
## "Later", "View on GitHub" and "Skip this version". Installing is delegated to
## UpdateInstaller; on success the editor restarts so the new addon scripts load.
## open_restore(...) reuses the same dialog to roll back to the backed-up version.

signal skip_requested(tag: String)

const RELEASES_PAGE_URL := "https://github.com/Claudot-Code/claudot-pub/releases"
const DIALOG_SIZE := Vector2i(560, 480)
const ERROR_COLOR := Color(1.0, 0.42, 0.42)

var _installer: Node = null
var _tcp_client: Node = null
var _bridge_launcher: Node = null

var _info: Dictionary = {}
var _mode: String = "update"  # "update" or "restore"
var _running: bool = false

var _title_label: Label
var _version_label: Label
var _date_label: Label
var _notes_scroll: ScrollContainer
var _notes_label: RichTextLabel
var _note_label: Label
var _stage_label: Label
var _progress_bar: ProgressBar
var _manual_link: LinkButton
var _github_button: Button
var _skip_button: Button


func _init() -> void:
	title = "Claudot Update"
	min_size = Vector2i(480, 360)
	dialog_hide_on_ok = false  # stay open while installing
	get_ok_button().text = "Update and restart"
	get_cancel_button().text = "Later"
	_github_button = add_button("View on GitHub", false, "view")
	_skip_button = add_button("Skip this version", false, "skip")
	confirmed.connect(_on_confirmed)
	custom_action.connect(_on_custom_action)
	_build_ui()


func _build_ui() -> void:
	var vbox := VBoxContainer.new()
	vbox.add_theme_constant_override("separation", 8)
	add_child(vbox)

	_title_label = Label.new()
	_title_label.text = "Claudot update available"
	_title_label.add_theme_font_size_override("font_size", 18)
	vbox.add_child(_title_label)

	_version_label = Label.new()
	vbox.add_child(_version_label)

	_date_label = Label.new()
	_date_label.modulate = Color(1, 1, 1, 0.7)
	vbox.add_child(_date_label)

	_notes_scroll = ScrollContainer.new()
	_notes_scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	_notes_scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	_notes_scroll.custom_minimum_size = Vector2(0, 200)
	vbox.add_child(_notes_scroll)

	_notes_label = RichTextLabel.new()
	_notes_label.bbcode_enabled = true
	_notes_label.fit_content = true
	_notes_label.scroll_active = false
	_notes_label.selection_enabled = true
	_notes_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_notes_label.meta_clicked.connect(func(meta): OS.shell_open(str(meta)))
	_notes_scroll.add_child(_notes_label)

	_note_label = Label.new()
	_note_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_note_label.modulate = Color(1, 1, 1, 0.8)
	_note_label.text = "Files under addons/claudot will change; if the project is under version control you will see a diff. The editor restarts after installing. Bridge/.env is preserved."
	vbox.add_child(_note_label)

	_stage_label = Label.new()
	_stage_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_stage_label.visible = false
	vbox.add_child(_stage_label)

	_progress_bar = ProgressBar.new()
	_progress_bar.min_value = 0.0
	_progress_bar.max_value = 1.0
	_progress_bar.step = 0.001
	_progress_bar.visible = false
	vbox.add_child(_progress_bar)

	_manual_link = LinkButton.new()
	_manual_link.text = "Manual update: " + RELEASES_PAGE_URL
	_manual_link.uri = RELEASES_PAGE_URL
	_manual_link.visible = false
	vbox.add_child(_manual_link)


## ---------------------------------------------------------------------------
## Public API
## ---------------------------------------------------------------------------

func setup(installer: Node, tcp_client: Node, bridge_launcher: Node) -> void:
	_tcp_client = tcp_client
	_bridge_launcher = bridge_launcher
	if _installer == installer:
		return
	if _installer and is_instance_valid(_installer):
		_disconnect_installer()
	_installer = installer
	if _installer:
		_installer.stage_changed.connect(_on_stage_changed)
		_installer.progress.connect(_on_progress)
		_installer.failed.connect(_on_failed)
		_installer.completed.connect(_on_completed)


func open_for(info: Dictionary, installed_version: String) -> void:
	## Populate with a release (UpdateChecker info Dictionary) and show.
	if _running:
		popup_centered(DIALOG_SIZE)
		return
	_mode = "update"
	_info = info
	var latest := str(info.get("version", "?"))
	title = "Claudot Update"
	_title_label.text = "Claudot update available"
	_version_label.text = "Installed v%s → Latest v%s" % [installed_version, latest]
	var published := str(info.get("published_at", ""))
	_date_label.text = ("Published " + published.substr(0, 10)) if published.length() >= 10 else ""
	_date_label.visible = not _date_label.text.is_empty()
	_notes_label.text = _format_notes(str(info.get("body", "")))
	_notes_scroll.visible = true
	_note_label.text = "Files under addons/claudot will change; if the project is under version control you will see a diff. The editor restarts after installing. Bridge/.env is preserved."
	_github_button.visible = not str(info.get("html_url", "")).is_empty()
	_skip_button.visible = true
	_reset_controls("Update and restart")
	popup_centered(DIALOG_SIZE)


func open_restore(installer: Node, tcp_client: Node, bridge_launcher: Node, backup_version: String) -> void:
	## Offer to roll back to the version backed up by the last update.
	setup(installer, tcp_client, bridge_launcher)
	if _running:
		popup_centered(DIALOG_SIZE)
		return
	_mode = "restore"
	_info = {}
	var installed := ""
	var cfg := ConfigFile.new()
	if cfg.load("res://addons/claudot/plugin.cfg") == OK:
		installed = str(cfg.get_value("plugin", "version", ""))
	title = "Restore Previous Claudot"
	_title_label.text = "Restore the previous Claudot version"
	_version_label.text = "Installed v%s → Backup v%s" % [installed, backup_version]
	_date_label.visible = false
	_notes_scroll.visible = false
	_note_label.text = "This puts back the addons/claudot folder saved before the last in-editor update. Bridge/.env is preserved. The editor restarts afterwards."
	_github_button.visible = false
	_skip_button.visible = false
	_reset_controls("Restore and restart")
	popup_centered(Vector2i(DIALOG_SIZE.x, 260))


## ---------------------------------------------------------------------------
## Actions
## ---------------------------------------------------------------------------

func _on_confirmed() -> void:
	if _running or _installer == null:
		return
	_running = true
	_set_buttons_disabled(true)
	_manual_link.visible = false
	_stage_label.remove_theme_color_override("font_color")
	_stage_label.text = "Starting…"
	_stage_label.visible = true
	_progress_bar.visible = true
	_progress_bar.indeterminate = true
	if _mode == "restore":
		_installer.restore_backup(_tcp_client, _bridge_launcher)
	else:
		_installer.start(_info, _tcp_client, _bridge_launcher)


func _on_custom_action(action: StringName) -> void:
	match String(action):
		"view":
			var url := str(_info.get("html_url", ""))
			OS.shell_open(url if not url.is_empty() else RELEASES_PAGE_URL)
		"skip":
			if _running:
				return
			skip_requested.emit(str(_info.get("tag", "")))
			hide()


func _on_stage_changed(stage: String) -> void:
	if not _running:
		return
	_stage_label.text = stage


func _on_progress(fraction: float) -> void:
	if not _running:
		return
	if fraction < 0.0:
		_progress_bar.indeterminate = true
	else:
		_progress_bar.indeterminate = false
		_progress_bar.value = fraction


func _on_failed(message: String) -> void:
	if not _running:
		return
	_running = false
	_progress_bar.visible = false
	_stage_label.text = message
	_stage_label.add_theme_color_override("font_color", ERROR_COLOR)
	_stage_label.visible = true
	_manual_link.visible = true
	_set_buttons_disabled(false)
	get_ok_button().text = "Retry"
	if not visible:
		popup_centered(DIALOG_SIZE)


func _on_completed(_from_version: String, to_version: String) -> void:
	if not _running:
		return
	_progress_bar.indeterminate = false
	_progress_bar.value = 1.0
	_stage_label.text = "Installed v%s. Restarting editor…" % to_version
	print("[Claudot] Installed v%s. Restarting editor..." % to_version)
	await get_tree().create_timer(1.0).timeout
	_restart_editor()


## ---------------------------------------------------------------------------
## Helpers
## ---------------------------------------------------------------------------

func _restart_editor() -> void:
	# Looked up at runtime so this script also loads headless / outside the editor.
	if Engine.has_singleton("EditorInterface"):
		var ei = Engine.get_singleton("EditorInterface")
		if ei:
			ei.restart_editor(true)
			return
	push_warning("[Claudot] Update installed; restart Godot to load it.")
	_stage_label.text += "\nRestart Godot to finish."


func _reset_controls(ok_text: String) -> void:
	_running = false
	_set_buttons_disabled(false)
	get_ok_button().text = ok_text
	_stage_label.visible = false
	_stage_label.remove_theme_color_override("font_color")
	_progress_bar.visible = false
	_progress_bar.value = 0.0
	_manual_link.visible = false


func _set_buttons_disabled(disabled: bool) -> void:
	get_ok_button().disabled = disabled
	get_cancel_button().disabled = disabled
	_github_button.disabled = disabled
	_skip_button.disabled = disabled


func _disconnect_installer() -> void:
	for pair in [
		[_installer.stage_changed, _on_stage_changed],
		[_installer.progress, _on_progress],
		[_installer.failed, _on_failed],
		[_installer.completed, _on_completed],
	]:
		var sig: Signal = pair[0]
		if sig.is_connected(pair[1]):
			sig.disconnect(pair[1])


static func _format_notes(markdown: String) -> String:
	## Release-notes markdown -> light BBCode: headings bold, bullets "• ", the rest plain.
	if markdown.strip_edges().is_empty():
		return "[i]No release notes.[/i]"
	var out: PackedStringArray = []
	for raw_line in markdown.replace("\r\n", "\n").split("\n"):
		var line := raw_line.replace("[", "[lb]")
		var stripped := line.strip_edges()
		if stripped.begins_with("#"):
			var heading := stripped.lstrip("#").strip_edges()
			out.append("[b]%s[/b]" % heading)
		elif stripped.begins_with("- ") or stripped.begins_with("* "):
			var indent := line.length() - line.lstrip(" \t").length()
			out.append("    ".repeat(indent >> 1) + "• " + stripped.substr(2))
		else:
			out.append(line)
	return "\n".join(out)

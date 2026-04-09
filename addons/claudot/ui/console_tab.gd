@tool
extends VBoxContainer

## ConsoleTab - Raw JSON debug console for Claude Code message inspection
##
## Displays all messages (requests, responses, errors) as pretty-printed JSON
## with timestamps, search filtering, type filtering, and syntax highlighting.

class_name ConsoleTab

# Preload utilities
const JsonHighlighterScript = preload("res://addons/claudot/ui/json_highlighter.gd")

# Filter types
enum FilterType { ALL, REQUEST, RESPONSE, ERROR }

# State
var all_messages: Array = []

# Smart auto-scroll state
var scroll_indicator_button: Button
const BOTTOM_THRESHOLD = 50  # 50px per user decision (vs 20px in conversation_tab)

# UI node references
var json_output: TextEdit
var search_box: LineEdit
var filter_dropdown: OptionButton
var clear_button: Button


func _ready() -> void:
	# Build UI tree programmatically
	_build_ui()

	# Connect UI signals
	clear_button.pressed.connect(_on_clear_pressed)
	search_box.text_changed.connect(_on_search_changed)
	filter_dropdown.item_selected.connect(_on_filter_selected)
	scroll_indicator_button.pressed.connect(_on_scroll_indicator_pressed)
	var scrollbar = json_output.get_v_scroll_bar()
	if scrollbar:
		scrollbar.value_changed.connect(_on_scrollbar_value_changed)


func _build_ui() -> void:
	## Build the entire console tab UI tree.

	# Toolbar at top
	var toolbar = HBoxContainer.new()
	toolbar.name = "Toolbar"
	add_child(toolbar)

	clear_button = Button.new()
	clear_button.name = "ClearButton"
	clear_button.text = "Clear"
	toolbar.add_child(clear_button)

	# Spacer
	var spacer = Control.new()
	spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	toolbar.add_child(spacer)

	# Search box
	search_box = LineEdit.new()
	search_box.name = "SearchBox"
	search_box.placeholder_text = "Search..."
	search_box.clear_button_enabled = true
	search_box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	toolbar.add_child(search_box)

	# Filter dropdown
	filter_dropdown = OptionButton.new()
	filter_dropdown.name = "FilterDropdown"
	filter_dropdown.add_item("All", FilterType.ALL)
	filter_dropdown.add_item("Request", FilterType.REQUEST)
	filter_dropdown.add_item("Response", FilterType.RESPONSE)
	filter_dropdown.add_item("Error", FilterType.ERROR)
	filter_dropdown.selected = FilterType.ALL
	toolbar.add_child(filter_dropdown)

	# JSON output area
	json_output = TextEdit.new()
	json_output.name = "JsonOutput"
	json_output.editable = false
	json_output.size_flags_vertical = Control.SIZE_EXPAND_FILL
	json_output.syntax_highlighter = JsonHighlighterScript.create()
	json_output.add_theme_font_size_override("font_size", 12)  # Compact developer density
	add_child(json_output)

	# Scroll indicator button (overlaid on json_output via anchors)
	scroll_indicator_button = Button.new()
	scroll_indicator_button.name = "ScrollIndicator"
	scroll_indicator_button.text = "v New output"
	scroll_indicator_button.visible = false
	scroll_indicator_button.anchor_left = 1.0
	scroll_indicator_button.anchor_right = 1.0
	scroll_indicator_button.anchor_top = 1.0
	scroll_indicator_button.anchor_bottom = 1.0
	scroll_indicator_button.offset_left = -120
	scroll_indicator_button.offset_right = -10
	scroll_indicator_button.offset_top = -35
	scroll_indicator_button.offset_bottom = -5
	json_output.add_child(scroll_indicator_button)


func append_json_message(message: Dictionary, type: String = "response") -> void:
	## Add a raw JSON message to the console with timestamp.
	var entry = {
		"timestamp": Time.get_datetime_string_from_system(),
		"type": type,
		"data": message
	}
	all_messages.append(entry)

	# If message passes current filter, append directly (avoid full refresh)
	if _passes_filter(entry):
		_append_entry_to_display(entry)


func _append_entry_to_display(entry: Dictionary) -> void:
	## Append a single entry to the JSON output display.
	var was_at_bottom = _is_at_bottom()  # Check BEFORE modifying text
	var header = "[%s] [%s]" % [entry.timestamp, entry.type.to_upper()]
	var json_text = JSON.stringify(entry.data, "  ")
	if json_text.is_empty():
		json_text = str(entry.data)  # Fallback for non-serializable data
	json_output.text += header + "\n" + json_text + "\n---\n"
	if was_at_bottom:
		_smart_scroll_to_bottom()
	else:
		scroll_indicator_button.visible = true


func _passes_filter(entry: Dictionary) -> bool:
	## Check if entry passes current filter and search criteria.
	# Check type filter
	var filter_index = filter_dropdown.selected
	if filter_index != FilterType.ALL:
		var type_name = FilterType.keys()[filter_index].to_lower()
		if entry.type != type_name:
			return false

	# Check search query
	var query = search_box.text.strip_edges().to_lower()
	if not query.is_empty():
		var json_text = JSON.stringify(entry.data).to_lower()
		if query not in json_text:
			return false

	return true


func _refresh_display() -> void:
	## Rebuild display from all_messages with current filter/search.
	json_output.text = ""
	for entry in all_messages:
		if _passes_filter(entry):
			var header = "[%s] [%s]" % [entry.timestamp, entry.type.to_upper()]
			var json_text = JSON.stringify(entry.data, "  ")
			if json_text.is_empty():
				json_text = str(entry.data)
			json_output.text += header + "\n" + json_text + "\n---\n"
	_force_scroll_to_bottom()


func _on_clear_pressed() -> void:
	## Handle Clear button - immediately clear without confirmation.
	all_messages.clear()
	json_output.text = ""
	scroll_indicator_button.visible = false


func _on_search_changed(_new_text: String) -> void:
	## Handle search box text change - refresh display with new filter.
	_refresh_display()


func _on_filter_selected(_index: int) -> void:
	## Handle filter dropdown selection - refresh display with new filter.
	_refresh_display()


func _is_at_bottom() -> bool:
	## Check if user is currently scrolled to bottom of console output.
	var scrollbar = json_output.get_v_scroll_bar()
	if not scrollbar:
		return true
	var distance_from_bottom = (scrollbar.max_value - json_output.size.y) - scrollbar.value
	return distance_from_bottom <= BOTTOM_THRESHOLD


func _smart_scroll_to_bottom() -> void:
	## Scroll to bottom after new content when user was at bottom.
	await get_tree().process_frame
	var scrollbar = json_output.get_v_scroll_bar()
	if scrollbar:
		scrollbar.value = scrollbar.max_value
		scroll_indicator_button.visible = false


func _force_scroll_to_bottom() -> void:
	## Force scroll to bottom (used after bulk operations like filter/search refresh).
	await get_tree().process_frame
	var scrollbar = json_output.get_v_scroll_bar()
	if scrollbar:
		scrollbar.value = scrollbar.max_value
	scroll_indicator_button.visible = false


func _on_scrollbar_value_changed(_value: float) -> void:
	## Hide scroll indicator when user scrolls back to bottom.
	if _is_at_bottom():
		scroll_indicator_button.visible = false


func _on_scroll_indicator_pressed() -> void:
	## Jump to bottom when user clicks the new output indicator.
	_force_scroll_to_bottom()

@tool
extends AcceptDialog

## SettingsDialog - Provider / model / API key configuration for Claudot.
##
## Stores all settings in EditorSettings (the user's editor config directory),
## NEVER in the project, so API keys cannot end up in version control.
##
## Providers:
##   claude-code  — Claude Agent SDK via Claude Code CLI (subscription OAuth login;
##                  Anthropic API key optional, used if provided)
##   anthropic    — Anthropic Messages API directly (API key required)
##   openai       — OpenAI chat completions (API key required)
##   custom       — any OpenAI-compatible endpoint (base URL required, key optional)

signal settings_changed(config: Dictionary)

const ES_PROVIDER = "claudot/provider"
const ES_MODEL_CLAUDE = "claudot/model_claude"
const ES_MODEL_OPENAI = "claudot/model_openai"
const ES_MODEL_CUSTOM = "claudot/model_custom"
const ES_KEY_ANTHROPIC = "claudot/api_key_anthropic"
const ES_KEY_OPENAI = "claudot/api_key_openai"
const ES_KEY_CUSTOM = "claudot/api_key_custom"
const ES_BASE_URL_CUSTOM = "claudot/base_url_custom"

const DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"
const DEFAULT_OPENAI_MODEL = "gpt-5.1"

const PROVIDERS = [
	{"id": "claude-code", "label": "Claude Code (subscription login)"},
	{"id": "anthropic", "label": "Anthropic API (bring your own key)"},
	{"id": "openai", "label": "OpenAI API (bring your own key)"},
	{"id": "custom", "label": "Custom OpenAI-compatible (Ollama, OpenRouter, ...)"},
]

const CLAUDE_MODELS = [
	{"id": "claude-opus-4-8", "label": "Claude Opus 4.8  —  recommended"},
	{"id": "claude-fable-5", "label": "Claude Fable 5  —  most capable"},
	{"id": "claude-opus-4-7", "label": "Claude Opus 4.7"},
	{"id": "claude-opus-4-6", "label": "Claude Opus 4.6"},
	{"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6  —  fast + smart"},
	{"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5  —  fastest"},
]

const OPENAI_MODELS = [
	{"id": "gpt-5.1", "label": "GPT-5.1"},
	{"id": "gpt-5", "label": "GPT-5"},
	{"id": "gpt-4.1", "label": "GPT-4.1"},
	{"id": "o3", "label": "o3"},
]

const PROVIDER_NOTES = {
	"claude-code": "Uses your Claude Code login (run [code]claude[/code] once to sign in). Full capabilities: file edits, bash, all 20 Godot tools. API key optional — if set, it is used instead of the login.",
	"anthropic": "Talks to the Anthropic API directly with your key from console.anthropic.com. Godot scene tools work; file editing is not available in this mode. Required for Claude Fable 5 once it moves to API-key-only access.",
	"openai": "Talks to the OpenAI API with your key from platform.openai.com. Godot scene tools work; file editing is not available in this mode.",
	"custom": "Any OpenAI-compatible endpoint. Examples: Ollama [code]http://localhost:11434/v1[/code], OpenRouter [code]https://openrouter.ai/api/v1[/code]. Godot scene tools work; file editing is not available in this mode.",
}

var provider_option: OptionButton
var model_option: OptionButton
var custom_model_edit: LineEdit
var api_key_edit: LineEdit
var base_url_edit: LineEdit
var note_label: RichTextLabel

var _custom_model_row: HBoxContainer
var _api_key_row: HBoxContainer
var _base_url_row: HBoxContainer


func _ready() -> void:
	title = "Claudot Settings"
	min_size = Vector2i(560, 0)
	_build_ui()
	add_cancel_button("Cancel")
	confirmed.connect(_on_confirmed)
	about_to_popup.connect(_load_from_settings)


## ---------------------------------------------------------------------------
## Static settings access (used by chat_panel without instantiating the dialog)
## ---------------------------------------------------------------------------

static func _setting(key: String, default_value: String) -> String:
	var es = EditorInterface.get_editor_settings()
	if es and es.has_setting(key):
		var v = es.get_setting(key)
		if v is String:
			return v
	return default_value


static func _store(key: String, value: String) -> void:
	var es = EditorInterface.get_editor_settings()
	if es:
		es.set_setting(key, value)


static func get_provider() -> String:
	return _setting(ES_PROVIDER, "claude-code")


static func get_model_for_provider(provider: String) -> String:
	match provider:
		"openai":
			return _setting(ES_MODEL_OPENAI, DEFAULT_OPENAI_MODEL)
		"custom":
			return _setting(ES_MODEL_CUSTOM, "")
		_:
			return _setting(ES_MODEL_CLAUDE, DEFAULT_CLAUDE_MODEL)


static func set_model_for_provider(provider: String, model: String) -> void:
	match provider:
		"openai":
			_store(ES_MODEL_OPENAI, model)
		"custom":
			_store(ES_MODEL_CUSTOM, model)
		_:
			_store(ES_MODEL_CLAUDE, model)


static func get_config() -> Dictionary:
	## Resolve the full bridge configuration from EditorSettings.
	## Shape matches the bridge's chat/configure params.
	var provider = get_provider()
	var api_key = ""
	var base_url = ""
	match provider:
		"claude-code", "anthropic":
			api_key = _setting(ES_KEY_ANTHROPIC, "")
		"openai":
			api_key = _setting(ES_KEY_OPENAI, "")
		"custom":
			api_key = _setting(ES_KEY_CUSTOM, "")
			base_url = _setting(ES_BASE_URL_CUSTOM, "")
	return {
		"provider": provider,
		"model": get_model_for_provider(provider),
		"api_key": api_key,
		"base_url": base_url,
	}


static func get_known_models(provider: String) -> Array:
	match provider:
		"openai":
			return OPENAI_MODELS
		"custom":
			return []
		_:
			return CLAUDE_MODELS


## ---------------------------------------------------------------------------
## UI
## ---------------------------------------------------------------------------

func _build_ui() -> void:
	var vbox = VBoxContainer.new()
	vbox.add_theme_constant_override("separation", 8)
	add_child(vbox)

	# Provider row
	var provider_row = HBoxContainer.new()
	vbox.add_child(provider_row)
	var provider_label = Label.new()
	provider_label.text = "Provider"
	provider_label.custom_minimum_size.x = 110
	provider_row.add_child(provider_label)
	provider_option = OptionButton.new()
	provider_option.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	for p in PROVIDERS:
		provider_option.add_item(p["label"])
	provider_option.item_selected.connect(_on_provider_selected)
	provider_row.add_child(provider_option)

	# Model row
	var model_row = HBoxContainer.new()
	vbox.add_child(model_row)
	var model_label = Label.new()
	model_label.text = "Model"
	model_label.custom_minimum_size.x = 110
	model_row.add_child(model_label)
	model_option = OptionButton.new()
	model_option.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	model_option.item_selected.connect(_on_model_selected)
	model_row.add_child(model_option)

	# Custom model row (visible when "Custom model…" selected, or custom provider)
	_custom_model_row = HBoxContainer.new()
	vbox.add_child(_custom_model_row)
	var custom_model_label = Label.new()
	custom_model_label.text = "Model ID"
	custom_model_label.custom_minimum_size.x = 110
	_custom_model_row.add_child(custom_model_label)
	custom_model_edit = LineEdit.new()
	custom_model_edit.placeholder_text = "e.g. claude-opus-4-8, llama3.3:70b, anthropic/claude-opus-4.8"
	custom_model_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_custom_model_row.add_child(custom_model_edit)

	# API key row
	_api_key_row = HBoxContainer.new()
	vbox.add_child(_api_key_row)
	var key_label = Label.new()
	key_label.text = "API key"
	key_label.custom_minimum_size.x = 110
	_api_key_row.add_child(key_label)
	api_key_edit = LineEdit.new()
	api_key_edit.secret = true
	api_key_edit.placeholder_text = "sk-..."
	api_key_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_api_key_row.add_child(api_key_edit)
	var show_key_button = Button.new()
	show_key_button.text = "Show"
	show_key_button.toggle_mode = true
	show_key_button.toggled.connect(func(pressed: bool): api_key_edit.secret = not pressed)
	_api_key_row.add_child(show_key_button)

	# Base URL row (custom provider only)
	_base_url_row = HBoxContainer.new()
	vbox.add_child(_base_url_row)
	var url_label = Label.new()
	url_label.text = "Base URL"
	url_label.custom_minimum_size.x = 110
	_base_url_row.add_child(url_label)
	base_url_edit = LineEdit.new()
	base_url_edit.placeholder_text = "http://localhost:11434/v1"
	base_url_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_base_url_row.add_child(base_url_edit)

	# Provider note
	note_label = RichTextLabel.new()
	note_label.bbcode_enabled = true
	note_label.fit_content = true
	note_label.custom_minimum_size = Vector2(520, 0)
	note_label.add_theme_font_size_override("normal_font_size", 11)
	vbox.add_child(note_label)

	# Storage note
	var storage_label = Label.new()
	storage_label.text = "Keys are stored in your Godot editor settings — outside the project, never committed."
	storage_label.add_theme_font_size_override("font_size", 10)
	storage_label.modulate = Color(1, 1, 1, 0.6)
	vbox.add_child(storage_label)


func _provider_id() -> String:
	var idx = provider_option.selected
	if idx < 0 or idx >= PROVIDERS.size():
		return "claude-code"
	return PROVIDERS[idx]["id"]


func _on_provider_selected(_index: int) -> void:
	_populate_models(_provider_id(), "")
	_load_key_for_provider(_provider_id())
	_update_visibility()


func _on_model_selected(index: int) -> void:
	# Last item is always "Custom model…" — reveal the free-text field
	var is_custom = index == model_option.item_count - 1
	_custom_model_row.visible = is_custom or _provider_id() == "custom"


func _populate_models(provider: String, selected_model: String) -> void:
	model_option.clear()
	var models = get_known_models(provider)
	var selected_idx = -1
	for i in models.size():
		model_option.add_item(models[i]["label"])
		if models[i]["id"] == selected_model:
			selected_idx = i
	model_option.add_item("Custom model…")

	if models.is_empty():
		# Custom provider: only the free-text field matters
		model_option.select(0)
		custom_model_edit.text = selected_model
	elif selected_idx >= 0:
		model_option.select(selected_idx)
		custom_model_edit.text = ""
	elif selected_model != "":
		# Saved model not in the known list — treat as custom
		model_option.select(model_option.item_count - 1)
		custom_model_edit.text = selected_model
	else:
		model_option.select(0)
		custom_model_edit.text = ""


func _load_key_for_provider(provider: String) -> void:
	match provider:
		"claude-code", "anthropic":
			api_key_edit.text = _setting(ES_KEY_ANTHROPIC, "")
			api_key_edit.placeholder_text = "sk-ant-...  (optional for Claude Code)" if provider == "claude-code" else "sk-ant-..."
		"openai":
			api_key_edit.text = _setting(ES_KEY_OPENAI, "")
			api_key_edit.placeholder_text = "sk-..."
		"custom":
			api_key_edit.text = _setting(ES_KEY_CUSTOM, "")
			api_key_edit.placeholder_text = "key (optional for local endpoints)"
	base_url_edit.text = _setting(ES_BASE_URL_CUSTOM, "")


func _update_visibility() -> void:
	var provider = _provider_id()
	_base_url_row.visible = provider == "custom"
	var is_custom_model = model_option.selected == model_option.item_count - 1
	_custom_model_row.visible = is_custom_model or provider == "custom"
	note_label.text = PROVIDER_NOTES.get(provider, "")


func _load_from_settings() -> void:
	## Sync dialog state from EditorSettings each time it opens.
	var provider = get_provider()
	for i in PROVIDERS.size():
		if PROVIDERS[i]["id"] == provider:
			provider_option.select(i)
			break
	_populate_models(provider, get_model_for_provider(provider))
	_load_key_for_provider(provider)
	_update_visibility()


func _selected_model() -> String:
	var provider = _provider_id()
	var models = get_known_models(provider)
	var idx = model_option.selected
	if provider == "custom" or idx >= models.size() or idx < 0:
		return custom_model_edit.text.strip_edges()
	return models[idx]["id"]


func _on_confirmed() -> void:
	var provider = _provider_id()
	var model = _selected_model()
	if model.is_empty():
		model = DEFAULT_OPENAI_MODEL if provider == "openai" else DEFAULT_CLAUDE_MODEL

	_store(ES_PROVIDER, provider)
	set_model_for_provider(provider, model)
	match provider:
		"claude-code", "anthropic":
			_store(ES_KEY_ANTHROPIC, api_key_edit.text.strip_edges())
		"openai":
			_store(ES_KEY_OPENAI, api_key_edit.text.strip_edges())
		"custom":
			_store(ES_KEY_CUSTOM, api_key_edit.text.strip_edges())
			_store(ES_BASE_URL_CUSTOM, base_url_edit.text.strip_edges())

	settings_changed.emit(get_config())

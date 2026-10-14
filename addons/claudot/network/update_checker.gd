@tool
extends Node

## UpdateChecker - Polls the GitHub "latest release" endpoint for a newer Claudot build.
##
## Call check() (e.g. on plugin load); it is rate-limited to once per CHECK_INTERVAL_SEC
## unless forced. Emits exactly one of update_available / up_to_date / check_failed per
## request that is actually sent. Skipped tags are NOT filtered here — the UI decides
## (see is_skipped()). All EditorSettings access is guarded so this loads headless.

const VersionUtil = preload("res://addons/claudot/util/version_util.gd")

signal update_available(info: Dictionary)
signal up_to_date(installed: String, latest: String)
signal check_failed(reason: String)

const REPO := "Claudot-Code/claudot-pub"
const LATEST_RELEASE_URL := "https://api.github.com/repos/Claudot-Code/claudot-pub/releases/latest"
const RELEASES_PAGE_URL := "https://github.com/Claudot-Code/claudot-pub/releases"
const PLUGIN_CFG := "res://addons/claudot/plugin.cfg"

const ES_AUTO_CHECK := "claudot/updates/auto_check"
const ES_LAST_CHECK_UNIX := "claudot/updates/last_check_unix"
const ES_SKIPPED_TAG := "claudot/updates/skipped_tag"

const CHECK_INTERVAL_SEC := 24 * 60 * 60
const REQUEST_TIMEOUT_SEC := 10.0

## Most recent successful result (info Dictionary as emitted by update_available), so UI can re-open it.
var last_info: Dictionary = {}

var _http: HTTPRequest = null
var _in_flight: bool = false
var _installed_at_request: String = ""


## ---------------------------------------------------------------------------
## Static settings access
## ---------------------------------------------------------------------------

static func _editor_settings() -> Object:
	## EditorSettings, or null when not running inside the editor (headless/tests).
	if not Engine.is_editor_hint() or not Engine.has_singleton("EditorInterface"):
		return null
	var ei = Engine.get_singleton("EditorInterface")
	return ei.get_editor_settings() if ei else null


static func _get_setting(key: String, default_value: Variant) -> Variant:
	var es = _editor_settings()
	if es and es.has_setting(key):
		var v = es.get_setting(key)
		if typeof(v) == typeof(default_value):
			return v
	return default_value


static func _set_setting(key: String, value: Variant) -> void:
	var es = _editor_settings()
	if es:
		es.set_setting(key, value)


static func get_installed_version() -> String:
	## Version string from plugin.cfg [plugin] version, or "" on failure.
	var cfg := ConfigFile.new()
	if cfg.load(PLUGIN_CFG) != OK:
		return ""
	return str(cfg.get_value("plugin", "version", ""))


static func is_auto_check_enabled() -> bool:
	return _get_setting(ES_AUTO_CHECK, true)


static func set_auto_check_enabled(v: bool) -> void:
	_set_setting(ES_AUTO_CHECK, v)


static func get_skipped_tag() -> String:
	return _get_setting(ES_SKIPPED_TAG, "")


static func skip_tag(tag: String) -> void:
	_set_setting(ES_SKIPPED_TAG, tag)


static func clear_skipped_tag() -> void:
	_set_setting(ES_SKIPPED_TAG, "")


func is_skipped(tag: String) -> bool:
	## True if the user chose "skip this version" for tag.
	var skipped := get_skipped_tag()
	return skipped != "" and skipped == tag


## ---------------------------------------------------------------------------
## Checking
## ---------------------------------------------------------------------------

func check(force: bool = false) -> void:
	## Query GitHub for the latest release. Without force, respects the auto-check
	## toggle and the 24h interval (emitting nothing when skipped).
	if _in_flight:
		return
	if not is_inside_tree():
		_fail("checker not in scene tree")
		return
	if not force:
		if not is_auto_check_enabled():
			return
		var last: int = _get_setting(ES_LAST_CHECK_UNIX, 0)
		var now := int(Time.get_unix_time_from_system())
		if last > 0 and now - last < CHECK_INTERVAL_SEC:
			return
	if _http == null:
		_http = HTTPRequest.new()
		_http.timeout = REQUEST_TIMEOUT_SEC
		_http.use_threads = false
		_http.request_completed.connect(_on_request_completed)
		add_child(_http)
	_installed_at_request = get_installed_version()
	var headers := PackedStringArray([
		"Accept: application/vnd.github+json",
		"User-Agent: Claudot/%s" % (_installed_at_request if _installed_at_request != "" else "unknown"),
		"X-GitHub-Api-Version: 2022-11-28",
	])
	var err := _http.request(LATEST_RELEASE_URL, headers)
	if err != OK:
		_fail("request error %d" % err)
		return
	_in_flight = true


func _on_request_completed(result: int, response_code: int, _headers: PackedStringArray, body: PackedByteArray) -> void:
	_in_flight = false
	if result != HTTPRequest.RESULT_SUCCESS:
		_fail("network error %d" % result)
		return
	# Any completed HTTP response counts as a check (avoids hammering on 4xx).
	_set_setting(ES_LAST_CHECK_UNIX, int(Time.get_unix_time_from_system()))
	if response_code != 200:
		var reason := "HTTP %d" % response_code
		if response_code == 403 or response_code == 429:
			reason += " (rate limited)"
		elif response_code == 404:
			reason += " (no releases found)"
		_fail(reason)
		return
	var parsed := parse_release_json(body.get_string_from_utf8(), _installed_at_request)
	if not parsed["ok"]:
		_fail(parsed["error"])
		return
	var info: Dictionary = parsed["info"]
	last_info = info
	if parsed["is_newer"]:
		print("[Claudot Updater] Update available: %s (installed %s)" % [info["tag"], _installed_at_request])
		update_available.emit(info)
	else:
		print_verbose("[Claudot Updater] Up to date (installed %s, latest %s)" % [_installed_at_request, info["version"]])
		up_to_date.emit(_installed_at_request, info["version"])


func _fail(reason: String) -> void:
	print_verbose("[Claudot Updater] Update check failed: " + reason)
	check_failed.emit(reason)


static func parse_release_json(text: String, installed_version: String) -> Dictionary:
	## Map a GitHub release JSON payload to an info Dictionary.
	## Returns {"ok": bool, "info": Dictionary, "is_newer": bool, "error": String}.
	var out := {"ok": false, "info": {}, "is_newer": false, "error": ""}
	var json := JSON.new()  # instance parse: no engine error spam on bad input
	if json.parse(text) != OK or typeof(json.data) != TYPE_DICTIONARY:
		out["error"] = "invalid JSON"
		return out
	var data: Dictionary = json.data
	var tag = data.get("tag_name")
	if not (tag is String) or tag == "":
		out["error"] = "missing tag_name"
		return out
	var info := {
		"tag": tag,
		"version": VersionUtil.normalize(tag),
		"html_url": _str(data.get("html_url")),
		"body": _str(data.get("body")),
		"published_at": _str(data.get("published_at")),
		"asset_name": "",
		"asset_url": "",
		"asset_size": 0,
		"sha256_url": "",
		"prerelease": data.get("prerelease") == true,
	}
	var assets = data.get("assets")
	if assets is Array:
		for a in assets:
			if not (a is Dictionary):
				continue
			var name := _str(a.get("name"))
			var url := _str(a.get("browser_download_url"))
			var lname := name.to_lower()
			if info["asset_url"] == "" and lname.ends_with(".zip"):
				info["asset_name"] = name
				info["asset_url"] = url
				var size = a.get("size")
				info["asset_size"] = int(size) if (size is int or size is float) else 0
			elif info["sha256_url"] == "" and lname.ends_with(".sha256"):
				info["sha256_url"] = url
	out["ok"] = true
	out["info"] = info
	out["is_newer"] = VersionUtil.is_newer(info["version"], installed_version)
	return out


static func _str(v: Variant) -> String:
	return v if v is String else ""

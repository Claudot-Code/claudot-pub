@tool
extends Node

## UpdateInstaller - Downloads a Claudot release zip and swaps it into addons/claudot.
##
## Automates the manual update procedure from the release notes:
##   "Close Godot first, delete the old addons/claudot/ folder entirely, then copy the
##    new one in (do not merge over a live install). Kill any leftover bridge daemon."
##
## Pipeline (start()): validate -> download -> verify -> extract to staging ->
## stop bridge -> back up the live addon -> wipe + copy the new addon in -> record.
## The editor restart is NOT done here; the update dialog does it on completed.
##
## Everything lives under user://claudot_update (staging, backup, downloaded zip),
## so nothing extra ends up in the project. bridge/.env (API keys) is carried over.
## The pure file helpers are static so they can be tested headless.

signal stage_changed(stage: String)
signal progress(fraction: float)  ## -1.0 means indeterminate
signal failed(message: String)
signal completed(from_version: String, to_version: String)

const UPDATE_DIR := "user://claudot_update"
const STAGING_DIR := UPDATE_DIR + "/staging"
const BACKUP_DIR := UPDATE_DIR + "/backup"
const BACKUP_INFO_FILE := UPDATE_DIR + "/backup_info.json"
const LIVE_DIR := "res://addons/claudot"
const RECORD_FILE := UPDATE_DIR + "/last_update.json"
const PRESERVE_FILES := ["bridge/.env"]
const ALLOWED_HOSTS := ["github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"]
const ZIP_ROOT := "addons/claudot/"

const DOWNLOAD_TIMEOUT_SEC := 120.0
const CHECKSUM_TIMEOUT_SEC := 30.0
const BRIDGE_STOP_WAIT_SEC := 3.0

## When true, start() runs validate/download/verify/extract only, then emits completed
## without stopping the bridge or touching LIVE_DIR. For testing.
@export var dry_run: bool = false

var _busy: bool = false
var _download_http: HTTPRequest = null


func is_busy() -> bool:
	return _busy


func _process(_delta: float) -> void:
	if _download_http == null:
		return
	var total := _download_http.get_body_size()
	if total > 0:
		progress.emit(clampf(float(_download_http.get_downloaded_bytes()) / float(total), 0.0, 1.0))
	else:
		progress.emit(-1.0)


## ---------------------------------------------------------------------------
## Public API
## ---------------------------------------------------------------------------

func start(info: Dictionary, tcp_client: Node, bridge_launcher: Node) -> void:
	## Run the full update pipeline as a coroutine. Emits failed or completed exactly once.
	if _busy:
		push_warning("[Claudot Updater] An update is already in progress.")
		return
	if not is_inside_tree():
		failed.emit("Internal error: the update installer is not in the scene tree.")
		return
	_busy = true
	var err := await _run_update(info, tcp_client, bridge_launcher)
	_busy = false
	if not err.is_empty():
		_wipe(STAGING_DIR)
		push_error("[Claudot Updater] " + err)
		failed.emit(err)


func restore_backup(tcp_client: Node = null, bridge_launcher: Node = null) -> void:
	## Put the backed-up addon (from the last update) back in place.
	if _busy:
		push_warning("[Claudot Updater] An update is already in progress.")
		return
	if not is_inside_tree():
		failed.emit("Internal error: the update installer is not in the scene tree.")
		return
	if not has_backup():
		failed.emit("No backup found in %s." % ProjectSettings.globalize_path(BACKUP_DIR))
		return
	_busy = true
	var current := _read_cfg_version(LIVE_DIR + "/plugin.cfg")
	var backup_version := get_backup_version()
	await _stop_bridge(tcp_client, bridge_launcher)
	stage_changed.emit("Restoring v%s…" % backup_version)
	progress.emit(-1.0)
	var err := _swap_in(ProjectSettings.globalize_path(BACKUP_DIR))
	_busy = false
	if not err.is_empty():
		push_error("[Claudot Updater] " + err)
		failed.emit(err)
		return
	_write_json(RECORD_FILE, {"from": current, "to": backup_version, "tag": "restore", "time": int(Time.get_unix_time_from_system())})
	print("[Claudot Updater] Restored v%s (was v%s)." % [backup_version, current])
	completed.emit(current, backup_version)


static func has_backup() -> bool:
	return FileAccess.file_exists(BACKUP_DIR + "/plugin.cfg")


static func get_backup_version() -> String:
	if FileAccess.file_exists(BACKUP_INFO_FILE):
		var data = JSON.parse_string(FileAccess.get_file_as_string(BACKUP_INFO_FILE))
		if data is Dictionary and str(data.get("version", "")) != "":
			return str(data["version"])
	return _read_cfg_version(BACKUP_DIR + "/plugin.cfg")


## ---------------------------------------------------------------------------
## Pipeline
## ---------------------------------------------------------------------------

func _run_update(info: Dictionary, tcp_client: Node, bridge_launcher: Node) -> String:
	## Returns "" on success (completed already emitted) or an error message.

	# a. Validate ---------------------------------------------------------------
	stage_changed.emit("Checking…")
	progress.emit(-1.0)
	var live_abs := ProjectSettings.globalize_path(LIVE_DIR)
	if not DirAccess.dir_exists_absolute(live_abs) or not FileAccess.file_exists(LIVE_DIR + "/plugin.cfg"):
		return "Claudot is not installed at %s (plugin.cfg missing), so it can't be updated in place." % live_abs
	var asset_url := str(info.get("asset_url", ""))
	var url_err := _validate_download_url(asset_url)
	if not url_err.is_empty():
		return url_err
	var new_version := str(info.get("version", ""))
	if new_version.is_empty():
		return "Release info has no version."
	var old_version := _read_cfg_version(LIVE_DIR + "/plugin.cfg")

	var asset_name := _safe_file_name(str(info.get("asset_name", "")), asset_url)
	var update_abs := ProjectSettings.globalize_path(UPDATE_DIR)
	var mk := DirAccess.make_dir_recursive_absolute(update_abs)
	if mk != OK:
		return "Could not create %s (error %d)." % [update_abs, mk]
	var zip_path := UPDATE_DIR + "/" + asset_name
	var zip_abs := ProjectSettings.globalize_path(zip_path)
	if FileAccess.file_exists(zip_path):
		var rm := DirAccess.remove_absolute(zip_abs)
		if rm != OK:
			return "Could not delete the stale download %s (error %d)." % [zip_abs, rm]

	# b. Download ---------------------------------------------------------------
	stage_changed.emit("Downloading…")
	var dl_err := await _download_to_file(asset_url, zip_path)
	if not dl_err.is_empty():
		_remove_file(zip_abs)
		return dl_err

	# c. Verify -----------------------------------------------------------------
	stage_changed.emit("Verifying…")
	progress.emit(-1.0)
	var expected_size := int(info.get("asset_size", 0))
	var f := FileAccess.open(zip_path, FileAccess.READ)
	if f == null:
		return "Downloaded file is missing: %s" % zip_abs
	var actual_size := f.get_length()
	f.close()
	if expected_size > 0 and actual_size != expected_size:
		_remove_file(zip_abs)
		return "Download size mismatch: expected %d bytes, got %d. Try again." % [expected_size, actual_size]
	var sha_url := str(info.get("sha256_url", ""))
	if sha_url.is_empty():
		print("[Claudot Updater] No .sha256 published for this release; checked size only.")
	else:
		var sha_url_err := _validate_download_url(sha_url)
		if not sha_url_err.is_empty():
			_remove_file(zip_abs)
			return "Checksum " + sha_url_err.substr(0, 1).to_lower() + sha_url_err.substr(1)
		var sha_res := await _fetch_text(sha_url)
		if not str(sha_res["error"]).is_empty():
			_remove_file(zip_abs)
			return "Could not download the checksum file: " + str(sha_res["error"])
		var expected_hash := _parse_sha256_text(str(sha_res["text"]))
		if expected_hash.is_empty():
			_remove_file(zip_abs)
			return "The checksum file is malformed (no SHA-256 hash found)."
		var actual_hash := FileAccess.get_sha256(zip_path).to_lower()
		if actual_hash != expected_hash:
			_remove_file(zip_abs)
			return "Checksum mismatch: the download is corrupt or was tampered with.\nexpected %s\ngot      %s" % [expected_hash, actual_hash]
		print("[Claudot Updater] SHA-256 verified: %s" % actual_hash)

	# d. Extract ----------------------------------------------------------------
	stage_changed.emit("Extracting…")
	var staging_abs := ProjectSettings.globalize_path(STAGING_DIR)
	var wipe_err := _wipe(STAGING_DIR)
	if not wipe_err.is_empty():
		return wipe_err
	var ex := _extract_zip_to(zip_path, staging_abs)
	if not str(ex["error"]).is_empty():
		return str(ex["error"])
	var staged_root := staging_abs.path_join("addons/claudot")
	var staged_cfg := staged_root.path_join("plugin.cfg")
	if not FileAccess.file_exists(staged_cfg):
		return "The release zip does not contain addons/claudot/plugin.cfg."
	var staged_version := _read_cfg_version(staged_cfg)
	if _normalize_version(staged_version) != _normalize_version(new_version):
		return "Version mismatch: the zip contains v%s but the release says v%s." % [staged_version, new_version]
	print("[Claudot Updater] Extracted %d files (v%s) to %s" % [int(ex["file_count"]), staged_version, staged_root])

	if dry_run:
		stage_changed.emit("Dry run: staged v%s, live install untouched." % staged_version)
		progress.emit(1.0)
		completed.emit(old_version, staged_version)
		return ""

	# e. Stop bridge ------------------------------------------------------------
	await _stop_bridge(tcp_client, bridge_launcher)

	# f. Backup -----------------------------------------------------------------
	stage_changed.emit("Backing up…")
	progress.emit(-1.0)
	var backup_abs := ProjectSettings.globalize_path(BACKUP_DIR)
	wipe_err = _wipe(BACKUP_DIR)
	if not wipe_err.is_empty():
		return wipe_err
	var cp_err := _copy_dir_recursive(live_abs, backup_abs, true)
	if not cp_err.is_empty():
		return "Backup failed, nothing was changed: " + cp_err
	_write_json(BACKUP_INFO_FILE, {"version": old_version, "time": int(Time.get_unix_time_from_system())})

	# g. Swap -------------------------------------------------------------------
	# Synchronous on purpose: no awaits between delete and copy, so the editor never
	# gets a frame (and a filesystem rescan) with a half-installed addon.
	stage_changed.emit("Installing…")
	var swap_err := _swap_in(staged_root)
	if not swap_err.is_empty():
		return swap_err

	# h. Record + cleanup -------------------------------------------------------
	_write_json(RECORD_FILE, {"from": old_version, "to": staged_version, "tag": str(info.get("tag", "")), "time": int(Time.get_unix_time_from_system())})
	_wipe(STAGING_DIR)
	DirAccess.remove_absolute(staging_abs)
	_remove_file(zip_abs)
	progress.emit(1.0)
	print("[Claudot Updater] Updated v%s -> v%s. Backup of v%s kept in %s" % [old_version, staged_version, old_version, backup_abs])
	completed.emit(old_version, staged_version)
	return ""


func _swap_in(source_abs: String) -> String:
	## Replace LIVE_DIR contents with source_abs contents, keeping PRESERVE_FILES.
	## On failure, restores from BACKUP_DIR (unless the source IS the backup).
	var live_abs := ProjectSettings.globalize_path(LIVE_DIR)
	var backup_abs := ProjectSettings.globalize_path(BACKUP_DIR)
	var preserved := _read_preserved(live_abs)

	var err := _remove_dir_contents(live_abs)
	if err.is_empty():
		err = _copy_dir_recursive(source_abs, live_abs)
	if err.is_empty():
		err = _write_preserved(live_abs, preserved)
	if err.is_empty():
		return ""

	# Something broke midway: put the old addon back.
	if source_abs.simplify_path() == backup_abs.simplify_path():
		_write_preserved(live_abs, preserved)
		return "Restore failed: %s\nThe backup is still at %s; copy it into %s manually with Godot closed." % [err, backup_abs, live_abs]
	var restore_err := _remove_dir_contents(live_abs)
	if restore_err.is_empty():
		restore_err = _copy_dir_recursive(backup_abs, live_abs)
	_write_preserved(live_abs, preserved)
	if restore_err.is_empty():
		return "Install failed: %s\nThe previous version was restored automatically from the backup." % err
	return "Install failed: %s\nAutomatic restore ALSO failed: %s\nThe previous version is backed up at %s. Close Godot and copy it into %s manually." % [err, restore_err, backup_abs, live_abs]


func _stop_bridge(tcp_client: Node, bridge_launcher: Node) -> void:
	## Ask the daemon to exit (uv exits after delegating, so the python pid is not
	## tracked by BridgeLauncher), drop the connection, then kill what we do track.
	stage_changed.emit("Stopping bridge…")
	progress.emit(-1.0)
	if tcp_client != null and is_instance_valid(tcp_client):
		if tcp_client.has_method("send_message"):
			var id = tcp_client.send_message("chat/shutdown", {})
			if typeof(id) == TYPE_INT and int(id) >= 0:
				print("[Claudot Updater] Sent chat/shutdown to the bridge.")
				await get_tree().process_frame
		if tcp_client.has_method("disconnect_from_bridge"):
			tcp_client.disconnect_from_bridge()
	if bridge_launcher != null and is_instance_valid(bridge_launcher) and bridge_launcher.has_method("stop"):
		bridge_launcher.stop()
	var waited := 0.0
	while waited < BRIDGE_STOP_WAIT_SEC:
		if bridge_launcher == null or not is_instance_valid(bridge_launcher) or not bridge_launcher.has_method("is_running") or not bridge_launcher.is_running():
			break
		await get_tree().create_timer(0.1).timeout
		waited += 0.1
	# Give an untracked daemon a moment to act on chat/shutdown.
	await get_tree().create_timer(0.5).timeout


## ---------------------------------------------------------------------------
## Networking
## ---------------------------------------------------------------------------

func _download_to_file(url: String, dest_path: String) -> String:
	var http := HTTPRequest.new()
	http.download_file = dest_path
	http.timeout = DOWNLOAD_TIMEOUT_SEC
	add_child(http)
	var req := http.request(url)
	if req != OK:
		http.queue_free()
		return "Could not start the download (error %d)." % req
	_download_http = http
	progress.emit(0.0)
	var res: Array = await http.request_completed
	_download_http = null
	http.queue_free()
	var err := _http_error(int(res[0]), int(res[1]))
	if not err.is_empty():
		return "Download failed: " + err
	progress.emit(1.0)
	return ""


func _fetch_text(url: String) -> Dictionary:
	var http := HTTPRequest.new()
	http.timeout = CHECKSUM_TIMEOUT_SEC
	http.body_size_limit = 64 * 1024
	add_child(http)
	var req := http.request(url)
	if req != OK:
		http.queue_free()
		return {"error": "could not start request (error %d)" % req, "text": ""}
	var res: Array = await http.request_completed
	http.queue_free()
	var err := _http_error(int(res[0]), int(res[1]))
	if not err.is_empty():
		return {"error": err, "text": ""}
	return {"error": "", "text": (res[3] as PackedByteArray).get_string_from_utf8()}


static func _http_error(result: int, code: int) -> String:
	match result:
		HTTPRequest.RESULT_SUCCESS:
			pass
		HTTPRequest.RESULT_TIMEOUT:
			return "timed out."
		HTTPRequest.RESULT_CANT_RESOLVE:
			return "could not resolve host (offline?)."
		HTTPRequest.RESULT_CANT_CONNECT, HTTPRequest.RESULT_CONNECTION_ERROR:
			return "could not connect (offline or blocked?)."
		HTTPRequest.RESULT_TLS_HANDSHAKE_ERROR:
			return "TLS handshake failed."
		HTTPRequest.RESULT_BODY_SIZE_LIMIT_EXCEEDED:
			return "response too large."
		HTTPRequest.RESULT_DOWNLOAD_FILE_CANT_OPEN, HTTPRequest.RESULT_DOWNLOAD_FILE_WRITE_ERROR:
			return "could not write the downloaded file."
		HTTPRequest.RESULT_REDIRECT_LIMIT_REACHED:
			return "too many redirects."
		_:
			return "request error %d." % result
	if code != 200:
		return "server returned HTTP %d." % code
	return ""


static func _validate_download_url(url: String) -> String:
	## "" if url is https on an allowed GitHub host, else an error message.
	if url.is_empty():
		return "This release has no downloadable .zip asset. Use the manual update instead."
	if not url.begins_with("https://"):
		return "Refusing non-HTTPS download URL: %s" % url
	var authority := url.substr(8).get_slice("/", 0).get_slice("?", 0).get_slice("#", 0)
	if authority.contains("@"):
		return "Refusing download URL with credentials: %s" % url
	var host := authority.get_slice(":", 0).to_lower()
	if not ALLOWED_HOSTS.has(host):
		return "Refusing download from unexpected host '%s'." % host
	return ""


static func _parse_sha256_text(text: String) -> String:
	## First token of a sha256sum line ("<hex>  <filename>"), lowercased, if it is 64 hex chars.
	var token := text.strip_edges().get_slice("\n", 0).strip_edges()
	token = token.replace("\t", " ").get_slice(" ", 0).to_lower()
	if token.length() != 64 or not token.is_valid_hex_number(false):
		return ""
	return token


static func _safe_file_name(asset_name: String, url: String) -> String:
	var n := asset_name.get_file() if not asset_name.is_empty() else url.get_slice("?", 0).get_file()
	if n.is_empty() or n.contains("..") or n.contains("/") or n.contains("\\") or n.contains(":"):
		n = "claudot_update.zip"
	return n


## ---------------------------------------------------------------------------
## Pure file helpers (static, headless-testable)
## ---------------------------------------------------------------------------

static func _abs(path: String) -> String:
	## res:// and user:// -> absolute OS path; absolute paths pass through.
	return ProjectSettings.globalize_path(path)


static func _normalize_version(v: String) -> String:
	var s := v.strip_edges().to_lower()
	if s.begins_with("v"):
		s = s.substr(1)
	return s


static func _read_cfg_version(cfg_path: String) -> String:
	var cfg := ConfigFile.new()
	if cfg.load(cfg_path) != OK:
		return ""
	return str(cfg.get_value("plugin", "version", ""))


static func _validate_zip_entries(files: PackedStringArray) -> String:
	## "" if every entry is a safe path under addons/claudot/, else an error message.
	## The parent directory entries "addons/" and "addons/claudot/" are allowed.
	if files.is_empty():
		return "The release zip is empty."
	for entry in files:
		if entry.is_empty():
			return "The release zip contains an entry with an empty name."
		if entry.contains("..") or entry.contains("\\") or entry.contains(":") or entry.begins_with("/"):
			return "Unsafe path in release zip: '%s'" % entry
		if entry.length() >= 2 and entry[1] == ":":
			return "Unsafe path in release zip: '%s'" % entry
		if entry == "addons/" or entry == ZIP_ROOT:
			continue
		if not entry.begins_with(ZIP_ROOT):
			return "Unexpected file outside addons/claudot/ in release zip: '%s'" % entry
	return ""


static func _extract_zip_to(zip_path: String, dest_dir: String) -> Dictionary:
	## Extract a validated zip into dest_dir. Returns {"error": String, "file_count": int}.
	var dest_abs := _abs(dest_dir)
	var zip := ZIPReader.new()
	var oerr := zip.open(zip_path)
	if oerr != OK:
		return {"error": "Could not open the downloaded zip (error %d); it may be corrupt." % oerr, "file_count": 0}
	var files := zip.get_files()
	var verr := _validate_zip_entries(files)
	if not verr.is_empty():
		zip.close()
		return {"error": verr, "file_count": 0}
	var count := 0
	for entry in files:
		var target := dest_abs.path_join(entry)
		if entry.ends_with("/"):
			var derr := DirAccess.make_dir_recursive_absolute(target)
			if derr != OK:
				zip.close()
				return {"error": "Could not create %s (error %d)." % [target, derr], "file_count": count}
			continue
		var perr := DirAccess.make_dir_recursive_absolute(target.get_base_dir())
		if perr != OK:
			zip.close()
			return {"error": "Could not create %s (error %d)." % [target.get_base_dir(), perr], "file_count": count}
		var data := zip.read_file(entry)
		var out := FileAccess.open(target, FileAccess.WRITE)
		if out == null:
			zip.close()
			return {"error": "Could not write %s (error %d)." % [target, FileAccess.get_open_error()], "file_count": count}
		out.store_buffer(data)
		out.close()
		count += 1
	zip.close()
	return {"error": "", "file_count": count}


static func _copy_dir_recursive(src_dir: String, dst_dir: String, skip_cache: bool = false) -> String:
	## Copy src_dir's contents into dst_dir (created if needed). "" on success.
	## skip_cache drops __pycache__ dirs and *.pyc files.
	var src_abs := _abs(src_dir)
	var dst_abs := _abs(dst_dir)
	var mk := DirAccess.make_dir_recursive_absolute(dst_abs)
	if mk != OK:
		return "Could not create %s (error %d)." % [dst_abs, mk]
	var dir := DirAccess.open(src_abs)
	if dir == null:
		return "Could not open %s (error %d)." % [src_abs, DirAccess.get_open_error()]
	dir.include_hidden = true
	dir.include_navigational = false
	var lerr := dir.list_dir_begin()
	if lerr != OK:
		return "Could not list %s (error %d)." % [src_abs, lerr]
	var name := dir.get_next()
	while name != "":
		var s := src_abs.path_join(name)
		var d := dst_abs.path_join(name)
		if dir.current_is_dir():
			if not (skip_cache and name == "__pycache__"):
				var sub := _copy_dir_recursive(s, d, skip_cache)
				if not sub.is_empty():
					dir.list_dir_end()
					return sub
		elif not (skip_cache and name.ends_with(".pyc")):
			var cerr := DirAccess.copy_absolute(s, d)
			if cerr != OK:
				dir.list_dir_end()
				return "Could not copy %s (error %d)." % [s, cerr]
		name = dir.get_next()
	dir.list_dir_end()
	return ""


static func _remove_dir_contents(dir_path: String) -> String:
	## Delete everything inside dir_path (but not dir_path itself). "" on success.
	var dir_abs := _abs(dir_path)
	if not DirAccess.dir_exists_absolute(dir_abs):
		return ""
	var dir := DirAccess.open(dir_abs)
	if dir == null:
		return "Could not open %s (error %d)." % [dir_abs, DirAccess.get_open_error()]
	dir.include_hidden = true
	dir.include_navigational = false
	# Collect first: deleting while iterating is unreliable on some platforms.
	var subdirs: PackedStringArray = []
	var files: PackedStringArray = []
	dir.list_dir_begin()
	var name := dir.get_next()
	while name != "":
		if dir.current_is_dir():
			subdirs.append(name)
		else:
			files.append(name)
		name = dir.get_next()
	dir.list_dir_end()
	for f in files:
		var p := dir_abs.path_join(f)
		var rerr := DirAccess.remove_absolute(p)
		if rerr != OK:
			return "Could not delete %s (error %d). Is it open in another program?" % [p, rerr]
	for sd in subdirs:
		var p := dir_abs.path_join(sd)
		var sub := _remove_dir_contents(p)
		if not sub.is_empty():
			return sub
		var rerr := DirAccess.remove_absolute(p)
		if rerr != OK:
			return "Could not delete folder %s (error %d)." % [p, rerr]
	return ""


static func _count_files(dir_path: String) -> int:
	## Number of regular files under dir_path (recursive).
	var dir := DirAccess.open(_abs(dir_path))
	if dir == null:
		return 0
	dir.include_hidden = true
	dir.include_navigational = false
	var n := 0
	dir.list_dir_begin()
	var name := dir.get_next()
	while name != "":
		if dir.current_is_dir():
			n += _count_files(_abs(dir_path).path_join(name))
		else:
			n += 1
		name = dir.get_next()
	dir.list_dir_end()
	return n


static func _read_preserved(live_dir: String) -> Dictionary:
	## {relative_path: PackedByteArray} for each PRESERVE_FILES entry present in live_dir.
	var out := {}
	for rel in PRESERVE_FILES:
		var p := _abs(live_dir).path_join(rel)
		if FileAccess.file_exists(p):
			out[rel] = FileAccess.get_file_as_bytes(p)
	return out


static func _write_preserved(live_dir: String, preserved: Dictionary) -> String:
	for rel in preserved:
		var p := _abs(live_dir).path_join(rel)
		DirAccess.make_dir_recursive_absolute(p.get_base_dir())
		var f := FileAccess.open(p, FileAccess.WRITE)
		if f == null:
			return "Could not restore %s (error %d)." % [p, FileAccess.get_open_error()]
		f.store_buffer(preserved[rel])
		f.close()
	return ""


static func _wipe(dir_path: String) -> String:
	## Empty dir_path (if it exists) and make sure it exists.
	var err := _remove_dir_contents(dir_path)
	if not err.is_empty():
		return err
	var mk := DirAccess.make_dir_recursive_absolute(_abs(dir_path))
	if mk != OK:
		return "Could not create %s (error %d)." % [_abs(dir_path), mk]
	return ""


static func _remove_file(abs_path: String) -> void:
	if FileAccess.file_exists(abs_path):
		DirAccess.remove_absolute(abs_path)


static func _write_json(path: String, data: Dictionary) -> void:
	DirAccess.make_dir_recursive_absolute(_abs(path).get_base_dir())
	var f := FileAccess.open(path, FileAccess.WRITE)
	if f == null:
		push_warning("[Claudot Updater] Could not write %s (error %d)." % [path, FileAccess.get_open_error()])
		return
	f.store_string(JSON.stringify(data, "\t"))
	f.close()

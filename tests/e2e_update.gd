extends SceneTree

## End-to-end: real GitHub check + real in-place install of the latest release.
## DESTRUCTIVE: replaces addons/claudot in the project it runs in. Use a scratch copy:
##   copy addons/ and tests/ into a throwaway project, lower the version in
##   addons/claudot/plugin.cfg, optionally add bridge/.env and STALE_FILE.txt, then
##   godot --headless --path <proj> -s res://tests/e2e_update.gd
## Not part of run_tests.gd on purpose.

const CheckerScript = preload("res://addons/claudot/network/update_checker.gd")
const InstallerScript = preload("res://addons/claudot/network/update_installer.gd")

var _info: Dictionary = {}
var _check_failed: String = ""
var _done := false
var _ok := false
var _msg := ""


func _initialize() -> void:
	call_deferred("_run")


func _on_update_available(i: Dictionary) -> void:
	_info = i


func _on_check_failed(r: String) -> void:
	_check_failed = r


func _on_up_to_date(a: String, b: String) -> void:
	_check_failed = "up_to_date %s %s" % [a, b]


func _on_stage(s: String) -> void:
	print("[E2E] stage: ", s)


func _on_completed(a: String, b: String) -> void:
	_done = true
	_ok = true
	_msg = "%s -> %s" % [a, b]


func _on_failed(m: String) -> void:
	_done = true
	_msg = m


func _run() -> void:
	await process_frame
	var before := _cfg_version("res://addons/claudot/plugin.cfg")
	print("[E2E] installed before: ", before)

	var checker = CheckerScript.new()
	root.add_child(checker)
	await process_frame
	checker.update_available.connect(_on_update_available)
	checker.check_failed.connect(_on_check_failed)
	checker.up_to_date.connect(_on_up_to_date)
	checker.check(true)
	var waited := 0.0
	while _info.is_empty() and _check_failed == "" and waited < 15.0:
		await create_timer(0.1).timeout
		waited += 0.1
	if _info.is_empty():
		print("[E2E] FAIL: checker gave no update: ", _check_failed)
		quit(1)
		return
	print("[E2E] update available: %s asset=%s size=%d has_sha=%s" % [_info.tag, _info.asset_name, _info.asset_size, _info.sha256_url != ""])

	var installer = InstallerScript.new()
	root.add_child(installer)
	await process_frame
	installer.stage_changed.connect(_on_stage)
	installer.completed.connect(_on_completed)
	installer.failed.connect(_on_failed)
	installer.start(_info, null, null)
	waited = 0.0
	while not _done and waited < 180.0:
		await create_timer(0.1).timeout
		waited += 0.1
	print("[E2E] result: ok=%s %s" % [_ok, _msg])

	var after := _cfg_version("res://addons/claudot/plugin.cfg")
	var backup_version: String = InstallerScript.get_backup_version()
	var has_record := FileAccess.file_exists("user://claudot_update/last_update.json")
	var staging_gone := not DirAccess.dir_exists_absolute(ProjectSettings.globalize_path("user://claudot_update/staging"))
	var env_kept := FileAccess.file_exists("res://addons/claudot/bridge/.env")
	var installer_present := FileAccess.file_exists("res://addons/claudot/network/update_installer.gd")
	var stale_gone := not FileAccess.file_exists("res://addons/claudot/STALE_FILE.txt")
	print("[E2E] after: live=%s backup=%s record=%s staging_gone=%s env_kept=%s installer_present=%s stale_gone=%s" % [after, backup_version, has_record, staging_gone, env_kept, installer_present, stale_gone])
	var passed := _ok and after == "3.2.0-beta" and backup_version == before and has_record and staging_gone and env_kept and installer_present and stale_gone
	print("[E2E] ", "PASS" if passed else "FAIL")
	quit(0 if passed else 1)


func _cfg_version(path: String) -> String:
	var cfg := ConfigFile.new()
	if cfg.load(path) != OK:
		return ""
	return str(cfg.get_value("plugin", "version", ""))

extends RefCounted

## Tests for UpdateInstaller's pure file helpers (zip validation, extract, copy, wipe,
## .env preservation). Uses a release zip if one is available; see _find_release_zip().

const UpdateInstaller = preload("res://addons/claudot/network/update_installer.gd")
const UpdateDialog = preload("res://addons/claudot/ui/update_dialog.gd")

const TMP := "user://claudot_installer_test"
## Optional real release zip for the extract round-trip. Override with env CLAUDOT_TEST_ZIP.
const DEFAULT_ZIP := "C:/Users/ryanr/AppData/Local/Temp/claude/C--Users-ryanr-Desktop-NOVOGODOT-claudot-public/94922298-c516-4ae8-a4e6-0fc72b7a467c/scratchpad/claudot-v3.1-BETA.zip"
const V31_ENTRIES := 65
const V31_DIR_ENTRIES := 7

var failures: Array[String] = []


func check(cond: bool, msg: String) -> void:
	if not cond:
		failures.append(msg)


func eq(actual: Variant, expected: Variant, msg: String) -> void:
	if typeof(actual) != typeof(expected) or actual != expected:
		failures.append("%s: expected %s, got %s" % [msg, var_to_str(expected), var_to_str(actual)])


func _tmp(sub: String) -> String:
	var p := ProjectSettings.globalize_path(TMP).path_join(sub)
	UpdateInstaller._remove_dir_contents(p)
	DirAccess.make_dir_recursive_absolute(p)
	return p


func _write(path: String, text: String) -> void:
	DirAccess.make_dir_recursive_absolute(path.get_base_dir())
	var f := FileAccess.open(path, FileAccess.WRITE)
	f.store_string(text)
	f.close()


func _find_release_zip() -> String:
	var p := OS.get_environment("CLAUDOT_TEST_ZIP")
	if p.is_empty():
		p = DEFAULT_ZIP
	return p if FileAccess.file_exists(p) else ""


func test_scripts_load() -> void:
	check(UpdateInstaller != null, "update_installer.gd loads")
	check(UpdateDialog != null, "update_dialog.gd loads")
	var inst = UpdateInstaller.new()
	check(inst.has_signal("completed") and inst.has_signal("failed"), "installer signals")
	inst.free()
	var dlg = UpdateDialog.new()
	check(dlg.has_signal("skip_requested"), "dialog skip_requested signal")
	eq(dlg.get_ok_button().text, "Update and restart", "ok button text")
	eq(dlg.get_cancel_button().text, "Later", "cancel button text")
	dlg.free()


func test_validate_zip_entries_rejects() -> void:
	for bad in ["../x", "/abs", "C:/x", "other/file", "addons/claudot/../../evil.gd",
			"addons/claudot\\..\\x", "addons/other/plugin.cfg", "addons/claudotx/a.gd", ""]:
		var err := UpdateInstaller._validate_zip_entries(PackedStringArray(["addons/claudot/plugin.cfg", bad]))
		check(not err.is_empty(), "should reject '%s'" % bad)
	check(not UpdateInstaller._validate_zip_entries(PackedStringArray()).is_empty(), "rejects empty zip")


func test_validate_zip_entries_accepts() -> void:
	eq(UpdateInstaller._validate_zip_entries(PackedStringArray(["addons/claudot/plugin.cfg"])), "", "plugin.cfg")
	eq(UpdateInstaller._validate_zip_entries(PackedStringArray(["addons/claudot/ui/"])), "", "dir entry")
	eq(UpdateInstaller._validate_zip_entries(PackedStringArray(["addons/", "addons/claudot/", "addons/claudot/bridge/.env.example"])), "", "parent dir entries (as in real zips)")


func test_url_validation() -> void:
	eq(UpdateInstaller._validate_download_url("https://github.com/Claudot-Code/claudot-pub/releases/download/v3.1-BETA/claudot-v3.1-BETA.zip"), "", "github ok")
	eq(UpdateInstaller._validate_download_url("https://objects.githubusercontent.com/x"), "", "objects ok")
	check(not UpdateInstaller._validate_download_url("").is_empty(), "empty rejected")
	check(not UpdateInstaller._validate_download_url("http://github.com/x.zip").is_empty(), "http rejected")
	check(not UpdateInstaller._validate_download_url("https://evil.com/x.zip").is_empty(), "other host rejected")
	check(not UpdateInstaller._validate_download_url("https://github.com.evil.com/x.zip").is_empty(), "suffix host rejected")
	check(not UpdateInstaller._validate_download_url("https://github.com@evil.com/x.zip").is_empty(), "userinfo rejected")


func test_sha256_parse_and_version_normalize() -> void:
	var h := "ABCDEF0123456789abcdef0123456789ABCDEF0123456789abcdef0123456789"
	eq(UpdateInstaller._parse_sha256_text(h + "  claudot-v3.2.0.zip\n"), h.to_lower(), "sha256sum line")
	eq(UpdateInstaller._parse_sha256_text(h), h.to_lower(), "bare hash")
	eq(UpdateInstaller._parse_sha256_text("nothex  file.zip"), "", "malformed")
	eq(UpdateInstaller._normalize_version("v3.1.0-BETA"), "3.1.0-beta", "normalize")


func test_extract_copy_remove_roundtrip() -> void:
	var zip_path := _find_release_zip()
	if zip_path.is_empty():
		print("     (skipped: release zip not found; set CLAUDOT_TEST_ZIP)")
		return
	var dest := _tmp("extract")
	var res := UpdateInstaller._extract_zip_to(zip_path, dest)
	eq(res["error"], "", "extract error")
	eq(res["file_count"], V31_ENTRIES - V31_DIR_ENTRIES, "file count")
	check(FileAccess.file_exists(dest.path_join("addons/claudot/plugin.cfg")), "plugin.cfg extracted")
	eq(UpdateInstaller._read_cfg_version(dest.path_join("addons/claudot/plugin.cfg")), "3.1.0-beta", "staged version")
	eq(UpdateInstaller._count_files(dest), V31_ENTRIES - V31_DIR_ENTRIES, "files on disk")
	check(FileAccess.file_exists(dest.path_join("addons/claudot/bridge/.env.example")), "dotfile extracted")

	# Copy with cache skipping.
	_write(dest.path_join("addons/claudot/bridge/__pycache__/x.cpython-312.pyc"), "junk")
	_write(dest.path_join("addons/claudot/bridge/stray.pyc"), "junk")
	var copy := _tmp("copy")
	eq(UpdateInstaller._copy_dir_recursive(dest.path_join("addons/claudot"), copy, true), "", "copy error")
	eq(UpdateInstaller._count_files(copy), V31_ENTRIES - V31_DIR_ENTRIES, "copied file count (cache skipped)")
	check(not DirAccess.dir_exists_absolute(copy.path_join("bridge/__pycache__")), "__pycache__ skipped")
	check(FileAccess.file_exists(copy.path_join("bridge/.env.example")), "dotfile copied")
	eq(FileAccess.get_sha256(copy.path_join("plugin.cfg")), FileAccess.get_sha256(dest.path_join("addons/claudot/plugin.cfg")), "content identical")

	# Remove contents keeps the dir itself.
	eq(UpdateInstaller._remove_dir_contents(copy), "", "remove error")
	check(DirAccess.dir_exists_absolute(copy), "dir itself kept")
	eq(UpdateInstaller._count_files(copy), 0, "empty after remove")
	eq(DirAccess.get_directories_at(copy).size(), 0, "no subdirs after remove")
	UpdateInstaller._remove_dir_contents(ProjectSettings.globalize_path(TMP))


func test_extract_rejects_bad_zip() -> void:
	var dir := _tmp("badzip")
	var zip_path := dir.path_join("evil.zip")
	var packer := ZIPPacker.new()
	eq(packer.open(zip_path), OK, "create zip")
	packer.start_file("addons/claudot/plugin.cfg")
	packer.write_file("[plugin]\nversion=\"9\"\n".to_utf8_buffer())
	packer.close_file()
	packer.start_file("../escape.txt")
	packer.write_file("x".to_utf8_buffer())
	packer.close_file()
	packer.close()
	var out := dir.path_join("out")
	var res := UpdateInstaller._extract_zip_to(zip_path, out)
	check(not str(res["error"]).is_empty(), "bad zip rejected")
	check(not FileAccess.file_exists(out.path_join("addons/claudot/plugin.cfg")), "nothing extracted from bad zip")
	check(not FileAccess.file_exists(dir.path_join("escape.txt")), "no escape")
	UpdateInstaller._remove_dir_contents(ProjectSettings.globalize_path(TMP))


func test_preserved_env_survives_swap() -> void:
	var live := _tmp("live")
	var staged := _tmp("staged")
	_write(live.path_join("plugin.cfg"), "[plugin]\nversion=\"1.0\"\n")
	_write(live.path_join("bridge/agent_bridge.py"), "old")
	_write(live.path_join("bridge/.env"), "ANTHROPIC_API_KEY=secret")
	_write(live.path_join("old_only/gone.gd"), "old")
	_write(staged.path_join("plugin.cfg"), "[plugin]\nversion=\"2.0\"\n")
	_write(staged.path_join("bridge/agent_bridge.py"), "new")
	_write(staged.path_join("bridge/.env.example"), "EXAMPLE=1")

	# The same sequence _swap_in() runs, against temp dirs instead of res://.
	var preserved := UpdateInstaller._read_preserved(live)
	check(preserved.has("bridge/.env"), ".env captured")
	eq(UpdateInstaller._remove_dir_contents(live), "", "wipe live")
	eq(UpdateInstaller._copy_dir_recursive(staged, live), "", "copy staged")
	eq(UpdateInstaller._write_preserved(live, preserved), "", "write preserved")

	eq(FileAccess.get_file_as_string(live.path_join("bridge/.env")), "ANTHROPIC_API_KEY=secret", ".env survived")
	eq(FileAccess.get_file_as_string(live.path_join("bridge/agent_bridge.py")), "new", "file replaced")
	eq(UpdateInstaller._read_cfg_version(live.path_join("plugin.cfg")), "2.0", "new version")
	check(not DirAccess.dir_exists_absolute(live.path_join("old_only")), "old-only files removed (no merge)")
	check(FileAccess.file_exists(live.path_join("bridge/.env.example")), "new files present")
	UpdateInstaller._remove_dir_contents(ProjectSettings.globalize_path(TMP))

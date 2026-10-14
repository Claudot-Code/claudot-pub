extends SceneTree

## Headless test runner (no GUT). Usage:
##   godot --headless --path <project> -s res://tests/run_tests.gd
## Runs every func named test_* in each suite, prints PASS/FAIL lines, exits 0/1.

const SUITES := [
	"res://tests/test_version_util.gd",
	"res://tests/test_update_installer.gd",
]


func _init() -> void:
	var total := 0
	var failed := 0
	for path in SUITES:
		var script: GDScript = load(path)
		if script == null:
			print("FAIL %s (could not load)" % path)
			failed += 1
			continue
		var suite = script.new()
		for m in script.get_script_method_list():
			var name: String = m["name"]
			if not name.begins_with("test_"):
				continue
			total += 1
			suite.failures.clear()
			suite.call(name)
			if suite.failures.is_empty():
				print("PASS %s" % name)
			else:
				failed += 1
				print("FAIL %s" % name)
				for f in suite.failures:
					print("     - %s" % f)
	print("[Claudot Tests] %d/%d passed" % [total - failed, total])
	quit(1 if failed > 0 else 0)

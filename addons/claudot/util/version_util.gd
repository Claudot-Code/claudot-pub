extends RefCounted

## VersionUtil - Static helpers for parsing and comparing Claudot version strings.
##
## Accepts both release tags ("v3.1-BETA") and plugin.cfg versions ("3.1.0-beta").
## Rules:
##   - Leading "v"/"V" and surrounding whitespace are ignored; comparison is case-insensitive.
##   - Missing minor/patch components default to 0 ("3.1-beta" == "3.1.0-beta").
##   - Pre-release is everything after the first "-"; build metadata after "+" is ignored.
##   - At equal major.minor.patch, a release (no pre-release) beats any pre-release.
##   - Two pre-releases are compared dot-segment by dot-segment: numeric segments
##     numerically, otherwise as lowercase strings; a shorter list of equal
##     segments sorts lower ("beta" < "beta.1" < "beta.2" < "beta.10" < "rc").
##   - Invalid versions sort lowest; two invalid versions compare equal.
##
## Pure static, no editor dependencies — safe to use headless.


static func normalize(s: String) -> String:
	## Strip whitespace and a leading "v"/"V", then lowercase.
	var t := s.strip_edges()
	if t.begins_with("v") or t.begins_with("V"):
		t = t.substr(1)
	return t.to_lower()


static func parse(s: String) -> Dictionary:
	## Parse into {"major", "minor", "patch", "pre", "valid"}. Garbage -> valid=false.
	var out := {"major": 0, "minor": 0, "patch": 0, "pre": "", "valid": false}
	var t := normalize(s)
	var plus := t.find("+")
	if plus >= 0:
		t = t.substr(0, plus)
	var core := t
	var pre := ""
	var dash := t.find("-")
	if dash >= 0:
		core = t.substr(0, dash)
		pre = t.substr(dash + 1)
		if pre.is_empty():
			return out
	if core.is_empty():
		return out
	var parts := core.split(".")
	if parts.size() < 1 or parts.size() > 3:
		return out
	var nums: Array[int] = [0, 0, 0]
	for i in parts.size():
		var p: String = parts[i]
		if p.is_empty() or not p.is_valid_int() or p.begins_with("+") or p.begins_with("-"):
			return out
		nums[i] = p.to_int()
	out["major"] = nums[0]
	out["minor"] = nums[1]
	out["patch"] = nums[2]
	out["pre"] = pre
	out["valid"] = true
	return out


static func compare(a: String, b: String) -> int:
	## Returns -1 if a < b, 0 if equal, 1 if a > b.
	var pa := parse(a)
	var pb := parse(b)
	if not pa["valid"] or not pb["valid"]:
		if pa["valid"] == pb["valid"]:
			return 0
		return 1 if pa["valid"] else -1
	for key in ["major", "minor", "patch"]:
		if pa[key] != pb[key]:
			return 1 if pa[key] > pb[key] else -1
	var pre_a: String = pa["pre"]
	var pre_b: String = pb["pre"]
	if pre_a == pre_b:
		return 0
	if pre_a.is_empty():
		return 1
	if pre_b.is_empty():
		return -1
	return _compare_pre(pre_a, pre_b)


static func is_newer(candidate: String, installed: String) -> bool:
	## True if candidate is strictly newer than installed.
	return compare(candidate, installed) > 0


static func _compare_pre(a: String, b: String) -> int:
	var sa := a.split(".")
	var sb := b.split(".")
	var n: int = mini(sa.size(), sb.size())
	for i in n:
		var x: String = sa[i]
		var y: String = sb[i]
		if x == y:
			continue
		var xn := x.is_valid_int()
		var yn := y.is_valid_int()
		if xn and yn:
			return 1 if x.to_int() > y.to_int() else -1
		if xn != yn:
			return -1 if xn else 1  # numeric identifiers sort below alphanumeric (semver)
		return 1 if x > y else -1
	if sa.size() == sb.size():
		return 0
	return 1 if sa.size() > sb.size() else -1

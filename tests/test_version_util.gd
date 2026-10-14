extends RefCounted

## Tests for VersionUtil and UpdateChecker.parse_release_json.

const VersionUtil = preload("res://addons/claudot/util/version_util.gd")
const UpdateChecker = preload("res://addons/claudot/network/update_checker.gd")

var failures: Array[String] = []


func check(cond: bool, msg: String) -> void:
	if not cond:
		failures.append(msg)


func eq(actual: Variant, expected: Variant, msg: String) -> void:
	if typeof(actual) != typeof(expected) or actual != expected:
		failures.append("%s: expected %s, got %s" % [msg, var_to_str(expected), var_to_str(actual)])


func test_normalize() -> void:
	eq(VersionUtil.normalize("v3.1-BETA"), "3.1-beta", "v3.1-BETA")
	eq(VersionUtil.normalize("3.1.0-beta"), "3.1.0-beta", "3.1.0-beta")
	eq(VersionUtil.normalize("  V2.0 "), "2.0", "whitespace + V")
	eq(VersionUtil.normalize(""), "", "empty")


func test_parse_basic() -> void:
	var p := VersionUtil.parse("v3.2.0-beta")
	eq(p["valid"], true, "valid")
	eq(p["major"], 3, "major")
	eq(p["minor"], 2, "minor")
	eq(p["patch"], 0, "patch")
	eq(p["pre"], "beta", "pre")
	var r := VersionUtil.parse("3.2.0")
	eq(r["pre"], "", "release has no pre")
	eq(r["valid"], true, "release valid")


func test_parse_missing_patch() -> void:
	var p := VersionUtil.parse("3.1-BETA")
	eq(p["valid"], true, "valid")
	eq(p["patch"], 0, "patch defaults 0")
	eq(p["pre"], "beta", "pre lowercased")
	eq(VersionUtil.parse("4")["minor"], 0, "major-only minor 0")
	eq(VersionUtil.parse("4")["valid"], true, "major-only valid")


func test_parse_invalid() -> void:
	for s in ["", "v", "abc", "1.2.3.4", "1..2", "1.x", "3.1-", "-beta", "v.1", "1.2.-3"]:
		eq(VersionUtil.parse(s)["valid"], false, "invalid '%s'" % s)


func test_equal_cfg_and_tag() -> void:
	eq(VersionUtil.compare("3.1.0-beta", "v3.1-BETA"), 0, "plugin.cfg == tag")
	eq(VersionUtil.is_newer("v3.1-BETA", "3.1.0-beta"), false, "tag not newer than cfg")
	eq(VersionUtil.compare("v3.2.0", "3.2"), 0, "3.2.0 == 3.2")


func test_real_tag_ordering() -> void:
	var tags := ["v2.1-BETA", "v2.3-BETA", "v3.0-BETA", "v3.1-BETA", "v3.2.0-beta", "v3.2.0"]
	for i in tags.size():
		for j in tags.size():
			var expected := 0 if i == j else (1 if i > j else -1)
			eq(VersionUtil.compare(tags[i], tags[j]), expected, "compare(%s, %s)" % [tags[i], tags[j]])
	var shuffled := ["v3.2.0", "v2.3-BETA", "v3.1-BETA", "v2.1-BETA", "v3.2.0-beta", "v3.0-BETA"]
	shuffled.sort_custom(func(a, b): return VersionUtil.compare(a, b) < 0)
	eq(shuffled, tags, "sorted order")


func test_prerelease_vs_release() -> void:
	eq(VersionUtil.compare("3.2.0", "3.2.0-beta"), 1, "release > pre")
	eq(VersionUtil.compare("3.2.0-rc.1", "3.2.0"), -1, "pre < release")
	eq(VersionUtil.compare("3.2.0-beta", "3.1.9"), 1, "numbers win over pre")
	eq(VersionUtil.compare("3.2.0-beta.2", "3.2.0-beta.1"), 1, "beta.2 > beta.1")
	eq(VersionUtil.compare("3.2.0-beta.10", "3.2.0-beta.2"), 1, "beta.10 > beta.2 (numeric)")
	eq(VersionUtil.compare("3.2.0-beta", "3.2.0-beta.1"), -1, "beta < beta.1")
	eq(VersionUtil.compare("3.2.0-BETA", "3.2.0-beta"), 0, "case-insensitive")
	eq(VersionUtil.compare("3.2.0-alpha", "3.2.0-beta"), -1, "alpha < beta")
	eq(VersionUtil.compare("3.2.0-rc", "3.2.0-beta"), 1, "rc > beta")
	eq(VersionUtil.compare("10.0", "9.9"), 1, "numeric not lexical")


func test_invalid_sorts_lowest() -> void:
	eq(VersionUtil.compare("garbage", "0.0.1"), -1, "invalid < valid")
	eq(VersionUtil.compare("0.0.1-alpha", ""), 1, "valid > empty")
	eq(VersionUtil.compare("foo", "bar"), 0, "two invalid equal")
	eq(VersionUtil.is_newer("garbage", "3.1.0-beta"), false, "garbage not newer")
	eq(VersionUtil.is_newer("v3.2.0-beta", ""), true, "anything valid newer than unknown install")


func test_is_newer() -> void:
	eq(VersionUtil.is_newer("v3.2.0-beta", "3.1.0-beta"), true, "3.2.0-beta newer")
	eq(VersionUtil.is_newer("v3.2.0", "3.2.0-beta"), true, "release newer than its beta")
	eq(VersionUtil.is_newer("v3.0-BETA", "3.1.0-beta"), false, "older not newer")
	eq(VersionUtil.is_newer("3.1.0-beta", "3.1.0-beta"), false, "same not newer")


const FAKE_RELEASE := """{
  "url": "https://api.github.com/repos/Claudot-Code/claudot-pub/releases/1",
  "html_url": "https://github.com/Claudot-Code/claudot-pub/releases/tag/v3.2.0-beta",
  "id": 1,
  "tag_name": "v3.2.0-beta",
  "name": "Claudot v3.2.0-beta",
  "draft": false,
  "prerelease": true,
  "created_at": "2026-10-10T12:00:00Z",
  "published_at": "2026-10-10T12:05:00Z",
  "assets": [
    {"name": "claudot-v3.2.0-beta.zip.sha256", "size": 97, "content_type": "text/plain",
     "browser_download_url": "https://github.com/Claudot-Code/claudot-pub/releases/download/v3.2.0-beta/claudot-v3.2.0-beta.zip.sha256"},
    {"name": "claudot-v3.2.0-beta.zip", "size": 1234567, "content_type": "application/zip",
     "browser_download_url": "https://github.com/Claudot-Code/claudot-pub/releases/download/v3.2.0-beta/claudot-v3.2.0-beta.zip"}
  ],
  "body": "## What is new\\n- In-editor updates"
}"""


func test_parse_release_json() -> void:
	var r: Dictionary = UpdateChecker.parse_release_json(FAKE_RELEASE, "3.1.0-beta")
	eq(r["ok"], true, "ok")
	eq(r["error"], "", "no error")
	eq(r["is_newer"], true, "is_newer vs 3.1.0-beta")
	var info: Dictionary = r["info"]
	var keys := info.keys()
	keys.sort()
	eq(keys, ["asset_name", "asset_size", "asset_url", "body", "html_url", "prerelease", "published_at", "sha256_url", "tag", "version"], "exact key set")
	eq(info["tag"], "v3.2.0-beta", "tag")
	eq(info["version"], "3.2.0-beta", "version")
	eq(info["html_url"], "https://github.com/Claudot-Code/claudot-pub/releases/tag/v3.2.0-beta", "html_url")
	eq(info["body"], "## What is new\n- In-editor updates", "body")
	eq(info["published_at"], "2026-10-10T12:05:00Z", "published_at")
	eq(info["asset_name"], "claudot-v3.2.0-beta.zip", "asset_name")
	eq(info["asset_url"], "https://github.com/Claudot-Code/claudot-pub/releases/download/v3.2.0-beta/claudot-v3.2.0-beta.zip", "asset_url")
	eq(info["asset_size"], 1234567, "asset_size")
	eq(info["sha256_url"], "https://github.com/Claudot-Code/claudot-pub/releases/download/v3.2.0-beta/claudot-v3.2.0-beta.zip.sha256", "sha256_url")
	eq(info["prerelease"], true, "prerelease")
	var same: Dictionary = UpdateChecker.parse_release_json(FAKE_RELEASE, "3.2.0-beta")
	eq(same["is_newer"], false, "not newer when equal")


func test_parse_release_json_edge_cases() -> void:
	eq(UpdateChecker.parse_release_json("not json", "1.0")["error"], "invalid JSON", "garbage")
	eq(UpdateChecker.parse_release_json("[1,2]", "1.0")["error"], "invalid JSON", "array")
	eq(UpdateChecker.parse_release_json("{}", "1.0")["ok"], false, "missing tag")
	var r: Dictionary = UpdateChecker.parse_release_json("{\"tag_name\":\"v9.0\",\"body\":null,\"assets\":[]}", "1.0")
	eq(r["ok"], true, "minimal ok")
	eq(r["is_newer"], true, "9.0 newer than 1.0")
	eq(r["info"]["asset_url"], "", "no zip -> empty url")
	eq(r["info"]["asset_size"], 0, "no zip -> size 0")
	eq(r["info"]["sha256_url"], "", "no sha")
	eq(r["info"]["body"], "", "null body -> empty")
	eq(r["info"]["prerelease"], false, "prerelease default false")


func test_update_checker_headless() -> void:
	eq(UpdateChecker.get_installed_version(), "3.1.0-beta", "reads plugin.cfg")
	eq(UpdateChecker.is_auto_check_enabled(), true, "auto_check default headless")
	eq(UpdateChecker.get_skipped_tag(), "", "skipped default headless")
	UpdateChecker.skip_tag("v9.9")  # no-op headless, must not crash
	UpdateChecker.clear_skipped_tag()
	var c = UpdateChecker.new()
	eq(c.is_skipped("v9.9"), false, "is_skipped headless")
	eq(c.last_info, {}, "last_info empty")
	c.free()

"""
godot_docs.py — Godot 4 class reference fetcher, parser, cacher, and indexer.

Provides three public async functions for use by godot_mcp_server.py:
    search_docs(query, kind)   — search class/method/property/signal names
    get_class_docs(class_name, section) — full class docs with inheritance
    refresh_docs(version)      — force re-download from GitHub

Data sources (in priority order):
  1. Godot's built-in ClassDB via HTTP bridge (always matches running editor version)
  2. Local cache (~/.claudot/godot-docs-cache/)
  3. Official Godot XML class reference on GitHub (fallback)

Uses only httpx (already a project dependency) and stdlib xml.etree.ElementTree.
No additional pip packages required.
"""

import asyncio
import datetime
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache configuration
# ---------------------------------------------------------------------------

CACHE_ROOT = Path(os.path.expanduser("~")) / ".claudot" / "godot-docs-cache"
FALLBACK_VERSION = "4.4-stable"
CACHE_MAX_AGE_DAYS = 30

# Godot HTTP bridge (ClassDB access — primary source)
BRIDGE_URL = os.environ.get("GODOT_BRIDGE_URL", "http://127.0.0.1:7778")

# GitHub API endpoints (fallback source)
GITHUB_RELEASES_URL = "https://api.github.com/repos/godotengine/godot/releases"
GITHUB_TREE_URL = "https://api.github.com/repos/godotengine/godot/git/trees/{tag}?recursive=1"
GITHUB_RAW_XML_URL = "https://raw.githubusercontent.com/godotengine/godot/{tag}/doc/classes/{class_name}.xml"

# Module-level search index (lazy, built on first search call)
_index: dict | None = None
_index_version: str = ""

# Track whether ClassDB init has been attempted this session
_classdb_init_done: bool = False


# ---------------------------------------------------------------------------
# ClassDB bridge (primary data source — no network, exact version match)
# ---------------------------------------------------------------------------

async def _forward_to_bridge(tool_name: str, params: dict) -> dict | None:
    """
    Forward a tool call to the Godot HTTP bridge.

    Returns the parsed tool result dict, or None if the bridge is unreachable.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{BRIDGE_URL}/mcp/invoke",
                json={"tool_name": tool_name, "tool_args": params}
            )
            resp.raise_for_status()
            result = resp.json()

            if result.get("is_error", False):
                logger.debug(f"Bridge tool {tool_name} returned error: {result.get('tool_call_result')}")
                return None

            raw = result.get("tool_call_result", "{}")
            if isinstance(raw, str):
                return json.loads(raw)
            return raw

    except (httpx.ConnectError, httpx.TimeoutException):
        return None  # Bridge not running — silent fallback
    except Exception as e:
        logger.debug(f"Bridge call failed for {tool_name}: {e}")
        return None


async def fetch_class_list_from_classdb() -> list[str]:
    """
    Fetch the complete class list from the running Godot editor's ClassDB.

    Returns a sorted list of class names, or an empty list if the bridge
    is not available.
    """
    result = await _forward_to_bridge("get_classdb_class_list", {})
    if result and result.get("success"):
        classes = result.get("classes", [])
        logger.info(f"ClassDB returned {len(classes)} classes (Godot {result.get('godot_version', '?')})")
        return classes
    return []


async def fetch_class_from_classdb(class_name: str) -> dict | None:
    """
    Fetch structured docs for a single class from ClassDB via the HTTP bridge.

    Returns a parsed dict in the same format as parse_class_xml(), or None
    if the bridge is not available or the class is not found.
    """
    result = await _forward_to_bridge("get_classdb_class_docs", {"class_name": class_name})
    if result and result.get("success"):
        # Strip bridge-specific keys to match XML parser output format
        data = {k: v for k, v in result.items() if k not in ("success", "timestamp", "godot_version")}
        return data
    return None


async def init_from_classdb() -> bool:
    """
    Populate the doc cache metadata from Godot's built-in ClassDB.

    Called once on startup to ensure _godot_class_names has full coverage
    for auto-injection detection. No GitHub network calls needed.

    Returns True if ClassDB initialization succeeded, False otherwise.
    """
    global _classdb_init_done

    if _classdb_init_done:
        return bool(_get_cached_class_list(FALLBACK_VERSION))

    _classdb_init_done = True

    class_list = await fetch_class_list_from_classdb()
    if not class_list:
        logger.debug("ClassDB init: bridge not available, will fall back to GitHub cache")
        return False

    # Merge with any existing cached class list (preserves GitHub-sourced entries)
    existing = set(_get_cached_class_list(FALLBACK_VERSION))
    merged = sorted(set(class_list) | existing)

    _save_meta(FALLBACK_VERSION, merged)
    logger.info(f"ClassDB init: {len(merged)} classes in cache metadata "
                f"({len(class_list)} from ClassDB, {len(existing)} previously cached)")

    # Invalidate search index so it rebuilds with the full class list
    global _index, _index_version
    _index = None
    _index_version = ""

    return True


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------

async def get_latest_stable_tag(client: httpx.AsyncClient) -> str:
    """
    Fetch the latest stable Godot 4.x release tag from GitHub.

    Returns the tag string (e.g., "4.4-stable").
    Falls back to FALLBACK_VERSION if the API call fails or no 4.x-stable
    release is found (handles rate limits and network errors gracefully).
    """
    try:
        response = await client.get(
            GITHUB_RELEASES_URL,
            headers={"Accept": "application/vnd.github+json"},
            timeout=10.0
        )
        response.raise_for_status()
        releases = response.json()

        # Find latest stable 4.x tag
        for release in releases:
            tag = release.get("tag_name", "")
            if re.match(r"^4\.\d+-stable$", tag):
                logger.info(f"Latest stable Godot tag: {tag}")
                return tag

        logger.warning("No 4.x-stable release found, using fallback")
        return FALLBACK_VERSION

    except Exception as e:
        logger.warning(f"GitHub releases API failed: {e} — using fallback {FALLBACK_VERSION}")
        return FALLBACK_VERSION


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

async def fetch_class_xml(client: httpx.AsyncClient, tag: str, class_name: str) -> str:
    """
    Fetch raw XML for a single Godot class from the official GitHub source.

    Returns the XML string on success.
    Raises httpx.HTTPError on HTTP errors (caller should handle).
    """
    url = GITHUB_RAW_XML_URL.format(tag=tag, class_name=class_name)
    logger.debug(f"Fetching XML: {url}")
    response = await client.get(url, timeout=15.0)
    response.raise_for_status()
    return response.text


async def fetch_class_list(client: httpx.AsyncClient, tag: str) -> list[str]:
    """
    Fetch the list of all class names available in doc/classes/ for the given tag.

    Uses the GitHub tree API to list all .xml files in doc/classes/, then extracts
    class names by stripping the path prefix and .xml suffix.

    Returns a list of class name strings (e.g., ["Node", "Node2D", "Sprite2D", ...]).
    Returns an empty list on failure.
    """
    url = GITHUB_TREE_URL.format(tag=tag)
    logger.info(f"Fetching class list from GitHub tree API: {url}")

    try:
        response = await client.get(
            url,
            headers={"Accept": "application/vnd.github+json"},
            timeout=30.0
        )
        response.raise_for_status()
        data = response.json()

        class_names: list[str] = []
        for item in data.get("tree", []):
            path = item.get("path", "")
            if path.startswith("doc/classes/") and path.endswith(".xml"):
                # Extract class name: "doc/classes/Node2D.xml" -> "Node2D"
                filename = path[len("doc/classes/"):]
                class_name = filename[:-4]  # strip .xml
                class_names.append(class_name)

        logger.info(f"Found {len(class_names)} classes for tag {tag}")
        return sorted(class_names)

    except Exception as e:
        logger.error(f"Failed to fetch class list for {tag}: {e}")
        return []


# ---------------------------------------------------------------------------
# XML Parsing
# ---------------------------------------------------------------------------

def _strip_bbcode(text: str) -> str:
    """
    Strip Godot's BBCode-like markup tags from documentation text.

    Replaces common tags with readable plain text equivalents:
    - [code]foo[/code] -> `foo`
    - [b]foo[/b]       -> foo (bold stripped)
    - [i]foo[/i]       -> foo
    - [url=...]foo[/url] -> foo
    - [member ...]     -> property reference
    - [method ...]     -> method reference
    - [signal ...]     -> signal reference
    - [enum ...]       -> enum reference
    - [constant ...]   -> constant reference
    - [param ...]      -> parameter reference
    """
    if not text:
        return ""

    # [code]...[/code] -> `...`
    text = re.sub(r'\[code\](.*?)\[/code\]', r'`\1`', text, flags=re.DOTALL)

    # [codeblock]...[/codeblock] -> preserve content with newlines
    text = re.sub(r'\[codeblock[^\]]*\](.*?)\[/codeblock\]', r'\n\1\n', text, flags=re.DOTALL)

    # [b]...[/b] and [i]...[/i] — strip
    text = re.sub(r'\[b\](.*?)\[/b\]', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\[i\](.*?)\[/i\]', r'\1', text, flags=re.DOTALL)

    # [url=...]...[/url] — keep text only
    text = re.sub(r'\[url=[^\]]*\](.*?)\[/url\]', r'\1', text, flags=re.DOTALL)

    # [member ClassName.prop_name] -> ClassName.prop_name
    text = re.sub(r'\[member ([^\]]+)\]', r'\1', text)

    # [method ClassName.method_name] -> ClassName.method_name
    text = re.sub(r'\[method ([^\]]+)\]', r'\1', text)

    # [signal ClassName.signal_name] -> ClassName.signal_name
    text = re.sub(r'\[signal ([^\]]+)\]', r'\1', text)

    # [enum ClassName.EnumName] -> ClassName.EnumName
    text = re.sub(r'\[enum ([^\]]+)\]', r'\1', text)

    # [constant ClassName.CONST] -> ClassName.CONST
    text = re.sub(r'\[constant ([^\]]+)\]', r'\1', text)

    # [param name] -> name
    text = re.sub(r'\[param ([^\]]+)\]', r'\1', text)

    # [annotation name] -> name
    text = re.sub(r'\[annotation ([^\]]+)\]', r'\1', text)

    # [constructor ClassName] -> ClassName
    text = re.sub(r'\[constructor ([^\]]+)\]', r'\1', text)

    # [operator ...] -> operator
    text = re.sub(r'\[operator ([^\]]+)\]', r'\1', text)

    # Any remaining [tag] or [tag attr] style — strip the brackets
    text = re.sub(r'\[/?[a-z_]+(?: [^\]]*)?\]', '', text)

    # Normalize whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def parse_class_xml(xml_text: str) -> dict:
    """
    Parse a Godot class XML file into a structured dict.

    Expected XML structure:
      <class name="Node2D" inherits="CanvasItem" ...>
        <brief_description>...</brief_description>
        <description>...</description>
        <tutorials>...</tutorials>
        <methods>
          <method name="foo" qualifiers="const">
            <return type="void" />
            <param index="0" name="arg" type="float" default="0.0" />
            <description>...</description>
          </method>
        </methods>
        <members>
          <member name="position" type="Vector2" setter="set_position" getter="get_position" default="Vector2(0, 0)">
            Description text
          </member>
        </members>
        <signals>
          <signal name="changed">
            <param index="0" name="delta" type="float" />
            <description>...</description>
          </signal>
        </signals>
        <constants>
          <constant name="FLAG_MAX" value="3" enum="Flags">Description</constant>
        </constants>
      </class>

    Returns a dict with keys:
      name, inherits, brief_description, description,
      methods, members, signals, constants, enums
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return {"error": f"XML parse error: {e}"}

    result: dict[str, Any] = {
        "name": root.get("name", ""),
        "inherits": root.get("inherits", ""),
        "brief_description": "",
        "description": "",
        "methods": [],
        "members": [],
        "signals": [],
        "constants": [],
        "enums": {}
    }

    # Brief description
    brief_el = root.find("brief_description")
    if brief_el is not None and brief_el.text:
        result["brief_description"] = _strip_bbcode(brief_el.text)

    # Full description
    desc_el = root.find("description")
    if desc_el is not None and desc_el.text:
        result["description"] = _strip_bbcode(desc_el.text)

    # Methods
    methods_el = root.find("methods")
    if methods_el is not None:
        for method in methods_el.findall("method"):
            m: dict[str, Any] = {
                "name": method.get("name", ""),
                "qualifiers": method.get("qualifiers", ""),
                "return_type": "void",
                "params": [],
                "description": ""
            }

            ret_el = method.find("return")
            if ret_el is not None:
                m["return_type"] = ret_el.get("type", "void")

            for param in method.findall("param"):
                p = {
                    "name": param.get("name", ""),
                    "type": param.get("type", ""),
                    "default": param.get("default", "")
                }
                m["params"].append(p)

            desc_el = method.find("description")
            if desc_el is not None and desc_el.text:
                m["description"] = _strip_bbcode(desc_el.text)

            result["methods"].append(m)

    # Members (properties)
    members_el = root.find("members")
    if members_el is not None:
        for member in members_el.findall("member"):
            m = {
                "name": member.get("name", ""),
                "type": member.get("type", ""),
                "default": member.get("default", ""),
                "setter": member.get("setter", ""),
                "getter": member.get("getter", ""),
                "description": _strip_bbcode(member.text or "")
            }
            result["members"].append(m)

    # Signals
    signals_el = root.find("signals")
    if signals_el is not None:
        for signal in signals_el.findall("signal"):
            s: dict[str, Any] = {
                "name": signal.get("name", ""),
                "params": [],
                "description": ""
            }
            for param in signal.findall("param"):
                p = {
                    "name": param.get("name", ""),
                    "type": param.get("type", "")
                }
                s["params"].append(p)
            desc_el = signal.find("description")
            if desc_el is not None and desc_el.text:
                s["description"] = _strip_bbcode(desc_el.text)
            result["signals"].append(s)

    # Constants + Enums
    constants_el = root.find("constants")
    if constants_el is not None:
        for const in constants_el.findall("constant"):
            c = {
                "name": const.get("name", ""),
                "value": const.get("value", ""),
                "enum": const.get("enum", ""),
                "description": _strip_bbcode(const.text or "")
            }
            result["constants"].append(c)

            # Group into enums dict
            enum_name = c["enum"]
            if enum_name:
                if enum_name not in result["enums"]:
                    result["enums"][enum_name] = []
                result["enums"][enum_name].append(c)

    return result


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

def _cache_dir(version: str) -> Path:
    """Return the cache directory path for the given version."""
    return CACHE_ROOT / version


def _meta_path(version: str) -> Path:
    return _cache_dir(version) / "_meta.json"


def is_cache_valid(version: str) -> bool:
    """
    Return True if the cache exists and was created within the last 30 days.
    """
    meta = _meta_path(version)
    if not meta.exists():
        return False
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
        fetched_at = datetime.datetime.fromisoformat(data.get("fetched_at", "2000-01-01"))
        age = datetime.datetime.utcnow() - fetched_at
        return age.days < CACHE_MAX_AGE_DAYS
    except Exception:
        return False


def load_cached_class(version: str, class_name: str) -> dict | None:
    """Load a parsed class dict from cache. Returns None if not cached."""
    path = _cache_dir(version) / f"{class_name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_cached_class(version: str, class_name: str, data: dict) -> None:
    """Persist a parsed class dict to the cache directory."""
    cache_dir = _cache_dir(version)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{class_name}.json"
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to cache {class_name}: {e}")


def _load_meta(version: str) -> dict:
    """Load cache metadata. Returns empty dict if not found."""
    try:
        return json.loads(_meta_path(version).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_meta(version: str, class_list: list[str]) -> None:
    """Write/update cache metadata with current timestamp and class list."""
    meta = {
        "version": version,
        "fetched_at": datetime.datetime.utcnow().isoformat(),
        "class_list": class_list
    }
    _cache_dir(version).mkdir(parents=True, exist_ok=True)
    _meta_path(version).write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _get_cached_class_list(version: str) -> list[str]:
    """Return the class list from metadata, or empty list if not available."""
    return _load_meta(version).get("class_list", [])


# ---------------------------------------------------------------------------
# Search index
# ---------------------------------------------------------------------------

_index: dict | None = None
_index_version: str = ""


async def build_search_index(client: httpx.AsyncClient, version: str) -> dict:
    """
    Build the search index for the given Godot version.

    Strategy:
    1. Load class list from cache metadata (fast, no network).
    2. If metadata doesn't have class list, fetch from GitHub tree API.
    3. For each cached class JSON, index all methods, members, signals, constants.
    4. For uncached classes, index only the class name itself.

    Returns the index dict with key "entries" -> list of entry dicts.
    """
    global _index, _index_version

    class_list = _get_cached_class_list(version)

    if not class_list:
        logger.info(f"No cached class list for {version}, fetching from GitHub...")
        class_list = await fetch_class_list(client, version)
        if class_list:
            _save_meta(version, class_list)

    entries: list[dict] = []

    for class_name in class_list:
        cached = load_cached_class(version, class_name)
        if cached and "error" not in cached:
            # Index class itself
            entries.append({
                "name": class_name,
                "kind": "class",
                "class_name": class_name,
                "brief": cached.get("brief_description", "")[:120]
            })
            # Index methods
            for m in cached.get("methods", []):
                entries.append({
                    "name": m["name"],
                    "kind": "method",
                    "class_name": class_name,
                    "brief": m.get("description", "")[:80]
                })
            # Index members (properties)
            for mem in cached.get("members", []):
                entries.append({
                    "name": mem["name"],
                    "kind": "property",
                    "class_name": class_name,
                    "brief": f"{mem.get('type', '')} = {mem.get('default', '')}".strip(" =")
                })
            # Index signals
            for sig in cached.get("signals", []):
                entries.append({
                    "name": sig["name"],
                    "kind": "signal",
                    "class_name": class_name,
                    "brief": sig.get("description", "")[:80]
                })
            # Index constants
            for c in cached.get("constants", []):
                entries.append({
                    "name": c["name"],
                    "kind": "constant",
                    "class_name": class_name,
                    "brief": f"= {c.get('value', '')}"
                })
        else:
            # Not yet cached — index class name only
            entries.append({
                "name": class_name,
                "kind": "class",
                "class_name": class_name,
                "brief": "(not yet fetched)"
            })

    _index = {"entries": entries}
    _index_version = version
    logger.info(f"Search index built: {len(entries)} entries for {version}")
    return _index


def search_index(index: dict, query: str, kind: str = "") -> list[dict]:
    """
    Search the index for entries matching the query string.

    Matching strategy (ranked):
    1. Exact name match (case-insensitive)
    2. Prefix match on name
    3. Substring match on name
    4. Substring match on brief description

    Optionally filter by kind: "class", "method", "property", "signal", "constant".
    Returns top 10 results.
    """
    q = query.lower()
    entries = index.get("entries", [])

    if kind:
        entries = [e for e in entries if e.get("kind") == kind]

    exact: list[dict] = []
    prefix: list[dict] = []
    substring_name: list[dict] = []
    substring_brief: list[dict] = []

    for entry in entries:
        name_lower = entry["name"].lower()
        brief_lower = entry.get("brief", "").lower()

        if name_lower == q:
            exact.append(entry)
        elif name_lower.startswith(q):
            prefix.append(entry)
        elif q in name_lower:
            substring_name.append(entry)
        elif q in brief_lower:
            substring_brief.append(entry)

    results = exact + prefix + substring_name + substring_brief
    return results[:10]


# ---------------------------------------------------------------------------
# Inheritance chain helper
# ---------------------------------------------------------------------------

async def _get_inheritance_chain(
    client: httpx.AsyncClient,
    version: str,
    class_name: str
) -> list[str]:
    """
    Return the full inheritance chain for a class as a list from child to root.
    E.g., ["CharacterBody2D", "PhysicsBody2D", "CollisionObject2D", "Node2D", ...]
    """
    chain = [class_name]
    current = class_name
    visited = set()

    while current and current not in visited:
        visited.add(current)
        cached = load_cached_class(version, current)
        if cached is None:
            # Try ClassDB first, then GitHub
            cached = await fetch_class_from_classdb(current)
            if cached is not None:
                save_cached_class(version, current, cached)
            else:
                try:
                    xml_text = await fetch_class_xml(client, version, current)
                    cached = parse_class_xml(xml_text)
                    save_cached_class(version, current, cached)
                except Exception:
                    break

        parent = cached.get("inherits", "")
        if parent and parent not in visited:
            chain.append(parent)
            current = parent
        else:
            break

    return chain


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def search_docs(query: str, kind: str = "") -> list[dict]:
    """
    Search Godot 4 class reference for classes, methods, properties, signals,
    or constants matching the query.

    Args:
        query: Search term (class name, method, property, or keyword)
        kind:  Optional filter — "class", "method", "property", "signal", "constant"

    Returns:
        List of up to 10 result dicts with keys:
          name, kind, class_name, brief
        On error, returns list with a single error dict.
    """
    global _index, _index_version

    try:
        # Try ClassDB init if not done yet (populates class list without GitHub)
        if not _classdb_init_done:
            await init_from_classdb()

        async with httpx.AsyncClient() as client:
            # Determine version
            version = FALLBACK_VERSION
            if is_cache_valid(version):
                pass  # use existing version
            else:
                version = await get_latest_stable_tag(client)
                if not is_cache_valid(version):
                    # Need to initialise metadata (fetch class list)
                    class_list = await fetch_class_list(client, version)
                    if class_list:
                        _save_meta(version, class_list)

            # Build or reuse index
            if _index is None or _index_version != version:
                await build_search_index(client, version)

            if not _index:
                return [{"error": "Failed to build search index"}]

            results = search_index(_index, query, kind)

            # For results that are stub entries (not yet fetched), try to enrich
            # Try ClassDB first, then GitHub XML
            enriched: list[dict] = []
            for entry in results:
                if entry.get("brief") == "(not yet fetched)" and entry.get("kind") == "class":
                    try:
                        # Try ClassDB bridge first (fast, local)
                        data = await fetch_class_from_classdb(entry["class_name"])
                        if data is None:
                            # Fall back to GitHub XML
                            xml_text = await fetch_class_xml(client, version, entry["class_name"])
                            data = parse_class_xml(xml_text)
                        save_cached_class(version, entry["class_name"], data)
                        entry = {
                            "name": data["name"],
                            "kind": "class",
                            "class_name": data["name"],
                            "brief": data.get("brief_description", "")[:120]
                        }
                    except Exception as e:
                        logger.debug(f"Could not enrich {entry['class_name']}: {e}")
                enriched.append(entry)

            return enriched

    except Exception as e:
        logger.error(f"search_docs error: {e}")
        return [{"error": f"search_docs failed: {str(e)}"}]


async def get_class_docs(class_name: str, section: str = "all") -> dict:
    """
    Fetch full documentation for a Godot 4 class, including inheritance chain.

    Args:
        class_name: The Godot class name (e.g., "Node2D", "CharacterBody2D")
        section:    Which section to return — "all", "methods", "properties",
                    "signals", "constants", "description"

    Returns:
        Dict with class documentation. May include "error" key on failure.
        Includes "inheritance_chain" key: list of class names from child to root.
    """
    try:
        async with httpx.AsyncClient() as client:
            version = FALLBACK_VERSION
            if not is_cache_valid(version):
                version = await get_latest_stable_tag(client)

            # Fetch the requested class — try cache, then ClassDB, then GitHub
            cached = load_cached_class(version, class_name)
            if cached is None:
                # Try ClassDB bridge first (fast, local, exact version match)
                cached = await fetch_class_from_classdb(class_name)
                if cached is not None:
                    save_cached_class(version, class_name, cached)
                    logger.info(f"Fetched {class_name} from ClassDB")
                else:
                    # Fall back to GitHub XML
                    try:
                        xml_text = await fetch_class_xml(client, version, class_name)
                        cached = parse_class_xml(xml_text)
                        save_cached_class(version, class_name, cached)
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code == 404:
                            return {"error": f"Class '{class_name}' not found in Godot {version}"}
                        return {"error": f"Failed to fetch {class_name}.xml: HTTP {e.response.status_code}"}
                    except Exception as e:
                        return {"error": f"Failed to fetch {class_name}: {str(e)}"}

            if "error" in cached:
                return cached

            # Build inheritance chain
            chain = await _get_inheritance_chain(client, version, class_name)
            cached["inheritance_chain"] = chain
            cached["version"] = version

            # Filter by section if requested
            if section != "all":
                section_map = {
                    "methods": "methods",
                    "properties": "members",
                    "signals": "signals",
                    "constants": "constants",
                    "description": None
                }

                if section in section_map:
                    filtered: dict[str, Any] = {
                        "name": cached["name"],
                        "inherits": cached["inherits"],
                        "inheritance_chain": chain,
                        "version": version,
                        "brief_description": cached["brief_description"]
                    }
                    if section == "description":
                        filtered["description"] = cached.get("description", "")
                    else:
                        key = section_map[section]
                        filtered[key] = cached.get(key, [])
                    return filtered

            return cached

    except Exception as e:
        logger.error(f"get_class_docs error: {e}")
        return {"error": f"get_class_docs failed: {str(e)}"}


# ---------------------------------------------------------------------------
# Sync helpers for prompt injection (no network, cache-only)
# ---------------------------------------------------------------------------

def get_known_classes(version: str = FALLBACK_VERSION) -> set[str]:
    """
    Return the set of all Godot class names that are present in cache metadata.

    This is a synchronous, network-free operation — it only reads what is
    already stored in the local cache. Coverage grows organically as users
    invoke the godot_get_class_docs MCP tool.

    Returns an empty set on any error (metadata missing, corrupt, etc.).
    """
    try:
        class_list = _get_cached_class_list(version)
        return set(class_list)
    except Exception:
        return set()


GODOT4_GOTCHAS: dict[str, str] = {
    "move_and_slide": "In Godot 4, move_and_slide() takes no arguments — set velocity directly on the body's `velocity` property before calling it.",
    "connect": "In Godot 4, signal.connect(callable) replaces connect('signal', obj, 'method'). Use Callable or lambdas.",
    "yield": "yield() removed in Godot 4 — use `await` instead.",
    "onready": "Godot 4 uses @onready decorator, not `onready var`.",
    "export": "Godot 4 uses @export decorator, not `export var`.",
    "kinematicbody2d": "KinematicBody2D renamed to CharacterBody2D in Godot 4.",
    "kinematicbody": "KinematicBody renamed to CharacterBody3D in Godot 4.",
    "area": "Area renamed to Area3D in Godot 4. Use Area2D for 2D.",
    "is_on_floor": "is_on_floor() only works after move_and_slide() is called.",
    "move_and_collide": "move_and_collide() still takes a Vector motion arg in Godot 4 (unlike move_and_slide which changed).",
    "get_node": "Consider using @onready + $NodeName shorthand instead of get_node() calls.",
    "instance": "instance() renamed to instantiate() in Godot 4.",
    "set_process": "set_process()/set_physics_process() unchanged, but prefer _process/_physics_process override patterns.",
}


def get_gotcha_notes(text: str) -> list[str]:
    """
    Scan text for Godot 3-to-4 gotcha patterns and return a list of warning note strings.

    Matches are case-insensitive and use word-boundary checks to avoid false positives
    (e.g., 'export' won't match 'exporter').

    Returns an empty list on any error.
    """
    try:
        text_lower = text.lower()
        notes: list[str] = []
        for key, message in GODOT4_GOTCHAS.items():
            if re.search(r'\b' + re.escape(key) + r'\b', text_lower):
                notes.append(f"- {key}: {message}")
        return notes
    except Exception:
        return []


def resolve_method_to_class(method_name: str, version: str = FALLBACK_VERSION) -> str | None:
    """
    Search cached class JSON files to find which Godot class owns the given method.

    Synchronous and cache-only — no network calls. Scans only classes present in
    the local cache (typically 20-50 classes). Returns the first matching class name,
    or None if not found or on any error.
    """
    try:
        class_list = _get_cached_class_list(version)
        for class_name in class_list:
            cached = load_cached_class(version, class_name)
            if cached is None or "error" in cached:
                continue
            for m in cached.get("methods", []):
                if m.get("name") == method_name:
                    return class_name
        return None
    except Exception:
        return None


def format_concise_docs(class_data: dict) -> str:
    """
    Produce a concise, single-class API reference block from a parsed class dict.

    Format:
      ### ClassName (inherits: Parent)
      Brief description (first line only).

      **Properties:**
      - property_name: Type = default

      **Methods:**
      - method_name(param: Type, ...) -> ReturnType

      **Signals:**
      - signal_name(param: Type, ...)

    Omits constants, enums, and full descriptions to stay concise.
    Targets roughly 30–80 lines per class depending on class size.
    """
    lines: list[str] = []

    name = class_data.get("name", "")
    inherits = class_data.get("inherits", "")
    header = f"### {name}"
    if inherits:
        header += f" (inherits: {inherits})"
    lines.append(header)

    brief = class_data.get("brief_description", "").strip()
    if brief:
        # Only first line of the brief description
        first_line = brief.split("\n")[0].strip()
        if first_line:
            lines.append(first_line)

    # Properties
    members = class_data.get("members", [])
    if members:
        lines.append("")
        lines.append("**Properties:**")
        for m in members:
            prop_name = m.get("name", "")
            prop_type = m.get("type", "")
            prop_default = m.get("default", "")
            entry = f"- {prop_name}: {prop_type}"
            if prop_default:
                entry += f" = {prop_default}"
            lines.append(entry)

    # Methods
    methods = class_data.get("methods", [])
    if methods:
        lines.append("")
        lines.append("**Methods:**")
        for m in methods:
            method_name = m.get("name", "")
            params = m.get("params", [])
            return_type = m.get("return_type", "void")
            qualifiers = m.get("qualifiers", "")

            param_strs: list[str] = []
            for p in params:
                p_name = p.get("name", "")
                p_type = p.get("type", "")
                p_default = p.get("default", "")
                p_str = f"{p_name}: {p_type}"
                if p_default:
                    p_str += f" = {p_default}"
                param_strs.append(p_str)

            sig = f"- {method_name}({', '.join(param_strs)}) -> {return_type}"
            if qualifiers:
                sig += f" [{qualifiers}]"
            lines.append(sig)

    # Signals
    signals = class_data.get("signals", [])
    if signals:
        lines.append("")
        lines.append("**Signals:**")
        for s in signals:
            sig_name = s.get("name", "")
            params = s.get("params", [])
            param_strs = [f"{p.get('name', '')}: {p.get('type', '')}" for p in params]
            lines.append(f"- {sig_name}({', '.join(param_strs)})")

    return "\n".join(lines)


async def refresh_docs(version: str = "") -> dict:
    """
    Force refresh of cached Godot documentation.

    Tries ClassDB first (fast, local). Falls back to GitHub if ClassDB
    is not available. Clears cached class JSON files so they'll be
    re-fetched from the preferred source on next access.

    Args:
        version: Godot version tag (e.g., "4.4-stable"). Empty = latest stable.

    Returns:
        Status dict with keys: version, class_count, status
    """
    global _index, _index_version, _classdb_init_done

    try:
        # Clear module-level state
        _index = None
        _index_version = ""
        _classdb_init_done = False

        # Try ClassDB first
        classdb_list = await fetch_class_list_from_classdb()
        if classdb_list:
            version = version or FALLBACK_VERSION
            logger.info(f"Refreshing docs from ClassDB ({len(classdb_list)} classes)")
            _save_meta(version, classdb_list)

            # Remove cached class JSON files so they'll be re-fetched from ClassDB
            cache_dir = _cache_dir(version)
            removed = 0
            if cache_dir.exists():
                for json_file in cache_dir.glob("*.json"):
                    if json_file.name != "_meta.json":
                        try:
                            json_file.unlink()
                            removed += 1
                        except Exception:
                            pass

            return {
                "version": version,
                "class_count": len(classdb_list),
                "cached_files_cleared": removed,
                "source": "classdb",
                "status": "ok",
                "message": f"Refreshed docs from ClassDB: {len(classdb_list)} classes. "
                           f"Classes will be fetched from ClassDB on demand (no GitHub needed)."
            }

        # Fall back to GitHub
        async with httpx.AsyncClient() as client:
            if not version:
                version = await get_latest_stable_tag(client)

            logger.info(f"Refreshing docs cache from GitHub for {version}")

            # Fetch fresh class list
            class_list = await fetch_class_list(client, version)
            if not class_list:
                return {
                    "version": version,
                    "status": "error",
                    "message": "Failed to fetch class list from both ClassDB and GitHub"
                }

            # Overwrite metadata (this resets fetched_at to now)
            _save_meta(version, class_list)

            # Remove cached class JSON files so they'll be re-fetched
            cache_dir = _cache_dir(version)
            removed = 0
            if cache_dir.exists():
                for json_file in cache_dir.glob("*.json"):
                    if json_file.name != "_meta.json":
                        try:
                            json_file.unlink()
                            removed += 1
                        except Exception:
                            pass

            return {
                "version": version,
                "class_count": len(class_list),
                "cached_files_cleared": removed,
                "status": "ok",
                "message": f"Refreshed docs for Godot {version}: {len(class_list)} classes available. Classes will be fetched on demand."
            }

    except Exception as e:
        logger.error(f"refresh_docs error: {e}")
        return {
            "version": version or "unknown",
            "status": "error",
            "message": f"refresh_docs failed: {str(e)}"
        }

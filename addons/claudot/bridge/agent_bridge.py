#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "claude-agent-sdk>=0.1.0",
#   "anyio>=4.0.0",
#   "httpx>=0.27.0",
# ]
# ///

"""
Claudot Agent Bridge - Chat relay daemon for Godot plugin.

Provides persistent AI conversation sessions for the Godot chat interface.

Three chat backends, selected at runtime via the chat/configure message:
- "claude-code" (default): Claude Agent SDK → Claude Code CLI. Uses the CLI's
  OAuth login, or an Anthropic API key if one is provided. Full Claude Code
  capabilities (file edits, bash, MCP tools).
- "anthropic": Anthropic Messages API directly with a user-supplied API key.
  Godot scene tools work via the editor HTTP bridge; no file/bash tools.
- "openai" / "custom": any OpenAI-compatible /chat/completions endpoint with
  a user-supplied key (OpenAI, OpenRouter, Ollama, ...). Same tool surface
  as "anthropic".

Architecture:
- TCP server (default port 7777, per-project port in practice) for Godot
- Claude Agent SDK or direct-API providers (providers.py) for chat
- MCP tools for Claude Code are provided by godot_mcp_server.py (not here)
"""

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Optional


import anyio
from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    HookMatcher,
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock
)

import providers
from providers import (
    AnthropicAPIProvider,
    OpenAICompatProvider,
    ProviderError,
    context_window_for_model,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-4-8"

_BUILTIN_COMMANDS = {"/clear", "/compact", "/cost", "/help", "/memory",
                     "/model", "/permissions", "/plan", "/review", "/status", "/vim"}

_GDSCRIPT_GUIDE = """## GDScript 4.x — Required Patterns

Write Godot 4 GDScript only. Never use Godot 3 syntax.

### Always type variables and function signatures
```gdscript
var speed: float = 200.0
func move(delta: float) -> void:
    pass
```

### Use @export and @onready decorators
```gdscript
@export var speed: float = 200.0
@onready var sprite: Sprite2D = $Sprite2D
```

### Signals — prefer signal-first architecture
Signals decouple systems and make testing easy. Use them everywhere state changes.
```gdscript
signal health_changed(new_health: int)
signal player_died

# Emit:
health_changed.emit(health)

# Connect in code:
player.health_changed.connect(_on_health_changed)

# Lambda connect:
button.pressed.connect(func(): do_thing())
```
Default to signals over direct method calls between nodes. Child nodes emit; parents/managers listen.

### Async / await (not yield)
```gdscript
await get_tree().create_timer(1.0).timeout
await animation_player.animation_finished
```

### super() calls
```gdscript
func _ready() -> void:
    super()
```

### Typed arrays and dicts
```gdscript
var items: Array[String] = []
var scores: Dictionary = {}
```

### Critical Godot 3 → 4 syntax (always use the right side)
- `export var` / `onready var` → `@export var` / `@onready var`
- `yield(signal, "signal_name")` → `await signal_name`
- `connect("signal", obj, "method")` → `signal_name.connect(callable)`
"""

_SYSTEM_PROMPT = """You are an expert Godot 4 GDScript developer embedded in the Godot editor as an AI assistant.

## MCP Tool Use — Mandatory

Always call MCP tools proactively. Never wait for the user to ask.

- Before ANY scene work: call get_editor_context()
- Before reading/editing a node's script: call get_node_script(node_path)
- Before set_node_property or scene mutations: call get_scene_state() or get_node_property()
- To find .gd files: call search_files(extensions=[".gd"]) — never guess paths
- After writing or changing code: call run_tests(), then get_debugger_output() and get_debugger_errors()
- After visual scene changes: call capture_screenshot()

""" + _GDSCRIPT_GUIDE

_DIRECT_SYSTEM_PROMPT = """You are an expert Godot 4 GDScript developer embedded in the Godot editor as an AI assistant.

## Godot Tools — Use Proactively

You have tools that inspect and modify the live Godot editor. Call them proactively; never wait for the user to ask.

- Before ANY scene work: call get_editor_context()
- Before reading a node's script: call get_node_script(node_path)
- Before set_node_property or scene mutations: call get_scene_state() or get_node_property()
- To find files: call search_files(extensions=[".gd"]) — never guess paths
- Before writing GDScript involving a built-in class you are not 100% sure about: call godot_search_docs() or godot_get_class_docs()
- After running the game: check get_debugger_output() and get_debugger_errors()

Scene modifications (set_node_property, create_node, delete_node, reparent_node) are undoable with Ctrl+Z.

## Limits

You can NOT edit files on disk in this mode. To change a script, show the user the complete updated GDScript in a code block and tell them which file to paste it into. You CAN read scripts (get_node_script), modify scene nodes and properties, run the game, run tests, and read debugger output.

""" + _GDSCRIPT_GUIDE


def _redact_config(message: dict) -> dict:
    """Return a copy of a JSON-RPC message safe for logging (API key masked)."""
    try:
        params = message.get("params")
        if isinstance(params, dict) and params.get("api_key"):
            redacted = dict(message)
            redacted["params"] = dict(params)
            redacted["params"]["api_key"] = "***redacted***"
            return redacted
    except Exception:
        pass
    return message


class GodotTCPConnection:
    """Manages TCP connection to a single Godot editor instance."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.client_addr = writer.get_extra_info('peername')

    async def receive_message(self) -> Optional[dict]:
        """
        Receive JSON-RPC message from Godot.

        Returns:
            Parsed JSON dict or None if connection closed
        """
        try:
            # Read newline-delimited JSON
            line = await self.reader.readline()
            if not line:
                return None

            message = json.loads(line.decode('utf-8'))
            logger.debug(f"Received from Godot: {_redact_config(message)}")
            return message

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Error receiving message: {e}")
            return None

    async def send_message(self, data: dict) -> bool:
        """
        Send JSON-RPC message to Godot.

        Args:
            data: Dictionary to send as JSON

        Returns:
            True if successful, False otherwise
        """
        try:
            message_json = json.dumps(data) + "\n"
            self.writer.write(message_json.encode('utf-8'))
            await self.writer.drain()
            logger.debug(f"Sent to Godot: {data}")
            return True

        except Exception as e:
            logger.error(f"Error sending message: {e}")
            return False

    def close(self):
        """Close the TCP connection."""
        self.writer.close()


class AgentBridge:
    """
    Chat relay bridge daemon with selectable backends.

    Architecture:
    - Godot connects via TCP (one connection = one chat session)
    - Backend is chosen via chat/configure: Claude Agent SDK (default) or a
      direct-API provider from providers.py
    - Responses stream back to Godot in real-time
    - MCP tools for the Claude Code path are handled by godot_mcp_server.py;
      direct providers call the Godot HTTP bridge themselves (godot_tools.py)
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7777,
        model: str = DEFAULT_MODEL,
        log_level: str = "INFO"
    ):
        self.host = host
        self.port = port
        self.log_level = log_level

        self.tcp_connection: Optional[GodotTCPConnection] = None
        self._answer_queue: asyncio.Queue = asyncio.Queue()
        self._query_queue: asyncio.Queue = asyncio.Queue()
        self._permission_queue: asyncio.Queue = asyncio.Queue()
        self._session_allowed_tools: set[str] = set()
        self._detected_model: Optional[str] = None

        # Runtime configuration (overridden by chat/configure from Godot)
        self._config: dict = {
            "provider": "claude-code",
            "model": model,
            "api_key": "",
            "base_url": "",
        }
        self._context_window_tokens: int = context_window_for_model(model)

        # Active sessions (created lazily, torn down on reconfigure / disconnect)
        self._client: Optional[ClaudeSDKClient] = None
        self._sdk_stack: Optional[AsyncExitStack] = None
        self._direct_provider = None
        self._env_key_injected: bool = False

        # Setup logging
        self._setup_logging()

    def _setup_logging(self):
        """Configure logging to stderr."""
        numeric_level = getattr(logging, self.log_level.upper(), logging.INFO)

        logging.basicConfig(
            level=numeric_level,
            format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
            stream=sys.stderr
        )

        logger.info(f"Logging configured at {self.log_level.upper()} level")



    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """
        Handle a single Godot editor connection with persistent chat session.

        Args:
            reader: TCP stream reader
            writer: TCP stream writer
        """
        self.tcp_connection = GodotTCPConnection(reader, writer)
        client_addr = writer.get_extra_info('peername')
        logger.info(f"Godot editor connected from {client_addr}")

        try:
            await self._run_conversation_loop()
        except Exception as e:
            logger.error(f"Conversation error: {e}", exc_info=True)
        finally:
            await self._teardown_sessions()
            self.tcp_connection.close()
            self.tcp_connection = None
            logger.info(f"Godot editor disconnected from {client_addr}")

    async def _ask_user_hook(self, hook_input, tool_use_id, context):
        """
        PreToolUse hook for AskUserQuestion tool calls.

        Intercepts AskUserQuestion before it executes, sends the questions to Godot
        via TCP, then blocks until Godot sends back the user's answer. The tool must
        be denied (to prevent the real CLI-based AskUserQuestion from executing), but
        additionalContext carries the user's answer so Claude treats it as a successful
        interaction rather than a rejection.
        """
        questions = hook_input["tool_input"]["questions"]
        await self.tcp_connection.send_message({
            "jsonrpc": "2.0",
            "method": "chat/ask_user_question",
            "params": {"questions": questions}
        })
        # Block until Godot sends back the user's answer via chat/ask_user_answer
        answer = await self._answer_queue.get()
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Tool handled by Godot UI — answer provided via additionalContext.",
                "additionalContext": f"The user answered your question via the Godot chat interface. Their response: {answer}"
            }
        }

    async def _permission_hook(self, hook_input, tool_use_id, context):
        """
        PreToolUse hook for tools that require explicit user permission (e.g. WebFetch, WebSearch).

        Sends a permission request to Godot, blocks until the user allows or denies,
        then returns the appropriate permission decision to the SDK.
        """
        tool_name = hook_input.get("tool_name", "unknown")
        tool_input = hook_input.get("tool_input", {})
        summary = tool_input.get("url") or tool_input.get("query") or str(tool_input)[:120]
        if tool_name in self._session_allowed_tools:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow"
                }
            }
        await self.tcp_connection.send_message({
            "jsonrpc": "2.0",
            "method": "chat/permission_request",
            "params": {"tool_name": tool_name, "summary": summary}
        })
        decision = await self._permission_queue.get()
        if decision in ("allow", "allow_all"):
            if decision == "allow_all":
                self._session_allowed_tools.add(tool_name)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow"
                }
            }
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "User denied permission to use this tool."
            }
        }

    async def _handle_builtin_command(self, command: str) -> None:
        """Handle a built-in slash command locally without forwarding to the SDK."""
        if command == "/clear":
            # Reset backend conversation state so the model forgets too,
            # not just the UI transcript.
            await self._teardown_sessions(keep_env=True)
            await self.tcp_connection.send_message({
                "jsonrpc": "2.0",
                "method": "chat/clear",
                "params": {}
            })
        elif command == "/plan":
            self._plan_mode = not self._plan_mode
            state_msg = (
                "Plan mode ON — I will describe changes step-by-step without executing them. "
                "Type /plan again to exit."
            ) if self._plan_mode else "Plan mode OFF — resuming normal execution."
            await self.tcp_connection.send_message({
                "jsonrpc": "2.0",
                "method": "chat/assistant_text",
                "params": {"content": state_msg, "is_partial": False}
            })
        # Send a synthetic response to reset is_working state in Godot
        await self.tcp_connection.send_message({
            "jsonrpc": "2.0",
            "method": "chat/response",
            "params": {"content": "", "cost_usd": 0.0, "duration_ms": 0, "num_turns": 0}
        })

    async def _tcp_router(self):
        """Read ALL incoming TCP messages and route by method.

        Runs concurrently with _processor so that TCP messages are always
        being read — even while the model is processing a query. This prevents
        the deadlock where _answer_queue.get() would block forever because
        nobody was reading the TCP stream.
        """
        while True:
            msg = await self.tcp_connection.receive_message()
            if not msg:
                break
            method = msg.get("method", "")
            if method in ("chat/send", "chat/configure"):
                # Sequenced through the query queue so reconfiguration never
                # tears down a session mid-turn.
                await self._query_queue.put(msg)
            elif method == "chat/ask_user_answer":
                answer = msg.get("params", {}).get("answer", "")
                await self._answer_queue.put(answer)
            elif method == "chat/permission_response":
                decision = msg.get("params", {}).get("decision", "deny")
                await self._permission_queue.put(decision)
            elif method == "chat/cancel":
                logger.info("Interrupt requested by user")
                if self._client:
                    await self._client.interrupt()
                if self._direct_provider:
                    self._direct_provider.interrupt()
            else:
                logger.warning(f"Unknown method from Godot: {method}")

    # ------------------------------------------------------------------
    # Configuration & session lifecycle
    # ------------------------------------------------------------------

    async def _apply_config(self, params: dict) -> None:
        """Apply a chat/configure message: provider/model/key/base_url."""
        new_config = {
            "provider": params.get("provider", self._config["provider"]) or "claude-code",
            "model": params.get("model", self._config["model"]) or DEFAULT_MODEL,
            "api_key": params.get("api_key", ""),
            "base_url": params.get("base_url", ""),
        }
        if new_config == self._config and (self._client or self._direct_provider):
            # No change and a session is already live — nothing to do.
            return

        changed_session = (
            new_config["provider"] != self._config["provider"]
            or new_config["model"] != self._config["model"]
            or new_config["api_key"] != self._config["api_key"]
            or new_config["base_url"] != self._config["base_url"]
        )
        self._config = new_config
        self._context_window_tokens = context_window_for_model(new_config["model"])
        self._detected_model = None

        if changed_session:
            await self._teardown_sessions()

        logger.info(
            f"Configured: provider={new_config['provider']} model={new_config['model']} "
            f"key={'set' if new_config['api_key'] else 'none'} base_url={new_config['base_url'] or '-'}"
        )
        await self.tcp_connection.send_message({
            "jsonrpc": "2.0",
            "method": "chat/configured",
            "params": {"provider": new_config["provider"], "model": new_config["model"]}
        })

    async def _teardown_sessions(self, keep_env: bool = False) -> None:
        """Close any active backend session. Next message recreates it lazily."""
        if self._sdk_stack:
            try:
                await self._sdk_stack.aclose()
            except Exception as e:
                logger.warning(f"Error closing Claude SDK session: {e}")
            self._sdk_stack = None
            self._client = None
        self._direct_provider = None
        if not keep_env and self._env_key_injected:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            self._env_key_injected = False

    def _build_agent_options(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            model=self._config["model"],
            system_prompt=_SYSTEM_PROMPT,
            allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebFetch", "WebSearch"],
            permission_mode="acceptEdits",  # Auto-approve file edits
            include_partial_messages=True,  # Enable streaming
            setting_sources=["user", "project", "local"],  # Required for custom slash commands
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher="AskUserQuestion", hooks=[self._ask_user_hook]),
                    HookMatcher(matcher="WebFetch", hooks=[self._permission_hook]),
                    HookMatcher(matcher="WebSearch", hooks=[self._permission_hook]),
                ]
            }
        )

    async def _ensure_sdk_client(self) -> ClaudeSDKClient:
        """Create the Claude Agent SDK session if it isn't running yet."""
        if self._client:
            return self._client

        # If the user supplied an Anthropic API key for the Claude Code path,
        # expose it to the CLI subprocess via the environment. Without a key,
        # the CLI falls back to its own OAuth login (~/.claude).
        if self._config["api_key"]:
            os.environ["ANTHROPIC_API_KEY"] = self._config["api_key"]
            self._env_key_injected = True

        logger.info("Creating persistent Claude session...")
        self._sdk_stack = AsyncExitStack()
        self._client = await self._sdk_stack.enter_async_context(
            ClaudeSDKClient(options=self._build_agent_options())
        )
        logger.info("Claude session established")

        # Log server info for diagnostics (model/context info discovery)
        try:
            server_info = await self._client.get_server_info()
            if server_info:
                logger.info(f"Server info keys: {list(server_info.keys())}")
        except Exception:
            pass

        return self._client

    def _ensure_direct_provider(self):
        """Create the direct-API provider if it isn't running yet."""
        if self._direct_provider:
            return self._direct_provider

        provider = self._config["provider"]
        if provider == "anthropic":
            if not self._config["api_key"]:
                raise ProviderError(
                    "Anthropic API provider selected but no API key configured. "
                    "Open Claudot Settings and enter your key (console.anthropic.com)."
                )
            self._direct_provider = AnthropicAPIProvider(
                api_key=self._config["api_key"],
                model=self._config["model"],
                system_prompt=_DIRECT_SYSTEM_PROMPT,
                base_url=self._config["base_url"],
            )
        elif provider in ("openai", "custom"):
            if provider == "custom" and not self._config["base_url"]:
                raise ProviderError(
                    "Custom provider selected but no base URL configured. "
                    "Open Claudot Settings and enter the endpoint base URL "
                    "(e.g. http://localhost:11434/v1 for Ollama)."
                )
            if provider == "openai" and not self._config["api_key"]:
                raise ProviderError(
                    "OpenAI provider selected but no API key configured. "
                    "Open Claudot Settings and enter your key (platform.openai.com)."
                )
            self._direct_provider = OpenAICompatProvider(
                api_key=self._config["api_key"],
                model=self._config["model"],
                system_prompt=_DIRECT_SYSTEM_PROMPT,
                base_url=self._config["base_url"],
            )
        else:
            raise ProviderError(f"Unknown provider '{provider}'.")

        logger.info(f"Direct provider ready: {provider} / {self._config['model']}")
        return self._direct_provider

    # ------------------------------------------------------------------
    # Message processing
    # ------------------------------------------------------------------

    async def _processor(self):
        """Process chat/send and chat/configure messages sequentially.

        Runs concurrently with _tcp_router. Picks up messages from _query_queue
        (fed by _tcp_router) and processes them one at a time.
        """
        while True:
            msg = await self._query_queue.get()
            try:
                params = msg.get("params", {})

                if msg.get("method") == "chat/configure":
                    await self._apply_config(params)
                    continue

                content = params.get("content", "")
                context = params.get("context", {})

                prompt = self._build_prompt_with_context(content, context)
                logger.info(f"User message: {content[:100]}...")

                if content.strip() in _BUILTIN_COMMANDS:
                    await self._handle_builtin_command(content.strip())
                    continue

                if self._config["provider"] == "claude-code":
                    client = await self._ensure_sdk_client()
                    await client.query(prompt)
                    await self._stream_response_to_godot(client)
                else:
                    provider = self._ensure_direct_provider()
                    await self._stream_direct_turn(provider, prompt)

            except asyncio.CancelledError:
                break
            except ProviderError as e:
                logger.error(f"Provider error: {e}")
                await self.tcp_connection.send_message({
                    "jsonrpc": "2.0",
                    "method": "chat/error",
                    "params": {"error": str(e)}
                })
            except Exception as e:
                logger.error(f"Message handling error: {e}", exc_info=True)
                await self.tcp_connection.send_message({
                    "jsonrpc": "2.0",
                    "method": "chat/error",
                    "params": {"error": str(e)}
                })

    async def _run_conversation_loop(self):
        """Main conversation loop with persistent chat session."""

        # Reset queues and session state for this connection
        self._answer_queue = asyncio.Queue()
        self._query_queue = asyncio.Queue()
        self._permission_queue = asyncio.Queue()
        self._session_allowed_tools = set()
        self._detected_model = None
        self._plan_mode = False

        # Send initial system message. Backend sessions are created lazily on
        # the first chat message, after Godot has sent chat/configure.
        await self.tcp_connection.send_message({
            "jsonrpc": "2.0",
            "method": "chat/system",
            "params": {"message": f"Claudot bridge ready. Working in: {os.getcwd()}"}
        })

        # Run TCP router and processor concurrently.
        # The task group exits when either task finishes (TCP disconnect or error).
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._tcp_router)
            tg.start_soon(self._processor)

    def _build_prompt_with_context(self, content: str, context: dict) -> str:
        """
        Build prompt with Godot editor context and auto-injected class API docs.

        Args:
            content: User's message
            context: Editor context (scene path, selected nodes)

        Returns:
            Enhanced prompt with context and optional Godot API reference block
        """
        # Slash commands must pass through verbatim — no context wrapping or doc injection
        if content.lstrip().startswith("/"):
            return content

        if self._plan_mode:
            plan_instruction = (
                "PLAN MODE ACTIVE: Do not call any tools or make any file changes. "
                "Instead, describe step-by-step what you would do to accomplish the "
                "following task, including which files you would modify and what changes "
                "you would make. The user will review your plan before you execute anything.\n\n"
                "User request:\n"
            )
            content = plan_instruction + content

        # Build the prompt body (context + message)
        if not context:
            prompt = content
        else:
            context_parts = []

            if "scene_path" in context:
                context_parts.append(f"Current scene: {context['scene_path']}")

            if "scene_root_name" in context:
                context_parts.append(f"Scene root: {context['scene_root_name']} ({context.get('scene_root_type', 'Node')})")

            if "selected_nodes" in context and context["selected_nodes"]:
                context_parts.append("Selected nodes:")
                for node in context["selected_nodes"]:
                    node_info = f"  - {node['path']} ({node['type']})"
                    if "script" in node:
                        node_info += f" [script: {node['script']}]"
                    context_parts.append(node_info)

            if context_parts:
                context_str = "\n".join(context_parts)
                prompt = f"**Current Godot Editor Context:**\n{context_str}\n\n**User Message:**\n{content}"
            else:
                prompt = content

        return prompt

    async def _stream_direct_turn(self, provider, prompt: str):
        """
        Run one turn through a direct-API provider and forward events to Godot.

        Args:
            provider: AnthropicAPIProvider or OpenAICompatProvider
            prompt: Fully-built prompt (context already injected)
        """
        await self.tcp_connection.send_message({
            "jsonrpc": "2.0",
            "method": "chat/stream_start",
            "params": {"timestamp": time.time()}
        })

        sent_any_text = False
        async for event in provider.run_turn(prompt):
            etype = event.get("type")

            if etype == "text":
                sent_any_text = True
                await self.tcp_connection.send_message({
                    "jsonrpc": "2.0",
                    "method": "chat/assistant_text",
                    "params": {"content": event["text"], "is_partial": True}
                })

            elif etype == "tool_use":
                await self.tcp_connection.send_message({
                    "jsonrpc": "2.0",
                    "method": "chat/tool_use",
                    "params": {"tool_name": event["name"], "tool_input": event["input"]}
                })

            elif etype == "refusal":
                category = event.get("category")
                cat_str = f" (category: {category})" if category else ""
                await self.tcp_connection.send_message({
                    "jsonrpc": "2.0",
                    "method": "chat/refusal",
                    "params": {
                        "category": category,
                        "message": (
                            f"The model's safety classifiers declined this request{cat_str}. "
                            "Try rephrasing, or switch to Claude Opus 4.8 in Claudot Settings — "
                            "it handles security- and biology-adjacent topics that Claude Fable 5 declines."
                        )
                    }
                })

            elif etype == "result":
                usage = event.get("usage", {})
                await self.tcp_connection.send_message({
                    "jsonrpc": "2.0",
                    "method": "chat/response",
                    "params": {
                        # Text was already streamed via chat/assistant_text; sending
                        # content again would duplicate it in the UI when no
                        # intermediate text was shown — send it only as fallback.
                        "content": "" if sent_any_text else event.get("content", ""),
                        "cost_usd": event.get("cost_usd", 0.0),
                        "duration_ms": event.get("duration_ms", 0),
                        "num_turns": event.get("num_turns", 1),
                        "usage": usage
                    }
                })
                pct = usage.get("context_pct", 0.0)
                logger.info(
                    f"Direct turn complete ({event.get('num_turns', 1)} requests, "
                    f"${event.get('cost_usd', 0.0):.4f}, {event.get('duration_ms', 0)}ms, {pct}% ctx)"
                )

    async def _stream_response_to_godot(self, client: ClaudeSDKClient):
        """
        Stream Claude's response back to Godot in real-time (Agent SDK path).

        Args:
            client: Claude SDK client
        """
        # Send stream start marker
        await self.tcp_connection.send_message({
            "jsonrpc": "2.0",
            "method": "chat/stream_start",
            "params": {"timestamp": time.time()}
        })

        current_text = ""

        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                # Detect context window from first response's model name
                if not self._detected_model and message.model:
                    self._detected_model = message.model
                    self._context_window_tokens = context_window_for_model(message.model)
                    logger.info(f"Detected model: {message.model} → context window: {self._context_window_tokens:,} tokens")
                # Complete message available
                for block in message.content:
                    if isinstance(block, TextBlock):
                        current_text = block.text
                        # Send intermediate text to Godot for real-time display
                        await self.tcp_connection.send_message({
                            "jsonrpc": "2.0",
                            "method": "chat/assistant_text",
                            "params": {
                                "content": block.text,
                                "is_partial": True
                            }
                        })
                    elif isinstance(block, ToolUseBlock):
                        # Tool is being used
                        await self.tcp_connection.send_message({
                            "jsonrpc": "2.0",
                            "method": "chat/tool_use",
                            "params": {
                                "tool_name": block.name,
                                "tool_input": block.input
                            }
                        })

            elif isinstance(message, SystemMessage):
                # System events (can log these)
                logger.debug(f"System event: {message.subtype}")

            elif isinstance(message, ResultMessage):
                # Final result - conversation turn complete
                # Use current_text if available, fall back to message.result
                final_text = current_text if current_text else (message.result or "")
                usage_data = {}
                if message.usage:
                    input_t = message.usage.get("input_tokens", 0)
                    output_t = message.usage.get("output_tokens", 0)
                    total = input_t + output_t
                    usage_data = {
                        "input_tokens": input_t,
                        "output_tokens": output_t,
                        "total_tokens": total,
                        "context_pct": round((total / self._context_window_tokens) * 100, 1)
                    }
                await self.tcp_connection.send_message({
                    "jsonrpc": "2.0",
                    "method": "chat/response",
                    "params": {
                        "content": final_text,
                        "cost_usd": message.total_cost_usd,
                        "duration_ms": message.duration_ms,
                        "num_turns": message.num_turns,
                        "usage": usage_data
                    }
                })

                pct_str = f", {usage_data.get('context_pct', '?')}% ctx" if usage_data else ""
                logger.info(f"Response complete ({message.num_turns} turns, ${message.total_cost_usd:.4f}, {message.duration_ms}ms{pct_str})")
                break

    async def run(self):
        """Start the bridge daemon TCP server."""
        server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port
        )

        addr = server.sockets[0].getsockname()
        logger.info(f"Agent Bridge listening on {addr[0]}:{addr[1]}")
        logger.info(f"Default model: {self._config['model']}")
        logger.info(f"Ready for Godot connections...")

        async with server:
            await server.serve_forever()


async def main():
    """Entry point for the agent bridge daemon."""
    import argparse

    parser = argparse.ArgumentParser(description="Claudot Agent Bridge Daemon")
    parser.add_argument("--host", default="127.0.0.1", help="TCP server host")
    parser.add_argument("--port", type=int, default=7777, help="TCP server port")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Default Claude model (overridden by chat/configure)")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    parser.add_argument("--project-root", default="", help="Godot project root directory")

    args = parser.parse_args()

    # Set working directory to the Godot project root so claude CLI finds the right CLAUDE.md.
    if args.project_root:
        project_root = Path(args.project_root)
        if project_root.is_dir():
            os.chdir(project_root)
            logger.info(f"Working directory: {project_root}")
        else:
            logger.warning(f"--project-root '{args.project_root}' is not a directory; using inherited cwd: {os.getcwd()}")
    else:
        logger.warning(f"No --project-root provided; using inherited cwd: {os.getcwd()}")

    bridge = AgentBridge(
        host=args.host,
        port=args.port,
        model=args.model,
        log_level=args.log_level
    )

    try:
        await bridge.run()
    except KeyboardInterrupt:
        logger.info("Bridge shutting down...")


if __name__ == "__main__":
    asyncio.run(main())

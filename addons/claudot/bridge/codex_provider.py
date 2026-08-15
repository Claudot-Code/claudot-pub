"""
OpenAI Codex backend for the Claudot bridge — an `codex app-server` client.

Unlike the direct-API providers in providers.py (which run their own tool loop
against the Godot HTTP bridge), this backend delegates the entire agent harness
to the locally installed Codex CLI. We spawn `codex app-server` as a subprocess
and drive it over newline-delimited JSON-RPC 2.0 on stdin/stdout. Codex runs
with a workspace-write sandbox, so it can edit files on disk itself; the Godot
scene tools are exposed to it as an MCP server (godot_mcp_server.py, wired in via
`-c mcp_servers.*` config overrides on the spawn command line).

Wire-format notes (verified live against codex-cli 0.147.0 — see the block
comments below):
- The app server speaks a "JSON-RPC lite" variant. Server -> client messages
  OMIT the `"jsonrpc"` field entirely. We therefore parse tolerantly and never
  require it. Client -> server messages may include it or not; we omit it to
  match what the server itself emits.
- Responses:      {"id": N, "result": {...}}  or  {"id": N, "error": {...}}
- Notifications:  {"method": "...", "params": {...}, "emittedAtMs": ...}   (no id)
- Server requests (client must reply): {"id": N, "method": "...", "params": {...}}

Public surface used by agent_bridge.py:
    run_turn(prompt) -> AsyncIterator[dict]   provider-neutral event stream
    interrupt()                                (sync) cancel the running turn
    reset_history()                            drop the thread; next turn starts fresh
    close()  (coroutine)                       terminate the subprocess

Event contract (identical to providers.py so the bridge forwards it unchanged):
    {"type": "text", "text": str}
    {"type": "tool_use", "name": str, "input": dict}
    {"type": "result", "content", "cost_usd", "duration_ms", "num_turns", "usage"}

Failures surface as providers.ProviderError (imported for that symbol only — this
module must NOT import agent_bridge, to avoid a circular import).
"""

import asyncio
import json
import logging
import os
import sys
import time
from typing import AsyncIterator, Awaitable, Callable, Optional

from providers import ProviderError

logger = logging.getLogger(__name__)

# Some Windows/asyncio builds raise different types when killing an already-dead
# process; catch broadly but keep the intent readable.
ProcessLastError = (ProcessLookupError, OSError)

# Codex CLI wire constants
_SANDBOX_MODE = "workspace-write"          # SandboxMode enum (schemas: read-only|workspace-write|danger-full-access)
_APPROVAL_POLICY = "on-request"            # AskForApproval enum

# How long to wait for the handshake / a request response before giving up.
_REQUEST_TIMEOUT = 30.0
# Grace period for a clean subprocess shutdown before we kill it.
_CLOSE_TIMEOUT = 5.0
# Bounded stderr ring buffer, mirrors claude_subprocess.ClaudeSubprocess.
_STDERR_BUFFER_SIZE = 50

# Server-request methods we know how to answer. Anything else gets a decline.
_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "item/tool/requestUserInput",
}


def _toml_value(value) -> str:
    """TOML-encode a scalar / list / dict for a `-c key=value` config override.

    Codex parses each `-c` argv entry as `key=<toml-value>`. We only need the
    handful of shapes the MCP server config uses: strings, string arrays, and a
    flat string->string table for env.
    """
    if isinstance(value, str):
        # Basic TOML string: escape backslashes and quotes. Windows paths are
        # full of backslashes, so this matters.
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items()) + "}"
    raise TypeError(f"Cannot TOML-encode {type(value)!r}")


class CodexProvider:
    """
    Drives `codex app-server` for one Godot chat session.

    One provider instance owns one subprocess and (lazily) one thread. Each
    call to run_turn() issues a single `turn/start` and streams the resulting
    notifications back as provider-neutral events. reset_history() discards the
    thread so the next turn starts a fresh conversation.
    """

    def __init__(
        self,
        model: str,
        system_prompt: str,
        cwd: str,
        api_key: str = "",
        approval_callback: Optional[Callable[[str, str], Awaitable[str]]] = None,
        mcp_server_config: Optional[dict] = None,
    ):
        self.model = model
        self.system_prompt = system_prompt
        self.cwd = cwd
        self.api_key = api_key
        self.approval_callback = approval_callback
        self.mcp_server_config = mcp_server_config

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._stderr_buffer: list[str] = []

        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._thread_id: Optional[str] = None
        self._current_turn_id: Optional[str] = None

        # The active turn drains events from this queue. A sentinel None means
        # "the reader task or the process died" — run_turn wakes and raises.
        self._turn_queue: Optional[asyncio.Queue] = None
        # Per-turn: approval scopes the user granted "allow_all" for.
        self._auto_approved: set[str] = set()
        self._closed = False

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def reset_history(self) -> None:
        """Forget the current thread; the next run_turn() starts a fresh one."""
        self._thread_id = None

    def interrupt(self) -> None:
        """Cancel the running turn (sync — schedules the async send).

        Matches DirectChatProvider.interrupt()'s sync signature so agent_bridge's
        chat/cancel path can call it the same way.
        """
        turn_id = self._current_turn_id
        thread_id = self._thread_id
        if not (self._proc and thread_id and turn_id):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._send_interrupt(thread_id, turn_id))

    async def _send_interrupt(self, thread_id: str, turn_id: str) -> None:
        try:
            await self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        except Exception as e:
            logger.warning(f"Codex turn/interrupt failed ({e}); killing subprocess")
            proc = self._proc
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLastError:  # pragma: no cover
                    pass

    async def run_turn(self, prompt: str) -> AsyncIterator[dict]:
        """Run one user turn and yield provider-neutral events."""
        start = time.monotonic()
        await self._ensure_thread()

        self._auto_approved = set()
        self._turn_queue = asyncio.Queue()
        queue = self._turn_queue

        # turn/start returns immediately with the (in-progress) turn; the actual
        # work streams in as notifications we consume from _turn_queue below.
        resp = await self._request("turn/start", {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text": prompt}],
        })
        self._current_turn_id = (resp.get("turn") or {}).get("id")

        # Accumulated agentMessage delta text, keyed by item id, so item/completed
        # can be cross-checked. We emit on item/completed (matching how providers.py
        # yields whole completed text blocks rather than raw deltas).
        agent_text: dict[str, str] = {}
        last_text = ""
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "context_pct": 0.0}
        terminal_error: Optional[str] = None

        try:
            while True:
                item = await queue.get()
                if item is None:
                    # Reader task signalled process death / EOF.
                    raise ProviderError(self._death_message())

                kind = item[0]
                payload = item[1]

                if kind == "notification":
                    method = payload.get("method", "")
                    params = payload.get("params", {}) or {}

                    if method == "item/agentMessage/delta":
                        item_id = params.get("itemId", "")
                        agent_text[item_id] = agent_text.get(item_id, "") + params.get("delta", "")

                    elif method == "item/started":
                        for ev in self._events_for_item(params.get("item", {}), started=True):
                            yield ev

                    elif method == "item/completed":
                        thread_item = params.get("item", {})
                        if thread_item.get("type") == "agentMessage":
                            text = thread_item.get("text") or agent_text.get(thread_item.get("id", ""), "")
                            if text:
                                last_text = text
                                yield {"type": "text", "text": text}
                        else:
                            for ev in self._events_for_item(thread_item, started=False):
                                yield ev

                    elif method == "thread/tokenUsage/updated":
                        usage = self._usage_from_token_notification(params.get("tokenUsage", {})) or usage

                    elif method == "turn/completed":
                        # TurnStatus: completed | interrupted | failed | inProgress.
                        # A failed turn still arrives as turn/completed — surface
                        # its error instead of yielding an empty success result.
                        turn = params.get("turn", {})
                        u = self._usage_from_turn(turn)
                        if u:
                            usage = u
                        if turn.get("status") == "failed":
                            err = turn.get("error") or {}
                            terminal_error = (
                                self._auth_error_if_any(err)
                                or self._turn_error_message({"error": err})
                            )
                        break

                    elif method in ("turn/failed", "turn/aborted"):
                        terminal_error = self._turn_error_message(params)
                        break

                    elif method == "error":
                        # Auth failures surface here (httpStatusCode 401) with
                        # willRetry=true; codex would otherwise retry ~10x before
                        # giving up. Treat an auth error as immediately fatal so
                        # the user gets a clear message instead of a long hang.
                        auth_msg = self._auth_error_if_any(params.get("error", {}))
                        if auth_msg:
                            terminal_error = auth_msg
                            self.interrupt()
                            break
                        # Non-auth transient error that codex will retry — log and
                        # keep draining.
                        logger.debug(f"Codex error notification (will retry): {params.get('error')}")

                    else:
                        # thread/started, turn/started, thread/status/changed,
                        # remoteControl/status/changed, skills/changed, warning,
                        # deprecationNotice, item/reasoning/*, output deltas, ...
                        logger.debug(f"Codex notification ignored: {method}")

                elif kind == "server_request":
                    await self._handle_server_request(payload)
        finally:
            self._current_turn_id = None
            self._turn_queue = None

        if terminal_error:
            raise ProviderError(terminal_error)

        duration_ms = int((time.monotonic() - start) * 1000)
        yield {
            "type": "result",
            "content": last_text,
            # ChatGPT-subscription auth has no per-request USD price to report.
            "cost_usd": 0.0,
            "duration_ms": duration_ms,
            "num_turns": 1,
            "usage": usage,
        }

    async def close(self) -> None:
        """Terminate the subprocess cleanly: close stdin, wait, then kill."""
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        self._proc = None

        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass

        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=_CLOSE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("Codex app-server did not exit; killing")
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLastError:  # pragma: no cover
                    pass

        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        # Fail any still-pending request futures.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ProviderError("Codex app-server closed."))
        self._pending.clear()

    # ------------------------------------------------------------------
    # Item -> event mapping
    # ------------------------------------------------------------------

    def _events_for_item(self, item: dict, started: bool) -> list[dict]:
        """Map a ThreadItem to zero or more tool_use events.

        We surface command / file-change / MCP-tool items as tool_use on
        item/started (so the UI shows activity promptly). agentMessage text is
        handled by the caller on item/completed. Unknown item types are skipped.
        """
        itype = item.get("type")

        # userMessage items are the echo of our own prompt — never surface them.
        if itype in ("userMessage", "hookPrompt", "reasoning", "agentMessage",
                     "plan", "contextCompaction", "enteredReviewMode", "exitedReviewMode"):
            return []

        # Emit tool activity once, when the item starts.
        if not started:
            return []

        if itype == "commandExecution":
            return [{"type": "tool_use", "name": "bash",
                     "input": {"command": item.get("command", "")}}]

        if itype == "fileChange":
            files = []
            for change in item.get("changes", []) or []:
                path = change.get("path") or change.get("targetPath") or ""
                if path:
                    files.append(path)
            return [{"type": "tool_use", "name": "Edit files", "input": {"files": files}}]

        if itype == "mcpToolCall":
            server = item.get("server", "")
            tool = item.get("tool", "")
            name = f"{server}.{tool}" if server else tool
            args = item.get("arguments")
            return [{"type": "tool_use", "name": name or "mcp_tool",
                     "input": args if isinstance(args, dict) else {"arguments": args}}]

        if itype == "dynamicToolCall":
            args = item.get("arguments")
            return [{"type": "tool_use", "name": item.get("tool", "tool"),
                     "input": args if isinstance(args, dict) else {"arguments": args}}]

        if itype == "webSearch":
            return [{"type": "tool_use", "name": "web_search",
                     "input": {"query": item.get("query", "")}}]

        # imageGeneration, imageView, sleep, subAgentActivity, collabAgentToolCall,
        # and any future variants: skip gracefully.
        logger.debug(f"Codex item type not surfaced as tool_use: {itype}")
        return []

    def _usage_from_turn(self, turn: dict) -> Optional[dict]:
        """Extract usage from a turn/completed payload if it carries any.

        In v2 the Turn object does not itself carry token usage — usage arrives
        via thread/tokenUsage/updated notifications. This is a defensive reader
        in case a future/alternate shape includes it inline.
        """
        u = turn.get("usage") or turn.get("tokenUsage")
        if isinstance(u, dict):
            return self._usage_from_token_notification(u)
        return None

    def _usage_from_token_notification(self, token_usage: dict) -> Optional[dict]:
        """ThreadTokenUsage -> our usage dict. Prefer the cumulative `total`."""
        breakdown = token_usage.get("total") or token_usage.get("last") or {}
        if not isinstance(breakdown, dict):
            return None
        input_t = int(breakdown.get("inputTokens", 0) or 0)
        output_t = int(breakdown.get("outputTokens", 0) or 0)
        total_t = int(breakdown.get("totalTokens", 0) or 0) or (input_t + output_t)
        context_window = token_usage.get("modelContextWindow")
        context_pct = 0.0
        if context_window:
            context_pct = round(total_t / int(context_window) * 100, 1)
        return {
            "input_tokens": input_t,
            "output_tokens": output_t,
            "total_tokens": total_t,
            "context_pct": context_pct,
        }

    def _turn_error_message(self, params: dict) -> str:
        err = params.get("error") or params.get("turn", {}).get("error") or {}
        if isinstance(err, dict):
            msg = err.get("message") or err.get("additionalDetails")
        else:
            msg = str(err)
        return f"Codex turn failed: {msg or 'unknown error'}"

    def _auth_error_if_any(self, error: dict) -> Optional[str]:
        """Return a friendly ProviderError message if `error` is an auth failure."""
        if not isinstance(error, dict):
            return None
        blob = json.dumps(error).lower()
        codex_info = error.get("codexErrorInfo") or {}
        http_status = None
        if isinstance(codex_info, dict):
            disc = codex_info.get("responseStreamDisconnected") or {}
            if isinstance(disc, dict):
                http_status = disc.get("httpStatusCode")
        is_auth = (
            http_status in (401, 403)
            or "unauthorized" in blob
            or "authentication" in blob
            or "missing bearer" in blob
            or "not logged in" in blob
        )
        if is_auth:
            return (
                "Codex is not authenticated. Run `codex login` in a terminal to sign in "
                "with your ChatGPT account, or set an OpenAI API key in Claudot Settings."
            )
        return None

    # ------------------------------------------------------------------
    # Server-request handling (approvals)
    # ------------------------------------------------------------------

    async def _handle_server_request(self, msg: dict) -> None:
        req_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params", {}) or {}

        if method == "item/commandExecution/requestApproval":
            decision = await self._ask_approval(
                method, "Run command", params.get("command") or "(command)")
            await self._respond(req_id, {"decision": "accept" if decision else "decline"})

        elif method == "item/fileChange/requestApproval":
            files = params.get("grantRoot") or params.get("itemId") or "files"
            decision = await self._ask_approval(method, "Edit files", str(files))
            await self._respond(req_id, {"decision": "accept" if decision else "decline"})

        elif method == "item/permissions/requestApproval":
            # PermissionsRequestApprovalResponse has no accept/decline field —
            # the client answers with the permissions it grants. We never grant
            # elevated permissions (empty profile, turn scope), so codex proceeds
            # sandboxed either way; the ask is surfaced to the user for awareness.
            await self._ask_approval(
                method, "Grant permissions", params.get("reason") or "elevated permissions")
            await self._respond(req_id, {"permissions": {}, "scope": "turn"})

        elif method == "item/tool/requestUserInput":
            # EXPERIMENTAL: respond with empty answers per question id.
            questions = params.get("questions", []) or []
            answers = {}
            for q in questions:
                qid = q.get("id") if isinstance(q, dict) else None
                if qid:
                    answers[qid] = {"answers": []}
            logger.info("Codex requestUserInput answered with empty defaults")
            await self._respond(req_id, {"answers": answers})

        else:
            # Any unhandled server request (mcpServer/elicitation/request,
            # item/tool/call, attestation/generate, ...): decline/refuse rather
            # than leaving it hanging.
            logger.warning(f"Codex server request '{method}' unhandled; declining")
            await self._respond_error(req_id, -32601, f"Method '{method}' not handled by client")

    async def _ask_approval(self, scope: str, tool_name: str, summary: str) -> bool:
        """Ask the user (via approval_callback); returns True to accept."""
        if scope in self._auto_approved:
            return True
        if self.approval_callback is None:
            return False
        decision = await self.approval_callback(tool_name, summary)
        if decision == "allow_all":
            self._auto_approved.add(scope)
            return True
        return decision == "allow"

    # ------------------------------------------------------------------
    # Subprocess lifecycle & JSON-RPC transport
    # ------------------------------------------------------------------

    async def _ensure_thread(self) -> None:
        await self._ensure_process()
        if self._thread_id:
            return
        params = {
            "cwd": self.cwd,
            "model": self.model,
            "developerInstructions": self.system_prompt,
            "approvalPolicy": _APPROVAL_POLICY,
            "sandbox": _SANDBOX_MODE,
        }
        resp = await self._request("thread/start", params)
        thread = resp.get("thread") or {}
        self._thread_id = thread.get("id")
        if not self._thread_id:
            raise ProviderError("Codex thread/start returned no thread id.")
        logger.info(f"Codex thread started: {self._thread_id} (model={resp.get('model', self.model)})")

    async def _ensure_process(self) -> None:
        if self._proc and self._proc.returncode is None:
            return
        self._closed = False
        self._thread_id = None

        argv = self._build_argv()
        env = dict(os.environ)
        if self.api_key:
            # Codex reads an API key from CODEX_API_KEY / OPENAI_API_KEY.
            env["CODEX_API_KEY"] = self.api_key
            env.setdefault("OPENAI_API_KEY", self.api_key)

        logger.info(f"Spawning: {' '.join(argv)}")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.cwd if os.path.isdir(self.cwd) else None,
            )
        except FileNotFoundError:
            raise ProviderError(
                "Codex CLI not found. Install it with: npm install -g @openai/codex, "
                "then run codex login (or set an API key in Claudot Settings)."
            )
        except OSError as e:
            raise ProviderError(f"Could not start Codex CLI: {e}")

        self._pending = {}
        self._stderr_buffer = []
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

        try:
            await self._request("initialize", {
                "clientInfo": {"name": "claudot", "title": "Claudot", "version": "0.1.0"},
            })
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Codex initialize failed: {e}")
        # initialized is a notification (no response expected).
        await self._notify("initialized", {})

    def _build_argv(self) -> list[str]:
        """Resolve the `codex app-server` command line, with MCP `-c` overrides."""
        exe = self._resolve_codex_executable()
        argv = [exe, "app-server"]
        cfg = self.mcp_server_config
        if cfg:
            # Register the Godot scene tools as an MCP server named "claudot".
            overrides = {
                "mcp_servers.claudot.command": cfg.get("command", ""),
                "mcp_servers.claudot.args": cfg.get("args", []),
            }
            env = cfg.get("env")
            if env:
                overrides["mcp_servers.claudot.env"] = env
            for key, value in overrides.items():
                argv += ["-c", f"{key}={_toml_value(value)}"]
        return argv

    @staticmethod
    def _resolve_codex_executable() -> str:
        """Find the codex launcher. On Windows npm installs a `.cmd` shim."""
        import shutil
        for candidate in ("codex.cmd", "codex.exe", "codex") if os.name == "nt" else ("codex",):
            found = shutil.which(candidate)
            if found:
                return found
        # Fall back to the bare name so create_subprocess_exec raises
        # FileNotFoundError (mapped to the friendly install message).
        return "codex.cmd" if os.name == "nt" else "codex"

    def _death_message(self) -> str:
        tail = "\n".join(self._stderr_buffer[-10:]) if self._stderr_buffer else ""
        rc = self._proc.returncode if self._proc else "unknown"
        base = f"Codex app-server exited (code {rc})."
        return f"{base}\n{tail}" if tail else base

    async def _read_stdout(self) -> None:
        """Dispatch responses to pending futures, notifications/requests to the turn."""
        proc = self._proc
        assert proc and proc.stdout
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:  # EOF — process gone
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    logger.debug(f"Codex stdout non-JSON line skipped: {text[:200]}")
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Codex stdout reader error: {e}")
        finally:
            # Wake a blocked turn and fail any pending requests.
            if self._turn_queue is not None:
                self._turn_queue.put_nowait(None)
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ProviderError(self._death_message()))
            self._pending.clear()

    def _dispatch(self, msg: dict) -> None:
        # NB: "jsonrpc" is intentionally NOT required — the app server omits it.
        if "id" in msg and ("result" in msg or "error" in msg):
            # Response to one of our requests.
            fut = self._pending.pop(msg["id"], None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(self._rpc_error(msg["error"]))
                else:
                    fut.set_result(msg.get("result") or {})
            return
        if "id" in msg and "method" in msg:
            # Server -> client request; needs a reply.
            if self._turn_queue is not None:
                self._turn_queue.put_nowait(("server_request", msg))
            else:
                # No active turn to route through — decline immediately.
                asyncio.create_task(self._respond_error(
                    msg["id"], -32601, "No active turn"))
            return
        if "method" in msg:
            # Notification.
            if self._turn_queue is not None:
                self._turn_queue.put_nowait(("notification", msg))
            else:
                logger.debug(f"Codex notification outside a turn: {msg.get('method')}")
            return
        logger.debug(f"Codex message with unrecognised shape skipped: {msg}")

    def _rpc_error(self, error: dict) -> ProviderError:
        message = error.get("message", "") if isinstance(error, dict) else str(error)
        auth = self._auth_error_if_any(error if isinstance(error, dict) else {})
        if auth:
            return ProviderError(auth)
        if any(w in message.lower() for w in ("auth", "login", "unauthorized")):
            return ProviderError(
                "Codex authentication failed. Run `codex login` in a terminal, or set an "
                f"OpenAI API key in Claudot Settings. ({message})"
            )
        return ProviderError(f"Codex error: {message}")

    async def _read_stderr(self) -> None:
        proc = self._proc
        assert proc and proc.stderr
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", "replace").rstrip()
                if decoded:
                    self._stderr_buffer.append(decoded)
                    if len(self._stderr_buffer) > _STDERR_BUFFER_SIZE:
                        self._stderr_buffer.pop(0)
                    logger.debug(f"Codex stderr: {decoded}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Codex stderr reader error: {e}")

    async def _request(self, method: str, params: dict) -> dict:
        """Send a client -> server request and await its response."""
        proc = self._proc
        if not (proc and proc.stdin and proc.returncode is None):
            raise ProviderError(self._death_message())
        self._next_id += 1
        req_id = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._write({"id": req_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout=_REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise ProviderError(f"Codex request '{method}' timed out.")

    async def _notify(self, method: str, params: dict) -> None:
        await self._write({"method": method, "params": params})

    async def _respond(self, req_id, result: dict) -> None:
        await self._write({"id": req_id, "result": result})

    async def _respond_error(self, req_id, code: int, message: str) -> None:
        await self._write({"id": req_id, "error": {"code": code, "message": message}})

    async def _write(self, msg: dict) -> None:
        proc = self._proc
        if not (proc and proc.stdin and proc.returncode is None):
            raise ProviderError(self._death_message())
        try:
            proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            raise ProviderError(self._death_message())

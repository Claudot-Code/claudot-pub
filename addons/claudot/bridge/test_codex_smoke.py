#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "anyio>=4.0.0",
#   "httpx>=0.27.0",
# ]
# ///
"""
Smoke test for codex_provider.py.

Instead of the real `codex app-server`, we spawn a small scripted fake app-server
(FAKE_SERVER_SRC, written to a temp file) as a subprocess and point CodexProvider
at it by overriding _build_argv. The fake replays a canned JSON-RPC-lite session
that matches what the real codex-cli 0.147.0 emits on the wire — notably it OMITS
the "jsonrpc" field on every server->client message, so this also exercises the
tolerant parser.

Covered:
- happy path: initialize -> thread/start -> turn/start -> item/started +
  agentMessage deltas + item/completed + thread/tokenUsage/updated + turn/completed;
  assert the provider yields text + tool_use + result with usage.
- approval round-trip: server sends item/commandExecution/requestApproval; assert
  the client replies {"decision":"accept"} after the callback returns "allow".
- interrupt(): server waits; interrupt sends turn/interrupt and the fake ends the turn.
- CLI-not-found: a bogus executable maps to the friendly ProviderError.
- process death mid-turn: fake exits after turn/start; run_turn raises ProviderError.

Run:  python test_codex_smoke.py   (or: uv run test_codex_smoke.py)
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from codex_provider import CodexProvider  # noqa: E402
from providers import ProviderError  # noqa: E402


# --------------------------------------------------------------------------
# Fake app-server. Reads client requests, replies with jsonrpc-lite messages.
# Behaviour selected by argv[1] scenario name.
# --------------------------------------------------------------------------
FAKE_SERVER_SRC = r'''
import json, sys, time

SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "happy"
TID = "01a00741-0000-7000-8000-000000000001"
TURN = "01a00741-0000-7000-8000-0000000000aa"

def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def readmsg():
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)

while True:
    msg = readmsg()
    if msg is None:
        break
    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        # Server->client responses OMIT "jsonrpc" (matches real codex).
        out({"id": mid, "result": {"userAgent": "fake/0", "codexHome": "x",
                                    "platformFamily": "windows", "platformOs": "windows"}})
    elif method == "initialized":
        pass  # notification, no reply
    elif method == "thread/start":
        out({"id": mid, "result": {"thread": {"id": TID}, "model": "gpt-5.6-sol"}})
    elif method == "turn/start":
        out({"id": mid, "result": {"turn": {"id": TURN, "status": "inProgress"}}})

        if SCENARIO == "death":
            # Die mid-turn before emitting any completion.
            sys.stderr.write("fake app-server: simulated crash\n")
            sys.stderr.flush()
            sys.exit(3)

        # userMessage echo (must be ignored by the provider).
        out({"method": "item/started", "params": {"threadId": TID, "turnId": TURN,
             "startedAtMs": 1, "item": {"type": "userMessage", "id": "u1",
             "content": [{"type": "text", "text": "hi"}]}}})
        out({"method": "item/completed", "params": {"threadId": TID, "turnId": TURN,
             "completedAtMs": 1, "item": {"type": "userMessage", "id": "u1",
             "content": [{"type": "text", "text": "hi"}]}}})

        if SCENARIO == "approval":
            # Ask the client to approve a command; wait for the reply.
            out({"id": 9001, "method": "item/commandExecution/requestApproval",
                 "params": {"itemId": "c1", "threadId": TID, "turnId": TURN,
                            "startedAtMs": 2, "command": "ls -la"}})
            reply = readmsg()
            # Echo the decision back inside the agent message so the test can assert it.
            decision = (reply or {}).get("result", {}).get("decision", "?")
            out({"method": "item/started", "params": {"threadId": TID, "turnId": TURN,
                 "startedAtMs": 3, "item": {"type": "commandExecution", "id": "c1",
                 "command": "ls -la", "commandActions": [], "cwd": ".",
                 "status": "inProgress"}}})
            out({"method": "item/completed", "params": {"threadId": TID, "turnId": TURN,
                 "completedAtMs": 4, "item": {"type": "agentMessage", "id": "a1",
                 "text": "decision=" + decision}}})
            out({"method": "turn/completed", "params": {"threadId": TID,
                 "turn": {"id": TURN, "status": "completed", "items": []}}})
            continue

        if SCENARIO == "interrupt":
            # Emit a tool_use item (yielded promptly), then hang until the
            # client sends turn/interrupt.
            out({"method": "item/started", "params": {"threadId": TID, "turnId": TURN,
                 "startedAtMs": 2, "item": {"type": "commandExecution", "id": "c1",
                 "command": "sleep 999", "commandActions": [], "cwd": ".",
                 "status": "inProgress"}}})
            while True:
                m = readmsg()
                if m is None:
                    sys.exit(0)
                if m.get("method") == "turn/interrupt":
                    out({"id": m.get("id"), "result": {}})
                    out({"method": "turn/completed", "params": {"threadId": TID,
                         "turn": {"id": TURN, "status": "aborted", "items": []}}})
                    break
            continue

        # happy path: a tool call, streamed text, usage, completion.
        out({"method": "item/started", "params": {"threadId": TID, "turnId": TURN,
             "startedAtMs": 2, "item": {"type": "commandExecution", "id": "c1",
             "command": "echo hi", "commandActions": [], "cwd": ".",
             "status": "inProgress"}}})
        out({"method": "item/agentMessage/delta", "params": {"threadId": TID,
             "turnId": TURN, "itemId": "a1", "delta": "Hello"}})
        out({"method": "item/agentMessage/delta", "params": {"threadId": TID,
             "turnId": TURN, "itemId": "a1", "delta": " world."}})
        out({"method": "item/completed", "params": {"threadId": TID, "turnId": TURN,
             "completedAtMs": 5, "item": {"type": "agentMessage", "id": "a1",
             "text": "Hello world."}}})
        out({"method": "thread/tokenUsage/updated", "params": {"threadId": TID,
             "turnId": TURN, "tokenUsage": {"modelContextWindow": 1000,
             "total": {"cachedInputTokens": 0, "inputTokens": 120, "outputTokens": 30,
                       "reasoningOutputTokens": 0, "totalTokens": 150}}}})
        out({"method": "turn/completed", "params": {"threadId": TID,
             "turn": {"id": TURN, "status": "completed", "items": []}}})
    elif method == "turn/interrupt":
        out({"id": mid, "result": {}})
    else:
        if mid is not None:
            out({"id": mid, "error": {"code": -32601, "message": "unhandled"}})
'''


def _write_fake_server() -> str:
    fd, path = tempfile.mkstemp(suffix="_fake_codex.py", prefix="claudot_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(FAKE_SERVER_SRC)
    return path


def _make_provider(fake_path: str, scenario: str, approval_callback=None) -> CodexProvider:
    p = CodexProvider(
        model="gpt-5.6-sol",
        system_prompt="test system prompt",
        cwd=os.getcwd(),
        approval_callback=approval_callback,
        mcp_server_config=None,
    )
    # Point the provider at the fake app-server instead of the real codex CLI.
    p._build_argv = lambda: [sys.executable, fake_path, scenario]  # type: ignore
    return p


async def collect(provider, prompt):
    return [e async for e in provider.run_turn(prompt)]


def expect(cond, label):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    return bool(cond)


async def main() -> int:
    fake = _write_fake_server()
    ok = True
    try:
        # -------------------------------------------------- happy path
        print("== CodexProvider: happy path (text + tool_use + result) ==")
        p = _make_provider(fake, "happy")
        events = await collect(p, "say hello")
        types = [e["type"] for e in events]
        ok &= expect(types == ["tool_use", "text", "result"], f"event sequence {types}")
        ok &= expect(events[0]["name"] == "bash", "commandExecution surfaced as bash tool_use")
        ok &= expect(events[0]["input"].get("command") == "echo hi", "command line carried")
        ok &= expect(events[1]["text"] == "Hello world.", "agentMessage text emitted whole")
        result = events[-1]
        ok &= expect(result["cost_usd"] == 0.0, "cost 0 for subscription auth")
        ok &= expect(result["num_turns"] == 1, "num_turns == 1")
        ok &= expect(result["usage"]["input_tokens"] == 120, f"usage input {result['usage']}")
        ok &= expect(result["usage"]["output_tokens"] == 30, "usage output")
        ok &= expect(result["usage"]["total_tokens"] == 150, "usage total")
        ok &= expect(result["usage"]["context_pct"] > 0, "context pct computed from window")
        ok &= expect(result["content"] == "Hello world.", "result content is last text")
        await p.close()

        # -------------------------------------------------- approval round-trip
        print("== CodexProvider: approval round-trip (allow -> accept) ==")
        seen = {}

        async def cb(tool_name, summary):
            seen["tool"] = tool_name
            seen["summary"] = summary
            return "allow"

        p = _make_provider(fake, "approval", approval_callback=cb)
        events = await collect(p, "run a command")
        texts = [e["text"] for e in events if e["type"] == "text"]
        ok &= expect(seen.get("tool") == "Run command", f"callback got tool name {seen.get('tool')}")
        ok &= expect(seen.get("summary") == "ls -la", "callback got command summary")
        ok &= expect(any(t == "decision=accept" for t in texts),
                     f"server received accept decision (texts={texts})")
        await p.close()

        # -------------------------------------------------- interrupt
        print("== CodexProvider: interrupt ends the turn ==")
        p = _make_provider(fake, "interrupt")

        async def drive_interrupt():
            events = []
            async for e in p.run_turn("hang please"):
                events.append(e)
                if e["type"] == "tool_use":
                    p.interrupt()
            return events

        events = await asyncio.wait_for(drive_interrupt(), timeout=15)
        types = [e["type"] for e in events]
        # Interrupt aborts the turn; provider still yields a terminal result.
        ok &= expect("result" in types, f"interrupt still produced a result ({types})")
        await p.close()

        # -------------------------------------------------- CLI not found
        print("== CodexProvider: CLI-not-found error ==")
        p = _make_provider(fake, "happy")
        p._build_argv = lambda: ["definitely-not-a-real-codex-binary-xyz", "app-server"]  # type: ignore
        try:
            await collect(p, "hi")
            ok &= expect(False, "expected ProviderError for missing CLI")
        except ProviderError as e:
            ok &= expect("Codex CLI not found" in str(e), f"friendly not-found message ({e})")
        await p.close()

        # -------------------------------------------------- process death mid-turn
        print("== CodexProvider: process death mid-turn raises ProviderError ==")
        p = _make_provider(fake, "death")
        try:
            await asyncio.wait_for(collect(p, "hi"), timeout=15)
            ok &= expect(False, "expected ProviderError on mid-turn death")
        except ProviderError as e:
            ok &= expect("exited" in str(e).lower() or "closed" in str(e).lower(),
                         f"death surfaced as ProviderError ({str(e)[:80]})")
        await p.close()

        # -------------------------------------------------- TOML encoding of MCP config
        print("== CodexProvider: MCP config -> -c overrides (TOML) ==")
        p2 = CodexProvider(
            model="m", system_prompt="s", cwd=os.getcwd(),
            mcp_server_config={
                "command": r"C:\Python\python.exe",
                "args": [r"C:\proj\godot_mcp_server.py"],
                "env": {"GODOT_BRIDGE_URL": "http://127.0.0.1:7778"},
            },
        )
        argv = p2._build_argv()
        joined = " ".join(argv)
        ok &= expect("app-server" in argv, "app-server subcommand present")
        ok &= expect('mcp_servers.claudot.command="C:\\\\Python\\\\python.exe"' in joined,
                     f"command TOML-escaped (argv={argv})")
        ok &= expect('mcp_servers.claudot.args=["C:\\\\proj\\\\godot_mcp_server.py"]' in joined,
                     "args TOML array")
        ok &= expect('GODOT_BRIDGE_URL = "http://127.0.0.1:7778"' in joined, "env TOML table")

    finally:
        try:
            os.unlink(fake)
        except OSError:
            pass

    print("\nALL PASS" if ok else "\nFAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

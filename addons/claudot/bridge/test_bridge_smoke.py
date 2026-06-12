#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "claude-agent-sdk>=0.1.0",
#   "anyio>=4.0.0",
#   "httpx>=0.27.0",
# ]
# ///
"""
Integration smoke test for agent_bridge.py — boots the real bridge TCP server
in-process, connects like the Godot plugin would, and verifies the
configure → chat → streamed-events path using a fake Anthropic server.

Run:  uv run test_bridge_smoke.py
"""

import asyncio
import json
import os
import sys

BRIDGE_PORT = 17877
FAKE_ANTHROPIC_PORT = 17879

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_providers_smoke import fake_anthropic_handler, _read_http_request  # noqa: E402
import agent_bridge  # noqa: E402


async def send(writer, method, params):
    writer.write((json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n").encode())
    await writer.drain()


async def recv_until(reader, method, timeout=10.0):
    """Read newline-delimited JSON until a message with the given method arrives."""
    seen = []
    async with asyncio.timeout(timeout):
        while True:
            line = await reader.readline()
            if not line:
                raise RuntimeError(f"connection closed while waiting for {method}; saw {seen}")
            msg = json.loads(line)
            seen.append(msg.get("method"))
            if msg.get("method") == method:
                return msg, seen


def expect(cond, label):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


async def main() -> int:
    fake_api = await asyncio.start_server(fake_anthropic_handler, "127.0.0.1", FAKE_ANTHROPIC_PORT)
    bridge = agent_bridge.AgentBridge(host="127.0.0.1", port=BRIDGE_PORT, log_level="WARNING")
    bridge_task = asyncio.create_task(bridge.run())
    await asyncio.sleep(0.3)

    ok = True
    reader, writer = await asyncio.open_connection("127.0.0.1", BRIDGE_PORT)

    # 1. Greeting
    msg, _ = await recv_until(reader, "chat/system")
    ok &= expect("ready" in msg["params"]["message"].lower(), "bridge greeting received")

    # 2. Configure: direct Anthropic provider pointed at the fake server
    await send(writer, "chat/configure", {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "api_key": "sk-test",
        "base_url": f"http://127.0.0.1:{FAKE_ANTHROPIC_PORT}",
    })
    msg, _ = await recv_until(reader, "chat/configured")
    ok &= expect(msg["params"]["provider"] == "anthropic", "configure acknowledged")
    ok &= expect(msg["params"]["model"] == "claude-opus-4-8", "model acknowledged")

    # 3. Chat turn with editor context — should stream text, tool_use, then response
    await send(writer, "chat/send", {
        "content": "What scene am I in?",
        "context": {"scene_path": "res://main.tscn"},
    })
    msg, seen = await recv_until(reader, "chat/response")
    ok &= expect("chat/stream_start" in seen, "stream_start emitted")
    ok &= expect("chat/assistant_text" in seen, "assistant text streamed")
    ok &= expect("chat/tool_use" in seen, "tool use forwarded")
    ok &= expect(msg["params"]["usage"]["total_tokens"] > 0, "usage reported")
    ok &= expect(msg["params"]["content"] == "", "no duplicate content after streamed text")

    # 4. Refusal path (Fable 5)
    await send(writer, "chat/configure", {
        "provider": "anthropic",
        "model": "claude-fable-5",
        "api_key": "sk-test",
        "base_url": f"http://127.0.0.1:{FAKE_ANTHROPIC_PORT}",
    })
    await recv_until(reader, "chat/configured")
    await send(writer, "chat/send", {"content": "TRIGGER_REFUSAL please"})
    msg, seen = await recv_until(reader, "chat/response")
    ok &= expect("chat/refusal" in seen, "refusal message emitted before response")

    # 5. Missing-key error path clears working state via chat/error
    await send(writer, "chat/configure", {
        "provider": "anthropic", "model": "claude-opus-4-8", "api_key": "", "base_url": "",
    })
    await recv_until(reader, "chat/configured")
    await send(writer, "chat/send", {"content": "hello"})
    msg, _ = await recv_until(reader, "chat/error")
    ok &= expect("no API key" in msg["params"]["error"], "missing key produces friendly chat/error")

    # 6. /clear resets backend and echoes chat/clear + synthetic response
    await send(writer, "chat/send", {"content": "/clear"})
    msg, seen = await recv_until(reader, "chat/response")
    ok &= expect("chat/clear" in seen, "/clear handled")

    writer.close()
    bridge_task.cancel()
    fake_api.close()
    await fake_api.wait_closed()

    print("\nALL PASS" if ok else "\nFAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "anyio>=4.0.0",
#   "httpx>=0.27.0",
# ]
# ///
"""
Smoke test for providers.py — runs fake Anthropic / OpenAI-compatible / Godot
bridge servers on localhost and drives both direct providers through a full
tool-loop turn, plus a Claude Fable 5 refusal case.

Run:  uv run test_providers_smoke.py
"""

import asyncio
import json
import os
import sys

FAKE_GODOT_PORT = 17778
FAKE_ANTHROPIC_PORT = 17779
FAKE_OPENAI_PORT = 17780

os.environ["GODOT_BRIDGE_URL"] = f"http://127.0.0.1:{FAKE_GODOT_PORT}"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import providers  # noqa: E402
from providers import AnthropicAPIProvider, OpenAICompatProvider  # noqa: E402

CAPTURED_BODIES: list[dict] = []


async def _read_http_request(reader: asyncio.StreamReader) -> tuple[str, dict, bytes]:
    request_line = await reader.readline()
    path = request_line.decode().split(" ")[1] if b" " in request_line else "/"
    headers = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        key, _, value = line.decode().partition(":")
        headers[key.strip().lower()] = value.strip()
    body = b""
    length = int(headers.get("content-length", "0"))
    if length:
        body = await reader.readexactly(length)
    return path, headers, body


def _http_response(body: bytes, content_type: str = "application/json") -> bytes:
    return (
        f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body


def _sse_response(events: list[dict]) -> bytes:
    payload = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    body = payload.encode()
    return (
        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body


# ---------------------------------------------------------------- fake Godot
async def fake_godot_handler(reader, writer):
    path, _, body = await _read_http_request(reader)
    req = json.loads(body)
    result = {
        "is_error": False,
        "tool_call_result": json.dumps({
            "success": True,
            "tool": req["tool_name"],
            "scene_path": "res://main.tscn",
        }),
    }
    writer.write(_http_response(json.dumps(result).encode()))
    await writer.drain()
    writer.close()


# ------------------------------------------------------------ fake Anthropic
async def fake_anthropic_handler(reader, writer):
    _, _, body = await _read_http_request(reader)
    req = json.loads(body)
    CAPTURED_BODIES.append(req)

    last_msg = req["messages"][-1]
    is_tool_result_turn = (
        isinstance(last_msg.get("content"), list)
        and any(b.get("type") == "tool_result" for b in last_msg["content"])
    )
    wants_refusal = "TRIGGER_REFUSAL" in json.dumps(req["messages"])

    if wants_refusal:
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
            {"type": "message_delta",
             "delta": {"stop_reason": "refusal", "stop_details": {"category": "cyber"}},
             "usage": {"output_tokens": 0}},
        ]
    elif not is_tool_result_turn:
        events = [
            {"type": "message_start", "message": {"usage": {
                "input_tokens": 100, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 500}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig123"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Checking the scene"}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": " now."}},
            {"type": "content_block_stop", "index": 1},
            {"type": "content_block_start", "index": 2,
             "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get_editor_context"}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
            {"type": "content_block_stop", "index": 2},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 40}},
        ]
    else:
        events = [
            {"type": "message_start", "message": {"usage": {
                "input_tokens": 50, "cache_read_input_tokens": 600, "cache_creation_input_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "The scene is main.tscn."}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 12}},
        ]

    writer.write(_sse_response(events))
    await writer.drain()
    writer.close()


# --------------------------------------------------------------- fake OpenAI
_openai_call_count = 0


async def fake_openai_handler(reader, writer):
    global _openai_call_count
    _, _, body = await _read_http_request(reader)
    req = json.loads(body)
    CAPTURED_BODIES.append(req)
    has_tool_msg = any(m.get("role") == "tool" for m in req["messages"])

    if not has_tool_msg:
        events = [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "get_editor_context", "arguments": ""}}]}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": "{}"}}]}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 80, "completion_tokens": 20}},
        ]
    else:
        events = [
            {"choices": [{"index": 0, "delta": {"content": "All "}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": "good."}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 5}},
        ]
    writer.write(_sse_response(events))
    await writer.drain()
    writer.close()


async def collect(provider, prompt):
    return [e async for e in provider.run_turn(prompt)]


def expect(cond, label):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    return cond


async def main() -> int:
    servers = [
        await asyncio.start_server(fake_godot_handler, "127.0.0.1", FAKE_GODOT_PORT),
        await asyncio.start_server(fake_anthropic_handler, "127.0.0.1", FAKE_ANTHROPIC_PORT),
        await asyncio.start_server(fake_openai_handler, "127.0.0.1", FAKE_OPENAI_PORT),
    ]
    ok = True

    print("== AnthropicAPIProvider: tool loop (claude-opus-4-8) ==")
    CAPTURED_BODIES.clear()
    p = AnthropicAPIProvider("sk-test", "claude-opus-4-8", "system prompt here",
                             base_url=f"http://127.0.0.1:{FAKE_ANTHROPIC_PORT}")
    events = await collect(p, "What scene am I in?")
    types = [e["type"] for e in events]
    ok &= expect(types == ["text", "tool_use", "text", "result"], f"event sequence {types}")
    ok &= expect(events[0]["text"] == "Checking the scene now.", "streamed text assembled")
    ok &= expect(events[1]["name"] == "get_editor_context", "tool call surfaced")
    result = events[-1]
    ok &= expect(result["content"] == "The scene is main.tscn.", "final text")
    ok &= expect(result["num_turns"] == 2, "two API requests")
    ok &= expect(result["usage"]["output_tokens"] == 52, f"output tokens {result['usage']}")
    ok &= expect(result["usage"]["context_pct"] > 0, "context pct computed")
    ok &= expect(result["cost_usd"] > 0, "cost computed")
    ok &= expect(CAPTURED_BODIES[0].get("thinking") == {"type": "adaptive"}, "opus-4-8 sends adaptive thinking")
    ok &= expect("temperature" not in CAPTURED_BODIES[0], "no temperature param")
    # History replay invariants
    second_body = CAPTURED_BODIES[1]
    assistant_turn = second_body["messages"][1]
    blk_types = [b["type"] for b in assistant_turn["content"]]
    ok &= expect(blk_types == ["thinking", "text", "tool_use"], f"assistant blocks replayed verbatim {blk_types}")
    ok &= expect(assistant_turn["content"][0].get("signature") == "sig123", "thinking signature preserved")
    tool_result_turn = second_body["messages"][2]
    ok &= expect(tool_result_turn["content"][0]["tool_use_id"] == "toolu_1", "tool_result paired to tool_use")
    ok &= expect("res://main.tscn" in tool_result_turn["content"][0]["content"], "Godot bridge result delivered")

    print("== AnthropicAPIProvider: Fable 5 request shaping + refusal ==")
    CAPTURED_BODIES.clear()
    p = AnthropicAPIProvider("sk-test", "claude-fable-5", "system prompt here",
                             base_url=f"http://127.0.0.1:{FAKE_ANTHROPIC_PORT}")
    events = await collect(p, "TRIGGER_REFUSAL please")
    types = [e["type"] for e in events]
    ok &= expect("thinking" not in CAPTURED_BODIES[0], "fable-5 omits thinking param")
    ok &= expect(CAPTURED_BODIES[0]["model"] == "claude-fable-5", "fable-5 model id")
    ok &= expect(types == ["refusal", "result"], f"refusal surfaced {types}")
    ok &= expect(events[0]["category"] == "cyber", "refusal category")

    print("== OpenAICompatProvider: tool loop ==")
    CAPTURED_BODIES.clear()
    p = OpenAICompatProvider("sk-test", "gpt-5.1", "system prompt here",
                             base_url=f"http://127.0.0.1:{FAKE_OPENAI_PORT}")
    events = await collect(p, "What scene am I in?")
    types = [e["type"] for e in events]
    ok &= expect(types == ["tool_use", "text", "result"], f"event sequence {types}")
    ok &= expect(events[0]["name"] == "get_editor_context", "tool call surfaced")
    ok &= expect(events[1]["text"] == "All good.", "streamed text assembled")
    result = events[-1]
    ok &= expect(result["usage"]["total_tokens"] == 225, f"usage accumulated {result['usage']}")
    second_body = CAPTURED_BODIES[1]
    roles = [m["role"] for m in second_body["messages"]]
    ok &= expect(roles == ["system", "user", "assistant", "tool"], f"openai history shape {roles}")
    ok &= expect(second_body["messages"][3]["tool_call_id"] == "call_1", "tool_call_id preserved")

    for s in servers:
        s.close()
        await s.wait_closed()

    print("\nALL PASS" if ok else "\nFAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

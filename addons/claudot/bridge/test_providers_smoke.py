#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "anyio>=4.0.0",
#   "httpx>=0.27.0",
# ]
# ///
"""
Smoke test for providers.py — runs fake Anthropic / OpenAI-compatible /
OpenRouter / Godot bridge servers on localhost and drives the direct providers
through a full tool-loop turn, plus a Claude Fable 5 refusal case.

Run:  uv run test_providers_smoke.py
"""

import asyncio
import json
import os
import sys

FAKE_GODOT_PORT = 17778
FAKE_ANTHROPIC_PORT = 17779
FAKE_OPENAI_PORT = 17780
FAKE_OPENROUTER_PORT = 17781

os.environ["GODOT_BRIDGE_URL"] = f"http://127.0.0.1:{FAKE_GODOT_PORT}"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import providers  # noqa: E402
from providers import AnthropicAPIProvider, OpenAICompatProvider, OpenRouterProvider  # noqa: E402

CAPTURED_BODIES: list[dict] = []
CAPTURED_HEADERS: list[dict] = []


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


# ----------------------------------------------------------- fake OpenRouter
async def fake_openrouter_handler(reader, writer):
    _, headers, body = await _read_http_request(reader)
    req = json.loads(body)
    CAPTURED_BODIES.append(req)
    CAPTURED_HEADERS.append(headers)
    has_tool_msg = any(m.get("role") == "tool" for m in req["messages"])

    if not has_tool_msg:
        events = [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "get_editor_context", "arguments": ""}}]}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": "{}"}}]}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 80, "completion_tokens": 20, "cost": 0.0021}},
        ]
    else:
        events = [
            {"choices": [{"index": 0, "delta": {"content": "All "}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": "good."}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 5, "cost": 0.0009}},
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
        await asyncio.start_server(fake_openrouter_handler, "127.0.0.1", FAKE_OPENROUTER_PORT),
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

    print("== AnthropicAPIProvider: 5.5 request shaping (effort / thinking) ==")
    CAPTURED_BODIES.clear()
    p = AnthropicAPIProvider("sk-test", "claude-opus-5-5", "system prompt here",
                             base_url=f"http://127.0.0.1:{FAKE_ANTHROPIC_PORT}")
    events = await collect(p, "What scene am I in?")
    ok &= expect(events[-1]["type"] == "result", "opus-5-5 turn completes")
    ok &= expect(CAPTURED_BODIES[0].get("output_config") == {"effort": "high"}, "opus-5-5 pins effort high")
    ok &= expect(CAPTURED_BODIES[0].get("thinking") == {"type": "adaptive"}, "opus-5-5 sends adaptive thinking")
    CAPTURED_BODIES.clear()
    p = AnthropicAPIProvider("sk-test", "claude-sonnet-5-5", "system prompt here",
                             base_url=f"http://127.0.0.1:{FAKE_ANTHROPIC_PORT}")
    events = await collect(p, "What scene am I in?")
    ok &= expect("output_config" not in CAPTURED_BODIES[0], "sonnet-5-5 sends no output_config")
    ok &= expect(CAPTURED_BODIES[0].get("thinking") == {"type": "adaptive"}, "sonnet-5-5 sends adaptive thinking")

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

    print("== OpenRouterProvider: tool loop + attribution + cost ==")
    CAPTURED_BODIES.clear()
    CAPTURED_HEADERS.clear()
    p = OpenRouterProvider("sk-or-test", "deepseek/deepseek-v4-flash", "system prompt here",
                           base_url=f"http://127.0.0.1:{FAKE_OPENROUTER_PORT}")
    events = await collect(p, "What scene am I in?")
    types = [e["type"] for e in events]
    ok &= expect(types == ["tool_use", "text", "result"], f"event sequence {types}")
    ok &= expect(events[1]["text"] == "All good.", "streamed text assembled")
    headers = CAPTURED_HEADERS[0]
    ok &= expect(headers.get("authorization") == "Bearer sk-or-test", "bearer auth header")
    ok &= expect(headers.get("x-title") == "Claudot", "X-Title attribution header")
    ok &= expect("http-referer" in headers, "HTTP-Referer attribution header")
    ok &= expect(CAPTURED_BODIES[0].get("usage") == {"include": True}, "usage accounting requested")
    result = events[-1]
    ok &= expect(abs(result["cost_usd"] - 0.003) < 1e-9, f"cost accumulated from usage chunks ({result['cost_usd']})")
    ok &= expect(result["usage"]["total_tokens"] == 225, f"usage accumulated {result['usage']}")
    ok &= expect(OpenRouterProvider("k", "m", "s").base_url == "https://openrouter.ai/api/v1",
                 "default base URL is openrouter.ai")

    print("== Claude model registry ==")
    fable51 = providers.claude_model_info("claude-fable-5-1")
    ok &= expect(fable51["in"] == 10.0 and fable51["out"] == 50.0, "fable-5-1 in/out pricing")
    ok &= expect(fable51["context"] == 1_000_000, "fable-5-1 context 1M")
    ok &= expect(fable51["thinking"] == "omit", "fable-5-1 omits thinking param")
    ok &= expect(fable51.get("cache_read") == 0.25, "fable-5-1 explicit cache_read rate")
    mythos51 = providers.claude_model_info("claude-mythos-5-1")
    ok &= expect(mythos51.get("cache_read") == 0.25, "mythos-5-1 explicit cache_read rate")
    sonnet5 = providers.claude_model_info("claude-sonnet-5")
    ok &= expect(sonnet5["in"] == 2.0 and sonnet5["out"] == 10.0, "sonnet-5 repriced to 2.0/10.0")
    # Dated/suffixed 5.1 id must resolve to the 5.1 entry (2.5% cache read),
    # not the generic claude-fable fallback.
    dated = providers.claude_model_info("claude-fable-5-1-20260901")
    ok &= expect(dated.get("cache_read") == 0.25, "dated fable-5-1 keeps 2.5% cache-read via prefix order")
    # cache-read rate: explicit for 5.1, computed 10% default otherwise.
    ok &= expect(providers._cache_read_rate(fable51) == 0.25, "cache_read_rate uses explicit 5.1 rate")
    ok &= expect(providers._cache_read_rate(providers.claude_model_info("claude-fable-5")) == 1.0,
                 "cache_read_rate defaults to 10% of input (fable-5)")
    ok &= expect(providers._cache_read_rate(providers._UNKNOWN_CLAUDE) is None,
                 "cache_read_rate is None when input rate unknown")

    # 5.5 lineup
    opus55 = providers.claude_model_info("claude-opus-5-5")
    ok &= expect(opus55["in"] == 4.0 and opus55["out"] == 20.0, "opus-5-5 in/out pricing")
    ok &= expect(opus55["context"] == 1_000_000, "opus-5-5 context 1M")
    ok &= expect(opus55.get("cache_read") == 0.20, "opus-5-5 explicit cache_read rate (5%)")
    ok &= expect(opus55["thinking"] == "adaptive", "opus-5-5 adaptive thinking")
    ok &= expect(opus55.get("effort") == "high", "opus-5-5 effort pinned high")
    sonnet55 = providers.claude_model_info("claude-sonnet-5-5")
    ok &= expect(sonnet55["in"] == 2.0 and sonnet55["out"] == 10.0, "sonnet-5-5 in/out pricing")
    ok &= expect(sonnet55["context"] == 1_000_000, "sonnet-5-5 context 1M")
    ok &= expect(sonnet55.get("cache_read") == 0.10, "sonnet-5-5 explicit cache_read rate (5%)")
    ok &= expect(sonnet55["thinking"] == "adaptive", "sonnet-5-5 adaptive thinking")
    ok &= expect("effort" not in sonnet55, "sonnet-5-5 has no effort override")
    haiku55 = providers.claude_model_info("claude-haiku-5-5")
    ok &= expect(haiku55["in"] == 0.1 and haiku55["out"] == 0.5, "haiku-5-5 base in/out pricing")
    ok &= expect(haiku55["context"] == 1_000_000, "haiku-5-5 context 1M")
    ok &= expect(haiku55.get("cache_read") == 0.01, "haiku-5-5 explicit cache_read rate (10%)")
    ok &= expect(haiku55["thinking"] == "adaptive", "haiku-5-5 adaptive thinking")
    ok &= expect(haiku55.get("over_100k") == {"in": 0.5, "out": 2.5, "cache_read": 0.05},
                 "haiku-5-5 over-100K tier rates")
    ok &= expect(list(providers.CLAUDE_MODELS)[:5] == [
        "claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1", "claude-sonnet-5-5", "claude-haiku-5-5"],
        "catalog reads newest-first")
    # Prefix order: dated/suffixed 5.5 ids must not fall through to the 5 rows.
    dated_opus = providers.claude_model_info("claude-opus-5-5-20260922")
    ok &= expect(dated_opus["in"] == 4.0 and dated_opus.get("cache_read") == 0.20
                 and dated_opus.get("effort") == "high", "dated opus-5-5 resolves to Opus 5.5 rates, not Opus 5")
    dated_sonnet = providers.claude_model_info("claude-sonnet-5-5-x")
    ok &= expect(dated_sonnet.get("cache_read") == 0.10, "suffixed sonnet-5-5 resolves to Sonnet 5.5")
    dated_haiku = providers.claude_model_info("claude-haiku-5-5-20261001")
    ok &= expect(dated_haiku.get("over_100k") is not None, "dated haiku-5-5 keeps tier data via claude-haiku-5")
    ok &= expect(providers.claude_model_info("claude-opus-5-20260101")["in"] == 5.0, "opus-5 prefix still 5.0")
    ok &= expect(providers.claude_model_info("claude-haiku-4-5-20251001")["in"] == 1.0, "haiku-4 prefix unchanged")
    # Haiku 5.5 tiered rates
    ok &= expect(providers._rates_for_request(haiku55, 50_000) == (0.1, 0.5, 0.01), "haiku-5-5 base tier under 100K")
    ok &= expect(providers._rates_for_request(haiku55, 100_000) == (0.1, 0.5, 0.01), "haiku-5-5 base tier at exactly 100K")
    ok &= expect(providers._rates_for_request(haiku55, 100_001) == (0.5, 2.5, 0.05), "haiku-5-5 over-100K tier")
    ok &= expect(providers._rates_for_request(opus55, 500_000) == (4.0, 20.0, 0.20), "untiered model ignores prompt size")
    ok &= expect(providers._cache_read_rate(haiku55) == 0.01, "cache_read_rate returns haiku-5-5 base tier")

    for s in servers:
        s.close()
        await s.wait_closed()

    print("\nALL PASS" if ok else "\nFAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

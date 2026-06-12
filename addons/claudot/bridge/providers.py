"""
Direct-API chat providers for the Claudot bridge.

The default Claudot path talks to Claude through the Claude Agent SDK
(Claude Code CLI, subscription/OAuth auth). The providers in this module are
the bring-your-own-API-key alternatives:

- AnthropicAPIProvider  — Anthropic Messages API over raw HTTP (httpx).
  Supports Claude Fable 5 (always-on thinking, refusal stop reason, 1M
  context) plus the Opus/Sonnet/Haiku 4.x families.
- OpenAICompatProvider  — any /chat/completions endpoint (OpenAI, OpenRouter,
  Ollama, Gemini's compat endpoint, ...) via a configurable base URL.

Both run their own agentic tool loop against the Godot editor through
godot_tools.execute_tool(), and emit a provider-neutral event stream that
agent_bridge.py forwards to the Godot chat panel:

    {"type": "text", "text": str}                       — completed assistant text block
    {"type": "tool_use", "name": str, "input": dict}    — tool call notification
    {"type": "refusal", "category": str|None}           — safety classifier declined (Fable)
    {"type": "result", "content", "cost_usd", "duration_ms", "num_turns", "usage"}

Providers raise ProviderError for user-facing failures (bad key, bad model,
rate limit); the bridge converts those to chat/error messages.
"""

import json
import logging
import time
from typing import AsyncIterator, Optional

import anyio
import httpx

import godot_tools

logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"

# Hard cap on tool-loop iterations per user turn (each iteration = one API request)
MAX_TOOL_ITERATIONS = 25

# Claude model catalog: context window, USD per MTok in/out, thinking config.
# thinking: "omit"     — never send a thinking param (Fable 5: always-on; Haiku: unsupported)
#           "adaptive" — send {"type": "adaptive"}
CLAUDE_MODELS = {
    "claude-fable-5":   {"context": 1_000_000, "in": 10.0, "out": 50.0, "thinking": "omit"},
    "claude-mythos-5":  {"context": 1_000_000, "in": 10.0, "out": 50.0, "thinking": "omit"},
    "claude-opus-4-8":  {"context": 1_000_000, "in": 5.0,  "out": 25.0, "thinking": "adaptive"},
    "claude-opus-4-7":  {"context": 1_000_000, "in": 5.0,  "out": 25.0, "thinking": "adaptive"},
    "claude-opus-4-6":  {"context": 1_000_000, "in": 5.0,  "out": 25.0, "thinking": "adaptive"},
    "claude-sonnet-4-6": {"context": 1_000_000, "in": 3.0, "out": 15.0, "thinking": "adaptive"},
    "claude-haiku-4-5": {"context": 200_000,  "in": 1.0,  "out": 5.0,  "thinking": "omit"},
}

# Prefix fallbacks for model IDs not in the catalog (future releases, dated IDs)
CLAUDE_PREFIX_DEFAULTS = [
    ("claude-fable",    {"context": 1_000_000, "in": 10.0, "out": 50.0, "thinking": "omit"}),
    ("claude-mythos",   {"context": 1_000_000, "in": 10.0, "out": 50.0, "thinking": "omit"}),
    ("claude-opus-4",   {"context": 1_000_000, "in": 5.0,  "out": 25.0, "thinking": "adaptive"}),
    ("claude-sonnet-4", {"context": 1_000_000, "in": 3.0,  "out": 15.0, "thinking": "adaptive"}),
    ("claude-haiku-4",  {"context": 200_000,  "in": 1.0,  "out": 5.0,  "thinking": "omit"}),
]
_UNKNOWN_CLAUDE = {"context": 200_000, "in": None, "out": None, "thinking": "omit"}


def claude_model_info(model_id: str) -> dict:
    """Catalog entry for a Claude model ID, with prefix fallback for unknown IDs."""
    if model_id in CLAUDE_MODELS:
        return CLAUDE_MODELS[model_id]
    for prefix, info in CLAUDE_PREFIX_DEFAULTS:
        if model_id.startswith(prefix):
            return info
    return _UNKNOWN_CLAUDE


def context_window_for_model(model_id: str) -> int:
    return claude_model_info(model_id)["context"]


class ProviderError(Exception):
    """User-facing provider failure (bad key, unknown model, rate limit, ...)."""


class DirectChatProvider:
    """Base class: conversation history + interrupt handling."""

    def __init__(self, api_key: str, model: str, system_prompt: str, base_url: str = ""):
        self.api_key = api_key
        self.model = model
        self.system_prompt = system_prompt
        self.base_url = base_url
        self.history: list[dict] = []
        self._interrupted = anyio.Event()

    def reset_history(self) -> None:
        self.history = []

    def interrupt(self) -> None:
        self._interrupted.set()

    def _begin_turn(self) -> None:
        # anyio.Event cannot be cleared — replace it each turn
        self._interrupted = anyio.Event()

    async def run_turn(self, prompt: str) -> AsyncIterator[dict]:
        raise NotImplementedError
        yield  # pragma: no cover


def _http_error_message(status: int, body_text: str, provider_label: str) -> str:
    """Map an HTTP error to a user-facing message, surfacing the API's own detail."""
    detail = body_text[:400]
    try:
        parsed = json.loads(body_text)
        detail = (
            parsed.get("error", {}).get("message")
            or parsed.get("message")
            or detail
        )
    except (json.JSONDecodeError, AttributeError):
        pass

    if status == 401:
        return f"{provider_label}: invalid or missing API key. Check your key in Claudot Settings. ({detail})"
    if status == 403:
        return f"{provider_label}: API key lacks permission. ({detail})"
    if status == 404:
        return f"{provider_label}: model or endpoint not found — check the model name. ({detail})"
    if status == 429:
        return f"{provider_label}: rate limited. Wait a moment and try again. ({detail})"
    if status >= 500:
        return f"{provider_label}: server error {status}. Try again shortly. ({detail})"
    return f"{provider_label}: request failed ({status}): {detail}"


async def _iter_sse_data(response: httpx.Response) -> AsyncIterator[dict]:
    """Yield parsed JSON payloads from an SSE stream (data: lines)."""
    async for line in response.aiter_lines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            logger.warning(f"Skipping malformed SSE payload: {payload[:200]}")


class AnthropicAPIProvider(DirectChatProvider):
    """
    Anthropic Messages API over raw HTTP, with streaming and a Godot tool loop.

    Model-aware request shaping:
    - Fable 5 / Mythos 5: no thinking param (always-on), refusal stop reason handled
    - Opus 4.6+/Sonnet 4.6: adaptive thinking
    - thinking/tool_use/text blocks are replayed verbatim in history (required
      for tool loops, and strictly required on Fable 5)
    """

    LABEL = "Anthropic API"

    def _headers(self) -> dict:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def _request_body(self) -> dict:
        info = claude_model_info(self.model)
        body = {
            "model": self.model,
            "max_tokens": 64000,
            "stream": True,
            "system": [
                {
                    "type": "text",
                    "text": self.system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": self.history,
            "tools": godot_tools.as_anthropic_tools(),
            # Auto-place a cache breakpoint on the last cacheable block so the
            # growing conversation prefix is reused across turns.
            "cache_control": {"type": "ephemeral"},
        }
        if info["thinking"] == "adaptive":
            body["thinking"] = {"type": "adaptive"}
        return body

    async def run_turn(self, prompt: str) -> AsyncIterator[dict]:
        self._begin_turn()
        start = time.monotonic()
        self.history.append({"role": "user", "content": prompt})

        info = claude_model_info(self.model)
        total_cost = 0.0
        cost_known = info["in"] is not None
        last_ctx_tokens = 0
        final_output_tokens = 0
        total_input_tokens = 0
        last_text = ""
        num_requests = 0
        base = (self.base_url or ANTHROPIC_API_URL).rstrip("/")

        for _ in range(MAX_TOOL_ITERATIONS):
            if self._interrupted.is_set():
                break
            num_requests += 1

            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0)) as client:
                    async with client.stream(
                        "POST", f"{base}/v1/messages",
                        headers=self._headers(), json=self._request_body(),
                    ) as response:
                        if response.status_code != 200:
                            body_text = (await response.aread()).decode("utf-8", "replace")
                            raise ProviderError(_http_error_message(response.status_code, body_text, self.LABEL))

                        content_blocks: list[dict] = []
                        stop_reason = None
                        stop_details = None
                        usage_in = 0
                        usage_out = 0
                        cache_read = 0
                        cache_write = 0
                        interrupted_mid_stream = False

                        async for event in _iter_sse_data(response):
                            if self._interrupted.is_set():
                                interrupted_mid_stream = True
                                break
                            etype = event.get("type")

                            if etype == "message_start":
                                u = event.get("message", {}).get("usage", {})
                                usage_in = u.get("input_tokens", 0)
                                cache_read = u.get("cache_read_input_tokens", 0) or 0
                                cache_write = u.get("cache_creation_input_tokens", 0) or 0

                            elif etype == "content_block_start":
                                block = dict(event.get("content_block", {}))
                                # Accumulators for streamed fields
                                if block.get("type") == "tool_use":
                                    block["_partial_json"] = ""
                                    block["input"] = {}
                                content_blocks.append(block)

                            elif etype == "content_block_delta":
                                delta = event.get("delta", {})
                                idx = event.get("index", len(content_blocks) - 1)
                                if idx >= len(content_blocks):
                                    continue
                                block = content_blocks[idx]
                                dtype = delta.get("type")
                                if dtype == "text_delta":
                                    block["text"] = block.get("text", "") + delta.get("text", "")
                                elif dtype == "thinking_delta":
                                    block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
                                elif dtype == "signature_delta":
                                    block["signature"] = block.get("signature", "") + delta.get("signature", "")
                                elif dtype == "input_json_delta":
                                    block["_partial_json"] = block.get("_partial_json", "") + delta.get("partial_json", "")

                            elif etype == "content_block_stop":
                                idx = event.get("index", len(content_blocks) - 1)
                                if idx < len(content_blocks):
                                    block = content_blocks[idx]
                                    if block.get("type") == "tool_use":
                                        raw = block.pop("_partial_json", "")
                                        try:
                                            block["input"] = json.loads(raw) if raw.strip() else {}
                                        except json.JSONDecodeError:
                                            block["input"] = {}
                                    # Emit completed text blocks as they finish
                                    if block.get("type") == "text" and block.get("text"):
                                        last_text = block["text"]
                                        yield {"type": "text", "text": block["text"]}

                            elif etype == "message_delta":
                                stop_reason = event.get("delta", {}).get("stop_reason", stop_reason)
                                stop_details = event.get("delta", {}).get("stop_details", stop_details)
                                u = event.get("usage", {})
                                usage_out = u.get("output_tokens", usage_out)

                        if interrupted_mid_stream:
                            # Discard the partial assistant turn entirely — incomplete
                            # thinking/tool_use blocks must not be replayed.
                            break

            except ProviderError:
                raise
            except httpx.ConnectError as e:
                raise ProviderError(f"{self.LABEL}: could not reach {base} ({e}). Check your network.")
            except httpx.TimeoutException:
                raise ProviderError(f"{self.LABEL}: request timed out.")
            except httpx.HTTPError as e:
                raise ProviderError(f"{self.LABEL}: HTTP error: {e}")

            # Account usage for this request
            total_input_tokens += usage_in + cache_read + cache_write
            final_output_tokens += usage_out
            last_ctx_tokens = usage_in + cache_read + cache_write + usage_out
            if cost_known:
                total_cost += (
                    usage_in * info["in"]
                    + cache_read * info["in"] * 0.1
                    + cache_write * info["in"] * 1.25
                    + usage_out * info["out"]
                ) / 1_000_000

            if stop_reason == "refusal":
                category = None
                if isinstance(stop_details, dict):
                    category = stop_details.get("category")
                yield {"type": "refusal", "category": category}
                # A refused turn leaves no replayable assistant content
                break

            # Strip private accumulators, store the assistant turn verbatim
            # (thinking blocks included — required for replay on the same model).
            clean_blocks = [{k: v for k, v in b.items() if not k.startswith("_")} for b in content_blocks]
            if clean_blocks:
                self.history.append({"role": "assistant", "content": clean_blocks})

            tool_uses = [b for b in clean_blocks if b.get("type") == "tool_use"]
            if stop_reason == "tool_use" and tool_uses:
                results = []
                for tu in tool_uses:
                    yield {"type": "tool_use", "name": tu.get("name", "?"), "input": tu.get("input", {})}
                    result_text = await godot_tools.execute_tool(tu.get("name", ""), tu.get("input", {}) or {})
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.get("id", ""),
                        "content": result_text,
                        "is_error": result_text.startswith("ERROR:"),
                    })
                self.history.append({"role": "user", "content": results})
                continue  # next loop iteration

            if stop_reason == "max_tokens":
                yield {"type": "text", "text": "_(Response hit the output token limit and was truncated.)_"}
            break

        duration_ms = int((time.monotonic() - start) * 1000)
        context_pct = round(last_ctx_tokens / info["context"] * 100, 1) if last_ctx_tokens else 0.0
        yield {
            "type": "result",
            "content": last_text,
            "cost_usd": round(total_cost, 6) if cost_known else 0.0,
            "duration_ms": duration_ms,
            "num_turns": num_requests,
            "usage": {
                "input_tokens": total_input_tokens,
                "output_tokens": final_output_tokens,
                "total_tokens": total_input_tokens + final_output_tokens,
                "context_pct": context_pct,
            },
        }


class OpenAICompatProvider(DirectChatProvider):
    """
    OpenAI-compatible /chat/completions provider with streaming and a Godot
    tool loop. Works with OpenAI, OpenRouter, Ollama, and any other endpoint
    speaking the chat-completions wire format (set base_url accordingly).
    """

    LABEL = "OpenAI-compatible API"

    def __init__(self, api_key: str, model: str, system_prompt: str, base_url: str = ""):
        super().__init__(api_key, model, system_prompt, base_url or "https://api.openai.com/v1")
        self._supports_stream_options = True  # downgraded on first 400 mentioning it

    def _headers(self) -> dict:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request_body(self) -> dict:
        messages = [{"role": "system", "content": self.system_prompt}] + self.history
        body = {
            "model": self.model,
            "messages": messages,
            "tools": godot_tools.as_openai_tools(),
            "stream": True,
        }
        if self._supports_stream_options:
            body["stream_options"] = {"include_usage": True}
        return body

    async def run_turn(self, prompt: str) -> AsyncIterator[dict]:
        self._begin_turn()
        start = time.monotonic()
        self.history.append({"role": "user", "content": prompt})

        base = self.base_url.rstrip("/")
        last_text = ""
        total_in = 0
        total_out = 0
        num_requests = 0

        for _ in range(MAX_TOOL_ITERATIONS):
            if self._interrupted.is_set():
                break
            num_requests += 1

            retry_without_stream_options = False
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0)) as client:
                    async with client.stream(
                        "POST", f"{base}/chat/completions",
                        headers=self._headers(), json=self._request_body(),
                    ) as response:
                        if response.status_code != 200:
                            body_text = (await response.aread()).decode("utf-8", "replace")
                            if (
                                response.status_code == 400
                                and self._supports_stream_options
                                and "stream_options" in body_text
                            ):
                                self._supports_stream_options = False
                                retry_without_stream_options = True
                            else:
                                raise ProviderError(_http_error_message(response.status_code, body_text, self.LABEL))

                        if not retry_without_stream_options:
                            text_acc = ""
                            tool_calls: dict[int, dict] = {}
                            finish_reason = None
                            interrupted_mid_stream = False

                            async for chunk in _iter_sse_data(response):
                                if self._interrupted.is_set():
                                    interrupted_mid_stream = True
                                    break
                                usage = chunk.get("usage")
                                if usage:
                                    total_in += usage.get("prompt_tokens", 0)
                                    total_out += usage.get("completion_tokens", 0)
                                choices = chunk.get("choices") or []
                                if not choices:
                                    continue
                                choice = choices[0]
                                finish_reason = choice.get("finish_reason") or finish_reason
                                delta = choice.get("delta") or {}
                                if delta.get("content"):
                                    text_acc += delta["content"]
                                for tc in delta.get("tool_calls") or []:
                                    idx = tc.get("index", 0)
                                    entry = tool_calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                                    if tc.get("id"):
                                        entry["id"] = tc["id"]
                                    fn = tc.get("function") or {}
                                    if fn.get("name"):
                                        entry["name"] = fn["name"]
                                    if fn.get("arguments"):
                                        entry["arguments"] += fn["arguments"]

                            if interrupted_mid_stream:
                                break

            except ProviderError:
                raise
            except httpx.ConnectError as e:
                raise ProviderError(f"{self.LABEL}: could not reach {base} ({e}). Check the base URL and your network.")
            except httpx.TimeoutException:
                raise ProviderError(f"{self.LABEL}: request timed out.")
            except httpx.HTTPError as e:
                raise ProviderError(f"{self.LABEL}: HTTP error: {e}")

            if retry_without_stream_options:
                num_requests -= 1
                continue  # immediately retry the same turn without stream_options

            if text_acc:
                last_text = text_acc
                yield {"type": "text", "text": text_acc}

            if finish_reason == "tool_calls" and tool_calls:
                assistant_msg = {
                    "role": "assistant",
                    "content": text_acc or None,
                    "tool_calls": [
                        {
                            "id": entry["id"] or f"call_{idx}",
                            "type": "function",
                            "function": {"name": entry["name"], "arguments": entry["arguments"] or "{}"},
                        }
                        for idx, entry in sorted(tool_calls.items())
                    ],
                }
                self.history.append(assistant_msg)

                for idx, entry in sorted(tool_calls.items()):
                    try:
                        args = json.loads(entry["arguments"]) if entry["arguments"].strip() else {}
                    except json.JSONDecodeError:
                        args = {}
                    yield {"type": "tool_use", "name": entry["name"], "input": args}
                    result_text = await godot_tools.execute_tool(entry["name"], args)
                    self.history.append({
                        "role": "tool",
                        "tool_call_id": entry["id"] or f"call_{idx}",
                        "content": result_text,
                    })
                continue

            # Terminal turn — store plain assistant message
            if text_acc:
                self.history.append({"role": "assistant", "content": text_acc})
            if finish_reason == "length":
                yield {"type": "text", "text": "_(Response hit the output token limit and was truncated.)_"}
            break

        duration_ms = int((time.monotonic() - start) * 1000)
        yield {
            "type": "result",
            "content": last_text,
            "cost_usd": 0.0,
            "duration_ms": duration_ms,
            "num_turns": num_requests,
            "usage": {
                "input_tokens": total_in,
                "output_tokens": total_out,
                "total_tokens": total_in + total_out,
                "context_pct": 0.0,
            },
        }

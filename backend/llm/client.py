"""OpenAI-compatible chat completion client (Rust llm.rs chat_completion).

``get_llm_config()`` delegates to ``config.get_config()`` — the single source
of truth for LLM settings — and adapts the snake_case fields to the camelCase
keys this module historically returned (so ``chat_completion`` and its callers
in worker.py / coordinator.py / api/*.py keep working unchanged). Uses
httpx.AsyncClient with bearer auth and a 120s timeout.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from config import get_config


def get_llm_config() -> dict[str, Any]:
    """Build the LLM config dict, delegating to ``config.get_config()``.

    The single source of truth is ``config.get_config()`` (live env read, so
    ``config.set_config(model)`` hot-switches are picked up without a restart).
    This wrapper only adapts the field names to the camelCase shape
    ``chat_completion`` consumes (apiKey / baseUrl / model / temperature /
    maxTokens), preserving the pre-CF-02 return contract for all callers.

    Multi-model provider connection-level fields (requestTimeout / extraHeaders
    / proxy) are surfaced so ``chat_completion`` can parameterize httpx. They
    default to 120s / None / "" when the cache omits them (legacy callers).
    """
    cfg = get_config()
    return {
        "apiKey": cfg["api_key"],
        "baseUrl": cfg["base_url"],
        "model": cfg["model"],
        "temperature": cfg["temperature"],
        "maxTokens": cfg["max_tokens"],
        # Connection-level config (provider owns; shared by all models).
        "requestTimeout": cfg.get("request_timeout", 120.0),
        "extraHeaders": cfg.get("extra_headers"),
        "proxy": cfg.get("proxy", ""),
    }


async def chat_completion(config: dict[str, Any], messages: list[dict[str, str]]) -> str:
    """Call an OpenAI-compatible ``/chat/completions`` endpoint.

    Returns ``choices[0].message.content``. Raises ``RuntimeError`` on non-200
    status or empty choices.

    Connection-level config consumed from the config dict (falls back to safe
    defaults when absent, so legacy 5-key configs still work):
    - ``requestTimeout`` → httpx ``timeout`` (default 120s).
    - ``extraHeaders`` → merged into the request headers (default: none).
    - ``proxy`` → httpx ``proxy`` (empty/None = no proxy, direct connection).
    """
    url = f"{config['baseUrl'].rstrip('/')}/chat/completions"
    body = {
        "model": config["model"],
        "messages": messages,
        "temperature": config["temperature"],
        "max_tokens": config["maxTokens"],
    }
    # Build headers: bearer auth + any provider-configured extra headers
    # (e.g. X-Org-Id for some proxies). extraHeaders None/empty = auth only.
    headers = {"Authorization": f"Bearer {config['apiKey']}"}
    extra_headers = config.get("extraHeaders") or {}
    if extra_headers:
        headers.update(extra_headers)

    # httpx transport kwargs: timeout always set (default 120s); proxy only
    # when configured (passing "" would be treated as "no proxy" by httpx but
    # explicit omission is cleaner and avoids edge-case proxy resolution).
    timeout = float(config.get("requestTimeout", 120.0) or 120.0)
    proxy = config.get("proxy", "") or ""
    client_kwargs: dict[str, Any] = {"timeout": timeout}
    if proxy:
        client_kwargs["proxy"] = proxy

    async with httpx.AsyncClient(**client_kwargs) as client:
        resp = await client.post(
            url,
            json=body,
            headers=headers,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"LLM API error {resp.status_code}: {resp.text}")
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("LLM returned empty choices")
        return choices[0]["message"]["content"]


async def chat_completion_stream(
    config: dict[str, Any], messages: list[dict[str, str]]
) -> AsyncIterator[tuple[str, str, int | None, int | None]]:
    """Stream an OpenAI-compatible ``/chat/completions`` response (SSE ``stream: true``).

    Yields ``(content_delta, reasoning_delta, completion_tokens, reasoning_tokens)``
    tuples per SSE chunk:

    - ``content_delta`` — incremental ``choices[0].delta.content`` (the visible
      reply text; ``""`` on chunks that carry only reasoning/role/usage data).
    - ``reasoning_delta`` — incremental ``choices[0].delta.reasoning_content``
      (the model's internal reasoning chain; ``""`` for non-reasoning models
      or chunks without reasoning). DeepSeek/GPT-o1-style models stream this
      *before* the visible content; non-reasoning providers simply never set it.
    - ``completion_tokens`` — ``None`` for every chunk except the final usage
      chunk (emitted once ``stream_options.include_usage`` is set): the real
      ``usage.completion_tokens`` for the whole completion.
    - ``reasoning_tokens`` — ``None`` except on the final usage chunk: the real
      ``usage.completion_tokens_details.reasoning_tokens`` (how many of the
      completion tokens were reasoning vs visible). Absent for providers that
      don't break tokens down → caller treats as 0.

    The async generator closes the response on early ``break`` by the consumer
    (httpx ``stream()`` context manager tears down the underlying connection).

    Raises ``RuntimeError`` on non-200 status (read before streaming begins).

    Connection-level config consumed from the config dict (same as
    ``chat_completion``): ``requestTimeout`` → httpx timeout (default 120s),
    ``extraHeaders`` → merged into request headers, ``proxy`` → httpx proxy
    (empty = direct). Legacy 5-key configs fall back to the defaults.
    """
    url = f"{config['baseUrl'].rstrip('/')}/chat/completions"
    body = {
        "model": config["model"],
        "messages": messages,
        "temperature": config["temperature"],
        "max_tokens": config["maxTokens"],
        "stream": True,
        # Ask the server to emit one final chunk carrying the real token usage
        # so the stats status line can show the authoritative count at "done".
        "stream_options": {"include_usage": True},
    }
    # Build headers: bearer auth + provider-configured extra headers.
    headers = {"Authorization": f"Bearer {config['apiKey']}"}
    extra_headers = config.get("extraHeaders") or {}
    if extra_headers:
        headers.update(extra_headers)

    # httpx transport kwargs: timeout always set (default 120s); proxy only
    # when configured.
    timeout = float(config.get("requestTimeout", 120.0) or 120.0)
    proxy = config.get("proxy", "") or ""
    client_kwargs: dict[str, Any] = {"timeout": timeout}
    if proxy:
        client_kwargs["proxy"] = proxy

    async with httpx.AsyncClient(**client_kwargs) as client:
        async with client.stream(
            "POST",
            url,
            json=body,
            headers=headers,
        ) as resp:
            if resp.status_code != 200:
                # drain so the body is available for the error message
                body_text = await resp.aread()
                raise RuntimeError(
                    f"LLM API error {resp.status_code}: {body_text.decode('utf-8', 'replace')}"
                )
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if line.startswith("data: "):
                    payload = line[len("data: ") :]
                elif line.startswith("data:"):
                    payload = line[len("data:") :]
                else:
                    # ignore keep-alive comments / SSE event framing
                    continue
                if payload.strip() == "[DONE]":
                    return
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                # final usage chunk (include_usage): usage present, choices empty
                usage = chunk.get("usage")
                completion_tokens = (
                    usage.get("completion_tokens") if isinstance(usage, dict) else None
                )
                # reasoning_tokens lives under completion_tokens_details on the
                # final usage chunk (None mid-stream — only the final chunk has it).
                reasoning_tokens: int | None = None
                if isinstance(usage, dict):
                    details = usage.get("completion_tokens_details") or {}
                    if isinstance(details, dict):
                        rt = details.get("reasoning_tokens")
                        if isinstance(rt, int):
                            reasoning_tokens = rt
                choices = chunk.get("choices") or []
                content_delta = ""
                reasoning_delta = ""
                if choices:
                    delta = choices[0].get("delta") or {}
                    content_delta = delta.get("content") or ""
                    reasoning_delta = delta.get("reasoning_content") or ""
                yield content_delta, reasoning_delta, completion_tokens, reasoning_tokens

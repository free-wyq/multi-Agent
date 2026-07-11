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
    """
    cfg = get_config()
    return {
        "apiKey": cfg["api_key"],
        "baseUrl": cfg["base_url"],
        "model": cfg["model"],
        "temperature": cfg["temperature"],
        "maxTokens": cfg["max_tokens"],
    }


async def chat_completion(config: dict[str, Any], messages: list[dict[str, str]]) -> str:
    """Call an OpenAI-compatible ``/chat/completions`` endpoint.

    Returns ``choices[0].message.content``. Raises ``RuntimeError`` on non-200
    status or empty choices.
    """
    url = f"{config['baseUrl'].rstrip('/')}/chat/completions"
    body = {
        "model": config["model"],
        "messages": messages,
        "temperature": config["temperature"],
        "max_tokens": config["maxTokens"],
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {config['apiKey']}"},
            timeout=120.0,
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
) -> AsyncIterator[tuple[str, int | None]]:
    """Stream an OpenAI-compatible ``/chat/completions`` response (SSE ``stream: true``).

    Yields ``(content_delta, completion_tokens)`` tuples per SSE chunk:

    - ``content_delta`` is the incremental text of ``choices[0].delta.content``
      (may be ``""`` for chunks that carry only tool/role/usage data).
    - ``completion_tokens`` is ``None`` for every chunk except the final usage
      chunk (emitted once ``stream_options.include_usage`` is set): the real
      ``usage.completion_tokens`` for the whole completion. Callers forward it
      as the terminal ``phase="done"`` statistic.

    The async generator closes the response on early ``break`` by the consumer
    (httpx ``stream()`` context manager tears down the underlying connection).

    Raises ``RuntimeError`` on non-200 status (read before streaming begins).
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
    async with httpx.AsyncClient() as client:
        async with client.stream(
            "POST",
            url,
            json=body,
            headers={"Authorization": f"Bearer {config['apiKey']}"},
            timeout=120.0,
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
                choices = chunk.get("choices") or []
                delta = ""
                if choices:
                    delta = (choices[0].get("delta") or {}).get("content") or ""
                yield delta, completion_tokens

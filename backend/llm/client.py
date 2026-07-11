"""OpenAI-compatible chat completion client (Rust llm.rs chat_completion).

``get_llm_config()`` delegates to ``config.get_config()`` — the single source
of truth for LLM settings — and adapts the snake_case fields to the camelCase
keys this module historically returned (so ``chat_completion`` and its callers
in worker.py / coordinator.py / api/*.py keep working unchanged). Uses
httpx.AsyncClient with bearer auth and a 120s timeout.
"""
from __future__ import annotations

from typing import Any

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

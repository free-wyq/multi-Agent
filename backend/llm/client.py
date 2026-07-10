"""OpenAI-compatible chat completion client (Rust llm.rs chat_completion).

Reads config from environment (OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL)
which are loaded by config.py at import time. Uses httpx.AsyncClient with
bearer auth and a 120s timeout.
"""
from __future__ import annotations

import os
from typing import Any

import httpx


def get_llm_config() -> dict[str, Any]:
    """Build the LLM config dict from environment variables.

    Reads keys loaded by config.py (which calls dotenv at import). Falls back
    to ANTHROPIC_API_KEY if OPENAI_API_KEY is absent, and to the OpenAI default
    base URL if OPENAI_BASE_URL is unset.
    """
    return {
        "apiKey": os.environ.get("OPENAI_API_KEY", "")
        or os.environ.get("ANTHROPIC_API_KEY", ""),
        "baseUrl": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "model": os.environ.get("LLM_MODEL", "glm-5.1"),
        "temperature": 0.0,
        "maxTokens": 4096,
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

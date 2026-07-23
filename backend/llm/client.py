"""OpenAI-compatible chat completion client (Rust llm.rs chat_completion).

``get_llm_config()`` delegates to ``config.get_config()`` — the single source
of truth for LLM settings — and adapts the snake_case fields to the camelCase
keys this module historically returned (so ``chat_completion`` and its callers
in worker.py / coordinator.py / api/*.py keep working unchanged). Uses
httpx.AsyncClient with bearer auth and a 120s timeout.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, AsyncIterator

import httpx

from config import get_config


# ── 重试策略（应用层，非 httpx transport retries）────────────────────────────
# 实测彩讯/智谱经 new-api 网关间歇性返回 500 ``do_request_failed`` 或 200 + 空 body
# （上游抽风，非鉴权/参数问题）。原 0 重试直调 httpx 一遇抖动即抛 RuntimeError →
# 调用方兜底成「卡壳能再说一遍」误导用户。这里对「瞬态」失败做指数退避重试：
#   - 网关 5xx / 429（限流）→ 可重试（4xx 鉴权/参数不重试，重试也修不好）
#   - httpx.TransportError（connect/read/timeout 等网络层）→ 可重试
#   - 200 但 choices 空（网关返空 body）→ 可重试
# 重试次数取 provider 的 ``max_retries``（默认 2，即总尝试 1+2=3 次）。退避 0.5/1.0/...
# 秒。注意：流式一旦 status 200 通过、开始 yield token 即不再重试（已吐半个回复，
# 重试会重复 token）；重试只覆盖「连接建立 + status 校验」阶段。
_RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})
_EMPTY_CHOICES_MARKER = "empty choices"


def _retry_backoff(attempt: int) -> float:
    """指数退避：0.5s → 1.0s → 2.0s ...（attempt 从 0 起）。"""
    return 0.5 * (2 ** attempt)


def _is_retryable_llm_error(exc: BaseException) -> bool:
    """该异常是否值得再试一次。瞬态（5xx/429/网络/空 200）→ True；4xx 鉴权/参数 → False。"""
    # 网络层瞬态：connect refused / read timeout / pipe broken 等
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, RuntimeError):
        msg = str(exc)
        # ``LLM API error 5xx: ...`` / ``LLM API error 429: ...``
        m = re.search(r"LLM API error (\d+)", msg)
        if m and int(m.group(1)) in _RETRYABLE_STATUS:
            return True
        # 200 但网关返空 body（彩讯间歇故障模式）→ 值得重试
        if _EMPTY_CHOICES_MARKER in msg:
            return True
    return False


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
        # Application-layer retry budget for transient upstream failures
        # (gateway 5xx/429, network errors, 200-but-empty body). Drives the
        # retry loop in chat_completion / chat_completion_stream. NOT httpx
        # transport retries (those need httpx.HTTPTransport(retries=) which we
        # don't use — direct httpx, app-level asyncio.sleep backoff instead).
        "maxRetries": cfg.get("max_retries", 2),
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

    Retry: transient failures (gateway 5xx/429, network errors, 200-but-empty
    body from flapping upstreams) are retried up to ``max_retries`` times with
    exponential backoff before raising. Non-transient errors (4xx auth/param)
    raise immediately. See ``_is_retryable_llm_error`` for the policy.
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

    max_retries = int(config.get("maxRetries", 0) or 0)
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.post(url, json=body, headers=headers)
            if resp.status_code != 200:
                # Mark 5xx/429 so the retry policy can flag it retryable.
                raise RuntimeError(f"LLM API error {resp.status_code}: {resp.text}")
            data = resp.json()
            choices = data.get("choices", [])
            if not choices:
                raise RuntimeError("LLM returned empty choices")
            return choices[0]["message"]["content"]
        except Exception as e:
            last_exc = e
            if attempt < max_retries and _is_retryable_llm_error(e):
                await asyncio.sleep(_retry_backoff(attempt))
                continue
            raise
    # Unreachable: loop either returns content or raises last_exc above.
    raise last_exc if last_exc else RuntimeError("LLM call failed without exception")


async def chat_completion_stream(
    config: dict[str, Any], messages: list[dict[str, str]]
) -> AsyncIterator[tuple[str, str, int | None, int | None]]:
    """Stream an OpenAI-compatible ``/chat/completions`` response (SSE ``stream: true``).

    Yields ``(content_delta, reasoning_delta, completion_tokens, reasoning_tokens)``
    tuples per SSE chunk:

    - ``content_delta`` — incremental ``choices[0].delta.content`` (the visible
      reply text; ``""`` on chunks that carry only reasoning/role/usage data).
    - ``reasoning_delta`` — incremental reasoning chain text. Standardized across
      upstream field variants: OpenAI/DeepSeek use ``reasoning_content``; some
      gateways (e.g. the kimi proxy via new-api) emit ``reasoning`` instead or
      alongside. Both are accepted here so the engine sees one consistent stream
      regardless of which field the upstream favors — this function IS the
      OpenAI-protocol normalization layer (the engine consumes this normalized
      tuple, never raw delta field names). ``""`` for non-reasoning models.
    - ``completion_tokens`` — ``None`` for every chunk except the final usage
      chunk (emitted once ``stream_options.include_usage`` is set): the real
      ``usage.completion_tokens`` for the whole completion.
    - ``reasoning_tokens`` — ``None`` except on the final usage chunk: the real
      ``usage.completion_tokens_details.reasoning_tokens`` (how many of the
      completion tokens were reasoning vs visible). Absent for providers that
      don't break tokens down (e.g. the kimi gateway returns only
      ``{prompt, completion, total}`` with no ``completion_tokens_details``) →
      falls back to a length-based estimate of the accumulated reasoning text
      so the "（含 N 推理）" status line still shows for reasoning models whose
      gateway omits the field.

    The async generator closes the response on early ``break`` by the consumer
    (httpx ``stream()`` context manager tears down the underlying connection).

    Raises ``RuntimeError`` on non-200 status (read before streaming begins).

    Connection-level config consumed from the config dict (same as
    ``chat_completion``): ``requestTimeout`` → httpx timeout (default 120s),
    ``extraHeaders`` → merged into request headers, ``proxy`` → httpx proxy
    (empty = direct). Legacy 5-key configs fall back to the defaults.

    Retry: the connection-establishment + status-code-check phase is retried on
    transient failures (gateway 5xx/429, network errors) up to ``max_retries``
    times with exponential backoff. Once status 200 passes and the first SSE
    line is being yielded, no retry happens — re-issuing would duplicate tokens
    already streamed to the user. So a mid-stream disconnect raises (caller
    falls back to the apology), but a flapping gateway that 500s on connect
    gets a quiet retry before the user ever sees an error.
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

    max_retries = int(config.get("maxRetries", 0) or 0)
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
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
                            # OpenAI-protocol normalization: accept both reasoning field
                            # variants upstreams use. OpenAI/DeepSeek (and the official
                            # spec extension) put the chain-of-thought under
                            # ``reasoning_content``; some gateways (e.g. new-api proxying
                            # kimi) emit ``reasoning`` instead. Prefer reasoning_content
                            # when present (the canonical name), fall back to reasoning.
                            # The engine consumes the normalized reasoning_delta here, so
                            # it never has to know which field the upstream favored.
                            reasoning_delta = delta.get("reasoning_content") or delta.get("reasoning") or ""
                        yield content_delta, reasoning_delta, completion_tokens, reasoning_tokens
                    return  # stream completed normally — exit the retry loop
        except Exception as e:
            last_exc = e
            if attempt < max_retries and _is_retryable_llm_error(e):
                await asyncio.sleep(_retry_backoff(attempt))
                continue
            raise
    if last_exc is not None:
        raise last_exc

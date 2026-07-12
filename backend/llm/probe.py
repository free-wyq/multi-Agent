"""Provider connectivity probing (PRD 多模型服务商 · 连通性测试).

``test_provider`` issues a minimal ``/chat/completions`` call against a
provider's own connection-level config (base_url / api_key / proxy /
extra_headers / request_timeout) so the UI's "测试连通" button gets a real
go/no-go signal before the provider is activated. ``fetch_models`` pulls the
upstream ``/v1/models`` catalog and normalizes it into ``LlmModel`` entries
for the "拉取模型" button.

These run against an explicit :class:`~store.entities.LlmProviderEntity` (NOT
``config.get_config()``) — the point is to test a *specific* provider as
configured, even if it is not the active one. The active cache is never read
here, so testing a non-active provider doesn't disturb the running engine.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from store.crud import _select_model


async def test_provider(entity: Any) -> dict[str, Any]:
    """Probe a provider's connectivity with a minimal chat completion.

    Sends a 1-token ``ping`` message using the entity's own connection config
    (base_url + api_key + proxy + extra_headers + request_timeout + the
    resolved active model via :func:`_select_model`). Returns a structured
    result the UI renders directly — never raises (all failure modes are
    captured into ``error`` so a flaky provider doesn't 500 the route):

    - ``ok``: True iff the upstream returned 200 with parseable choices.
    - ``latency_ms``: round-trip wall-clock in ms (0 on failure).
    - ``error``: short human-readable failure reason (empty on success).
    - ``status_code``: HTTP status (None if the request never reached the
      server — DNS/connect/timeout/proxy errors).

    Minimal-payload design: ``max_tokens=1`` + a single ``"ping"`` user turn
    keeps the probe cheap (most providers bill <0.0001¢ for one token) while
    still exercising auth, routing, and the model field end-to-end.
    """
    base_url = (getattr(entity, "base_url", "") or "").rstrip("/")
    api_key = getattr(entity, "api_key", "") or ""
    if not base_url:
        return {"ok": False, "latency_ms": 0, "error": "base_url 未配置", "status_code": None}
    if not api_key:
        return {"ok": False, "latency_ms": 0, "error": "api_key 未配置", "status_code": None}

    model = _select_model(entity)
    url = f"{base_url}/chat/completions"

    # Connection-level config from the entity (mirrors what the engine will
    # actually use when this provider runs — so a passing probe means the real
    # call will work too).
    timeout = float(getattr(entity, "request_timeout", 120.0) or 120.0)
    proxy = getattr(entity, "proxy", "") or ""
    extra_headers = getattr(entity, "extra_headers", None) or {}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)

    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }

    # Transport options: proxy only when configured (httpx treats "" as
    # "no proxy" but passing None is the explicit no-proxy form).
    transport_kwargs: dict[str, Any] = {"timeout": timeout}
    if proxy:
        transport_kwargs["proxy"] = proxy

    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(**transport_kwargs) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.TimeoutException:
        return {
            "ok": False,
            "latency_ms": int((time.perf_counter() - start) * 1000),
            "error": f"请求超时（{timeout:.0f}s）",
            "status_code": None,
        }
    except httpx.ConnectError as exc:
        return {
            "ok": False,
            "latency_ms": int((time.perf_counter() - start) * 1000),
            "error": f"连接失败：{exc}",
            "status_code": None,
        }
    except httpx.HTTPError as exc:
        # Proxy / transport / protocol errors not covered above.
        return {
            "ok": False,
            "latency_ms": int((time.perf_counter() - start) * 1000),
            "error": f"HTTP 错误：{exc}",
            "status_code": None,
        }

    latency_ms = int((time.perf_counter() - start) * 1000)

    if resp.status_code != 200:
        # Truncate the body so a verbose upstream error doesn't bloat the
        # response (and never echo the api_key if it appeared in the error).
        snippet = (resp.text or "")[:300]
        return {
            "ok": False,
            "latency_ms": latency_ms,
            "error": f"上游返回 {resp.status_code}：{snippet}",
            "status_code": resp.status_code,
        }

    try:
        data = resp.json()
    except Exception:
        return {
            "ok": False,
            "latency_ms": latency_ms,
            "error": "响应非 JSON（可能 base_url 指向了非 OpenAI 兼容端点）",
            "status_code": resp.status_code,
        }

    choices = data.get("choices", []) if isinstance(data, dict) else []
    if not choices:
        return {
            "ok": False,
            "latency_ms": latency_ms,
            "error": "上游返回空 choices（model 可能无效）",
            "status_code": resp.status_code,
        }

    return {"ok": True, "latency_ms": latency_ms, "error": "", "status_code": 200}


def _connection_kwargs(entity: Any) -> tuple[dict[str, Any], dict[str, str]]:
    """Build (httpx transport kwargs, request headers) from an entity.

    Shared by ``test_provider`` and ``fetch_models`` so both probes use the
    exact same connection-level config (proxy / timeout / extra_headers +
    bearer auth). Returns ``(client_kwargs, headers)``:
    - ``client_kwargs``: ``{"timeout": float, "proxy": str}`` (proxy omitted
      when empty so httpx uses its default no-proxy transport).
    - ``headers``: Authorization + Content-Type + any extra_headers merged.
    """
    timeout = float(getattr(entity, "request_timeout", 120.0) or 120.0)
    proxy = getattr(entity, "proxy", "") or ""
    extra_headers = getattr(entity, "extra_headers", None) or {}
    api_key = getattr(entity, "api_key", "") or ""

    client_kwargs: dict[str, Any] = {"timeout": timeout}
    if proxy:
        client_kwargs["proxy"] = proxy

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    return client_kwargs, headers


async def fetch_models(entity: Any) -> dict[str, Any]:
    """Pull the upstream ``/v1/models`` catalog and normalize to LlmModel list.

    GET ``{base_url}/models`` with the entity's connection config (same proxy /
    timeout / auth as ``test_provider``). The OpenAI-compatible response is
    ``{"data": [{"id": "gpt-4o", ...}, ...]}``; each ``id`` becomes a
    ``LlmModel`` entry with capability metadata defaulted (the /models
    endpoint rarely reports vision/function-calling support, so we default
    conservatively and let the user toggle them in the UI). The first entry
    is marked ``is_default=True`` so the catalog is immediately usable.

    Returns a structured result (never raises):

    - ``ok``: True iff the upstream returned 200 with a parseable ``data`` list.
    - ``models``: list of LlmModel-shaped dicts (empty on failure).
    - ``error``: short failure reason (empty on success).
    - ``status_code``: HTTP status (None if the request never reached the
      server).

    Dedup by ``model_id`` (some providers list the same id under multiple
    ``owned_by``) and sort for stable UI ordering.
    """
    base_url = (getattr(entity, "base_url", "") or "").rstrip("/")
    api_key = getattr(entity, "api_key", "") or ""
    if not base_url:
        return {"ok": False, "models": [], "error": "base_url 未配置", "status_code": None}
    if not api_key:
        return {"ok": False, "models": [], "error": "api_key 未配置", "status_code": None}

    url = f"{base_url}/models"
    client_kwargs, headers = _connection_kwargs(entity)

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(url, headers=headers)
    except httpx.TimeoutException:
        return {"ok": False, "models": [], "error": "请求超时", "status_code": None}
    except httpx.ConnectError as exc:
        return {"ok": False, "models": [], "error": f"连接失败：{exc}", "status_code": None}
    except httpx.HTTPError as exc:
        return {"ok": False, "models": [], "error": f"HTTP 错误：{exc}", "status_code": None}

    if resp.status_code != 200:
        snippet = (resp.text or "")[:300]
        return {
            "ok": False,
            "models": [],
            "error": f"上游返回 {resp.status_code}：{snippet}",
            "status_code": resp.status_code,
        }

    try:
        data = resp.json()
    except Exception:
        return {
            "ok": False,
            "models": [],
            "error": "响应非 JSON（可能 base_url 指向了非 OpenAI 兼容端点）",
            "status_code": resp.status_code,
        }

    # OpenAI shape: {"data": [{"id": "...", "owned_by": "..."}, ...]}.
    # Tolerate a bare list (some proxies strip the wrapper) and a top-level
    # dict-of-models, but the canonical form is data list.
    raw_items: list = []
    if isinstance(data, dict):
        raw_items = data.get("data") or []
    elif isinstance(data, list):
        raw_items = data
    if not isinstance(raw_items, list):
        return {
            "ok": False,
            "models": [],
            "error": "响应 data 字段非列表",
            "status_code": resp.status_code,
        }

    seen: set[str] = set()
    models: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        # model_id lives under "id" (OpenAI) — fall back to "model"/"name" for
        # non-conformant upstreams so a useful id is always extracted.
        model_id = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        # Some providers report context_window under "context_length" /
        # "max_context_length" (e.g. DeepSeek, Anthropic-via-proxy). Coerce to
        # int; 0 = unknown (UI shows "—").
        ctx = item.get("context_window")
        if ctx is None:
            ctx = item.get("context_length")
        if ctx is None:
            ctx = item.get("max_context_length")
        try:
            context_window = int(ctx) if ctx is not None else 0
        except (TypeError, ValueError):
            context_window = 0
        models.append(
            {
                "model_id": model_id,
                "display_name": model_id,
                "context_window": context_window,
                # /models rarely reports capability flags — default to the
                # LlmModel defaults (function_calling=True, streaming=True,
                # vision=False) and let the user refine in the UI.
                "supports_function_calling": True,
                "supports_vision": bool(item.get("supports_vision", False)),
                "supports_streaming": True,
                "is_default": False,
            }
        )

    if not models:
        return {
            "ok": False,
            "models": [],
            "error": "上游返回空模型列表",
            "status_code": resp.status_code,
        }

    # Stable alphabetical order (deterministic UI, not provider insertion
    # order) + first entry is the default (single-default invariant).
    models.sort(key=lambda m: m["model_id"])
    models[0]["is_default"] = True
    return {"ok": True, "models": models, "error": "", "status_code": 200}

"""LLM Provider preset catalog (PRD 多模型服务商 · 预设目录).

A curated, static set of well-known LLM provider presets backing
``GET /api/providers/catalog`` (the "预设服务商" picker in ProviderEditor).
Mirrors ``agent_templates._CATALOG``'s design (lowest-risk, always-
deterministic):

  - The catalog is a **module-level tuple constant** — cheap to load, easy to
    extend, no network dependency, so the catalog is usable in air-gapped /
    unconfigured envs and the provider-editor doesn't block on a remote fetch.
  - Each preset carries a ready-to-use ``base_url`` + default connection
    config + a seeded ``models`` list (capability metadata per model), so
    selecting a preset pre-fills the editor with a working starting point.
    The user still supplies the ``api_key`` (the one secret presets can't
    ship) and may refine models via "拉取模型" (``probe.fetch_models``).

Catalog entries intentionally **omit** ``api_key`` / ``is_active`` /
``id`` / timestamps — those are fill-in-at-create-time fields, not preset
identity. A selected preset is a template the editor loads, not a row ready
to INSERT (``crud.create_provider`` assigns id/timestamps/is_active).

Field names are snake_case to match the frontend ``api.ts`` convention
(``LlmProvider`` / ``LlmModel``) and the ``LlmProviderCreatePayload`` shape
so the editor can POST the edited preset directly.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class CatalogModel(BaseModel):
    """One model entry in a preset's seeded ``models`` list.

    Mirrors :class:`~models.llm_provider.LlmModel` (same field names/defaults)
    so the editor can drop the preset's models straight into the catalog
    table without reshaping. ``extra="allow"`` tolerates future fields.
    """

    model_config = ConfigDict(extra="allow")

    model_id: str
    display_name: str = ""
    context_window: int = 0
    supports_function_calling: bool = True
    supports_vision: bool = False
    supports_streaming: bool = True
    is_default: bool = False


class ProviderPreset(BaseModel):
    """A ready-to-load provider preset (PRD 多模型服务商 · 预设).

    Carries the connection identity (slug / name / provider / base_url) +
    default connection-level config + a seeded ``models`` catalog. The editor
    fills ``api_key`` from user input; everything else is a sensible default
    the user can override. ``slug`` is the stable id (``"openai"`` /
    ``"deepseek"`` …) the catalog route keys on.
    """

    model_config = ConfigDict(extra="allow")

    slug: str
    name: str
    provider: str
    base_url: str
    # Default connection-level config (applies to the endpoint).
    api_version: str = ""
    organization: str = ""
    extra_headers: dict[str, Any] | None = None
    request_timeout: float = 120.0
    max_retries: int = 2
    proxy: str = ""
    # Default sampling params for the provider (per-model overrides happen in
    # the editor; these are the provider-wide defaults persisted on create).
    temperature: float = 0.0
    max_tokens: int = 4096
    # Seeded model catalog (capability metadata per model; exactly one
    # is_default). Empty list = preset ships no catalog (user pulls via probe).
    models: list[CatalogModel] = []
    # UI hint: a short note shown under the preset (e.g. "需自备 API Key").
    note: str = ""


# ── Curated catalog ────────────────────────────────────────────────────
# Each tuple: (slug, name, provider, base_url, api_version, organization,
#   extra_headers, request_timeout, max_retries, proxy, temperature,
#   max_tokens, models, note). `models` is a tuple of CatalogModel-shaped
#   tuples (model_id, display_name, context_window, fn_call, vision, stream,
#   is_default). Kept as nested tuples (not Pydantic instances) so the
#   constant is plain data — materialized into models in _catalog_entries().
# Real base_urls + real model ids so selecting a preset + adding a key yields
# a genuinely usable provider, not a stub (same principle as agent_templates).
_CATALOG: tuple[
    tuple[
        str,                      # slug
        str,                      # name
        str,                      # provider
        str,                      # base_url
        str,                      # api_version
        str,                      # organization
        dict[str, Any] | None,    # extra_headers
        float,                    # request_timeout
        int,                      # max_retries
        str,                      # proxy
        float,                    # temperature
        int,                      # max_tokens
        tuple[
            tuple[str, str, int, bool, bool, bool, bool],  # model entry
            ...,
        ],                        # models
        str,                      # note
    ],
    ...,
] = (
    (
        "openai", "OpenAI", "openai", "https://api.openai.com/v1",
        "", "", None, 60.0, 2, "", 0.0, 4096,
        (
            ("gpt-4o", "GPT-4o", 128000, True, True, True, True),
            ("gpt-4o-mini", "GPT-4o mini", 128000, True, True, True, False),
            ("gpt-4.1", "GPT-4.1", 1047576, True, False, True, False),
            ("o1-mini", "o1-mini", 128000, True, False, True, False),
        ),
        "需自备 OpenAI API Key（platform.openai.com）",
    ),
    (
        "deepseek", "DeepSeek", "deepseek", "https://api.deepseek.com/v1",
        "", "", None, 120.0, 2, "", 0.0, 4096,
        (
            ("deepseek-chat", "DeepSeek Chat", 64000, True, False, True, True),
            ("deepseek-reasoner", "DeepSeek Reasoner (R1)", 64000, True, False, True, False),
        ),
        "需自备 DeepSeek API Key（platform.deepseek.com）",
    ),
    (
        "anthropic", "Anthropic Claude", "anthropic",
        "https://api.anthropic.com/v1",
        "2023-06-01", "", None, 120.0, 2, "", 0.0, 4096,
        # Anthropic uses api_version (the x-api-key + anthropic-version header
        # scheme); the editor / client adapts. Models use the Messages API.
        (
            ("claude-sonnet-5", "Claude Sonnet 5", 200000, True, True, True, True),
            ("claude-opus-4-8", "Claude Opus 4.8", 200000, True, True, True, False),
            ("claude-haiku-4-5-20251001", "Claude Haiku 4.5", 200000, True, True, True, False),
        ),
        "需自备 Anthropic API Key + api_version；认证用 x-api-key 头（非 Bearer）",
    ),
    (
        "kimi", "Kimi (Moonshot)", "moonshot", "https://api.moonshot.cn/v1",
        "", "", None, 120.0, 2, "", 0.0, 4096,
        (
            ("kimi-k2", "Kimi K2", 131072, True, False, True, True),
            ("moonshot-v1-128k", "Moonshot v1 128k", 131072, True, False, True, False),
            ("moonshot-v1-32k", "Moonshot v1 32k", 32768, True, False, True, False),
        ),
        "需自备 Moonshot API Key（platform.moonshot.cn）",
    ),
    (
        "glm", "智谱 GLM", "zhipu", "https://open.bigmodel.cn/api/paas/v4",
        "", "", None, 120.0, 2, "", 0.0, 4096,
        (
            ("glm-5.1", "GLM-5.1", 128000, True, False, True, True),
            ("glm-4-plus", "GLM-4-Plus", 128000, True, False, True, False),
            ("glm-4v", "GLM-4V (视觉)", 8192, False, True, True, False),
        ),
        "需自备智谱 API Key（open.bigmodel.cn）",
    ),
    (
        "qwen", "通义千问 Qwen", "dashscope",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "", "", None, 120.0, 2, "", 0.0, 4096,
        (
            ("qwen-max", "Qwen Max", 32768, True, False, True, True),
            ("qwen-plus", "Qwen Plus", 131072, True, False, True, False),
            ("qwen-vl-max", "Qwen VL Max (视觉)", 32768, False, True, True, False),
        ),
        "需自备阿里云 DashScope API Key；OpenAI 兼容模式",
    ),
    (
        "ollama", "Ollama (本地)", "ollama", "http://localhost:11434/v1",
        "", "", None, 300.0, 1, "", 0.0, 8192,
        # Ollama is local — no api_key needed (any non-empty placeholder works
        # since Ollama ignores the bearer token). Longer timeout (300s) for
        # local model loads; fewer retries (1) since local failures are fatal.
        (
            ("llama3.3", "Llama 3.3", 131072, False, False, True, True),
            ("qwen2.5", "Qwen 2.5", 131072, False, False, True, False),
            ("deepseek-r1", "DeepSeek R1 (本地)", 65536, False, False, True, False),
        ),
        "本地部署，无需 API Key（建议先 ollama pull 拉取模型）",
    ),
)


def _catalog_entries() -> list[ProviderPreset]:
    """Materialize the static catalog into ProviderPreset objects."""
    out: list[ProviderPreset] = []
    for (
        slug, name, provider, base_url,
        api_version, organization, extra_headers,
        request_timeout, max_retries, proxy,
        temperature, max_tokens, models, note,
    ) in _CATALOG:
        catalog_models = [
            CatalogModel(
                model_id=mid,
                display_name=disp,
                context_window=ctx,
                supports_function_calling=fn,
                supports_vision=vis,
                supports_streaming=stream,
                is_default=default,
            )
            for (mid, disp, ctx, fn, vis, stream, default) in models
        ]
        out.append(
            ProviderPreset(
                slug=slug,
                name=name,
                provider=provider,
                base_url=base_url,
                api_version=api_version,
                organization=organization,
                extra_headers=extra_headers,
                request_timeout=request_timeout,
                max_retries=max_retries,
                proxy=proxy,
                temperature=temperature,
                max_tokens=max_tokens,
                models=catalog_models,
                note=note,
            )
        )
    return out


# In-memory index for O(1) lookup by slug (catalog route resolves slug → preset).
_CATALOG_INDEX: dict[str, ProviderPreset] = {p.slug: p for p in _catalog_entries()}


def list_catalog() -> list[dict[str, Any]]:
    """List all provider presets as plain dicts (GET /api/providers/catalog).

    Returns each preset's serializable shape (slug / name / provider /
    base_url / connection-level config / models catalog / note) in catalog
    declaration order (stable for the UI picker). Plain dicts (not Pydantic
    instances) so the FastAPI route can ``JSONResponse`` them directly without
    an extra ``model_dump`` hop, and the frontend receives the exact
    ``LlmProvider``/``LlmModel``-aligned snake_case shape it expects.
    """
    return [p.model_dump() for p in _CATALOG_INDEX.values()]


def get_catalog(slug: str) -> dict[str, Any] | None:
    """Resolve a single preset by slug (GET /api/providers/catalog/{slug}).

    Returns the preset's serializable dict, or ``None`` if the slug is unknown
    (the caller maps that to a 404). Used by the provider editor's preset
    picker — selecting a slug loads the full preset (base_url / connection
    config / seeded models) into the form.
    """
    preset = _CATALOG_INDEX.get(slug)
    return preset.model_dump() if preset else None


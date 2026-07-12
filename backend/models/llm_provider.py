"""LlmProvider + LlmProviderCreatePayload Pydantic models.

A provider is a configured LLM service endpoint (OpenAI / DeepSeek / Kimi /
GLM …). Multiple can be configured; exactly one is ``is_active`` at a time.
The active provider's config is cached in ``config._ACTIVE_CACHE`` so the
sync ``get_config()`` call path never blocks on the async DB.

Output masking: the ``api_key`` field on the output ``LlmProvider`` model
carries a MASKED preview (first 3 + last 3 chars), never the raw secret. The
crud mapper (``_provider_to_model``) applies ``config._mask_key`` before
building the model, and sets ``has_key`` so the UI can show configured status
without exposing the key.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class LlmModel(BaseModel):
    """One model entry in a provider's model catalog.

    A provider owns multiple models; each carries capability metadata so the
    UI / engine can decide which model fits a task (vision / function calling /
    streaming / context window). Exactly one model per provider is ``is_default``
    (the single-active invariant for model selection — enforced on write by
    ``crud.update_provider`` / ``create_provider``). ``model_id`` is the value
    sent to the upstream ``/chat/completions`` ``model`` field; ``display_name``
    is the human label shown in the UI.
    """

    model_config = ConfigDict(extra="allow")

    model_id: str
    display_name: str = ""
    context_window: int = 0
    supports_function_calling: bool = True
    supports_vision: bool = False
    supports_streaming: bool = True
    is_default: bool = False


class LlmProvider(BaseModel):
    """Output model — always masked (api_key is a preview, not the raw key).

    Multi-model catalog: ``models`` is the provider's list of ``LlmModel``
    entries (capability metadata per model). Connection-level fields
    (``api_version``/``organization``/``extra_headers``/``request_timeout``/
    ``max_retries``/``proxy``) describe how to reach the upstream endpoint —
    shared by every model under this provider. The legacy flat ``model`` field
    is retained for backward-compat (the active model is resolved from
    ``models`` first, falling back to ``model``; see ``crud._select_model``).
    """

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    provider: str = "openai"
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    has_key: bool = False
    temperature: float = 0.0
    max_tokens: int = 4096
    is_active: bool = False
    created_at: str = ""
    updated_at: str = ""
    # Multi-model catalog (provider owns N models, one is_default).
    models: list[LlmModel] = []
    # Connection-level config (applies to the endpoint, shared by all models).
    api_version: str = ""
    organization: str = ""
    extra_headers: dict[str, Any] | None = None
    request_timeout: float = 120.0
    max_retries: int = 2
    proxy: str = ""


class LlmProviderCreatePayload(BaseModel):
    """Create/update payload. ``api_key`` is optional — when omitted/None the
    existing key is left unchanged on update (empty string also means "leave
    unchanged" so editing other fields doesn't wipe the stored key).

    Multi-model catalog + connection-level fields mirror :class:`LlmProvider`.
    All optional (``None`` = leave unchanged on update); on create they fall
    back to defaults. ``models=None`` means "don't touch the catalog" while
    ``models=[]`` means "clear it" — crud distinguishes the two.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    provider: str | None = "openai"
    model: str | None = ""
    base_url: str | None = ""
    api_key: str | None = None
    temperature: float | None = 0.0
    max_tokens: int | None = 4096
    is_active: bool | None = False
    # Multi-model catalog (provider owns N models, one is_default).
    models: list[LlmModel] | None = None
    # Connection-level config (applies to the endpoint, shared by all models).
    api_version: str | None = ""
    organization: str | None = ""
    extra_headers: dict[str, Any] | None = None
    request_timeout: float | None = 120.0
    max_retries: int | None = 2
    proxy: str | None = ""

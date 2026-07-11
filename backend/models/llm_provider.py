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

from pydantic import BaseModel, ConfigDict


class LlmProvider(BaseModel):
    """Output model — always masked (api_key is a preview, not the raw key)."""

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


class LlmProviderCreatePayload(BaseModel):
    """Create/update payload. ``api_key`` is optional — when omitted/None the
    existing key is left unchanged on update (empty string also means "leave
    unchanged" so editing other fields doesn't wipe the stored key)."""

    model_config = ConfigDict(extra="allow")

    name: str
    provider: str | None = "openai"
    model: str | None = ""
    base_url: str | None = ""
    api_key: str | None = None
    temperature: float | None = 0.0
    max_tokens: int | None = 4096
    is_active: bool | None = False

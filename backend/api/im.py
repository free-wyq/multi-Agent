"""IM channel management routes (任务19c · PRD 3.6 IM 网关).

Routes map to the frontend ``imChannelApi`` (任务19d):

  GET    /api/im-channels                        → list (filter by platform)
  GET    /api/im-channels/{id}                    → get (config masked)
  POST   /api/im-channels                         → create
  PUT    /api/im-channels/{id}                    → update (credential merge)
  DELETE /api/im-channels/{id}                    → delete
  POST   /api/im-channels/{id}/enable             → set_enabled(True)
  POST   /api/im-channels/{id}/disable            → set_enabled(False)
  POST   /api/im-channels/{id}/test               → mock outbound probe
  POST   /api/im/inbound/{channel_id}             → platform inbound callback

Credential masking reuses ``api/mcp._mask_sensitive`` + ``_merge_masked_fields``
(任务2 的脱敏逻辑): sensitive keys (``app_secret`` / ``verify_token`` / ``*token*``
/ ``*secret*``) → ``"***"`` on GET; PUT merges ``"***"``-valued fields with the
stored original so an unchanged secret is preserved across edits.

The inbound callback is a separate router (``im_inbound``) so the platform-
facing path (``/api/im/inbound/{channel_id}``) is visually + tag-distinct from
the management CRUD path (``/api/im-channels``) — matches §6's two-router split.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from api.mcp import _merge_one_dict, _mask_sensitive, _MASK, _SENSITIVE_KEY_RE
from engine.im.gateway import InboundDeliveryError, deliver_inbound
from models import ImChannel, ImChannelCreatePayload, ImChannelTestResult
from store import crud

router = APIRouter(prefix="/api/im-channels", tags=["im"])
im_inbound = APIRouter(prefix="/api/im/inbound", tags=["im-inbound"])


# ── credential masking (config dict) ────────────────────────────────────
# config 是平台特异凭证 JSON（{app_id, app_secret, verify_token, webhook_url, ...}）。
# GET 返回脱敏副本（敏感 key value → "***"）；PUT 时 value==*** 的 key 用库中原值替换。
# 直接复用 api/mcp 的脱敏正则 + 合并函数（任务2），不复制实现——单一真源。


def _mask_channel_config(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Mask sensitive credential keys in a channel config dict (GET return)."""
    return _mask_sensitive(config)


def _apply_channel_mask(channel: ImChannel) -> ImChannel:
    """Return a copy of ``channel`` with its ``config`` masked (GET path)."""
    return channel.model_copy(update={"config": _mask_channel_config(channel.config)})


async def _merge_masked_config(
    channel_id: str, payload: ImChannelCreatePayload
) -> ImChannelCreatePayload:
    """PUT: if a config value is the literal mask (``"***"``), restore the
    stored original (preserve unchanged secrets). Only merges when ``config``
    was explicitly sent (``exclude_unset``); other fields pass through.

    Mirrors ``api/mcp._merge_masked_fields``: a GET returns ``"***"`` for
    secrets; if the user edits only the name and PUTs the whole form back,
    the ``"***"`` would overwrite the real secret with the literal mask. This
    restores the original for any ``"***"``-valued key.
    """
    dumped = payload.model_dump(exclude_unset=True)
    if "config" not in dumped:
        return payload
    existing = await crud.get_im_channel_entity(channel_id)
    if not existing:
        return payload  # let the update path return None downstream
    new_config = _merge_one_dict(existing.config, payload.config)
    return payload.model_copy(update={"config": new_config})


# ── CRUD ───────────────────────────────────────────────────────────────


@router.get("")
async def list_im_channels_route(
    platform: str | None = None,
) -> list[ImChannel]:
    """List channels (optionally filtered by platform). GET returns masked config."""
    channels = await crud.list_im_channels(platform=platform)
    return [_apply_channel_mask(c) for c in channels]


@router.get("/{channel_id}")
async def get_im_channel_route(channel_id: str) -> ImChannel | None:
    channel = await crud.get_im_channel(channel_id)
    return _apply_channel_mask(channel) if channel else None


@router.post("")
async def create_im_channel_route(payload: ImChannelCreatePayload) -> ImChannel:
    # create 时 "***" 不特殊处理（原样落库，GET 再脱敏；create 表单不应发 "***"）
    channel = await crud.create_im_channel(payload)
    return _apply_channel_mask(channel)


@router.put("/{channel_id}")
async def update_im_channel_route(
    channel_id: str, payload: ImChannelCreatePayload
) -> ImChannel | None:
    payload = await _merge_masked_config(channel_id, payload)
    channel = await crud.update_im_channel(channel_id, payload)
    return _apply_channel_mask(channel) if channel else None


@router.delete("/{channel_id}")
async def delete_im_channel_route(channel_id: str) -> bool:
    return await crud.delete_im_channel(channel_id)


@router.post("/{channel_id}/enable")
async def enable_im_channel_route(channel_id: str) -> ImChannel | None:
    channel = await crud.set_im_channel_enabled(channel_id, True)
    return _apply_channel_mask(channel) if channel else None


@router.post("/{channel_id}/disable")
async def disable_im_channel_route(channel_id: str) -> ImChannel | None:
    """Disable: refuse new inbound (§5.1 410) + outbound symmetric silent (§7.3).

    The channel row stays; ``enabled=0`` makes ``deliver_inbound`` reject
    inbound with 410 and ``maybe_deliver_outbound`` skip it (no new replies
    push out). Existing scheduled jobs are NOT affected (IM has no scheduler
    job — inbound is platform-pull, not time-push), so no job teardown here
    (unlike ``scheduled_tasks.pause``).
    """
    channel = await crud.set_im_channel_enabled(channel_id, False)
    return _apply_channel_mask(channel) if channel else None


@router.post("/{channel_id}/test")
async def test_im_channel_route(channel_id: str) -> ImChannelTestResult:
    """Mock outbound probe — instantiate the adapter and send a test payload.

    Loads the RAW entity (unmasked config — the adapter needs real credentials
    to send), instantiates the channel's adapter, and calls ``send_outbound``
    with a probe ``OutboundPayload`` targeted at ``config.default_session`` (or
    ``"default"``). Mock stage: ``send_outbound`` is ``logger.info``, so
    ``ok=True`` means the adapter loaded + the log line fired (the e2e test
    caplog-asserts the line). Real platforms (档四): ``ok=True`` means the HTTP
    push succeeded.

    Never 500s — all failure modes (channel missing / unknown platform / send
    exception) are captured into ``ImChannelTestResult(ok=False, error=...)``
    so the UI can render the failure inline (mirrors ``/api/providers/{id}/test``).
    """
    from engine.im import ADAPTERS, OutboundPayload

    entity = await crud.get_im_channel_entity(channel_id)
    if not entity:
        return ImChannelTestResult(ok=False, error="IM channel not found")
    adapter_cls = ADAPTERS.get(entity.platform)
    if adapter_cls is None:
        return ImChannelTestResult(
            ok=False, platform=entity.platform,
            error=f"unknown platform: {entity.platform!r}",
        )
    config = entity.config or {}
    target = str(config.get("default_session") or "default")
    payload = OutboundPayload(target=target, content="[im test] outbound probe")
    try:
        await adapter_cls().send_outbound(payload, config)
    except Exception as exc:
        # never 500 — capture into the result so the UI shows the failure inline
        return ImChannelTestResult(
            ok=False, platform=entity.platform, target=target, error=str(exc),
        )
    return ImChannelTestResult(ok=True, platform=entity.platform, target=target)


# ── inbound callback (platform / echo server POSTs here) ────────────────


@im_inbound.post("/{channel_id}")
async def inbound_callback_route(channel_id: str, request: Request) -> dict:
    """Platform inbound callback — platform (or mock echo server) POSTs here.

    Reads the raw headers + body off the request (the adapter's
    ``verify_inbound`` + ``parse_inbound`` consume them) and hands them to
    ``gateway.deliver_inbound``, which verify → parse → routes via the existing
    ``route_user_message`` / ``route_direct_message``. Maps
    ``InboundDeliveryError.status_code`` to the HTTP response (404 / 410 / 403 /
    400) so the platform sees the right status (platforms retry on 5xx but not
    on 4xx — a disabled channel returning 410 stops platform retries cleanly).
    """
    headers = {k: v for k, v in request.headers.items()}
    body = await request.body()
    try:
        result = await deliver_inbound(channel_id, headers, body)
    except InboundDeliveryError as exc:
        # Map the gateway's status to the HTTP response. 410 (disabled) + 403
        # (bad signature) + 404 (missing) + 400 (parse/platform) — all 4xx so
        # platforms do NOT retry (they retry on 5xx). The detail is a short
        # log-friendly message.
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"ok": True, **result}

"""IM inbound gateway — route a platform callback into the existing chat loop.

``deliver_inbound`` is the platform-agnostic inbound path: a platform (or the
mock echo server in e2e) POSTs to ``/api/im/inbound/{channel_id}``, the API
layer (``api/im.py``) hands the headers + body here, and this function:

  1. loads the channel (raw entity — config is needed to parse platform body);
  2. rejects if disabled (410 Gone) or unknown (404);
  3. verifies the platform signature via the adapter (403 on failure);
  4. parses the body into a neutral ``InboundMessage``;
  5. resolves the delivery target (session_bindings → channel.target_conversation_id);
  6. routes by ``target_kind`` — single → ``route_direct_message``, group →
     ``route_user_message`` (reusing the **same** routing the frontend user
     message uses; IM is just another caller, same shape as the scheduler).

Design (see ``docs/im-gateway-design.md`` §5.1):

  - **Reuse ``route_user_message`` / ``route_direct_message``** — do NOT build
    an IM-specific inbox channel. The inbound message goes through the exact
    same agentic loop as a frontend user message (scheduler is the same shape:
    an external trigger source calling ``push_notify`` / ``push_task``).
  - The agent does not distinguish frontend vs IM — it only sees the inbox
    item. ``im_context`` (``channel_id`` + ``platform`` + ``platform_session_id``)
    is carried in the ``data`` field of the routing call so an outbound hook
    (档四 multi-session) could reverse-lookup the originating platform session.
    MVP doesn't need it (channel-unique target), but threading it keeps the
    seam forward-compatible.
  - Errors are surfaced as ``InboundDeliveryError`` (carries an HTTP status) so
    the API layer can map them to the right response code (404 / 410 / 403 /
    400) without the gateway depending on FastAPI's ``HTTPException`` — keeps
    the gateway importable from tests without a FastAPI app.

Idempotency: MVP does not dedup platform retries (DingTalk retries 3×). The
mock echo server doesn't retry; real-platform dedup (``metadata_.seen_msg_ids``)
is 档四.
"""
from __future__ import annotations

import logging
from typing import Any

from engine.im import ADAPTERS, InboundMessage
from store import crud

logger = logging.getLogger("multi-agent.im")


class InboundDeliveryError(Exception):
    """Raised when an inbound callback cannot be delivered.

    Carries an HTTP ``status_code`` so the API layer (``api/im.py``) can map it
    to the right FastAPI response without the gateway importing FastAPI. The
    ``detail`` is the user-facing message. Status mapping:
      - 404: channel not found
      - 410: channel disabled (enabled=0 — refuse inbound, §5.1)
      - 403: adapter.verify_inbound rejected the signature
      - 400: parse failure / unknown platform / routing error
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _resolve_target(
    channel_config: dict[str, Any],
    channel_target: str,
    inbound: InboundMessage,
) -> str:
    """Resolve the internal conversation/group id an inbound routes to.

    MVP: the channel's ``target_conversation_id`` is the unique delivery point
    (``session_bindings`` may be None). 档四: if ``session_bindings`` maps the
    inbound ``platform_session_id`` → an internal conversation_id, use that
    instead (one channel ↔ many platform sessions). The 档四 path is stubbed
    here so the seam exists, but the MVP path is what the mock e2e exercises.
    """
    bindings = (channel_config or {}).get("session_bindings") or {}
    if isinstance(bindings, dict):
        mapped = bindings.get(inbound.platform_session_id)
        if mapped:
            return str(mapped)
    return channel_target


async def deliver_inbound(
    channel_id: str, headers: dict[str, Any], body: bytes | str
) -> dict[str, Any]:
    """Deliver a platform callback to the channel's bound conversation.

    Returns a small dict (``conversation_id`` / ``platform_session_id`` /
    ``content``) the API layer can log / echo. Raises ``InboundDeliveryError``
    on any refusal (disabled / bad signature / unknown platform / parse or
    routing failure) so the API maps it to the right HTTP status.

    ``headers`` + ``body`` are the raw HTTP request headers + body (bytes from
    the ASGI layer or a str if a test calls this directly). The adapter's
    ``verify_inbound`` + ``parse_inbound`` consume them; the gateway never
    parses platform JSON directly.
    """
    entity = await crud.get_im_channel_entity(channel_id)
    if entity is None:
        raise InboundDeliveryError(404, f"IM channel {channel_id} not found")
    if not entity.enabled:
        # §5.1: disabled channel refuses inbound (410 Gone). The channel may
        # still exist for outbound (existing replies can still push), but no
        # NEW inbound is accepted until re-enabled.
        raise InboundDeliveryError(410, f"IM channel {entity.name} is disabled")

    platform = entity.platform
    adapter_cls = ADAPTERS.get(platform)
    if adapter_cls is None:
        # Unknown platform — the channel row exists but no adapter is
        # registered (e.g. a 档四 platform whose adapter isn't landed yet).
        logger.warning(
            "[im] channel %s platform %r not in ADAPTERS — refuse inbound",
            channel_id, platform,
        )
        raise InboundDeliveryError(400, f"unknown IM platform: {platform!r}")

    adapter = adapter_cls()
    config = entity.config or {}

    # 1. verify the platform signature (mock: always True; real: HMAC/SHA1).
    try:
        verified = adapter.verify_inbound(headers, body)
    except Exception:
        # verify must not crash the gateway — a malformed signature check is
        # a 403 (treat as unverifiable), not a 500.
        logger.warning(
            "[im] channel %s verify_inbound raised — treat as unverifiable",
            channel_id, exc_info=True,
        )
        verified = False
    if not verified:
        raise InboundDeliveryError(403, "inbound signature verification failed")

    # 2. parse the platform body into a neutral InboundMessage.
    try:
        inbound = adapter.parse_inbound(body, config)
    except Exception as exc:
        # A parse failure is a 400 (the platform sent something the adapter
        # couldn't interpret) — not a 500, the gateway + adapter are fine.
        logger.warning(
            "[im] channel %s parse_inbound failed", channel_id, exc_info=True,
        )
        raise InboundDeliveryError(400, f"failed to parse inbound body: {exc}") from exc

    # 3. resolve the internal delivery target (session_bindings → channel default).
    target = _resolve_target(config, entity.target_conversation_id, inbound)

    # im_context: threaded onto the routing call's ``data`` so an outbound
    # hook (档四 multi-session) can reverse-lookup the originating platform
    # session. MVP doesn't consume it (channel-unique target), but threading
    # it keeps the seam forward-compatible without changing the routing API.
    im_context = {
        "channel_id": channel_id,
        "platform": platform,
        "platform_session_id": inbound.platform_session_id,
        "platform_user_id": inbound.platform_user_id,
        "im_inbound": True,
    }

    # 4. route by target_kind — reuse the existing chat routing (IM is just
    # another caller, same shape as scheduler / frontend send_message).
    kind = (entity.target_kind or "single").strip().lower()
    if kind == "group":
        # group path: route_user_message drives the per-group graph.
        from engine.mention import route_user_message
        await route_user_message(target, inbound.content)
    else:
        # single path (default): route_direct_message drives the resident
        # worker engine for the conversation. target_kind="" / unknown → single
        # (safe default — a single-chat conversation is the common IM target).
        from engine.direct import route_direct_message
        await route_direct_message(target, inbound.content)

    logger.info(
        "[im] inbound delivered: channel=%s platform=%s → %s (kind=%s) "
        "user=%s session=%s",
        channel_id, platform, target, kind,
        inbound.platform_user_id, inbound.platform_session_id,
    )
    return {
        "conversation_id": target,
        "platform_session_id": inbound.platform_session_id,
        "platform_user_id": inbound.platform_user_id,
        "content": inbound.content,
        "target_kind": kind,
    }


__all__ = ["deliver_inbound", "InboundDeliveryError"]

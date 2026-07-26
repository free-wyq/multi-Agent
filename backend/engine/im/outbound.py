"""IM outbound hook — the single seam between ``reply.py`` and IM adapters.

``maybe_deliver_outbound`` is the only function ``reply.py`` calls; it does NOT
import any adapter / gateway. The hook is a **pure additive** call at the end of
``persist_agent_reply`` (after persist + emit): for each enabled IM channel
whose ``target_conversation_id`` matches the reply's ``conversation_id``,
instantiate the channel's adapter and call ``send_outbound`` with an
``OutboundPayload`` built from the reply. No channel → no-op (pure-frontend
conversation, zero side effect, behavior degrades to pre-19c).

Design (see ``docs/im-gateway-design.md`` §5.2 方案 C):

  - ``reply.py`` is the single truth for agent_reply (three reply paths
    converge there after B10); hooking here covers every reply (frontend
    chat / scheduled-trigger reply / IM-inbound reply) without touching the
    graph nodes or the registry execute path.
  - ``reply.py`` calls only ``maybe_deliver_outbound(msg)`` — it does not know
    whether IM exists, how many channels there are, or what platform. The
    adapter / channel lookup lives entirely here.
  - Same-process direct call (no WS round-trip); ``await`` but mock
    ``send_outbound`` is a ``logger.info`` so the overhead is negligible.

Outbound target resolution (§5.2):
  - MVP: channel-unique ``target_conversation_id`` — every reply on that
    conversation pushes to the channel's default ``platform_session_id``
    (``config.default_session`` or ``"default"``). One channel = one
    platform session (simple, matches the mock echo e2e).
  - 档四: ``session_bindings[platform_session_id] = conversation_id`` reverse
    lookup so a reply's ``conversation_id`` maps back to the originating
    platform session — one channel ↔ many platform groups/users. NOT done here
    in MVP.

Error isolation: each channel's ``send_outbound`` is wrapped in try/except — a
failure in one channel (or one adapter) does not raise to ``reply.py`` and
break the reply path. Failures are logged at warning (not debug — outbound
delivery failure is observable, not a benign no-op like a missing channel).
"""
from __future__ import annotations

import logging
from typing import Any

from store import crud

logger = logging.getLogger("multi-agent.im")


def _resolve_target(channel: dict[str, Any]) -> str:
    """Resolve the platform-side target for an outbound push (MVP).

    MVP: the channel's default platform session — ``config.default_session`` if
    set, else ``"default"``. The mock ``send_outbound`` logs this verbatim; a
    real adapter (档四) would POST to this session id via the platform API.
    """
    config = channel.get("config") or {}
    default = config.get("default_session") if isinstance(config, dict) else None
    return str(default) if default else "default"


async def maybe_deliver_outbound(msg: dict[str, Any]) -> None:
    """Push an agent reply to every IM channel bound to its conversation.

    Called by ``persist_agent_reply`` after persist + emit. ``msg`` is the
    persisted message dict (``msg.model_dump()`` shape: ``conversation_id``,
    ``content``, ``sender_id``, ``id``, ``type``). Looks up enabled channels
    whose ``target_conversation_id`` matches ``msg["conversation_id"]``; for
    each, instantiates the adapter (``ADAPTERS[channel.platform]``) and calls
    ``send_outbound`` with an ``OutboundPayload``. No channel → no-op short-
    circuit (pure-frontend conversation, the common case, zero DB overhead
    beyond the lookup query).

    Error isolation: per-channel try/except — one failing adapter does NOT
    raise to the caller (``persist_agent_reply``) and break the reply path.
    Failures log at warning (observable) and the loop continues to the next
    channel so a multi-channel setup still delivers to the healthy ones.
    """
    conversation_id = msg.get("conversation_id")
    content = msg.get("content")
    if not conversation_id or content is None:
        # Nothing to route — a reply with no conversation or no content is not
        # an IM delivery target. Short-circuit before the DB query.
        return

    channels = await crud.list_im_channels_for_target(conversation_id)
    if not channels:
        # The common case: a pure-frontend conversation with no IM channel
        # bound. No-op (zero side effect, behavior identical to pre-19c).
        return

    # Lazy import: avoids pulling the adapter package (and its platform
    # modules) at module-import time — keeps ``reply.py``'s import of this
    # function cheap and avoids any chance of a circular import on startup.
    from engine.im import ADAPTERS, OutboundPayload

    for channel in channels:
        platform = channel.platform
        adapter_cls = ADAPTERS.get(platform)
        if adapter_cls is None:
            logger.warning(
                "[im] channel %s platform %r has no adapter (not in ADAPTERS) — skip",
                channel.id, platform,
            )
            continue
        if not channel.outbound_log:
            # outbound_log=0 = mock stage still sends but doesn't log; in
            # mock stage the send IS the log, so outbound_log=0 = skip send
            # entirely (no other delivery mechanism in mock). 档四真平台时
            # outbound_log 语义变为「是否记出站审计日志」（发送照常，仅日志开关）.
            continue
        target = _resolve_target(channel.model_dump())
        payload = OutboundPayload(target=target, content=content)
        try:
            await adapter_cls().send_outbound(payload, channel.config or {})
        except Exception:
            # Per-channel isolation: a failing adapter must not break the
            # reply path or starve other channels. Log at warning (not debug —
            # an outbound failure is observable, unlike the no-channel no-op).
            logger.warning(
                "[im] channel %s send_outbound failed for reply %s",
                channel.id, msg.get("id"), exc_info=True,
            )


__all__ = ["maybe_deliver_outbound"]

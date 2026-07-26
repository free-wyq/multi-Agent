"""ImChannelAdapter protocol + the platform-agnostic inbound/outbound DTOs.

This is the **single boundary** between platform-specific IM protocols (DingTalk
outgoing / Feishu event subscription / WeChat Work callback) and the
platform-agnostic gateway (任务19c). The gateway only consumes the three methods
defined here; it never parses platform JSON directly. Adding a new platform =
adding one adapter implementing this protocol + registering it in ``ADAPTERS``
(see ``adapters/__init__.py``) — gateway code stays untouched.

Design (see ``docs/im-gateway-design.md`` §3):
- ``InboundMessage`` / ``OutboundPayload`` are platform-neutral dataclasses the
  gateway consumes; adapters translate platform shapes ↔ these.
- ``ImChannelAdapter`` is a ``typing.Protocol`` (structural, not nominal) — an
  adapter is recognized by having the three methods + the ``platform`` attr, no
  inheritance needed. This keeps the protocol importable without pulling the
  adapter implementations (gateway imports only ``base``).
- ``verify_inbound`` + ``parse_inbound`` are sync (pure CPU: HMAC, JSON parse);
  ``send_outbound`` is async (HTTP push, or the mock ``logger.info``).

Mock stage (任务19b): the three adapters live in ``wechat.py`` / ``dingtalk.py``
/ ``feishu.py``. Their ``verify_inbound`` returns ``True`` (echo server doesn't
sign) and ``send_outbound`` is ``logger.info`` (no real HTTP). Real algorithms +
httpx push are 档四.

This module deliberately holds ONLY the protocol + DTOs + shared decode helpers
+ the mock scaffolding base — NOT the adapter implementations and NOT the
``ADAPTERS`` registry. That keeps the protocol (stable across mock→real upgrade)
decoupled from the per-platform mock files (each replaced wholesale in 档四).
The registry is assembled in ``adapters/__init__.py`` from the three platform
modules, avoiding a base→platforms import cycle.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("multi-agent.im")


@dataclass
class InboundMessage:
    """Platform-neutral inbound message (adapter output, gateway input).

    Each adapter's ``parse_inbound`` translates the platform's raw callback
    (DingTalk JSON body / Feishu event JSON / WeChat Work XML) into this shape —
    the gateway then routes on ``platform_session_id`` without caring about
    platform protocol details.
    """

    platform_user_id: str  # platform-side user id (openid / user_id / userid)
    platform_session_id: str  # platform-side session (group chat_id / single from_user)
    content: str  # message body (with the @bot prefix stripped)
    msg_type: str = "text"  # text | image | event (MVP only handles text)
    raw: dict[str, Any] | None = None  # original callback body (debug/audit, not routing)


@dataclass
class OutboundPayload:
    """Platform-neutral outbound message (gateway output, adapter input).

    The reply content + the platform target to push to. ``target`` corresponds
    to the inbound ``platform_session_id`` so the reply lands back in the same
    platform conversation the inbound came from.
    """

    target: str  # platform-side session id (mirrors inbound platform_session_id)
    content: str  # reply body
    reply_to: str | None = None  # optional: original platform msg id being replied to


@runtime_checkable
class ImChannelAdapter(Protocol):
    """Per-platform adapter: platform-specific auth + inbound parse + outbound send.

    The three methods are the **platform-specific** boundary — the gateway calls
    only these three and never touches platform protocol directly. Adding a new
    platform = adding one adapter implementing this protocol + registering it in
    ``ADAPTERS``; gateway code is unchanged.
    """

    platform: str  # "wechat" | "dingtalk" | "feishu" — aligns with ImChannelEntity.platform

    def verify_inbound(self, headers: dict[str, Any], body: bytes | str) -> bool:
        """Verify an inbound request is genuinely from this platform (signature).

        DingTalk: timestamp+sign HMAC-SHA256 (app_secret).
        Feishu: encrypt_key decrypt + challenge echo.
        WeChat Work: msg_signature SHA1(token+timestamp+nonce).
        MVP mock: always True (echo server doesn't sign); real platforms fill
        the real algorithm in 档四. Return False → gateway rejects (403).
        """

    def parse_inbound(
        self, body: bytes | str, channel_config: dict[str, Any]
    ) -> InboundMessage:
        """Parse the platform's raw callback body into a neutral InboundMessage.

        ``channel_config`` is the channel's credentials (app_id / app_secret
        etc.) used to decrypt / parse the platform protocol (Feishu events need
        decryption). Extracts platform_user_id / platform_session_id / content,
        strips the @bot prefix.
        """

    async def send_outbound(
        self, payload: OutboundPayload, channel_config: dict[str, Any]
    ) -> None:
        """Push a reply to the platform (outbound).

        DingTalk: POST robot webhook (outgoing token).
        Feishu: POST /im/v1/messages (tenant_access_token).
        WeChat Work: POST /cgi-bin/message/send.
        MVP mock: ``logger.info("[im:%s] outbound → %s: %s", platform, target,
        content)`` — no real HTTP (任务19e asserts this log). Real platforms 档四.
        """


# ── helpers shared by the mock adapters ─────────────────────────────────


def _decode_body(body: bytes | str) -> Any:
    """Decode an inbound body to a JSON object, tolerating bytes/str.

    Adapters receive the raw HTTP body — bytes from the ASGI layer or a str if
    a test calls the gateway function directly. Mock callbacks are JSON; real
    platforms vary (WeChat Work uses XML, handled in 档四), so this helper only
    covers the JSON path used by the mock + DingTalk/Feishu JSON callbacks.
    Returns the parsed object, or an empty dict on decode failure (the adapter
    then surfaces a clear "empty body" InboundMessage rather than crashing — the
    gateway's verify step is the gate, not the parser).
    """
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", errors="replace")
    if not body:
        return {}
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        # Non-JSON body (real WeChat Work XML lands here in 档四; mock is always
        # JSON). Return {} so attribute access in the adapter falls back to
        # defaults rather than raising.
        return {}


def _as_dict(value: Any) -> dict[str, Any]:
    """Coerce a parsed JSON value to a dict (top-level arrays/scalars → {})."""
    return value if isinstance(value, dict) else {}


class _BaseMockAdapter:
    """Shared scaffolding for the three mock adapters (任务19b).

    Mock adapters all (a) accept inbound unconditionally (``verify_inbound``
    True) and (b) push outbound via ``logger.info``. This base holds the shared
    outbound log line, so each platform adapter only declares its ``platform``
    class attr + its ``parse_inbound`` field extraction.

    Deliberately a plain class (not ``@dataclass``): the adapters are stateless
    — no per-instance fields — so dataclass machinery would only add a default
    ``__init__(platform="")`` that shadows a subclass's ``platform = "wechat"``
    class attribute (the dataclass-generated ``__init__`` overwrites the class
    attr with the parent's default at instantiation). A plain class lets each
    adapter's ``platform`` class attr win directly. Real-platform adapters (档四)
    will NOT inherit this — they'll implement the protocol directly with real
    HTTP/crypto, so the mock coupling stays here and a 档四 upgrade is a clean
    per-file swap of the adapter class body.
    """

    platform: str = ""

    def verify_inbound(self, headers: dict[str, Any], body: bytes | str) -> bool:
        # Mock: echo server doesn't sign callbacks, so accept everything. Real
        # platforms verify HMAC/SHA1 signatures here (档四). The gateway still
        # calls this before parse — so flipping to real verification is a one-
        # method change with no gateway impact.
        return True

    async def send_outbound(
        self, payload: OutboundPayload, channel_config: dict[str, Any]
    ) -> None:
        # Mock: log instead of HTTP. This logger.info line is the e2e assertion
        # point (任务19e caplog intercepts "[im:{platform}] outbound → ...").
        # channel_config is unused in mock (no real endpoint to POST to); real
        # adapters read webhook_url / access_token from it (档四).
        logger.info(
            "[im:%s] outbound → %s: %s",
            self.platform,
            payload.target,
            payload.content,
        )


__all__ = [
    "ImChannelAdapter",
    "InboundMessage",
    "OutboundPayload",
    "_BaseMockAdapter",
    "_decode_body",
    "_as_dict",
]

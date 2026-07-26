"""Feishu (飞书) event-subscription adapter — 任务19b mock.

Real platform (档四): encrypt_key decrypt + challenge echo verify, event JSON
with event.message.chat_id + event.message.content, POST /im/v1/messages
outbound. Mock: verify True, logger.info outbound.
"""
from __future__ import annotations

import json
from typing import Any

from .base import InboundMessage, _BaseMockAdapter, _as_dict, _decode_body


class FeishuAdapter(_BaseMockAdapter):
    """Feishu mock adapter."""

    platform: str = "feishu"

    def parse_inbound(
        self, body: bytes | str, channel_config: dict[str, Any]
    ) -> InboundMessage:
        data = _as_dict(_decode_body(body))
        # Feishu event subscription: {event:{message:{chat_id, content,
        # message_type, ...}, sender:{sender_id:{open_id}}}}. content is a JSON
        # string like '{"text":"hi"}' — the real adapter parses it; mock takes
        # it verbatim + falls back to a top-level content/text for the echo
        # server's convenience.
        event = data.get("event") if isinstance(data.get("event"), dict) else {}
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
        chat_id = str(message.get("chat_id") or "")
        content_raw = message.get("content")
        # Feishu content is a JSON string {"text":"..."}; fall back to top-level
        # text/content for the mock echo server (which may send a flat shape).
        content = ""
        if isinstance(content_raw, str) and content_raw:
            try:
                parsed = json.loads(content_raw)
                if isinstance(parsed, dict):
                    content = str(parsed.get("text") or parsed.get("content") or "")
            except json.JSONDecodeError:
                content = content_raw
        if not content:
            content = str(
                data.get("text")
                or data.get("content")
                or message.get("content")
                or ""
            )
        return InboundMessage(
            platform_user_id=str(
                sender_id.get("open_id")
                or data.get("platform_user_id")
                or ""
            ),
            platform_session_id=str(
                chat_id
                or data.get("platform_session_id")
                or sender_id.get("open_id")
                or ""
            ),
            content=content,
            msg_type=str(message.get("message_type") or data.get("msg_type") or "text"),
            raw=data,
        )

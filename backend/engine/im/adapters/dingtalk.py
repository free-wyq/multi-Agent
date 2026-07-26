"""DingTalk (钉钉) robot outgoing adapter — 任务19b mock.

Real platform (档四): timestamp+sign HMAC-SHA256 verify, JSON body with
senderId/senderNick/text.content, POST robot webhook outbound. Mock: verify
True, logger.info outbound.
"""
from __future__ import annotations

from typing import Any

from .base import InboundMessage, _BaseMockAdapter, _as_dict, _decode_body


class DingtalkAdapter(_BaseMockAdapter):
    """DingTalk mock adapter."""

    platform: str = "dingtalk"

    def parse_inbound(
        self, body: bytes | str, channel_config: dict[str, Any]
    ) -> InboundMessage:
        data = _as_dict(_decode_body(body))
        # DingTalk outgoing robot: {msgtype:"text", text:{content}, senderId,
        # senderNick, conversationId}. text.content carries "@bot " prefix that
        # the real adapter strips; mock assumes the echo server already stripped it.
        text_obj = data.get("text") if isinstance(data.get("text"), dict) else {}
        content = str(text_obj.get("content") or data.get("content") or "")
        sender = str(data.get("senderId") or data.get("sender_id") or "")
        nick = str(data.get("senderNick") or data.get("sender_nick") or "")
        return InboundMessage(
            platform_user_id=sender or nick,
            platform_session_id=str(
                data.get("conversationId")
                or data.get("conversation_id")
                or data.get("platform_session_id")
                or sender
                or nick
            ),
            content=content,
            msg_type=str(data.get("msgtype") or data.get("msg_type") or "text"),
            raw=data,
        )

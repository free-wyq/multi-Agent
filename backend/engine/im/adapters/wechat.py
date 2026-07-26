"""WeChat Work (企业微信) callback adapter — 任务19b mock.

Real platform (档四): XML body, msg_signature SHA1(token+timestamp+nonce)
verify, POST /cgi-bin/message/send outbound. Mock: JSON body, verify True,
logger.info outbound.

Why a separate file per platform: a 档四 upgrade swaps this one file's class
body (mock → real crypto + httpx) with zero edits elsewhere — the gateway and
protocol stay put, the platform file is self-contained.
"""
from __future__ import annotations

from typing import Any

from .base import InboundMessage, _BaseMockAdapter, _as_dict, _decode_body


class WechatAdapter(_BaseMockAdapter):
    """WeChat Work mock adapter."""

    platform: str = "wechat"

    def parse_inbound(
        self, body: bytes | str, channel_config: dict[str, Any]
    ) -> InboundMessage:
        data = _as_dict(_decode_body(body))
        # WeChat Work text callback: {FromUserName, ToUserName, Content, MsgType}
        user = str(data.get("FromUserName") or data.get("from_user_name") or "")
        content = str(data.get("Content") or data.get("content") or "")
        # platform_session_id = the originating user (single-chat); for群聊 the
        # real callback carries a chat id under a different key — mock keeps
        # FromUserName as the session so a single-chat round-trip resolves.
        return InboundMessage(
            platform_user_id=user,
            platform_session_id=str(data.get("platform_session_id") or user),
            content=content,
            msg_type=str(data.get("MsgType") or data.get("msg_type") or "text"),
            raw=data,
        )

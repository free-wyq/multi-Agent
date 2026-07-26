"""IM gateway package — adapter shells + (任务19c) the gateway + outbound hook.

Adapter层 (任务19b, this package's ``adapters/``): platform-specific薄壳 —
wechat / dingtalk / feishu, each a ``ImChannelAdapter`` implementing verify /
parse / send. Mock-first: ``send_outbound`` is ``logger.info`` (no real HTTP);
real platforms land in 档四.

Gateway层 + API层 + outbound hook land in 任务19c (``gateway.py`` /
``outbound.py``); this package keeps the import surface stable so the gateway
module is the only thing 19c adds.
"""
from __future__ import annotations

from .adapters import (
    ADAPTERS,
    DingtalkAdapter,
    FeishuAdapter,
    ImChannelAdapter,
    InboundMessage,
    OutboundPayload,
    WechatAdapter,
)

__all__ = [
    "ADAPTERS",
    "ImChannelAdapter",
    "InboundMessage",
    "OutboundPayload",
    "WechatAdapter",
    "DingtalkAdapter",
    "FeishuAdapter",
]

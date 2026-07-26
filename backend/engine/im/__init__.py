"""IM gateway package — adapter shells + the gateway + outbound hook.

Adapter层 (任务19b, this package's ``adapters/``): platform-specific薄壳 —
wechat / dingtalk / feishu, each a ``ImChannelAdapter`` implementing verify /
parse / send. Mock-first: ``send_outbound`` is ``logger.info`` (no real HTTP);
real platforms land in 档四.

Gateway层 + API层 + outbound hook (任务19c): ``gateway.deliver_inbound`` is the
platform-agnostic inbound path (verify → parse → route via the existing
``route_user_message`` / ``route_direct_message``); ``outbound.maybe_deliver_outbound``
is the single seam ``persist_agent_reply`` calls after persist+emit (no channel
→ no-op). Both are re-exported here so callers import from ``engine.im`` and
the internal module split (gateway / outbound / adapters) stays free to evolve.
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
from .gateway import InboundDeliveryError, deliver_inbound
from .outbound import maybe_deliver_outbound

__all__ = [
    "ADAPTERS",
    "ImChannelAdapter",
    "InboundMessage",
    "OutboundPayload",
    "WechatAdapter",
    "DingtalkAdapter",
    "FeishuAdapter",
    "deliver_inbound",
    "InboundDeliveryError",
    "maybe_deliver_outbound",
]

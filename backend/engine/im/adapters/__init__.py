"""IM platform adapter implementations (任务19b mock stage).

Each platform gets its own module so a real-platform upgrade (档四) is a
self-contained edit to one file (swap the mock class body for real crypto +
httpx). The shared protocol + DTOs + mock scaffolding live in ``base``.

``ADAPTERS`` is assembled here (not in ``base``) to avoid a base→platforms
import cycle: ``base`` is the stable protocol layer (imported by the gateway +
by each platform module), the platforms import from ``base``, and this package
init imports the platforms + builds the registry.
"""
from __future__ import annotations

from .base import (
    ImChannelAdapter,
    InboundMessage,
    OutboundPayload,
)
from .dingtalk import DingtalkAdapter
from .feishu import FeishuAdapter
from .wechat import WechatAdapter

#: ``platform → adapter_cls``. The gateway (任务19c) looks up by
#: ``channel.platform`` and instantiates. Adding a platform = adding an entry
#: here; the gateway code is unchanged (open/closed via the registry).
ADAPTERS: dict[str, type[ImChannelAdapter]] = {
    "wechat": WechatAdapter,
    "dingtalk": DingtalkAdapter,
    "feishu": FeishuAdapter,
}

__all__ = [
    "ADAPTERS",
    "ImChannelAdapter",
    "InboundMessage",
    "OutboundPayload",
    "WechatAdapter",
    "DingtalkAdapter",
    "FeishuAdapter",
]

# IM 网关设计（IM Gateway Design）

> 单一真源：外部即时通讯平台（微信 / 钉钉 / 飞书）接入的分层模型、ImChannelAdapter 协议、入站/出站数据流、ImChannelEntity schema、与 inbox/reply 落点关系、mock echo e2e 方案。
> 锁定任务 `[任务19a-设计]`。后端实体+适配器见 `[任务19b]`，网关核心+API 见 `[任务19c]`，前端面板见 `[任务19d]`，mock echo e2e 见 `[任务19e]`。
> 参考业界 IM 机器人网关（钉钉 outgoing / 飞书事件订阅 / 企业微信回调）+ 本仓 scheduler/inbox/mcp 既有模式裁剪，不照搬云端多租户 SaaS 形态。

---

## 1. 设计目标与背景缺口

### 1.1 现状（已查证）

引擎层**已有**两条入站路径 + 一条出站真源：

| 机制 | 位置 | 作用 | 入口 |
|---|---|---|---|
| 群聊入站路由 | `engine/mention.py:241 route_user_message` | 群聊用户消息 → group graph（@mention 分叉 decentralized / 无 mention 走 Leader centralized） | `POST /api/messages`（conversation_id 命中 Group 行） |
| 单聊入站路由 | `engine/direct.py:32 route_direct_message` | 单聊用户消息 → resident worker engine（`push_notify` 唤醒） | `POST /api/messages`（conversation_id 不命中 Group 行） |
| 出站回复真源 | `engine/reply.py:49 persist_agent_reply` | 三条 reply 路径（registry 执行态 / coordinator graph / worker graph）的统一落盘 + `emit_message_added` | 各 graph 节点 + registry `_run_worker_task` |
| 入站队列 | `engine/inbox.py:49 push_task` / `:85 push_notify` | per-(group,agent) asyncio.Queue + `_task_queues`/`_notify_queues` 内存真源 | 路由层调 |
| 定时触发器 | `engine/scheduler.py:171 _fire` → `push_task` | 按计划把 prompt 推进 agent inbox（与交互式 dispatch 同一条 loop） | APScheduler job |

**以上全是「系统内部消息源」**——前端用户输入（`/api/messages`）和定时任务（scheduler）。无「外部 IM 平台消息源」入口。

### 1.2 缺口

**无 IM 平台接入**：外部 IM 平台（微信/钉钉/飞书）上的用户消息无法进入系统交给智能体处理；智能体回复也无法回推到原 IM 平台。用户在钉钉群里 @机器人，机器人不会响应；要响应必须在前端 Web UI 里发。这是本模块要解决的——给智能体一个「IM 平台双向通道」。

### 1.3 设计原则

1. **复用现有入站路由，不新造 inbox 通道**：IM 入站消息经网关标准化后，**走 `route_user_message`（群聊）/ `route_direct_message`（单聊）**——与前端用户消息同一条 loop，不另起 IM 专用队列。inbox 是单一真源，IM 只是又一个 `push_notify` 的 caller（与 scheduler 同型）。
2. **出站挂 reply 真源，不侵入 graph**：出站回推挂在 `persist_agent_reply` 落盘后（通过出站分发器查 `im_channels` 表），不在 graph 节点里硬编码 IM 逻辑——graph 节点不知道也不需要知道回复会被推到哪里。
3. **用开源不手搓 / 引擎用框架**（[[use-open-source-not-handrolled]] / [[engines-use-frameworks-not-handrolled]]）：HTTP 入站接收用 FastAPI（已有）；出站 HTTP 调用用 `httpx`（已有，coordinator 流式已用）；平台签名校验用各平台官方算法（不可替换，但封装在 adapter 内不外泄）。**不自研 IM 协议框架**，adapter 是薄壳封装。
4. **mock 先行，真平台后接**：MVP 三 adapter（微信/钉钉/飞书）`send_outbound` 走 `logger.info` mock（不真发 HTTP），e2e 用 aiohttp echo server 模拟平台回调。真平台凭证填充 + 真发 HTTP 留档四（非本轮范围）。
5. **本地单用户**：`user_id` 先固定本地单用户，schema 预留多租户字段但 MVP 不做隔离。
6. **设计先行，避免边写边改**：本文档定稿后再开发（19b 实体+adapter / 19c 网关+API / 19d 前端 / 19e e2e）。

---

## 2. 分层模型

| 层 | 位置 | 作用 | 本轮状态 |
|---|---|---|---|
| **Adapter 层** | `backend/engine/im/adapters/{wechat,dingtalk,feishu}.py` | 每平台一个 `ImChannelAdapter` 实现：入站原始回调解析、出站 HTTP 发送、平台签名/鉴权校验 | ❌ 缺口，19b 补（mock） |
| **Gateway 层** | `backend/engine/im/gateway.py` | 渠道路由：入站投递（标准化 → `route_*`）、出站钩子（reply 落盘后查表分发）、channel 生命周期（enable/disable 启停） | ❌ 缺口，19c 补 |
| **Entity 层** | `backend/store/entities.py:ImChannelEntity` | 渠道配置持久化（平台/凭证/target/启用状态） | ❌ 缺口，19b 补 |
| **API 层** | `backend/api/im.py` | `/api/im-channels` CRUD + enable/disable/test + `/api/im/inbound/{channel_id}` 入站回调端点 | ❌ 缺口，19c 补 |
| **前端层** | `src/components/ImChannelPanel.tsx` | 渠道列表 / 配置 Modal / 启停 / 测试 / mock 日志 | ❌ 缺口，19d 补（替换 `SettingsModal.tsx:297` im 占位） |

**Adapter 与 Gateway 的关系**：
- Adapter 是**平台特异**的薄壳（解析钉钉 outgoing 协议 / 飞书事件订阅 / 企业微信回调的差异）。
- Gateway 是**平台无关**的路由大脑（投递到哪个 agent、出站回推到哪个 channel）。
- Gateway 持有 `ImChannelAdapter` 注册表（`platform → adapter_cls`），按 channel.platform 取 adapter 实例。

---

## 3. ImChannelAdapter 协议

### 3.1 协议定义（`backend/engine/im/adapters/base.py` 新增）

```python
from typing import Protocol, Any
from dataclasses import dataclass


@dataclass
class InboundMessage:
    """平台无关的入站消息标准化结构。

    各 adapter 的 ``parse_inbound`` 把平台原始回调（钉钉的 JSON body / 飞书的
    event JSON / 企业微信的 XML）解析成这个统一结构——gateway 不关心平台
    协议细节，只消费标准化字段。
    """
    platform_user_id: str       # 平台侧用户标识（openid / user_id / userid）
    platform_session_id: str     # 平台侧会话标识（群聊 group_id / 单聊 from_user）
    content: str                 # 消息正文（已脱平台 @ 机器人前缀）
    msg_type: str = "text"      # text | image | event（MVP 只处理 text）
    raw: dict | None = None     # 原始回调体（调试 / 审计用，不参与路由）


@dataclass
class OutboundPayload:
    """平台无关的出站消息结构。reply 内容 + 投递目标。"""
    target: str                  # 平台侧会话标识（与入站 platform_session_id 对应）
    content: str                 # 回复正文
    reply_to: str | None = None  # 可选：被回复的原平台消息 id（平台支持时用）


class ImChannelAdapter(Protocol):
    """每平台一个适配器：封装平台特异的认证、入站解析、出站发送。

    三个方法都是**平台特异**逻辑的边界——gateway 只调这三个方法，不直接
    处理平台协议。新增平台 = 新增一个 adapter 实现此协议 + 注册到
    ``ADAPTERS`` dict，gateway 代码零改。
    """

    platform: str  # "wechat" | "dingtalk" | "feishu" — 与 ImChannelEntity.platform 对齐

    def verify_inbound(self, headers: dict, body: bytes | str) -> bool:
        """校验入站请求是否来自该平台（签名 / 鉴权）。

        钉钉：timestamp+sign HMAC-SHA256（app_secret）。
        飞书：encrypt_key 解密 + challenge 应答。
        企业微信：msg_signature SHA1(token+timestamp+nonce)。
        MVP mock：恒返回 True（echo server 不签名）；真平台时填真实算法。
        返回 False → gateway 拒绝入站（403）。
        """

    def parse_inbound(self, body: bytes | str, channel_config: dict) -> InboundMessage:
        """把平台原始回调体解析成标准化 InboundMessage。

        ``channel_config`` 是该渠道的凭证（app_id / app_secret 等），用于
        解密 / 解析平台协议（飞书事件需解密）。提取 platform_user_id /
        platform_session_id / content，剥掉 @机器人 前缀。
        """

    async def send_outbound(self, payload: OutboundPayload, channel_config: dict) -> None:
        """把回复推送到平台（出站）。

        钉钉：POST robot webhook（outgoing token）。
        飞书：POST /im/v1/messages（tenant_access_token）。
        企业微信：POST /cgi-bin/message/send。
        MVP mock：``logger.info("[im:%s] outbound → %s: %s", self.platform, payload.target, payload.content)``
        不真发 HTTP（19e e2e 断言此日志）。真平台留档四。
        """
```

### 3.2 三 mock adapter（19b 落地）

| adapter | platform | verify_inbound | parse_inbound | send_outbound |
|---|---|---|---|---|
| `WechatAdapter` | `wechat` | mock: `True` | 从 body 取 `FromUserName`/`Content` | `logger.info` mock |
| `DingtalkAdapter` | `dingtalk` | mock: `True` | 从 body 取 `senderId`/`senderNick`/`text.content` | `logger.info` mock |
| `FeishuAdapter` | `feishu` | mock: `True` | 从 body 取 `event.message.chat_id`/`event.message.content` | `logger.info` mock |

`ADAPTERS = {"wechat": WechatAdapter, "dingtalk": DingtalkAdapter, "feishu": FeishuAdapter}`，gateway 按 `channel.platform` 取实例（adapter 无状态，可共享实例）。

---

## 4. ImChannelEntity schema

### 4.1 实体定义（`store/entities.py` 新增）

```python
class ImChannelEntity(Base):
    """An IM platform channel — a bidirectional bridge to an external IM bot.

    One row = one configured IM bot connection (e.g. one DingTalk robot). The
    ``platform`` selects the ``ImChannelAdapter``; ``config`` holds the platform-
    specific credentials (app_id / app_secret / verify_token / webhook_url);
    ``target_conversation_id`` is the internal conversation/group the inbound
    message routes to (and whose agent replies get pushed back out). At fire
    time the gateway calls ``route_user_message`` / ``route_direct_message`` to
    reuse the existing inbound routing — IM is just another ``push_notify``
    caller (same shape as the scheduler). ``enabled`` is the connect/disconnect
    toggle (mirrors ``ScheduledTaskEntity.enabled`` TM-05).
    """

    __tablename__ = "im_channels"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # wechat | dingtalk | feishu — selects the adapter (见 §3.2)
    platform: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # 平台特异凭证 JSON：{app_id, app_secret, verify_token, webhook_url, ...}
    # mock 阶段可空 / 占位；真平台时填实。敏感字段 API 返回时脱敏（参考 mcp._mask_sensitive）
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # 入站投递目标：单聊 conversation_id 或群聊 group_id（Path C 后两语义统一于
    # conversation_id 字段）。gateway 按 target_kind 分流到 route_direct_message / route_user_message
    target_conversation_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # single | group — 决定入站走 route_direct_message 还是 route_user_message
    target_kind: Mapped[str] = mapped_column(String, nullable=False, default="single")
    # 该 channel 绑定的 agent（单聊即 conversation.agent_id；群聊可空，走 @mention 路由）
    target_agent_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    # 默认 disabled——channel 显式 enable 后才接收入站（防误配凭证时被外部刷入站）
    # 出站 mock 日志开关：mock 阶段恒 True，logger.info 记出站；档四真平台时此字段语义变为「是否记出站审计日志」
    outbound_log: Mapped[bool] = mapped_column(Integer, nullable=False, default=1)
    # 平台会话映射：{platform_session_id → internal_conversation_id}
    # 一对多：一个 channel 可能对应平台上多个群/多个单聊用户，每个映射到不同的内部会话
    # MVP 可空（target_conversation_id 即唯一投递点）；多会话路由留档四
    session_bindings: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
```

### 4.2 字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | str PK | `imc_xxx`（`imc_` 前缀，与 `mcp_`/`sched_`/`mem_` 同款命名空间隔离，见 [[naming-conventions]]） |
| `name` | str | 渠道名（用户起，如「钉钉研发群机器人」） |
| `platform` | str | `wechat`/`dingtalk`/`feishu`，选 adapter |
| `config` | JSON | 平台凭证 `{app_id, app_secret, verify_token, webhook_url}`，敏感字段 API 返回脱敏 |
| `target_conversation_id` | str | 入站投递目标（单聊 conversation_id / 群聊 group_id） |
| `target_kind` | str | `single`/`group`，分流路由 |
| `target_agent_id` | str | 单聊即 conversation.agent_id；群聊可空（走 @mention） |
| `enabled` | int(bool) | 启停开关，默认 0 |
| `outbound_log` | int(bool) | mock 出站日志开关 |
| `session_bindings` | JSON | 平台会话↔内部会话映射（MVP 可空） |

### 4.3 与既有实体的关系

- **与 `ConversationEntity`**：`target_conversation_id` 指向单聊会话（`target_kind=single`）。入站即 `route_direct_message(conversation_id, content)`，复用单聊 resident worker engine。
- **与 `GroupEntity`**：`target_conversation_id` 持 group_id（`target_kind=group`）。入站即 `route_user_message(group_id, content)`，走 group graph + @mention 路由。
- **与 `ScheduledTaskEntity`**：同型（外部触发源 → agent inbox）。scheduler 是「时间触发」，IM 是「平台触发」，两者都是 `push_notify`/`route_*` 的 caller，复用同一条入站 → 执行 loop。
- **与 `McpConnectionEntity`**：凭证 JSON + enable/disable 开关 + 命名空间前缀，模式一致（CRUD + 启停 + 测试 三件套，见 §6 API）。

### 4.4 与持久层决策的关系

[[persistence-db-vs-session-file-2026-07-26]] 用户拍「先不改 DB」针对的是「为查询便利加表」，IM 渠道是**新功能需新实体**——渠道配置本身就是要持久化的核心数据（凭证 / 启停 / 会话映射），加 `im_channels` 表是合理的，不在该决策范围内（与 [[memory-module-2026-07-27]] 记忆模块加 `memories` 表同理）。加表理由：
- 渠道是独立实体（自身 CRUD / 启停 / 多会话映射），不是某行的 JSON 字段。
- 与 `mcp_connections` / `scheduled_tasks` / `memories` 同型（新功能新实体）。

---

## 5. 入站/出站数据流

### 5.1 入站数据流（IM 平台 → 智能体）

```
IM 平台用户消息
  ↓ 平台回调（HTTP POST）
POST /api/im/inbound/{channel_id}                # FastAPI 路由（api/im.py）
  ↓
gateway.deliver_inbound(channel_id, headers, body)
  ├─ crud.get_im_channel(channel_id)
  ├─ if not channel.enabled → 410 Gone（渠道已停用，不入站）
  ├─ adapter = ADAPTERS[channel.platform]()
  ├─ if not adapter.verify_inbound(headers, body) → 403 Forbidden
  ├─ inbound = adapter.parse_inbound(body, channel.config)
  │     → InboundMessage(platform_user_id, platform_session_id, content, ...)
  ├─ 投递目标解析：
  │     target = _resolve_target(channel, inbound)
  │       · session_bindings 有 platform_session_id 映射 → 用映射的 conversation_id
  │       · 无映射 → 用 channel.target_conversation_id（MVP 唯一投递点）
  └─ 按 target_kind 分流（复用现有路由，不新造通道）：
       · target_kind=single → route_direct_message(target, content)
            → ensure_engine → push_notify → resident worker engine
       · target_kind=group  → route_user_message(target, content)
            → GroupRuntime.invoke_turn（@mention 分叉 / Leader centralized）
```

**关键决策**：入站**复用 `route_user_message` / `route_direct_message`**，不新造 IM 专用 inbox 通道。这意味着：
- IM 入站消息与前端用户消息**走完全相同的 agentic loop**（与 scheduler 一致的设计哲学）。
- agent 不区分消息来自前端还是 IM——它只看到 inbox 里的 `push_notify` item。
- 入站可携带 `im_context`（`channel_id` + `platform` + `platform_session_id`）放在 notify 的 `data` 字段，供出站回推时反查 channel（见 §5.2）。

**入站幂等**：平台可能重试回调（钉钉 3 次重试）。MVP 不做消息去重（平台 msg_id 缓存）——mock echo server 不重试，真平台留档四加 `metadata_.seen_msg_ids` 去重。

### 5.2 出站数据流（智能体回复 → IM 平台）

```
智能体 reply（graph 节点 / registry 执行态）
  ↓
persist_agent_reply(group_id, agent_id, content, data, task_id)   # engine/reply.py:49
  ├─ crud.create_message(...)          # 落盘 agent_reply 行
  ├─ emit_message_added(msg)            # WS 推前端
  └─ 出站钩子（新增，19c 落地）：
       outbound.maybe_deliver_outbound(msg)
         ├─ 按 msg.conversation_id 查 im_channels（target_conversation_id 匹配 + enabled）
         ├─ 无匹配 channel → 直接返回（非 IM 会话，纯前端对话，零副作用）
         ├─ 有匹配 → adapter = ADAPTERS[channel.platform]()
         ├─ payload = OutboundPayload(target=platform_session_id, content=msg.content)
         └─ await adapter.send_outbound(payload, channel.config)
              · MVP mock: logger.info("[im:%s] outbound → %s: %s", platform, target, content)
              · 真平台: httpx POST 平台 API（档四）
```

**出站钩子挂点选择**（已比选）：

| 方案 | 描述 | 优劣 |
|---|---|---|
| ❌ A. 在 graph 节点里硬编码 | coordinator/worker `_unified_reply` 内调 IM 发送 | graph 节点耦合 IM，破坏「graph 不知回复去哪」原则；每加一个出口都要改 graph |
| ❌ B. 监听 WS bus | gateway 订阅 `message_added` 事件 | 解耦但需事件订阅基础设施；进程内调用绕一圈 WS 序列化，延迟 + 复杂 |
| ✅ C. 挂 `persist_agent_reply` 后（推荐） | reply 真源落盘后调 `outbound.maybe_deliver_outbound(msg)` | reply.py 是单一真源（三路径汇聚），挂此处覆盖所有 reply；解耦（reply.py 只调一个 `maybe_*`，不知 IM 细节）；同进程直调无 WS 开销 |

**方案 C 的解耦边界**：
- `reply.py` 只调 `outbound.maybe_deliver_outbound(msg)`（一个函数），**不 import adapter / gateway**。
- `outbound.py` 查表 + 分发，是 reply↔IM 的唯一接缝。reply.py 不知道 IM 是否存在、有几个 channel、什么平台。
- mock 阶段 `maybe_deliver_outbound` 内部对每条 reply 查 `im_channels` 表——无 channel 则 no-op（纯前端对话零副作用），有 channel 则走 adapter mock。
- 这样 reply.py 的改动是**纯加法**（落盘后多调一个 maybe 函数），不破坏现有流式 / 持久化 / 卡片观测逻辑。

**出站目标反查**：出站要把回复推回「入站来源的那个平台会话」。两种方式：
- **MVP**：channel 唯一 `target_conversation_id`——该会话的所有 reply 都推到该 channel 的默认 `platform_session_id`（channel.config.default_session）。简单但一个 channel 只回推一个会话。
- **档四**：入站时把 `platform_session_id` 记进 `session_bindings[platform_session_id] = conversation_id`，出站时按 `msg.conversation_id` 反查 `platform_session_id` 推回原会话——支持一个 channel 对应多个平台群/用户。

### 5.3 数据流总览图

```
┌─────────────┐   inbound    ┌──────────┐  route_*   ┌─────────┐  push_notify  ┌────────┐
│ IM 平台      │ ───────────→ │ gateway  │ ─────────→ │ inbox   │ ────────────→ │ engine │
│ (微信/钉钉/飞书)│              │ +adapter │            │ (真源)  │               │ (loop) │
└─────────────┘              └──────────┘            └─────────┘               └────┬───┘
      ↑                          ↑                                                   │
      │ outbound (httpx/mock)    │ maybe_deliver_outbound                            │ reply
      │                          │ (挂 persist_agent_reply 后)                        │
      └──────────────────────────┴───────────────────────────────────────────────────┘
```

---

## 6. API 设计预览（19c 落地）

路由 `APIRouter(prefix="/api/im-channels", tags=["im"])` + 入站回调 `APIRouter(prefix="/api/im/inbound", tags=["im-inbound"])`，参考 `api/mcp.py` / `api/scheduled_tasks.py` 模式：

| 方法 | 路径 | 说明 | 对应任务 |
|---|---|---|---|
| `GET` | `/api/im-channels` | 列渠道（按 platform 筛选） | 19c |
| `GET` | `/api/im-channels/{id}` | 取单渠道（config 脱敏） | 19c |
| `POST` | `/api/im-channels` | 建渠道（platform + config + target） | 19c |
| `PUT` | `/api/im-channels/{id}` | 改渠道（凭证 / target / 启停） | 19c |
| `DELETE` | `/api/im-channels/{id}` | 删渠道（连带 remove 入站监听） | 19c |
| `POST` | `/api/im-channels/{id}/enable` | 启用渠道（开始接收入站） | 19c |
| `POST` | `/api/im-channels/{id}/disable` | 停用渠道（拒入站，出站钩子仍可查到但 enabled=0 跳过） | 19c |
| `POST` | `/api/im-channels/{id}/test` | 测试出站（mock 发一条到 default_session，验凭证/adapter） | 19c |
| `POST` | `/api/im/inbound/{channel_id}` | **入站回调端点**（平台 / echo server POST 到此） | 19c |

**脱敏**（参考 `api/mcp.py:_mask_sensitive`）：`config.app_secret` / `config.verify_token` 在 GET 返回时打码（`***`），POST/PUT 接受明文，update 时 merge（未传的敏感字段保留原值，见 `mcp._merge_masked_fields`）。

Pydantic 模型放 `models/im.py`（`ImChannel` / `ImChannelCreatePayload` / `ImChannelTestResult`），`models/__init__.py` 注册导出。路由注册到 `backend/main.py:app.include_router`（在 `usage.router` 后加 `im.router` + `im_inbound.router`）。

---

## 7. 与 inbox/reply 落点关系（重点）

### 7.1 入站落点：复用 `route_user_message` / `route_direct_message`

| 入站源 | 落点 | 触发函数 |
|---|---|---|
| 前端用户（Web UI） | `POST /api/messages` → `route_user_message`（群）/ `route_direct_message`（单聊） | `api/messages.py:51 send_message` |
| 定时任务 | `scheduler._fire` → `push_task` | `engine/scheduler.py:171` |
| **IM 平台（本模块）** | `gateway.deliver_inbound` → `route_user_message`（群）/ `route_direct_message`（单聊） | `engine/im/gateway.py`（19c 新增） |

**三者最终都落到 `push_notify` / `push_task` → resident engine inbox**。IM 不新造通道，只是又一个 caller——与 scheduler 同型（scheduler 是「时间触发源」，IM 是「平台触发源」）。

### 7.2 出站落点：挂 `persist_agent_reply` 后

`engine/reply.py:49 persist_agent_reply` 是三条 reply 路径（registry 执行态 announce / coordinator graph / worker graph）的**单一落盘真源**（B10 重构后）。出站钩子挂在它落盘 + emit 之后：

```python
# engine/reply.py:persist_agent_reply 末尾（19c 新增一行）
async def persist_agent_reply(...):
    msg = await crud.create_message({...})
    ...
    await emit_message_added(msg.model_dump())
    # 19c 新增：IM 出站钩子（无 channel 则 no-op，纯前端对话零副作用）
    from engine.im.outbound import maybe_deliver_outbound  # 延迟 import 防循环
    await maybe_deliver_outbound(msg.model_dump())
    return msg.model_dump()
```

**为什么挂这里而不是 graph 节点**：见 §5.2 方案 C——reply.py 是真源，挂此处覆盖所有 reply（前端对话 / 定时触发回复 / IM 入站回复 都经此），且 reply.py 不知 IM 存在（只调一个 `maybe_*` 函数）。

### 7.3 边界约束

- **IM 入站不改 inbox 协议**：`push_notify` 的 item shape 不变（IM 只是 caller）。入站携带的 `im_context` 放 `data` 字段（已有 `data: dict | None` 参数），不新增字段。
- **IM 出站不改 reply schema**：`persist_agent_reply` 签名不变，出站钩子是**纯加法**（落盘后多调一个函数）。Message 行不带 IM 标记（出站是投递动作，不是消息属性）。
- **disable 不阻断出站查询**：`maybe_deliver_outbound` 查 `im_channels` 时**不过滤 enabled**——即便 channel disabled，已存在的会话回复仍可推（出站是被动响应入站，入站时 channel 必 enabled）。但 disabled channel 的入站被 gateway 拒（§5.1），故 disable 后不会有新 reply 触发出站。MVP 简化：`maybe_deliver_outbound` 过滤 `enabled=1`，与入站对称（disable 后双向静默）。

---

## 8. mock echo e2e 方案（19e 落地）

### 8.1 测试目标

验证 IM 双向链路：入站 → 投递到 agent → agent reply → 出站钩子 → mock 出站日志；disable 后不再投递。

### 8.2 测试架构（参考 18b/18c 范式）

```
backend/tests/test_im_e2e_full_chain.py
  ├─ 段 A：建 channel + 入站投递 + 出站 mock 日志
  │    1. _init_isolated_db（MULTI_AGENT_DATA_DIR 临时目录，与 18b/18c 同）
  │    2. 建 agent + 单聊 conversation（target_kind=single）
  │    3. POST /api/im-channels 建 channel（platform=wechat, target=conversation_id, enabled）
  │    4. 起 aiohttp echo server 模拟平台回调（POST /api/im/inbound/{channel_id}）
  │    5. echo server 发一条入站消息 → gateway.deliver_inbound → route_direct_message
  │    6. 断言：inbox 有 notify item（_notify_queues[conversation_id] 有条目）
  │    7. mock agent reply（直接调 persist_agent_reply 模拟 engine 回复）
  │    8. 断言：adapter.send_outbound 被 caplog 拦截到 logger.info("[im:wechat] outbound → ...")
  ├─ 段 B：disable 后不再投递
  │    1. POST /api/im-channels/{id}/disable
  │    2. echo server 再发入站 → 断言 410 Gone（gateway 拒入站）
  │    3. inbox 无新 item（_notify_queues 不增长）
  ├─ 段 C：test 端点（mock 出站测试）
  │    1. POST /api/im-channels/{id}/test → 断言 mock 出站日志 + 返回 ImChannelTestResult(ok=True)
  └─ 段 D：清理
       1. delete channel + conversation + agent
       2. shutdown_scheduler（防 AsyncIOScheduler 跨 asyncio.run 坑，见 [[scheduler-e2e-test-pattern-2026-07-27]]）
       3. echo server 关闭
```

### 8.3 关键测试技巧

- **caplog 拦截 mock 出站**：`caplog.set_level(logging.INFO, logger="multi-agent.im")`，断言 `"[im:wechat] outbound →" in caplog.text`。adapter mock 的 `send_outbound` 走 `logger.info`，caplog 是最直接的断言点（不 monkeypatch adapter，保持真实链路）。
- **不跑真 LLM**：agent reply 用 `persist_agent_reply` 直接模拟（与 18b 不建 engine 思路一致——本测聚焦 IM 链路，不验 agent 执行）。
- **aiohttp echo server**：起一个最小 aiohttp.web app，`POST /` 把收到的 body 转发到 `http://localhost:{app_port}/api/im/inbound/{channel_id}`（即被测的 FastAPI app）。或更简单：测试直接调 `deliver_inbound(channel_id, headers, body)` 函数，跳过 HTTP 层（与 18b 直接调路由函数同型）。
- **每段 shutdown_scheduler**：AsyncIOScheduler 跨 asyncio.run 必 RuntimeError（[[scheduler-e2e-test-pattern-2026-07-27]]），即使本测不起 scheduler 也习惯性收（防未来加 scheduler 依赖时踩坑）。
- **pytest 真门**：`assert not errs`（与 18b/18c 同，失败 raise AssertionError 判 FAIL；不用 16c 的 `return errs` 仅 warn）。

### 8.4 测试覆盖契约

| 契约 | 断言 |
|---|---|
| 入站投递到 inbox | `_notify_queues[conversation_id]` 有 item（receiver/sender/content 对齐） |
| 出站钩子触发 | caplog 命中 `[im:{platform}] outbound → {target}: {content}` |
| disable 阻断入站 | disable 后入站返回 410 + inbox 不增长 |
| test 端点 mock 发 | `/test` 返回 `ok=True` + caplog 命中出站日志 |
| 清理干净 | channel/conversation/agent 删除后 list 为空 |

---

## 9. 风险与约束

| 风险/约束 | 应对 |
|---|---|
| [[use-open-source-not-handrolled]] 不手搓 IM 协议 | adapter 是薄壳封装平台官方协议，不自研 IM 框架；HTTP 用 httpx（已有），不引 IM SDK |
| [[engines-use-frameworks-not-handrolled]] 网关不自研 | 入站路由复用 route_user_message/route_direct_message（既有），不新造调度框架；gateway 是路由器不是引擎 |
| [[agent-no-cli-decouple]] worker 不调 CLI | IM 出站是 HTTP/logger，与 CLI 无关；agent 不感知 IM |
| 入站安全（伪造回调） | adapter.verify_inbound 校验平台签名；MVP mock 恒 True，真平台必填真实算法；档四加 IP 白名单 |
| 凭证泄露 | config.app_secret/verify_token API 返回脱敏（参考 mcp._mask_sensitive）；update merge 保留未传字段 |
| 平台重试导致重复入站 | MVP 不去重（echo server 不重试）；档四加 `metadata_.seen_msg_ids` 去重 |
| 出站钩子性能 | maybe_deliver_outbound 查表是单条 select；无 channel 则 no-op 短路；不阻塞 reply 主流程（await 但 mock 极快） |
| reply.py 改动侵入性 | 纯加法（落盘后多调一个 maybe 函数）；函数无 channel 则 no-op，行为退化到现状 |
| AsyncIOScheduler 跨 asyncio.run 坑 | 测试每段 shutdown_scheduler（[[scheduler-e2e-test-pattern-2026-07-27]]） |
| 多会话路由（一个 channel 多群） | MVP channel 唯一 target_conversation_id；多会话 session_bindings 留档四 |

---

## 10. 分档路线

| 档 | 范围 | 任务 |
|---|---|---|
| **档一（本文档·设计先行）** | 分层 + adapter 协议 + 数据流 + schema + 落点 + mock e2e 方案 | ✅ 任务19a（本任务） |
| **档二（MVP 后端）** | `ImChannelEntity` + 三 mock adapter + `gateway.py`（deliver_inbound + 出站钩子）+ `outbound.py`（maybe_deliver_outbound）+ `/api/im-channels` CRUD + enable/disable/test + `/api/im/inbound/{id}` | 任务19b（实体+adapter）+ 任务19c（网关+API） |
| **档三（前端 + e2e）** | `ImChannelPanel`（替换 SettingsModal.tsx:297 占位）+ mock echo e2e 全链路 | 任务19d（前端）+ 任务19e（e2e） |
| **档四（真平台接入·后续）** | 真凭证填充 + httpx 真发 + 平台签名校验真实算法 + IP 白名单 + 入站去重 + 多会话路由 | 留 v2，依赖各平台开发者账号申请 |

---

## 11. 关联

- 入站路由真源：`engine/mention.py:241 route_user_message`（群聊）+ `engine/direct.py:32 route_direct_message`（单聊）。
- 出站回复真源：`engine/reply.py:49 persist_agent_reply`（三路径汇聚，B10 重构后）。
- inbox 真源：`engine/inbox.py:49 push_task` / `:85 push_notify`（per-(group,agent) asyncio.Queue）。
- scheduler 同型参考：`engine/scheduler.py`（「外部触发源 → agent inbox」同型，add_job/remove_job/_fire 模式可借鉴 channel enable/disable）。
- 实体模式参考：`store/entities.py:222 ScheduledTaskEntity` / `McpConnectionEntity`（凭证 JSON + enable 开关 + 命名空间前缀同型）。
- API 模式参考：`api/mcp.py`（CRUD + enable/disable + 脱敏 + mount/test）+ `api/scheduled_tasks.py`（CRUD + run_now/pause/resume + 历史）。
- 测试范式参考：`backend/tests/test_tm_e2e_full_chain.py`（18b 全链路 e2e）+ `test_tm_e2e_18c_regressions.py`（18c 回归）+ [[scheduler-e2e-test-pattern-2026-07-27]]（四段契约 / shutdown_scheduler / pytest 真门）。
- 前端占位：`src/components/SettingsModal.tsx:297`（im nav `<ImChannelCard>` × 3 mock 卡片，NavKey='im' 已就绪，19d 替换为 `ImChannelPanel`）。
- 命名约定：[[naming-conventions]]（`imc_` 前缀与 `mcp_`/`sched_`/`mem_` 同款）。

[[use-open-source-not-handrolled]]（adapter 薄壳不自研 IM 框架）· [[engines-use-frameworks-not-handrolled]]（入站复用既有路由不自研网关引擎）· [[agent-no-cli-decouple]]（出站与 CLI 无关）· [[persistence-db-vs-session-file-2026-07-26]]（加 `im_channels` 表是新功能核心数据非查询便利，见 §4.4）· [[scheduler-e2e-test-pattern-2026-07-27]]（e2e 测试范式 + AsyncIOScheduler 跨 loop 坑）· [[overnight-batch-plan-2026-07-26]]（任务19 明细设计真源）

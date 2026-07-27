# 豆包式单聊重构 · 交接文档

> 本文为交接给其他开发者/脚手的任务清单与上下文。最后更新 2026-07-28。
> 改动分支：`master`（未 commit，16 文件 +645/−243，全在工作区）。

## 0. 一句话背景

把单聊从「find-or-create per agent（侧栏列智能体）」改成**豆包式**：
- 全平台只有一个常驻「平台助手」agent（`slug='platform_assistant'`），侧栏【会话】所有会话都绑它；
- 侧栏不再列智能体，改列**会话标题**（首条消息后自动生成）；
- 智能体广场点 agent = 「体验对话」（transient 临时会话，不进侧栏，可转正）；
- 智能体管理（增删改）移到广场页「管理」tab。

## 1. 已完成（T86/T87/T88，已校验 tsc+build+pytest）

### 后端（T86，9 文件 +314/−31）

| 子步 | 落点 | 说明 |
|---|---|---|
| 平台助手 seed + 启动幂等补种 | `backend/store/seed.py`：`PLATFORM_ASSISTANT_ID="agent_platform_assistant"` / `PLATFORM_ASSISTANT_SLUG="platform_assistant"` / `ensure_platform_assistant(SessionLocal)` | `backend/store/database.py::init_db` 在 `seed_demo_data` 后调，每次启动跑（存量库也补种）。还兜底 DROP 老库残留的 `agents.allowed_tools/denied_tools` NOT NULL 死列。 |
| `ConversationEntity.transient` 字段 | `backend/store/entities.py`：`transient: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)` | `1=体验会话不进侧栏`；`models/conversation.py` 的 `Conversation` model + `ConversationCreatePayload` 同步加 `transient`；`crud._conversation_to_model` 映射。 |
| `create_conversation` 改语义 | `backend/store/crud.py::create_conversation` | `agent_id=None` → 按 slug 查平台助手绑定（查不到 raise→API 返 503）；`name` 默认 `""`（不再取 agent 名）；`transient` 透传；**每次新建**（不再 find-or-create）。旧 `get_or_create_conversation` 保留标 `[DEPRECATED T86]`。 |
| `list_conversations` 过滤 transient | `backend/store/crud.py` | `.where(ConversationEntity.transient == 0)`，只返非体验会话。 |
| 标题自动生成 | `backend/api/messages.py`：`_build_title(content)` + `_maybe_autotitle_conversation()` | `send_message` 在 `emit_message_added` 后调；查 `get_conversation`，群聊跳过，name 为空时取 content 前 20 字（strip 换行，超 20 加「…」）写回 + emit `conversation_updated`；已有 name 不覆盖（仅首条触发）；best-effort 不阻塞主流程。 |
| 转正端点 | `POST /api/conversations/{conversation_id}/finalize` → `backend/store/crud.py::finalize_conversation` | UPDATE `transient=0` + `updated_at`；404 若不存在。 |
| 存量库兼容 | `backend/store/database.py::_migrate_schema` | `_ensure_column("conversations","transient","INTEGER NOT NULL DEFAULT 0")`——PRAGMA table_info 查列，无则 ADD COLUMN；try/except 兜已存在。 |
| 新事件 | `backend/events/bus.py::emit_conversation_updated(conversation_id, name)` | `type="conversation_updated"`，payload 含 `conversation_id` + `name`，走 `bus-event:{conversationId}` 通道（与 `emit_message_added` 同 BusManager）。`backend/events/__init__.py` 导出。 |

**后端测试**：`pytest tests/` → 49 passed / 2 failed。2 个失败（`test_vh63_single_chat_turn_one_bubble.py::test_C10/C11`）是**预存在的前端失败**（`git stash` 验证 master 上同样失败），原因是 `ChatPanel.tsx` 之前重构后 `coordinatorStreamingBubbles` 符号不存在，断言源码字符串命中失败——**非本次引入**，不在后端范围。

### 前端（T87/T88，已过 `npx tsc --noEmit` exit 0 + `npm run build` exit 0）

**T87 侧栏 + Context + 聊天头**
- `src/components/Sidebar.tsx`：`ConversationsPanel` 会话条显 `c.name || '新对话'`（去 agent 名 fallback、去 subtitle）；底部加「➕ 新对话」按钮（豆包式，调 `createNewConversation`）。
- `src/contexts/SelectionContext.tsx`：退役 `selectAgent`，加 `createNewConversation()`（调 `conversationApi.create({})` 不带 agent_id→后端绑平台助手）；JSDoc 全面更新。
- `src/components/ChatView.tsx`：单聊头部只显会话标题（`activeConversation?.name || '新对话'`，不显 agent 名/角色）；去掉 `AgentEditButton`，保留 ⚙会话信息。
- `src/services/api.ts`：`ConversationCreatePayload.agent_id` 改可选 + 加 `transient`；`Conversation` 加 `transient`；加 `conversationApi.finalize(id)`。
- `src/App.tsx` / `src/components/Layout.tsx`：注释 `selectAgent` → `createNewConversation` 文案同步。

**T88 广场 tab 化 + 体验面板**
- `src/pages/AgentPage.tsx`：顶部 Segmented「体验/管理」tab 切换。
  - 体验 tab：agent 卡片 actions = 「试聊」按钮 → `openTrial(agent)` 创建 transient 会话（`conversationApi.create({agent_id, transient:1})`）+ `setGroupId(conv.id)` 切全局 WS 通道 + 开 Drawer。
  - 体验 Drawer（720px）：顶部 Alert「不进侧栏」+「转为正式会话」按钮（调 `finalize`）；内嵌 `ChatPanel`（hideHeader）复用全套聊天能力。
  - 管理 tab：原 CRUD（新建/编辑/删除 Modal + 自然语言生成 + 角色模板广场）全保留；新建占位卡仅管理 tab 显。
- 关键串台点：体验试聊时把全局 `groupId` 设到 transient 会话 id，`ChatPanel`（从 `BusEventContext` 读 `chatGroupId`）才订阅对的 WS 通道、消息才落对会话。transient 不进侧栏 list（后端过滤），侧栏不高亮，关闭后用户点侧栏别会话即恢复。

## 2. 待办任务（T90 / T91 / T89）

### T90 后端：存量会话 name 清理（去智能体名）· blocked by T86（已落，可做）

**问题**：侧栏出现「新对话/新对话/协调者/后端工程师/前端工程师」——根因是旧 `create_conversation` 把 `name` 默认填成 agent 名（`crud.py:362` 旧逻辑 `name = agent.name if agent else "单聊"`）。前端已改显 `c.name || '新对话'`，但**存量数据 name 就是 agent 名**，得后端迁移函数清理。

**方案**（启动迁移函数，dev 数据可丢所以跑一次即可）：
1. 启动时扫描所有 `ConversationEntity`，若 `name` 为空 OR `name === 该会话 agent 的 name` → 视为「未生成标题」。
2. 对这些会话，查 `messages` 表取该会话最早的 `user_input`，用其前 ~20 字生成标题写回 `name`（**复用 T86 的 `_build_title` 逻辑**，别重写）。
3. 若会话无任何 `user_input` 消息 → `name` 置空（前端显「新对话」），首条新消息触发 T86 标题生成。
4. 迁移函数幂等（已生成标题的会话 `name !== agent 名`，跳过），放 `backend/store/seed.py` 或 `database.py` 启动期，`ensure_platform_assistant` 同期调。

**约束**：与 T86 标题生成复用同一段「取首条用户消息前 N 字」代码；dev 数据可丢；不 git commit；不动 group_graph 拓扑。

### T91 前端：接 conversation_updated 事件刷新侧栏标题 · blocked by T86（已落，可做）

**问题**：后端 T86⑤ 首条消息生成标题后 emit `conversation_updated` 事件（payload: `conversation_id` + `name`，走 `bus-event:{conversationId}` 通道），但**前端 `useBusEvent` 完全没接**（`grep conversation_updated src/` 为空）。后果：侧栏会话标题不实时刷新，一直显「新对话」到手动 `refreshAll`。

**接线方案**：
1. `src/hooks/useBusEvent.ts`：`mapKind` 加 `'conversation_updated'` → `'conversation_updated'`。
2. `useBusEvent` 暴露 `conversationUpdates` state（收到事件 push `{conversationId, name, ts}`）。
3. `src/contexts/BusEventContext.tsx` 透传 `conversationUpdates`。
4. `src/contexts/SelectionContext.tsx` effect 监听 `conversationUpdates` → 本地 patch 对应 conversation 的 `name`（省一次 list 请求；或退一步调 `conversationApi.list()` 全量刷新）。

**注意防串台**：`conversation_updated` 走 `bus-event:{conversationId}` 通道，`useBusEvent` 按 `groupId` 订阅，只收当前会话事件，天然不串台。但侧栏要刷的是「所有会话」列表里那条——当前会话标题变了，本地 patch 即可，不需全量 list。

**约束**：不灵动（hover 可、无 infinite 动画）；antd 优先不手搓；不 git commit。

### T89 校验：tsc + build + 手测清单 · blocked by T90 + T91

1. `npx tsc --noEmit -p tsconfig.json` exit 0。
2. `npm run build` 成功。
3. 后端 `pytest backend/tests/`（预期 49 passed，2 个预存在前端失败可忽略，或一并修）。
4. 手测清单（dev 跑起来后）：
   - 广场体验 tab → 点「试聊」→ 体验 Drawer 开 → 发消息能回 → 关闭即清（不进侧栏）→「转为正式会话」→ 落进左侧【会话】。
   - 侧栏「+新对话」→ 开空对话（绑平台助手）→ 发首条消息 → 侧栏标题自动从「新对话」变内容摘要（验证 T91 接线）。
   - 存量会话（旧 name=agent 名）启动后 name 被清理成内容摘要或「新对话」（验证 T90 迁移）。
   - 空态：新库无会话 → 侧栏【会话】显「暂无会话」+「+新对话」。
   - 切群组/单聊不串台（StreamingBubbleList 防串台双保险仍在 `src/components/StreamingBubbleList.tsx:189-208`）。

## 3. 硬约束（全程遵守）

- LangGraph 原生术语，不自创比喻（如「扔字条」）；
- agent 不调 Claude Code CLI；
- 引擎用框架不自研；
- 不动 `backend/engine/group_graph.py` 拓扑；
- 不碰群聊路径（`route_user_message` / `group_graph` 未动）；
- 前端不灵动（hover 可、无 infinite 动画，状态脉冲 OK）；antd 优先不手搓；
- dev 数据可丢（迁移函数跑一次即可）；
- **不 git commit**（主会话统筹后处理）。

## 4. 关键文件/符号速查

| 符号 | 位置 |
|---|---|
| 平台助手 id/slug 常量 | `backend/store/seed.py:28-29` |
| `ensure_platform_assistant()` | `backend/store/seed.py:218` |
| `create_conversation`（改后） | `backend/store/crud.py:355` |
| `finalize_conversation()` | `backend/store/crud.py:459` |
| `_build_title()` / `_maybe_autotitle_conversation()` | `backend/api/messages.py:51` / `:61` |
| `emit_conversation_updated()` | `backend/events/bus.py:496` |
| `_migrate_schema`（transient 补列） | `backend/store/database.py` |
| 前端 `createNewConversation` | `src/contexts/SelectionContext.tsx` |
| 前端 `openTrial` / `finalizeTrial` | `src/pages/AgentPage.tsx` |
| 前端 `conversationApi.create` / `finalize` | `src/services/api.ts` |

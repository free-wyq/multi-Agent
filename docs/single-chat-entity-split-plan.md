# 单聊彻底分实体重构方案（Path C · 阶段 1 探查 + 方案）

## 0. 背景与目标

用户决定走 **Path C**：单聊从 `Group` 实体里彻底独立出来，自己的存储 / 消息 / UI 路径，不再用 `single_chat` flag 把单聊塞进 `Group`。**开发期数据可丢弃**（不写迁移脚本，老 `single_chat` 群数据直接弃）。

**完成标志**：group 相关代码里不再有 `single_chat` flag（实体 / 路由 / registry / 前端 / 测试全部清除），单聊有自己独立的实体与路径。

**阶段 1 范围**：只探查 + 写本方案文档，禁止改任何实现代码（.py / .tsx / .ts 都不许动）。

> **【阶段 1 收口·用户拍板 2026-07-23】** 选定 **C2 实体分共享底层** + **严格改名**（`MessageEntity.group_id` / `TaskEntity.group_id` → `conversation_id`，开发期数据可弃直接 drop+recreate 表，不写迁移脚本）。阶段 2 按本方案 6 commit 落地。下方凡写「保留字段名」处均已被本决策推翻为「严格改名」。

---

## 1. 现状：22+ touchpoint 分类清单

实际 grep 出的 touchpoint 文件数比任务描述的 22 多（含 reply.py / state.py / prompts.py 的零散 `single` / `single-chat` 词义引用，非 flag 读取）。按层分类：

### 1.1 实体 / 存储层（4 文件，核心 flag 读取处）

| 文件 | 现状（一句话） |
|---|---|
| `backend/store/entities.py` | `GroupEntity.config` JSON 列承载 `single_chat: true`，单聊群和群聊群共用 `groups` 表，无独立实体。 |
| `backend/models/group.py` | `GroupConfig` TypedDict 未声明 `single_chat` 字段（靠 `extra="allow"` 容纳），单聊 flag 走开放 dict 透传。 |
| `backend/store/crud.py` `create_group` :225-228 | 注释提到「透传 payload.config（single_chat 等群组级标记）」，把前端传的 `{single_chat:true}` 原样落库到 `config` JSON。 |
| `backend/store/crud.py` `delete_group` :279+ | 删 group 级联删 members / tasks / messages（单聊和群聊共用此路径，单聊独立后需保留同款级联语义）。 |

### 1.2 引擎 / 路由层（5 文件，运行时分叉）

| 文件 | 现状（一句话） |
|---|---|
| `backend/engine/mention.py` `route_user_message` :298-305 | **核心分叉点**——`single_chat` 群早返回走 legacy `push_notify` 到驻留 worker engine，不进群图。 |
| `backend/engine/registry.py` `AgentEngine.__init__` :89/107/110/132 | `single_chat` 参数传入 + `self.single_chat` 字段，选图公式 `if self.is_coordinator and not self.single_chat` 决定 coordinator 图 vs worker 图。 |
| `backend/engine/registry.py` `load_from_store` :1262-1268 | 启动期遍历 `groups` 表，读 `g.config.single_chat` 决定引擎选图传参。 |
| `backend/engine/registry.py` `_handle_notify` :795 | `if not self.single_chat:` 守卫——群聊 worker 加 `TEAM_INTERACTION_SUFFIX`，单聊 worker 保持原 persona。 |
| `backend/engine/coordinator.py` :125 | 仅注释提到「单聊不走 coordinator 图（registry 按 single_chat 判）」，无逻辑读取。 |

### 1.3 消息 / 流式层（3 文件，关键分叉）

| 文件 | 现状（一句话） |
|---|---|
| `backend/store/entities.py` `MessageEntity` :109-121 | `messages` 表用 `group_id` 关联，单聊和群聊消息共用一张表，靠 `group_id` 区分。 |
| `backend/store/crud.py` `list_messages` :531 / `create_message` :557 | 消息 CRUD 按 `group_id` 过滤，单聊群聊共用同一套查询，无类型字段区分。 |
| `backend/events/bus.py` `BusManager` :41-98 | WS 通道 `bus-event:{groupId}` 按群 id 分通道，单聊群聊共用同一通道机制（一个 id 一通道）。 |

### 1.4 前端层（6 文件，路由 / UI 分叉）

| 文件 | 现状（一句话） |
|---|---|
| `src/contexts/SelectionContext.tsx` :113/132/144 | `isSingleChat = !!activeGroup?.config?.single_chat` 派生 `activeKind`；`selectAgent` find-or-create `single_chat:true` 群。 |
| `src/components/Sidebar.tsx` :70/123 | `multiAgentGroups = groups.filter(g => !g.config?.single_chat)` 过滤掉单聊；`activeGroupId` 同款守卫。 |
| `src/components/ChatView.tsx` :60 | `isSingleChat = !!activeGroup.config?.single_chat` 决定标题区显 agent 名还是群名 + 不显群信息按钮。 |
| `src/components/GroupInfoDrawer.tsx` :716 | `{!group?.config?.single_chat && <协作模式 Segmented>}` 单聊群隐藏协作模式设置。 |
| `src/components/CreateGroupModal.tsx` :42 | 注释提到「不处理单聊（single_chat）——单聊走点选智能体的 find-or-create 路径」。 |
| `src/App.tsx` :13 | 注释提到 selectAgent find-or-create single_chat 群。 |

### 1.5 测试层（11 文件，断言形态各异）

| 文件 | 现状（一句话） |
|---|---|
| `test_vb3_single_chat_streaming.py` | 单聊流式契约锁——group_e53545c（single_chat=true）+ agent_backend_1 走 worker 图 + brain reply_id 流式。 |
| `test_vb1_task_think_visible.py` | 用同一单聊群 group_e53545c 验证 task_think 可见性。 |
| `test_vb2_artifact_download.py` | 用同一单聊群验证 artifact 下载。 |
| `test_va1_repro_essay_bubbles.py` | 用同一单聊群验证作文气泡 repro。 |
| `test_vh23_naming_namespace_consistency.py` | 锁「两套身份分类轴」(graph_kind vs single_chat) 与「三套 id 命名空间」——29 处 `single_chat` 断言，最重测试。 |
| `test_vh9_team_interaction_suffix.py` | 锁 `if not self.single_chat:` 守卫（单聊不加 TEAM_INTERACTION_SUFFIX）。 |
| `test_vh57_collaboration_mode_field.py` | F13 断言 single_chat bypass 不读 collaboration_mode（14 处提及）。 |
| `test_vh8_coordinator_id_freshness_contract.py` | 注释提 single_chat 是身份层字段。 |
| `test_m12_e2e_three_actions.py` / `test_m12_boundary_new_demand.py` / `test_m12_unit_interrupt_resume.py` | m12 计划确认 e2e 测试，`AgentEngine(agent_def, group_id, coord_id, single_chat=False)` 显式传 False（群聊场景）。 |

---

## 2. C1 vs C2 论证

### 2.1 C1 纯分（单聊独占消息表 + 独占流式 + 独占 UI）

**做法**：
- 新增 `ConversationEntity`（单聊实体）+ `DirectMessageEntity`（单聊独占消息表）。
- 单聊流式走独立 WS 通道 `bus-event:conversation:{conversationId}` 或新端点。
- 前端 `ChatPanel` 拆出 `SingleChatPanel`，单聊独占组件。

**利**：
- 实体纯净，单聊和群聊物理隔离，代码层级清晰。
- 单聊未来若要换协议（如 IM 风格的 read receipt / typing indicator），改单聊表不影响群聊。
- WS 通道隔离，单聊流式故障不影响群聊通道。

**弊**：
- **消息层复制成本高**：`MessageEntity` 已有完整 schema（id / group_id / task_id / sender_id / receiver_id / type / content / data / created_at）+ CRUD（list / create / clear / by-task）+ emit 投影。复制一套 `DirectMessageEntity` 要复制 schema + CRUD + emit + WS 通道 + 前端渲染，改动面翻倍。
- **改消息格式要改两处**：未来加字段（如 reply_to / attachments）要同步改两张表，易漂移。
- **task_id 关联复杂**：单聊也有 execute 路径（worker push_task 跑 agentic loop），`TaskEntity.group_id` 现在关联 group，单聊独立后 task 关联单聊 id 还是 group id？若单聊 task 走单聊表，task 关联也要拆。
- **前端 ChatPanel 复制**：当前 `ChatPanel` 单聊群聊共享，渲染逻辑（气泡 / 流式 / 状态行 / 折叠区）成熟。拆两套组件意味着两套渲染逻辑要同步维护。

### 2.2 C2 实体分共享底层（单聊独立实体，Message / 流式 / ChatPanel 共享）

**做法**：
- 新增 `ConversationEntity`（单聊实体，字段：id / agent_id / name / created_at / updated_at），与 `GroupEntity` 并列。
- `MessageEntity.group_id` 改名 `conversation_id`（语义中立，既关联单聊也关联群聊），或保留 `group_id` 字段名但语义泛化为「会话 id」（单聊 id / 群聊 id 都行）。
- WS 通道 `bus-event:{conversationId}` 按会话 id 分通道（单聊 id / 群聊 id 各一通道，机制不变）。
- resident worker engine 复用——单聊 conversation_id 作 engine key（原 group_id 角色），engine 逻辑零改。
- 前端 `ChatPanel` 共享，`SelectionContext` 按 `activeKind` 切单聊 / 群聊路由，ChatPanel 不感知实体差异。

**利**：
- **分该分的**：实体 / 路由 / 选图分（单聊走自己的 `ConversationEntity` + `route_direct_message`，群聊走 `GroupEntity` + `route_user_message` + group graph）。
- **共享该共享的**：Message / 流式 / ChatPanel 共享（单一消息格式 / 单一流式通道机制 / 单一渲染组件），无复制成本。
- **运行时行为不破**：单聊走 mention.py:298-305 bypass + resident worker engine 的行为完全保留，只是触发入口从 `group.config.single_chat` 改成 `ConversationEntity` 独立路由。
- **task 关联自然**：`TaskEntity.group_id` 改名 `conversation_id`（或保留字段名），单聊 task 关联单聊 conversation_id，群聊 task 关联群聊 conversation_id，无歧义。
- **改动面收敛**：MessageEntity schema 不动（字段名保留），只改路由 / 选图 / 前端派生逻辑。

**弊**：
- `MessageEntity.group_id` 字段名语义泛化（既是单聊 id 也是群聊 id），若严格要字段名对齐需改名 `conversation_id`（一次性 ALTER TABLE，开发期数据可弃故成本可控）。
- `route_user_message` 要拆出 `route_direct_message`（单聊专属入口），多一个函数但逻辑更清晰。

### 2.3 推荐：C2 实体分共享底层

**一句话理由**：运行时已分（单聊走 mention bypass + resident worker engine；群聊走 group graph），纠结只在存储命名 / 实体边界；消息 / 流式 / UI 复制一套是负收益（改动面翻倍 + 未来格式漂移风险），C2 分该分的、共享该共享的，最贴合实际耦合度。

**C1 没有压倒性理由**：探查后确认消息层耦合是「group_id 关联」而非「single_chat flag 耦合」——`MessageEntity` 本身不读 `single_chat`，只按 `group_id` 过滤。把 `group_id` 泛化为 `conversation_id` 就能让消息层同时服务单聊和群聊，无需拆表。流式通道同理（`bus-event:{groupId}` 改名 `bus-event:{conversationId}` 即可）。因此 C1 的「单聊独占消息表 / 独占流式」是过度拆分，无技术必要性。

---

## 3. C2 改造清单（按层）

### 3.1 实体 / 存储层

| 改动 | 文件 | 描述 |
|---|---|---|
| 新增 `ConversationEntity` | `backend/store/entities.py` | 新实体：id / agent_id / name / created_at / updated_at。`__tablename__ = "conversations"`。 |
| 新增 `Conversation` Pydantic 模型 | `backend/models/conversation.py`（新建） | 镜像 Entity，供 API 层返回。 |
| 新增 `ConversationCreatePayload` | 同上 | 仅 `agent_id`（必填）+ `name`（可选，缺省取 agent.name）。 |
| 新增 `crud_direct.py` 或扩展 `crud.py` | `backend/store/crud.py` | 加 `list_conversations` / `get_conversation` / `create_conversation` / `delete_conversation` / `get_or_create_conversation(agent_id)`。 |
| `MessageEntity.group_id` 字段 | `backend/store/entities.py` | **保留字段名不改**（开发期数据可弃，但改字段名要 ALTER TABLE + 改所有 CRUD 查询，成本高）。语义泛化为「会话 id」（单聊 conversation_id / 群聊 group_id 都用它）。注释更新说明语义。 |
| `GroupEntity.config` 的 `single_chat` key | 无需删（老群组数据弃，新建群不再写 single_chat） | 开发期清库即可，`GroupEntity.config` schema 不动（只是不再写 single_chat key）。 |

### 3.2 API 层

| 改动 | 文件 | 描述 |
|---|---|---|
| 新增 `/api/conversations` 路由 | `backend/api/conversations.py`（新建） | `GET /api/conversations` 列表 / `POST /api/conversations` 建单聊（find-or-create 语义）/ `GET /{id}` / `DELETE /{id}`。 |
| 注册路由 | `backend/main.py` | import + `app.include_router(conversations.router)`。 |
| `POST /api/messages` 路由分叉 | `backend/api/messages.py` | `send_message` 按 payload 的会话类型分流：单聊走 `route_direct_message`，群聊走 `route_user_message`。或 payload 加 `conversation_type` 字段，或单聊走独立端点 `POST /api/conversations/{id}/messages`。**推荐独立端点**（路径更清晰）。 |
| 删 `single_chat` 从 `GroupCreatePayload` 透传 | `backend/models/group.py` / `backend/store/crud.py` | `create_group` 注释 :225-228 删掉 single_chat 提及（建群不再写 single_chat）。 |

### 3.3 引擎 / 路由层

| 改动 | 文件 | 描述 |
|---|---|---|
| 新增 `route_direct_message` | `backend/engine/mention.py` 或新 `backend/engine/direct.py` | 单聊专属路由：persist user message + `push_notify` 到驻留 worker engine（原 mention.py:298-305 逻辑搬到此处）。不读 `single_chat` flag，直接按 `conversation_id` 路由。 |
| `route_user_message` 删 single_chat bypass | `backend/engine/mention.py` :298-305 | 删掉 `if group and (group.config or {}).get("single_chat")` 早返回——单聊不再走此函数。 |
| `AgentEngine.__init__` 删 `single_chat` 参数 | `backend/engine/registry.py` :89/107/110/132 | 选图公式简化为 `if self.is_coordinator:` → coordinator 图，否则 worker 图。单聊 engine 直接用 worker 图（不传 single_chat）。**或更彻底**：单聊 engine 用 conversation_id 作 key，不走 group 路径——但 resident worker engine 复用更稳，保留 engine 机制，只删 single_chat 参数。 |
| `load_from_store` 分两遍 | `backend/engine/registry.py` :1259-1295 | 先遍历 `groups` 建群聊 engine（不再读 single_chat），再遍历 `conversations` 建单聊 engine（worker 图，key=conversation_id）。 |
| `_handle_notify` 删 `if not self.single_chat` 守卫 | `backend/engine/registry.py` :795 | 单聊 engine 默认就是 worker 图，不加 `TEAM_INTERACTION_SUFFIX`（单聊无同事互动场景）。守卫改成「单聊 engine 不加」——可通过 engine 类型判定（如 `engine.kind == "direct"`）或 conversation_id 前缀判定。 |

### 3.4 消息 / 流式层（共享，几乎不改）

| 改动 | 文件 | 描述 |
|---|---|---|
| `MessageEntity.group_id` 语义注释 | `backend/store/entities.py` :113 | 注释更新：「会话 id（单聊 conversation_id / 群聊 group_id）」，字段名不改。 |
| `list_messages` / `create_message` | `backend/store/crud.py` :531/557 | 参数名 `group_id` 保留（语义泛化），查询逻辑零改。 |
| `BusManager` 通道 | `backend/events/bus.py` | 零改——`bus-event:{conversationId}` 机制不变，单聊 conversation_id / 群聊 group_id 各一通道。 |
| `emit_*` helpers | `backend/events/bus.py` | 零改——所有 emit 按 `group_id`（泛化语义）路由。 |

### 3.5 前端层

| 改动 | 文件 | 描述 |
|---|---|---|
| `SelectionContext` 重构 | `src/contexts/SelectionContext.tsx` | `selectAgent` 改调 `POST /api/conversations`（find-or-create 单聊）；`activeKind` 派生不再读 `single_chat`，改看 `activeConversation` vs `activeGroup`。新增 `conversations` state + `activeConversation` 派生。 |
| `Sidebar` 过滤逻辑 | `src/components/Sidebar.tsx` :70/123 | 删 `multiAgentGroups` 过滤（groups 列表不再含单聊，单聊走独立 `conversations` 列表）；新增「智能体」分组列表项从 `conversations` 派生（点选进单聊）。 |
| `ChatView` 标题区 | `src/components/ChatView.tsx` :60 | `isSingleChat` 改成 `activeKind === 'agent'`（从 SelectionContext 派生，不读 config.single_chat）。 |
| `GroupInfoDrawer` 协作模式守卫 | `src/components/GroupInfoDrawer.tsx` :716 | 删 `{!group?.config?.single_chat && ...}`——Drawer 只在群聊场景渲染（单聊不显 Drawer，或单聊有独立设置入口）。 |
| `ChatPanel` | `src/components/ChatPanel.tsx` | 零改——`group` prop 泛化为「会话对象」（单聊 Conversation / 群聊 Group），字段兼容（id / name / coordinator_id 单聊也用 agent_id 当 coordinator_id）。 |
| `CreateGroupModal` | `src/components/CreateGroupModal.tsx` | 零改——只建群聊，不建单聊。 |
| `App.tsx` | `src/App.tsx` | 注释更新（selectAgent find-or-create conversation）。 |
| `api.ts` | `src/services/api.ts` | 加 `conversationApi`（list / get / create / delete / listMessages / sendMessage），或扩展 `messageApi` 支持单聊会话。 |

### 3.6 测试层

| 改动 | 文件 | 描述 |
|---|---|---|
| 重写 | `test_vb3_single_chat_streaming.py` | 改用 `ConversationEntity` + `route_direct_message`，断言流式契约不变（reply_id / task_token）。 |
| 重写 | `test_vb1_task_think_visible.py` / `test_vb2_artifact_download.py` / `test_va1_repro_essay_bubbles.py` | 改用单聊 conversation，断言逻辑不变。 |
| 重写 | `test_vh23_naming_namespace_consistency.py` | 删 `single_chat` 断言（29 处），改成断言「单聊 engine 用 worker 图 + conversation_id 作 key」「群聊 engine 按 is_coordinator 选图」。 |
| 重写 | `test_vh9_team_interaction_suffix.py` | 守卫断言改成「单聊 engine 不加 TEAM_INTERACTION_SUFFIX」（不再读 `self.single_chat`）。 |
| 更新 | `test_vh57_collaboration_mode_field.py` F13 | 删 single_chat bypass 断言，改成「单聊走 route_direct_message 不进群图」断言。 |
| 更新 | `test_vh8_coordinator_id_freshness_contract.py` / `test_m12_*.py` | 删 `single_chat=False` 参数（AgentEngine 签名删此参数）。 |
| 新增 | `test_vh59_direct_conversation_entity.py`（或同号段） | 锁单聊独立实体 + route_direct_message + resident worker engine 复用契约。 |

---

## 4. commit 拆分（6 个 commit）

1. **commit 1：实体 + 存储层** — 新增 `ConversationEntity` + `Conversation` 模型 + `ConversationCreatePayload` + `crud.create_conversation` / `list_conversations` / `get_conversation` / `delete_conversation` / `get_or_create_conversation`。`MessageEntity.group_id` 注释更新（语义泛化）。`init_db` 建表。不改任何现有逻辑（纯加法）。
2. **commit 2：API 层** — 新增 `api/conversations.py`（GET / POST / GET /{id} / DELETE /{id}）+ `POST /api/conversations/{id}/messages` 独立消息端点。`main.py` 注册路由。`GroupCreatePayload` / `crud.create_group` 注释清理（删 single_chat 提及）。
3. **commit 3：引擎 / 路由层** — 新增 `route_direct_message`（从 mention.py:298-305 搬逻辑）；`route_user_message` 删 single_chat bypass；`AgentEngine.__init__` 删 `single_chat` 参数 + 选图公式简化；`load_from_store` 分两遍（groups + conversations）；`_handle_notify` 守卫改判定方式。
4. **commit 4：前端层** — `SelectionContext` 重构（selectAgent 改调 conversations API + activeKind 派生）；`Sidebar` 删 single_chat 过滤 + 单聊列表从 conversations 派生；`ChatView` 标题区改 activeKind 判定；`GroupInfoDrawer` 删 single_chat 守卫；`api.ts` 加 conversationApi。
5. **commit 5：测试层** — 重写 11 个测试（vb3 / vb1 / vb2 / va1 / vh23 / vh9 / vh57 / vh8 / m12×3），新增 vh59 直接实体锁。
6. **commit 6：清理 + 验证** — 全仓 grep 确认无 `single_chat` 残留（除历史注释 / git log）；跑全量 v* sweep + tsc；清开发期数据（老 single_chat 群数据弃）。

---

## 5. 风险点

### 5.1 resident worker engine 复用边界（最棘手）

`AgentEngine` 现在 key 是 `{group_id}:{agent_id}`，单聊独立后 conversation_id 作 key。若同一 agent 既有单聊又有群聊，两个 engine 实例并存（单聊 engine key=`{conversation_id}:{agent_id}`，群聊 engine key=`{group_id}:{agent_id}`）。**风险**：engine 状态隔离（memory / dispatch_plan / inbox）是否正确？**缓解**：engine 本就是 per-group per-agent，conversation_id 作 group_id 角色不影响隔离性，但要确认 `stop_task_by_id` / `list_group_status` 等跨群扫描逻辑能正确处理两种 key。

### 5.2 流式通道归属

`bus-event:{conversationId}` 单聊群聊共用通道机制。**风险**：前端 WS 订阅切群聊↔单聊时，旧通道是否正确退订？**缓解**：`useBusEvent` 已按 groupId 切换订阅，conversation_id 替代 group_id 后行为不变，但需 e2e 验证切换不漏事件。

### 5.3 `MessageEntity.group_id` 字段名语义泛化

保留字段名不改，但单聊消息写入时 `group_id` 实际是 `conversation_id`。**风险**：未来开发者看字段名误以为只关联群聊。**缓解**：注释 + 命名规范文档说明语义泛化；若要严格改名 `conversation_id`，开发期清库 + ALTER TABLE 一次性改（成本可控，但改动面大，不推荐第一轮做）。

### 5.4 `AgentEngine` 选图公式简化

删 `single_chat` 后，选图公式 `if self.is_coordinator and not self.single_chat` → `if self.is_coordinator:`。**风险**：单聊 engine 现在用 conversation_id 作 key，`is_coordinator` 判定（`agent_id == coordinator_id`）在单聊场景下是否成立？单聊 ConversationEntity 的 `agent_id` 就是对方 agent，没有 coordinator 概念——单聊 engine 应直接用 worker 图，不走 `is_coordinator` 判定。**缓解**：单聊 engine 构造时 `coordinator_id=""`（空），`is_coordinator=False`，自然走 worker 图。或新增 `engine.kind` 字段显式区分 `direct` / `group`。

### 5.5 前端 `ChatPanel` 的 `group` prop 兼容

`ChatPanel` 接收 `group` prop，单聊 ConversationEntity 字段（id / agent_id / name）与 GroupEntity（id / coordinator_id / name）不完全对齐。**风险**：`ChatPanel` 内部读 `group.coordinator_id` 的地方单聊会 undefined。**缓解**：`Conversation` 模型加 `coordinator_id` 字段（值=agent_id，语义对齐）；或 `ChatPanel` 改用 `activeKind` 判定单聊/群聊，分别读不同字段。

---

## 6. 不动的边界

明确以下路径完全不动：

- **group graph**（`engine/group_graph.py` 的 `route_entry` / `build_group_graph` / `build_route_entry`）—— 群聊拓扑 + 协作模式分流不动。
- **coordinator**（`engine/coordinator.py` 的 supervisor 子图 + `COORDINATOR_SYSTEM`）—— 群主调度逻辑不动。
- **swarm**（`langgraph_swarm` 的 `create_handoff_tool` + agent 节点 handoff）—— 去中心化 handoff 不动。
- **协作模式**（`collaboration_mode` centralized / decentralized + `recompile_group`）—— 群聊模式切换不动。
- **resident worker engine 的群聊用法**——群聊 engine 的 execute 路径（`_run_worker_task` / `execute_agent_task` / `create_react_agent`）不动。
- **单聊运行时行为**——`mention.py:298-305` bypass + resident worker engine 的行为原样保留，只是触发入口从 `group.config.single_chat` 改成 `ConversationEntity` 独立路由（`route_direct_message`）。
- **PL-02/03 计划确认**（`api/plan.py` + `GroupRuntime.resume_plan`）—— 群聊计划确认闭环不动。
- **技能系统 / MCP / 定时任务**——不动。

---

## 7. 探查中发现的、用户该知道但可能没想到的点

### 7.1 `MessageEntity.group_id` 保留字段名是务实选择

严格改名 `conversation_id` 要 ALTER TABLE + 改所有 CRUD 查询 + 改 emit 投影 + 改前端字段名——开发期数据可弃但改动面大。**建议第一轮保留字段名，注释说明语义泛化**；未来若要严格命名，单开一个重构 commit。

### 7.2 `test_vh23` 是最重测试（29 处 single_chat 断言）

`test_vh23_naming_namespace_consistency.py` 锁了「两套身份分类轴」(graph_kind vs single_chat)，删 single_chat 后这整个测试的断言形态要重写——不是简单删参数，是要重新定义「单聊 engine 身份判定」的契约。**建议 commit 5 测试层重写时优先处理 vh23**，它是身份层的命名规范锁，重写后要锁新契约（单聊 engine 用 worker 图 + conversation_id key + 无 coordinator 概念）。

### 7.3 `load_from_store` 分两遍有隐含顺序依赖

启动期先遍历 groups 建群聊 engine，再遍历 conversations 建单聊 engine。若同一 agent 同时在群聊和单聊里，两个 engine 实例并存——这是设计意图（隔离），但 `list_all_status` / `stop_task_by_id` 等跨群扫描逻辑要确认能正确处理两套 key。**建议 commit 3 引擎层改动后，跑一次全量状态聚合验证**。

### 7.4 前端 `ChatPanel` 的 `group` prop 需兼容 Conversation

`ChatPanel` 内部多处读 `group.coordinator_id`（用于显示群主 / 路由 @群主）。单聊 ConversationEntity 若不加 `coordinator_id` 字段，单聊渲染会 undefined。**建议 `Conversation` 模型加 `coordinator_id` 字段（值=agent_id）**，让 ChatPanel 零改——这是 C2「共享该共享的」的关键兼容点。

### 7.5 `route_direct_message` 应否独立文件

`mention.py` 已经 410 行，把 `route_direct_message` 搬进去会更臃肿。**建议新建 `backend/engine/direct.py`**（单聊专属路由），`mention.py` 只留群聊路由 + @mention 解析。职责更清晰，commit 3 改动面更收敛。

---

## 8. 完成标志

- `grep -r "single_chat" backend/ src/`（排除 `__pycache__` / `node_modules` / git history）无 flag 读取残留（仅历史注释可保留说明「曾用 single_chat flag，已拆为 ConversationEntity」）。
- 全量 `test_v*.py` 绿（含重写的 11 个 + 新增的 vh59）。
- `npx tsc --noEmit` 绿。
- 端到端：建单聊 / 发消息 / 收流式回复 / 切群聊 / 群聊协作模式 / 切回单聊，全链路通。

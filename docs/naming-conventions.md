# 命名一致性清单（id 命名空间 + 身份分类轴）

> 本文档是**单一真源**，消歧义 B26 审计发现的「两套平行分类」与「三套 id 命名空间」。
> 锁契约见 `backend/tests/test_vh23_naming_namespace_consistency.py`。
> **B26 只文档化 + 加交叉引用注释，不改任何运行时语义**（与 B24/B25 审计类任务同型）。
>
> **Path C 更新（单聊分实体）**：`single_chat` flag 已删除。单聊现为独立 `ConversationEntity`，
> 单聊 engine 构造时 `coordinator_id=""`（单聊无协调者概念）→ `is_coordinator=False` →
> 自然走 worker 图。身份派生链从「`single_chat` 输入 → `graph_kind` 派生」简化为
> 「`coordinator_id` 输入 → `is_coordinator` 派生 → `graph_kind` 派生」。

---

## 一、身份分类轴（输入→派生，非平行）

Path C 后只剩一条身份派生链：`coordinator_id`（输入）→ `is_coordinator`（派生）→
`graph_kind`（派生）。旧 `single_chat` 输入轴已删除（单聊分实体后由 `ConversationEntity`
独立承载，单聊 engine 的 `coordinator_id=""` 使 `is_coordinator=False` 自然走 worker 图）。

### 1.1 身份派生链（registry.py `AgentEngine.__init__`，startup-baked，生命周期内不变）

| 字段 | 层 | 类型 | 来源 | 作用 |
|---|---|---|---|---|
| `coordinator_id` | 输入（群级） | `str` | `GroupEntity.coordinator_id`（群聊）/ `""`（单聊 ConversationEntity，无协调者概念） | 该群谁是协调者；单聊为空串 → `is_coordinator=False` |
| `is_coordinator` | 派生（agent 级） | `bool` | `self.agent_id == coordinator_id`（registry.py:100） | 该 agent 是否是本群协调者（单聊 engine 恒 False） |
| `graph_kind` | 派生（agent 级） | `"coordinator" \| "worker"` | 见下真值表 | 编译哪张 LangGraph 图 |

### 1.2 graph_kind 派生真值表（registry.py:131）

```python
if self.is_coordinator:
    self.graph = build_coordinator_graph()
    self.graph_kind = "coordinator"
else:
    self.graph = build_worker_graph()
    self.graph_kind = "worker"
```

| is_coordinator | 场景 | graph_kind | 语义 |
|---|---|---|---|
| `True`  | 群聊 Leader（`agent_id == coordinator_id`） | `"coordinator"` | 群聊协调者，跑调度图 |
| `False` | 群聊普通成员 | `"worker"` | 普通成员，跑 worker 图 |
| `False` | 单聊 engine（`coordinator_id=""` → `is_coordinator=False`） | `"worker"` | 单聊=退化的多智能体，跑 worker 图（无 supervisor） |

**消歧义要点**：Path C 后单聊 engine 的 `coordinator_id=""` 使 `is_coordinator=False` →
自然走 worker 图。旧 `single_chat=True` 把 `is_coordinator=True` 的 agent 降级成 worker 图
的逻辑，现已由「单聊 engine 构造时 `coordinator_id=""`」等效实现——单聊无协调者概念，
`is_coordinator` 恒 False，无需额外 flag。

### 1.3 两轴各读处（勿混「输入」与「派生」）

- `coordinator_id`（输入）读处：
  - `registry.py:131` 选图公式（`if self.is_coordinator:`，`is_coordinator` 由 `coordinator_id` 派生）
  - `registry.py:803` `sys_for_invoke` 守卫（`if not self.is_coordinator and self.coordinator_id:` 加 `TEAM_INTERACTION_SUFFIX`，单聊 engine `coordinator_id=""` 使守卫短路 → 不加 suffix）
  - `registry.py` `load_from_store` 分两遍：群聊遍历 groups 传群 `coordinator_id`；单聊遍历 conversations 传 `coordinator_id=""`
  - `registry.py` `_run_worker_task` report-back 用 `self.coordinator_id`（缓存，不二次查库）
- `graph_kind`（派生）读处：
  - `registry.py:224` `_handle_task` 看门狗仅 worker 装（MT-17：协调者 LLM hang 由 httpx 兜底，不在此 kill coordinator 图）
  - `registry.py:279` `_execute_body` 分流（coordinator → 合成 notify 触发图；worker → `execute_agent_task`）
  - `registry.py:615` `_handle_notify` coordinator 分支装 `set_reply_callback` + 每次现读群配置
  - `registry.py:896` `reset_session` 仅 coordinator 图调 `aupdate_state(END)` 清 interrupt（worker 图无 interrupt 站点，no-op）

### 1.4 时效口径（B11 已锁，此处重申避免与命名混淆）

身份层字段（`coordinator_id` / `is_coordinator` / `graph_kind` / `system_prompt`）
皆 **startup-baked**（`__init__` 落定，生命周期内不再变）。换群主只落 DB 行不重建驻留引擎
（pending-restart）。配置层（`auto_confirm` / `leader_strategy`）才是 per-notify 现读。
详见 `test_vh8_coordinator_id_freshness_contract.py`。

---

## 二、三套 id 命名空间

三套 id 形状不同、作用域不同、复用规则不同。**有意的跨命名空间复用**已在下文显式标注——
那是设计而非碰撞 bug。

### 2.1 `task_id` — DAG 任务身份

| 项 | 值 |
|---|---|
| 形状 | `task_` + uuid hex（`crud._next_id("task")`，`_PREFIX_MAP["task"]="task_"`） |
| 生成 | `crud.create_task`（store/crud.py:428 `id=_next_id("task")`） |
| 作用域 | 一个 DAG 任务（dispatch 派发的一个步骤）的生命周期 |
| 落点 | `TaskEntity.id`；`MessageEntity.task_id`（execute-path announce 的 agent_reply 回填，B22）；`task_token`/`task_log`/`task_think`/`task_complete`/`task_failed`/`task_dispatch` WS 事件的 `task_id` 槽 |
| 前缀判定 | 前端 `useBusEvent.ts:430/455` 用 `startsWith('task_')` 判「真 task」流式 vs worker 单聊 reply_id（见 2.2 复用） |
| 跨命名空间复用 | **作为 `thread_id`**：`agent_loop.py:257` `thread_id = task_id or str(uuid4())`——`create_react_agent` 的 MemorySaver checkpointer 用 task_id 做 key，使同一 task 的多轮 tool 调用共享检查点（有意复用，非碰撞） |

### 2.2 `reply_id` — 单轮流式归并键

| 项 | 值 |
|---|---|
| 形状 | 裸 `uuid.uuid4().hex`（**无 `task_` 前缀**，这是与 task_id 的判别特征） |
| 生成 | 2 处：`coordinator.py:1348`（`_stream_coordinator_decision`）+ `worker.py:161`（`_stream_brain_decision`）——协调者与单聊 worker 同构 |
| 作用域 | 一次 LLM 回复（一个 turn）的流式生命周期 |
| 落点 | `coordinator_token`/`coordinator_reasoning`/`coordinator_stats` WS 事件的 `data.reply_id`；落盘到 `agent_reply.data["reply_id"]`（定稿气泡退场后仍可按 reply_id 找回流式统计） |
| 前端归并 | `coordStreaming[reply_id]` / `coordReasoning[reply_id]` / `coordStats[reply_id]`（useBusEvent.ts） |
| 跨命名空间复用 | **塞进 `task_id` 槽**：worker 单聊回复走 `task_token` 通道（非 `coordinator_token`），后端把 `reply_id` 放进事件的 `task_id` 字段。前端靠 `task_` 前缀**判别**：有前缀→真 task 流式（`streaming[task_id]`）；无前缀→ worker 单聊 reply_id（`coordStreaming[reply_id]`）。判别可靠因 reply_id 恒为裸 hex、task_id 恒有 `task_` 前缀 |

### 2.3 `thread_id` — LangGraph MemorySaver 检查点键

| 项 | 值 |
|---|---|
| 形状 | 两型：<br>① 驻留引擎图：`f"{group_id}:{agent_id}"`（registry.py:143，稳定 per (group,agent)；Path C 单聊 engine 的 `group_id` 实为 `conversation_id`，同款稳定键）<br>② `create_react_agent`（agent_loop.py:257）：`task_id or str(uuid4())`（per-execution） |
| 作用域 | ① 跨 invoke 持久化图状态（memory/dispatch_plan/recent_routes/interrupt）；② 单次 task 执行的 tool 多轮检查点 |
| 不碰撞保证 | ① 稳定键复用→跨 invoke 状态延续；② 用 task_id（若有）或新鲜 uuid4→每次执行独立检查点，不与历史 task 串话 |
| 跨命名空间复用 | 见 2.1：agent_loop 的 thread_id 在有 task_id 时复用 task_id（task-scoped 检查点） |

### 2.4 三套 id 判别速查

| 要判别 | 看什么 |
|---|---|
| 真 task 流式 vs worker 单聊 reply_id | WS 事件 `task_id` 字段有无 `task_` 前缀（useBusEvent.ts:430） |
| 驻留图检查点 vs 执行检查点 | thread_id 是 `{group}:{agent}`（稳定）还是 `task_*`/uuid（per-exec） |
| agent_reply 关闭哪个 task | `agent_reply.task_id`（B22 回填，exact 匹配 task_complete/failed 事件）；无 task_id 的 chat 路径回落 sender+timestamp |

---

## 三、B26 审计结论（Path C 更新）

1. **身份派生链简化**：Path C 删除 `single_chat` flag 后，派生链从
   「`single_chat` 输入 → `graph_kind` 派生」简化为
   「`coordinator_id` 输入 → `is_coordinator` 派生 → `graph_kind` 派生」。
   单聊 engine 的 `coordinator_id=""` 使 `is_coordinator=False` → 自然走 worker 图，
   等效旧 `single_chat=True` 降级逻辑，但无需额外 flag（单聊分实体后由 `ConversationEntity`
   独立承载）。已在本章 1.1–1.3 显式拆解输入/派生/读处。
2. **「三套 id 命名空间」形状/作用域/复用规则各异，且有意的跨命名空间复用**（task_id 兼作
   agent_loop thread_id；reply_id 塞进 task_token 的 task_id 槽靠前缀判别）。非碰撞 bug，
   是设计。已在本章 2.1–2.4 显式标注每处复用与判别规则。
3. **不改语义**：B26 只文档化 + 在 2 个最易混淆点加交叉引用注释（registry.py 选图分支、
   agent_loop thread_id 赋值），不动任何运行时逻辑。Path C 更新文档与测试断言形态
   （`single_chat` 断言改为 `coordinator_id=""` / `is_coordinator=False` 断言），
   不改运行时语义。契约测 `test_vh23` 锁住形状/前缀/复用规则防未来回归。

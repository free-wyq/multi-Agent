# 命名一致性清单（id 命名空间 + 身份分类轴）

> 本文档是**单一真源**，消歧义 B26 审计发现的「两套平行分类」与「三套 id 命名空间」。
> 锁契约见 `backend/tests/test_vh23_naming_namespace_consistency.py`。
> **B26 只文档化 + 加交叉引用注释，不改任何运行时语义**（与 B24/B25 审计类任务同型）。

---

## 一、两套身份分类轴（非平行，是输入→派生）

审计发现 `graph_kind` 与 `single_chat` 看似两套平行分类，实则是**输入→派生**关系：
`single_chat` 是输入（群级配置标志），`graph_kind` 是派生输出（编译哪张 LangGraph 图）。
它们不是两个独立维度，而是同一条身份派生链上的两环。混淆「平行」会误以为可独立翻转其一。

### 1.1 身份派生链（registry.py `AgentEngine.__init__`，startup-baked，生命周期内不变）

| 字段 | 层 | 类型 | 来源 | 作用 |
|---|---|---|---|---|
| `coordinator_id` | 输入（群级） | `str` | `GroupEntity.coordinator_id`（建群/换群主时落库） | 该群谁是协调者 |
| `single_chat` | 输入（群级配置） | `bool` | `GroupEntity.config["single_chat"]`（前端建单聊群时传 `true` 落库） | 单聊群里唯一 agent 行为应是「个体」非「调度者」 |
| `is_coordinator` | 派生（agent 级） | `bool` | `self.agent_id == coordinator_id`（registry.py:100） | 该 agent 是否是本群协调者 |
| `graph_kind` | 派生（agent 级） | `"coordinator" \| "worker"` | 见下真值表 | 编译哪张 LangGraph 图 |

### 1.2 graph_kind 派生真值表（registry.py:126）

```python
if self.is_coordinator and not self.single_chat:
    self.graph = build_coordinator_graph()
    self.graph_kind = "coordinator"
else:
    self.graph = build_worker_graph()
    self.graph_kind = "worker"
```

| is_coordinator | single_chat | graph_kind | 语义 |
|---|---|---|---|
| `True`  | `False` | `"coordinator"` | 群聊 Leader，跑调度图 |
| `True`  | `True`  | `"worker"` | 单聊群唯一 agent（虽是 coordinator_id 但行为个体），跑 worker 图 |
| `False` | `*`     | `"worker"` | 普通成员，跑 worker 图 |

**消歧义要点**：`single_chat=True` 把一个 `is_coordinator=True` 的 agent 降级成 worker 图——
这是「单聊 = 退化的多智能体」共识（supervisor 只在多 agent 里存在）。故 `single_chat` 不是
`graph_kind` 的平行兄弟，而是 `graph_kind` 派生公式的一个**输入项**。

### 1.3 两轴各读处（勿混「输入」与「派生」）

- `single_chat`（输入）读处：
  - `registry.py:126` 选图公式（`not self.single_chat`）
  - `registry.py:722` `sys_for_invoke` 守卫（单聊不加 `TEAM_INTERACTION_SUFFIX`，保持原 persona）
  - `registry.py:1054` `AgentRegistry.ensure_engine` 从群配置取 `single = bool((g.config or {}).get("single_chat"))` 传给 `AgentEngine`
  - 前端 `src/contexts/SelectionContext.tsx` / `Sidebar.tsx` / `ChatView.tsx`：find-or-create 单聊群、过滤多智能体列表、单聊 UI 分支
- `graph_kind`（派生）读处：
  - `registry.py:224` `_handle_task` 看门狗仅 worker 装（MT-17：协调者 LLM hang 由 httpx 兜底，不在此 kill coordinator 图）
  - `registry.py:279` `_execute_body` 分流（coordinator → 合成 notify 触发图；worker → `execute_agent_task`）
  - `registry.py:615` `_handle_notify` coordinator 分支装 `set_reply_callback` + 每次现读群配置
  - `registry.py:896` `reset_session` 仅 coordinator 图调 `aupdate_state(END)` 清 interrupt（worker 图无 interrupt 站点，no-op）

### 1.4 时效口径（B11 已锁，此处重申避免与命名混淆）

身份层 4 字段（`coordinator_id` / `is_coordinator` / `graph_kind` / `single_chat` / `system_prompt`）
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
| 形状 | 两型：<br>① 驻留引擎图：`f"{group_id}:{agent_id}"`（registry.py:132，稳定 per (group,agent)）<br>② `create_react_agent`（agent_loop.py:257）：`task_id or str(uuid4())`（per-execution） |
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

## 三、B26 审计结论

1. **「两套平行分类」实为输入→派生**：`single_chat`（输入群级标志）→ `graph_kind`（派生，编译哪张图）。
   非两个可独立翻转的维度。已在本章 1.1–1.3 显式拆解输入/派生/读处。
2. **「三套 id 命名空间」形状/作用域/复用规则各异，且有意的跨命名空间复用**（task_id 兼作
   agent_loop thread_id；reply_id 塞进 task_token 的 task_id 槽靠前缀判别）。非碰撞 bug，
   是设计。已在本章 2.1–2.4 显式标注每处复用与判别规则。
3. **不改语义**：B26 只文档化 + 在 2 个最易混淆点加交叉引用注释（registry.py 选图分支、
   agent_loop thread_id 赋值），不动任何运行时逻辑。契约测 `test_vh23` 锁住形状/前缀/复用规则
   防未来回归。

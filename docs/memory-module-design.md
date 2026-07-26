# 记忆模块设计（Memory Module Design）

> 单一真源：跨会话持久记忆的分层模型、实体 schema、生命周期四阶段、检索方案、prompt 注入落点。
> 锁定任务 `[任务17a-设计]`。后端实体+API 见 `[任务17b]`，前端记忆管理页见 `[任务17c]`。
> 参考业界记忆框架（mem0 / letta / langchain memory）设计，本地单机场景裁剪，不照搬云端多用户形态。

---

## 1. 设计目标与背景缺口

### 1.1 现状（已查证）

引擎层**已有**「会话记忆」机制（易混淆，须区分）：

| 机制 | 位置 | 作用 | 生命周期 |
|---|---|---|---|
| `GroupRuntime._memory` | `engine/group_runtime.py:255` | per-group 运行时会话记忆，`_record_turn_memory`(`:821`) 每轮 append 用户侧消息 | 会话内（`:1006` 每轮清） |
| 协调者 memory 注入 | `engine/coordinator.py:963-964` | `memory = state.get("memory")`，`conversation = "\n".join(... memory[-8:])` 拼进协调者决策 prompt | 会话内 |
| worker 上下文 | `engine/worker.py:139 _build_context_from_db` | 从 `messages` 表真源拉最近 8 条对话（已取代旧 `_memory` list） | 会话内（DB 持久但按 conversation 隔离） |
| `MemorySaver` | langgraph checkpointer | 单进程内存 checkpointer | 进程重启即丢 |

**以上全是会话级短期上下文**——管「这轮/这次会话聊到哪了」。

### 1.2 缺口

**无跨会话持久记忆**：用户事实 / 偏好 / 历史结论无法跨会话保留。每次新会话从零开始，不记得用户是谁、偏好什么、之前聊出过什么结论。这是本模块要解决的——给 agent 一个「跨会话的用户画像与长期事实库」。

### 1.3 设计原则

1. **不替换现有会话记忆**：短期工作记忆（`_memory` / `_build_context_from_db`）保留不动。长期记忆是**额外注入**。
2. **用开源不手搓**（[[use-open-source-not-handrolled]] / [[engines-use-frameworks-not-handrolled]]）：检索/抽取能用开源就用。本地单机 MVP 先用 SQLite FTS5 全文检索（零外部依赖），语义检索（向量 embedding）留 v2。
3. **本地单用户**：`user_id` 先固定本地单用户（无云端账号体系），schema 预留 `user_id` 字段以便未来多用户扩展，但 MVP 不做多租户隔离。
4. **设计先行，避免边写边改**：本文档定稿后再开发（17b 后端 + 17c 前端）。

---

## 2. 分层模型

参考业界记忆框架三段式（mem0 的 archival/working、letta 的 core/archival memory），结合本仓现状裁剪：

| 层 | 作用 | 生命周期 | 数据源 | 本仓现状 |
|---|---|---|---|---|
| **L1 短期/工作记忆** | 当前会话上下文（最近若干轮对话） | 会话内 | 现有机制 | ✅ 已有（`_memory` / `_build_context_from_db`），**不重做** |
| **L2 长期/情景记忆** | 跨会话的用户事实/偏好/历史结论 | 跨会话持久（DB） | **本模块新增** | ❌ 缺口，本模块补 |
| **L3 检索** | 按当前对话语义/关键词检索相关 L2 记忆注入 prompt | 实时 | 全文检索（v1）/ 向量（v2） | ❌ 缺口，本模块补 |

**L1 与 L2 的关系**：
- L1 管「这轮对话上下文」——最近 8 条消息，谁说了什么。
- L2 管「跨会话的用户画像」——用户是 Java 后端、偏好简洁回复、上次项目用的是 React。
- 两者**叠加注入** prompt（L1 是对话历史，L2 是用户画像段），不互相替换。

---

## 3. 与现有会话记忆边界（重点消歧）

| 维度 | L1 会话记忆（现有） | L2 长期记忆（本模块） |
|---|---|---|
| 真源 | `messages` 表 / `GroupRuntime._memory` | `memories` 表（新） |
| 粒度 | 每条消息 | 每条「事实/偏好」（一段话总结） |
| 时效 | 最近 8 条（滑动窗口） | 全部持久，按 importance + last_accessed 排序 |
| 跨会话 | ❌ 按 conversation_id 隔离 | ✅ 跨所有会话 |
| 写入时机 | 每轮自动 append | 会话/轮次末 LLM 抽取后写入（去重合并） |
| 注入位置 | 对话历史段（已有） | **新增**「关于用户的长期记忆」段 |
| 清理 | 每轮清（`_memory.clear()`）/ 按 conversation 自然隔离 | 显式删除或 importance 衰减 |

**关键约束**：L2 注入**不改 L1 的拼装逻辑**——`coordinator.py:963` 的 `memory[-8:]` 和 `worker._build_context_from_db` 的最近 8 条**原样保留**，L2 是在 system_prompt 层另起一段，与 L1 物理隔离。

---

## 4. MemoryEntity schema

### 4.1 实体定义（`store/entities.py` 新增）

```python
class MemoryEntity(Base):
    """A long-term memory record (PRD 记忆模块 · 任务17).

    A memory is a single persistent fact/preference/conclusion extracted from
    conversations and surfaced across sessions. Scoped by ``scope`` (global /
    agent / conversation); ``content`` is the natural-language statement;
    ``importance`` (0.0–1.0) ranks relevance at retrieval time. Retrieved
    memories are injected into the system prompt as a「关于用户的长期记忆」
    section, distinct from the L1 session-context (recent messages) which is
    unchanged.

    v1 retrieval uses SQLite FTS5 full-text search over ``content`` (zero
    external deps). v2 may add a ``vector_embedding`` column + semantic search
    (sqlite-vss / external store) — the schema reserves the column slot but v1
    leaves it NULL.
    """

    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, default="local", index=True)
    scope: Mapped[str] = mapped_column(String, nullable=False, default="global", index=True)
    # global | agent | conversation — 见 §4.3
    scope_ref: Mapped[str] = mapped_column(String, nullable=False, default="")
    # scope=agent → agent_id; scope=conversation → conversation_id; global → ""
    content: Mapped[str] = mapped_column(String, nullable=False, default="")
    # 自然语言陈述，如「用户是 Java 后端工程师，偏好简洁回复」
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # 来源信息：{source_conversation_id, source_agent_id, extracted_at, ...}
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    # 0.0–1.0，检索时与相关性加权排序；MVP 规则版赋值，v2 LLM 评估
    enabled: Mapped[bool] = mapped_column(Integer, nullable=False, default=1)
    # 软删除/禁用开关（用户可在前端手动禁用某条记忆）
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
    last_accessed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 检索命中时更新（last_accessed_at + access_count），用于衰减排序 + 前端「最近用过」展示
    # ── v2 预留（v1 不用，建表时 NULL）──────────────────────────────
    # vector_embedding: Mapped[...]  # v2 加列：向量 embedding（语义检索用）
```

### 4.2 字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | str PK | `mem_xxx`（`mem_` 前缀，与 `mcp_`/`sched_` 同款命名空间隔离，见 [[naming-conventions]]） |
| `user_id` | str | 本地单用户先固定 `"local"`；预留多用户扩展 |
| `scope` | str | `global` / `agent` / `conversation`（见 §4.3） |
| `scope_ref` | str | scope 的指向 id；`global` 时为 `""` |
| `content` | str | 记忆正文（自然语言陈述） |
| `metadata_` | JSON | 来源溯源：`{source_conversation_id, source_agent_id, extracted_at}` |
| `importance` | float | 0.0–1.0，检索排序权重 |
| `enabled` | int(bool) | 软删除开关 |
| `last_accessed_at` | str\|None | 最近命中检索时间 |
| `access_count` | int | 累计命中次数 |

### 4.3 scope 三档

| scope | scope_ref | 含义 | 注入范围 |
|---|---|---|---|
| `global` | `""` | 全局用户画像（「用户是 Java 后端」） | 所有会话都注入 |
| `agent` | `agent_id` | 某智能体专属记忆（「与后端工程师协作时偏好贴完整代码」） | 仅该 agent 的会话注入 |
| `conversation` | `conversation_id` | 某会话专属长期记忆（跨轮次但限本会话） | 仅该会话注入 |

MVP 先实现 `global` + `agent`（`conversation` 与 L1 重叠度高，留 v2 评估）。

### 4.4 FTS5 全文检索表（v1 检索真源）

```sql
-- 虚拟表，与 memories 同步 content（trigger 维护）
CREATE VIRTUAL TABLE memories_fts USING fts5(
    content,
    content_rowid='rowid',
    tokenize='unicode61'   -- 中文按字符分（MVP 够用）；v2 换 jieba 分词器
);
```

- **为何 FTS5**：sqlite 3.37.2 自带 FTS5（已验证 `CREATE VIRTUAL TABLE t USING fts5(content)` 在 `:memory:` 可建），零外部依赖，符合 [[use-open-source-not-handrolled]]「本地单机能用 SQLite 自带能力就不引外部向量库」。
- **同步方式**：`memories` 表 INSERT/UPDATE/DELETE 时用 SQLite trigger 同步到 `memories_fts`（或 crud 层显式双写，MVP 选显式双写便于测试）。
- **中文分词**：`unicode61` 按字符分，中文检索召回尚可（「Java 后端」能命中「后端」）；v2 评估 `jieba` tokenizer 或换向量检索。

### 4.5 与持久层决策的关系

[[persistence-db-vs-session-file-2026-07-26]] 用户拍「先不改 DB」是针对 **token 用量查询便利**场景（从 JSON 列查够用，不为查询便利加表）。记忆模块是**新功能需新实体**——记忆本身就是要持久化的核心数据，加 `memories` 表是合理的，**不在该决策范围内**（该决策针对「为查询便利加表」，非「为新功能加表」）。加表理由：
- 记忆是独立实体（有自身 CRUD / 软删除 / 衰减），不是某行的 JSON 字段。
- FTS5 虚拟表必须独立表，无法塞进 JSON 列。
- 与 `mcp_connections` / `skills` / `scheduled_tasks` 同型（新功能新实体）。

---

## 5. 生命周期四阶段

### 5.1 抽取（Extract）

**时机**：会话/轮次末，从最近对话抽取值得长期记的事实。

**MVP（规则版，v1）**：
- 触发点：会话结束（无显式「结束」信号，MVP 用「N 轮无活动」或「用户主动保存」）/ 用户手动新增。
- 抽取方式：**先规则版**——检测显式记忆指令（用户说「记住：我是 Java 后端」「以后回复简洁点」）→ 直接落库。不调 LLM，零成本零延迟。
- 落点：`engine/memory/extractor.py`（新模块）。

**v2（LLM 版，后续）**：
- 轮次末调一次轻量 LLM（与主模型分离，可用更便宜模型）扫最近 N 条消息，抽取事实/偏好，输出结构化 `{content, scope, importance}`。
- 评估 `mem0` / `langchain memory` 的抽取能力（[[use-open-source-not-handrolled]]），不自研抽取算法。

### 5.2 写入（Write）

**去重合并（核心，防每轮硬 append 爆库）**：
- 写入前先检索现有记忆，若语义/关键词高度相似（v1 用 FTS5 MATCH 命中 + 阈值；v2 用向量相似度）→ **合并**（更新 `content` + `importance` 取大 + `metadata_` 追加来源），不新增行。
- 不相似 → 新增。
- 落点：`store/crud.py` 新增 `upsert_memory(content, scope, scope_ref)`。

**importance 赋值**：
- v1 规则版：用户显式「记住」→ `1.0`；自动抽取 → `0.5`。
- v2：LLM 评估 0.0–1.0。

### 5.3 检索（Retrieve）

**时机**：新会话/新轮开始，按当前对话内容检索相关 L2 记忆。

**v1 全文检索**：
```python
# 伪码
async def retrieve_relevant_memories(
    query: str,           # 当前用户消息 / 最近对话摘要
    scope_filter: dict,   # {user_id, scope∈{global,agent,conversation}, scope_ref}
    top_k: int = 5,
) -> list[MemoryEntity]:
    # FTS5 MATCH 检索 content
    # 按 bm25 排序 + importance 加权
    # 过滤 enabled=1
    # 命中后更新 last_accessed_at + access_count（衰减排序用）
```

**检索 query 来源**：当前用户消息文本（MVP）/ 最近 N 条对话摘要（v2）。

**top_k 与 token 预算**：MVP `top_k=5`，按 `importance * bm25_score` 排序，content 总长度截断在 ~500 字符内（避免灌爆 system_prompt）。

**v2 向量检索**：
- 加 `vector_embedding` 列，用本地 embedding 模型（`sentence-transformers`）或外部 API 生成。
- 评估 `sqlite-vss`（SQLite 向量扩展）或 `chromadb`（本地向量库）。
- 语义去重合并（cosine 相似度阈值）。
- **本档需评估 [[use-open-source-not-handrolled]]**——用 `mem0` / `letta` 的向量能力不手搓。

### 5.4 注入（Inject）

**位置**：system_prompt 层，与 L1 对话历史**物理隔离**（不改 L1 拼装）。

**格式**（拼在 system_prompt 末尾，在技能 manifest / TEAM_INTERACTION_SUFFIX 之前，因为它更稳定）：
```
{原有 system_prompt}

关于用户的长期记忆（跨会话持久，供你参考）：
- 用户是 Java 后端工程师，偏好简洁回复
- 上次项目技术栈是 React + Vite
- ...
```

空记忆时不加该段（零注入，prompt 不变）。

**注入落点**（见 §6 详述）：
- 协调者：`build_coordinator_prompt`(prompts.py:341) 增 `long_term_memory` 参数。
- worker：`build_brain_prompt`(prompts.py:96) 增 `long_term_memory` 参数，或拼进 `system_prompt`（registry.py:876 `sys_for_invoke`）。

---

## 6. prompt 注入位置（具体落点）

### 6.1 现有 system_prompt 拼装链（已核实）

| 路径 | 拼装函数 | 落点 |
|---|---|---|
| 群聊 Leader | `coordinator._leader_system`(coordinator.py:128) | `base + "\n" + COORDINATOR_SYSTEM` |
| 群聊普通成员 | `registry.py:876 sys_for_invoke` | `self.system_prompt + "\n\n" + TEAM_INTERACTION_SUFFIX` |
| 单聊 worker | `registry.py:876 sys_for_invoke` | `self.system_prompt`（无 suffix） |
| 协调者决策 prompt | `build_coordinator_prompt`(prompts.py:341) | 含 `conversation`(=memory[-8:]) 段 |
| worker 决策 prompt | `build_brain_prompt`(prompts.py:96) | 含 `context`(=_build_context_from_db) 段 |

### 6.2 L2 注入方案（最小侵入）

**方案 A（推荐，MVP）**：在 system_prompt 拼装的最末追加「长期记忆」段。

落点：新增 `engine/memory/injector.py`:
```python
def format_long_term_memory_section(memories: list[MemoryEntity]) -> str:
    """格式化长期记忆段，空则返回 ''（零注入）。"""
    if not memories:
        return ""
    lines = [f"- {m.content}" for m in memories]
    return "\n\n关于用户的长期记忆（跨会话持久，供你参考）：\n" + "\n".join(lines)
```

调用点（3 处，每处加一段）：
1. **群聊 Leader** — `coordinator._leader_system`：`base + "\n" + COORDINATOR_SYSTEM + memory_section`。
2. **群聊普通成员 + 单聊 worker** — `registry.py:876 sys_for_invoke`：`(self.system_prompt or "") + memory_section + "\n\n" + TEAM_INTERACTION_SUFFIX`（memory 在 suffix 前，更稳定）。
3. **协调者决策 prompt** — `build_coordinator_prompt`：在「对话上下文」段后插「长期记忆」段（与 L1 的 `conversation` 并列，物理隔离）。
4. **worker 决策 prompt** — `build_brain_prompt`：`context`(L1) 之后、决策指令之前插 `long_term_memory` 段。

**为何不在 `state` 里加 `long_term_memory` 字段**：
- L2 记忆是**只读注入**（agent 不改它），不像 L1 `memory` 有 `append_list` reducer 累加。
- 每轮 invoke 前由 engine 检索后注入 system_prompt 即可，无需进 state schema（避免与 L1 `memory` 字段混淆，且不引入 reducer 复杂度）。

**检索时机**：engine 在 `ainvoke` 前（`registry.py:830` 拼 state 字段处 / `coordinator` 拼 conversation 处）调 `retrieve_relevant_memories(current_message, scope_filter)` → 注入 system_prompt。

### 6.3 注入边界

- **不改 L1**：`coordinator.py:963 memory[-8:]` 和 `worker._build_context_from_db` **原样保留**。
- **token 预算**：L2 段总长截断 ~500 字符（top_k=5 + content 截断），避免挤占 L1 上下文。
- **空记忆零注入**：无记忆时 system_prompt 不变，行为退化到现状。

---

## 7. API 设计预览（17b 落地）

路由 `APIRouter(prefix="/api/memory", tags=["memory"])`，参考 `api/mcp.py` / `api/usage.py` 模式：

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/memory` | 列记忆（按 scope / scope_ref / keyword 筛选） |
| `POST` | `/api/memory` | 手动新增记忆（用户在前端录入） |
| `PUT` | `/api/memory/{id}` | 编辑记忆 content / importance / enabled |
| `DELETE` | `/api/memory/{id}` | 删除记忆 |
| `POST` | `/api/memory/search` | 检索（FTS5 MATCH，返回 top_k） |
| `POST` | `/api/memory/extract` | 手动触发抽取（从某会话抽，调试用） |

Pydantic 模型放 `models/memory.py`（`Memory` / `MemoryCreatePayload` / `MemorySearchResult`），`models/__init__.py` 注册导出。

---

## 8. 风险与约束

| 风险/约束 | 应对 |
|---|---|
| [[use-open-source-not-handrolled]] 检索/抽取不手搓 | v1 FTS5 是 SQLite 自带（非手搓算法）；v2 向量检索评估 mem0/letta 不自研 |
| [[engines-use-frameworks-not-handrolled]] 记忆引擎不自研 | 不自研记忆引擎框架；抽取/检索用库能力（FTS5 / 未来 mem0） |
| [[persistence-db-vs-session-file-2026-07-26]] 加表边界 | 加 `memories` 表是新功能核心数据，非查询便利，不违反该决策（见 §4.5） |
| 记忆爆库 | 去重合并（§5.2）+ importance 衰减 + 软删除 |
| 注入灌爆 prompt | top_k=5 + ~500 字符截断（§6.3） |
| 抽取 LLM 成本 | v1 规则版（零 LLM 调用）；v2 用便宜模型 |
| 中文 FTS5 分词 | `unicode61` 够用（MVP）；v2 评估 jieba |
| 与 L1 混淆 | §3 边界表 + L2 不进 state schema（§6.2）物理隔离 |

---

## 9. 分档路线

| 档 | 范围 | 任务 |
|---|---|---|
| **档一（本文档·设计先行）** | 分层模型 + schema + 生命周期 + 检索方案 + 注入落点 | ✅ 任务17a（本任务） |
| **档二（MVP 开发）** | `MemoryEntity` + FTS5 表 + `/api/memory` CRUD + 规则版抽取 + 全文检索 + coordinator/worker prompt 注入 + 前端记忆管理页 | 任务17b（后端）+ 任务17c（前端） |
| **档三（语义检索·后续）** | 向量 embedding + 语义去重合并 + LLM 抽取 + importance LLM 评估 | 留 v2，依赖评估 mem0/letta/sqlite-vss |

---

## 10. 关联

- 现有会话记忆单真源：`engine/group_runtime.py:255,821`(`_memory` append) + `engine/coordinator.py:963-964`(memory 拼进 prompt) + `engine/worker.py:139`(`_build_context_from_db` 从 messages 表拉)。
- prompt 拼装落点：`llm/prompts.py:96`(`build_brain_prompt`) + `llm/prompts.py:341`(`build_coordinator_prompt`) + `engine/coordinator.py:128`(`_leader_system`) + `engine/registry.py:876`(`sys_for_invoke`)。
- 实体模式参考：`store/entities.py`(`McpConnectionEntity` / `SkillEntity` / `ScheduledTaskEntity` 同型)。
- API 模式参考：`api/mcp.py` / `api/usage.py`。
- 前端占位：`src/components/SettingsModal.tsx:256`(memory nav `<Empty>`，NavKey='memory' 已就绪)。

[[persistence-db-vs-session-file-2026-07-26]]（加表边界）· [[use-open-source-not-handrolled]]（检索/抽取用开源）· [[engines-use-frameworks-not-handrolled]]（记忆引擎不自研）· [[group-graph-runtime-state]]（现有会话记忆 `_memory` 边界）· [[overnight-batch-plan-2026-07-26]]（任务17 明细设计真源）

# 技术栈与能力实现映射

> 本文回答一个问题：**这个项目的每一项能力，是用什么技术/框架 API 实现的，落在哪个文件。**
> 读这份文档能对应到代码。所有行号已核实（截至 2026-07-23，master 分支）。

---

## 0. 技术栈总览

### 0.1 依赖清单（`backend/requirements.txt`）

| 用途 | 依赖 | 说明 |
|---|---|---|
| Web 框架 | `fastapi` / `uvicorn[standard]` | ASGI 后端 |
| 配置 | `python-dotenv` | 加载项目根 `.env` |
| 持久化 | `sqlalchemy` / `aiosqlite` | 异步 ORM + SQLite 异步驱动 |
| HTTP 客户端 | `httpx` | LLM 直调 / 探测 / 远端技能市场 |
| 编排框架 | `langgraph` | StateGraph / Command / Send / interrupt / MemorySaver |
| Swarm | `langgraph-swarm` | `create_handoff_tool`（仅作声明式 registry） |
| 模型绑定 | `langchain-openai` / `langchain-core` | `ChatOpenAI` / `@tool` / `BaseMessage`（execute 重路径） |
| MCP | `mcp` / `langchain-mcp-adapters` | `MultiServerMCPClient` |
| 定时 | `apscheduler` / `croniter` | `AsyncIOScheduler` + cron 解析 |

### 0.2 三进程分层

```
Electron 桌面壳（electron/main.ts）
  ├── spawn Python 后端（uvicorn main:app :8000 / PyInstaller exe）
  └── BrowserWindow → 加载 React 前端（dev: Vite :5173 / prod: dist/index.html）

前端 Renderer（src/）
  └── HTTP fetch + WebSocket → localhost:8000（无 Electron IPC）

后端 FastAPI（backend/）
  ├── REST + WebSocket 路由（api/）
  ├── LangGraph 引擎层（engine/）— Coordinator 调度 + Worker create_react_agent
  ├── 事件总线（events/bus.py）— WebSocket per-group 推送
  └── SQLite 持久化（store/）+ 外部 LLM（httpx）
```

### 0.3 框架原生 vs 自研（全局速查）

| 维度 | 框架原生 API | 自研逻辑 |
|---|---|---|
| ReAct 装配 | `create_react_agent` | — |
| 流式 | `astream_events(version="v2")` | 事件→`on_log`/`on_event` 投射、`ContentExtractor` JSON 状态机 |
| 检查点 | `MemorySaver`/`aget_state`/`GraphRecursionError` | 递归恢复兜底 |
| 群图装配 | `StateGraph`/`START`/`END`/`MemorySaver`/`add_conditional_edges` | 节点业务、route_entry 分叉 |
| Swarm handoff | `create_handoff_tool`（声明式） | handoff 实际由 `Command(goto=)` 驱动 |
| 计划中断 | `interrupt()`/`Command(resume=)` | contextvar 桥接 workaround |
| DAG 调度 | `Send` fan-out | `apply_fail_fast`/`find_ready_steps` 全自研 |
| 模型(langgraph 路径) | `ChatOpenAI` | 13-key active cache 适配 |
| 模型(httpx 路径) | — | `chat_completion`/`_stream` + reasoning 归一化 |
| 受控工具 | `@tool`/`BaseTool` | bash denylist(60 条)、`safe_path`、按技能沙箱绑定、manifest 去重 |
| MCP | `MultiServerMCPClient`/`get_tools` | 配置组装、失败隔离、自省 |
| 沙箱隔离 | — | 目录 cwd 限制 + 路径校验 + denylist（非容器） |
| 持久化 | `create_async_engine`/`AsyncSession`/`Mapped` | WAL PRAGMA、加列迁移、Path C drop/recreate |
| 实时推送 | FastAPI `WebSocket` | `BusManager` fan-out + 13 投影器 + 背压超时 |
| 定时 | `AsyncIOScheduler` + 三 trigger | fire 回调复用 `push_task` |

---

## 1. 桌面壳：Electron 拉起后端 + 窗口

**文件**：`electron/main.ts`（编译产物 `dist-electron/main.js`）

| 能力 | 技术 | 位置 |
|---|---|---|
| 跨平台数据目录 | `app.getPath('userData')` → 写入 `process.env.MULTI_AGENT_DATA_DIR` | `main.ts:13-15` |
| WSL2/Linux 输入法与白屏修复 | 设 `GTK_IM_MODULE/QT_IM_MODULE/XMODIFIERS=ibus` + `ozone-platform=x11` + `disableHardwareAcceleration()` | `main.ts:20-27` |
| dev 拉起后端 | `spawn('python3', ['-m','uvicorn','main:app','--host','127.0.0.1','--port','8000'], {cwd: backend})` | `main.ts:48-59` |
| prod 拉起后端 | `spawn(PythonInstaller exe, [...])`（`packagedServerName()` 返回 `multi-agent-server[.exe]`） | `main.ts:61-69` |
| 等待就绪 | `http.get('/health')` × 30 次重试 × 500ms | `main.ts:81-109` |
| 创建窗口 | `new BrowserWindow({contextIsolation:true, nodeIntegration:false})` | `main.ts:111-122` |
| 加载内容 | dev `loadURL(VITE_DEV_SERVER_URL)` + 开 DevTools；prod `loadFile(dist/index.html)` | `main.ts:124-129` |
| 生命周期 | `whenReady`→起 Python→`waitForPythonReady`→`createWindow`；`before-quit`→`killPython`（SIGTERM→500ms→SIGKILL） | `main.ts:136-174` |
| 日志管道 | `proc.stdout/stderr.on('data')` 转发到 electron console | `main.ts:72-79` |

**关键**：**无 Electron IPC**——`ipcMain/ipcRenderer/contextBridge` 全仓 0 处，前端与后端纯 HTTP + WebSocket 到 `localhost:8000`。

---

## 2. 后端框架：FastAPI 装配

**文件**：`backend/main.py`

| 能力 | 技术 | 位置 |
|---|---|---|
| lifespan 启动 | `@asynccontextmanager` → `init_db()` → `registry.load_from_store()` → `load_schedule()` | `main.py:35-45` |
| lifespan 关闭 | `await shutdown_scheduler()` → `await registry.shutdown_all()` | `main.py:47-49` |
| FastAPI 实例 | `FastAPI(title=..., version="0.2.0", lifespan=lifespan)` | `main.py:52` |
| CORS | 放行 `localhost:5173`/`127.0.0.1:5173`/`file://` | `main.py:54-63` |
| 路由注册 | 11 个 router：system/agents/groups/conversations/tasks/messages/skills/mcp/scheduled_tasks/websocket/plan | `main.py:65-75` |
| `.env` 加载 | 在 `config.py:33-35`（`load_dotenv(PROJECT_ROOT/.env)`），config 被 import 时触发 | `config.py:33-35` |

**启动序列**：建表+种子 → 建常驻引擎（每 agent 一个 `asyncio.Task` + per-group `GroupRuntime`）→ 重建 APScheduler jobs。

---

## 3. 持久化层

### 3.1 SQLite 连接（`backend/store/database.py`）

| 能力 | 技术 | 位置 |
|---|---|---|
| 异步引擎 | `create_async_engine("sqlite+aiosqlite:///{DB_PATH}", connect_args={check_same_thread:False}, pool_pre_ping=True)` | `database.py:177-182` |
| DB 路径 | `DATA_DIR/data.db`（`DATA_DIR` 来自 `config.py`） | `database.py:27` |
| WAL 模式 | 同步 `sqlite3.connect` 执行 `PRAGMA journal_mode=WAL` + `foreign_keys=ON`（WAL 是文件级持久属性，import 时设一次） | `database.py:31-52, 172` |
| 加列迁移 | `_migrate_schema()` 用 `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` 给老库补列 | `database.py:70-169` |
| Path C 重命名迁移 | `messages`/`tasks` 仍有 `group_id` 无 `conversation_id` 时 DROP + 同步 `create_engine` 重建 | `database.py:106-160` |
| 会话工厂 | `async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)` | `database.py:186-190` |
| 建表 | `Base.metadata.create_all`（非手写 DDL） | `database.py:231-232` |

**表清单**（11 张，`__tablename__`）：`agents`/`groups`/`conversations`/`members`/`tasks`/`messages`/`mcp_connections`/`skills`/`scheduled_tasks`/`scheduled_task_runs`/`llm_providers`。

### 3.2 ORM 实体（`backend/store/entities.py`）

SQLAlchemy 2.0 声明式 ORM（`DeclarativeBase` + `Mapped`/`mapped_column`），**非手写 SQL、非 dataclass**。

- JSON 列：`mapped_column(JSON, ...)`——SQLAlchemy 序列化为 SQLite TEXT 存 JSON，Python 侧原生 list/dict。
- 时间戳：ISO8601 字符串（`String` 列，`_now_iso()` 默认值）。
- bool 列：用 `Integer` 存（`enabled`/`installed`/`is_active`）。
- 关键实体：`AgentEntity`(:29)、`GroupEntity`(:59)、`ConversationEntity`(:72，单聊独立实体)、`MessageEntity`(:142，`conversation_id` 关联)、`TaskEntity`(:116)、`SkillEntity`(:185，含 `requires_tools`/`triggers`/`outputs` frontmatter)、`McpConnectionEntity`(:160)、`LlmProviderEntity`(:257)。

### 3.3 CRUD 层（`backend/store/crud.py`）

全走 ORM（`select`/`db.add`/`db.delete`/`db.execute(delete(...))`/`db.get`/`db.commit`），**无手写 INSERT/SELECT/UPDATE/DELETE**（唯一 raw SQL 在 `database.py` 的 PRAGMA/ALTER）。每个函数内部 `async with SessionLocal() as db:` 开会话。

- Agent/Group/Conversation/Member/Task/Message/Skill/MCP/ScheduledTask/LlmProvider 各一套 CRUD。
- **级联删除**：`delete_group`(:284-299) 与 `delete_conversation`(:367-389) 都删 `MemberEntity`/`TaskEntity`/`MessageEntity`。
- `delete_mcp_connection`(:1121-1136) 从 agents 的 `mounted_mcp` 摘除。
- `_provider_to_model`(:1387) 的 `api_key` 经 `config._mask_key` 脱敏，不回原文。
- `_select_model`(:1475-1496) 5 级 fallback 选活跃模型：is_default → 匹配 legacy model → 首条目录 → legacy `model` 列 → `_DEFAULT_MODEL`。

### 3.4 种子数据（`backend/store/seed.py`）

首次启动（`agents` 表空时）插演示数据：3 skills + 3 agents（协调者/前端/后端）+ 1 group + 3 members + 1 task + 1 message。`seed_demo_data()` (:23-212)。

---

## 4. 实时事件推送

### 4.1 WebSocket 端点（`backend/api/websocket.py`）

单端点 `/ws/bus/{group_id}`：`accept()` → `bus_manager.subscribe(group_id, ws)` → `while: receive_text()`（入站忽略）→ `WebSocketDisconnect` → `finally: unsubscribe`。**无心跳/重连**（后端侧），重连由前端负责。

### 4.2 事件总线（`backend/events/bus.py`）

`BusManager`：进程内内存 `dict[str, set[WebSocket]]` 按 `group_id`（或单聊 `conversation_id`，同一通道键）分通道 fan-out。**不是 SSE、不是 app.emit/pubsub、不是 redis**——纯 `await ws.send_json(event_data)`。

| 能力 | 技术 | 位置 |
|---|---|---|
| subscribe/unsubscribe | `dict.setdefault(set).add/discard` | `bus.py:46-55` |
| emit fan-out | `for ws in list(conns): await asyncio.wait_for(ws.send_json(...), timeout=5.0)` | `bus.py:79-98` |
| 背压兜底 | `WS_SEND_TIMEOUT=5.0`，慢客户端超时即 prune | `bus.py:33, 85-87` |
| 错误处理 | `except Exception: logger.debug(exc_info)` + prune（非 exception 防流式 per-token 洪水） | `bus.py:88-98` |
| 全局单例 | `bus_manager = BusManager()` | `bus.py:101` |

### 4.3 13 个事件投影器（DomainEvent → BusEventData）

把领域事件投成总线事件 dict，全在 `bus.py`：

| 投影器 | 事件类型 | 位置 |
|---|---|---|
| `emit_message_added` | `message_added` | `:106-128` |
| `emit_task_dispatched` | `task_dispatch` | `:131-157` |
| `emit_task_completed` | `task_complete`/`task_failed`（带 artifact） | `:160-197` |
| `emit_task_log` | `task_log` | `:200-214` |
| `emit_task_tool` | `task_tool`（工具生命周期） | `:220-246` |
| `emit_task_think` | `task_think`（推理/最终答） | `:249-270` |
| `emit_task_token` | `task_token`（**per-token 流式**，PL-08） | `:273-302` |
| `emit_agent_status` | `agent_status`（idle/executing/offline） | `:305-330` |
| `emit_coordinator_plan` | `coordinator_plan`（DAG 计划） | `:333-352` |
| `emit_coordinator_think` | `coordinator_think` | `:355-375` |
| `emit_coordinator_token` | `coordinator_token`（协调者 per-token 流式，按 `reply_id` 归并） | `:378-406` |
| `emit_coordinator_reasoning` | `coordinator_reasoning`（reasoning model 的 `reasoning_content` 流式） | `:409-441` |
| `emit_coordinator_stats` | `coordinator_stats`（elapsed_ms/tokens/reasoning_tokens，~200ms 节流） | `:444-493` |

---

## 5. LLM 调用（双路径）

### 5.1 httpx 直调路径（coordinator/worker brain 决策用）

**文件**：`backend/llm/client.py`（全自研，httpx 直连 OpenAI 兼容端点）

| 能力 | 技术 | 位置 |
|---|---|---|
| 配置读取 | `get_llm_config()` 委托 `config.get_config()`（snake→camelCase） | `client.py:19-43` |
| 非流式 | `chat_completion()` httpx POST `/chat/completions` + Bearer + `extraHeaders` 合并 | `client.py:46-93` |
| SSE 流式 | `chat_completion_stream()` body 加 `stream:True` + `stream_options:{include_usage:True}`，`async with client.stream("POST",...)` + `async for line in resp.aiter_lines()` 解析 `data:` 前缀，`[DONE]` 终止 | `client.py:96-218` |
| 协议归一化 | yield 四元组 `(content_delta, reasoning_delta, completion_tokens, reasoning_tokens)`；`reasoning_delta` 兼容 `delta.reasoning_content`（OpenAI/DeepSeek）优先，回退 `delta.reasoning`（kimi/new-api） | `client.py:208-218` |
| 连接级 kwarg | `requestTimeout`→httpx timeout(120s)、`proxy`→httpx proxy、`extraHeaders`→合并 headers | `client.py:75-79` |

### 5.2 ChatOpenAI 路径（execute 重路径用）

**文件**：`backend/engine/agent_loop.py`

| 能力 | 技术 | 位置 |
|---|---|---|
| 模型实例化 | `langchain_openai.ChatOpenAI`（传 model/base_url/api_key/temperature + 连接级 kwarg） | `agent_loop.py:40, 201-236` |
| 代理 | httpx 读 `HTTP_PROXY/HTTPS_PROXY` 环境变量 | `agent_loop.py:232-235` |
| ReAct 装配 | `langgraph.prebuilt.create_react_agent`（选它而非 `create_agent`，因后者非流式） | `agent_loop.py:43, 269-274` |
| 流式订阅 | `agent.astream_events(..., version="v2")`，订阅 `on_tool_start`/`on_tool_end`/`on_chat_model_stream`/`on_chat_model_end`/`on_chain_end` | `agent_loop.py:305-419` |
| 检查点 | `MemorySaver()` + `thread_id`（task_id 或 uuid4） + `recursion_limit = max_turns*2+4` | `agent_loop.py:273, 282, 288` |
| 递归恢复 | `GraphRecursionError` 捕获后 `agent.aget_state(config)` 读检查点恢复 | `agent_loop.py:420-452` |

**两条路径分工**：coordinator/worker 的 brain 决策走 httpx 直调（要 JSON 决策 + reasoning 流式）；execute 重路径（worker 跑工具循环）走 `ChatOpenAI` + `create_react_agent`（要 LangGraph 的 ToolMessage 自动回喂 + 流式工具事件）。

### 5.3 流式解码状态机

`backend/llm/json_stream.py` 的 `ContentExtractor`——JSON-aware 状态机解码。coordinator/worker 的 LLM 常返回 JSON（`{action, content, plan}`），`ContentExtractor` 只把 `content` 字段值增量推 WS（`emit_*_token`），跳过 JSON 骨架。`coordinator.py:1859-1873` 别名保留。

### 5.4 模型服务商目录

**文件**：`backend/llm_provider_catalog.py`（静态目录）+ `backend/llm/probe.py`（httpx 探测）

- 静态目录 `_CATALOG`（7 预设：OpenAI/DeepSeek/Anthropic/Kimi/智谱/Qwen/Ollama），无网络依赖、air-gapped 可用。`list_catalog()`/`get_catalog(slug)`。
- `probe.test_provider(entity)`：发 1-token `"ping"`，`time.perf_counter()` 测延迟，从不 raise。`:25-144`
- `probe.fetch_models(entity)`：GET `/models` 解析 `{"data":[{"id":...}]}`，按 `model_id` 去重 + 排序 + 首个标 `is_default`。`:172-310`

---

## 6. 多智能体引擎核心

### 6.1 双轨注册表（`backend/engine/registry.py`）

```
AgentRegistry（全局单例 registry.py:1470）
├── _engines: dict[group_id, dict[agent_id, AgentEngine]]   # execute 路径（驻留引擎 + asyncio.Queue inbox）
└── _runtimes: dict[group_id, GroupRuntime]                 # orchestration 路径（编译群图）
```

| 能力 | 技术 | 位置 |
|---|---|---|
| 双轨数据结构 | `_engines` + `_runtimes`（自研） | `registry.py:1042-1044` |
| 驻留 AgentEngine | 每引擎一个 `asyncio.Queue` inbox + `_run_loop`（`wait_for(inbox.get(), timeout=1.0)`） | `registry.py:146-181` |
| 选图 | `is_coordinator = agent_id == coordinator_id` → `build_coordinator_graph()` 或 `build_worker_graph()` | `registry.py:131-136` |
| MemorySaver 键 | `thread_id = f"{group_id}:{agent_id}"` | `registry.py:143` |
| notify 处理 | plan_resume 走 `Command(resume=...)`，其余走 fresh-input dict；每 notify 现读 `auto_confirm`/`leader_strategy` | `registry.py:659-832` |
| worker 执行 | `_run_worker_task` 调 `execute_agent_task`，report-back 走 `rt.invoke_turn(incoming_kind="agent_reply")` | `registry.py:329-500` |
| on_log 回调 | tool_start/end→`emit_task_tool`；token→`emit_task_token`；think/answer→`emit_task_think` | `registry.py:343-373` |
| MT-17 超时看门狗 | `asyncio.create_task(_watch())` sleep `timeout` 后 cancel `_worker_task` | `registry.py:536-594` |
| PL-11 取消 | `request_cancel(task_id)` → `self._worker_task.cancel()` | `registry.py:308-327` |
| reset_session | `aupdate_state(values=None, as_node=END)` resolve dangling interrupt | `registry.py:918-1004, 981-985` |
| 懒建群运行时 | `ensure_runtime(group_id)` → `GroupRuntime(group)` + `compile_graph()` | `registry.py:1046-1071` |
| 懒建单聊引擎 | `ensure_engine(conversation_id, agent_id)`（Path C） | `registry.py:1083-1131` |
| 重编译群图 | `recompile_group(group_id)`（collaboration_mode 切换/成员增删后，先 `cancel_turn` 防 race） | `registry.py:1133-1186` |
| 启动加载 | `load_from_store()` 遍历所有 group 建 engine + runtime，第二遍遍历单聊 ConversationEntity | `registry.py:1299-1370` |

### 6.2 群图状态 schema（`backend/engine/state.py`）

`GroupState`（TypedDict, total=False）——单图共享状态，去中心化 swarm 拓扑。

| 字段 | reducer | 用途 | 位置 |
|---|---|---|---|
| `messages` | `add_messages`（框架原生，按 id 去重，resume-safe） | 共享消息日志 | `state.py:207` |
| `dispatch_plan` | `replace_value`（自研，last-write-wins） | coordinator DAG 计划 | `state.py:213` |
| `memory` | `append_list`（自研） | 共享回合记忆 | `state.py:261` |
| `recent_speakers` | `append_list`（自研） | 本回合发言顺序（防同 agent 连发） | `state.py:217` |
| `turn_count` | 无（last-value channel） | 本回合 handoff 计数 | `state.py:216` |
| `current_speaker` | 无 | 当前发言者 | `state.py:210` |
| `converge` | 无 | @收束标志 | `state.py:228` |
| `action_taken`/`reply_content`/`_stream_stats` | 无 | coordinator 子节点控制通道 | `state.py:280-294` |
| `auto_confirm`/`leader_strategy`/`collaboration_mode` | 无 | group config 每回合注入 | `state.py:230-239` |

自研 reducer 三件套：`append_list`(:23)、`merge_dict`(:28)、`replace_value`(:35)。

**关键约束**：`turn_count`/`current_speaker`/`converge` 是 last-value channel（无 reducer），多节点同 superstep 写会触发 `InvalidUpdateError`——worker 的 `is_dispatch_fanout` 守卫就是为此。

### 6.3 群图装配（`backend/engine/group_graph.py`）

`build_group_graph(group, members, coordinator_id)` (:641-826) 装配 `START→route_entry→{coordinator subgraph | agent_<id> 节点}→END`。

| 能力 | 技术 | 位置 |
|---|---|---|
| StateGraph | `StateGraph(GroupState)` | `:729` |
| 入口边 | `g.add_edge(START, "route_entry")` | `:779` |
| 编译 | `g.compile(checkpointer=MemorySaver())` | `:811` |
| swarm handoff 声明 | `create_handoff_tool(agent_name=..., name=transfer_to_agent_<id>, ...)`（**仅作声明式 registry**，未绑 tool-calling agent，`handoff_destinations()` 读 `tool.metadata["__handoff_destination"]` 校验） | `:51, 92-104, 108-123` |
| handoff 边 | **全动态 `Command(goto="agent_<peer>")`**——agent 节点之间无静态边 | `:705-707` |
| coordinator 子图（中心化） | `build_coordinator_subnodes` 注册 7 节点 | `:745-760` |
| agent 节点 | `worker.build_agent_node(...)` 闭包绑定身份，节点名 `agent_<agent_id>`（下划线分隔，因 LangGraph 禁 `:` `|`） | `:765-775` |
| 条件边 | classify→{dispatch_next_group, handle_reply_group, llm_decide}；llm_decide→{chat, dispatch}；dispatch→{dispatch_next_group, END} | `:786-804` |
| chat→END 静态边 | — | `:809` |

### 6.4 route_entry 分叉（`backend/engine/group_graph.py`）

START 后第一个节点，决定「谁先开口 / 走中心化还是去中心化」。

| 能力 | 技术 | 位置 |
|---|---|---|
| 返回 Command | `Command(goto="classify")`（中心化）/ `Command(goto="agent_<id>")`（去中心化）/ `Command(goto=END)` | `:482-638`（生产 closure 版） |
| 中心化判定 | `_looks_central`：按 `_CENTRAL_KINDS` + 计划确认关键词启发式 | `:138-175` |
| report-back 分叉 | `_is_report_back`：`agent_reply` 有 task_id=中心化回报 / 无=去中心化 peer handoff | `:178-206` |
| 会话封顶守卫 | `rt.is_session_capped()` 命中即 `Command(goto=END)` | `:352-357, 529-534` |
| 去中心化裸消息 | 群主当首发 `Command(goto=agent_<coordinator_id>)`（对标 swarm `default_active_agent`） | `:446-469, 607-630` |
| @群主死胡同修复 | centralized 模式 `@群主` 走 `Command(goto="classify")` | `:209-252, 428, 591` |

### 6.5 GroupRuntime：回合边界 + 可中止性（`backend/engine/group_runtime.py`）

`GroupRuntime` 类（每群一个）。

| 能力 | 技术 | 位置 |
|---|---|---|
| 编译群图 | `compile_graph(members)` 调 `build_group_graph` | `:287-326` |
| 回合 invoke | `await self._graph.ainvoke(turn_input, config)` | `:784` |
| resume 计划 | `await self._graph.ainvoke(Command(resume=payload), config)`（**复用上一回合 thread**，不 mint 新 thread） | `:894` |
| reset_session | `aupdate_state(values=None, as_node=END)` resolve dangling interrupt | `:954-958` |
| 回合=可取消 Task | `asyncio.create_task(coro)` + `task.cancel()` 硬停（Option B 移除软停层） | `:484-510, 411-445` |
| 回合串行锁 | `self._turn_lock: asyncio.Lock` 包整个 turn body（防 report-back 与 in-flight turn 并发写 last-value channel） | `:210, 755, 879` |
| 会话发言封顶 | `SESSION_SPEECH_CAP=50`（env 可调），`is_session_capped()`/`record_speech()` 跨回合累加 | `:100, 448-481` |
| per-turn fresh-thread | `_next_thread_id()` 每回 `{thread_id}:{seq}` 新 thread（避免 reducer 跨回合累积） | `:655-666` |
| cross-turn 驻留镜像 | `self._memory`/`self._dispatch_plan` 作 fresh thread 的 initial state 注入（非 thread 累积） | `:255-256, 600-653` |
| contextvar 注入 | `worker.set_group_runtime(self)` + `set_reply_callback`（每 ainvoke 前） | `:773-781` |

### 6.6 coordinator 子图（`backend/engine/coordinator.py`）

中心化路径，7 节点。

| 节点 | 能力 | 技术 | 位置 |
|---|---|---|---|
| `classify` | 三分叉 confirm_dispatch/handle_reply/llm_decide | httpx 直调 LLM + 残留 interrupt 探测 | `:282-367` |
| `llm_decide` | 流式拉 LLM，解析四态决策 chat/dispatch/ask/continue | `_stream_coordinator_decision`（httpx）+ `_parse_coordinator_decision` | `:947-1068` |
| `chat` | 持久化 + emit | `_unified_reply` | `:1071-1085` |
| `dispatch` | **LangGraph 原生 `interrupt({"plan": plan})`**（auto_confirm=True 跳过） | `interrupt()` + `_runnable_config_ctx` contextvar 桥接（Py3.10 + langgraph workaround） | `:1088-1191, 1180-1181, 101-117` |
| `dispatch_next` | 驻留路径派工 | `dispatch_ready_steps`（push_task 到 inbox） | `:1194-1241` |
| `dispatch_next_group` | **GROUP twin，LangGraph `Send` fan-out** | `build_dispatch_sends` 返回 `(sends, dispatched)` → `Command(goto=sends, update={...})` | `:1244-1381, 1381` |
| `handle_reply_group` | GROUP twin 接收 agent 节点 in-graph 报告 | 复用 `_maybe_handle_step_failure`/`_maybe_adjust_remaining_steps` | `:1426-1559` |
| `summarize_group` | GROUP twin 收尾 | `Command(goto=END, update={"dispatch_plan": []})` | `:1562-1591` |

- 路由函数（读 `state["action_taken"]`）：`route_after_classify`(:1597)、`route_after_llm_decide`(:1627)、`route_after_dispatch`(:1637) 等。
- 流式 LLM：`_stream_coordinator_decision`(:1876-2009) 消费 `chat_completion_stream`，用 `ContentExtractor` 只把 `content` 字段值增量推 `emit_coordinator_token`(:1963)，reasoning 推 `emit_coordinator_reasoning`(:1945)，stats ~200ms 节流 `emit_coordinator_stats`(:1975)。
- MT-14 步骤调整：`_maybe_adjust_remaining_steps`(:449-570)；MT-15 失败恢复：`_maybe_handle_step_failure`(:777-923，retry/reassign/skip/keep_failed，`MAX_RETRY_ATTEMPTS=2`)。
- ContextVar 三件套（自研，并发隔离）：`_REPLY_CB`(:55)、`_GRAPH_INSTANCE`(:65)、`_PENDING_PLAN_VIEW`(:74)。

### 6.7 worker agent 节点（`backend/engine/worker.py`）

| 能力 | 技术 | 位置 |
|---|---|---|
| 驻留 worker 图 | `StateGraph(WorkerState)` 4 节点 brain/chat/execute/ask + `route_brain` + `MemorySaver` | `:404-422` |
| 群图 agent 节点工厂 | `make_agent_node` 返回 `Command`；`build_agent_node` 用 `functools.partial` 闭包绑定身份 | `:589-919, 922-967` |
| contextvar 隔离 | `_REPLY_CB`/`_GROUP_RUNTIME`（每 task copy context，并发不串台） | `:50-52, 70-72` |
| `is_dispatch_fanout` 守卫 | `incoming_kind == "coordinator_task"` 时禁止写 `turn_count`/`current_speaker`（防 Send fan-out 多节点同 superstep 写 last-value channel 撞 `InvalidUpdateError`） | `:680, 704-705, 870-883` |
| 防连发守卫 | `agent_id in recent_speakers` 命中直接 `Command(goto=END)` | `:694-706` |
| 会话封顶二次守卫 | `rt.is_session_capped()` → `Command(goto=END)`（只挡闲聊/handoff，不挡 fan-out） | `:718-726` |
| @收束守卫 | `converge` 命中强制 `next_speaker=None`（回一句即 END 不 handoff） | `:899-905` |
| handoff 解析 | `_resolve_handoff_target` 复用 `mention.find_mentions`/`resolve_mention` | `:485-563` |
| handoff 上限 | `AGENT_NODE_MAX_HANDOFFS=8`（per-turn 链长护栏） | `:482` |
| 流式 brain | `_stream_brain_decision`（httpx + `ContentExtractor` + `emit_task_token`/`emit_coordinator_reasoning`） | `:180-302` |
| brain 决策解析 | `_parse_brain_decision` 三态 chat/execute/ask，`extract_json` 失败用 `ContentExtractor().extract_final` 兜底 | `:425-464` |
| 消息累加 | `AIMessage(content=..., name=agent_name, id=msg_id)` 写 `GroupState.messages`（经 `add_messages` reducer） | `:25, 867` |
| 技能注入 | `mounted_skills` 闭包绑定 + `_compose_skill_prompt` 拼进 system prompt | `:742-774` |
| execute 透传 dispatch_task_id | 让 report-back 携带 dispatch 侧 `task_` id（否则 step match miss → 计划死锁） | `:830-834` |

### 6.8 DAG 调度（`backend/engine/dispatcher.py`）

**全自研，无框架依赖。**

| 能力 | 技术 | 位置 |
|---|---|---|
| DAG fail-fast 级联 | `apply_fail_fast(plan)` while 循环到 fixpoint，pending step 的 `depends_on` 命中 failed step 则级联标 failed | `:50-82` |
| ready 步骤查询 | `find_ready_steps(plan)`：pending + 所有 `depends_on` 已 completed | `:85-102` |
| 驻留 fan-out | `dispatch_ready_steps`：每 ready step mark `dispatched` → 派发 announce → `push_task` → `emit_task_dispatched` | `:155-178` |
| 群图 twin fan-out | `build_dispatch_sends` 返回 `(list[Send], dispatched)`，每 `Send(agent_node_target(agent_id), {incoming_kind:"coordinator_task", ...})`，DAG 语义与驻留路径 byte-for-byte 一致 | `:191-282` |

### 6.9 A2A 收件箱（`backend/engine/inbox.py`）

**每 (group_id, agent_id) 一个 `asyncio.Queue`**（单消费者 per-agent，非 mpsc）。

| 能力 | 技术 | 位置 |
|---|---|---|
| 数据结构 | `_inboxes: dict[tuple[str,str], asyncio.Queue]` + `_task_queues`/`_notify_queues`（group→list）+ `_lock` | `:19-24` |
| 惰性建 | `get_inbox(group_id, agent_id)` | `:31-36` |
| push_task | 构造 TaskQueueItem（`id=f"tq_{uuid4().hex}"`），append（截断 2000）+ `await inbox.put` | `:49-82` |
| push_notify | 构造 NotifyQueueItem（`id=f"nq_..."`），broadcast 时遍历该 group 所有 inbox 投递 | `:85-120` |
| claim_task | 找第一个 `receiver_id==agent_id and status==pending`，标 `claimed` | `:123-135` |
| complete_task | 标 completed/failed，**不自动 push notify**（anti-double-notify，由 AgentEngine 推单一 `agent_reply`） | `:138-159` |
| cancel_task | PL-11 标 queued/pending task 为 `cancelled` | `:162-190` |

### 6.10 @mention 路由（`backend/engine/mention.py`）

| 能力 | 技术 | 位置 |
|---|---|---|
| 扫描 @mention | `find_mentions(content)` 扫 `@token`，遇终止标点停 | `:71-94` |
| 三层匹配 | `resolve_mention`：agent_id → name → role → alias 子串 | `:109-143` |
| 出站路由 | `route_mentions`：用 `push_notify`（非 `push_task`）走 brain→chat 轻路径 | `:146-238` |
| 30s 防循环 | `recent_routes[f"{sender}->{target}"]=now`，同方向 30s 内已路由跳过 | `:223-231` |
| 反向清键 | push 后 `pop(f"{target}->{sender}")`，允许 A→B→A→B 交替 | `:234` |
| 群级共享 dict | `_group_recent_routes`（原 per-engine dict 反向清键打不中对方 dict → 接龙 4 轮断） | `:53, 56-62` |
| A2A cap | `_A2A_CAP=50`（env 可调），达 cap 不再 push | `:41, 182-187` |
| 入站路由 | `route_user_message`：@mention 命中→`rt.invoke_turn(incoming_kind="agent_reply")` 去中心化 handoff；无→`incoming_kind="coordinator_reply"` 中心化 | `:241-345` |
| 双轨降级 | 无 runtime 时 fallback `push_notify` | `:317-319, 338-345` |
| @收束校验 | `converge=True` 但无 @mention → `raise ValueError`（API 转 400） | `:324-325` |
| 计划恢复 | `route_plan_resume` → `rt.resume_plan(payload)`（bypass inbox） | `:348-392` |

### 6.11 单聊路由（`backend/engine/direct.py`）

`route_direct_message(conversation_id, content)` (:32-68)：查 `ConversationEntity.agent_id` → `registry.ensure_engine(conversation_id, agent_id)` 懒建驻留 worker 图 → `push_notify(conversation_id, "coordinator_reply", "user", agent_id, content, None)`。单聊无群图、无协作面。

### 6.12 防死循环多层护栏总览

| 层级 | 护栏 | 位置 |
|---|---|---|
| per-turn handoff 链 | `AGENT_NODE_MAX_HANDOFFS=8` | `worker.py:482` |
| 图内 | `recent_speakers` 防同 agent 连发 | `worker.py:694-706` |
| 图内 | `is_dispatch_fanout` 守卫防多节点同 superstep 写 last-value channel | `worker.py:680` |
| 跨回合 | `SESSION_SPEECH_CAP=50` | `group_runtime.py:100` |
| @mention | 30s 反向清键 + `_A2A_CAP=50` | `mention.py:41, 223-234` |
| LangGraph | `recursion_limit = max_turns*2+4` | `agent_loop.py:282` |
| 协作式停止 | `cancel_turn()`（Option B 后只留硬切 + 50 封顶） | `group_runtime.py:411-445` |
| @收束 | `converge` 标志回一句即 END | `worker.py:899-905` |

---

## 7. 技能系统

### 7.1 技能模型（`backend/models/skill.py`）

`Skill` Pydantic 模型（`ConfigDict(extra="allow")`），关键字段：
- `content`：自然语言 skill body（注入 agent prompt）。
- `source`：`builtin|market|custom`。
- **frontmatter（Claude Skills 化）**：`requires_tools: list[str]`（受控工具名，非空→解析后 `bind_tools`；空=纯文档技能只走 prompt 注入）、`triggers`、`outputs`。三字段皆可选、默认空 list、向后兼容。`:36-38`

### 7.2 受控工具池（`backend/engine/tools.py`）

`langchain_core.tools.tool` 装饰的 `@tool` 闭包工厂。

| 工具组 | 工具 | 沙箱 | 位置 |
|---|---|---|---|
| 群工作区 `tools_for_group(group_id)` | `read_file`/`write_file`/`edit_file`/`list_dir`/`run_command`（5 个） | `engine.workspace.safe_path` | `:85-227` |
| 技能沙箱 `tools_for_skill(skill_id)` | `file_read`/`file_write`/`bash_run`（3 个） | `skill_assets.safe_skill_path` + cwd=技能 workspace | `:298-399` |

| 能力 | 技术 | 位置 |
|---|---|---|
| bash denylist | `_DANGEROUS_PATTERNS` **60 条**（删除/包管理/网络客户端/提权/设备/进程脱离/宿主内省/定时），`_is_dangerous` 大小写不敏感子串匹配 | `:47-82` |
| 命令执行 | `asyncio.create_subprocess_exec` + `wait_for(proc.communicate(), timeout)` + `proc.kill()`，输出截断 8000 字符 | `:187-203, 368-383` |
| 工具名注册表 | `SKILL_TOOL_NAMES = ("file_read","file_write","bash_run")` | `:238` |
| manifest 解析 | `resolve_skill_tools(manifest)` 按 `requires_tools` 查 `SKILL_TOOL_NAMES` → 绑**该技能自家** `DATA_DIR/skills/{skill_id}/workspace/` 沙箱，按名去重（首个赢）+ 未知工具 warning | `:252-295` |

**注意**：`run_command`（群工作区）**无 denylist**，仅 `bash_run`（技能沙箱）有——设计取舍。

### 7.3 沙箱隔离（`backend/engine/workspace.py` + `backend/store/skill_assets.py`）

**纯目录隔离**（cwd 限制 + 路径校验 + denylist），非容器、非受限 shell——代码注释明确承认这是 MVP 安全债。

| 能力 | 技术 | 位置 |
|---|---|---|
| 群工作区根 | `WORKSPACE_ROOT = DATA_DIR/workspaces` | `workspace.py:14` |
| 路径逃逸防护 | `safe_path(group_id, rel)`：`candidate = (ws/rel).resolve()`，`candidate.relative_to(ws.resolve())` 失败抛 `ValueError` | `workspace.py:35-55` |
| 产物扫描 | `scan_workspace_artifacts`：`rglob("*")`，`_MAX_DEPTH=4`/`_MAX_FILES=200`/`_SKIP_DIRS`（node_modules/.git 等），按 mtime 过滤，newest-first | `workspace.py:86-179` |
| 技能根 | `SKILLS_ROOT = DATA_DIR/skills` | `skill_assets.py:28` |
| 资产白名单子目录 | `_ASSET_SUBDIRS = ("scripts","templates")`（Claude Skills 约定） | `skill_assets.py:32` |
| 资产路径防护 | `safe_asset_path`：顶层 segment 必须在 `_ASSET_SUBDIRS` | `skill_assets.py:46-74` |
| 沙箱目录化 | `skill_workspace_path(skill_id)` 幂等建 `workspace/` + `workspace/output/` | `skill_assets.py:166-179` |
| 资产上限 | `_MAX_SINGLE_ASSET=1MB`/`_MAX_TOTAL_ASSETS=10MB` | `skill_assets.py:35-36` |

技能目录布局：`DATA_DIR/skills/{skill_id}/{scripts,templates,workspace/output}/`。

### 7.4 MCP 集成（`backend/engine/mcp_manager.py`）

| 能力 | 技术 | 位置 |
|---|---|---|
| MCP 客户端 | `langchain_mcp_adapters.client.MultiServerMCPClient` | `:22` |
| 配置组装 | `_build_client(configs)` 组装 `dict[str, dict]`，同名按 `_N` 去重 | `:33-45` |
| 加载工具 | `load_mcp_tools(mcp_ids)`：`crud.resolve_mcp_configs` → `_build_client` → 遍历 `await client.get_tools(server_name=name)`，单连接失败 `continue` | `:48-73` |
| 生命周期 | 每次建临时 client，拉完工具列表后 stdio 进程终止，调用时 adapter 重新 spawn | `:9-14` |
| 自省预览 | `list_mcp_tools(mcp_ids)` 返 `[{name, description}]` | `:76-86` |
| transport | stdio（`{command,args,env?}`）/ sse（`{url,headers?}`），配置来自 `crud._mcp_connection_config` | `crud.py:1028-1042` |

### 7.5 技能执行（`backend/engine/agent_loop.py`）

`run_skill_loop`（**纯函数、无全局状态**，test_vh53 D13 契约锁）——与 `run_agent_loop` 同款 `create_react_agent` + `astream_events`，但解耦群：`tools` 由调用方传入，`on_event` 是 SSE 投射回调，`on_event=None` 时 `_emit` 守卫静默。`:499-674`

### 7.6 技能→工具绑定链路

`Skill.requires_tools` 声明工具名 → `resolve_skill_tools(manifest)` 按名查 `SKILL_TOOL_NAMES` → `skill_tool_by_name` 调 `tools_for_skill(skill_id)` 闭包绑该技能自家 `workspace/`。注入点三处：群执行路径 `agent_executor.py:154-168`（累积进 `set_extra_tools`）、技能 run endpoint `api/skills.py:555-583`（直接传 `run_skill_loop`）、delegate 工具内拉起子 agent `agent_loop.py:710-730`。

### 7.7 技能→prompt 注入（`backend/engine/agent_executor.py`）

| 方式 | 实现 | 位置 |
|---|---|---|
| 全文注入（当前兜底） | `_compose_system_prompt`：`_SKILL_HEADER` + 每个 skill content 作 `### 技能 N` 块 | `:44-59` |
| 渐进式 manifest | `_compose_skill_manifest`（只 manifest 常驻）+ `_load_skill_full`（按需 load 全文），开关 `_SKILL_PROGRESSIVE=False` | `:62-93, 35` |

### 7.8 delegate 自派子智能体（deer-flow 借鉴，自研）

**文件**：`backend/engine/agent_loop.py`

worker 执行路径工具列表加一个 `delegate` `@tool`，按需调 `delegate(skill_id, subtask)` → 复用 `run_skill_loop` 拉起绑该 skill 受控工具池 + 独立沙箱的临时子 agent → 阻塞 `await` 子结果 → `return` 字符串 → LangGraph 自动包成 ToolMessage 回喂 worker。

| 能力 | 技术 | 位置 |
|---|---|---|
| contextvar 深度 | `_DELEGATE_DEPTH` + `_DELEGATE_MAX_DEPTH=2`（per-task copy，并发安全，镜像 `_REPLY_CB`/`_GROUP_RUNTIME` 模式） | `agent_loop.py:56-63` |
| 工厂 | `_build_delegate_tool(group_id, agent_name, task_id, on_log)` 闭包返回 `@tool delegate(skill_id, subtask)` | `:683-770` |
| 递归防死循环 | `token = _DELEGATE_DEPTH.set(depth+1)` + `finally: reset(token)`；仅 `depth==0` 注入 delegate（子 agent 经 `run_skill_loop` 自装配 tools 不带 delegate → 物理断递归） | `:192-193, 247, 732, 767-768` |
| 工具体流程 | depth 守卫 → `crud.get_skill` → `requires_tools` 校验 → 幂等建沙箱 → `resolve_skill_tools` → `run_skill_loop(on_event=None)` 阻塞 → 扫 `skill_output_path` 产物 → 拼 summary（含 `[产物]`）→ `on_log` tool_start/tool_end 透传 | `:698-768` |
| system prompt 指引 | `if _DELEGATE_DEPTH.get() == 0:` 条件性追加 delegate 使用指引 | `:247-257` |

**约束**：delegate 只活在执行路径 `run_agent_loop`（独立 asyncio task，不经群图守卫）；防死循环靠自己的 depth contextvar；不走 `_EXTRA_TOOLS` 全局（per-call 注入）；子 agent 流式 token 暂丢弃（只取结果）。test_vh60 锁此契约。

### 7.9 技能市场与模板

- **技能市场**（`backend/skill_hub.py`）：内置目录 `_CATALOG`（10 真实技能）+ 远端 hub overlay（读 `SKILL_HUB_URL` env，best-effort）。`search_market`/`get_market_entry`/`fetch_remote_entry_content`（SK-12 安装时拉远端全文，1MB 上限）。全自研 httpx + Pydantic。
- **角色模板**（`backend/agent_templates.py`）：`_CATALOG`（10 预设角色，含真实 system_prompt + skills）。`list_templates`/`get_template`。
- **prompt 拼装**（`backend/llm/prompts.py`）：`COORDINATOR_SYSTEM`/`build_brain_prompt`/`build_coordinator_prompt`（内嵌 `COORDINATOR_SYSTEM` + roster + `leader_strategy` + 严格 JSON 要求）/`build_plan_adjust_prompt`/`build_step_recovery_prompt`。

---

## 8. 计划确认闭环（PL-02/PL-03）

基于 LangGraph 原生 `interrupt`/`Command(resume=)`。

| 端点 | 后端 | 位置 |
|---|---|---|
| `GET /{group_id}/plan` | 从 GroupRuntime checkpointer 线程读 `dispatch_plan` | `api/plan.py:102-133` |
| `POST /{group_id}/plan/confirm` | 推 `plan_resume` notify `{"mode":"confirm"}` → 协调者转 `Command(resume=)` → `node_dispatch` 的 `interrupt()` 返回 payload，跳过 LLM 扇出 | `api/plan.py:190-209` |
| `POST /{group_id}/plan/direct` | 置 `config.auto_confirm=True` + 若有 pending 计划 `route_plan_resume({"mode":"direct"})` | `api/plan.py:212-241` |
| `POST /{group_id}/plan/modify` | 按 step 号合并修改（重置 pending/task_id=None）→ `emit_coordinator_plan` 重播 → `route_plan_resume({"mode":"modify","amended_steps":plan})` | `api/plan.py:244-298` |

- `interrupt({"plan":plan})` 在 `coordinator.py:1180-1181`；`Command(resume=payload)` 在 `group_runtime.py:894`。
- Python 3.10 + langgraph 1.2.5 的 contextvar 桥接 workaround 在 `_runnable_config_ctx`（`coordinator.py:101-117`）。

---

## 9. 定时任务（APScheduler）

**文件**：`backend/engine/scheduler.py`（进程内单例 `AsyncIOScheduler`）

| 能力 | 技术 | 位置 |
|---|---|---|
| 调度器 | `AsyncIOScheduler`（惰性 `get_scheduler()` 启动） | `:31, 34-41` |
| cron 触发 | `CronTrigger.from_crontab(...)` | `:57` |
| 一次性 | `DateTrigger(run_date=...)` | `:59` |
| 间隔 | `IntervalTrigger(seconds=...)`（<=0 兜底 1 小时） | `:61-64` |
| 注册 job | `sched.add_job(_fire, trigger=..., args=[task_id], id=f"sched_{task_id}", replace_existing=True)` | `:118-124` |
| fire 回调 | `push_task(group_id, "scheduler", agent_id, f"[定时任务:{name}] {content}", {...})`——**复用驻留 AgentEngine 同一条 agentic loop**（不另起执行路径） | `:71-109` |
| 启动重建 | `load_from_store()` 逐条 `add_job` | `:147-156` |
| 关闭 | `shutdown(wait=False)` | `:44-50` |
| 立即执行 | `_fire(task_id, force=True)`（跳过 enabled 检查） | `:71, 60-66`（API） |

`croniter` 是 `CronTrigger` 的 cron 表达式解析依赖（间接用）。调度器在 `main.py:44` lifespan 启动。

---

## 10. 前端

### 10.1 入口与主题

| 能力 | 技术 | 位置 |
|---|---|---|
| React 入口 | `createRoot` + `StrictMode` | `src/main.tsx:13-17` |
| AntD 主题 | `ConfigProvider` token `colorPrimary:'#F26522'`（品牌橙，2026-07-23 由蓝迁橙）、`borderRadius:6` | `src/App.tsx:31-38` |
| Provider 嵌套 | `ConfigProvider → SettingsProvider → BusEventProvider → SelectionProvider → Layout` | `src/App.tsx:39-45` |
| 路由 | **无 react-router**——视图切换由 `Layout` 内 `Segmented` 控制（`chat`/`agent`/`skill`） | `src/components/Layout.tsx:76-84` |

### 10.2 通信：HTTP fetch + WebSocket（非 IPC）

**文件**：`src/services/api.ts`

| 能力 | 技术 | 位置 |
|---|---|---|
| HTTP 基座 | `fetch(url, {method, headers, body: JSON.stringify})` + `!resp.ok` 抛错 + `JSON.parse(resp.text())` | `:236-259` |
| API 命名空间 | `agentApi`/`conversationApi`/`groupApi`/`planApi`/`taskApi`/`messageApi`/`skillApi`/`mcpApi`/`scheduledTaskApi`/`systemApi`/`configApi`/`providerApi`/`slashApi` | 各段 |
| 文件下载 | `fetch(url).blob()`（绕过 `http<T>`） | `groupApi.downloadFile :406-412` |
| 技能上传 | `FormData`（multipart） | `skillApi.upload :633-655` |
| WebSocket | `new WebSocket('ws://localhost:8000/ws/bus/{groupId}')` | `onBusEvent :1399` |
| WS 重连 | `onclose` 指数退避 `Math.min(1000*2**retry, 16000)`，`MAX_RETRIES=5` | `:1423-1434` |
| 技能运行 SSE | 手写 `fetch` + `resp.body.getReader()` + `TextDecoder` 解析 `text/event-stream`（非 `EventSource`） | `skillApi.run :705-764` |

**全仓 `src/` 无 `ipcRenderer`/`contextBridge`**——纯 HTTP + WS。

### 10.3 实时事件 hook

**文件**：`src/hooks/useBusEvent.ts` + `src/contexts/BusEventContext.tsx`

| 能力 | 技术 | 位置 |
|---|---|---|
| 共享 WS | `BusEventProvider` 调一次 `useBusEvent(groupId)`，全应用共享一条 WS | `BusEventContext.tsx:76-111` |
| 事件分流 | `ws.onmessage` 按 `d.type` 分流：`task_token`→`streaming[task_id]`、`coordinator_token`→`coordStreaming[reply_id]`、`coordinator_reasoning`→`coordReasoning`、`coordinator_stats`→`coordStats`、`agent_reply`落地清流式缓冲 | `useBusEvent.ts:301-614` |
| 批量 flush | events/logs 攒进 ref，~50ms `setTimeout` flush 一次（防 setState 风暴） | `:163-204` |
| 重连重拉 | `handleReconnect`：`systemApi.listStatus` + `refreshPlan` + `messageApi.listByGroup` 重建历史 | `:248-286` |

### 10.4 流式渲染

| 能力 | 技术 | 位置 |
|---|---|---|
| 流式气泡 | `streamingBubbles`/`coordinatorStreamingBubbles` 渲染 `<ChatMessageBubble isStreaming>` | `ChatPanel.tsx:558-592, 1309-1373` |
| 闪烁光标 | `.chat-bubble--streaming` 描边 + `<span className="chat-streaming-cursor">` | `ChatMessageBubble.tsx:490-495` |
| 推理折叠区 | `reasoning` prop 经 `Collapse`「思考过程（N tokens）」，流式期展开、正文开始流时收起 | `ChatMessageBubble.tsx:340-379` |
| 工具摘要行 | `toolEvents` 按 phase start/end 配对算耗时 | `ChatMessageBubble.tsx:435-487` |
| 产物下载卡 | `artifactFiles` 按扩展名图标 + 下载按钮 | `ChatMessageBubble.tsx:508-536` |
| 状态行 | Claude-Code 风格 `model · Ns · ↓ N tokens（含 N 推理）· thinking` | `ChatMessageBubble.tsx:541` |

### 10.5 ReactFlow 用途

**唯一使用文件**：`src/pages/TaskPage.tsx`

`buildNodesEdges(tasks)`：Task→Node（position/label/状态色边框），`dependencies`→Edge（animated）。`<ReactFlow fitView>` + `MiniMap`/`Controls`/`Background`，包在 `Card title="任务依赖图"`。`:21-23, 82-111, 219-224`

### 10.6 透明化面板

| 组件 | 消费 | 位置 |
|---|---|---|
| `LeaderPanel` | `events`+`plan`：思考链 `coord_think` + 协作计划 step 徽标 + 派工时间线（antd `Timeline`） | `LeaderPanel.tsx` |
| `WorkerTrace` | `events`/`agentStatuses`/`streaming`：状态徽标 + 工具卡片 + 思考文本 + 流式 token 浅橙块 | `WorkerTrace.tsx` |

### 10.7 计划确认卡

**文件**：`src/components/PlanConfirmCard.tsx`——三动作（confirm/directRun/modify），行内编辑 `instruction` + `Select mode="multiple"` 编辑 `depends_on`，依赖校验（非空/不依赖自身/依赖必须存在），只读进度模式随 `coordinator_plan` 实时翻色。`:66-157, 266-316`

### 10.8 群信息抽屉与协作模式

**文件**：`src/components/GroupInfoDrawer.tsx`

- 协作模式 `Segmented`：`centralized`（群主主导，supervisor 子图拆计划派工）/ `decentralized`（纯 swarm，裸消息群主当首发）。`:722-728`
- `handleUpdateGroup`：合并 `group.config` 与 `leader_strategy` + `collaboration_mode` 整体传 `groupApi.update`。`:258-290`
- 群主不可移除防御。`:206-210`

### 10.9 角色色

`getAgentColor`（`ChatPanel.tsx:85-107`）：`ROLE_COLORS` snake_case 主键（backend_engineer:#6366f1 / frontend_engineer:#06b6d4 / qa_engineer:#f59e0b / devops_engineer:#10b981 / product_manager:#f43f5e），coordinator 用紫 `#722ed1`，未知默认 `#8b5cf6`。这些是身份色，正交于品牌橙，全保留。

### 10.10 TTS 语音朗读（纯前端）

**文件**：`src/lib/tts.ts` + `src/hooks/useTts.ts`——Web Speech API（`window.speechSynthesis` + `SpeechSynthesisUtterance`），零 npm 依赖、零后端改动。`SettingsContext` 用 `localStorage`（key `ma.tts`）持久化偏好。ChatPanel 自动朗读新 `agent_reply`（按 id 去重防重读）。

---

## 11. 单聊分实体（Path C）

单聊从 Group 实体独立：独立 `ConversationEntity` + 严格改名 `MessageEntity.group_id`/`TaskEntity.group_id` → `conversation_id`（不保留字段名），共享 Message/流式/ChatPanel。开发期数据可弃（直接 drop+recreate 表，不写迁移脚本）。6 commit + ensure_engine 懒建修复已 push。详见记忆 `single-chat-entity-split-c2-2026-07-23`。

---

## 12. 关键调用链（一句话版）

- **写消息**：`api/messages.py:send_message` → `crud.create_message` → `emit_message_added`（WS fan-out）→ `route_user_message`/`route_direct_message` → 常驻 `AgentEngine` → worker 流式 `on_chat_model_stream` → `emit_task_token` → `bus_manager.emit` → `ws.send_json`。
- **定时任务**：`api/scheduled_tasks.py:create` → `crud.create` + `scheduler.add_job` → APScheduler 到点回调 `_fire` → `inbox.push_task` → 同一 `AgentEngine` inbox → 同一 agentic loop。
- **计划恢复**：`api/plan.py:plan_confirm` → `route_plan_resume` → 协调者 `_handle_notify` → `Command(resume=)` → `node_dispatch` 的 `interrupt()` 返回 payload → 扇出 pending 步骤。
- **delegate 子智能体**：worker turn 内 `delegate(skill_id, subtask)` → `run_skill_loop`（绑该 skill 沙箱）阻塞 await → 扫产物 → summary 回喂 worker 模型。

---

## 13. 协作模式

`collaboration_mode`（`centralized` 默认 / `decentralized`）走 `auto_confirm` 同款 config 链路（model→state→每回合现读注入）。route_entry 按 mode 分流：去中心化裸消息群主当首发（对标 swarm `default_active_agent`）、@群主 合法 handoff；中心化维持 classify。切换 mode 触发 `recompile_group` 重编译群图。单聊不进群图，mode 对单聊无意义。详见记忆 `collaboration-mode-centralized-decentralized-2026-07-23`。

---

## 附：硬约束（实现时遵守）

- worker 不调 Claude Code CLI（与 CLI 解耦，worker = 框架内 LLM+LangGraph 智能体）。
- 引擎用框架不自研（`create_react_agent`/APScheduler/langchain-mcp-adapters）；DAG 调度 `apply_fail_fast`/`find_ready_steps` 是业务逻辑非引擎件。
- LangGraph 原生术语（interrupt/Command(resume=)/route_entry/handoff/Send fan-out/END），无自创比喻。
- 不动 `group_graph.py`/`coordinator.py`/`collaboration_mode`（群图锁定）。
- delegate 只活在执行路径 `run_agent_loop`，不经群图守卫，防死循环靠 depth contextvar。

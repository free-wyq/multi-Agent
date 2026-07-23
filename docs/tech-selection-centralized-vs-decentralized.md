# 技术选型：中心化 vs 去中心化多智能体编排范式

> 本文档记录业界主流多智能体编排框架在「中心化 / 去中心化」上的设计范式，作为本项目架构决策的技术参考。核心结论先行：**去中心化模式群里不应有群主概念，群主是中心化范式的专属角色。** 一个产品同时需要两种范式时，业界做法是「图级二选一 + 入口层分流」，而非在一张图里混用。

## 1. 三种控制流范式

去中心化程度的分水岭是**「下一个发言者由谁决定」**。三种范式正好覆盖从去中心化到中心化的频谱：

| # | 范式 | 下一个发言者 | 群主角色 | 代表框架 | API |
|---|---|---|---|---|---|
| ① | **纯 Swarm handoff** | 上一个发言者交出（回复里 @谁 / 调哪个 handoff tool） | **无 supervisor 概念** | LangGraph Swarm · OpenAI Agents SDK | `create_swarm` / `create_handoff_tool` |
| ② | **Selector GroupChat** | selector 函数选（LLM / round-robin / 自定义），selector 自身不发言 | manager 默认静默，只在被选/需要时介入 | AutoGen `GroupChat` + `Selector` | `selector_func` + 组合式 `TerminationCondition` |
| ③ | **Hierarchical Supervisor** | supervisor 节点编排全程 | supervisor 常驻节点，每轮参与路由/汇总 | LangGraph Supervisor · CrewAI hierarchical | `create_supervisor`（中心化专用，与 swarm 互斥） |

### ① 纯 Swarm handoff（最彻底去中心化）

无中央调度者，话筒由上一个发言者交出——谁发言谁决定下一棒。

- 机制：`create_handoff_tool(agent_name=...)` 声明合法交接目标 + `Command(goto=目标节点, update={...})` 运行时交接。
- 下一个发言者 = 当前 agent 回复里 @了谁（或调用了哪个 handoff tool）。
- **无群主**：没有 supervisor 节点。谁被 @ 谁接话，谁不被 @ 话筒就落地 END。`create_supervisor` 和 swarm 是**两套独立互斥 API**，不混用。
- 终止：靠外部条件（max_turns / 用户停 / 会话封顶），组合式 TerminationCondition。

**LangGraph Swarm 实际 API**（本项目已装 `langgraph-swarm==0.1.0`）：

```python
from langgraph_swarm import create_swarm, create_handoff_tool, SwarmState

# 1) 声明合法交接目标（每个目标一个 tool）
handoff_to_b = create_handoff_tool(agent_name="backend_engineer")

# 2) 把 agent 绑上 handoff tool，create_swarm 装配成无中心图
graph = create_swarm(
    agents=[agent_a, agent_b],
    default_active_agent="agent_a",       # 第一棒
    state_schema=SwarmState,
)
```

三个本质特征：**无中央调度者**（只有定第一棒的 `default_active_agent`，之后靠 handoff 不回任何 supervisor）/ **发言权靠 handoff 边传递** / **终止是组合外部条件**。

### ② Selector GroupChat（半去中心化）

有 selector 函数选下一个发言者，但 selector 自身不发言只做路由决策。

- 机制：`GroupChat` + `selector_func`（可 LLM 选 / round-robin / 自定义）。
- selector 可以是 LLM——那就接近「动态选人」，但它**不是 agent**，只做路由。
- 群主角色：可有一个 agent 充当 manager（`GroupChatManager`），但默认不发言，只在需要时介入；AutoGen v0.4 也支持 `selector_func` 指向某 agent 作为「主持人」。
- 终止：组合式 `TerminationCondition`，**OR 语义**任一命中即停：

```python
termination = (
    MaxMessageTermination(50)            # 消息数封顶
    | TextMentionTermination(["停"])     # 关键词停
    | ExternalTermination()              # 外部信号停（按钮）
)
```

### ③ Hierarchical Supervisor（中心化）

显式 supervisor 节点编排全程：拆解任务 → 派给 worker → 收回报告 → 汇总。supervisor 是图里的常驻节点，worker 是它的子节点。

- 机制：`langgraph-supervisor` 包的 `create_supervisor`（或 CrewAI 的 hierarchical process）。
- supervisor 既路由**又发言**（发布任务、收尾汇总）。
- 群主角色：supervisor = 群主，常驻每轮参与。
- 终止：supervisor 判定全任务完成。

```python
from langgraph_supervisor import create_supervisor  # 需装 langgraph-supervisor（本项目未装）

graph = create_supervisor(
    agents=[backend, frontend, tester],
    model=...,                  # supervisor 用 LLM 做路由+汇总
    prompt="你是群主，负责拆任务派工汇总",
)
```

## 2. 关键对照：群主在去中心化里的角色

业界在「群主在去中心化里算什么」上有**两条干净的路**，没有「群主在去中心化图里留后门」这种混法：

| 设计 | 群主在去中心化里 | 代表 |
|---|---|---|
| **纯 swarm（无群主）** | 完全没有 supervisor 概念，谁 @ 谁 | LangGraph Swarm / OpenAI Agents SDK |
| **supervisor + worker 两层（群主常驻）** | supervisor 是常驻节点，每轮都参与路由/汇总 | LangGraph Supervisor / CrewAI hierarchical |
| **selector（可选主持人）** | manager 默认静默，只在被选/需要时介入 | AutoGen Selector |

**大厂共性：一个系统选一种范式，不混。** LangGraph 官方明确把 `create_supervisor`（中心化）和 Swarm handoff（去中心化）做成**互斥的两套独立 API**——要 supervisor 就全 supervisor，要 swarm 就全 swarm，不在一张 swarm 图里塞个 supervisor 后门。

## 3. 大厂如何解决「同产品双范式」

一个产品同时需要两种范式是常态（成语接龙要 swarm、工程交付要 supervisor）。业界解法是**「图级二选一 + 入口层分流」**：

| 框架 | 双范式解法 |
|---|---|
| **LangGraph** | 两张独立图，入口层按消息特征选走哪张。supervisor 图管工程交付，swarm 图管对话接龙。**不在一张图里混。** |
| **AutoGen** | `selector_func` 运行时动态决定要不要让 manager 介入——但 manager 在不在图里是**图装配期**定的，不是路由期。 |
| **CrewAI** | hierarchical（有 manager）vs sequential（无 manager）两种 process，任务级二选一。 |

对照到产品入口：判断一条消息走哪张图，依据是**消息形态**——

| 用户消息形态 | 走哪 | 判据 |
|---|---|---|
| @成员 | 去中心化 swarm 图 | 有 @（显式 opt-in 对话） |
| 裸工程/计划确认 | 中心化 supervisor 图 | 工程意图线索 / plan_resume |
| 裸闲聊无 @ | 取决于产品定位（swarm END 或 supervisor 兜底） | 需单独决策 |

## 4. 终止条件设计对照

终止机制也是范式的一部分，业界用**组合式条件 OR 语义**：

| 入口 | 作用域 | 对标业界 |
|---|---|---|
| UI 停止按钮（硬切 task） | 单回合硬切 | AutoGen `ExternalTermination` / OpenAI `max_turns` 外部中断 |
| handoff 链封顶（per-turn） | 单回合内 | OpenAI `max_turns` / LangGraph `recursion_limit` |
| 会话发言总量封顶 | 跨回合 | AutoGen `MaxMessageTermination` |
| 关键词停（软停） | 入站 | AutoGen `TextMentionTermination` |

业界组合写法：`MaxMessageTermination(50) | TextMentionTermination(["停"]) | ExternalTermination()`——OR 语义，任一命中即停。本项目 Option B 后删了关键词软停层，保留硬切 + per-turn 封顶 + 跨回合封顶三入口。

## 5. 本项目当前对齐情况

本项目去中心化路径**对齐范式 ①（纯 Swarm handoff）**，符合「引擎用框架不自研」约束：

| 范式 ① 落地项 | 本项目代码 |
|---|---|
| `create_handoff_tool` 声明合法目标 | `engine/group_graph.py` `_build_handoff_tools` |
| `Command(goto=目标)` 交接话筒 | `engine/worker.py` `make_agent_node` |
| 解析 @mention 决定下一棒 | `engine/worker.py` `_resolve_handoff_target` |
| 无静态 inter-agent 边（handoff 动态） | `build_group_graph` 不加静态 agent 间边 |
| 终止组合条件 | `cancel_turn`（ExternalTermination）+ `AGENT_NODE_MAX_HANDOFFS=8`（max_turns）+ `SESSION_SPEECH_CAP=50`（MaxMessageTermination） |

## 6. 已知偏离点（按大厂范式衡量）

> 本节是对照业界范式发现的本项目偏离，**非已实现**，记录供后续架构决策参考。

1. **去中心化群图内塞了群主 classify 入口**——`route_entry` 给 `coordinator_reply` kind / 裸工程线索 goto `classify`。纯 swarm 范式里 swarm 图无 supervisor，LangGraph 官方 swarm 示例无此「无 @ 回退 supervisor」边。这是范式混用的代码味道。

2. **`@群主` 死胡同**——`_resolve_handoff_target` 跳过 coordinator。纯 swarm 里要么把 supervisor 注册成正常 handoff 目标（member 可 @ 求助），要么不存在。「跳过」是折中产物，既不纯 swarm（跳了）也不纯 supervisor（没常驻）。

3. **`route_entry` 一函数干两个范式**——既做 swarm 的「第一棒 handoff」又做 supervisor 的「工程需求进群主」。AutoGen `selector_func` 单一职责（只选人），LangGraph Supervisor 的路由是 supervisor 节点自己的事。两范式塞进一个路由函数是混用信号。

## 7. 选型结论（技术参考）

- **去中心化 = 纯 Swarm（范式 ①），群里无群主概念**——群主是中心化 supervisor 范式的专属角色，去中心化模式下不应存在。本项目走 `langgraph-swarm` 原生 `create_handoff_tool` + `Command(goto)`，不自研。
- **中心化 = Supervisor（范式 ③）**——群主常驻、拆计划/派工/收报告/收尾。
- **同产品双范式 = 图级二选一 + 入口层分流**（参考 LangGraph 官方范式），不在一张图里混用 swarm + supervisor。
- **终止 = 组合式条件 OR 语义**（参考 AutoGen TerminationCondition），硬切 + 封顶 + （可选）关键词。

## 参考

- LangGraph Swarm — `langgraph-swarm`（`create_swarm` / `create_handoff_tool` / `SwarmState`）
- LangGraph Supervisor — `langgraph-supervisor`（`create_supervisor`，与 swarm 互斥）
- AutoGen — `GroupChat` + `Selector` + 组合式 `TerminationCondition`
- CrewAI — hierarchical vs sequential process
- 本项目落地对照：`backend/engine/group_graph.py`、`backend/engine/worker.py`、`backend/engine/coordinator.py`、`backend/engine/group_runtime.py`

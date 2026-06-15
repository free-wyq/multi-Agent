"""
群主 LangGraph 状态图定义

状态图流程：
    analyze → decompose → dispatch ↔ monitor → summarize

- analyze:    接收用户需求，分析意图和涉及的角色
- decompose:  将需求拆解为子任务 DAG
- dispatch:   按拓扑序派发无依赖任务，创建 Task 记录，发布 task_dispatch 消息
- monitor:    监听任务完成事件（消息总线驱动），推进下游任务
- summarize:  所有任务完成后，汇总结果
"""
import asyncio
from datetime import datetime, timezone
from typing import Literal

from langgraph.graph import END, START, StateGraph

from app.coordinator.llm import get_intent_analyzer, get_summarizer, get_task_decomposer
from app.coordinator.state import CoordinatorState
from app.core.database import async_session
from app.models.agent_definition import AgentDefinition
from app.models.group import Group
from app.models.group_member import GroupMember
from app.models.task import Task
from app.services import task_service

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession


# ── 任务事件追踪（monitor 节点用）────────────────────────────────

_task_events: dict[str, asyncio.Event] = {}


def _get_task_event(task_id: str) -> asyncio.Event | None:
    return _task_events.get(task_id)


def _clear_task_event(task_id: str) -> None:
    _task_events.pop(task_id, None)


async def _handle_task_event(message: dict) -> None:
    """Bus handler：收到 task_complete/task_failed 时设置 asyncio.Event"""
    task_id = message.get("task_id")
    if not task_id:
        return
    event = _task_events.get(task_id)
    if event:
        event.set()


# ── 节点函数 ────────────────────────────────────────────────────────


async def analyze(state: CoordinatorState) -> dict:
    """意图分析节点：理解用户需求，识别涉及的角色"""
    # 获取群组内可用角色列表
    available_roles = await _get_group_roles(state["group_id"])
    roles_hint = ""
    if available_roles:
        roles_hint = "\n\n可用角色（必须从中选择）：\n" + "\n".join(
            f"- {r['role']}: {r['name']}" for r in available_roles
        )

    analyzer = get_intent_analyzer()
    result = await analyzer.ainvoke(
        f"分析以下需求，识别涉及的智能体角色：\n\n{state['requirement']}"
        f"{roles_hint}\n\n"
        f"注意：involved_roles 必须是角色标识（如 backend-engineer），不要使用中文描述。"
    )
    return {
        "intent_analysis": result.analysis,
        "involved_roles": result.involved_roles,
    }


async def decompose(state: CoordinatorState) -> dict:
    """任务拆解节点：将需求拆解为子任务 DAG"""
    decomposer = get_task_decomposer()

    # 获取群组内可用角色列表
    available_roles = await _get_group_roles(state["group_id"])
    roles_context = ""
    if available_roles:
        roles_context = "\n".join(
            f"- {r['role']}: {r['name']}" for r in available_roles
        )
    else:
        roles_context = _build_roles_context(state["involved_roles"])

    prompt = (
        f"用户需求：{state['requirement']}\n\n"
        f"意图分析：{state['intent_analysis']}\n\n"
        f"可用角色（assigned_role 必须使用角色标识）：\n{roles_context}\n\n"
        f"请将需求拆解为子任务，指定每个任务的执行角色和依赖关系。\n"
        f"注意：assigned_role 必须是角色标识（如 backend-engineer），不要使用中文描述。\n"
        f"注意：depends_on 是前置子任务的 0-based 序号，第1个子任务的序号是0。"
    )
    result = await decomposer.ainvoke(prompt)

    # 将 SubTaskDef 列表转换为 state 中的 subtasks 格式
    subtasks = []
    for st in result.subtasks:
        # 规范化角色标识：如果 LLM 返回了中文描述，尝试模糊匹配
        role_id = _normalize_role(st.assigned_role, available_roles)
        subtasks.append({
            "title": st.title,
            "description": st.description,
            "assigned_agent_id": role_id,
            "dependencies": st.depends_on,
        })

    # 构建 DAG 可视化结构
    dag_nodes, dag_edges = _build_dag_structure(subtasks)

    return {
        "subtasks": subtasks,
        "dag_nodes": dag_nodes,
        "dag_edges": dag_edges,
    }


async def dispatch(state: CoordinatorState) -> dict:
    """调度派发节点：按 DAG 拓扑序创建 Task 记录，派发无依赖任务，发布 task_dispatch 消息"""
    group_id = state["group_id"]
    subtasks = state["subtasks"]
    pending_task_ids = list(state.get("pending_task_ids", []))
    running_task_ids = list(state.get("running_task_ids", []))

    async with async_session() as db:
        # 解析角色 → 智能体 ID 映射
        role_to_agent_id = await _resolve_role_mapping(db, group_id, state["involved_roles"])

        # 为所有子任务创建 Task 记录
        task_id_map: dict[int, str] = {}  # 子任务序号 → task ID
        for idx, st in enumerate(subtasks):
            assigned_role = st["assigned_agent_id"]
            agent_id = role_to_agent_id.get(assigned_role)

            # 解析依赖：子任务序号 → 真实 task ID
            dep_ids = [task_id_map[d] for d in st.get("dependencies", []) if d in task_id_map]

            task = await task_service.create_task(db, {
                "group_id": group_id,
                "title": st["title"],
                "description": st["description"],
                "assigned_agent_id": agent_id,
                "dependencies": dep_ids,
                "status": "submitted",
                "dag_order": idx,
            })
            task_id_map[idx] = task.id

        # 派发无依赖的任务（状态改为 working）
        for idx, st in enumerate(subtasks):
            if not st.get("dependencies"):
                task_id = task_id_map[idx]
                await task_service.update_task(db, task_id, {"status": "working"})
                running_task_ids.append(task_id)

    # 发布 task_dispatch 消息到消息总线
    try:
        from app.bus.core import get_bus, CHANNEL_PREFIX
        bus = get_bus()
        channel = f"{CHANNEL_PREFIX}{group_id}"
        for idx, st in enumerate(subtasks):
            if not st.get("dependencies"):
                task_id = task_id_map[idx]
                await bus.publish_and_persist(
                    channel,
                    group_id=group_id,
                    task_id=task_id,
                    sender_id="coordinator",
                    receiver_id=st.get("assigned_agent_id", "broadcast"),
                    type="task_dispatch",
                    content=f"任务已派发: {st['title']}",
                    data={"task_id": task_id, "agent_id": st.get("assigned_agent_id")},
                )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Failed to publish task_dispatch events: %s", exc)

    return {
        "pending_task_ids": pending_task_ids,
        "running_task_ids": running_task_ids,
    }


async def monitor(state: CoordinatorState) -> dict:
    """状态监控节点：监听消息总线事件，等待任务完成，推进下游任务

    基于 asyncio.Event 的事件驱动模式：
    - 为每个 running task 创建 Event，等待任一触发
    - Bus handler 收到 task_complete/task_failed 时 set Event
    - 30 秒超时兜底（防止消息丢失时无限等待）
    - 事件触发后从 DB 读取权威状态并推进下游任务
    """
    group_id = state["group_id"]
    running_task_ids = list(state.get("running_task_ids", []))
    completed_task_ids = list(state.get("completed_task_ids", []))
    failed_task_ids = list(state.get("failed_task_ids", []))
    pending_task_ids = list(state.get("pending_task_ids", []))

    if not running_task_ids:
        return {
            "running_task_ids": running_task_ids,
            "completed_task_ids": completed_task_ids,
            "failed_task_ids": failed_task_ids,
            "pending_task_ids": pending_task_ids,
        }

    # 订阅 bus handler（幂等，已订阅则不重复）
    try:
        from app.bus.core import get_bus, CHANNEL_PREFIX
        bus = get_bus()
        channel = f"{CHANNEL_PREFIX}{group_id}"
        await bus.subscribe(channel, _handle_task_event)
    except Exception:
        pass  # bus 不可用时仍可走超时兜底

    # 为 running tasks 创建/复用 Event
    events: dict[str, asyncio.Event] = {}
    for tid in running_task_ids:
        if tid not in _task_events:
            _task_events[tid] = asyncio.Event()
        events[tid] = _task_events[tid]

    # 等待任一任务事件触发（30s 超时兜底）
    done, pending_waits = await asyncio.wait(
        [e.wait() for e in events.values()],
        timeout=30.0,
        return_when=asyncio.FIRST_COMPLETED,
    )
    # 取消未完成的等待
    for task in pending_waits:
        task.cancel()

    # 从 DB 读取权威状态
    async with async_session() as db:
        for tid in list(running_task_ids):
            task = await task_service.get_task(db, tid)
            if not task:
                continue
            if task.status == "completed":
                running_task_ids.remove(tid)
                completed_task_ids.append(tid)
                _clear_task_event(tid)
            elif task.status == "failed":
                running_task_ids.remove(tid)
                failed_task_ids.append(tid)
                _clear_task_event(tid)
            elif task.status == "input-required":
                # 子智能体请求澄清，暂时标记但不移出 running
                pass

        # 检查是否有新的可派发任务
        ready_tasks = await task_service.get_ready_tasks(db, group_id)
        for rt in ready_tasks:
            if rt.id not in running_task_ids and rt.id not in completed_task_ids:
                await task_service.update_task(db, rt.id, {"status": "working"})
                running_task_ids.append(rt.id)
                # 发布下游任务的 task_dispatch 事件
                try:
                    from app.bus.core import get_bus, CHANNEL_PREFIX
                    bus = get_bus()
                    channel = f"{CHANNEL_PREFIX}{group_id}"
                    await bus.publish_and_persist(
                        channel,
                        group_id=group_id,
                        task_id=rt.id,
                        sender_id="coordinator",
                        receiver_id=rt.assigned_agent_id or "broadcast",
                        type="task_dispatch",
                        content=f"下游任务已派发: {rt.title}",
                        data={"task_id": rt.id},
                    )
                except Exception:
                    pass

    return {
        "running_task_ids": running_task_ids,
        "completed_task_ids": completed_task_ids,
        "failed_task_ids": failed_task_ids,
        "pending_task_ids": pending_task_ids,
    }


async def summarize(state: CoordinatorState) -> dict:
    """结果汇总节点：所有子任务完成后，生成汇总报告"""
    group_id = state["group_id"]
    artifacts = []
    summaries = []

    async with async_session() as db:
        tasks = await task_service.list_tasks_by_group(db, group_id)
        for t in tasks:
            if t.artifact_path:
                artifacts.append({
                    "task_id": t.id,
                    "title": t.title,
                    "path": t.artifact_path,
                    "description": t.result_summary or "",
                })
            if t.result_summary:
                summaries.append(f"- {t.title}: {t.result_summary}")

    # 用 LLM 生成自然语言汇总
    summarizer = get_summarizer()
    summary_text = await summarizer.ainvoke(
        f"以下是各子任务的执行结果：\n\n"
        + "\n".join(summaries)
        + f"\n\n请用简洁的语言汇总整体执行结果。原始需求：{state['requirement']}"
    )

    return {
        "summary": summary_text.content if hasattr(summary_text, "content") else str(summary_text),
        "artifacts": artifacts,
    }


# ── 条件边 ──────────────────────────────────────────────────────────


def should_continue(state: CoordinatorState) -> Literal["monitor", "summarize", "end"]:
    """判断调度走向：
    - 还有 running 任务 → 继续监控
    - 有 failed 且无 running → 汇总（含失败信息）
    - 全部 completed → 汇总
    """
    running = state.get("running_task_ids", [])
    completed = state.get("completed_task_ids", [])
    failed = state.get("failed_task_ids", [])
    subtask_count = len(state.get("subtasks", []))

    if not subtask_count:
        return "end"

    # 还有任务在跑
    if running:
        return "monitor"

    # 全部完成或有失败
    if len(completed) + len(failed) >= subtask_count:
        return "summarize"

    return "monitor"


# ── 图构建 ──────────────────────────────────────────────────────────


def build_coordinator_graph() -> StateGraph:
    """构建群主 LangGraph 状态图"""
    graph = StateGraph(CoordinatorState)

    # 添加节点
    graph.add_node("analyze", analyze)
    graph.add_node("decompose", decompose)
    graph.add_node("dispatch", dispatch)
    graph.add_node("monitor", monitor)
    graph.add_node("summarize", summarize)

    # 定义边
    graph.add_edge(START, "analyze")
    graph.add_edge("analyze", "decompose")
    graph.add_edge("decompose", "dispatch")
    graph.add_conditional_edges(
        "dispatch",
        should_continue,
        {
            "monitor": "monitor",
            "summarize": "summarize",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "monitor",
        should_continue,
        {
            "monitor": "monitor",
            "summarize": "summarize",
            "end": END,
        },
    )
    graph.add_edge("summarize", END)

    return graph


# ── 辅助函数 ────────────────────────────────────────────────────────


def _build_roles_context(involved_roles: list[str]) -> str:
    """构建角色上下文描述"""
    role_descriptions = {
        "frontend-engineer": "前端工程师 — 负责页面开发、组件实现、样式编写",
        "backend-engineer": "后端工程师 — 负责API开发、数据库操作、业务逻辑",
        "tester": "测试工程师 — 负责编写测试用例、执行测试、报告缺陷",
        "reviewer": "代码审查员 — 负责代码审查、质量把关、最佳实践建议",
        "devops": "运维工程师 — 负责部署、CI/CD、环境配置",
    }
    lines = []
    for role in involved_roles:
        desc = role_descriptions.get(role, f"{role} — 自定义角色")
        lines.append(f"- {role}: {desc}")
    return "\n".join(lines)


def _build_dag_structure(subtasks: list[dict]) -> tuple[list[dict], list[dict]]:
    """从子任务列表构建 DAG 节点和边（供前端 ReactFlow 渲染）"""
    nodes = []
    edges = []
    for idx, st in enumerate(subtasks):
        nodes.append({
            "id": f"task-{idx}",
            "label": st["title"],
            "agent_id": st["assigned_agent_id"],
            "status": "submitted",
        })
        for dep in st.get("dependencies", []):
            edges.append({
                "source": f"task-{dep}",
                "target": f"task-{idx}",
            })
    return nodes, edges


async def _resolve_role_mapping(
    db: AsyncSession, group_id: str, involved_roles: list[str]
) -> dict[str, str]:
    """将角色标识解析为群组内智能体定义的 ID

    返回 {role: agent_definition_id} 映射。
    """
    # 查询群组内所有成员
    result = await db.execute(
        sa.select(GroupMember.agent_id)
        .where(GroupMember.group_id == group_id)
    )
    member_ids = [row[0] for row in result.all()]

    if not member_ids:
        return {}

    # 查询成员的角色
    result = await db.execute(
        sa.select(AgentDefinition.id, AgentDefinition.role)
        .where(AgentDefinition.id.in_(member_ids))
    )
    role_map = {}
    for agent_id, role in result.all():
        role_map[role] = agent_id

    return role_map


async def _get_group_roles(group_id: str) -> list[dict]:
    """获取群组内可用角色列表

    Returns:
        [{"role": "backend-engineer", "name": "后端工程师", "id": "xxx"}, ...]
    """
    async with async_session() as db:
        result = await db.execute(
            sa.select(GroupMember.agent_id)
            .where(GroupMember.group_id == group_id)
        )
        member_ids = [row[0] for row in result.all()]
        if not member_ids:
            return []

        result = await db.execute(
            sa.select(AgentDefinition.id, AgentDefinition.role, AgentDefinition.name)
            .where(AgentDefinition.id.in_(member_ids))
        )
        return [
            {"id": row[0], "role": row[1], "name": row[2]}
            for row in result.all()
        ]


def _normalize_role(role_str: str, available_roles: list[dict]) -> str:
    """规范化 LLM 返回的角色标识

    LLM 可能返回 '后端开发智能体' 或 'Backend Developer Agent'，
    需要将其映射为标准角色标识如 'backend-engineer'。

    匹配优先级：
    1. 精确匹配 role 字段
    2. role 标识包含在返回字符串中
    3. 返回字符串包含 role 或 name 的任意关键词
    """
    if not role_str or not available_roles:
        return role_str

    lower_str = role_str.lower()

    # 1. 精确匹配
    for r in available_roles:
        if role_str == r["role"]:
            return r["role"]

    # 2. 模糊匹配：角色标识是否在返回字符串中
    for r in available_roles:
        if r["role"].lower() in lower_str:
            return r["role"]

    # 3. 关键词反向匹配：从 role 和 name 字段自动提取关键词
    def _extract_keywords(role_val: str, name_val: str | None) -> set[str]:
        """从 role 和 name 提取可用于匹配的关键词"""
        keywords: set[str] = set()

        # 从 role 标识提取（backend-engineer → backend, engineer）
        if role_val:
            keywords.update(
                kw.strip()
                for kw in role_val.lower().replace("_", "-").split("-")
                if len(kw.strip()) > 1
            )

        # 从 name 字段提取（如 "后端工程师" → {"后端", "端工", "工程", "程师", "后", "端", "工", "程", "师"}）
        if name_val:
            import re
            # 提取英文单词
            keywords.update(re.findall(r"[a-z]{2,}", name_val.lower()))
            # 提取中文字符（连续 CJK 字符）
            cjk = re.findall(r"[一-鿿]+", name_val)
            for seg in cjk:
                # 2-gram
                if len(seg) >= 2:
                    keywords.update(seg[i : i + 2] for i in range(len(seg) - 1))
                # 单字（长度合适时才加入，避免极端长串）
                if 2 <= len(seg) <= 6:
                    keywords.update(seg)

        return keywords

    # 构建动态关键词表
    role_keywords: dict[str, set[str]] = {}
    for r in available_roles:
        role_id = r["role"]
        role_keywords[role_id] = _extract_keywords(role_id, r.get("name"))

    best_match: str | None = None
    best_score = 0

    for role_id, keywords in role_keywords.items():
        hits = sum(1 for kw in keywords if kw in lower_str)
        if hits > best_score:
            best_score = hits
            best_match = role_id

    if best_match and best_score > 0:
        return best_match

    # 都匹配不上，返回原始值（dispatch 阶段会进一步处理或报错）
    return role_str

"""
群主 LangGraph 状态图定义

状态图流程：
    analyze → decompose → dispatch ↔ monitor → summarize

- analyze:    接收用户需求，分析意图和涉及的角色
- decompose:  将需求拆解为子任务 DAG
- dispatch:   按拓扑序派发无依赖任务，创建 Task 记录
- monitor:    监听任务完成事件，推进下游任务
- summarize:  所有任务完成后，汇总结果
"""
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


# ── 节点函数 ────────────────────────────────────────────────────────


async def analyze(state: CoordinatorState) -> dict:
    """意图分析节点：理解用户需求，识别涉及的角色"""
    analyzer = get_intent_analyzer()
    result = await analyzer.ainvoke(
        f"分析以下需求，识别涉及的智能体角色：\n\n{state['requirement']}"
    )
    return {
        "intent_analysis": result.analysis,
        "involved_roles": result.involved_roles,
    }


async def decompose(state: CoordinatorState) -> dict:
    """任务拆解节点：将需求拆解为子任务 DAG"""
    decomposer = get_task_decomposer()

    # 构建角色上下文：告诉 LLM 群组中有哪些角色可用
    roles_context = _build_roles_context(state["involved_roles"])

    prompt = (
        f"用户需求：{state['requirement']}\n\n"
        f"意图分析：{state['intent_analysis']}\n\n"
        f"可用角色：\n{roles_context}\n\n"
        f"请将需求拆解为子任务，指定每个任务的执行角色和依赖关系。"
    )
    result = await decomposer.ainvoke(prompt)

    # 将 SubTaskDef 列表转换为 state 中的 subtasks 格式
    subtasks = []
    for st in result.subtasks:
        subtasks.append({
            "title": st.title,
            "description": st.description,
            "assigned_agent_id": st.assigned_role,  # 暂用角色标识，dispatch 时解析为真实 ID
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
    """调度派发节点：按 DAG 拓扑序创建 Task 记录，派发无依赖任务"""
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

    return {
        "pending_task_ids": pending_task_ids,
        "running_task_ids": running_task_ids,
    }


async def monitor(state: CoordinatorState) -> dict:
    """状态监控节点：检查运行中的任务，更新状态，推进下游任务

    此节点在 task-007 消息总线实现后完善，
    目前提供基础逻辑：检查数据库中任务状态变更。
    """
    group_id = state["group_id"]
    running_task_ids = list(state.get("running_task_ids", []))
    completed_task_ids = list(state.get("completed_task_ids", []))
    failed_task_ids = list(state.get("failed_task_ids", []))
    pending_task_ids = list(state.get("pending_task_ids", []))

    async with async_session() as db:
        for tid in list(running_task_ids):
            task = await task_service.get_task(db, tid)
            if not task:
                continue
            if task.status == "completed":
                running_task_ids.remove(tid)
                completed_task_ids.append(tid)
            elif task.status == "failed":
                running_task_ids.remove(tid)
                failed_task_ids.append(tid)
            elif task.status == "input-required":
                # 子智能体请求澄清，暂时标记但不移出 running
                pass

        # 检查是否有新的可派发任务
        ready_tasks = await task_service.get_ready_tasks(db, group_id)
        for rt in ready_tasks:
            if rt.id not in running_task_ids and rt.id not in completed_task_ids:
                await task_service.update_task(db, rt.id, {"status": "working"})
                running_task_ids.append(rt.id)

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

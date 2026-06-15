"""
群主调度服务

封装 LangGraph 图的编译和执行，对外提供简洁的 API：
- start_requirement: 启动一次需求调度
- get_dag_structure: 获取 DAG 图结构（供前端渲染）
- get_execution_status: 获取执行状态
"""
import json
from typing import Any

from langgraph.graph.state import CompiledStateGraph

from app.coordinator.graph import build_coordinator_graph
from app.coordinator.state import CoordinatorState
from app.core.database import async_session
from app.services import task_service


# 全局图实例（编译一次，多次执行）
_compiled_graph: CompiledStateGraph | None = None


def _get_compiled_graph() -> CompiledStateGraph:
    """获取编译后的 LangGraph 图（单例）"""
    global _compiled_graph
    if _compiled_graph is None:
        graph = build_coordinator_graph()
        _compiled_graph = graph.compile()
    return _compiled_graph


async def start_requirement(group_id: str, requirement: str) -> dict[str, Any]:
    """启动一次需求调度

    Args:
        group_id: 群组 ID
        requirement: 用户原始需求

    Returns:
        执行结果，包含 subtasks/dag_nodes/dag_edges/summary 等
    """
    graph = _get_compiled_graph()

    # 初始状态
    initial_state: CoordinatorState = {
        "group_id": group_id,
        "requirement": requirement,
        "intent_analysis": "",
        "involved_roles": [],
        "subtasks": [],
        "dag_nodes": [],
        "dag_edges": [],
        "pending_task_ids": [],
        "running_task_ids": [],
        "completed_task_ids": [],
        "failed_task_ids": [],
        "summary": "",
        "artifacts": [],
        "messages": [],
    }

    # 执行状态图
    result = await graph.ainvoke(initial_state)
    return result


async def get_dag_structure(group_id: str) -> dict[str, Any]:
    """获取群组的 DAG 图结构（供前端 ReactFlow 渲染）

    Returns:
        {nodes: [{id, label, agent_id, status}], edges: [{source, target}]}
    """
    async with async_session() as db:
        tasks = await task_service.list_tasks_by_group(db, group_id)

    nodes = []
    edges = []
    for t in tasks:
        nodes.append({
            "id": t.id,
            "label": t.title,
            "agent_id": t.assigned_agent_id,
            "status": t.status,
            "dag_order": t.dag_order,
        })
        for dep_id in (t.dependencies or []):
            edges.append({
                "source": dep_id,
                "target": t.id,
            })

    return {"nodes": nodes, "edges": edges}


async def get_coordinator_graph_structure() -> dict[str, Any]:
    """获取群主 LangGraph 状态图结构（供前端 ReactFlow 渲染调度流程图）

    Returns:
        {nodes: [{id, name, type}], edges: [{source, target, conditional}]}
    """
    graph = build_coordinator_graph()
    compiled = graph.compile()
    g = compiled.get_graph()

    # 过滤掉 __start__ / __end__ 虚拟节点，保留业务节点
    nodes = []
    for n in g.nodes.values():
        if n.name.startswith("__"):
            continue
        nodes.append({
            "id": n.name,
            "name": n.name,
            "type": "coordinator_node",
        })

    # 边：只保留业务节点之间的边
    edges = []
    for e in g.edges:
        src = e.source if not e.source.startswith("__") else None
        dst = e.target if not e.target.startswith("__") else None
        if src and dst:
            edges.append({
                "source": src,
                "target": dst,
                "conditional": e.conditional,
            })

    return {"nodes": nodes, "edges": edges}


async def get_execution_status(group_id: str) -> dict[str, Any]:
    """获取群组任务的执行状态汇总

    Returns:
        {total, completed, running, failed, submitted, tasks: [...]}
    """
    async with async_session() as db:
        tasks = await task_service.list_tasks_by_group(db, group_id)

    status_counts = {"submitted": 0, "working": 0, "completed": 0, "failed": 0, "canceled": 0, "input-required": 0}
    task_list = []
    for t in tasks:
        status_counts[t.status] = status_counts.get(t.status, 0) + 1
        task_list.append({
            "id": t.id,
            "title": t.title,
            "status": t.status,
            "assigned_agent_id": t.assigned_agent_id,
            "result_summary": t.result_summary,
            "exit_code": t.exit_code,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        })

    return {
        "total": len(tasks),
        **status_counts,
        "tasks": task_list,
    }

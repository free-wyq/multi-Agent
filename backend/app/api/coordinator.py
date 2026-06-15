"""
群主调度 API 路由

提供需求提交、DAG 图结构、执行状态查询等接口。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.common import OK
from app.coordinator.service import (
    get_coordinator_graph_structure,
    get_dag_structure,
    get_execution_status,
    start_requirement,
)
from app.core.database import get_db

router = APIRouter(prefix="/coordinator", tags=["群主调度"])


# ── 请求 Schema ────────────────────────────────────────────────────


class RequirementSubmit(BaseModel):
    """提交需求"""
    group_id: str = Field(..., description="群组 ID")
    requirement: str = Field(..., min_length=1, description="用户原始需求")


class RequirementResponse(BaseModel):
    """需求调度结果"""
    group_id: str
    intent_analysis: str
    involved_roles: list[str]
    subtasks: list[dict]
    dag_nodes: list[dict]
    dag_edges: list[dict]
    summary: str
    artifacts: list[dict]


class DAGResponse(BaseModel):
    """DAG 图结构（供前端 ReactFlow 渲染）"""
    nodes: list[dict]
    edges: list[dict]


class ExecutionStatusResponse(BaseModel):
    """执行状态汇总"""
    total: int
    submitted: int
    working: int
    completed: int
    failed: int
    canceled: int
    input_required: int
    tasks: list[dict]


# ── 路由 ───────────────────────────────────────────────────────────


@router.post("/requirement", response_model=RequirementResponse)
async def submit_requirement(body: RequirementSubmit):
    """提交需求，群主自动分析→拆解→派发→监控→汇总"""
    try:
        result = await start_requirement(body.group_id, body.requirement)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"调度执行失败: {str(e)}")

    return RequirementResponse(
        group_id=result.get("group_id", body.group_id),
        intent_analysis=result.get("intent_analysis", ""),
        involved_roles=result.get("involved_roles", []),
        subtasks=result.get("subtasks", []),
        dag_nodes=result.get("dag_nodes", []),
        dag_edges=result.get("dag_edges", []),
        summary=result.get("summary", ""),
        artifacts=result.get("artifacts", []),
    )


@router.get("/graph", response_model=DAGResponse)
async def get_coordinator_graph():
    """获取群主 LangGraph 状态图结构（前端可同时渲染调度流程图和任务 DAG）"""
    result = await get_coordinator_graph_structure()
    return DAGResponse(**result)


@router.get("/dag/{group_id}", response_model=DAGResponse)
async def get_dag(group_id: str):
    """获取群组的 DAG 图结构（供前端 ReactFlow 渲染）"""
    result = await get_dag_structure(group_id)
    return DAGResponse(**result)


@router.get("/status/{group_id}", response_model=ExecutionStatusResponse)
async def get_status(group_id: str):
    """获取群组任务的执行状态汇总"""
    result = await get_execution_status(group_id)
    # key name mapping: input-required → input_required for Pydantic
    result["input_required"] = result.pop("input-required", 0)
    return ExecutionStatusResponse(**result)

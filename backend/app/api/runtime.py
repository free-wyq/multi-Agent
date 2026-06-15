"""
运行时 API 路由

子智能体容器管理接口：启动、停止、执行任务、获取日志。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.common import OK
from app.core.database import get_db
from app.services import runtime_service
from app.runtime.instance_pool import ContainerInstancePool

router = APIRouter(prefix="/runtime", tags=["子智能体运行时"])

# 全局实例池（单例）
_instance_pool: ContainerInstancePool | None = None


def get_pool() -> ContainerInstancePool:
    global _instance_pool
    if _instance_pool is None:
        _instance_pool = ContainerInstancePool()
    return _instance_pool


# ── 请求/响应 Schema ────────────────────────────────────────────────


class StartAgentRequest(BaseModel):
    definition_id: str = Field(..., description="智能体定义 ID")
    group_id: str = Field(..., description="群组 ID")
    strategy: str = Field(default="on_demand", description="启动策略: on_demand / pooled / always_on")


class StartAgentResponse(BaseModel):
    instance_id: str
    container_id: str | None
    container_name: str | None
    status: str
    message: str


class ExecuteRequest(BaseModel):
    task: str = Field(..., min_length=1, description="任务指令")
    task_id: str | None = Field(None, description="可选的任务 ID")
    timeout: float = Field(default=600.0, description="超时秒数")


class ExecuteResponse(BaseModel):
    success: bool
    exit_code: int
    output: str
    artifact_paths: list[str]
    task_id: str | None
    agent_id: str | None
    instance_id: str


class InstanceResponse(BaseModel):
    instance_id: str
    definition_id: str
    definition_name: str
    group_id: str
    role: str
    status: str
    current_task_id: str | None
    container_id: str | None
    container_name: str | None
    created_at: str | None = None
    stopped_at: str | None = None


class LogResponse(BaseModel):
    instance_id: str
    logs: str


class PoolStatsResponse(BaseModel):
    stats: dict[str, int]


# ── 路由 ───────────────────────────────────────────────────────────


@router.post("/start", response_model=StartAgentResponse)
async def start_agent(body: StartAgentRequest, db: AsyncSession = Depends(get_db)):
    """启动智能体运行时（创建容器）"""
    try:
        result = await runtime_service.start_agent(
            db,
            definition_id=body.definition_id,
            group_id=body.group_id,
            pool=get_pool(),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"启动失败: {exc}")

    return StartAgentResponse(
        instance_id=result["instance_id"],
        container_id=result.get("container_id"),
        container_name=result.get("container_name"),
        status=result["status"],
        message=result.get("message", "启动成功"),
    )


@router.post("/{instance_id}/execute", response_model=ExecuteResponse)
async def execute_task(instance_id: str, body: ExecuteRequest):
    """向运行中的智能体下发任务"""
    try:
        result = await runtime_service.execute_task(
            instance_id=instance_id,
            task=body.task,
            task_id=body.task_id,
            timeout=body.timeout,
            pool=get_pool(),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"任务执行失败: {exc}")

    return ExecuteResponse(
        success=result.success,
        exit_code=result.exit_code,
        output=result.output,
        artifact_paths=result.artifact_paths,
        task_id=result.task_id,
        agent_id=result.agent_id,
        instance_id=instance_id,
    )


@router.get("/{instance_id}", response_model=InstanceResponse)
async def get_instance(instance_id: str, db: AsyncSession = Depends(get_db)):
    """获取实例详情"""
    instance = await runtime_service.get_instance(db, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")

    return InstanceResponse(
        instance_id=instance.id,
        definition_id=instance.definition_id,
        definition_name="",
        group_id="",
        role="",
        status=instance.status,
        current_task_id=instance.current_task_id,
        container_id=instance.container_id,
        container_name=instance.container_name,
        created_at=instance.created_at.isoformat() if instance.created_at else None,
        stopped_at=instance.stopped_at.isoformat() if instance.stopped_at else None,
    )


@router.get("/{instance_id}/logs", response_model=LogResponse)
async def get_instance_logs(instance_id: str, tail: int = Query(default=100, ge=1, le=10000)):
    """获取实例容器日志"""
    try:
        logs = await runtime_service.get_logs(instance_id, tail=tail)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取日志失败: {exc}")

    return LogResponse(instance_id=instance_id, logs=logs)


@router.post("/{instance_id}/stop", response_model=OK)
async def stop_instance(instance_id: str, remove: bool = True):
    """停止智能体实例"""
    try:
        await runtime_service.stop_instance(instance_id, remove_container=remove)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"停止失败: {exc}")

    return OK(message="实例已停止")


@router.post("/{instance_id}/restart", response_model=StartAgentResponse)
async def restart_instance(instance_id: str):
    """重启智能体实例"""
    try:
        result = await runtime_service.restart_instance(instance_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"重启失败: {exc}")

    return StartAgentResponse(
        instance_id=result["instance_id"],
        container_id=result.get("container_id"),
        container_name=result.get("container_name"),
        status=result["status"],
        message=result.get("message", "重启成功"),
    )


@router.get("/group/{group_id}/instances", response_model=list[InstanceResponse])
async def list_group_instances(group_id: str, db: AsyncSession = Depends(get_db)):
    """列出群组的所有运行时实例"""
    instances = await runtime_service.list_instances_by_group(db, group_id)
    return [
        InstanceResponse(
            instance_id=i.id,
            definition_id=i.definition_id,
            definition_name="",
            group_id="",
            role="",
            status=i.status,
            current_task_id=i.current_task_id,
            container_id=i.container_id,
            container_name=i.container_name,
            created_at=i.created_at.isoformat() if i.created_at else None,
        )
        for i in instances
    ]


@router.get("/pool/stats", response_model=PoolStatsResponse)
async def get_pool_stats():
    """获取实例池统计"""
    return PoolStatsResponse(stats=get_pool().stats())

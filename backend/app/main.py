"""
Multi-Agent Framework - 后端入口
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.agents import router as agents_router
from app.api.groups import router as groups_router
from app.api.tasks import router as tasks_router
from app.api.messages import router as messages_router
from app.api.coordinator import router as coordinator_router
from app.api.runtime import router as runtime_router
from app.ws.routes import router as ws_router
from app.core.database import engine, Base


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：建表
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 启动：消息总线连接 Redis
    from app.bus.core import get_bus
    bus = get_bus()
    await bus.connect()

    # 启动：加载并启动所有子智能体引擎（常驻）
    from app.agent_engine import get_registry
    registry = get_registry()
    await registry.load_from_db()

    yield

    # 关闭：先停 AgentEngine，再断 Redis
    from app.agent_engine import get_registry
    await get_registry().shutdown_all()
    await bus.disconnect()


app = FastAPI(
    title="Multi-Agent Framework",
    description="多智能体协作平台 API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # Vite dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(agents_router, prefix="/api/v1")
app.include_router(groups_router, prefix="/api/v1")
app.include_router(tasks_router, prefix="/api/v1")
app.include_router(messages_router, prefix="/api/v1")
app.include_router(coordinator_router, prefix="/api/v1")
app.include_router(runtime_router, prefix="/api/v1")
# WebSocket 路由（无 /api/v1 前缀）
app.include_router(ws_router)


@app.get("/api/v1/health")
async def health():
    return {"status": "ok"}

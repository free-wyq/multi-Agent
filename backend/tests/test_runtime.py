"""
Task-006 自测用例

运行方式:
    cd /home/wyq/work/project/multi-Agent/backend
    source .venv/bin/activate
    python3 -m pytest backend/tests/test_runtime.py -v -s

注意：Docker 容器测试需要 Docker daemon 可用。
"""
import asyncio
import uuid

import pytest


class TestConfigGenerator:
    """测试配置生成器"""

    def test_generate_claude_md_default(self):
        from app.runtime.config_generator import generate_claude_md

        md = generate_claude_md("测试智能体", "backend-engineer")
        assert "测试智能体" in md
        assert "backend-engineer" in md or "后端开发工程师" in md
        assert "/workspace" in md
        assert "CLAUDE.md" not in md  # 不应该是模板名

    def test_generate_claude_md_with_skills(self):
        from app.runtime.config_generator import generate_claude_md

        md = generate_claude_md("测试智能体", "backend-engineer", extra_skills=["Docker", "K8s"])
        assert "Docker（技能市场挂载）" in md
        assert "K8s（技能市场挂载）" in md

    def test_generate_claude_md_custom_prompt(self):
        from app.runtime.config_generator import generate_claude_md

        custom = "我是一个自定义角色"
        md = generate_claude_md("测试智能体", "custom-role", custom_system_prompt=custom)
        assert custom in md
        # 自定义 prompt 时不依赖 role 模板

    def test_generate_settings_json(self):
        from app.runtime.config_generator import generate_settings_json

        sj = generate_settings_json("测试智能体", "backend-engineer")
        assert '"Bash"' in sj
        assert '"测试智能体"' in sj

    def test_generate_settings_json_denied(self):
        from app.runtime.config_generator import generate_settings_json

        sj = generate_settings_json("测试智能体", "reviewer", extra_denied_tools=["Bash"])
        assert "Bash" not in sj.split("allowed_tools")[1].split("denied_tools")[0]


class TestDockerManager:
    """测试 Docker 管理器（需要 Docker daemon）"""

    @pytest.fixture
    def dm(self):
        from app.runtime.docker_manager import DockerContainerManager
        return DockerContainerManager()

    @pytest.fixture
    def group_id(self):
        return f"test-group-{uuid.uuid4().hex[:8]}"

    @pytest.mark.asyncio
    async def test_volume_lifecycle(self, dm, group_id):
        """测试卷创建和删除"""
        vol = dm.ensure_volume(group_id)
        assert "agenticx-volume" in vol

        # 重复创建不应报错
        vol2 = dm.ensure_volume(group_id)
        assert vol == vol2

        dm.remove_volume(group_id)

    @pytest.mark.asyncio
    async def test_network_lifecycle(self, dm, group_id):
        """测试网络创建和删除"""
        net = dm.ensure_network(group_id)
        assert "agenticx-net" in net

        dm.remove_network(group_id)

    @pytest.mark.asyncio
    async def test_container_lifecycle(self, dm, group_id):
        """测试容器完整生命周期"""
        from app.runtime.docker_manager import ContainerConfig

        vol = dm.ensure_volume(group_id)
        net = dm.ensure_network(group_id)

        config = ContainerConfig(
            name=f"test-container-{group_id}",
            image="alpine:latest",
            command=["sleep", "30"],
            volumes={vol: {"bind": "/workspace", "mode": "rw"}},
            network=net,
            labels={"agenticx.group": group_id},
            memory="128m",
        )

        # 创建并启动
        info = await dm.create(config, auto_start=True)
        assert "id" in info
        assert "name" in info
        container_id = info["id"]

        # 检查运行状态
        assert await dm.is_running(container_id)

        # 执行命令
        exec_result = await dm.exec_command(container_id, ["echo", "hello"])
        assert exec_result["exit_code"] == 0
        assert "hello" in exec_result["output"]

        # 写文件
        await dm.exec_command(container_id, ["sh", "-c", "echo 'test-data' > /workspace/test.txt"])

        # 读日志
        logs = await dm.get_logs(container_id)
        assert len(logs) >= 0  # 至少不报错

        # 停止并删除
        await dm.stop(container_id, remove=True)

        # 清理
        dm.remove_volume(group_id)
        dm.remove_network(group_id)

    @pytest.mark.asyncio
    async def test_cleanup_group(self, dm, group_id):
        """测试群组资源清理"""
        dm.ensure_volume(group_id)
        dm.ensure_network(group_id)
        await dm.cleanup_group(group_id)


class TestInstancePool:
    """测试实例池（部分需要 Docker）"""

    def test_pool_empty_stats(self):
        from app.runtime.instance_pool import ContainerInstancePool
        pool = ContainerInstancePool()
        assert pool.stats() == {}

    @pytest.mark.asyncio
    async def test_pool_drain_empty(self):
        from app.runtime.instance_pool import ContainerInstancePool
        pool = ContainerInstancePool()
        await pool.drain()


class TestRuntimeImports:
    """测试模块导入完整性"""

    def test_runtime_package_import(self):
        from app import runtime
        assert hasattr(runtime, "AgentRuntime")
        assert hasattr(runtime, "ClaudeCodeRuntime")
        assert hasattr(runtime, "ContainerInstancePool")
        assert hasattr(runtime, "DockerContainerManager")
        assert hasattr(runtime, "ContainerConfig")
        assert hasattr(runtime, "generate_claude_md")
        assert hasattr(runtime, "generate_settings_json")

    def test_base_runtime_abc(self):
        from app.runtime.base_runtime import AgentRuntime, AgentResult, InstanceStatus
        assert len(AgentRuntime.__abstractmethods__) > 0
        assert InstanceStatus.RUNNING.value == "running"

    def test_services_import(self):
        from app.services import runtime_service, agent_service_instance


class TestFastAPIIntegration:
    """测试 FastAPI 集成"""

    def test_app_loads(self):
        from app.main import app
        assert app.title == "Multi-Agent Framework"

    def test_runtime_router_registered(self):
        """通过 TestClient 验证 runtime 路由可用"""
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)

        # 请求实例池统计接口（GET，不需要数据）
        resp = client.get("/api/v1/runtime/pool/stats")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "stats" in data
        assert isinstance(data["stats"], dict)

    def test_runtime_start_needs_auth(self):
        """验证 start 接口返回 422（缺少 body，证明路由存在）"""
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)

        resp = client.post("/api/v1/runtime/start", json={})
        # body 缺失必填字段，返回 422
        assert resp.status_code == 422, resp.text

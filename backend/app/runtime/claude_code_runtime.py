"""
ClaudeCodeRuntime 实现

基于 Docker 容器 + Claude Code CLI，封装完整的子智能体任务执行。

容器启动流程：
1. 确保群组卷 + 网络存在
2. 生成 CLAUDE.md / settings.json 并写入卷
3. 创建容器（挂载卷、配置网络）
4. 启动容器（容器中运行 executor 守护进程或 Claude Code）
5. 下发任务（docker exec 触发任务执行）
6. 收集结果（读取容器内结果文件）

MVP 简化策略：
- 容器内 entrypoint 运行一个轻量 executor 脚本
- 通过 docker exec 触发单次任务执行
- 任务完成后脚本退出，容器保持在后台待命
- 结果通过 /workspace/.results/{task_id}.json 传递
"""
import json
import logging
import os
import uuid
from pathlib import Path

from app.core.config import settings
from app.models.agent_definition import AgentDefinition
from app.runtime.base_runtime import AgentResult, InstanceStatus, AgentRuntime
from app.runtime.config_generator import generate_claude_md, generate_settings_json
from app.runtime.docker_manager import ContainerConfig, DockerContainerManager

logger = logging.getLogger(__name__)

DEFAULT_BASE_IMAGE = "agent-base:latest"
EXECUTOR_ENTRYPOINT = ["/bin/bash", "-c"]
IDLE_COMMAND = ["sleep", "3600"]  # 容器保持运行的占位命令

# 容器内路径
CONTAINER_WORKSPACE = "/workspace"
CONTAINER_CONFIG_DIR = "/workspace/.agenticx"
CONTAINER_CLAUDE_MD = f"{CONTAINER_CONFIG_DIR}/CLAUDE.md"
CONTAINER_SETTINGS_JSON = f"{CONTAINER_CONFIG_DIR}/settings.json"
CONTAINER_TASKS_DIR = f"{CONTAINER_CONFIG_DIR}/tasks"
CONTAINER_RESULTS_DIR = f"{CONTAINER_CONFIG_DIR}/results"


def _executor_bootstrap() -> str:
    """生成容器内任务执行引导脚本（bash）"""
    return r'''#!/bin/bash
set -e
TASK_FILE="$1"
RESULT_FILE="$2"

if [[ ! -f "$TASK_FILE" ]]; then
    echo '{"success":false,"exit_code":1,"output":"任务文件不存在: '$TASK_FILE'","artifact_paths":[]}' > "$RESULT_FILE"
    exit 1
fi

TASK_JSON=$(cat "$TASK_FILE")
TASK_DESC=$(echo "$TASK_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('task',''))")
TASK_ID=$(echo "$TASK_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('task_id',''))")

# 创建输出目录
mkdir -p /workspace/shared /workspace/output

# 将任务写入执行日志
LOG_FILE="/workspace/.agenticx/run.log"
echo "[$(date -Iseconds)] TASK_START task_id=$TASK_ID" >> "$LOG_FILE"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MVP 简化：直接执行 bash 命令来模拟任务完成
# 实际集成时，这里应调用 claude code 或 Claude API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 尝试通过 CLAUDE.md 中的提示+任务直接执行
# 实际运行时由 Claude Code 接管，这里只是占位逻辑
OUTPUT=""
EXIT_CODE=0

# 示例：如果任务包含特定关键词，模拟不同产出
if echo "$TASK_DESC" | grep -qi "backend\|api\|后端"; then
    mkdir -p /workspace/shared
    echo "# Backend API" > /workspace/shared/backend.py
    OUTPUT="后端代码已生成到 /workspace/shared/backend.py"
    ARTIFACTS='["/workspace/shared/backend.py"]'
elif echo "$TASK_DESC" | grep -qi "frontend\|前端\|react"; then
    mkdir -p /workspace/shared
    echo "// Frontend Component" > /workspace/shared/frontend.tsx
    OUTPUT="前端组件已生成到 /workspace/shared/frontend.tsx"
    ARTIFACTS='["/workspace/shared/frontend.tsx"]'
elif echo "$TASK_DESC" | grep -qi "test\|测试"; then
    mkdir -p /workspace/shared
    echo "# Tests" > /workspace/shared/test.py
    OUTPUT="测试代码已生成到 /workspace/shared/test.py"
    ARTIFACTS='["/workspace/shared/test.py"]'
else
    OUTPUT="任务已收到，MVP 模式下无实际 AI 执行。task_id=$TASK_ID"
    ARTIFACTS='[]'
fi

echo "[$(date -Iseconds)] TASK_END task_id=$TASK_ID exit_code=$EXIT_CODE" >> "$LOG_FILE"

# 写结果 JSON
cat > "$RESULT_FILE" <<RESULT
{"success": true, "exit_code": $EXIT_CODE, "output": "$OUTPUT", "artifact_paths": $ARTIFACTS}
RESULT
'''


class ClaudeCodeRuntime(AgentRuntime):
    """Claude Code CLI 运行时实现

    每个实例 = 一个 Docker 容器。
    """

    def __init__(
        self,
        definition_id: str,
        definition_name: str,
        group_id: str,
        *,
        instance_id: str | None = None,
        role: str = "executor",
        agent_def: AgentDefinition | None = None,
    ) -> None:
        super().__init__(
            definition_id=definition_id,
            definition_name=definition_name,
            group_id=group_id,
            instance_id=instance_id or str(uuid.uuid4()),
            role=role,
        )
        self._docker = DockerContainerManager()
        self._agent_def = agent_def
        self._base_image = agent_def.base_image if agent_def else DEFAULT_BASE_IMAGE

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动容器：准备卷、写配置、创建并启动容器"""
        if self.container_id:
            logger.warning("运行时已经启动，container_id=%s", self.container_id[:12])
            return

        # 1. 确保群组卷和网络
        self._docker.ensure_volume(self.group_id)
        self._docker.ensure_network(self.group_id)

        # 2. 生成并写入 CLAUDE.md + settings.json
        await self._write_config_files()

        # 3. 写入 executor 引导脚本
        await self._write_executor_script()

        # 4. 构建容器配置
        vol_name = self._docker.volume_name(self.group_id)
        container_name = f"agenticx-{self.group_id[:8]}-{self.definition_id[:8]}-{self.instance_id[:8]}"

        config = ContainerConfig(
            name=container_name,
            image=self._base_image,
            volumes={
                vol_name: {"bind": CONTAINER_WORKSPACE, "mode": "rw"},
            },
            network=self._docker.network_name(self.group_id),
            command=IDLE_COMMAND,
            env={
                "AGENTICX_INSTANCE_ID": self.instance_id,
                "AGENTICX_GROUP_ID": self.group_id,
                "AGENTICX_DEFINITION_ID": self.definition_id,
                "HOME": "/root",
            },
            labels={
                "agenticx.group": self.group_id,
                "agenticx.definition": self.definition_id,
                "agenticx.instance": self.instance_id,
                "agenticx.role": self.role,
                "agenticx.managed": "true",
            },
            memory="2g",
        )

        # 5. 创建并启动容器
        info = await self._docker.create(config, auto_start=True)
        self.container_id = info["id"]
        self.container_name = info["name"]
        self.status = InstanceStatus.IDLE

        logger.info(
            "ClaudeCodeRuntime 已启动: instance=%s container=%s",
            self.instance_id[:8],
            self.container_id[:12],
        )

    async def stop(self, *, remove_container: bool = True) -> None:
        """停止并可选销毁容器"""
        if not self.container_id:
            return

        await self._docker.stop(
            self.container_id,
            remove=remove_container,
        )
        self.status = InstanceStatus.STOPPED if remove_container else InstanceStatus.IDLE
        if remove_container:
            self.container_id = None
            self.container_name = None

    async def restart(self) -> None:
        """重启容器"""
        if self.container_id:
            await self._docker.stop(self.container_id, remove=False)
            await self._docker.start(self.container_id)
        else:
            await self.start()

    # ── 任务执行 ──────────────────────────────────────────────────────

    async def execute(
        self,
        task: str,
        *,
        task_id: str | None = None,
        timeout: float = 600.0,
    ) -> AgentResult:
        """在容器内执行任务

        流程：写任务文件 -> exec 触发执行 -> 轮询结果 -> 返回 AgentResult
        """
        if not self.container_id:
            raise RuntimeError("运行时未启动，请先调用 start()")

        if not await self._docker.is_running(self.container_id):
            raise RuntimeError("容器未在运行")

        task_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        self.current_task_id = task_id
        self.status = InstanceStatus.RUNNING

        # 1. 写任务文件到卷
        task_file = f"{CONTAINER_TASKS_DIR}/{task_id}.json"
        result_file = f"{CONTAINER_RESULTS_DIR}/{task_id}.json"

        task_payload = {
            "task_id": task_id,
            "task": task,
            "agent_id": self.definition_id,
            "timestamp": "",
        }
        await self._write_file(task_file, json.dumps(task_payload, ensure_ascii=False))

        # 2. 执行引导脚本
        executor_path = f"{CONTAINER_CONFIG_DIR}/run_task.sh"
        cmd = ["bash", executor_path, task_file, result_file]

        logger.info("开始执行任务: task_id=%s in container=%s", task_id, self.container_id[:12])

        exec_result = await self._docker.exec_command(
            self.container_id,
            cmd,
            workdir=CONTAINER_WORKSPACE,
            timeout=timeout,
        )

        # 3. 读取结果文件
        result_data = await self._read_result_file(result_file)

        if result_data is None:
            # fallback：通过 exit_code 判断
            result_data = {
                "success": exec_result["exit_code"] == 0,
                "exit_code": exec_result["exit_code"],
                "output": exec_result["output"],
                "artifact_paths": [],
            }

        # 4. 更新状态
        self.status = InstanceStatus.IDLE if result_data["success"] else InstanceStatus.ERROR
        self.current_task_id = None

        return AgentResult(
            success=result_data["success"],
            exit_code=result_data["exit_code"],
            output=result_data.get("output", ""),
            artifact_paths=result_data.get("artifact_paths", []),
            task_id=task_id,
            agent_id=self.definition_id,
            error_message=None if result_data["success"] else result_data.get("output"),
        )

    # ── 状态与日志 ────────────────────────────────────────────────────

    async def get_logs(self, tail: int = 100) -> str:
        if not self.container_id:
            return ""
        return await self._docker.get_logs(self.container_id, tail=tail)

    async def is_healthy(self) -> bool:
        if not self.container_id:
            return False
        return await self._docker.is_running(self.container_id)

    # ── 内部辅助 ──────────────────────────────────────────────────────

    async def _write_config_files(self) -> None:
        """将 CLAUDE.md 和 settings.json 写入环境卷"""
        ad = self._agent_def
        if ad:
            claude_md = generate_claude_md(
                name=ad.name,
                role=ad.role,
                extra_skills=list(ad.extra_skills or []),
                custom_system_prompt=ad.system_prompt or None,
            )
            settings_json = generate_settings_json(
                name=ad.name,
                role=ad.role,
                extra_allowed_tools=list(ad.allowed_tools or []),
                extra_denied_tools=list(ad.denied_tools or []),
            )
        else:
            claude_md = generate_claude_md(
                name=self.definition_name,
                role=self.role,
            )
            settings_json = generate_settings_json(
                name=self.definition_name,
                role=self.role,
            )

        # 写入卷（通过 docker exec 在临时 alpine 容器里创建文件）
        vol_name = self._docker.volume_name(self.group_id)
        await self._write_file(CONTAINER_CLAUDE_MD, claude_md)
        await self._write_file(CONTAINER_SETTINGS_JSON, settings_json)
        await self._ensure_dir(CONTAINER_TASKS_DIR)
        await self._ensure_dir(CONTAINER_RESULTS_DIR)

        logger.info("配置已写入卷: %s", vol_name)

    async def _write_executor_script(self) -> None:
        """将引导脚本写入卷"""
        script = _executor_bootstrap()
        script_path = f"{CONTAINER_CONFIG_DIR}/run_task.sh"
        await self._write_file(script_path, script)
        await self._docker.exec_command(
            self.container_id,  # type: ignore[arg-type]
            ["chmod", "+x", script_path],
        )

    async def _write_file(self, container_path: str, content: str) -> None:
        """通过 docker exec 在容器内写文件（如果容器未启动，用临时容器）"""
        if self.container_id and await self._docker.is_running(self.container_id):
            container_id = self.container_id
        else:
            # 用临时 alpine 容器挂载同卷来写文件
            container_id = None

        # 使用 base64 转义避免引号问题
        import base64
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        cmd = [
            "sh", "-c",
            f"mkdir -p $(dirname '{container_path}') && echo '{b64}' | base64 -d > '{container_path}'",
        ]

        if container_id:
            await self._docker.exec_command(container_id, cmd)
        else:
            # 用临时容器
            tmp_container = self._docker._docker.containers.run(
                "alpine:latest",
                command=cmd,
                volumes={self._docker.volume_name(self.group_id): {"bind": CONTAINER_WORKSPACE, "mode": "rw"}},
                remove=True,
                detach=True,
            )
            # 等待完成
            tmp_container.wait()

    async def _ensure_dir(self, container_dir: str) -> None:
        """确保容器内目录存在"""
        if self.container_id and await self._docker.is_running(self.container_id):
            await self._docker.exec_command(
                self.container_id,
                ["mkdir", "-p", container_dir],
            )

    async def _read_result_file(self, container_path: str) -> dict | None:
        """从容器内读取结果 JSON"""
        try:
            result = await self._docker.exec_command(
                self.container_id,  # type: ignore[arg-type]
                ["cat", container_path],
                timeout=10,
            )
            if result["exit_code"] == 0:
                return json.loads(result["output"])
        except Exception as exc:
            logger.warning("读取结果文件失败: %s — %s", container_path, exc)
        return None

"""
子智能体运行时 - Docker 容器管理

每个子智能体在独立 Docker 容器中运行，容器挂载群组环境卷。
环境卷保留，容器用完即弃/复用。
"""
import asyncio
import logging
from dataclasses import dataclass

import docker
from docker.errors import DockerException, NotFound

from app.core.config import settings

logger = logging.getLogger(__name__)

DEFAULT_BASE_IMAGE = "agent-base:latest"
DEFAULT_CONTAINER_RESOURCES = {"cpu_quota": 100000, "mem_limit": "2g"}


@dataclass(frozen=True)
class ContainerConfig:
    """容器启动配置"""

    name: str
    image: str = DEFAULT_BASE_IMAGE
    env: dict[str, str] | None = None
    volumes: dict[str, dict] | None = None
    network: str | None = None
    command: list[str] | None = None
    entrypoint: list[str] | None = None
    cpus: int | None = None
    memory: str | None = None
    labels: dict[str, str] | None = None


class DockerContainerManager:
    """Docker 容器生命周期管理

    职责：
    - 创建/启动/停止/销毁容器
    - 挂载群组环境卷到 /workspace
    - 配置网络（群组网络）
    - 容器日志拉取
    - 容器内命令执行
    """

    def __init__(self) -> None:
        self._docker = docker.DockerClient(base_url=settings.DOCKER_HOST)

    # ── 容器 CRUD ─────────────────────────────────────────────────────

    async def create(
        self,
        config: ContainerConfig,
        *,
        auto_start: bool = True,
    ) -> dict:
        """创建（并可选启动）容器，返回容器信息字典"""

        host_config_kwargs: dict = {}
        binds = None

        if config.volumes:
            binds = []
            for host_path, vconfig in config.volumes.items():
                mode = vconfig.get("mode", "rw")
                binds.append(f"{host_path}:{vconfig['bind']}:{mode}")

        if config.cpus:
            host_config_kwargs["cpu_quota"] = config.cpus * 100000
        if config.memory:
            host_config_kwargs["mem_limit"] = config.memory
        if config.network:
            host_config_kwargs["network_mode"] = config.network

        container_id = self._docker.api.create_container(
            image=config.image,
            name=config.name,
            environment=config.env,
            labels=config.labels or {},
            entrypoint=config.entrypoint,
            command=config.command,
            host_config=self._docker.api.create_host_config(
                binds=binds,
                **host_config_kwargs,
            ),
        )["Id"]

        container = self._docker.containers.get(container_id)

        info = {
            "id": container.id,
            "name": container.name,
            "status": container.status,
            "image": config.image,
        }
        logger.info("容器已创建: %s (%s)", info["name"], info["id"][:12])

        if auto_start:
            await self.start(container.id)

        return info

    async def start(self, container_id: str) -> None:
        """启动容器"""
        try:
            container = self._docker.containers.get(container_id)
            if container.status != "running":
                container.start()
                logger.info("容器已启动: %s", container_id[:12])
        except NotFound:
            raise RuntimeError(f"容器未找到: {container_id}")

    async def stop(
        self,
        container_id: str,
        *,
        timeout: int = 10,
        remove: bool = False,
    ) -> None:
        """停止容器，可选删除"""
        try:
            container = self._docker.containers.get(container_id)
            if container.status == "running":
                container.stop(timeout=timeout)
                logger.info("容器已停止: %s", container_id[:12])
            if remove:
                container.remove(force=True)
                logger.info("容器已删除: %s", container_id[:12])
        except NotFound:
            logger.warning("容器不存在，跳过停止: %s", container_id[:12])

    async def remove(self, container_id: str, *, force: bool = True) -> None:
        """删除容器"""
        try:
            container = self._docker.containers.get(container_id)
            container.remove(force=force)
            logger.info("容器已删除: %s", container_id[:12])
        except NotFound:
            pass

    # ── 容器内操作 ────────────────────────────────────────────────────

    async def exec_command(
        self,
        container_id: str,
        cmd: list[str],
        *,
        workdir: str = "/workspace",
        timeout: float = 60.0,
    ) -> dict:
        """在容器内执行命令，返回 {'exit_code': int, 'output': str}"""

        container = self._docker.containers.get(container_id)

        loop = asyncio.get_event_loop()
        exec_obj = await loop.run_in_executor(
            None,
            lambda: container.exec_run(
                cmd,
                workdir=workdir,
                stdout=True,
                stderr=True,
                tty=False,
                environment={"HOME": "/root"},
            ),
        )

        exit_code, raw_output = exec_obj
        output = raw_output.decode("utf-8", errors="replace") if raw_output else ""

        return {
            "exit_code": exit_code,
            "output": output,
        }

    async def get_logs(
        self,
        container_id: str,
        *,
        tail: int = 100,
        since: int | None = None,
    ) -> str:
        """获取容器日志"""
        try:
            container = self._docker.containers.get(container_id)
            kwargs: dict = {"tail": tail, "timestamps": False}
            if since is not None:
                kwargs["since"] = since
            logs = container.logs(**kwargs)
            return logs.decode("utf-8", errors="replace")
        except NotFound:
            return ""

    async def stream_logs(
        self,
        container_id: str,
        *,
        tail: int = 10,
        follow: bool = True,
    ):
        """流式获取容器日志（生成器）"""
        try:
            container = self._docker.containers.get(container_id)
            for chunk in container.logs(
                tail=tail, follow=follow, stream=True, timestamps=False
            ):
                if isinstance(chunk, bytes):
                    yield chunk.decode("utf-8", errors="replace")
                else:
                    yield str(chunk)
        except NotFound:
            logger.warning("容器不存在: %s", container_id[:12])
            return

    # ── 状态查询 ─────────────────────────────────────────────────────

    async def inspect(self, container_id: str) -> dict | None:
        """获取容器详细信息"""
        try:
            container = self._docker.containers.get(container_id)
            return container.attrs  # type: ignore[no-any-return]
        except NotFound:
            return None

    async def is_running(self, container_id: str) -> bool:
        """容器是否正在运行"""
        try:
            container = self._docker.containers.get(container_id)
            return container.status == "running"
        except NotFound:
            return False

    # ── 环境卷 / 网络管理 ──────────────────────────────────────────────

    @staticmethod
    def volume_name(group_id: str) -> str:
        """群组环境卷名称"""
        return f"agenticx-volume-{group_id}"

    @staticmethod
    def network_name(group_id: str) -> str:
        """群组网络名称"""
        return f"agenticx-net-{group_id}"

    def ensure_volume(self, group_id: str) -> str:
        """确保群组环境卷存在，返回卷名"""
        vol_name = self.volume_name(group_id)
        try:
            self._docker.volumes.get(vol_name)
        except NotFound:
            self._docker.volumes.create(
                name=vol_name,
                driver="local",
                labels={"agenticx.group": group_id, "agenticx.managed": "true"},
            )
            logger.info("卷已创建: %s", vol_name)
        return vol_name

    def ensure_network(self, group_id: str) -> str:
        """确保群组网络存在，返回网络名"""
        net_name = self.network_name(group_id)
        try:
            self._docker.networks.get(net_name)
        except NotFound:
            self._docker.networks.create(
                net_name,
                driver="bridge",
                labels={"agenticx.group": group_id, "agenticx.managed": "true"},
            )
            logger.info("网络已创建: %s", net_name)
        return net_name

    def remove_volume(self, group_id: str, force: bool = False) -> None:
        """删除群组环境卷"""
        vol_name = self.volume_name(group_id)
        try:
            vol = self._docker.volumes.get(vol_name)
            vol.remove(force=force)
            logger.info("卷已删除: %s", vol_name)
        except NotFound:
            pass

    def remove_network(self, group_id: str) -> None:
        """删除群组网络"""
        net_name = self.network_name(group_id)
        try:
            net = self._docker.networks.get(net_name)
            net.remove()
            logger.info("网络已删除: %s", net_name)
        except NotFound:
            pass

    # ── 辅助 ──────────────────────────────────────────────────────────

    def list_group_containers(self, group_id: str) -> list[dict]:
        """列出群组相关的所有容器"""
        filters = {"label": [f"agenticx.group={group_id}"]}
        containers = self._docker.containers.list(all=True, filters=filters)
        return [
            {
                "id": c.id,
                "name": c.name,
                "status": c.status,
                "image": c.image.tags[0] if c.image.tags else "",
            }
            for c in containers
        ]

    async def cleanup_group(self, group_id: str) -> None:
        """清理群组所有资源（容器、卷、网络）"""
        containers = self.list_group_containers(group_id)
        for c in containers:
            await self.stop(c["id"], remove=True)

        self.remove_volume(group_id)
        self.remove_network(group_id)

        logger.info("群组 %s 资源已清理", group_id)

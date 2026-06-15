# agent-base Docker Image

多智能体协作框架的子智能体基础镜像。

## 设计原则

- **Ubuntu 单一镜像**：所有角色（前端/后端/测试/DevOps）通用，不区分镜像
- **预装 Claude Code CLI**：子智能体的核心运行时
- **root 权限**：容器内可 `apt-get` 安装任意中间件（Redis/MySQL/...）
- **自装自起**：工具链由 Claude Code 运行时按需安装，平台不预装

## 镜像内容

| 组件 | 版本 | 说明 |
|------|------|------|
| Ubuntu | 24.04 | 基础系统 |
| Python | 3.12 | python3 + pip + venv |
| Node.js | 20.17.0 | 通过 nvm 安装，含 pnpm/yarn |
| Claude Code CLI | latest | `@anthropic-ai/claude-code` |
| Git | latest | 代码管理 |
| Build Essentials | latest | gcc/make 等编译工具 |
| Docker CLI | latest | 容器内访问宿主机 Docker |

## 构建

```bash
cd docker/agent-base
docker build -t agent-base:latest .
```

或使用 Makefile：

```bash
cd docker/agent-base
make build
```

## 用法

### 直接运行

```bash
docker run -d --name my-agent \
  -v agenticx-volume-group-1:/workspace \
  -e AGENTICX_GROUP_ID=group-1 \
  -e AGENTICX_INSTANCE_ID=inst-001 \
  agent-base:latest
```

### 与运行时集成

平台通过 `DockerContainerManager` 自动创建容器并挂载群组环境卷：

```python
config = ContainerConfig(
    name="agenticx-xxx",
    image="agent-base:latest",
    volumes={"agenticx-volume-group-1": {"bind": "/workspace", "mode": "rw"}},
    network="agenticx-net-group-1",
)
```

## 目录结构

容器内 `/workspace`：

```
/workspace/
├── .agenticx/
│   ├── CLAUDE.md          ← 角色定义
│   ├── settings.json      ← 工具权限
│   ├── tasks/             ← 任务文件
│   └── results/           ← 执行结果
├── source/                ← 代码
├── shared/                ← 智能体间共享文件
└── output/                ← 最终交付物
```

## 进入容器调试

```bash
docker exec -it my-agent bash
```

## 平台集成说明

- 镜像名在 `app/core/config.py` 或环境变量中配置
- `ClaudeCodeRuntime` 启动时自动生成 CLAUDE.md/settings.json 并写入卷
- entrypoint 负责建立符号链接和目录结构

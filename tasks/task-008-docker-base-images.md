# task-008 Docker 基础镜像

## 任务信息

- **编号**: task-008
- **名称**: Docker 基础镜像（Ubuntu 单镜像）
- **依赖**: task-006 子智能体运行时（已完成）
- **状态**: ✅ 完成
- **完成日期**: 2026-06-15

## 设计原则

- **Ubuntu 单一镜像**：所有角色（前端/后端/测试/DevOps）通用，不区分镜像
- **预装 Claude Code CLI**：子智能体核心运行时
- **root 权限**：容器内可 `apt-get` 安装任意中间件
- **Claude Code 自装自起**：Python/Redis/MySQL 等运行时按需安装，平台不预装

## 交付物

| 文件 | 说明 |
|------|------|
| `docker/agent-base/Dockerfile` | 基础镜像构建文件 |
| `docker/agent-base/entrypoint.sh` | 容器入口脚本 |
| `docker/agent-base/README.md` | 使用说明 |

## 镜像内容

| 组件 | 版本 | 说明 |
|------|------|------|
| Ubuntu | 24.04 | 基础系统 |
| Python | 3.12 | python3 + pip + venv |
| Node.js | 20.17.0 | 直接安装 tar.xz，不走 nvm（避免网络不稳定） |
| Claude Code CLI | 2.1.177 | `@anthropic-ai/claude-code` |
| Git | 2.43 | 代码管理 |
| Build Essentials | 13.2 | gcc/g++/make |
| Docker CLI | 24.x | 宿主机 Docker daemon 访问 |

## 构建结果

```bash
$ docker images | grep agent-base
agent-base:latest   f32c3568f464   1.89GB   504MB
```

## 自测清单

- [x] `docker build -t agent-base:latest` 构建成功
- [x] 容器内 `node --version` → v20.17.0
- [x] 容器内 `python3 --version` → Python 3.12.3
- [x] 容器内 `git --version` → git version 2.43.0
- [x] 容器内 `claude --version` → 2.1.177 (Claude Code)
- [x] 启动测试容器，workspace 目录结构正确
- [x] 环境变量 AGENTICX_GROUP_ID/INSTANCE_ID/DEFINITION_ID 正常打印
- [x] `/workspace/.agenticx/tasks` 和 `results` 目录存在

## 与现有运行时集成

- `ClaudeCodeRuntime` 中使用 `DEFAULT_BASE_IMAGE = "agent-base:latest"`
- `DockerContainerManager` 中 `ContainerConfig.image` 默认使用此镜像
- `entrypoint.sh` 负责在容器启动时建立 `/workspace/CLAUDE.md` 软链接

## 踩坑记录

1. **nvm 安装脚本不可靠**：容器内 `curl | bash` 后，`nvm install` 需要从 GitHub 克隆 nvm repo，网络极不稳定（GnuTLS 错误 / 连接超时）。
   - **修复**：改为直接下载 [Node.js 官方 tar.xz](https://nodejs.org/dist/) 解压到 `/usr/local/lib/nodejs`，一步到位，无需 GitHub 克隆。

2. **corepack enable 后 pnpm 冲突**：`npm install -g pnpm` 与 corepack 提供的 pnpm 冲突。
   - **修复**：只 `corepack enable`，不额外 `npm install -g pnpm`。

## 相关文档

- [[sandbox-design]] — 沙箱方案全貌
- [[agent-runtime-architecture]] — 运行时架构

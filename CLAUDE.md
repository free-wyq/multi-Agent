# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

🟡 开发阶段。项目初始化已完成，正在进行 task-002 数据模型设计。

## Architecture

多智能体协作框架，解决软件工程虚拟交付问题。

- **群主**：LangGraph 状态图 + LangChain ChatAnthropic，运行在后端进程内
- **子智能体**：Claude Code CLI 实例，运行在 Docker 容器内
- **中间件**：智能体需要的 Redis/MySQL 由 Claude Code 运行时自装自起，用户不感知
- **镜像**：统一 Ubuntu + Claude Code CLI 单一基础镜像
- **技能**：内置技能自动映射 + 技能市场可选挂载

## Tech Stack

| 层 | 技术 |
|----|------|
| 前端 | React + Vite + Ant Design + ReactFlow |
| 后端 | Python / FastAPI |
| 群主调度 | LangGraph + LangChain |
| 子智能体 | Claude Code CLI/SDK |
| 容器 | Docker（Ubuntu + Claude Code CLI） |
| 数据库 | PostgreSQL + Redis |
| DAG 可视化 | LangGraph → API → ReactFlow |

## Environment

- **Python** — available via `python3` / `pip3`
- **Node.js** — managed via `nvm`
- **Docker** — 已启动，socket: `/var/run/docker.sock`

## Local Infrastructure（本机开发环境）

本机已有以下服务，`.env` 中配置连接：

| 服务 | 容器名 | 连接信息 |
|------|--------|---------|
| PostgreSQL | agenticx-postgres-lite | localhost:5432, user: agenticx, db: multi_agent |
| Redis | agenticx-redis-lite | localhost:6379 |

**注意**：这些是本机开发环境的配置。其他人启动项目时，需要自行准备 PostgreSQL 和 Redis（可用 docker-compose 或已有实例），并在 `.env` 中配置对应连接信息。

## Project Structure

```
backend/
  app/
    api/        # REST API 路由
    models/     # SQLAlchemy ORM 模型
    services/   # 业务逻辑
    core/       # 配置、依赖注入
    main.py     # FastAPI 入口
  pyproject.toml
frontend/
  src/
    pages/      # 页面组件
    components/ # 通用组件
    services/   # API 调用
    hooks/      # React hooks
  package.json
docker/
  docker-compose.yml
docs/
```

## Claude Code Settings

Project-level permissions are configured in `settings.local.json` and `.claude/settings.local.json` to allow `pip`/`pip3` installs, `sudo apt-get`, and `nvm` usage. Update these files if new tool permissions are needed.

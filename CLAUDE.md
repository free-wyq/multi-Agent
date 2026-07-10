# Multi-Agent 协作桌面应用

多智能体协作框架，解决软件工程虚拟交付问题。

## 架构

- **桌面框架**：Tauri v2 + Rust（tokio async runtime）
- **前端**：React + Vite + Ant Design + ReactFlow
- **后端**：Rust（`src-tauri/src/core/`）— 状态管理、引擎、LLM 调用、CLI 进程
- **群主 Coordinator**：主进程内 tokio task + 轻量 LLM 直调（reqwest，OpenAI 兼容）
- **子智能体 Worker**：本地 Claude Code CLI 实例（`tokio::process::spawn` 跑 `claude --print`）
- **数据持久化**：内存索引 + JSON 文件（500ms 防抖 + 原子写 .tmp→rename）
- **实时事件**：Tauri `app.emit` / `listen`（`bus-event:{groupId}` 通道）
- **A2A 通信**：InboxHub 收件箱中心（`core/inbox.rs`，扔字条式解耦）
  - 每 (group, agent) 一个 tokio mpsc channel，`push_*` 直接送信唤醒
  - 队列在 inbox.rs 单一真源（零空转真消息驱动）
- **.env 配置**：`run()` 启动时通过 dotenvy 自动加载项目根 `.env`（OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL）

## 快速开始

```bash
# 安装依赖
npm install

# 开发模式（Tauri + Vite dev server）
npm run tauri:dev

# 打包桌面应用
npm run tauri:build
```

## 项目结构

```
src-tauri/src/              # Rust + Tauri 后端
  lib.rs                    # Tauri 入口：.env 加载 → init → setup → 24 命令 → Exit 优雅关闭
  main.rs                   # windows_subsystem 配置
  core/
    mod.rs                  # 模块树声明
    types.rs                # serde 数据模型（byte 兼容旧 data/*.json）
    persistence.rs          # JSON 持久化（500ms 防抖 + 原子写）
    store.rs                # 五实体内存索引
    inbox.rs                # A2A 收件箱（InboxHub · mpsc channel · 队列单一真源）
    llm.rs                  # OpenAI 兼容 HTTP + extract_json
    prompts.rs              # worker/coordinator 提示词
    event.rs                # 类型化事件（DomainEvent → BusEventData 投影 → app.emit）
    workspace.rs            # Workspace trait + LocalWorkspace（留 Docker/E2B 接缝）
    permission.rs           # allowed/denied tools + model/max_turns → CLI flags
    middleware.rs           # outbound @mention 路由 + inbound stub（v2）
    engine.rs               # AgentEngine + AgentRegistry（调度大脑 + DAG fail-fast）
    commands/               # #[tauri::command]（camelCase 参数）
      agent.rs group.rs task.rs message.rs status.rs system.rs
src/                        # 前端 Renderer（React）
  pages/                    # 页面组件
  components/               # 通用组件
  services/api.ts           # invoke() 调用层
  hooks/useBusEvent.ts      # Tauri listen 实时事件 hook
```

## 核心概念

- **智能体（Agent）**：角色定义 + system prompt，映射到 CLAUDE.md
- **群组（Group）**：协作单元，群主 + 成员
- **任务（Task）**：DAG 依赖调度，A2A 协议状态机
- **消息（Message）**：智能体间通信，@mention 路由
- **协调者（Coordinator）**：需求分析 → 任务拆解 → 调度 → 监控 → 汇总

## 环境要求

- Rust toolchain（stable）+ Tauri 系统依赖
  - Linux：`libwebkit2gtk-4.1-dev libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev`
- Node.js 20+
- Claude Code CLI 已安装（或设置 `CLAUDE_CODE_PATH` 环境变量）
- LLM API 密钥（OpenAI / DeepSeek / 其他兼容端点，通过 `.env` 注入）

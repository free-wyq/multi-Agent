# Multi-Agent 协作桌面应用

多智能体协作框架，解决软件工程虚拟交付问题。

## 项目简介

一个多智能体协作桌面应用：创建智能体、配置角色与技能，拉入群组协作完成软件交付任务。群主智能体负责意图分析与任务调度，子智能体通过本地 Claude Code CLI 执行开发、编译、测试等具体工作。

**核心定位**：桌面端工具，双击即用，零基础设施。

> 后端已用 **Rust + Tauri v2** 重写，A2A 引擎基于 **tokio mpsc** 真消息驱动（替代轮询）。前端保留 React + Ant Design + ReactFlow。

## 整体架构图

```mermaid
graph TB
    subgraph Frontend["前端 Renderer (WebView)"]
        UI["React + Ant Design + ReactFlow<br/>智能体 / 群组 / 任务 / 监控"]
        API["services/api.ts<br/>invoke()"]
        Hook["hooks/useBusEvent.ts<br/>listen()"]
    end
    subgraph Tauri["Tauri v2 桥接"]
        Cmd["commands/<br/>#[tauri::command]"]
        Bus["bus.rs<br/>app.emit()"]
    end
    subgraph Backend["Rust 后端 (src-tauri/src/)"]
        Store["store/<br/>内存 + JSON 持久化"]
        SS["shared_state.rs<br/>A2A 共享状态中心<br/>tokio mpsc 收件箱"]
        Engine["engine.rs<br/>AgentEngine + 大脑 + 注册表"]
        RT["runtime.rs<br/>spawn Claude Code CLI"]
        LLM["llm.rs / prompts.rs<br/>OpenAI 兼容 HTTP"]
    end
    CLI["本地 Claude Code CLI 实例<br/>--print 非交互"]

    UI --> API
    UI -.事件.-> Hook
    API <-->|invoke / command| Cmd
    Bus -->|app.emit 事件| Hook
    Cmd --> Store
    Cmd --> Engine
    Engine --> SS
    Engine --> LLM
    Engine --> RT
    Store --> SS
    RT -->|"tokio::process"| CLI
```

## A2A 通信：SharedStateCenter「扔字条」

智能体之间**不点对点直连**，而是通过共享状态中心解耦通信——任何 agent 向中心「扔字条」（任务 / 通知），接收者通过 tokio mpsc channel 被唤醒取信，互不知道对方是否存在。

```mermaid
graph LR
    A["智能体 A"]
    B["智能体 B"]
    C["Coordinator"]
    SS(("SharedStateCenter<br/>任务队列 + 通知队列<br/>tokio mpsc 收件箱"))

    A -->|"push_task / push_notify"| SS
    B -->|"push_task / push_notify"| SS
    C -->|"push_task / push_notify"| SS
    SS -->|"rx.recv() 唤醒"| A
    SS -->|"rx.recv() 唤醒"| B
    SS -->|"rx.recv() 唤醒"| C
```

> 核心改造：TS 版用 `setInterval` 100ms 轮询收件箱；Rust 版改为每 (group, agent) 一个 mpsc channel，`push_*` 直接 `tx.send()` 唤醒目标引擎，引擎 `rx.recv().await` 阻塞等待——**零空转、真消息驱动**。

## 数据流图

### 完整任务执行流程

```mermaid
sequenceDiagram
    actor U as 用户
    participant F as 前端
    participant C as commands
    participant SS as SharedState
    participant CO as Coordinator Engine
    participant M as 成员 Engine
    participant CLI as Claude Code CLI

    U->>F: 提交需求
    F->>C: send_message (invoke)
    C->>SS: push_notify → coordinator
    C->>F: app.emit bus-event
    F-->>U: 显示自己的消息

    SS-->>CO: rx.recv() 唤醒
    CO->>CO: 调度大脑 LLM 决策
    CO->>CO: action = dispatch（生成计划）
    CO->>F: 官宣调度计划 (app.emit)
    CO->>SS: push_task → 成员（第 1 步）

    SS-->>M: rx.recv() 唤醒
    M->>M: claim_task
    M->>CLI: spawn --print task
    CLI-->>M: stdout 逐行日志
    M->>F: app.emit task_log
    M->>C: complete_task
    C->>SS: push_notify → coordinator（汇报）

    SS-->>CO: rx.recv() 收到汇报
    CO->>CO: action = continue（下一步）
    CO->>SS: push_task → 下一个成员
    Note over CO: 全部完成 → 汇总
    CO->>F: app.emit 汇总结果
    F-->>U: 查看交付物
```

### 群聊消息流

```mermaid
sequenceDiagram
    actor U as 用户
    participant F as 前端
    participant C as commands (send_message)
    participant SS as SharedState
    participant E as AgentEngine

    U->>F: 发消息（可能带 @mention）
    F->>C: send_message (invoke)
    C->>C: 存 Store + JSON
    C->>F: app.emit bus-event
    F-->>U: 看到自己的消息

    alt 有 @mention
        C->>SS: push_notify → 被 @ 的 agent
    else 无 @mention
        C->>SS: push_notify → coordinator
    end

    SS-->>E: rx.recv() 唤醒
    E->>E: 大脑 LLM 决策<br/>chat / execute / ask
    alt chat / ask
        E->>F: app.emit 回复
        F-->>U: 看到智能体回复
    else execute
        E->>SS: push_task → 自己
        Note over E: 进入任务执行流
    end
```

### Coordinator 调度状态机

```mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> Dispatch: 收到新需求<br/>action = dispatch
    Dispatch --> Dispatching: 生成计划<br/>派发第 1 步
    Dispatching --> Dispatching: 收到汇报<br/>action = continue<br/>派发下一步
    Dispatching --> Summarize: 所有步骤完成
    Dispatching --> Ask: 信息不足<br/>action = ask
    Ask --> Idle: 用户补充后
    Summarize --> Idle: 输出汇总
    Idle --> Chat: 闲聊<br/>action = chat
    Chat --> Idle
```

### 智能体间文件交换

```mermaid
sequenceDiagram
    participant A as 开发智能体A
    participant WD as 群组工作目录<br/>group_files/{groupId}/
    participant CO as Coordinator
    participant B as 测试智能体B

    A->>WD: 写代码 + npm install
    A->>CO: 汇报完成 (push_notify)
    CO->>CO: continue → 派发测试任务
    CO->>WD: spawn B 到同目录
    B->>WD: 读取代码 / 依赖 / 产物
    B->>B: 跑测试
    B->>CO: 汇报结果
```

## 核心设计决策

### 1. 两类智能体

|  | 群主 Coordinator | 子智能体 |
|--|------|---------|
| 本质 | LLM API 直调 + 调度大脑 | Claude Code CLI 进程 |
| 职责 | 意图分析、任务拆解、DAG 调度 | 开发、编译、测试 |
| 运行位置 | tokio task（主进程内） | `tokio::process::spawn` |
| 成本 | 低 | 中 |

### 2. 本地进程替代容器

子智能体都是 Claude Code CLI 实例，只需不同的 system prompt 和工作目录。本地进程启动更快，同一群组共享工作目录，天然支持文件交换和依赖复用。

### 3. A2A 共享状态中心（扔字条）

智能体间通信全部走 `SharedStateCenter`（`shared_state.rs`），禁止点对点直接调用。父/子 agent 真正成为独立任务实体，通过写/读中间队列通信。这是相对早期「直接路由」的关键架构升级。

### 4. 内存 + JSON 文件存储

单机桌面应用，数据全在内存，持久化用 JSON 文件（防抖 + 原子写）。事件用 Tauri `app.emit` / `listen`，无需查询优化、事务、跨进程通信。

### 5. DAG 依赖感知调度

无依赖的任务并行派发，有依赖的等前置完成后再派发。Coordinator 调度大脑按步骤依赖推进下游。

### 6. @mention 智能路由

群聊消息中的 @mention 自动扔字条到对应智能体收件箱。30 秒防循环机制，避免两个智能体互相 @ 死循环。

## 技术栈

| 层 | 技术 |
|----|------|
| 桌面框架 | Tauri v2（Rust） |
| 前端 | React + Vite + Ant Design + ReactFlow |
| 后端 | Rust（tokio async runtime） |
| 群主调度 | tokio task + mpsc channel 消息驱动 |
| 群主 LLM | OpenAI 兼容 HTTP API 直调（reqwest） |
| 子智能体运行时 | 本地 Claude Code CLI（`tokio::process::Command`） |
| 数据存储 | 内存 + JSON 文件（防抖 + 原子写） |
| A2A 通信 | SharedStateCenter + tokio mpsc 收件箱 |
| 实时事件 | Tauri `app.emit` / `listen` |
| 进程间通信 | Tauri `invoke` / `#[command]` |
| 跨平台 | macOS / Windows / Linux（tauri bundler） |

## 默认角色模板

| 角色 | 职责 | 技能（自动映射） |
|------|------|---------|
| 前端工程师 | 页面开发、组件实现 | React/Vue, CSS/Tailwind, Jest/Vitest |
| 后端工程师 | API 开发、数据库操作 | Python/FastAPI, SQL, API 设计 |
| 测试工程师 | 测试用例、执行测试 | 测试用例设计, pytest, 缺陷跟踪 |
| 代码审查员 | 代码质量、安全审查 | 代码审查, 安全检查, 架构评估 |
| DevOps 工程师 | 部署、CI/CD | Docker, CI/CD, 部署脚本 |

## 快速开始

```bash
# 安装依赖
npm install

# 开发模式（启动 Tauri + Vite dev server）
npm run tauri:dev

# 打包桌面应用
npm run tauri:build
```

开发前需注入 LLM 环境变量（WSL/Linux）：

```bash
set -a; source .env; set +a   # OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL
```

## 环境要求

- Rust toolchain（stable）+ Tauri 系统依赖
  - Linux：`libwebkit2gtk-4.1-dev libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev`
- Node.js 20+
- Claude Code CLI 已安装（或设置 `CLAUDE_CODE_PATH` 环境变量）
- LLM API 密钥（OpenAI / DeepSeek / 其他兼容端点）

## 项目结构

```
multi-Agent/
  src-tauri/                    # Rust + Tauri 后端
    src/
      lib.rs / main.rs          # Tauri 入口 + 应用装配
      store/                    # serde 数据模型 + JSON 持久化 + SharedStateCenter
        mod.rs                  # 内存 Store（多 Map 索引）
        types.rs                # 数据类型（与旧 data/*.json 兼容）
        persistence.rs          # 防抖 + 原子写 JSON
        shared_state.rs         # A2A 共享状态中心 + mpsc 收件箱
      engine.rs                 # AgentEngine + 大脑 + 调度大脑 + 注册表
      runtime.rs                # spawn Claude Code CLI + 生成 CLAUDE.md
      llm.rs / prompts.rs       # OpenAI 兼容 HTTP 客户端 + 提示词
      commands/                 # #[tauri::command]（agent/group/task/message）
      bus.rs                    # Tauri app.emit 事件推送
      rt.rs                     # async runtime 桥接
    tauri.conf.json             # 窗口 / 打包配置
  src/                          # 前端 Renderer（React）
    pages/                      # 页面组件
    components/                 # 通用组件
    services/api.ts             # invoke() 调用层
    hooks/useBusEvent.ts        # Tauri listen 实时事件 hook
  data/                         # 运行时数据（JSON + 群组文件，开发期路径）
```

## 路线图

- [x] Tauri v2 + Rust 后端重写
- [x] A2A 引擎 tokio mpsc 消息驱动化
- [ ] Coordinator workflow 全流程迁移（analyze/decompose/monitor/summarize）
- [ ] settings IPC 迁移为 Tauri command
- [ ] 清理 Electron 残留（`electron/` `dist-electron/` `main/`）
- [ ] 端到端 LLM 协作流实测验证

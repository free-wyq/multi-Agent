# Multi-Agent 协作桌面应用

多智能体协作框架，解决软件工程虚拟交付问题。

## 架构

- **桌面框架**：Electron + TypeScript
- **前端**：React + Vite + Ant Design + ReactFlow
- **主进程**：Node.js/TS — 状态管理、进程管理、LLM 调用
- **子智能体**：本地 Claude Code CLI 实例（child_process.spawn）
- **数据持久化**：内存 Map + JSON 文件
- **事件总线**：进程内 EventEmitter
- **LLM 调用**：直接 HTTP API（OpenAI 兼容端点）
- **A2A 通信**：SharedStateCenter 共享状态中心（扔字条式解耦）
  - 任务队列（TaskQueue）+ 通知队列（NotifyQueue）
  - 父/子 agent 对等，通过轮询收件箱通信
  - 详见 [docs/architecture-a2a.md](docs/architecture-a2a.md)

## 快速开始

```bash
# 安装依赖
npm install

# 开发模式
npm run dev

# 打包
npm run build
```

## 项目结构

```
electron/
  main.ts                # Electron 主进程入口
  preload.ts             # preload 脚本，暴露 IPC API
src/                     # Renderer 进程（React 前端）
  pages/                 # 页面组件
  components/            # 通用组件
  services/api.ts        # IPC API 调用层
  hooks/useBusEvent.ts    # 实时事件 hook
  ipc/                   # IPC 通道定义 + 类型
main/                    # 主进程业务逻辑
  store/                 # 内存状态 + JSON 持久化
  bus/                   # EventEmitter 事件总线
  coordinator/           # 工作流 + LLM + 提示词
  agent-engine/          # 智能体引擎 + 大脑 + 注册表
  runtime/               # Claude Code CLI 进程管理
  ipc-handlers/          # IPC 处理器
data/                    # 运行时数据（JSON + 群组文件）
```

## 核心概念

- **智能体（Agent）**：角色定义 + system prompt，映射到 CLAUDE.md
- **群组（Group）**：协作单元，群主 + 成员
- **任务（Task）**：DAG 依赖调度，A2A 协议状态机
- **消息（Message）**：智能体间通信，@mention 路由
- **协调者（Coordinator）**：需求分析 → 任务拆解 → 调度 → 监控 → 汇总

## 环境要求

- Node.js 20+
- Claude Code CLI 已安装（或设置 CLAUDE_CODE_PATH 环境变量）
- LLM API 密钥（OpenAI / DeepSeek / 其他兼容端点）

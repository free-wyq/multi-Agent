# A2A 通信全景架构

> 本架构图描述了「父 agent → 共享状态中心 → 子 agent」的解耦协作模式。

![通信全景总览](../images/a2a-architecture.png)
*(请将原图 `通信全景总览` 保存到 `docs/images/a2a-architecture.png`)*

---

## 核心概念

### 1. 父 agent（Coordinator）
- 角色：群主、协调者
- 本地上下文：独立（对话历史、项目文件视图、工具权限）
- 行为：发出任务 → 扔字条到共享状态 → 等待结果通知

### 2. 子 agent（Worker Agent）
- 角色：执行者（前端、后端、测试等）
- 本地上下文：独立（对话历史、项目文件视图、工具权限）
- 行为：轮询自己的收件箱 → 处理任务 → 扔通知回共享状态

### 3. 共享状态中心（SharedStateCenter）
位于 `main/store/shared-state.ts`，替代了旧的点对点路由：

| 结构 | 说明 |
|---|---|
| **任务队列 (TaskQueue)** | `pushTask()` 任何 agent 都可向某个 agent 扔任务字条 |
| **通知队列 (NotifyQueue)** | `pushNotify()` 任务完成/状态变更等广播通知 |
| **元数据** | group_id、状态映射、DAG 依赖关系 |

### 4. 扔字条（Write-and-Forget）
- 通信方式：agent A 向共享状态写入任务 → agent B 主动轮询拉取
- agent A **不知道** agent B 是否在线、何时消费
- 完全解耦，符合 A2A 协议的核心理念

### 5. 隔离边界
- 每个 agent 拥有独立的 `WORKDIR`（`data/group_files/{groupId}/{agentId}/`）
- 独立的 `CLAUDE.md` 上下文文件
- 独立的 `settings.json` 权限配置

### 6. 异步循环
- 子 agent：`setInterval` 每 100ms 轮询 `sharedState.pollInbox()`
- coordinator：`while` 循环每 2s 轮询 `notifyQueue` 等待子任务完成
- 事件总线：自动将共享状态变更推送到 Electron Renderer 前端

---

## 与当前代码的对应关系

| 图中概念 | 代码实现 |
|---|---|
| 父 agent | `AgentEngine` (role='coordinator') + `CoordinatorWorkflow` |
| 子 agent | `AgentEngine` (普通 role) + `ClaudeCodeRuntime` |
| 收件箱 (消息队列) | `SharedStateCenter.pollInbox(groupId, agentId)` |
| 通知 (任务完成) | `SharedStateCenter.pushNotify({ type: 'task_complete' })` |
| 状态对象 | `main/store/shared-state.ts` 中的 `taskQueues` + `notifyQueues` |
| 隔离边界 | 每个 agent 的独立 `work_dir` + `CLAUDE.md` |
| 扔字条 | `sharedState.pushTask()` / `sharedState.pushNotify()` |
| 送信 | `sharedState.completeTask()` → 自动触发 `task_complete` 通知 |

---

## 数据流示例

### 用户提交需求

```
用户 MESSAGE_SEND @coordinator "帮我开发一个登录功能"
  ↓
main/ipc-handlers/message.handlers.ts
  通过 sharedState.pushNotify() 扔字条到 coordinator 的收件箱
  ↓
AgentEngine (coordinator) 轮询 inbox → 拿到通知
  brainDecide() 判断为 execute
  调用 CoordinatorWorkflow.run()
    analyze → decompose → dispatch
    dispatch 通过 sharedState.pushTask() 向各子 agent 扔字条
  ↓
AgentEngine (子 agent) 轮询 inbox → claimTask → 执行
  非阻塞 spawn Claude Code CLI
  执行完成后 sharedState.completeTask() → 自动发布 task_complete 通知
  ↓
CoordinatorWorkflow.monitor() 轮询 notifyQueue
  收到 task_complete → 触发下游任务 dispatch
  全部完成后 summarize → 发布 coordinator_reply 通知
  ↓
前端通过 eventBus 收到所有消息/日志，实时更新 UI
```

### @mention 路由

```
用户发消息 "@后端工程师 请提供登录API接口"
  ↓
message.handlers.ts triggerA2ARouting()
  解析 @mention → 找到 backend-engineer 的 agentId
  sharedState.pushNotify({ receiver_id: agentId })
  ↓
AgentEngine(backend) 轮询 inbox → 收到通知
  brainDecide() → chat / execute
  回复内容中如果包含 @其他agent → sharedState.pushTask()
  ↓
其他 agent 在自己的轮询中消费
```

---

## 关键改进点

### Before（点对点）
```ts
// 路由者直接调用 engine 的方法
agentRegistry.routeMessage(targetId, msg, groupId)
// → engine.pushMessage(msg) → 直接写入对方的 inbox 数组
```

### After（共享状态解耦）
```ts
// 发布者向共享中心写入，不关心接收者是否存在
sharedState.pushTask({ group_id, sender_id, receiver_id, content })
// 接收者主动轮询
const { tasks, notifies } = sharedState.pollInbox(group_id, agentId)
```

---

## 持久化

每个群组的队列状态存储在 `data/queues/{groupId}.json`：

```json
{
  "group_id": "xxx",
  "tasks": [...],
  "notifies": [...]
}
```

- 500ms 防抖写入
- 原子写入（先 .tmp 再 rename）
- 启动时从 JSON 恢复

---

## 常驻 vs 一次性

| | 旧版 | 新版 |
|---|---|---|
| AgentEngine | 常驻 ✅ | 常驻 ✅ |
| Inbox | 私有数组（push/shift） | 轮询 sharedState |
| 执行 | 阻塞 `await runtime.execute()` | 非阻塞 Promise + then |
| Coordinator | 主进程类 | 对等 AgentEngine |
| 通信 | 点对点直接调用 | 共享状态扔字条 |

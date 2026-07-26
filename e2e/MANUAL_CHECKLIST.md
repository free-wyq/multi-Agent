# 任务20c 手动清单 — 流式动画 / reload 持久化（非自动化项）

> 以下两项依赖真实流式时序 + 浏览器 reload 行为，Playwright 自动化覆盖成本高且
> 易 flaky（流式动画依赖肉眼观测连续性；reload 持久化依赖 WS 重连 + 历史回灌时序）。
> 按 .task.md 任务20c 要求，这两类走人工 checklist——每轮 e2e 跑完后人工过一遍。

## 前置模块 SKIP 标记

任务20c 流程8-12 的前置模块（MCP/记忆/定时/IM/Token 仪表盘）均已在前期任务落地：
- MCP（任务16a/b/c）✅ 已落，流程8 自动化已覆盖（08-mcp.spec.ts）。
- 记忆（任务17a/b/c）✅ 已落，流程9 自动化已覆盖（09-memory.spec.ts）。
- 定时（任务18a/b/c）✅ 已落，流程10 自动化已覆盖（10-schedule.spec.ts）。
- IM（任务19a/b/c/d/e）✅ 已落，流程11 自动化已覆盖（11-im.spec.ts）。
- Token 仪表盘（任务15a/b/c）✅ 已落，流程12 自动化已覆盖（12-usage.spec.ts）。

无前置未完模块需 SKIP——流程8-12 全部自动化覆盖。

## 清单 A：流式动画（人工肉眼验）

流式动画依赖「逐字流出 + 思考展开 + 完成态全展开」的视觉连续性，自动化只能断
终态（气泡出现探针串），无法验「流式期逐字 + 光标闪烁 + 思考区展开」的过程感。

### A-1 单聊流式逐字 + 闪烁光标
- [ ] 开 seed agent（前端工程师）单聊，发一条普通消息（如「你好」）。
- [ ] 观察回复气泡：**流式期**（isStreaming=true）气泡有淡蓝描边（`.chat-bubble--streaming`），
      正文尾部有闪烁光标，文本逐字增长（mock LLM 是逐字 SSE，真实模型同理）。
- [ ] 流式完成后：描边消失，光标消失，正文定稿，气泡转为持久化态。
- 参考实现：`src/components/ChatMessageBubble.tsx` isStreaming 分支 + `.chat-bubble--streaming`。

### A-2 协调者流式状态行 + 思考区展开
- [ ] 开群聊（演示协作组），发「派工」关键词触发 coordinator dispatch。
- [ ] 观察协调者流式气泡：状态行显 `model · Ns · ↓ N tokens（含 N 推理）· thinking`，
      随 token 累积实时更新（coordStats ~200ms 节流）。
- [ ] 若模型为推理模型（如 glm-5.2 强推理）：思考区（reasoning）在正文流出前先逐字
      流出，思考活跃期（isStreaming && reasoning && 正文未流出）自动展开全文。
- [ ] 回答完成后：reasoning 折叠区标题显「思考过程（N tokens）」，正文定稿。
- 参考实现：`StreamingBubbleList.tsx` 协调者流式段 + `ChatMessageBubble.tsx` reasoningActive。

### A-3 完成态自动全展开
- [ ] 任意流式气泡完成后：`autoExpandOnDoneRef` 触发，processPanel（思考/工具/最终生成
      Timeline 三段折叠区）自动展开（commit e8fe6b6，原 bubble-expand-on-done 已并入）。
- [ ] 历史消息回显（切群/重连后）：mount 即完成态，processPanel 默认展开（`useState(() => !isStreaming)`）。
- 参考实现：`ChatMessageBubble.tsx:550-555` autoExpandOnDoneRef effect。

### A-4 卡片 JSON 转义降级（已知约束，人工确认未静默丢弃）
- [ ] 发「[卡片]」关键词触发结构化卡片，mock 回合法 JSON 卡片。
- [ ] 若 LLM 回的卡片 JSON 内双引号未转义（真实模型偶发）：前端降级为 code 块展示，
      不静默丢弃（设计 §6）。mock 服务器回的是合法 JSON（已转义），故自动化能过；
      人工确认真实模型场景降级路径正常。
- 参考实现：`ChatMessageBubble.tsx` splitContentByCards 降级分支（任务10d 单测锁）。

## 清单 B：reload 持久化（人工验历史回灌）

reload 持久化依赖「页面刷新 → WS 重连 → loadMessages 拉历史 → logs effect 重扫
markReplied 回填退场状态」的完整时序，自动化 reload + 断言历史在技术上可行但
对 WS 重连时序敏感易 flaky，走人工更稳。

### B-1 单聊消息 reload 后历史在
- [ ] 开 seed agent 单聊，发 2-3 条消息，确认有 mock 回复气泡。
- [ ] 浏览器刷新（Ctrl+R / Cmd+R）。
- [ ] 重新进入该 agent 单聊：历史消息（用户气泡 + agent 回复气泡）应全部回显，
      顺序与刷新前一致。
- 参考实现：`ChatPanel.tsx` loadMessages（messageApi.list）+ chatGroupId effect。

### B-2 群聊消息 + 计划卡 reload 后历史在
- [ ] 开群聊发「派工」→ PlanConfirmCard 出现 → 确认 → worker 回复。
- [ ] 浏览器刷新。
- [ ] 重新进入该群：用户消息 + 协调者计划卡 + worker 回复气泡全部回显。
- [ ] 退场状态（已回复的 task 标记）正确回填（reload-safe，markReplied 经 logs effect 重扫）。
- 参考实现：`ChatPanel.tsx:184-185` reload-safe 注释 + markReplied 回填逻辑。

### B-3 流式中断后 reload（边界态）
- [ ] 发消息触发流式，**流式中途**刷新页面（模拟连接断开）。
- [ ] 重新进入：流式中断的气泡若已 persist（agent_reply 落库）则回显定稿文本；
      若未 persist（纯 streaming buffer 未落库）则不回显（预期——streaming 是内存态）。
- [ ] 确认无重复气泡（去重：agent_reply 带 task_id 入 markReplied 集合，prev.has 去重）。
- 参考实现：`ChatPanel.tsx:360-375` reply 落地回填退场集合 + 去重。

## 自动化覆盖对照（流程8-12）

| 流程 | spec | 自动化覆盖 | 人工清单 |
|------|------|-----------|---------|
| 8 MCP | 08-mcp.spec.ts | CRUD + 自省 echo + 挂载 | — |
| 9 记忆 | 09-memory.spec.ts | CRUD + FTS5 检索 | — |
| 10 定时 | 10-schedule.spec.ts | CRUD + 立即执行 + 历史 | — |
| 11 IM | 11-im.spec.ts | CRUD + 测试探针 | — |
| 12 Token 仪表盘 | 12-usage.spec.ts | 渲染 + 数据回流 | — |
| 流式动画 | — | 终态断言（探针串） | 清单 A（逐字/光标/思考展开） |
| reload 持久化 | — | — | 清单 B（历史回灌/退场回填） |

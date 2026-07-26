import { useCallback, useState } from 'react'

/**
 * useBubbleRetire — [任务9b] 定稿气泡退场状态 hook。
 *
 * 职责单一：维护「已退场定稿气泡的 task_id 集合」(`repliedTaskIds`)——即 reply 已落地、
 * 定稿气泡（finalizedBubbles）应退场的 task。从 ChatPanel logs 桥接 effect 抽出，让退场
 * 状态管理自成一块，ChatPanel 不再内联 `setRepliedTaskIds` 三处。
 *
 * 暴露三个口：
 *  - `repliedTaskIds: Set<string>`：当前已退场 task_id 集合（finalizedBubbles memo 读它
 *    `repliedTaskIds.has(e.taskId)` 判退场，O(1) 集合查）。
 *  - `markReplied(taskId)`：reply 落地时回填——agent_reply 带 task_id 即该 task 的持久化
 *    回复已落地，标记其定稿气泡可退场。函数式更新 + prev.has 去重，避免重复 add 触发无谓重渲染。
 *  - `reset()`：切群时清空——新群退场状态与旧群无关（旧群 task_id 概率极低但语义独立应清）。
 *
 * 为何用 state 不用 ref（缺陷5 修复，原 B23 用 ref 埋时序 bug）：
 * emit 顺序是 emit_task_completed（registry:421）先于 _reply announce（registry:460），
 * agent_reply 落地时若用 ref.add（不触发渲染），finalizedBubbles memo 在 render 期算时
 * ref 仍空 → 渲染 finalized bubble；随后 setChatMessages 触发的重渲染里 memo deps
 * [events/streaming/agentStatuses] 均未变 → memo 返回缓存 → finalized bubble 卡住不退场
 * （即「一轮多泡」缺陷的 finalized 那条）。改用 state 并把 repliedTaskIds 纳入 memo deps：
 * reply 落地 markReplied → setRepliedTaskIds → 触发重渲染 → memo 重算 → has 命中 → 退场。
 * perf 不回归：repliedTaskIds 仅在 agent_reply 落地时变化（每 task 一次，低频），不像
 * chatMessages 每 token/log 换引用。reload-safe：重连/切群回灌历史 agent_reply 经 logs
 * effect 重扫 markReplied 回填（历史 agent_reply 带 task_id 同样入集合，prev.has 去重），
 * 故 reload 后退场状态与 live 一致。
 */
export function useBubbleRetire() {
  const [repliedTaskIds, setRepliedTaskIds] = useState<Set<string>>(new Set())

  /** reply 落地回填退场集合——agent_reply 带 task_id 即该 task 持久化回复已落地，
   *  标记其定稿气泡可退场。函数式更新 + prev.has 去重，避免重复 add 触发无谓重渲染。 */
  const markReplied = useCallback((taskId: string) => {
    setRepliedTaskIds((prev) => {
      if (prev.has(taskId)) return prev
      const next = new Set(prev)
      next.add(taskId)
      return next
    })
  }, [])

  /** 切群时清空——新群退场状态与旧群无关，避免旧群退场状态泄漏到新群。 */
  const reset = useCallback(() => {
    setRepliedTaskIds(new Set())
  }, [])

  return { repliedTaskIds, markReplied, reset }
}

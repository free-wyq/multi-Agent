/**
 * Coordinator Brain — 群主专用调度大脑
 *
 * 与通用 brainDecide 不同：
 * - 负责分析用户意图并生成分步骤调度计划
 * - 跟踪调度进度（当前执行到第几步）
 * - 收到成员汇报后判断是否继续下一步或汇总
 */

import { chatCompletion } from '../coordinator/llm'
import type { LLMConfig } from '../store/types'

export interface DispatchStep {
  step: number           // 步骤序号（1-based）
  agent_id: string
  agent_name: string
  instruction: string    // 给该成员的明确指令
  depends_on: number[]  // 前置步骤序号
  status: 'pending' | 'dispatched' | 'completed' | 'failed'
  result?: string        // 该步骤的汇报结果
}

export interface CoordinatorBrainDecision {
  action: 'chat' | 'dispatch' | 'ask' | 'continue'
  content: string        // 在群里回复的内容
  plan?: DispatchStep[]  // 调度计划（action=dispatch 时有效）
  next_step?: number     // 下一步骤序号（action=continue 时有效）
}

const COORDINATOR_SYSTEM = `
你是群主，团队协调中枢。你的职责：
1. 理解用户/成员消息，决定如何响应
2. 如果需要多人协作 → 输出串行调度计划
3. 收到成员汇报后 → 判断继续下一步还是汇总

规则：
- 尽量串行调度（先A后B），减少复杂度
- 每个步骤指令要明确、可验证
- 如果需求不清晰 → 先 ask 确认
- 如果所有步骤完成 → 汇总给用户
`

function buildPrompt(params: {
  name: string
  members: { id: string; name: string; role: string }[]
  conversation: string
  dispatchState: string
  sender: string
  message: string
}): string {
  return `${COORDINATOR_SYSTEM}

你的群名：${params.name}
群成员：
${params.members.map(m => `- ${m.name}（${m.role}）id=${m.id}`).join('\n')}

对话上下文：
${params.conversation || '（无）'}

当前调度状态：
${params.dispatchState || '（空闲，无进行中的调度）'}

收到消息：
来自「${params.sender}」：${params.message}

请严格按照以下 JSON 格式回复（不要 markdown 代码块，只输出纯 JSON）：
{
  "action": "chat | dispatch | ask | continue",
  "content": "群聊回复内容",
  "plan": [
    {"step": 1, "agent_id": "xxx", "agent_name": "成员名", "instruction": "具体指令", "depends_on": []}
  ],
  "next_step": 0
}

action 说明：
- chat：直接回复，不需要调度
- dispatch：用户有新需求，输出步骤计划（plan）
- ask：信息不足，向用户提问
- continue：收到成员汇报，继续下一步（用 next_step 指定）

plan 只在 dispatch 时必填。next_step 只在 continue 时必填。
`
}

export async function coordinatorBrainDecide(
  config: LLMConfig,
  params: {
    name: string
    members: { id: string; name: string; role: string }[]
    conversation: string
    dispatchState: string
    sender: string
    message: string
  },
): Promise<CoordinatorBrainDecision> {
  const prompt = buildPrompt(params)

  try {
    const raw = await chatCompletion(config, [
      { role: 'system', content: COORDINATOR_SYSTEM },
      { role: 'user', content: prompt },
    ])

    const jsonMatch = raw.match(/\{[\s\S]*\}/)
    if (!jsonMatch) {
      throw new Error('LLM 未返回 JSON')
    }

    const data = JSON.parse(jsonMatch[0])
    return {
      action: ['chat', 'dispatch', 'ask', 'continue'].includes(data.action) ? data.action : 'chat',
      content: data.content || '',
      plan: data.plan || [],
      next_step: data.next_step ?? 0,
    }
  } catch (err) {
    console.error('[CoordinatorBrain] 决策失败:', err)
    return {
      action: 'chat',
      content: '抱歉，我这边理解有点困难，能再说一次吗？',
    }
  }
}

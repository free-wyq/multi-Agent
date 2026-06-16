/**
 * Agent Engine 大脑
 *
 * - 用 LLM 判断消息类型：chat / execute / ask
 * - 解析 JSON 响应为 BrainDecision
 */

import { chatCompletion, getDefaultConfig } from '../coordinator/llm'
import { formatBrainPrompt } from '../coordinator/prompts'
import type { BrainDecision, LLMConfig } from '../store/types'

/**
 * 调用大脑并解析结果
 */
export async function brainDecide(
  config: LLMConfig,
  role: string,
  name: string,
  context: string,
  message: string,
): Promise<BrainDecision> {
  const prompt = formatBrainPrompt(role, name, context, message)

  try {
    const raw = await chatCompletion(config, [
      { role: 'user', content: prompt },
    ])

    // 提取 JSON 块
    const jsonMatch = raw.match(/\{[\s\S]*\}/)
    if (jsonMatch) {
      const data = JSON.parse(jsonMatch[0])
      return {
        action: data.action || 'chat',
        content: data.content || '',
        reasoning: data.reasoning || '',
      }
    }
  } catch (err) {
    console.warn('大脑决策失败:', err)
  }

  return {
    action: 'chat',
    content: '抱歉，我这边有点卡壳，能再说一遍吗？',
    reasoning: 'parse_failed',
  }
}

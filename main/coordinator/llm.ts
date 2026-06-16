/**
 * OpenAI 兼容 HTTP 客户端
 *
 * 替代 LangChain：
 * - 直接 HTTP 调 /v1/chat/completions
 * - 支持 JSON mode 结构化输出
 * - 支持 OpenAI / DeepSeek / 其他兼容端点
 */

import type { LLMConfig } from '../store/types'

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant'
  content: string
}

/**
 * 调用 LLM，返回纯文本
 */
export async function chatCompletion(config: LLMConfig, messages: ChatMessage[]): Promise<string> {
  const response = await fetch(`${config.baseUrl}/chat/completions`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${config.apiKey}`,
    },
    body: JSON.stringify({
      model: config.model,
      messages,
      temperature: config.temperature,
      max_tokens: config.maxTokens,
    }),
  })

  if (!response.ok) {
    const text = await response.text()
    throw new Error(`LLM API error ${response.status}: ${text}`)
  }

  const data = await response.json() as Record<string, unknown>
  const choices = data.choices as Array<{ message: { content: string } }>
  return choices[0].message.content
}

/**
 * 调用 LLM 并解析为 JSON（结构化输出）
 * 使用 response_format: { type: "json_object" }
 */
export async function structuredInvoke<T>(
  config: LLMConfig,
  prompt: string,
  schemaDescription: string,
): Promise<T> {
  const messages: ChatMessage[] = [
    {
      role: 'system',
      content: `你是一个专业的任务分析助手。请严格按照 JSON 格式回复，不要使用 markdown 代码块标记，只输出纯 JSON。\n\nSchema: ${schemaDescription}`,
    },
    { role: 'user', content: prompt },
  ]

  const response = await fetch(`${config.baseUrl}/chat/completions`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${config.apiKey}`,
    },
    body: JSON.stringify({
      model: config.model,
      messages,
      temperature: 0,
      max_tokens: config.maxTokens,
      response_format: { type: 'json_object' },
    }),
  })

  if (!response.ok) {
    const text = await response.text()
    throw new Error(`LLM API error ${response.status}: ${text}`)
  }

  const data = await response.json() as Record<string, unknown>
  const choices = data.choices as Array<{ message: { content: string } }>
  const raw = choices[0].message.content

  // 提取 JSON 块（容错：LLM 可能仍返回 markdown 包裹）
  const jsonMatch = raw.match(/\{[\s\S]*\}/)
  if (!jsonMatch) {
    throw new Error(`Failed to parse LLM JSON response: ${raw.substring(0, 200)}`)
  }

  return JSON.parse(jsonMatch[0]) as T
}

/**
 * 获取默认 LLM 配置
 * 从应用设置中读取
 */
export function getDefaultConfig(): LLMConfig {
  // 从环境变量读取（开发模式兼容）
  const apiKey = process.env.OPENAI_API_KEY || process.env.ANTHROPIC_API_KEY || ''
  const baseUrl = process.env.OPENAI_BASE_URL || 'https://api.openai.com/v1'
  const model = process.env.LLM_MODEL || 'glm-5.1'

  return {
    apiKey,
    baseUrl,
    model,
    temperature: 0,
    maxTokens: 4096,
  }
}

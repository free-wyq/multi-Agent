/**
 * 提示词模板
 *
 * 从 Python coordinator/llm.py, agent_engine/brain.py, api/messages.py 提取
 */

// ── 意图分析 ─────────────────────────────────────────────────

export function buildAnalyzePrompt(requirement: string, rolesHint: string): string {
  return `分析以下需求，识别涉及的智能体角色：\n\n${requirement}\n${rolesHint}\n\n注意：involved_roles 必须是角色标识（如 backend-engineer），不要使用中文描述。`
}

// ── 任务拆解 ─────────────────────────────────────────────────

export function buildDecomposePrompt(
  requirement: string,
  intentAnalysis: string,
  rolesContext: string,
): string {
  return `用户需求：${requirement}\n\n意图分析：${intentAnalysis}\n\n可用角色（assigned_role 必须使用角色标识）：\n${rolesContext}\n\n请将需求拆解为子任务，指定每个任务的执行角色和依赖关系。\n注意：assigned_role 必须是角色标识（如 backend-engineer），不要使用中文描述。\n注意：depends_on 是前置子任务的 0-based 序号，第1个子任务的序号是0。`
}

// ── 结果汇总 ─────────────────────────────────────────────────

export function buildSummarizePrompt(summaries: string[], requirement: string): string {
  return `以下是各子任务的执行结果：\n\n${summaries.join('\n')}\n\n请用简洁的语言汇总整体执行结果。原始需求：${requirement}`
}

// ── 大脑（Agent Engine）──────────────────────────────────────

export const BRAIN_PROMPT = `你是一名专业的 {role}，名字叫 {name}。

当前对话上下文：
{context}

用户发来消息：{message}

请判断：
- chat：如果只是讨论、咨询、确认方案 → 直接回复用户
- execute：如果用户明确要求你动手干活（写代码、改配置、运行命令） → 输出给执行器的任务指令
- ask：如果意图不清/缺少必要信息 → 向用户提问

执行任务时的要求：
1. 把任务拆解为清晰的执行指令（一句话说明要做什么）
2. 指定必须遵守的约束（如"用 FastAPI"、"不要改现有路由"）
3. 如果需要先和用户确认方案，用 ask 模式

重要：如果你需要请求其他团队成员协助，在回复中用 @对方名字 的方式提及对方，系统会自动将消息路由给他们。
例如：@后端工程师 请提供登录API接口

请严格按照以下 JSON 格式回复（不要用 markdown 代码块标记，只输出纯 JSON）：
{{
  "action": "chat | execute | ask",
  "content": "你的回复或任务指令",
  "reasoning": "决策理由"
}}
`

export function formatBrainPrompt(role: string, name: string, context: string, message: string): string {
  return BRAIN_PROMPT
    .replace('{role}', role)
    .replace('{name}', name)
    .replace('{context}', context)
    .replace('{message}', message)
}

// ── Coordinator 自动回复 ──────────────────────────────────────

export function buildCoordinatorReplyPrompt(
  message: string,
  coordinatorName: string,
  memberNames: string[],
): string {
  return `你是群主"${coordinatorName}"，负责协调团队成员协作。

群成员：${memberNames.join('、')}

用户消息：${message}

请以群主身份回复用户。如果需要委派任务给特定成员，使用 @成员名 提及对方。
回复要简洁专业。`
}

// ── 结构化输出 Schema 描述 ──────────────────────────────────

export const INTENT_ANALYSIS_SCHEMA = `{
  "analysis": "对用户需求的理解和分析（字符串）",
  "involved_roles": ["角色标识列表，如 frontend-engineer, backend-engineer"]
}`

export const TASK_DECOMPOSITION_SCHEMA = `{
  "subtasks": [
    {
      "title": "任务标题",
      "description": "任务详细描述",
      "assigned_role": "执行角色标识，如 frontend-engineer",
      "depends_on": [0]
    }
  ],
  "reasoning": "拆解理由"
}`

// ── 角色描述 ─────────────────────────────────────────────────

export const ROLE_DESCRIPTIONS: Record<string, string> = {
  'frontend-engineer': '前端开发工程师 — 负责页面开发、组件实现、样式编写',
  'backend-engineer': '后端开发工程师 — 负责 API 开发、数据库操作、业务逻辑',
  'tester': '测试工程师 — 负责编写测试用例、执行测试、报告缺陷',
  'reviewer': '代码审查员 — 负责代码审查、质量把关、最佳实践建议',
  'devops': 'DevOps 工程师 — 负责部署、CI/CD、环境配置',
}

export function buildRolesContext(involvedRoles: string[]): string {
  return involvedRoles
    .map(role => `- ${role}: ${ROLE_DESCRIPTIONS[role] || `${role} — 自定义角色`}`)
    .join('\n')
}

export function buildRolesHint(availableRoles: { role: string; name: string }[]): string {
  if (!availableRoles.length) return ''
  return '\n\n可用角色（必须从中选择）：\n' +
    availableRoles.map(r => `- ${r.role}: ${r.name}`).join('\n')
}

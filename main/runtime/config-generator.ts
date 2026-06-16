/**
 * CLAUDE.md + settings.json 配置生成
 *
 * 从 Python runtime/config_generator.py 移植
 */

const CLAUDE_MD_TEMPLATE = `# {name} 的角色定义

## 角色

{name} — {role_description}

## 职责

{responsibilities}

## 技能

{skills_section}

## 约束

{constraints}

## 环境

- 工作目录为当前群组共享目录
- 产出物放到 shared/ 目录供其他智能体共享
- 最终交付物放到 output/
- 任务完成后主动通知群主

## 交接规范

完成子任务后：
1. 将产出文件复制到 shared/
2. 生成简要 summary 文件
3. 退出码 0 表示成功，非 0 表示失败
`

const ROLE_DESCRIPTIONS: Record<string, string> = {
  'frontend-engineer': '前端开发工程师 — 负责页面开发、组件实现、样式编写',
  'backend-engineer': '后端开发工程师 — 负责 API 开发、数据库操作、业务逻辑',
  'tester': '测试工程师 — 负责编写测试用例、执行测试、报告缺陷',
  'reviewer': '代码审查员 — 负责代码审查、质量把关、最佳实践建议',
  'devops': 'DevOps 工程师 — 负责部署、CI/CD、环境配置',
}

const ROLE_RESPONSIBILITIES: Record<string, string> = {
  'frontend-engineer': `- 根据需求实现前端页面与组件
- 确保 UI 符合设计规范
- 编写前端单元测试
- 与后端 API 对接`,
  'backend-engineer': `- 设计和实现 REST/GraphQL API
- 数据库模型设计与查询优化
- 业务逻辑实现
- 编写接口文档`,
  'tester': `- 编写测试计划与用例
- 执行功能测试和回归测试
- 缺陷跟踪与报告
- 自动化测试脚本编写`,
  'reviewer': `- 审查代码质量与规范
- 检查安全漏洞
- 建议最佳实践
- 确保架构一致性`,
  'devops': `- 配置部署环境
- 编写 Dockerfile/docker-compose
- CI/CD 流水线配置
- 监控与日志管理`,
}

const DEFAULT_CONSTRAINTS = `- 只能在工作目录下操作文件
- 不要访问外部生产系统
- 不要泄露敏感凭据
- git commit 前确认 commit message 清晰`

// ── settings.json ──────────────────────────────────────────

const ALL_TOOLS = [
  'Bash',
  'Read',
  'Write',
  'Edit',
  'WebSearch',
  'WebFetch',
]

const ROLE_DEFAULT_TOOLS: Record<string, string[]> = {
  'frontend-engineer': ['Bash', 'Read', 'Write', 'Edit', 'WebSearch', 'WebFetch'],
  'backend-engineer': ['Bash', 'Read', 'Write', 'Edit', 'WebSearch', 'WebFetch'],
  'tester': ['Bash', 'Read', 'Write', 'Edit', 'WebSearch'],
  'reviewer': ['Read', 'Edit', 'WebSearch', 'WebFetch'],
  'devops': ['Bash', 'Read', 'Write', 'Edit', 'WebSearch', 'WebFetch'],
}

// ── 技能描述 ──────────────────────────────────────────────

const DEFAULT_SKILLS: Record<string, string[]> = {
  'frontend-engineer': ['React/Vue 开发', 'CSS/Tailwind 样式', '前端测试 (Jest/Vitest)'],
  'backend-engineer': ['Python/FastAPI 开发', 'SQL 数据库操作', 'API 设计与文档'],
  'tester': ['测试用例设计', '自动化测试 (pytest)', '缺陷跟踪'],
  'reviewer': ['代码审查', '安全检查', '架构评估'],
  'devops': ['Docker 容器化', 'CI/CD 配置', '部署脚本'],
}

// ── 生成函数 ──────────────────────────────────────────────

export function generateClaudeMd(
  name: string,
  role: string,
  extraSkills?: string[],
  customSystemPrompt?: string,
): string {
  // 自定义 system_prompt 优先
  if (customSystemPrompt?.trim()) {
    return `# ${name} 的角色定义

## 角色

${customSystemPrompt}

## 技能

${buildSkillsSection(role, extraSkills)}

## 约束

${DEFAULT_CONSTRAINTS}

## 环境与交接

- 工作目录为当前群组共享目录
- 产出物 -> shared/
- 最终交付 -> output/
- 完成后通知群主
`
  }

  const roleDesc = ROLE_DESCRIPTIONS[role] || `${role} — 自定义角色`
  const responsibilities = ROLE_RESPONSIBILITIES[role] || '- 根据需求完成分配的工作'
  const skillsSection = buildSkillsSection(role, extraSkills)

  return CLAUDE_MD_TEMPLATE
    .replace('{name}', name)
    .replace('{role_description}', roleDesc)
    .replace('{responsibilities}', responsibilities)
    .replace('{skills_section}', skillsSection)
    .replace('{constraints}', DEFAULT_CONSTRAINTS)
}

export function generateSettingsJson(
  name: string,
  role: string,
  extraAllowedTools?: string[],
  extraDeniedTools?: string[],
): string {
  const defaultTools = ROLE_DEFAULT_TOOLS[role] || [...ALL_TOOLS]
  let allowed = [...new Set([...defaultTools, ...(extraAllowedTools || [])])]
  const denied = [...new Set(extraDeniedTools || [])]

  // 从 allowed 中移除 denied
  allowed = allowed.filter(t => !denied.includes(t))

  return JSON.stringify({
    $schema: 'https://code.visualstudio.org/schema/settings',
    permissions: {
      allowed_tools: allowed,
      denied_tools: denied,
    },
    agent: {
      name,
      role,
    },
  }, null, 2)
}

function buildSkillsSection(role: string, extraSkills?: string[]): string {
  const lines: string[] = []

  for (const skill of DEFAULT_SKILLS[role] || []) {
    lines.push(`- ${skill}（内置）`)
  }

  for (const skill of extraSkills || []) {
    lines.push(`- ${skill}（技能市场挂载）`)
  }

  return lines.length ? lines.join('\n') : '- 通用开发技能'
}

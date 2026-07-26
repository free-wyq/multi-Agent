/**
 * 需求2-前端：气泡外追问引导 chip 生成（纯前端规则，零后端调用）。
 *
 * 设计单真源：``docs/structured-result-card-schema.md`` mockup 第 4 层
 * 「💡 您可能还想问: [chip1] [chip2] [chip3]」——按回复内容生成 2-3 个 follow-up
 * 问题，渲染成可点 ``Tag``（点即填入输入框，不自动发送，用户可改后发）。
 *
 * 策略选择（自决，铁律 #2/#5）：任务给出「先纯前端规则或调 LLM 生成」二选一，
 * 选纯前端规则——零后端端点 / 零 LLM 调用成本 / 零延迟 / 离线可用。LLM 生成虽更
 * 上下文相关但每条回复都要等一次 LLM 往返（百毫秒~秒级），对「轻量追问引导」过重；
 * 规则生成即时、确定、可解释。后续如需更智能可加 LLM 开关（v2），v1 先规则落地。
 *
 * 生成逻辑（分层降级，保 2-3 条）：
 *  1. 卡片感知：扫 ```` ```card ```` 块的 title/kind——table 建议榜首详情 / kv 逐项深挖 /
 *     list 逐条展开；首个卡片标题造「X 的详情？」。
 *  2. 关键词感知：散文含「步骤/方法/流程」→「具体怎么做？」；含「对比/区别」→「举个例子？」；
 *     含「注意/风险/坑」→「还有什么要注意的？」；含「原因/原理」→「详细解释下原因？」。
 *  3. 通用兜底：「详细说说？」「举个例子？」「还有什么需要注意的？」。
 *  去重 + 截断到 max（默认 3）；空内容返 []。
 *
 * 模块独立（不 import ChatMessageBubble 的 parseCards/CARD_RE）——lib 不应依赖组件；
 * 卡片 title/kind 扫描用独立轻量正则（关注点是「找标题造追问」非「精确解析渲染」，
 * 与 ChatMessageBubble 的 byte-identical 解析正则属不同关注点，容许各自定义）。
 */

/** ```card 围栏块扫描（与 ChatMessageBubble CARD_RE 同模式，但本模块独立定义——
 *  关注点是「找卡片块取标题/类型造追问」，非「精确切段渲染」）。 */
const CARD_BLOCK_RE = /```card\s*\n([\s\S]*?)```/g

/** 从 card 块 JSON 里取 title（容错——只取首个 title 字段，非法 JSON 不命中自然跳过）。 */
const TITLE_RE = /"title"\s*:\s*"([^"]+)"/
/** 从 card 块 JSON 里取 kind。 */
const KIND_RE = /"kind"\s*:\s*"([^"]+)"/

export interface FollowUpContext {
  /** 卡片标题列表（按出现顺序），用于生成「X 详情？」类追问。 */
  cardTitles: string[]
  /** 卡片 kind 列表，用于按类型造追问（table→榜首详情，list→逐项展开，kv→逐项深挖）。 */
  cardKinds: string[]
  /** 去除 card 块后的纯散文正文（用于关键词感知）。 */
  prose: string
}

/** 扫描 content 提取 follow-up 生成所需的上下文（卡片标题/kind + 散文）。
 *  matchAll 自管 lastIndex，但 CARD_BLOCK_RE 带 g flag 是模块级共享单例——matchAll 内部
 *  用副本迭代不会污染外部，仍显式 reset 防被前次 exec 残留。 */
function scanContent(content: string): FollowUpContext {
  const cardTitles: string[] = []
  const cardKinds: string[] = []
  let prose = content
  CARD_BLOCK_RE.lastIndex = 0
  for (const m of content.matchAll(CARD_BLOCK_RE)) {
    const block = m[1] ?? ''
    const titleMatch = block.match(TITLE_RE)
    if (titleMatch && titleMatch[1]) cardTitles.push(titleMatch[1])
    const kindMatch = block.match(KIND_RE)
    if (kindMatch && kindMatch[1]) cardKinds.push(kindMatch[1])
    prose = prose.replace(m[0], ' ')
  }
  return { cardTitles, cardKinds, prose: prose.trim() }
}

/** 生成 2-3 个 follow-up 追问（纯前端规则）。空内容返 []。
 *  分层降级：卡片感知 → 关键词感知 → 通用兜底；去重 + 截断到 max。 */
export function generateFollowUps(content: string, max = 3): string[] {
  if (!content || !content.trim()) return []
  const ctx = scanContent(content)
  const out: string[] = []
  const seen = new Set<string>()
  const push = (s: string) => {
    const t = s.trim()
    if (!t || seen.has(t)) return
    seen.add(t)
    out.push(t)
  }

  // 1. 卡片感知追问
  if (ctx.cardTitles.length > 0) {
    push(`${ctx.cardTitles[0]} 的详情？`)
  }
  if (ctx.cardKinds.includes('table')) {
    push('第一名具体是什么？')
  }
  if (ctx.cardKinds.includes('list')) {
    push('逐条展开说说？')
  }
  if (ctx.cardKinds.includes('kv')) {
    push('各项分别详细说明？')
  }

  // 2. 关键词感知追问（基于去卡片段后的散文）
  const prose = ctx.prose
  if (/步骤|方法|流程|怎么/.test(prose)) push('具体怎么做？')
  if (/对比|区别|比较|优劣/.test(prose)) push('举个例子？')
  if (/注意|风险|限制|坑/.test(prose)) push('还有什么要注意的？')
  if (/原因|为什么|原理/.test(prose)) push('详细解释下原因？')

  // 3. 通用兜底（保底填充——即便内容简短也至少有追问可点）
  push('详细说说？')
  push('举个例子？')
  push('还有什么需要注意的？')

  return out.slice(0, max)
}

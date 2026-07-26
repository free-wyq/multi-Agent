/**
 * PersistentBubbleList — 持久化消息气泡列表（[任务9a] 从 ChatPanel.tsx 抽出）。
 *
 * 职责单一：把 `chatMessages`（已落库的 Message[]）渲染成聊天气泡流——日期分组分隔条
 * + slash 命令卡片 + 用户气泡 + AI 气泡（复用 ChatMessageBubble）。纯展示组件，
 * 数据全由父组件 ChatPanel 注入（chatMessages/agents/members/回调），不自管消息状态、
 * 不订阅 context——与 ChatMessageBubble「纯展示」设计约定一致。
 *
 * 抽出动机：ChatPanel.tsx 1675 行职责过多（消息流渲染 + 输入框 + 计划卡 + 流式/定稿气泡
 * + @mention + slash 补全 + 文件上传）。本模块吃掉最大的一块——持久化气泡 flatMap（~175 行）
 * + 其专用纯函数（颜色/耗时/日期/trace 解析等 12 个 helper），让 ChatPanel 聚焦交互编排。
 * 流式/定稿气泡（streamingBubbles/finalizedBubbles）留 ChatPanel（[任务9b] 再抽
 * StreamingBubbleList + useBubbleRetire）。
 *
 * 行为零变：所有渲染逻辑、注释、样式 class 逐字从 ChatPanel 搬来，仅把「闭包捕获的
 * ChatPanel state/handler」改成 props 传入（chatMessages/agents/members/ttsEnabled/
 * regeneratingReplyIds/onRegenerate/onFollowUpClick/lastDateRef）。lastDateRef 仍由
 * ChatPanel 持有 + 切群 effect 重置（行为真源不变），作为 RefObject 透传——本组件在
 * render 期读/写 .current（与原 flatMap 同款，render-phase cursor ref，非 state）。
 */
import type { ReactNode } from 'react'
import { Button, Tag, Tooltip } from 'antd'
import { ReloadOutlined, UserOutlined } from '@ant-design/icons'
import {
  parseStats,
  safeRecord,
  type AgentDefinition,
  type FinalizedStats,
  type GroupMember,
  type Message,
  type TraceEvent,
} from '../services/api'
import { renderMarkdownWithMentions } from '../lib/renderMarkdown'
import { generateFollowUps } from '../lib/followUpSuggestions'
import ChatMessageBubble, { type ArtifactFile } from './ChatMessageBubble'
import BubbleSpeakButton from './BubbleSpeakButton'
import BubbleCopyButton from './BubbleCopyButton'

/** 获取智能体角色主题色。
 *  B19：role 字段在全仓命名不一致——后端 agent_templates.py / store/seed.py 用 snake_case
 *  （backend_engineer / frontend_engineer / qa_engineer / devops_engineer /
 *  product_manager / fullstack_engineer），前端 AgentPage ROLES / Sidebar 表单用中文
 *  （后端开发工程师 / 前端开发工程师 / 测试工程师 / DevOps 工程师 / 产品经理 / 自定义）。
 *  原 ROLE_COLORS 按中文键硬编码 → 模板雇佣的 agent（role=snake_case）查不到色落默认，
 *  与表单创建的 agent（role=中文）显色不一致。改按 snake_case 匹配为主键，LEGACY_ROLE_ALIASES
 *  兼容旧中文名——中文 role 经别名归一化到 snake_case 再查色（单色源，不复制色值）。
 *
 *  行为零变：5 个有显式色的角色 hex 逐字保留（backend #6366f1 / frontend #06b6d4 /
 *  qa #f59e0b / devops #10b981 / product #f43f5e）；fullstack_engineer / 自定义 / 未知
 *  role 原未在 ROLE_COLORS 显式键（落 ?? '#8b5cf6' 默认），现仍不显式键 → 落同默认。
 *  coordinator 由 ChatAvatar 预过滤（id==='coordinator' 直接 #722ed1），不进 getAgentColor。 */
export function getAgentColor(id: string, agents: AgentDefinition[]): string {
  // 主键 snake_case（后端 agent_templates.py role 规范 + store/seed.py 落盘值）。
  const ROLE_COLORS: Record<string, string> = {
    backend_engineer: '#6366f1',
    frontend_engineer: '#06b6d4',
    qa_engineer: '#f59e0b',
    devops_engineer: '#10b981',
    product_manager: '#f43f5e',
  }
  // 旧中文名兼容（前端 AgentPage ROLES / Sidebar 表单创建的 agent role 仍是中文）。
  // 归一到对应 snake_case 主键再查色——单色源（不复制色值，中文仅作别名）。
  const LEGACY_ROLE_ALIASES: Record<string, string> = {
    '后端开发工程师': 'backend_engineer',
    '前端开发工程师': 'frontend_engineer',
    '测试工程师': 'qa_engineer',
    'DevOps 工程师': 'devops_engineer',
    '产品经理': 'product_manager',
  }
  const agent = agents.find((a) => a.id === id)
  if (!agent) return '#722ed1'
  const key = LEGACY_ROLE_ALIASES[agent.role] ?? agent.role
  return ROLE_COLORS[key] ?? '#8b5cf6'
}

/** 毫秒 → 人类可读耗时：<1s 显示 ms，否则保留 1 位小数秒（与 ChatMessageBubble 一致）。 */
export function formatElapsed(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

/** 取 ISO 时间戳的「年-月-日」本地日期 key（用于判断两条消息是否同一天）。
 *  B21：返回值仅用于 ``dateKey(prevIso) === dateKey(iso)`` 相等比较，从不展示——故
 *  0-index/1-index 对比较结果本无影响。但 dateLabel 同样取本地月日且 ``getMonth()+1``
 *  展示，两函数共用「本地年月日」口径——dateKey 显式 ``getMonth()+1`` 与 dateLabel 对齐
 *  （隐式耦合改显式：两处都 +1，一处改另一处忘改则肉眼可见不一致）。
 *  不用 ``toISOString().slice(0,10)``：那是 UTC 日期，会与 dateLabel 的本地「今天/昨天」
 *  判定在非 UTC 时区跨日边界处脱钩（本地同日但 UTC 跨日 / 反之），致分隔条漏渲染或误渲染。 */
function dateKey(iso: string): string {
  const d = new Date(iso)
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`
}

/** 日期分隔条标签：今天/昨天/更早完整日期。与微信/钉钉同款口语化。
 *  B21：与 dateKey 共用「本地年月日」口径——``getMonth()+1`` 1-indexed 月展示，
 *  ``getDate()`` 日，``getFullYear()`` 年。午夜锚点 ``new Date(y, m0, d)`` 用 0-indexed
 *  getMonth()（Date 构造器要求 0-indexed 月）算日差，与展示口径分开（构造器口径非展示口径）。 */
function dateLabel(iso: string): string {
  const d = new Date(iso)
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const that = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const diffDays = Math.round((today.getTime() - that.getTime()) / 86400000)
  if (diffDays === 0) return '今天'
  if (diffDays === 1) return '昨天'
  // 同年省年份，跨年带年份
  return d.getFullYear() === now.getFullYear()
    ? `${d.getMonth() + 1}月${d.getDate()}日`
    : `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`
}

/**
 * 日期分组分隔条：本条与上一条不在同一天时返回分隔条节点，否则返回 null。
 * 调用方在 flatMap 里把返回值（可能 null）与消息一起铺平；null 会被 React 忽略。
 *
 * 用法（在 chatMessages.flatMap 回调里）：
 *   const dateDivider = renderDateDivider(msg.created_at, lastDateRef.current)
 *   if (dateDivider) lastDateRef.current = msg.created_at
 *   return [dateDivider, <MsgBubble .../>]
 *
 * lastDateRef 跨渲染保持上一条日期——切群时由切群 effect 重置为 null，
 * 避免新群首条消息被误判与旧群末条同天而漏渲染分隔条。
 */
function renderDateDivider(iso: string, prevIso: string | null): React.ReactNode {
  if (prevIso !== null && dateKey(prevIso) === dateKey(iso)) return null
  return (
    <div key={`date-${iso}`} className="chat-date-divider">
      <span className="chat-date-label">{dateLabel(iso)}</span>
    </div>
  )
}

/** 从持久化 agent_reply 的 data 字段提取协调者流式统计。
 *  node_chat 经 _unified_reply 把 {reply_id, elapsed_ms, tokens, model, reasoning_tokens}
 *  落盘到 message.data，定稿气泡据此渲染「model · Ns · ↓ N tokens（含 N 推理）· 完成」状态行
 *  ——流式期间的统计在完成后保留可见，不随流式气泡退场消失。
 *  reasoning_tokens > 0 时追加「（含 N 推理）」——否则 5 字回复显示 148 tokens 显得假，
 *  其实 133 个是模型内部推理（用户看不见），点明后数字才可解释。
 *  非协调者 chat 回复（dispatch/summarize announce、user_input、task_log、slash_card）
 *  data 无 elapsed_ms → null，不渲染状态行。
 *
 *  B18：Number()/Number.isFinite 守卫抽到 services/api.ts parseStats（与 useBusEvent
 *  coordinator_stats 分支共享单一真源，原两处重复守卫去重）。定稿气泡走 strictElapsed=true
 *  守卫（elapsed_ms 非有限/<=0 返 null——announce 类回复无 elapsed_ms 不渲染假状态行，
 *  A8/vg2 契约）+ withPhase=false（持久化 data 无 phase，返 FinalizedStats 子集）。 */
function extractCoordStats(data: Record<string, unknown> | null): FinalizedStats | null {
  return parseStats(data, { withPhase: false, strictElapsed: true }) as FinalizedStats | null
}

/** 取持久化协调者回复的推理文本（agent_reply.data.reasoning，推理模型落盘的 reasoning_content 全文）。
 *  定稿气泡的折叠区据此展开——流式期靠 coordReasoning 实时累加，phase=done 清空后只能靠落盘文本。
 *
 *  B20：null-guard 走 services/api.ts safeRecord 单一真源（原 ``if (!data) return undefined``，
 *  与 extractCoordStats/extractFinalizedArtifacts 三处重复守卫去重）。safeRecord 把
 *  ``unknown`` data 归一为 ``Record<string, unknown> | null``——非 object/null/undefined
 *  返 null，调用方 ``if (!dd) return undefined`` 兜底。reasoning 字段仍本处守卫
 *  （typeof string && 非空——reasoning 口径独立于 stats，不复用 parseStats）。 */
function extractCoordReasoning(data: Record<string, unknown> | null): string | undefined {
  // B20：data 已是 Record|null（调用方传 msg.data: Record<string,unknown>|null），但 safeRecord
  // 统一兜底——若上层传入 unknown（未来重构成 TraceEvent.data 透传）也安全。复用单一守卫。
  const dd = safeRecord(data)
  if (!dd) return undefined
  const r = dd['reasoning']
  return typeof r === 'string' && r ? r : undefined
}

/** [需求2-后端] 取持久化回复的 reply_id（agent_reply.data.reply_id）。
 *  chat 路径（worker node_brain / coordinator node_chat）落盘时由 persist_agent_reply
 *  透传 reply_id 到 message.data——这是 regenerate 端点的回查键。execute 路径模板 announce
 *  + user_input 行无 reply_id → 返 undefined（操作栏「重新生成」按钮 disabled 兜底）。
 *  B20：null-guard 复用 services/api.ts safeRecord 单一真源。 */
function extractReplyId(data: Record<string, unknown> | null): string | undefined {
  const dd = safeRecord(data)
  if (!dd) return undefined
  const rid = dd['reply_id']
  return typeof rid === 'string' && rid ? rid : undefined
}

/** 回放 trace：从持久化 agent_reply.data.trace 提取 tool/think 事件，映射成前端 TraceEvent[]
 *  形状（对齐 services/api.ts TraceEvent，复用 ChatMessageBubble 现有折叠区渲染——不另造数据形状）。
 *
 *  后端 registry.on_log 把 tool_start/tool_end/think/answer 累加到 _turn_trace[turn_reply_id]，
 *  reply 时塞进 reply_data["trace"] 落到 message.data.trace。本函数把后端落的精简数组
 *  （每元素 {kind, content, timestamp, phase?, name?, args?, output?}）映射成前端 TraceEvent
 *  （{id, kind, agentId, agentName, taskId, content, data, timestamp}）：
 *   - kind: 后端 'tool_start'/'tool_end' → 前端 'tool'（ChatMessageBubble.toolEvents 按 kind==='tool' 过滤）；
 *     后端 'think'/'answer' → 前端 'think'（ChatMessageBubble.thinkEvents 按 kind==='think' 过滤）。
 *   - data: {phase, name, args, output} 透传——ChatMessageBubble.toolRows 按 data.phase==='end'
 *     算耗时 + data.name 取工具名 + data.args/data.output 取 payload（与流式期同款逻辑）。
 *   - timestamp: 后端 ISO 字符串 → 前端 number（Date.parse）——与流式期 TraceEvent.timestamp 同单位。
 *   - id/agentId/agentName/taskId: 不在后端 trace 里（无意义）——填占位让类型兼容（ChatMessageBubble
 *     不依赖这些字段渲染折叠区，只用 data + content + timestamp）。
 *
 *  返回 {toolEvents, thinkEvents}：tool 类→toolEvents，think/answer 类→thinkEvents。
 *  两者皆空数组 → 持久化气泡走 ChatMessageBubble hasTools=false/hasThinks=false 不渲染折叠区
 *  （chat 路径 trace 天然为空，行为零变）。 */
function extractTraceEvents(
  data: Record<string, unknown> | null,
  senderId: string,
): { toolEvents: TraceEvent[]; thinkEvents: TraceEvent[] } {
  const dd = safeRecord(data)
  if (!dd) return { toolEvents: [], thinkEvents: [] }
  const rawTrace = dd['trace']
  if (!Array.isArray(rawTrace)) return { toolEvents: [], thinkEvents: [] }
  const toolEvents: TraceEvent[] = []
  const thinkEvents: TraceEvent[] = []
  for (let i = 0; i < rawTrace.length; i++) {
    const step = safeRecord(rawTrace[i])
    if (!step) continue
    const kind = typeof step['kind'] === 'string' ? step['kind'] : ''
    const content = typeof step['content'] === 'string' ? step['content'] : ''
    const tsStr = typeof step['timestamp'] === 'string' ? step['timestamp'] : ''
    const ts = tsStr ? Date.parse(tsStr) : 0
    const dataPayload: Record<string, unknown> = {}
    const phase = step['phase']
    if (typeof phase === 'string') dataPayload['phase'] = phase
    const name = step['name']
    if (typeof name === 'string') dataPayload['name'] = name
    if ('args' in step) dataPayload['args'] = step['args']
    if ('output' in step) dataPayload['output'] = step['output']
    const ev: TraceEvent = {
      id: `trace-${i}-${ts}`,
      kind: kind === 'think' || kind === 'answer' ? 'think' : 'tool',
      agentId: senderId,
      agentName: '',
      taskId: null,
      content,
      data: dataPayload,
      timestamp: Number.isFinite(ts) ? ts : 0,
    }
    if (kind === 'tool_start' || kind === 'tool_end') {
      toolEvents.push(ev)
    } else if (kind === 'think' || kind === 'answer') {
      thinkEvents.push(ev)
    }
  }
  return { toolEvents, thinkEvents }
}

/** ST-06（task 21）：从 task_complete 事件 data 提取产物文件列表（data.artifact.files[]）。
 *
 * 后端 bus.py emit_task_completed 仅成功路径把 scan_workspace_artifacts manifest 写入
 * data.artifact（`{"files":[{name,path,size,modified_at},...]}`），失败/取消/超时路径 artifact
 * key 缺省（key omission，非 null）→ 本函数返空数组。失败气泡因此自然无下载卡——语义正确，
 * 失败任务不留可用产物。
 *
 * data 形状：TraceEvent.data（unknown，bus 事件透传）。容错解析——非对象/非数组/files 空
 * 全返 []，不抛错（WS 事件结构偶发异常不应炸渲染）。返回元素字段做最小类型守卫（name/path
 * 字符串化），与 ChatMessageBubble.ArtifactFile 形状对齐。
 *
 * B20：三层 null-guard（data / artifact / file 条目）都走 services/api.ts safeRecord 单一
 * 真源——原 ``if (!data || typeof data !== 'object')`` + ``if (!artifact || typeof artifact
 * !== 'object')`` + ``if (!raw || typeof raw !== 'object')`` 三处重复守卫去重。safeRecord
 * 额外排除数组（数组非 record），artifact manifest 是 dict 非数组，行为零变。
 *
 * export 给 ChatPanel：finalizedBubbles memo（定稿过渡气泡，[任务9b] 抽 StreamingBubbleList
 * 时再随迁）仍留在 ChatPanel，它按 task_complete 事件 data 提取产物——故本 helper 由
 * PersistentBubbleList 持有 + export，ChatPanel import 复用（单一真源，不复制）。 */
export function extractFinalizedArtifacts(data: unknown): ArtifactFile[] {
  const dd = safeRecord(data)
  if (!dd) return []
  const manifest = safeRecord(dd['artifact'])
  if (!manifest) return []
  const files = manifest['files']
  if (!Array.isArray(files)) return []
  return files
    .map((raw) => {
      const f = safeRecord(raw)
      if (!f) return null
      const name = typeof f['name'] === 'string' ? (f['name'] as string) : ''
      const path = typeof f['path'] === 'string' ? (f['path'] as string) : ''
      if (!name && !path) return null
      return {
        name: name || path,
        path: path || name,
        size: typeof f['size'] === 'number' ? (f['size'] as number) : 0,
        modified_at: typeof f['modified_at'] === 'string' ? (f['modified_at'] as string) : '',
      } as ArtifactFile
    })
    .filter((x): x is ArtifactFile => x !== null)
}

/** 把 sender_id 解析成显示名（纯 string，供 ChatMessageBubble.senderName 用——该 prop
 *  要求 string，不能直接传 React 元素）。与 SenderName 组件共用同一逻辑，单一真源。 */
function resolveSenderName(id: string, agents: AgentDefinition[]): string {
  if (id === 'user') return '用户'
  if (id === 'coordinator') return '群主(协调者)'
  if (id === 'broadcast') return '系统广播'
  if (id === 'system') return '系统'
  const agent = agents.find((a) => a.id === id)
  return agent?.name ?? id.slice(0, 8) + '...'
}

/** 获取成员显示名（@mention 候选名解析）。export 给 ChatPanel：@mention 自动补全 +
 *  候选列表（mentionCandidates）复用本 helper（留在 ChatPanel，属输入交互非气泡渲染，
 *  [任务9b] 不随迁）——单一真源，不复制。 */
export function getMemberDisplayName(member: GroupMember) {
  return member.alias || member.agent_name
}

/** 聊天气泡头像（从 GroupPage 抽出，逻辑/视觉不变） */
export function ChatAvatar({ id, agents }: { id: string; agents: AgentDefinition[] }) {
  if (id === 'user') {
    return (
      <div className="chat-avatar chat-avatar--user">
        <UserOutlined style={{ fontSize: 16, color: '#F26522' }} />
      </div>
    )
  }
  const color = id === 'coordinator' || id === 'broadcast' || id === 'system' ? '#722ed1' : getAgentColor(id, agents)
  return (
    <div className="chat-avatar" style={{ borderColor: color }}>
      <img
        src="/robot-avatar.png"
        alt=""
        className="chat-avatar-img"
      />
      <span
        className="chat-avatar-ring"
        style={{ borderColor: color }}
      />
    </div>
  )
}

/** 获取发送者显示名 */
function SenderName({ id, agents }: { id: string; agents: AgentDefinition[] }) {
  return resolveSenderName(id, agents)
}

export interface PersistentBubbleListProps {
  /** 已落库的持久化消息列表（ChatPanel chatMessages state）。 */
  chatMessages: Message[]
  /** 全部智能体（头像角色色 + 发送者名解析）。 */
  agents: AgentDefinition[]
  /** 当前群成员（@mention 高亮用 memberNames 投影）。 */
  members: GroupMember[]
  /** TTS 是否启用（启用时 AI 气泡 hover 操作组显朗读按钮）。 */
  ttsEnabled: boolean
  /** regenerate 正在重跑的 reply_id 集合（按钮 loading 态真源）。 */
  regeneratingReplyIds: Set<string>
  /** 点「重新生成」回调（reply_id → 后端 regenerate 端点）。 */
  onRegenerate: (replyId: string) => void
  /** 点追问引导 chip 回调（chip 文本填入输入框）。 */
  onFollowUpClick: (text: string) => void
  /** 日期分组游标 ref（跨渲染保持上一条日期，切群时由 ChatPanel 切群 effect 重置为 null）。
   *  本组件在 render 期读/写 .current——render-phase cursor ref（非 state，避免每条消息触发
   *  重渲染），与原 flatMap 同款。透传而非自管：重置真源在 ChatPanel 切群 effect（与
   *  stickToBottom/autoPlayReady/spokenIds 等同批重置），自管会拆散重置时序。 */
  lastDateRef: React.RefObject<string | null>
}

/**
 * 渲染持久化消息气泡流。原 ChatPanel `chatMessages.flatMap(...)` 块逐字搬来——
 * 日期分组 + slash 卡片 + 用户气泡 + AI 气泡（ChatMessageBubble），行为零变。
 */
export default function PersistentBubbleList({
  chatMessages,
  agents,
  members,
  ttsEnabled,
  regeneratingReplyIds,
  onRegenerate,
  onFollowUpClick,
  lastDateRef,
}: PersistentBubbleListProps) {
  return (
    <>
      {chatMessages.flatMap((msg) => {
        const isUser = msg.sender_id === 'user'
        // 日期分组：当本条与上一条不在同一天时，插一条日期分隔条。
        // today/yesterday 用口语化标签，更早用完整日期；分隔条 sticky 顶部，
        // 滚动时当前可见天的标签常驻顶，便于定位「这是哪天的对话」（微信/钉钉同款）。
        const dateDivider = renderDateDivider(msg.created_at, lastDateRef.current)
        if (dateDivider) lastDateRef.current = msg.created_at
        // SC-11：slash 命令卡片（type=slash_card）——handler 经 ctx.renderCard 推入，
        // content 存字符串（stub 占位），data.node 存富卡片 ReactNode（SC-03~10 实现）。
        // 渲染为系统消息（左对齐，头像 + 卡片 + 时间戳），不走 ChatMessageBubble 的
        // @mention 高亮（slash 卡片已是富 antd Card 节点，无需再 markdown/mention 处理）。
        //
        // 关键：node 是 antd Card（ModelCard/ToolsCard 等，自带白底+边框+圆角+标题），
        // 不再套 .chat-bubble 气泡层——否则灰底气泡 + padding + max-width:70% 会把卡片
        // 挤变形（双层背景/双层圆角/padding 双挤/字段被压窄）。卡片直接裸露渲染，仅靠
        // chat-bubble-wrap 对齐头像与时间戳，宽度对系统卡片放宽到 90%（信息密集需舒展）。
        if (msg.type === 'slash_card') {
          return [
            dateDivider,
            <div key={msg.id} className="chat-msg" style={{ flexDirection: 'row' }}>
              <ChatAvatar id="system" agents={agents} />
              <div className="chat-bubble-wrap" style={{ flex: 1, minWidth: 0, maxWidth: 760 }}>
                <div className="chat-sender-name">
                  <SenderName id="system" agents={agents} />
                </div>
                {msg.data?.node as ReactNode ?? msg.content}
                <div className="chat-timestamp">
                  {new Date(msg.created_at).toLocaleTimeString()}
                </div>
              </div>
            </div>,
          ]
        }
        // 持久化气泡渲染分两路，避免双头像回归：
        //  · 用户气泡（isUser）：手写 .chat-msg + ChatAvatar + .chat-bubble-wrap 结构
        //    （用户消息纯文本，不走 ChatMessageBubble，保持纯文本语义 + 不渲折叠区）。
        //  · AI 气泡（非用户）：直接渲染 ChatMessageBubble——它自带 .chat-msg + avatar
        //    + .chat-bubble-wrap 结构。不能再外包一层 .chat-msg + ChatAvatar，否则双头像
        //    + 双层 wrap 嵌套（persistent bubble 复用 ChatMessageBubble 后引入的回归：
        //    外层 .chat-msg 的 ChatAvatar 与内层 ChatMessageBubble 的 avatar prop 各渲
        //    一个头像，渲染出两个机器人头像）。streaming/finalized/coordinator 气泡路径
        //    均直接渲染 ChatMessageBubble 无外层包裹，此处对齐。
        if (isUser) {
          return [
            dateDivider,
            <div
              key={msg.id}
              className="chat-msg"
              style={{ flexDirection: 'row-reverse' }}
            >
              <ChatAvatar id={msg.sender_id} agents={agents} />
              <div className="chat-bubble-wrap chat-bubble-wrap--self">
                {/* hover 操作组：复制按钮（用户消息也支持复制，与 AI 气泡对齐）。
                    .chat-bubble-wrap position:relative 提供 hover 显隐锚点。 */}
                <div className="bubble-action-group">
                  <BubbleCopyButton content={msg.content ?? ''} />
                </div>
                <div className="chat-sender-name chat-sender-name--right">
                  <SenderName id={msg.sender_id} agents={agents} />
                </div>
                <div className="chat-bubble chat-bubble--self">
                  {msg.content}
                </div>
                <div className="chat-timestamp chat-timestamp--right">
                  {new Date(msg.created_at).toLocaleTimeString()}
                </div>
              </div>
            </div>,
          ]
        }
        // 非用户气泡：复用 ChatMessageBubble（与流式期同一组件，受控展开逻辑统一）。
        //   - reasoning：extractCoordReasoning(msg.data) 落盘的推理全文
        //   - reasoningTokens：extractCoordStats().reasoning_tokens 落盘真值
        //   - toolEvents/thinkEvents：extractTraceEvents(msg.data) 把 data.trace 解析成
        //     TraceEvent[]（后端 registry.on_log 落的 tool_start/tool_end/think/answer）
        //   - statusLine：复用 extractCoordStats 状态行渲染（model · Ns · ↓ N tokens）
        //   - finalStats：把 stats 透传给「🔍 执行过程详情」面板的「📝 最终生成」item，
        //     只放元数据（字数 + tokens + 耗时 + model），不重复渲染 content 正文。
        //   - footerExtra：追问引导 chip（chat-bubble 之外、chat-bubble-wrap 之内，与 mockup 一致——
        //     ChatMessageBubble 把 footerExtra 渲在 Bubble 闭合之后，气泡外 wrap 内）
        // hideFooterAction=true：抑制 footer 内置「复制+重新生成」操作栏，统一走顶部
        //   hover actionGroup（含复制/朗读/重新生成），避免双份操作栏。ChatMessageBubble
        //   footer 仍渲产物卡（持久化气泡当前无 artifact，footer 不显）。
        // mention 高亮：持久化气泡走 markdown 渲染，mention 用 renderMarkdownWithMentions
        //   注入（renderContent 闭包把 members 投影成 memberNames Set 传进去）。
        const reasoning = extractCoordReasoning(msg.data) ?? undefined
        const stats = extractCoordStats(msg.data)
        const reasoningTokens = stats?.reasoning_tokens
        const replyId = extractReplyId(msg.data)
        const { toolEvents, thinkEvents } = extractTraceEvents(msg.data, msg.sender_id)
        const memberNames = new Set<string>()
        for (const m of members) {
          if (m.agent_name) memberNames.add(m.agent_name)
          if (m.alias) memberNames.add(m.alias)
        }
        const renderContent = (c: string) => renderMarkdownWithMentions(c, memberNames)
        const followUps = msg.content ? generateFollowUps(msg.content) : []
        const actionGroup = (
          <div className="bubble-action-group">
            <BubbleCopyButton content={msg.content ?? ''} />
            {ttsEnabled && <BubbleSpeakButton content={msg.content ?? ''} />}
            {replyId && (
              <Tooltip title={regeneratingReplyIds.has(replyId) ? '正在重新生成…' : '重新生成'}>
                <Button
                  type="text"
                  size="small"
                  className="bubble-action-btn"
                  icon={<ReloadOutlined />}
                  loading={regeneratingReplyIds.has(replyId)}
                  disabled={regeneratingReplyIds.has(replyId)}
                  onClick={() => onRegenerate(replyId)}
                />
              </Tooltip>
            )}
          </div>
        )
        const statusLine = stats ? (
          <div className="chat-status-line">
            {stats.model && <span className="chat-status-model">{stats.model}</span>}
            {stats.model && ' · '}
            {`${formatElapsed(stats.elapsed_ms)} · ↓ ${stats.tokens} tokens`}
            {stats.reasoning_tokens && (
              <span className="chat-status-reasoning">
                {' '}（含 {stats.reasoning_tokens} 推理）
              </span>
            )}
            {' · 完成'}
          </div>
        ) : undefined
        // finalStats 透传给「📝 最终生成」Timeline item：stats 有值时把 elapsedMs/tokens/
        // reasoningTokens/model 全传；无 stats 时也传一个最小对象（仅字数会从 content 自算），
        // 让持久化气泡的「📝 最终生成」item 始终渲染（完成态元数据可见）。
        const finalStats = stats
          ? {
              elapsedMs: stats.elapsed_ms,
              tokens: stats.tokens,
              reasoningTokens: stats.reasoning_tokens,
              model: stats.model,
            }
          : {}
        // 追问引导 chip 在 chat-bubble 之外、chat-bubble-wrap 之内（ChatMessageBubble
        //   footerExtra 渲在 Bubble 闭合之后，气泡外 wrap 内，与 mockup「气泡外 chip」一致）。
        const footerExtra = followUps.length > 0 ? (
          <div className="chat-followup-chips">
            <span className="chat-followup-label">💡 您可能还想问：</span>
            {followUps.map((q) => (
              <Tag key={q} className="chat-followup-chip" onClick={() => onFollowUpClick(q)}>
                {q}
              </Tag>
            ))}
          </div>
        ) : undefined
        return [
          dateDivider,
          <ChatMessageBubble
            key={msg.id}
            senderId={msg.sender_id}
            senderName={resolveSenderName(msg.sender_id, agents)}
            avatar={<ChatAvatar id={msg.sender_id} agents={agents} />}
            content={msg.content ?? ''}
            renderContent={renderContent}
            reasoning={reasoning}
            reasoningTokens={reasoningTokens}
            toolEvents={toolEvents}
            thinkEvents={thinkEvents}
            timestamp={msg.created_at}
            actionGroup={actionGroup}
            statusLine={statusLine}
            finalStats={finalStats}
            hideFooterAction
            footerExtra={footerExtra}
          />,
        ]
      })}
    </>
  )
}

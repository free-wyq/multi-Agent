import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Button, Collapse, Empty, Input, Spin, Tag, Tooltip, Typography, message } from 'antd'
import type { ComponentRef } from 'react'
import { BulbOutlined, RobotOutlined, SendOutlined, SettingOutlined, UserOutlined, VerticalAlignBottomOutlined } from '@ant-design/icons'
import {
  messageApi,
  taskApi,
  parseStats,
  safeRecord,
  type AgentDefinition,
  type FinalizedStats,
  type Group,
  type GroupMember,
  type Message,
  type TraceEvent,
} from '../services/api'
import { useBusEventContext } from '../contexts/BusEventContext'
import { useSettings } from '../contexts/SettingsContext'
import { useTts } from '../hooks/useTts'
import {
  getSlashCommand,
  matchSlashCommands,
  parseSlashCommand,
  type SlashCommandContext,
} from '../lib/slashCommands'
import PlanConfirmCard from './PlanConfirmCard'
import StopTaskButton from './StopTaskButton'
import SlashAutocomplete from './SlashAutocomplete'
import ChatMessageBubble, { type ArtifactFile } from './ChatMessageBubble'
import BubbleSpeakButton from './BubbleSpeakButton'
import BubbleCopyButton from './BubbleCopyButton'
import './ChatPanel.css'

const { Text } = Typography

/**
 * 可渲染成聊天气泡的 BusEvent/Message type 白名单。
 *
 * 为什么需要白名单：WS 事件流把所有 content truthy 的事件都灌进 logs，但只有
 * 「消息语义」的事件（agent_reply 智能体回复 / user_input 用户消息 / task_log
 * 任务日志 / slash_card slash 命令卡片）才该出现在聊天气泡流里。其余是 trace
 * 事件——coordinator_think 协调者思考、task_token 流式 token、task_think 工作思考、
 * task_tool 工具调用、agent_status 状态迁移、coordinator_plan 计划——它们有自己的
 * 展示区（LeaderPanel 思考链 / 流式气泡 / 气泡内折叠块 / 计划卡片），不该作为独立
 * 气泡混进消息气泡流。
 *
 * 特别是 coordinator_think：它携带协调者完整回复文本，若也桥接成气泡，会与随后
 * node_chat 持久化的 agent_reply 消息（id 不同，去重命中不了）同时渲染 → 「协调者
 * 回复两次」缺陷。白名单从源头排除这类重复。
 *
 * task_think 不在白名单（非独立气泡）：worker 在 ReAct 循环里流出的中间推理
 * （on_chat_model_end 的 think phase，registry.on_log → emit_task_think，data
 * {phase:'thinking'|'final'}）走 TraceEvent 流（useBusEvent events，mapKind→'think'），
 * 由 thinkEventsByTask（task 18）按 task_id 归并到对应流式/定稿气泡的 thinkEvents，
 * 由 ChatMessageBubble 渲染成气泡内折叠块（task 19）。worker think 是 ReAct 中间步、
 * 与该 task 最终回复不重复，故作为气泡内折叠块安全（区别于 coordinator_think 即协调者
 * 回复正文、会与 agent_reply 重复）。曾短暂试过白名单放行成独立气泡（task 17 过渡
 * 方案），但与归并折叠重复，故改走 events 归并路径，白名单不放行。
 */
const CHAT_MESSAGE_TYPES = new Set([
  'agent_reply',
  'user_input',
  'task_log',
  'slash_card',
])

/** antd Input.TextArea 的 ref 类型（antd v6 未从顶层导出 TextAreaRef，用 ComponentRef 推导）。 */
type TextAreaRef = ComponentRef<typeof Input.TextArea>

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
 *  coordinator 由 line 207 预过滤（id==='coordinator' 直接 #722ed1），不进 getAgentColor。 */
function getAgentColor(id: string, agents: AgentDefinition[]): string {
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
function formatElapsed(ms: number): string {
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
 * 额外排除数组（数组非 record），artifact manifest 是 dict 非数组，行为零变。 */
function extractFinalizedArtifacts(data: unknown): ArtifactFile[] {
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

/** 聊天气泡头像（从 GroupPage 抽出，逻辑/视觉不变） */
function ChatAvatar({ id, agents }: { id: string; agents: AgentDefinition[] }) {
  const hash = id.split('').reduce((a, c) => a + c.charCodeAt(0), 0)
  const ringDelay = (hash % 3000)
  const ringDuration = 2500 + (hash % 7) * 200
  const bobDelay = (hash >> 4) % 4000
  const bobDuration = 3000 + (hash >> 3) % 5 * 300

  if (id === 'user') {
    return (
      <div className="chat-avatar chat-avatar--user">
        <UserOutlined style={{ fontSize: 16, color: '#1677ff' }} />
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
        style={{ animationDelay: `${bobDelay}ms`, animationDuration: `${bobDuration}ms` }}
      />
      <span
        className="chat-avatar-ring"
        style={{ borderColor: color, animationDelay: `${ringDelay}ms`, animationDuration: `${ringDuration}ms` }}
      />
    </div>
  )
}

/** 获取发送者显示名 */
function SenderName({ id, agents }: { id: string; agents: AgentDefinition[] }) {
  if (id === 'user') return '用户'
  if (id === 'coordinator') return '群主(协调者)'
  if (id === 'broadcast') return '系统广播'
  if (id === 'system') return '系统'
  const agent = agents.find((a) => a.id === id)
  return agent?.name ?? id.slice(0, 8) + '...'
}

/** 高亮 @mention 的消息内容
 *
 *  B29 重渲染优化：`memo` + `memberNames` 稳定集两道防线。
 *
 *  问题：`HighlightMessage` 在 `chatMessages.flatMap` 里每条非用户消息渲染一次。ChatPanel
 *  是个大组件——`chatMessages`/`events`/`streaming`/`coordStreaming`/`agentStatuses` 任一
 *  变化（高频：task_token 流式逐字推送、stats ~200ms 节流、reasoning delta 攒批 flush）都
 *  触发整个 ChatPanel 重渲染，`flatMap` 重跑，**每条历史消息的 HighlightMessage 都重跑
 *  `content.split(regex)` + `members.some()`**——N 条消息 × 每次 setState 全量重算。长会话
 *  （几百条历史）+ 流式期高频重渲染，split+some 重复算 O(N×M) 是肉眼可见的卡顿源。
 *
 *  优化（两道防线，互补）：
 *  1. **`memo` 包裹**：props `content`(string|null) + `members`(GroupMember[]) 浅比较。`content`
 *     是消息正文（持久化后不变，除非编辑——本项目无编辑），`members` 是 ChatView state（切群时
 *     整体替换，平时稳定）。故 memo 让「props 没变的历史气泡」直接跳过重渲染——流式期只有当前
 *     正在流式的那条气泡（content 在变）+ stats 行重渲染，其余历史气泡 memo 命中零开销。
 *  2. **`memberNames` 稳定集**：原 `members.some(m => m.agent_name===name || m.alias===name)`
 *     每个候选 mention 都 O(M) 扫全部成员。改 `useMemo` 把 members 投影成 `Set<string>`（agent_name
 *     + alias 去空），查 mention 成员身份从 O(M).some → O(1).has。`memberNames` deps=[members]——
 *     members 引用变（切群）才重算 Set，平时稳定引用复用。
 *
 *  为何 memo 的 props 浅比较够用：`content` 是 string（值类型，=== 可靠）；`members` 是数组引用
 *  （ChatView 切群才 setMembers 新数组，平时同引用）。memo 默认 `Object.is` 浅比较这两类 props
 *  正确——不需自定义 areEqual。`members` 投影成 Set 后，HighlightMessage 内部不再依赖 members 数组
 *  结构（只读 Set），故 members 引用即使每帧变（不会，但假设）也不破 memo——memo 比 props 早短路。
 */
const HighlightMessage = memo(function HighlightMessage({
  content,
  members,
}: {
  content: string | null
  members: GroupMember[]
}) {
  // 成员名稳定集：agent_name + alias（去空）→ Set，mention 成员身份查 O(1)。
  // deps=[members]：members 引用变（切群/加成员）才重算 Set，平时稳定复用。
  const memberNames = useMemo(() => {
    const s = new Set<string>()
    for (const m of members) {
      if (m.agent_name) s.add(m.agent_name)
      if (m.alias) s.add(m.alias)
    }
    return s
  }, [members])

  if (!content) return <Text type="secondary" italic>（空消息）</Text>
  const parts = content.split(/(@[^\s,，.。!！?？:：;；\n]+)/g)
  return (
    <span>
      {parts.map((part, i) => {
        if (part.startsWith('@')) {
          const name = part.slice(1)
          if (memberNames.has(name)) {
            return <Tag key={i} color="blue" style={{ margin: 0, padding: '0 4px', lineHeight: '18px' }}>{part}</Tag>
          }
        }
        return <span key={i}>{part}</span>
      })}
    </span>
  )
})

/** 获取成员显示名 */
function getMemberDisplayName(member: GroupMember) {
  return member.alias || member.agent_name
}

/** ST-04 定稿气泡数据：task_complete/failed 收尾后，持久化回复尚未落地期间渲染的过渡气泡。 */
interface FinalizedBubble {
  key: string
  agentId: string
  agentName: string
  taskId: string
  content: string
  isFailed: boolean
  timestamp: number
  /** ST-06（task 21）：task_complete 事件 data.artifact.files[]（worker 任务产物 manifest）。
   *  仅成功路径携带（bus.py emit_task_completed 仅 success 时透传 scan_workspace_artifacts
   *  manifest）——失败/取消/超时路径 artifact key 缺省，extractFinalizedArtifacts 返空数组，
   *  失败气泡自然无下载卡（语义正确，失败不留产物）。ChatMessageBubble.artifactFiles 据此
   *  渲染下载卡（task 22：按扩展名图标 + 下载按钮）。 */
  artifactFiles: ArtifactFile[]
}

interface ChatPanelProps {
  /** 当前会话的群组（null/未选群时展示占位）。 */
  group: Group | null
  /** 全部智能体（用于头像角色色 + 发送者名解析 + @mention 候选）。 */
  agents: AgentDefinition[]
  /** 当前群成员（用于 @mention 候选 + 高亮 mention）。 */
  members: GroupMember[]
  /** 消息加载中态。 */
  loading?: boolean
  /** 群信息抽屉开关 setter（ChatPanel 头部「群信息」按钮触发，抽屉本体留 ChatView 管）。 */
  onOpenInfo?: () => void
  /** 清空聊天记录回调（重置时由父统一协调 messageApi.clearByGroup + reset-session，SH-04 仅触发回调）。 */
  onClearMessages?: () => void
  /**
   * 隐藏 ChatPanel 自带的聊天头部（标题+成员数+停止按钮+群信息按钮）。
   *
   * 左右布局重构后由 ChatView 统一渲染标题区（单聊显 agent 名/角色、群聊显群名+成员数+⚙群信息），
   * ChatPanel 不再自画头部，避免双头部。默认 false 保持向后兼容（独立使用时仍自带头部）。
   */
  hideHeader?: boolean
}

/**
 * SH-04 ChatPanel 聊天列：消息流 + 输入框 + 计划卡 + 停止按钮。
 *
 * 从 GroupPage 抽出聊天主区（原「中间对话区」），状态自治：
 *  - 消息流：本地 `chatMessages` state，loadMessages 拉历史 + WS logs 追加（与 GroupPage 逻辑一致）。
 *  - 输入框（SC-11 升级）：单行 Input → 多行 TextArea（autoSize 1~6 行），Enter 发送 /
 *    Shift+Enter 换行；保留 @mention 自动补全；接入 slash 命令拦截——回车时若整行是 /name args
 *    则走 getSlashCommand(name).handler(ctx) 而非默认发送。slash 补全下拉（SlashAutocomplete）
 *    输入 `/`（+ 前缀）时弹出，↑↓/Enter 选择。
 *  - 计划卡：PlanConfirmCard（plan 含 pending 步骤时展示于消息列表顶部）。
 *  - 停止按钮：从 BusEventContext.agentStatuses 找 executing agent，头部展示 StopTaskButton。
 *
 * plan / agentStatuses / logs 从 BusEventContext 消费（全应用共享一条 WS，不自起订阅）。
 * groupId 同样从 context（chatGroupId）——与 SessionList/ChatShell 共享全局聚焦会话。
 * 群信息抽屉、新建群组 Modal、群设置 Modal 等管理类 UI 留 ChatPage（SH-03）统一持有，
 * ChatPanel 通过 onOpenInfo/onClearMessages 回调触发，避免组件臃肿。
 */
export default function ChatPanel({
  group,
  agents,
  members,
  loading,
  onOpenInfo,
  hideHeader,
}: ChatPanelProps) {
  const { groupId: chatGroupId, logs, plan, agentStatuses, streaming, events, coordStreaming, coordReasoning, coordStats, refreshPlan } = useBusEventContext()
  // TTS 自动朗读：读 SettingsContext.tts 配置 + useTts 引擎。speak 在新 agent_reply 落地 effect 中触发。
  const { tts } = useSettings()
  const { supported: ttsSupported, speak: ttsSpeak } = useTts()
  const [chatMessages, setChatMessages] = useState<Message[]>([])
  const [chatLoading, setChatLoading] = useState(false)
  const [sending, setSending] = useState(false)
  const [chatInput, setChatInput] = useState('')
  const chatEndRef = useRef<HTMLDivElement>(null)
  const messagesContainerRef = useRef<HTMLDivElement>(null)
  // 用户是否「贴底」——上滑读历史时置 false，新消息/流式增量不再自动滚到底，
  // 避免用户正读着旧消息被一把拽回最底部。发送消息 / 切群 时重置为 true。
  const stickToBottomRef = useRef(true)
  // 自动朗读「就绪」闸门：切群/重连会批量回灌历史消息进 logs，逐条触发 logs effect。
  // 拉历史前置 false、拉完置 true——仅 effect 在 true 时才朗读，挡掉初始历史回灌窗口。
  const autoPlayReadyRef = useRef(false)
  // 已朗读过的消息 id 集合——按 id 去重而非按时间戳。
  // 切群/重连会把历史消息重新灌进 logs（id 不变），用集合记下「读过哪些 id」即可跳过，
  // 不依赖前后端时钟同步（WSL2 后端时钟与 Windows 浏览器时钟常偏差秒级，时间戳比较会误判）。
  // 新到的 WS agent_reply 是全新 id，不在集合中 → 朗读 + 记入集合。
  const spokenIdsRef = useRef<Set<string>>(new Set())
  // 日期分组：上一条消息的 created_at，用于判断本条是否跨天（跨天则插日期分隔条）。
  // 切群时重置为 null，让新群首条消息渲染分隔条（否则可能误判与旧群末条同天）。
  const lastDateRef = useRef<string | null>(null)

  // B23：已退场定稿气泡的 task_id 集合——「reply 已落地、定稿气泡已退场」的增量真源。
  // finalizedBubbles 原 deps=[events, streaming, chatMessages, agentStatuses]——chatMessages
  // 每条新消息（含 task_token 流式期桥接的 task_log）都换新引用 → finalizedBubbles 全量重算
  // （遍历 events + 对每个收尾事件 chatMessages.some 扫描）。高频聊天时 finalizedBubbles 其实
  // 几乎不变（只在 task 收尾 + reply 落地两个时刻变化），却被每条新消息拖着重算——浪费。
  // B23 把「退场判定」从「每次重算 chatMessages.some」改为「reply 落地 effect 增量回填 ref」：
  //   - repliedTaskIdsRef.current: Set<string> 记录已退场 task_id（reply 已落地的 task）。
  //   - logs 桥接 effect（line ~628 setChatMessages）每次新 agent_reply 落地后，若其 task_id
  //     非空就把 task_id 加入 ref（标记该 task 的定稿气泡可退场）。
  //   - finalizedBubbles 改用 repliedTaskIdsRef.current.has(e.taskId) 判退场（O(1) 集合查），
  //     不再 chatMessages.some 扫描 → deps 去掉 chatMessages（chatMessages 变化不再触发重算）。
  // 切群 effect（line ~661）清空 ref（新群定稿状态独立）。
  //为何用 ref 不用 state：ref 变化不触发渲染（避免「回填 ref → finalizedBubbles 重算 → 渲染」
  // 链）。finalizedBubbles 的重算时机回归「events/streaming/agentStatuses 变化时」——这是
  // 定稿气泡真正变化的时机（task 收尾事件入 events / 流式缓冲清空 / agent 名变）。ref 只是
  // 让 finalizedBubbles 在重算时能读到最新退场集合，不自己驱动重算。这复刻 logsLenRef（B17
  // 增量桥接）+ spokenIdsRef（TTS 去重）+ lastDateRef（日期分组）的同款 ref-as-truth 模式。
  const repliedTaskIdsRef = useRef<Set<string>>(new Set())

  // 是否展示「回到底部」浮动按钮：上滑离底部一段距离（>120px）时显示。
  // 距底 80px 内视为贴底（与 stickToBottomRef 同阈值，但浮动按钮用更宽的 120px 门槛，
  // 让用户上滑一点点就能看到回底入口，不必滑到顶才有）。
  const [showScrollBottom, setShowScrollBottom] = useState(false)

  const handleContainerScroll = useCallback(() => {
    const el = messagesContainerRef.current
    if (!el) return
    const distToBottom = el.scrollHeight - el.scrollTop - el.clientHeight
    // 80px 阈值：距底不足一个气泡高度即视为贴底，新消息继续自动跟随。
    stickToBottomRef.current = distToBottom < 80
    // 120px 阈值：离底超过一个多气泡高度就显示「回到底部」浮动按钮。
    setShowScrollBottom(distToBottom > 120)
  }, [])

  // 点击「回到底部」：平滑滚到底 + 重置贴底态（后续新消息自动跟随）。
  const scrollToBottom = useCallback(() => {
    const el = messagesContainerRef.current
    if (!el) return
    stickToBottomRef.current = true
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  }, [])

  // ── @mention 自动补全 ──
  const [mentionOpen, setMentionOpen] = useState(false)
  const [mentionQuery, setMentionQuery] = useState('')
  const [mentionIndex, setMentionIndex] = useState(0)
  const inputRef = useRef<TextAreaRef | null>(null)
  const [inputCursor, setInputCursor] = useState(0)

  // ── slash 命令补全（SC-11）──
  const [slashOpen, setSlashOpen] = useState(false)
  const [slashIndex, setSlashIndex] = useState(0)
  const [slashQuery, setSlashQuery] = useState('')
  const slashCommands = slashOpen ? matchSlashCommands(slashQuery) : []

  const mentionCandidates = members.filter((m) =>
    getMemberDisplayName(m).toLowerCase().includes(mentionQuery.toLowerCase()),
  )

  // PL-11：当前群组中正在 executing 的智能体（群聊头部停止按钮入口）。
  const executingAgent = chatGroupId
    ? Object.values(agentStatuses).find(
        (a) => a.status === 'executing' && a.current_task_id,
      )
    : undefined

  // 计划含 pending 步骤 → 展示计划确认卡片（M12-PL02）。
  const showPlanCard =
    !!chatGroupId &&
    !!plan &&
    plan.length > 0 &&
    plan.some((s) => s.status === 'pending')

  // ST-02：流式 token 接入聊天气泡逐字渲染。
  // BusEventContext.streaming[task_id] 是 PL-08 逐字增量拼接的「正在生成」缓冲。
  // 对每个 executing 且有 current_task_id 的 agent，若其 task 缓冲非空，在消息流末尾追加一条
  // 流式气泡（ChatMessageBubble isStreaming=true），content=streaming[taskId]，尾部闪烁光标。
  // 已被 logs 收尾事件（task_complete/failed/dispatch，见 useBusEvent 清缓冲逻辑）收编为持久
  // 气泡的 task 不再展示流式气泡——streaming[tid] 被清空后 streamings 自然过滤掉。
  // 多 agent 同时执行时各占一条流式气泡（按 agentStatuses 顺序）。
  const streamingBubbles = chatGroupId
    ? Object.values(agentStatuses)
        .filter(
          (a) =>
            a.status === 'executing' &&
            a.current_task_id &&
            streaming[a.current_task_id],
        )
        .map((a) => ({
          agentId: a.id,
          agentName: a.name,
          taskId: a.current_task_id as string,
          content: streaming[a.current_task_id as string] as string,
        }))
    : []

  // 协调者流式气泡：coordinator_token 按 reply_id 累积的 delta，配合 coordinator_stats
  // 渲染 Claude-Code 风格状态行（"model · Ns · ↓ N tokens（含 N 推理）· thinking"）。
  // 与 worker 流式气泡区别：协调者无 task_id（不经 create_react_agent），按 reply_id 归并；
  // sender 是 group.coordinator_id（真实 agent_id，ChatAvatar/SenderName 据此解析角色色/名）。
  // phase="done" 时 useBusEvent 清空 coordStreaming[reply_id] → 气泡自然退场，
  // 由随后落地的持久化 agent_reply 接管（同 worker streaming→finalized 模式）。
  // reasoning 取 coordReasoning[reply_id]——推理模型在可见 content 前流出的内部思维链，
  // 传给 ChatMessageBubble 渲染默认折叠的「思考过程」区，用户可自行展开/收起。
  const coordinatorStreamingBubbles = chatGroupId
    ? Object.entries(coordStreaming).map(([replyId, content]) => ({
        replyId,
        content,
        reasoning: coordReasoning[replyId] || '',
        stats: coordStats[replyId],
      }))
    : []

  // ST-03：task_tool 事件接入聊天气泡——按 task 聚合工具摘要行。
  // events 是全局 TraceEvent 流（useBusEvent cap 500），按 taskId 分组 kind==='tool'
  // 事件；流式气泡按其 current_task_id 取对应工具行，渲染在气泡顶部（ChatMessageBubble
  // toolEvents）。task 与执行 worker 1:1，按 taskId 过滤即该 agent 当前任务的全部工具调用。
  // useMemo 稳住引用：task_tool 远少于 task_token，但分组仍 memo 避免每帧重算波及子组件。
  const toolEventsByTask = useMemo(() => {
    const m: Record<string, TraceEvent[]> = {}
    for (const e of events) {
      if (e.kind !== 'tool' || !e.taskId) continue
      ;(m[e.taskId] || (m[e.taskId] = [])).push(e)
    }
    return m
  }, [events])

  // ST-05（task 18）：task_think 事件按 task_id 归并——worker ReAct 循环里 on_chat_model_end
  // 流出的中间推理片段（registry.on_log think/answer → emit_task_think，data {phase:'thinking'|'final'}，
  // useBusEvent mapKind→'think'）。与 toolEventsByTask 同构、同来源（events cap 500），同按 taskId 分组。
  // 流式气泡（streamingBubbles，b.taskId）与定稿气泡（finalizedBubbles，b.taskId）各取对应 think 行
  // 传入 ChatMessageBubble.thinkEvents，渲染为气泡内折叠块（task 19 渲染）。worker think 是 ReAct 中间步、
  // 与该 task 最终回复不重复，安全归并（区别于 coordinator_think 那个会重复的坑，coordinator 不经此通道）。
  // 持久化 agent_reply 气泡也可挂 think——若其 task_id 非空且 events 里有对应 think，复用同一归并表即可。
  // useMemo 稳住引用：与 toolEventsByTask 同 memo 策略，避免每帧重算波及子组件。
  const thinkEventsByTask = useMemo(() => {
    const m: Record<string, TraceEvent[]> = {}
    for (const e of events) {
      if (e.kind !== 'think' || !e.taskId) continue
      ;(m[e.taskId] || (m[e.taskId] = [])).push(e)
    }
    return m
  }, [events])

  // ST-04：task_complete/failed 时定稿流式气泡——用持久化消息替换缓冲。
  // events 中 kind 'complete'/'failed' 标志 task 收尾（携带 result[:500]）。对每个收尾
  // task，若其流式缓冲已清（不在 streaming，即 useBusEvent 收尾逻辑已清空），渲染一条
  // 定稿气泡（ChatMessageBubble isStreaming=false）：content=收尾事件 result，toolEvents
  // 保留 ST-03 工具摘要行。填补「流式气泡消失 ↔ 持久化回复出现」的间隙——避免生成内容
  // + 工具调用瞬间蒸发。
  // 自动退场：当该 task 的持久化回复消息落进 chatMessages 即过滤掉定稿气泡——持久化
  // 回复接管，无永久重复。
  // 退场匹配（B22 重写·消除时序依赖）：主路径按 task_id 精确匹配——
  //   replied = chatMessages.some(m => m.sender_id === e.agentId && m.task_id === e.taskId)
  // 后端 _reply（execute 收尾 announce：任务完成🎉/执行出错了/⏹任务已停止/⏱超时）经
  // persist_agent_reply(task_id=...) 把 task_id 落到 message.task_id + message_added WS
  // 事件，前端 ChatPanel logs 桥接（line ~614）把 log.taskId 回填到 chatMessages 的
  // task_id——故 e.taskId（task_complete/failed 事件的 task_id）== m.task_id（持久化回复
  // 的 task_id）时回复已落地，退场定稿气泡。
  // 这取代了原 fragile 的「sender_id 匹配 + created_at >= 收尾事件时间戳」判定——原判定
  // 依赖前后端时钟同步（WSL2 后端 UTC 与 Windows 浏览器本地时区常偏差秒级，时间戳比较
  // 会误判）+ logs 追加路径 coerce WS 消息时 task_id「可能丢失」（原注释自承 fragile）。
  // B22 让后端把 task_id 持久化到回复行（reload-safe：切群/重连回灌从 DB 重建 chatMessages
  // 时 task_id 仍在），前端按精确 task_id 匹配——同一 task_id 在收尾事件和退场回复上都有，
  // 不论经 live WS 还是 reload-from-DB 抵达都能匹配。
  // 兜底 sender+时间戳保留：chat 路径（coordinator/worker node_chat）的 agent_reply
  // 不经 _reply（走 graph _unified_reply 不传 task_id）→ m.task_id===null，task_id 匹配
  // 不命中。但 chat 路径无 task_complete/failed 事件（非 execute 路径），finalizedBubbles
  // 循环根本不会为 chat 回复生成定稿气泡（kind 仅 complete/failed 进循环）——故兜底分支
  // 实际不命中，保留仅防御性（未来若 chat 路径也接 task_complete 收尾，兜底仍能退场）。
  // ST-06（task 21）：成功路径同时提取 data.artifact.files[]（extractFinalizedArtifacts）
  // → artifactFiles 传 ChatMessageBubble 渲染下载卡。失败/取消/超时路径后端不透传 artifact
  // （bus.py emit_task_completed 仅 success 时写 data.artifact）→ 返空数组 → 失败气泡无下载卡。
  const finalizedBubbles = useMemo(() => {
    const out: FinalizedBubble[] = []
    const seen = new Set<string>()
    // B23：退场判定读 repliedTaskIdsRef（reply 落地 effect 增量回填的 task_id 集合），
    // 不再 chatMessages.some 全量扫描——chatMessages 变化不再触发本 memo 重算（deps 去掉
    // chatMessages）。reply 落地时 effect 往 ref 加 task_id，本 memo 下次因 events/streaming
    // 变化重算时读到最新集合。集合查 O(1) vs chatMessages.some O(n) 扫描，且避 chatMessages
    // 每条新消息拖重算。reload-safe：切群/重连回灌从 DB 拉 chatMessages 时，logs 桥接
    // effect 重扫历史 agent_reply 也会回填 ref（历史 agent_reply 带 task_id 同样入集合），
    // 故 reload 后退场状态与 live 一致。
    const repliedTaskIds = repliedTaskIdsRef.current
    for (const e of events) {
      if (e.kind !== 'complete' && e.kind !== 'failed') continue
      if (!e.taskId || seen.has(e.taskId)) continue
      seen.add(e.taskId)
      // 仍在流式（缓冲未清）→ 流式气泡自己渲染，不定稿
      if (streaming[e.taskId]) continue
      // 持久化回复已落地 → 已被替换，不再渲染定稿气泡。
      // B22：主路径按 task_id 精确匹配（repliedTaskIds.has，reload-safe，不依赖时钟同步）。
      // B23：改读 repliedTaskIdsRef（reply 落地 effect 回填），去 chatMessages.some 扫描。
      // 兜底 sender+时间戳保留：chat 路径无 task_id 时防御性退场（实际不命中——chat 路径
      // 无 complete/failed 事件不进循环）。chatMessages 仍读一次仅供兜底时间戳比较（只在
      // repliedTaskIds 未命中时才扫，主路径 task_id 命中即短路不扫——hot path O(1)）。
      if (repliedTaskIds.has(e.taskId)) continue
      if (
        chatMessages.some(
          (m) =>
            m.sender_id === e.agentId &&
            new Date(m.created_at).getTime() >= e.timestamp,
        )
      )
        continue
      out.push({
        key: `finalized-${e.taskId}`,
        agentId: e.agentId,
        agentName: agentStatuses[e.agentId]?.name || e.agentId,
        taskId: e.taskId,
        content: e.content || '',
        isFailed: e.kind === 'failed',
        timestamp: e.timestamp,
        artifactFiles: extractFinalizedArtifacts(e.data),
      })
    }
    return out
  }, [events, streaming, agentStatuses])
  // B23：deps 去掉 chatMessages——退场判定改读 repliedTaskIdsRef（reply 落地 effect 回填），
  // 不再 chatMessages.some 扫描。chatMessages 仅在兜底时间戳分支读一次（主路径 task_id
  // 命中即短路不扫），故 chatMessages 变化无需触发本 memo 重算——避每条新消息拖重算。
  // chatMessages 仍读一次的原因：兜底分支需它做时间戳比较（task_id-less 路径防御性退场），
  // 但读它是「重算时顺带读最新 chatMessages」，非「chatMessages 变化驱动重算」——chatMessages
  // 不进 deps 不影响兜底正确性（兜底命中的极端 case——chat 路径无 complete/failed 事件
  // 实际不进循环——本就几乎不触发，且触发时 chatMessages 已含该回复，下次 events 变化
  // 重算时读到）。

  // 新消息追加到末尾（跳过用户自己发的，已由乐观更新处理）——与 GroupPage 逻辑一致。
  // 按类型白名单过滤：agent_reply/user_input/task_log/slash_card 桥接成聊天气泡，
  // 其余 trace 事件（coordinator_think/task_token/task_think/task_tool/agent_status/
  // coordinator_plan/...）不进气泡——否则 coordinator_think 携带的完整回复文本会被
  // 渲染成气泡，与随后 node_chat 的 agent_reply 持久化消息（id 不同，不去重）重复，
  // 即「协调者回复两次」缺陷根因。
  // 注意：coordinator_think 等非白名单 type 直接跳过，不落进 chatMessages。
  // task_think 不走此 logs 桥接通道（会成独立气泡，与归并折叠重复）：它经 TraceEvent
  // 流（useBusEvent events，mapKind→'think'），由 thinkEventsByTask（task 18）按 task_id
  // 归并到对应流式/定稿气泡的 thinkEvents，由 ChatMessageBubble 渲染成气泡内折叠块（task 19）。
  //
  // B17 桥接遍历新增尾部（替代「只取 logs 最后一条」旧契约）：useBusEvent B17 把 logs
  // 改批量 flush（~50ms 聚合多条 entry 一次 setLogs），单次 logs 变化可能含多条新 entry。
  // 旧「logs[logs.length-1]」只桥接最后一条 → 同批更早的 task_log/agent_reply 气泡被丢
  // （回归）。改为遍历本次新增的尾部 entry（靠 wsMsgId 去重：setChatMessages prev.some
  // + spokenIdsRef 防 TTS 重读），已桥接过的 id 跳过，新 id 才桥接+朗读。
  // 增量判定靠 logsLenRef（上次桥接时的 logs.length）：本次只处理 logs[prevLen..]，避免
  // 每次 logs 变化都全量重扫（logs cap 200，全量扫虽 O(200) 可接受，但增量更省且语义清晰）。
  const logsLenRef = useRef(0)
  useEffect(() => {
    const prevLen = logsLenRef.current
    logsLenRef.current = logs.length
    // B17：增量遍历上次桥接后的新增尾部（替代旧「只取最后一条」）。
    // prevLen > logs.length 时（重连回灌重建 logs 较短，或切群重置），从 0 重扫更稳——
    // 重灌的历史 id 不变，wsMsgId 去重会跳过已桥接的，不会重复加气泡。
    const start = prevLen > logs.length ? 0 : prevLen
    for (let i = start; i < logs.length; i++) {
      const log = logs[i]
      if (log.agentId === 'user') continue
      // 只把可成气泡的消息类型桥接进 chatMessages；思考/token/工具等 trace 事件跳过
      if (!CHAT_MESSAGE_TYPES.has(log.type)) continue
      const wsMsgId = log.id || `ws-${log.timestamp}`
      // 自动朗读：仅 agent_reply（智能体定稿回复）触发，且需总开关+自动朗读开关+引擎支持。
      // 去重靠 spokenIdsRef（按 id），不依赖前后端时钟同步（WSL2 后端时钟与 Windows 浏览器常偏差秒级，
      // 时间戳比较会误判）。切群/重连回灌的历史消息 id 不变 → 在集合里 → 跳过；新 WS 消息是全新 id → 朗读+记入。
      // autoPlayReadyRef 闸门挡掉切群首拉历史窗口（拉历史前置 false、拉完置 true）。
      if (
        log.type === 'agent_reply' &&
        tts.enabled &&
        tts.autoPlay &&
        ttsSupported &&
        autoPlayReadyRef.current &&
        !spokenIdsRef.current.has(wsMsgId)
      ) {
        spokenIdsRef.current.add(wsMsgId)
        ttsSpeak(log.message)
      }
      setChatMessages((prev) => {
        if (prev.some((m) => m.id === wsMsgId)) return prev
        return [...prev, {
          id: wsMsgId,
          group_id: chatGroupId || '',
          task_id: log.taskId || null,
          sender_id: log.agentId,
          receiver_id: 'broadcast',
          type: log.type,
          content: log.message,
          data: (log.data ?? null) as Record<string, unknown> | null,
          created_at: new Date(log.timestamp).toISOString(),
        }]
      })
      // B23：reply 落地回填退场集合——agent_reply 带 task_id 即该 task 的持久化回复已落地，
      // 标记其定稿气泡可退场（finalizedBubbles 据 repliedTaskIdsRef.has(task_id) 过滤）。
      // 只记 agent_reply（type='agent_reply' 是收尾 announce；task_log/user_input/slash_card
      // 无 task_id 或非回复语义）。task_id 为空（chat 路径）不入集合——其退场靠兜底时间戳
      // （finalizedBubbles 仍保留 sender+时间戳兜底分支，但 chat 路径无 complete/failed 事件
      // 实际不进循环，故兜底不命中）。增量回填：每次新 agent_reply 落地加一项 O(1)，不触发
      // 渲染（ref 变化不渲染）——finalizedBubbles 下次因 events/streaming 变化重算时读到最新集合。
      if (log.type === 'agent_reply' && log.taskId) {
        repliedTaskIdsRef.current.add(log.taskId)
      }
    }
  }, [logs, chatGroupId, tts.enabled, tts.autoPlay, ttsSupported])

  // 滚动到底部（仅滚动消息列表容器内部，不触发页面级滚动）。
  // 贴底跟随：仅在 stickToBottomRef 为 true（用户在底部附近）时自动滚，
  // 用户上滑读历史时新消息/流式增量不强行拽回——微信/钉钉同款手感。
  // 同步滚动放在 rAF 内：chatMessages 变化到 DOM 完成布局有间隙，
  // 直接 scrollTo 时 scrollHeight 可能还是旧值 → 滚不到底。
  useEffect(() => {
    if (!stickToBottomRef.current) return
    const el = messagesContainerRef.current
    if (!el) return
    const raf = requestAnimationFrame(() => {
      el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
    })
    return () => cancelAnimationFrame(raf)
  }, [chatMessages, streamingBubbles, coordinatorStreamingBubbles, finalizedBubbles])

  // 切换群组时加载历史消息（chatGroupId 来自全局 active group）。
  useEffect(() => {
    // 切群即贴底：新群历史消息加载后应展示最新一条，默认停在底部。
    stickToBottomRef.current = true
    // 关闭自动朗读闸门——拉历史期间不朗读历史 agent_reply（拉完置 true）。
    autoPlayReadyRef.current = false
    // 清空已朗读 id 集合：新群的 WS 消息都是新 id，旧集合的 id 与新群无关，
    // 保留会误把「旧群某条 id 恰好与新群新消息前缀撞上」的概率（虽极低）清掉。
    spokenIdsRef.current = new Set()
    // 重置日期分组游标：新群首条消息应渲染日期分隔条（与旧群末条无关联）。
    lastDateRef.current = null
    // B17：重置 logs 增量游标——新群 logs（经 useBusEvent 切群清空 + 拉历史重建）
    // 与旧群无关，避免 logsLenRef 停在旧群长度导致漏桥接/错位。切群后 logs effect
    // 从 0 重新扫，wsMsgId 去重保证不重复加气泡。
    logsLenRef.current = 0
    // B23：重置退场集合——新群的已退场 task_id 与旧群无关，避免旧群退场状态泄漏到新群
    // （旧群某 task_id 恰好与新群 task_id 撞——虽 tid 是 task_+uuid 概率极低，但语义独立
    // 应清）。新群历史 agent_reply 经 logs 桥接 effect 重扫回填（历史 agent_reply 带 task_id
    // 同样入集合），故 reload 后退场状态从历史重建，与 live 一致。
    repliedTaskIdsRef.current = new Set()
    if (chatGroupId) {
      setChatLoading(true)
      messageApi
        .listByGroup(chatGroupId)
        .then((data) => setChatMessages(data))
        .catch(() => setChatMessages([]))
        .finally(() => {
          setChatLoading(false)
          // 历史加载完打开闸门——仅此后通过 WS 新到达的 agent_reply 才朗读。
          autoPlayReadyRef.current = true
        })
    } else {
      setChatMessages([])
    }
  }, [chatGroupId])

  const handleSendMessage = async () => {
    if (!chatInput.trim() || !chatGroupId || sending) return
    setSending(true)
    const content = chatInput.trim()
    setChatInput('')
    setMentionOpen(false)
    setSlashOpen(false)
    // 发送即跟到底：用户主动发消息必然想看回复，强制贴底，回复/流式自动滚入视野。
    stickToBottomRef.current = true

    // SH-08 busy_input_mode：若有 agent 正在 executing，回车发送前先 interrupt 当前任务
    // （taskApi.stop）。语义：用户在智能体忙碌时回车输入 = 想打断它说新话，而非排队。
    // stop 是 best-effort——失败不阻断发送（可能任务恰好刚结束 / 后端 no-op 200），
    // 仅 toast 告知打断结果。stop 成功后引擎回 idle 会推 agent_status(idle) WS 事件，
    // useBusEvent 自动刷新 agentStatuses。
    const interrupting = executingAgent?.current_task_id
    if (interrupting) {
      try {
        const resp = await taskApi.stop(interrupting, chatGroupId)
        message.info(resp.message || `已打断 ${executingAgent!.name} 的任务`)
      } catch (e) {
        // 打断失败不阻断发送——用户消息优先级高于打断结果
        message.warning(`打断失败（仍发送消息）：${e instanceof Error ? e.message : String(e)}`)
      }
    }

    const tempId = `temp-${Date.now()}`
    const optimisticMsg: Message = {
      id: tempId,
      group_id: chatGroupId,
      task_id: null,
      sender_id: 'user',
      receiver_id: 'broadcast',
      type: 'user_input',
      content,
      data: null,
      created_at: new Date().toISOString(),
    }
    setChatMessages((prev) => [...prev, optimisticMsg])

    try {
      const sent = await messageApi.send({
        group_id: chatGroupId,
        sender_id: 'user',
        receiver_id: 'broadcast',
        type: 'user_input',
        content,
      })
      setChatMessages((prev) => {
        const alreadyExists = prev.some((m) => m.id === sent.id)
        if (alreadyExists) return prev.filter((m) => m.id !== tempId)
        return prev.map((m) => (m.id === tempId ? sent : m))
      })
    } catch {
      setChatMessages((prev) => prev.filter((m) => m.id !== tempId))
      setChatInput(content)
      message.error('发送失败')
    } finally {
      setSending(false)
    }
  }

  // SC-11：slash 命令执行——构造 SlashCommandContext 注入 handler，由 handler 自决副作用
  // （renderCard 推卡片进聊天流 / clearChat 清空视图 / 读 busState 纯本地聚合）。
  // 各 handler 当前为 stub（SC-01），SC-03~SC-10 替换为真实实现后自动生效。
  const handleSlashCommand = async (name: string, args: string) => {
    const cmd = getSlashCommand(name)
    if (!cmd) {
      message.warning(`未知命令：/${name}`)
      return
    }
    const ctx: SlashCommandContext = {
      groupId: chatGroupId,
      args,
      renderCard: (node: ReactNode) => {
        setChatMessages((prev) => [
          ...prev,
          {
            id: `slash-${name}-${Date.now()}`,
            group_id: chatGroupId || '',
            task_id: null,
            sender_id: 'system',
            receiver_id: 'broadcast',
            type: 'slash_card',
            content: typeof node === 'string' ? node : null,
            data: typeof node === 'string' ? null : { node },
            created_at: new Date().toISOString(),
          },
        ])
      },
      clearChat: () => {
        setChatMessages([])
        setSlashOpen(false)
        setMentionOpen(false)
      },
      busState: { agentStatuses, plan, streaming },
    }
    try {
      await cmd.handler(ctx)
    } catch (e) {
      message.error(`/${name} 执行失败：${e instanceof Error ? e.message : String(e)}`)
    }
    setChatInput('')
    setSlashOpen(false)
  }

  const handleInputChange = (
    e: React.ChangeEvent<HTMLTextAreaElement>,
  ) => {
    const value = e.target.value
    const cursor = e.target.selectionStart ?? value.length
    setChatInput(value)
    setInputCursor(cursor)

    const beforeCursor = value.slice(0, cursor)
    // @mention 检测：光标前最近一个 @ 触发成员补全（@ 后非空格字符为 query）。
    const atMatch = beforeCursor.match(/@([^\s]*)$/)
    if (atMatch) {
      setMentionQuery(atMatch[1])
      setMentionOpen(true)
      setMentionIndex(0)
    } else {
      setMentionOpen(false)
    }
    // slash 命令检测：仅当 `/` 出现在行首（前面只有空白或无字符）时触发——
    // 避免句中 `/`（如「用 a/b 方案」）误触。query = `/` 之后到光标的文本。
    const lineStart = beforeCursor.lastIndexOf('\n') + 1
    const lineToCursor = beforeCursor.slice(lineStart)
    const slashMatch = lineToCursor.match(/^\/(\S*)$/)
    if (slashMatch) {
      setSlashQuery(slashMatch[1])
      setSlashOpen(true)
      setSlashIndex(0)
    } else {
      setSlashOpen(false)
    }
  }

  const insertMention = useCallback((member: GroupMember) => {
    const name = getMemberDisplayName(member)
    const beforeCursor = chatInput.slice(0, inputCursor)
    const afterCursor = chatInput.slice(inputCursor)
    const atIndex = beforeCursor.lastIndexOf('@')
    if (atIndex === -1) return

    const newValue = beforeCursor.slice(0, atIndex) + `@${name} ` + afterCursor
    setChatInput(newValue)
    setMentionOpen(false)

    setTimeout(() => {
      const newCursor = atIndex + name.length + 2
      const textarea = inputRef.current?.resizableTextArea?.textArea
      textarea?.setSelectionRange(newCursor, newCursor)
      inputRef.current?.focus()
    }, 0)
  }, [chatInput, inputCursor])

  // slash 补全选中：把当前行首 `/query` 替换为 `/name `（name 后加空格，便于继续输参数）。
  const selectSlashCommand = useCallback((cmd: { name: string }) => {
    const cursor = inputCursor
    const before = chatInput.slice(0, cursor)
    const after = chatInput.slice(cursor)
    const lineStart = before.lastIndexOf('\n') + 1
    const slashIdx = before.indexOf('/', lineStart)
    if (slashIdx === -1) {
      setSlashOpen(false)
      return
    }
    // 保留 `/` 之前内容 + `/name ` + 光标后内容（丢弃 `/` 到光标间的旧 query）。
    const head = chatInput.slice(0, slashIdx)
    const rewritten = head + `/${cmd.name} ` + after
    setChatInput(rewritten)
    setSlashOpen(false)
    setTimeout(() => {
      const newCursor = (head + `/${cmd.name} `).length
      const textarea = inputRef.current?.resizableTextArea?.textArea
      textarea?.setSelectionRange(newCursor, newCursor)
      inputRef.current?.focus()
    }, 0)
  }, [chatInput, inputCursor])

  const handleInputKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // ── 补全下拉打开时，优先处理导航/选择（拦截 Enter/Arrow/Escape）──
    if (slashOpen && slashCommands.length > 0) {
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault()
        const cmd = slashCommands[slashIndex]
        if (cmd) selectSlashCommand(cmd)
        return
      }
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setSlashIndex((i) => (i + 1) % slashCommands.length)
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setSlashIndex((i) => (i - 1 + slashCommands.length) % slashCommands.length)
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        setSlashOpen(false)
        return
      }
    }
    if (mentionOpen && mentionCandidates.length > 0) {
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault()
        const candidate = mentionCandidates[mentionIndex]
        if (candidate) insertMention(candidate)
        return
      }
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setMentionIndex((idx) => (idx + 1) % mentionCandidates.length)
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setMentionIndex((idx) => (idx - 1 + mentionCandidates.length) % mentionCandidates.length)
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        setMentionOpen(false)
        return
      }
    }
    // ── 无补全下拉：Enter 发送 / Shift+Enter 换行（TextArea 默认 Enter 换行，这里反转）──
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      // slash 命令拦截：整行以 /name 开头时走 handler 而非默认发送。
      const parsed = parseSlashCommand(chatInput)
      if (parsed) {
        e.preventDefault()
        void handleSlashCommand(parsed.name, parsed.args)
        return
      }
      e.preventDefault()
      void handleSendMessage()
    }
    // Shift+Enter 不拦截 → TextArea 默认换行行为
  }

  return (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden', position: 'relative' }}>
      {/* 聊天头部 — 钉钉风格：标题 + 人数，右侧停止按钮 + 群信息按钮。
          hideHeader 时整段不渲染（左右布局由 ChatView 统一画标题区，避免双头部）。 */}
      {group && !hideHeader && (
        <div
          style={{
            padding: '12px 20px',
            borderBottom: '1px solid #f0f0f0',
            background: '#fff',
            flexShrink: 0,
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, overflow: 'hidden' }}>
            <Text strong style={{ fontSize: 15, flexShrink: 0 }}>
              {group.name}
            </Text>
            <Text type="secondary" style={{ fontSize: 13, flexShrink: 0 }}>
              ( {members.length + 1} )
            </Text>
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            {executingAgent && chatGroupId && (
              <StopTaskButton
                taskId={executingAgent.current_task_id!}
                groupId={chatGroupId}
                agentName={executingAgent.name}
              />
            )}
            <Tooltip title="群信息">
              <Button
                type="text"
                icon={<SettingOutlined />}
                size="small"
                onClick={onOpenInfo}
              />
            </Tooltip>
          </div>
        </div>
      )}

      {/* 消息列表 — minHeight:0 是钉死输入框的关键：flex 列布局中 flex 子项默认
          min-height:auto（不小于内容高），消息多了列表会撑高把输入框顶出可视区，
          表现为「输入框随消息一起漂浮滚动」。minHeight:0 解除该下限 → flex:1 收缩到
          父容器剩余高度，overflowY:auto 才真正在列表内部滚动，输入框（flexShrink:0）钉底。 */}
      <div
        ref={messagesContainerRef}
        onScroll={handleContainerScroll}
        style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '16px 20px' }}
      >
        {!chatGroupId ? (
          <div style={{ textAlign: 'center', padding: 60 }}>
            <Empty description="请在左侧选择一个群组开始对话" />
          </div>
        ) : chatLoading || loading ? (
          <div style={{ textAlign: 'center', padding: 40 }}>
            <Spin />
          </div>
        ) : chatMessages.length === 0 ? (
          <Empty description="暂无消息，开始对话吧" />
        ) : (
          chatMessages.flatMap((msg) => {
            const isUser = msg.sender_id === 'user'
            // 日期分组：当本条与上一条不在同一天时，插一条日期分隔条。
            // today/yesterday 用口语化标签，更早用完整日期；分隔条 sticky 顶部，
            // 滚动时当前可见天的标签常驻顶，便于定位「这是哪天的对话」（微信/钉钉同款）。
            const dateDivider = renderDateDivider(msg.created_at, lastDateRef.current)
            if (dateDivider) lastDateRef.current = msg.created_at
            // SC-11：slash 命令卡片（type=slash_card）——handler 经 ctx.renderCard 推入，
            // content 存字符串（stub 占位），data.node 存富卡片 ReactNode（SC-03~10 实现）。
            // 渲染为系统消息（左对齐，头像 + 卡片 + 时间戳），不复用 HighlightMessage 的
            // @mention 高亮。
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
            return [
              dateDivider,
              <div
                key={msg.id}
                className="chat-msg"
                style={{ flexDirection: isUser ? 'row-reverse' : 'row' }}
              >
                <ChatAvatar id={msg.sender_id} agents={agents} />
                <div className={`chat-bubble-wrap${isUser ? ' chat-bubble-wrap--self' : ''}`}>
                  {/* 单条气泡操作按钮组：hover 显隐。朗读仅非用户消息且总开关开时渲染；
                      复制对所有消息可见（用户/agent 都可复制）。
                      用户气泡右对齐——操作组改定位到左侧（.chat-bubble-wrap--self），
                      否则贴在右边缘会被容器 overflow 裁切、看不到。 */}
                  <div className="bubble-action-group">
                    <BubbleCopyButton content={msg.content ?? ''} />
                    {!isUser && tts.enabled && (
                      <BubbleSpeakButton content={msg.content ?? ''} />
                    )}
                  </div>
                  <div className={`chat-sender-name ${isUser ? 'chat-sender-name--right' : ''}`}>
                    <SenderName id={msg.sender_id} agents={agents} />
                  </div>
                  <div className={`chat-bubble ${isUser ? 'chat-bubble--self' : 'chat-bubble--other'}`}>
                    {/* 定稿协调者回复的推理折叠区：读持久化 agent_reply.data.reasoning。
                        复用 ChatMessageBubble（与流式期同一组件，受控展开逻辑统一）——
                        不再内联一份非受控 Collapse（原导致定稿气泡思考区默认收起且不接 reasoningExpanded，
                        与流式期行为不一致）。reasoningTokens 传落盘的真值；用户可手动展开看历史思考。 */}
                    {(() => {
                      const reasoning = extractCoordReasoning(msg.data)
                      if (!reasoning) return null
                      const rt = extractCoordStats(msg.data)?.reasoning_tokens
                      return (
                        <div style={{ marginBottom: 6 }}>
                          <Collapse
                            size="small"
                            ghost
                            items={[{
                              key: 'reasoning',
                              label: (
                                <span style={{ color: '#faad14', fontSize: 12, display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                                  <BulbOutlined style={{ fontSize: 12 }} />
                                  思考过程（{(rt && rt > 0 ? rt : Math.max(1, Math.ceil(reasoning.length / 3)))} tokens）
                                </span>
                              ),
                              children: (
                                <pre style={{
                                  margin: '6px 0 2px',
                                  padding: '8px 10px',
                                  background: 'rgba(250, 173, 20, 0.06)',
                                  borderLeft: '2px solid #faad14',
                                  borderRadius: 4,
                                  fontSize: 12,
                                  lineHeight: 1.6,
                                  color: '#595959',
                                  whiteSpace: 'pre-wrap',
                                  wordBreak: 'break-word',
                                  maxHeight: 320,
                                  overflowY: 'auto',
                                  fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
                                }}>
                                  {reasoning}
                                </pre>
                              ),
                            }]}
                          />
                        </div>
                      )
                    })()}
                    {isUser ? (
                      msg.content
                    ) : (
                      <HighlightMessage content={msg.content} members={members} />
                    )}
                  </div>
                  <div className={`chat-timestamp ${isUser ? 'chat-timestamp--right' : ''}`}>
                    {new Date(msg.created_at).toLocaleTimeString()}
                  </div>
                  {/* 定稿协调者回复的状态行：从持久化 agent_reply.data 取流式统计
                      （node_chat 落盘的 {reply_id, elapsed_ms, tokens, model, reasoning_tokens}），
                      渲染「model · Ns · ↓ N tokens（含 N 推理）· 完成」状态行。model 在最前——
                      用户能直观看到这条回复是哪个模型生成的（热切换模型后历史气泡保留当时的模型名）。
                      reasoning_tokens > 0 时追加「（含 N 推理）」——推理模型的 token 多为内部思维链，
                      点明后「5 字回复却 148 tokens」才可解释（其中 133 是看不见的推理）。
                      流式期间的统计在完成后保留可见——不随流式气泡退场消失。
                      非协调者 chat 回复（dispatch/summarize announce、user_input、task_log、slash_card）
                      data 无 elapsed_ms → extractCoordStats 返回 null → 不渲染状态行。 */}
                  {(() => {
                    const stats = extractCoordStats(msg.data)
                    if (!stats) return null
                    return (
                      <div className="chat-status-line">
                        {stats.model && (
                          <span className="chat-status-model">{stats.model}</span>
                        )}
                        {stats.model && ' · '}
                        {`${formatElapsed(stats.elapsed_ms)} · ↓ ${stats.tokens} tokens`}
                        {stats.reasoning_tokens && (
                          <span className="chat-status-reasoning">
                            {' '}（含 {stats.reasoning_tokens} 推理）
                          </span>
                        )}
                        {' · 完成'}
                      </div>
                    )
                  })()}
                </div>
              </div>,
            ]
          })
        )}
        {/* ST-02：流式生成气泡——executing agent 的 streaming[task_id] 逐字渲染。
         * 接在 chatMessages 之后渲染，自然落在消息流末尾（最新生成内容在底部）。
         * ChatMessageBubble isStreaming=true → 气泡淡蓝描边 + 尾部闪烁光标。
         * task_complete/failed 后 streaming[tid] 被清空（useBusEvent 收尾逻辑），bubble 自动消失；
         * ST-04 在此之后渲染定稿气泡（finalizedBubbles）填补间隙，待持久化回复落地后退场。 */}
        {streamingBubbles.map((b) => (
          <ChatMessageBubble
            key={`streaming-${b.taskId}`}
            senderId={b.agentId}
            senderName={b.agentName}
            avatar={<ChatAvatar id={b.agentId} agents={agents} />}
            content={b.content}
            timestamp={new Date().toISOString()}
            toolEvents={toolEventsByTask[b.taskId] || []}
            thinkEvents={thinkEventsByTask[b.taskId] || []}
            isStreaming
          />
        ))}
        {/* 协调者流式气泡：coordinator_token 按 reply_id 累积的 delta + coordinator_stats 状态行。
         * sender 用 group.coordinator_id（真实 agent_id），与持久化 agent_reply 的 sender_id 一致，
         * 头像/名按角色解析；statusLine 实时显示 "Ns · ↓ N tokens · thinking"。
         * phase="done" 时 coordStreaming 被清空 → 气泡退场，持久化 agent_reply 接管。 */}
        {coordinatorStreamingBubbles.map((b) => {
          const stats = b.stats
          const elapsedStr = stats
            ? stats.elapsed_ms < 1000
              ? `${stats.elapsed_ms}ms`
              : `${(stats.elapsed_ms / 1000).toFixed(1)}s`
            : '0s'
          const tokens = stats?.tokens ?? 0
          const reasoningTokens = stats?.reasoning_tokens
          const phaseLabel =
            stats?.phase === 'done' ? '完成' : '思考中'
          const model = stats?.model
          return (
            <ChatMessageBubble
              key={`coord-streaming-${b.replyId}`}
              senderId={group?.coordinator_id ?? 'coordinator'}
              senderName="群主(协调者)"
              avatar={
                <ChatAvatar id={group?.coordinator_id ?? 'coordinator'} agents={agents} />
              }
              content={b.content}
              reasoning={b.reasoning || undefined}
              reasoningTokens={reasoningTokens}
              timestamp={new Date().toISOString()}
              isStreaming={stats?.phase !== 'done'}
              statusLine={
                <>
                  {model && <span className="chat-status-model">{model}</span>}
                  {model && ' · '}
                  {`${elapsedStr} · ↓ ${tokens} tokens`}
                  {reasoningTokens && (
                    <span className="chat-status-reasoning">
                      {' '}（含 {reasoningTokens} 推理）
                    </span>
                  )}
                  {` · ${phaseLabel}`}
                </>
              }
            />
          )
        })}
        {/* ST-04：定稿气泡——task_complete/failed 后持久化回复落地前的过渡气泡。
         * content=收尾事件 result（result[:500]），保留 ST-03 工具摘要行，isStreaming=false。
         * 持久化回复（_reply）落地后自动退场（finalizedBubbles 内 replied 判定过滤），
         * 避免重复。失败任务用灰调气泡标记（isFailed → 调用方加 failed 描边 class）。 */}
        {finalizedBubbles.map((b) => (
          <ChatMessageBubble
            key={b.key}
            senderId={b.agentId}
            senderName={b.agentName}
            avatar={<ChatAvatar id={b.agentId} agents={agents} />}
            content={b.content}
            timestamp={new Date(b.timestamp).toISOString()}
            toolEvents={toolEventsByTask[b.taskId] || []}
            thinkEvents={thinkEventsByTask[b.taskId] || []}
            artifactFiles={b.artifactFiles}
            groupId={chatGroupId ?? undefined}
            isFailed={b.isFailed}
            actionGroup={
              <div className="bubble-action-group">
                <BubbleCopyButton content={b.content} />
                {tts.enabled && <BubbleSpeakButton content={b.content} />}
              </div>
            }
          />
        ))}
        <div ref={chatEndRef} />
      </div>

      {/* 回到底部浮动按钮——用户上滑读历史时浮现，点击平滑滚回最新消息。
          绝对定位在消息列表右下角（相对 ChatPanel 根容器），不随列表滚动（钉在可视区）。
          showScrollBottom 由 onScroll 维护（距底 >120px 显示），微信/钉钉同款手感。 */}
      {showScrollBottom && (
        <Tooltip title="回到底部">
          <Button
            className="scroll-bottom-btn"
            type="default"
            shape="circle"
            size="large"
            icon={<VerticalAlignBottomOutlined />}
            onClick={scrollToBottom}
          />
        </Tooltip>
      )}

      {/* 计划确认卡——粘在输入框上方，不随消息列表滚动。
          原先卡片渲染在消息列表顶部（messagesContainerRef 内），出计划后用户一发问或协调者一回复，
          新消息就把卡片顶出可视区，看不到也点不到确认按钮。现抽出滚动容器，钉在输入框正上方：
          计划有 pending 步骤时展示，flexShrink:0 保证它和输入框都不被消息列表挤掉；
          卡内步骤多时 maxHeight + 自滚，避免撑高把输入框顶出可视区。 */}
      {showPlanCard && plan && chatGroupId && (
        <div style={{ flexShrink: 0, padding: '8px 16px 0', background: '#fff' }}>
          <div style={{ maxHeight: 280, overflowY: 'auto', padding: 2 }}>
            <PlanConfirmCard groupId={chatGroupId} plan={plan} refreshPlan={refreshPlan} />
          </div>
        </div>
      )}

      {/* 输入框 */}
      {chatGroupId && (
        <div style={{ padding: '12px 16px', borderTop: '1px solid #f0f0f0', background: '#fff', flexShrink: 0, position: 'relative' }}>
          {slashOpen && slashCommands.length > 0 && (
            <SlashAutocomplete
              commands={slashCommands}
              activeIndex={slashIndex}
              onSelect={selectSlashCommand}
              onHover={setSlashIndex}
            />
          )}
          {mentionOpen && mentionCandidates.length > 0 && (
            <div
              style={{
                position: 'absolute',
                bottom: '100%',
                left: 16,
                marginBottom: 4,
                background: '#fff',
                border: '1px solid #f0f0f0',
                borderRadius: 6,
                boxShadow: '0 4px 12px rgba(0,0,0,0.1)',
                zIndex: 100,
                maxHeight: 200,
                overflowY: 'auto',
                width: 220,
              }}
            >
              {mentionCandidates.map((m, idx) => (
                <div
                  key={m.id}
                  onClick={() => insertMention(m)}
                  style={{
                    padding: '8px 12px',
                    cursor: 'pointer',
                    background: idx === mentionIndex ? '#e6f4ff' : '#fff',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                  }}
                >
                  <RobotOutlined style={{ color: '#1677ff' }} />
                  <div>
                    <div style={{ fontSize: 13 }}>{getMemberDisplayName(m)}</div>
                    <div style={{ fontSize: 11, color: '#999' }}>{m.agent_role}</div>
                  </div>
                </div>
              ))}
            </div>
          )}
          <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
            <Input.TextArea
              ref={inputRef}
              value={chatInput}
              onChange={handleInputChange}
              onKeyDown={handleInputKeyDown}
              placeholder="输入消息... @ 点名成员，/ 触发命令，Enter 发送，Shift+Enter 换行（智能体忙碌时回车会先打断当前任务）"
              disabled={sending}
              autoSize={{ minRows: 1, maxRows: 6 }}
              style={{ flex: 1, resize: 'none' }}
            />
            <Button
              type="primary"
              icon={<SendOutlined />}
              onClick={handleSendMessage}
              loading={sending}
            >
              发送
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}

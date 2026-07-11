import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Button, Empty, Input, Spin, Tag, Tooltip, Typography, message } from 'antd'
import type { ComponentRef } from 'react'
import { RobotOutlined, SendOutlined, SettingOutlined, UserOutlined } from '@ant-design/icons'
import {
  messageApi,
  taskApi,
  type AgentDefinition,
  type Group,
  type GroupMember,
  type Message,
  type TraceEvent,
} from '../services/api'
import { useBusEventContext } from '../contexts/BusEventContext'
import {
  getSlashCommand,
  matchSlashCommands,
  parseSlashCommand,
  type SlashCommandContext,
} from '../lib/slashCommands'
import PlanConfirmCard from './PlanConfirmCard'
import StopTaskButton from './StopTaskButton'
import SlashAutocomplete from './SlashAutocomplete'
import ChatMessageBubble from './ChatMessageBubble'
import './ChatPanel.css'

const { Text } = Typography

/**
 * 可渲染成聊天气泡的 BusEvent/Message type 白名单。
 *
 * 为什么需要白名单：WS 事件流把所有 content truthy 的事件都灌进 logs，但只有
 * 「消息语义」的事件（agent_reply 智能体回复 / user_input 用户消息 / task_log
 * 任务日志 / slash_card slash 命令卡片）才该出现在聊天气泡流里。其余是 trace
 * 事件——coordinator_think 协调者思考、task_token 流式 token、task_think/tool
 * 工作过程、agent_status 状态迁移、coordinator_plan 计划——它们有自己的展示区
 * （LeaderPanel 思考链 / 流式气泡 / 计划卡片），不该再混进消息气泡流。
 *
 * 特别是 coordinator_think：它携带协调者完整回复文本，若也桥接成气泡，会与随后
 * node_chat 持久化的 agent_reply 消息（id 不同，去重命中不了）同时渲染 → 「协调者
 * 回复两次」缺陷。白名单从源头排除这类重复。
 */
const CHAT_MESSAGE_TYPES = new Set([
  'agent_reply',
  'user_input',
  'task_log',
  'slash_card',
])

/** antd Input.TextArea 的 ref 类型（antd v6 未从顶层导出 TextAreaRef，用 ComponentRef 推导）。 */
type TextAreaRef = ComponentRef<typeof Input.TextArea>

/** 获取智能体角色主题色 */
function getAgentColor(id: string, agents: AgentDefinition[]): string {
  const ROLE_COLORS: Record<string, string> = {
    '后端开发工程师': '#6366f1',
    '前端开发工程师': '#06b6d4',
    '测试工程师': '#f59e0b',
    'DevOps 工程师': '#10b981',
    '产品经理': '#f43f5e',
    '自定义': '#8b5cf6',
  }
  const agent = agents.find((a) => a.id === id)
  return agent ? (ROLE_COLORS[agent.role] ?? '#8b5cf6') : '#722ed1'
}

/** 毫秒 → 人类可读耗时：<1s 显示 ms，否则保留 1 位小数秒（与 ChatMessageBubble 一致）。 */
function formatElapsed(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

/** 从持久化 agent_reply 的 data 字段提取协调者流式统计。
 *  node_chat 经 _unified_reply 把 {reply_id, elapsed_ms, tokens} 落盘到 message.data，
 *  定稿气泡据此渲染「Ns · ↓ N tokens · 完成」状态行——流式期间的统计在完成后保留可见，
 *  不随流式气泡退场消失。非协调者 chat 回复（dispatch/summarize announce、user_input、
 *  task_log、slash_card）data 无 elapsed_ms → null，不渲染状态行。 */
function extractCoordStats(
  data: Record<string, unknown> | null,
): { elapsed_ms: number; tokens: number } | null {
  if (!data) return null
  const elapsed = Number(data.elapsed_ms)
  if (!Number.isFinite(elapsed) || elapsed <= 0) return null
  const tokens = Number(data.tokens)
  return { elapsed_ms: elapsed, tokens: Number.isFinite(tokens) ? tokens : 0 }
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

/** 高亮 @mention 的消息内容 */
function HighlightMessage({ content, members }: { content: string | null; members: GroupMember[] }) {
  if (!content) return <Text type="secondary" italic>（空消息）</Text>
  const parts = content.split(/(@[^\s,，.。!！?？:：;；\n]+)/g)
  return (
    <span>
      {parts.map((part, i) => {
        if (part.startsWith('@')) {
          const name = part.slice(1)
          const isMember = members.some((m) => m.agent_name === name || m.alias === name)
          if (isMember) {
            return <Tag key={i} color="blue" style={{ margin: 0, padding: '0 4px', lineHeight: '18px' }}>{part}</Tag>
          }
        }
        return <span key={i}>{part}</span>
      })}
    </span>
  )
}

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
  const { groupId: chatGroupId, logs, plan, agentStatuses, streaming, events, coordStreaming, coordStats } = useBusEventContext()
  const [chatMessages, setChatMessages] = useState<Message[]>([])
  const [chatLoading, setChatLoading] = useState(false)
  const [sending, setSending] = useState(false)
  const [chatInput, setChatInput] = useState('')
  const chatEndRef = useRef<HTMLDivElement>(null)

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
  // 渲染 Claude-Code 风格状态行（"Ns · ↓ N tokens · thinking"）。
  // 与 worker 流式气泡区别：协调者无 task_id（不经 create_react_agent），按 reply_id 归并；
  // sender 是 group.coordinator_id（真实 agent_id，ChatAvatar/SenderName 据此解析角色色/名）。
  // phase="done" 时 useBusEvent 清空 coordStreaming[reply_id] → 气泡自然退场，
  // 由随后落地的持久化 agent_reply 接管（同 worker streaming→finalized 模式）。
  const coordinatorStreamingBubbles = chatGroupId
    ? Object.entries(coordStreaming).map(([replyId, content]) => ({
        replyId,
        content,
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

  // ST-04：task_complete/failed 时定稿流式气泡——用持久化消息替换缓冲。
  // events 中 kind 'complete'/'failed' 标志 task 收尾（携带 result[:500]）。对每个收尾
  // task，若其流式缓冲已清（不在 streaming，即 useBusEvent 收尾逻辑已清空），渲染一条
  // 定稿气泡（ChatMessageBubble isStreaming=false）：content=收尾事件 result，toolEvents
  // 保留 ST-03 工具摘要行。填补「流式气泡消失 ↔ 持久化回复出现」的间隙——避免生成内容
  // + 工具调用瞬间蒸发。
  // 自动退场：当该 agent 的持久化回复消息落进 chatMessages（sender_id 匹配 + 时间晚于
  // 收尾事件）即过滤掉定稿气泡——持久化回复接管，无永久重复。匹配按 sender+时间而非
  // task_id：logs 追加路径会把所有 WS 消息 coerce 成 type:'log' 且 task_id 可能丢失，
  // 故用「该 agent 在收尾时间戳之后的消息」判定回复已落地（_reply 是收尾后唯一后续消息）。
  const finalizedBubbles = useMemo(() => {
    const out: FinalizedBubble[] = []
    const seen = new Set<string>()
    for (const e of events) {
      if (e.kind !== 'complete' && e.kind !== 'failed') continue
      if (!e.taskId || seen.has(e.taskId)) continue
      seen.add(e.taskId)
      // 仍在流式（缓冲未清）→ 流式气泡自己渲染，不定稿
      if (streaming[e.taskId]) continue
      // 持久化回复已落进 chatMessages → 已被替换，不再渲染定稿气泡
      const replied = chatMessages.some(
        (m) =>
          m.sender_id === e.agentId &&
          new Date(m.created_at).getTime() >= e.timestamp,
      )
      if (replied) continue
      out.push({
        key: `finalized-${e.taskId}`,
        agentId: e.agentId,
        agentName: agentStatuses[e.agentId]?.name || e.agentId,
        taskId: e.taskId,
        content: e.content || '',
        isFailed: e.kind === 'failed',
        timestamp: e.timestamp,
      })
    }
    return out
  }, [events, streaming, chatMessages, agentStatuses])

  // 新消息追加到末尾（跳过用户自己发的，已由乐观更新处理）——与 GroupPage 逻辑一致。
  // 按类型白名单过滤：只 agent_reply/user_input/task_log/slash_card 桥接成聊天气泡，
  // 其余 trace 事件（coordinator_think/task_token/task_think/task_tool/agent_status/
  // coordinator_plan/...）不进气泡——否则 coordinator_think 携带的完整回复文本会被
  // 渲染成气泡，与随后 node_chat 的 agent_reply 持久化消息（id 不同，不去重）重复，
  // 即「协调者回复两次」缺陷根因。logs 只取最后一条（旧契约，保留）。
  // 注意：lastLog 若是 coordinator_think 直接 return，不落进 chatMessages。
  useEffect(() => {
    if (logs.length === 0) return
    const lastLog = logs[logs.length - 1]
    if (lastLog.agentId === 'user') return
    // 只把可成气泡的消息类型桥接进 chatMessages；思考/token/工具等 trace 事件跳过
    if (!CHAT_MESSAGE_TYPES.has(lastLog.type)) return
    setChatMessages((prev) => {
      const wsMsgId = lastLog.id || `ws-${lastLog.timestamp}`
      if (prev.some((m) => m.id === wsMsgId)) return prev
      return [...prev, {
        id: wsMsgId,
        group_id: chatGroupId || '',
        task_id: lastLog.taskId || null,
        sender_id: lastLog.agentId,
        receiver_id: 'broadcast',
        type: lastLog.type,
        content: lastLog.message,
        data: (lastLog.data ?? null) as Record<string, unknown> | null,
        created_at: new Date(lastLog.timestamp).toISOString(),
      }]
    })
  }, [logs, chatGroupId])

  // 滚动到底部
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [chatMessages])

  // 切换群组时加载历史消息（chatGroupId 来自全局 active group）。
  useEffect(() => {
    if (chatGroupId) {
      setChatLoading(true)
      messageApi
        .listByGroup(chatGroupId)
        .then((data) => setChatMessages(data.reverse()))
        .catch(() => setChatMessages([]))
        .finally(() => setChatLoading(false))
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
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
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

      {/* 消息列表 */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '16px 20px' }}>
        {showPlanCard && plan && chatGroupId && (
          <PlanConfirmCard groupId={chatGroupId} plan={plan} />
        )}
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
          chatMessages.map((msg) => {
            const isUser = msg.sender_id === 'user'
            // SC-11：slash 命令卡片（type=slash_card）——handler 经 ctx.renderCard 推入，
            // content 存字符串（stub 占位），data.node 存富卡片 ReactNode（SC-03~10 实现）。
            // 渲染为系统消息气泡（左对齐，不复用 HighlightMessage 的 @mention 高亮）。
            if (msg.type === 'slash_card') {
              return (
                <div key={msg.id} className="chat-msg" style={{ flexDirection: 'row' }}>
                  <ChatAvatar id="system" agents={agents} />
                  <div className="chat-bubble-wrap">
                    <div className="chat-sender-name">
                      <SenderName id="system" agents={agents} />
                    </div>
                    <div className="chat-bubble chat-bubble--other">
                      {msg.data?.node as ReactNode ?? msg.content}
                    </div>
                    <div className="chat-timestamp">
                      {new Date(msg.created_at).toLocaleTimeString()}
                    </div>
                  </div>
                </div>
              )
            }
            return (
              <div
                key={msg.id}
                className="chat-msg"
                style={{ flexDirection: isUser ? 'row-reverse' : 'row' }}
              >
                <ChatAvatar id={msg.sender_id} agents={agents} />
                <div className="chat-bubble-wrap">
                  <div className={`chat-sender-name ${isUser ? 'chat-sender-name--right' : ''}`}>
                    <SenderName id={msg.sender_id} agents={agents} />
                  </div>
                  <div className={`chat-bubble ${isUser ? 'chat-bubble--self' : 'chat-bubble--other'}`}>
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
                      （node_chat 落盘的 {reply_id, elapsed_ms, tokens}），渲染「Ns · ↓ N tokens · 完成」。
                      流式期间的统计在完成后保留可见——不随流式气泡退场消失。
                      非协调者 chat 回复（dispatch/summarize announce、user_input、task_log、slash_card）
                      data 无 elapsed_ms → extractCoordStats 返回 null → 不渲染状态行。 */}
                  {(() => {
                    const stats = extractCoordStats(msg.data)
                    if (!stats) return null
                    return (
                      <div className="chat-status-line">
                        {`${formatElapsed(stats.elapsed_ms)} · ↓ ${stats.tokens} tokens · 完成`}
                      </div>
                    )
                  })()}
                </div>
              </div>
            )
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
          const phaseLabel =
            stats?.phase === 'done' ? '完成' : '思考中'
          return (
            <ChatMessageBubble
              key={`coord-streaming-${b.replyId}`}
              senderId={group?.coordinator_id ?? 'coordinator'}
              senderName="群主(协调者)"
              avatar={
                <ChatAvatar id={group?.coordinator_id ?? 'coordinator'} agents={agents} />
              }
              content={b.content}
              timestamp={new Date().toISOString()}
              isStreaming={stats?.phase !== 'done'}
              statusLine={
                <>{`${elapsedStr} · ↓ ${tokens} tokens · ${phaseLabel}`}</>
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
            isFailed={b.isFailed}
          />
        ))}
        <div ref={chatEndRef} />
      </div>

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

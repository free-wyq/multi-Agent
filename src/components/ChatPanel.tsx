import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Button, Empty, Input, Spin, Tooltip, Typography, Upload, message } from 'antd'
import type { UploadProps } from 'antd'
import type { ComponentRef } from 'react'
import { CompressOutlined, PaperClipOutlined, RobotOutlined, SendOutlined, SettingOutlined, VerticalAlignBottomOutlined } from '@ant-design/icons'
import {
  messageApi,
  groupApi,
  type AgentDefinition,
  type Conversation,
  type Group,
  type GroupMember,
  type Message,
} from '../services/api'
import { useBusEventContext } from '../contexts/BusEventContext'
import { useSettings } from '../contexts/SettingsContext'
import { useTts } from '../hooks/useTts'
import { useBubbleRetire } from '../hooks/useBubbleRetire'
import {
  getSlashCommand,
  matchSlashCommands,
  parseSlashCommand,
  type SlashCommandContext,
} from '../lib/slashCommands'
import PlanConfirmCard from './PlanConfirmCard'
import StopTaskButton from './StopTaskButton'
import SlashAutocomplete from './SlashAutocomplete'
import PersistentBubbleList, { getMemberDisplayName } from './PersistentBubbleList'
import StreamingBubbleList from './StreamingBubbleList'
import './ChatPanel.css'

const { Text } = Typography

/**
 * 可渲染成聊天气泡的 BusEvent/Message type 白名单。
 *
 * 为什么需要白名单：WS 事件流把所有 content truthy 的事件都灌进 logs，但只有
 * 「消息语义」的事件（agent_reply 智能体回复 / user_input 用户消息 / slash_card
 * slash 命令卡片）才该出现在聊天气泡流里。其余是 trace 事件——task_log 任务日志 /
 * coordinator_think 协调者思考 / task_token 流式 token / task_think 工作思考 /
 * task_tool 工具调用 / agent_status 状态迁移 / coordinator_plan 计划——它们有自己的
 * 展示区（LogPanel 任务日志 / LeaderPanel 思考链 / 流式气泡 / 气泡内折叠块 / 计划卡片），
 * 不该作为独立气泡混进消息气泡流。
 *
 * 特别是 coordinator_think：它携带协调者完整回复文本，若也桥接成气泡，会与随后
 * node_chat 持久化的 agent_reply 消息（id 不同，去重命中不了）同时渲染 → 「协调者
 * 回复两次」缺陷。白名单从源头排除这类重复。
 *
 * task_log 不在白名单（vh64 chat-bubble redundancy fix）：
 *   - registry._publish_log 发的执行 bookkeeping 行（▶ 开始执行任务 / 📦 产物已记录 /
 *     ⏹ 任务已取消 / ⏱ 任务超时降级 / ❌ 找不到智能体定义）和 agent_loop on_log 的
 *     ReAct loop 标记（[开始] 智能体 ... / [停止] / [错误] / [完成]）是执行过程 trace，
 *     与「该 turn 的最终答案」不重复——它们已在 LogPanel（按 taskId 过滤）+ WorkerTrace
 *     显示，不该再独立成气泡（用户原话：单聊 execute turn 一个气泡全收，不要 6 个噪音气泡）。
 *   - 收尾 announce（任务完成🎉 / 执行出错了 / ⏹ 任务已停止 / ⏱ 超时）仍走
 *     ``persist_agent_reply``（agent_reply 类型），不受 task_log 出白名单影响——
 *     成功路径的「任务完成 🎉」收尾仍是一个 agent_reply 气泡，承载该 turn 的最终答案。
 *   - 群聊无回归：群聊 execute 路径同样的 task_log 行原本就只进 LogPanel/WorkerTrace
 *     （GroupPage 不读 chatMessages 桥接 effect），单聊只是对齐群聊行为。
 *   - 群聊 task_complete 收尾事件（events 流 kind='complete'/'failed'）驱动 finalizedBubbles
 *     定稿气泡退场，与 task_log 是否成气泡无关——退场判定按 task_id 精确匹配
 *     （repliedTaskIds.has(e.taskId)），task_log 不参与。
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
  'slash_card',
])

/** antd Input.TextArea 的 ref 类型（antd v6 未从顶层导出 TextAreaRef，用 ComponentRef 推导）。 */
type TextAreaRef = ComponentRef<typeof Input.TextArea>

// [任务9b] ST-04 定稿气泡数据 interface FinalizedBubble 已随 StreamingBubbleList 抽出
// （src/components/StreamingBubbleList.tsx 导出 FinalizedBubble + extractFinalizedArtifacts）。
// ChatPanel 不再直接构造定稿气泡，故本 interface 与 ArtifactFile import 一并移除。

interface ChatPanelProps {
  /** 当前会话的群组（null/未选群时展示占位）。
   *  Path C：单聊传 ConversationEntity（字段形状兼容——coordinator_id 镜像 agent_id，
   *  ChatPanel 读 group.coordinator_id 的代码零改）。 */
  group: Group | Conversation | null
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

  // 已退场定稿气泡的 task_id 集合——「reply 已落地、定稿气泡已退场」的真源（B22/B23 + 缺陷5 修复）。
  // [任务9b] 抽出 useBubbleRetire hook 持有（src/hooks/useBubbleRetire.ts），ChatPanel 消费
  // repliedTaskIds（透传给 StreamingBubbleList 的 finalizedBubbles memo 判退场）+ markReplied
  // （logs 桥接 effect 在 agent_reply 落地时回填）+ reset（切群 effect 清空）。
  //
  // finalizedBubbles 原 deps=[events, streaming, chatMessages, agentStatuses]——chatMessages
  // 每条新消息（含 task_log）都换新引用 → finalizedBubbles 全量重算。B23 把退场判定从「每次
  // 重算 chatMessages.some」改为「reply 落地 effect 增量回填 repliedTaskIds 集合」：
  //   - repliedTaskIds: Set<string> 记录已退场 task_id（announce reply 已落地的 task）。
  //   - logs 桥接 effect 每次新 agent_reply 落地后，若其 task_id 非空就把它加入集合
  //     （标记该 task 的定稿气泡可退场）。
  //   - finalizedBubbles 用 repliedTaskIds.has(e.taskId) 判退场（O(1) 集合查），不再
  //     chatMessages.some 扫描。
  // 为何用 state 不用 ref（缺陷5 修复）：B23 原用 ref + 把 chatMessages 移出 memo deps 避高频
  // 重算，但埋了时序 bug——emit 顺序是 emit_task_completed（registry:421）先于 _reply announce
  // （registry:460），agent_reply 落地时 logs effect 回填 ref（commit 后），而 finalizedBubbles
  // memo 在 render 期算；本批 memo 算时 ref 仍空 → 渲染 finalized bubble。随后 setChatMessages
  // 触发的重渲染里 memo deps [events/streaming/agentStatuses] 均未变 → memo 返回缓存 →
  // finalized bubble 卡住不退场（即「一轮多泡」缺陷的 finalized 那条）。改用 state 并把
  // repliedTaskIds 纳入 memo deps：reply 落地 effect markReplied → 触发重渲染 → memo
  // 重算 → repliedTaskIds.has 命中 → finalized bubble 退场。perf 不回归：repliedTaskIds 仅在
  // agent_reply 落地时变化（每 task 一次，低频），不像 chatMessages 每 token/log 换引用——
  // 故纳入 deps 不会重拾 B23 想避开的高频重算。切群 effect reset 清空集合（新群定稿状态独立）。
  // reload-safe：重连/切群回灌历史 agent_reply 经 logs effect 重扫 markReplied 回填（历史
  // agent_reply 带 task_id 同样入集合，prev.has 去重），故 reload 后退场状态与 live 一致。
  const { repliedTaskIds, markReplied, reset: resetRepliedTaskIds } = useBubbleRetire()
  // [需求2-后端] regenerate 正在重跑的 reply_id 集合——按钮 loading 态真源。
  // 点「重新生成」时把该回复的 reply_id 加入集合（按钮转 loading），新 agent_reply
  // 经 WS bus-event 落地后被 logs 桥接进 chatMessages——此时新一轮已开始，按钮可恢复。
  // 用 reply_id 作 key 而非 message.id：新回复是不同 message.id 但同一原回复的 regenerate
  // 源，按 reply_id 标 loading 才能精确定位到被点的那个气泡的按钮。
  // 简化语义：集合非空 = 该 reply_id 对应的「重新生成」按钮转 loading；新回复落地即清
  // （新一轮 turn 已接管，旧回复的按钮无需继续 loading——见 handleRegenerate finally）。
  const [regeneratingReplyIds, setRegeneratingReplyIds] = useState<Set<string>>(new Set())

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

  // ── @收束 一次性开关（converge-turn-design）──────────────────────
  // Option B 删停关键词后去中心化「人工停止」入口空缺的柔性收口。点亮 → 下条消息以收束
  // 回合发（@某 agent → 该 agent 回一句即 END 不 handoff，回合自然收敛）→ 发完自动灭。
  // 开关亮但消息无 @ → handleSendMessage 前端拦截 toast「收束必须选择 @ 收口对象」不发。
  // 仅去中心化路径：中心化收口走 plan 全 done / summarize，不需要 @收束。开关是 UI 控件，
  // 不解析消息内容——不重蹈停关键词的语义歧义。纯加性，converge=false 时一切照旧。
  // 切群时复位（收束是当前群一次性意图，跨群不应残留）。
  const [convergeActive, setConvergeActive] = useState(false)
  useEffect(() => {
    setConvergeActive(false)
  }, [chatGroupId])

  const mentionCandidates = members.filter((m) =>
    getMemberDisplayName(m).toLowerCase().includes(mentionQuery.toLowerCase()),
  )

  // PL-11 + task-25：当前群组中「活跃」的智能体——停止按钮入口。
  // 去中心化回合（闲聊/@人/成语接龙）下，发言人不走驻留引擎的 executing 状态机，
  // 而是经 GroupRuntime 群图跑 handoff，活跃信号体现在流式缓冲：
  //   - worker 流式：streaming[task_id] 有内容（但去中心化回合 worker 无 task_id，靠下条）
  //   - 协调者流式：coordStreaming[reply_id] 有内容（协调者发言时按 reply_id 累积）
  // 故活跃判定放宽为：status==='executing'（驻留引擎路径，有 current_task_id）
  //   OR coordStreaming 当前群组有任一 reply_id 正在累积（去中心化协调者/worker发言）。
  // 任一命中即渲染停止按钮（调 groupApi.stopTurn 硬停整个回合，见 task-26）。
  // 注意：coordStreaming 是全局 Map（跨群组 reply_id），需按当前群组发言者过滤——
  // reply_id 编码含 group_id（coordinator_reply:{group_id}:{ts}，见后端 emit_coordinator_token），
  // 但为稳妥这里只判「本群有活跃流式」：取本群协调者 id 对照 coordStreaming key 前缀。
  const coordinatorId = group?.coordinator_id
  const hasActiveStream =
    !!chatGroupId &&
    (Object.keys(coordStreaming).length > 0 ||
      Object.values(agentStatuses).some(
        (a) => a.status === 'executing' && a.current_task_id,
      ))
  const executingAgent = chatGroupId
    ? Object.values(agentStatuses).find(
        (a) => a.status === 'executing' && a.current_task_id,
      ) ??
      // 去中心化回合：无 executing agent 但有活跃流式 → 取协调者作为停止按钮的代理发言人
      // （stopTurn 是回合级停止，不依赖具体 task_id，用协调者 id 作展示锚点即可）。
      (hasActiveStream && coordinatorId
        ? {
            id: coordinatorId,
            name: agentStatuses[coordinatorId]?.name || '协调者',
            role: agentStatuses[coordinatorId]?.role || 'coordinator',
            status: 'executing' as const,
            current_task_id: null,
          }
        : undefined)
    : undefined

  // 计划存在即展示（Bug B：实时可视化）。去掉「必须含 pending」门槛——「直接干」模式
  // auto_confirm 跳过 interrupt，pending 几乎瞬间翻 dispatched，原门槛致卡片永不显示。
  // 现在只要 plan.length>0 就显示：确认模式（有 pending，带确认按钮）或只读进度模式
  // （无 pending，dispatched/completed 混合实时翻色）。node_summarize_group emit [] →
  // plan 清空 → 卡片自动隐藏。
  const showPlanCard = !!chatGroupId && !!plan && plan.length > 0

  // [任务9b] 流式/协调者流式/定稿三条气泡路径的 memo 与渲染已抽到 StreamingBubbleList
  // （src/components/StreamingBubbleList.tsx）——streamingBubbles / coordinatorStreamingBubbles
  // / finalizedBubbles / toolEventsByTask / thinkEventsByTask 全在那儿自管（纯展示 + memo）。
  // 退场 state (repliedTaskIds) 由 useBubbleRetire hook 持有，markReplied/reset 在本组件
  // logs 桥接 effect / 切群 effect 调用，repliedTaskIds 透传给 <StreamingBubbleList> 只读。
  // 原三段 memo + 兜底退场注释（B22/B23/缺陷5/ST-02~06）逐字保留在 StreamingBubbleList.tsx。

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
          // Path C: conversation_id（后端 emit 双 key，conversation_id 主 + group_id 兼容）
          conversation_id: chatGroupId || '',
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
      // 缺陷5 修复：reply 落地回填退场集合——agent_reply 带 task_id 即该 task 的持久化回复已落地，
      // 标记其定稿气泡可退场（finalizedBubbles 据 repliedTaskIds.has(task_id) 过滤）。只记
      // agent_reply（type='agent_reply' 是收尾 announce；task_log/user_input/slash_card 无 task_id
      // 或非回复语义）。task_id 为空（chat 路径）不入集合——其退场靠兜底时间戳（finalizedBubbles
      // 仍保留 sender+时间戳兜底分支，但 chat 路径无 complete/failed 事件实际不进循环，故兜底
      // 不命中）。
      // B23 原用 ref.add（不触发渲染），缺陷5 改用 markReplied（state 更新触发重渲染 →
      // finalizedBubbles memo 重算 → 退场命中）。这是修「emit_task_completed 先于 _reply
      // announce → ref 在 commit 后回填但 memo 在 render 期已算完 → finalized 卡住不退场」
      // 时序 bug 的关键。[任务9b] 抽出 useBubbleRetire hook，markReplied 内部函数式更新 + prev.has 去重，
      // 避免重复 add 触发无谓重渲染。
      if (log.type === 'agent_reply' && log.taskId) {
        markReplied(log.taskId)
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
      // 流式期每个 token（~50ms）都触发本 effect：用 'auto' 瞬切而非 'smooth'——
      // smooth 动画未跑完就被下一帧改写目标 scrollTop，浏览器反复重算布局 → 视觉抖动 + 主线程占用；
      // 流式结束高度突变时（reasoning 收起 / 气泡退场换 key）smooth 追高度会"抖一下"，瞬切杜绝。
      el.scrollTo({ top: el.scrollHeight, behavior: 'auto' })
    })
    return () => cancelAnimationFrame(raf)
    // [任务9b] 原 deps=[chatMessages, streamingBubbles, coordinatorStreamingBubbles, finalizedBubbles]
    // （4 个派生数组）。9b 把三段气泡 memo 抽到 StreamingBubbleList 后，本组件不再持有派生数组——
    // 改依赖派生它们的上游 context 信号：streaming/coordStreaming/coordReasoning/coordStats（流式
    // 每 token 变，驱动流式气泡内容）+ events/repliedTaskIds（complete/failed + 退场，驱动定稿
    // 气泡）+ agentStatuses（executing 成员，驱动 streamingBubbles 成员）。任一变化即 StreamingBubbleList
    // 重渲出新气泡 → 本 rAF 滚到底。频率与原派生数组等价（streaming 每 token 变 = 原 streamingBubbles
    // 每 token 换引用）。stickToBottomRef 守卫：用户上滑时不强拽。
  }, [chatMessages, streaming, coordStreaming, coordReasoning, coordStats, events, repliedTaskIds, agentStatuses])

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
    // 缺陷5 修复：重置退场集合——新群的已退场 task_id 与旧群无关，避免旧群退场状态泄漏到新群
    // （旧群某 task_id 恰好与新群 task_id 撞——虽 tid 是 task_+uuid 概率极低，但语义独立
    // 应清）。新群历史 agent_reply 经 logs 桥接 effect 重扫 markReplied 回填（历史
    // agent_reply 带 task_id 同样入集合），故 reload 后退场状态从历史重建，与 live 一致。
    // [任务9b] useBubbleRetire.reset 清空集合。
    resetRepliedTaskIds()
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

  const handleSendMessage = async (overrideContent?: string) => {
    // overrideContent：文件上传（任务8）把读出的文件文本拼进 chatInput 后直接调本函数发送，
    // 不依赖异步 setState 生效——传显式 content 走与手输完全一致的路径（@收束拦截/打断/
    // optimistic/失败回填），零特例分支。手输回车/点发送时为 undefined → 走 chatInput 闭包值。
    const raw = overrideContent ?? chatInput
    if (!raw.trim() || !chatGroupId || sending) return
    // @收束 前端拦截（converge-turn-design）：开关亮但消息无 @ → 不发，toast 提示。
    // 收束必须 @ 收口对象（@某成员后再开收束开关）。后端也会 400 兜底，但前端先拦避免无效请求。
    if (convergeActive && !/@\S/.test(raw)) {
      message.warning('收束必须选择 @ 收口对象（先 @ 某成员再开收束开关）')
      return
    }
    setSending(true)
    const content = raw.trim()
    const wasConverge = convergeActive
    setChatInput('')
    setConvergeActive(false)
    setMentionOpen(false)
    setSlashOpen(false)
    // 发送即跟到底：用户主动发消息必然想看回复，强制贴底，回复/流式自动滚入视野。
    stickToBottomRef.current = true

    // SH-08 busy_input_mode：若当前群组有活跃发言人（executing 或去中心化流式），
    // 回车发送前先 interrupt 当前回合（task-26 起改调 groupApi.stopTurn——群图整回合
    // 硬停，去中心化闲聊回合无 task_id，旧 taskApi.stop 打不中）。语义：用户在智能体
    // 忙碌时回车输入 = 想打断它说新话，而非排队。
    // stop 是 best-effort——失败不阻断发送（可能回合恰好刚结束 / 后端 no-op 200），
    // 仅 toast 告知打断结果。stopTurn 成功后 stop-turn 端点 emit agent_status(idle)，
    // useBusEvent 自动刷新 agentStatuses。
    if (executingAgent && chatGroupId) {
      try {
        const resp = await groupApi.stopTurn(chatGroupId)
        message.info(resp.message || `已打断 ${executingAgent.name} 的回合`)
      } catch (e) {
        // 打断失败不阻断发送——用户消息优先级高于打断结果
        message.warning(`打断失败（仍发送消息）：${e instanceof Error ? e.message : String(e)}`)
      }
    }

    const tempId = `temp-${Date.now()}`
    const optimisticMsg: Message = {
      id: tempId,
      // Path C: conversation_id（后端 emit 双 key 兼容）
      conversation_id: chatGroupId,
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
        // Path C: conversation_id（后端 MessageCreatePayload 字段已改名）
        conversation_id: chatGroupId,
        sender_id: 'user',
        receiver_id: 'broadcast',
        type: 'user_input',
        content,
        // @收束（converge-turn-design）：wasConverge=发送前开关是否亮。透传到后端
        // invoke_turn(converge=True) → make_agent_node 强制 next_speaker=None 回一句即 END。
        converge: wasConverge,
      })
      setChatMessages((prev) => {
        const alreadyExists = prev.some((m) => m.id === sent.id)
        if (alreadyExists) return prev.filter((m) => m.id !== tempId)
        return prev.map((m) => (m.id === tempId ? sent : m))
      })
    } catch {
      setChatMessages((prev) => prev.filter((m) => m.id !== tempId))
      setChatInput(content)
      // 发送失败时恢复收束开关（一次性开关只在发送成功后灭——失败重来仍可收束）。
      if (wasConverge) setConvergeActive(true)
      message.error('发送失败')
    } finally {
      setSending(false)
    }
  }

  // [需求2-后端] 按 reply_id 重跑回复——ChatMessageBubble footer 操作栏「重新生成」按钮的回调。
  // 后端 POST /api/messages/regenerate?replyId=... 回查 chat 路径落盘的 agent_reply
  // （data.reply_id），取其前最近一条 user_input 的内容作原 prompt，合成新 user_input
  // 落盘 + 走与 send 相同的路由分流，新回复经现有 WS bus-event 实时到达（与正常发送一致）。
  // 历史回复不删——新回复作为新气泡追加，旧回复保留（regenerate 是追加不是覆盖）。
  // replyId 来自该回复持久化 message.data.reply_id（chat 路径 worker/coordinator 回复落盘时
  // 由 persist_agent_reply 透传；execute 路径模板 announce 无 reply_id → 不该走到这里）。
  const handleRegenerate = useCallback(
    async (replyId: string) => {
      if (!replyId) return
      // 按钮 loading 态：把该 reply_id 加入集合，对应气泡的「重新生成」按钮转 loading。
      setRegeneratingReplyIds((prev) => new Set(prev).add(replyId))
      try {
        await messageApi.regenerate(replyId)
        // 成功仅提示「已重新生成」——新回复靠 WS bus-event 实时落地（与 send 同款，
        // 不在此处手动 append）。loading 不在此处清——新一轮 turn 已被后端路由层接管，
        // 新回复落地期间按钮保持 loading 更直观（用户能看到「正在重跑」）。
        message.success('已重新生成，新回复即将到达')
      } catch (e) {
        // 后端 404（reply_id 无对应回复 / 模板公告 / 已清会话）或 409（前无 user_input）
        // 都会走这里——message.error 告知用户重跑失败，按钮恢复可点。
        message.error(`重新生成失败：${e instanceof Error ? e.message : String(e)}`)
      } finally {
        // 清 loading：无论成败，请求本身已返回（fire-and-forget 踢一轮 turn），按钮恢复。
        // 新回复的流式/落地由 useBusEvent 现有桥接处理，不依赖此 loading 状态。
        setRegeneratingReplyIds((prev) => {
          const next = new Set(prev)
          next.delete(replyId)
          return next
        })
      }
    },
    [],
  )

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
            // Path C: conversation_id（后端 emit 双 key 兼容）
            conversation_id: chatGroupId || '',
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

  // 需求2-前端：追问引导 chip 点击 → 填入输入框（不自动发送，用户可改后发）。
  // 与 insertMention/selectSlashCommand 同款——改 chatInput + 聚焦输入框光标到末尾。
  // 不自动发送：用户可能想合并多个 chip / 修改后再发，点即发会跳过用户确认。
  const handleFollowUpClick = useCallback((text: string) => {
    setChatInput(text)
    setTimeout(() => {
      const textarea = inputRef.current?.resizableTextArea?.textArea
      if (textarea) textarea.setSelectionRange(text.length, text.length)
      inputRef.current?.focus()
    }, 0)
  }, [])

  // 任务8：对话框文件上传（方案 A 零后端端点）——antd Upload beforeUpload 读 File.text()
  // 把文件文本拼进 chatInput 直接发送。不传文件到后端（零上传端点 / 零 multipart / 零存储），
  // 文件内容作为普通消息文本走现有 messageApi.send 路径，与手输完全一致。
  //
  // 决策点自决（铁律 #2/#5）：
  //  - accept 仅 .md/.txt：纯文本/markdown，读出即 UTF-8 文本可直拼；二进制（docx/pdf/图片）
  //    不在范围（需后端解析，超本任务边界）。前端 accept 仅是 OS 文件选择器过滤提示，
  //    beforeUpload 仍要按扩展名二次校验（用户可拖入任意文件绕过选择器）。
  //  - <100KB 校验：防超大文本撑爆单条消息（后端 content 是 TEXT 列无硬上限，但 LLM context
  //    有窗口，超大文本会吃满 token 且前端 TextArea 渲染卡顿）。100KB ≈ 2.5万汉字/5万英文词，
  //    足够覆盖常规 md/txt 文档。
  //  - 上传即发送（非填入输入框等用户改）：与「追问 chip 不自动发送」不同——文件上传是用户明确
  //    的「把这个文件内容发给智能体」意图，点即发符合预期；若填入输入框反而要多一步点发送，
  //    与「上传」语义不符。文件名作标题行「📄 filename.md\n」前缀拼进 content，让智能体知道
  //    来源文件。纯文本内容直接拼，markdown 渲染由 ChatMessageBubble contentRender 接管。
  //  - 多文件逐个发：Upload multiple 不开（一次一个，避免并发发送乱序）；beforeUpload 返
  //    Upload.LIST_IGNORE 不进 Upload 的内置文件列表（本组件不维护文件列表 UI）。
  const MAX_FILE_SIZE = 100 * 1024 // 100KB
  const ACCEPTED_FILE_EXT = ['.md', '.txt']
  const beforeUploadFile: UploadProps['beforeUpload'] = (file) => {
    // 扩展名二次校验（OS 选择器可被绕过，这里以实际 file.name 判定）
    const name = file.name.toLowerCase()
    const isAccepted = ACCEPTED_FILE_EXT.some((ext) => name.endsWith(ext))
    if (!isAccepted) {
      message.error(`不支持的文件类型：仅支持 ${ACCEPTED_FILE_EXT.join('/')}（当前：${file.name}）`)
      return Upload.LIST_IGNORE
    }
    // 大小校验
    if (file.size > MAX_FILE_SIZE) {
      message.error(`文件过大：${file.name}（${(file.size / 1024).toFixed(1)}KB），请控制在 100KB 以内`)
      return Upload.LIST_IGNORE
    }
    // 异步读文本——beforeUpload 支持返回 Promise<RcFile>，但这里不传文件给后端，
    // 读出文本后直接拼进 chatInput 发送，返回 LIST_IGNORE 让 Upload 不进文件列表。
    file
      .text()
      .then((text) => {
        // 拼装：文件名标题行 + 原文。若已有输入（用户在输入框写了话再上传），用空行分隔
        // 已有内容与文件内容，避免挤在一行。文件名前缀让智能体感知来源。
        const fileLabel = `📄 ${file.name}\n`
        const existing = chatInput.trim()
        const composed = existing ? `${existing}\n\n${fileLabel}${text}` : `${fileLabel}${text}`
        // 直接发送（不 setChatInput + 手动调 send——异步 setState 不可靠，传显式 content
        // 走 handleSendMessage 同款路径，含 @收束拦截/打断/optimistic/失败回填）。
        void handleSendMessage(composed)
      })
      .catch((e) => {
        message.error(`读取文件失败：${e instanceof Error ? e.message : String(e)}`)
      })
    return Upload.LIST_IGNORE
  }


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
          <PersistentBubbleList
            chatMessages={chatMessages}
            agents={agents}
            members={members}
            ttsEnabled={tts.enabled}
            regeneratingReplyIds={regeneratingReplyIds}
            onRegenerate={handleRegenerate}
            onFollowUpClick={handleFollowUpClick}
            lastDateRef={lastDateRef}
          />
        )}
        <StreamingBubbleList
          chatGroupId={chatGroupId}
          group={group}
          agents={agents}
          agentStatuses={agentStatuses}
          streaming={streaming}
          coordStreaming={coordStreaming}
          coordReasoning={coordReasoning}
          coordStats={coordStats}
          events={events}
          chatMessages={chatMessages}
          repliedTaskIds={repliedTaskIds}
          ttsEnabled={tts.enabled}
        />
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
                    background: idx === mentionIndex ? '#FFF3ED' : '#fff',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                  }}
                >
                  <RobotOutlined style={{ color: '#F26522' }} />
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
              placeholder={
                convergeActive
                  ? '收束模式：@ 某成员让其回一句即收束（回复后不再 handoff）。再点「收束」取消'
                  : '输入消息... @ 点名成员，/ 触发命令，Enter 发送，Shift+Enter 换行（智能体忙碌时回车会先打断当前任务）'
              }
              disabled={sending}
              autoSize={{ minRows: 1, maxRows: 6 }}
              style={{ flex: 1, resize: 'none' }}
            />
            {/* @收束 一次性开关（converge-turn-design）：点亮后下条消息以收束回合发，
                @某 agent → 回一句即 END 不 handoff。发完自动灭。仅去中心化柔性收口。 */}
            <Tooltip
              title={
                convergeActive
                  ? '收束已开启：下条消息 @ 某成员 → 其回一句即收束，不再 handoff（再点取消）'
                  : '收束：@ 某成员让其回一句即收束（回复后不再 handoff），用于柔性收口'
              }
            >
              <Button
                type={convergeActive ? 'primary' : 'default'}
                ghost={convergeActive}
                icon={<CompressOutlined />}
                onClick={() => setConvergeActive((v) => !v)}
                disabled={sending}
                aria-pressed={convergeActive}
              >
                收束
              </Button>
            </Tooltip>
            {/* 任务8：文件上传（方案 A 零后端端点）——antd Upload beforeUpload 读 File.text()
                拼进 chatInput 直接发送。accept=".md,.txt"+<100KB 校验，非法 toast。
                showUploadList=false 不显示文件列表（上传即发送，无文件列表 UI）。
                disabled=sending 与收束/发送按钮一致——发送中禁用避免并发。 */}
            <Upload
              beforeUpload={beforeUploadFile}
              accept=".md,.txt"
              showUploadList={false}
              disabled={sending}
            >
              <Tooltip title="上传 .md/.txt 文件作为消息发送（<100KB，方案A 零后端端点）">
                <Button icon={<PaperClipOutlined />} disabled={sending} aria-label="上传文件" />
              </Tooltip>
            </Upload>
            <Button
              type="primary"
              icon={<SendOutlined />}
              onClick={() => void handleSendMessage()}
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

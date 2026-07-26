import { useEffect, useMemo, useState } from 'react'
import { Bubble, ThoughtChain } from '@ant-design/x'
import { Collapse, Tag, Tooltip, Button, message } from 'antd'
import { CaretRightOutlined, ToolOutlined, BulbOutlined, DownloadOutlined } from '@ant-design/icons'
import type { TraceEvent } from '../services/api'
import { groupApi } from '../services/api'
import { fileIconFor, saveBlob, humanSize } from '../lib/fileIcon'
import './ChatMessageBubble.css'

/** ST-06（task 21 数据 / task 22 渲染）：worker 任务产物文件条目。
 *  形状对齐后端 scan_workspace_artifacts（workspace.py）manifest 的 files[] 元素：
 *  name（basename）、path（工作区相对 POSIX 路径，可能含子目录如 `login-api/index.js`）、
 *  size（字节）、modified_at（ISO）。与 TaskPage.ArtifactFile 同构——task 22 下载卡复用
 *  TaskPage 按扩展名图标 + groupApi.downloadFileUrl（GET /api/groups/{id}/files/{name}）。
 *  定义在此（prop 的消费方）并导出，ChatPanel.finalizedBubbles 导入复用——单一类型真源，
 *  避免 ChatPanel/ChatMessageBubble 各定义一份漂移。 */
export interface ArtifactFile {
  name: string
  path: string
  size: number
  modified_at: string
}

interface ChatMessageBubbleProps {
  /** 消息发送者 id（'user' | 'coordinator' | agent_id | 'system'）。决定左右对齐 + 头像色。 */
  senderId: string
  /** 发送者显示名（由父组件解析，如「群主(协调者)」「小后端」「用户」）。 */
  senderName: string
  /** 头像节点（由父组件 ChatAvatar 渲染，保持头像逻辑/视觉与现有 chat 一致）。 */
  avatar: React.ReactNode
  /** 气泡正文内容。
   *  - 已定稿消息：完整文本（持久化 message.content 或 task_complete 后的定稿文本）。
   *  - 流式生成中：当前累积的文本（来自 streaming[task_id] 增量拼接），isStreaming=true 时尾部追加闪烁光标。
   */
  content: string
  /** 推理模型的内部思维链（reasoning_content 流式拼接，可见回复 content 之前流出）。
   *  仅协调者流式气泡可能携带（推理模型 + 协调者走 LLM 直调流式）。非推理模型 / 已定稿气泡
   *  不传（undefined → 不渲染折叠区）。用户可点击展开/收起——默认收起，想看模型「怎么想的」
   *  再展开，不干扰正常阅读回复正文。 */
  reasoning?: string
  /** 推理 token 数（流式期 coordStats[reply_id].reasoning_tokens，后端 ~200ms 节流推送的实时估值）。
   *  折叠区标题用它显示「思考过程（N tokens）」——与状态行「↓ N tokens」同单位，不用字符数。
   *  首个 stats 事件到达前（前 ~200ms）可能为 undefined，此时用 reasoning.length//3 临时估算
   *  （与后端 live_reasoning_tokens 同启发式），stats 一到即切回真实值。 */
  reasoningTokens?: number
  /** ISO 时间戳（消息或事件时间），渲染为气泡下时间。 */
  timestamp: string
  /** 该消息关联的任务的工具调用事件（kind==='tool'，已由父组件按 agent+task 过滤）。
   *  空数组则不渲染工具摘要区。每条 task_tool 事件 data 含 {phase:'start'|'end', name, args?, output?}。 */
  toolEvents?: TraceEvent[]
  /** 该消息关联的任务的深度思考事件（kind==='think'，已由父组件按 task_id 过滤）。
   *  即 worker 在 ReAct 循环里 on_chat_model_end 流出的中间推理片段（registry.on_log
   *  think→emit_task_think，data {phase:'thinking'|'final'}）。空数组则不渲染思考折叠区。
   *  复用协调者 reasoning 折叠区视觉（task 19 渲染）——worker think 是 ReAct 中间步，与该
   *  task 最终回复不重复，故可安全作为气泡内折叠块（区别于 coordinator_think 即回复正文）。 */
  thinkEvents?: TraceEvent[]
  /** ST-06（task 21）：worker 任务产物文件列表（task_complete 事件 data.artifact.files[]）。
   *  空数组则不渲染下载卡。仅 finalizedBubbles（定稿气泡，task 21 从 task_complete 事件
   *  data.artifact 提取）传入——失败/取消/超时路径 artifact key 缺省（bus.py emit_task_completed
   *  仅成功路径透传 manifest），故失败气泡自然无下载卡（失败任务不留产物，语义正确）。
   *  task 22 渲染按扩展名图标（fileIconFor，与 TaskPage 交付物卡同款）+ 下载按钮
   *  （groupApi.downloadFileUrl → GET /api/groups/{id}/files/{name}，复用 PL-12 saveBlob）。 */
  artifactFiles?: ArtifactFile[]
  /** ST-06（task 22）：下载产物所需 group_id（GET /api/groups/{id}/files/{name} 的路径段）。
   *  由父组件传当前会话 groupId。仅 finalizedBubbles 传（产物只在定稿气泡出现）；
   *  未传时下载按钮禁用（无 group 无法拼 URL，禁用 + tooltip 提示，不报错）。
   *  从父组件传而非气泡内自己读 context——保持 ChatMessageBubble「纯展示」设计约定
   *  （不订阅 context/不读 store，数据全由父注入）。 */
  groupId?: string
  /** 是否正在流式生成（PL-08 逐字 token）。true → 气泡加 streaming 描边 + 正文尾追加闪烁光标。 */
  isStreaming?: boolean
  /** ST-04：是否失败定稿（task_failed 收尾）。true → 气泡加红描边标记失败语义。 */
  isFailed?: boolean
  /** 是否用户自己发的消息（决定左右对齐 + self/other 气泡样式 + @mention 是否高亮）。 */
  isUser?: boolean
  /** 气泡正文的自定义渲染（用于 @mention 高亮等富文本）。未提供时直接渲染 content 纯文本。 */
  renderContent?: (content: string) => React.ReactNode
  /** 状态行（Claude-Code 风格 "Ns · ↓ N tokens · thinking"）。
   *  协调者流式气泡用：渲染在气泡下方时间戳旁，实时刷新耗时/token/阶段。
   *  普通气泡不传（undefined → 不渲染），保持向后兼容。 */
  statusLine?: React.ReactNode
  /** 气泡右上角的操作按钮组（复制/朗读等）。父组件传 .bubble-action-group 内的按钮，
   *  hover 时显隐。绝对定位锚点由 .chat-bubble-wrap 提供（position:relative）。不传则不渲染。 */
  actionGroup?: React.ReactNode
}

/** 单条 task_tool 事件 → 摘要行数据。 */
interface ToolRow {
  key: string
  /** 工具名（run_command / write_file / ...），来自 data.name。 */
  name: string
  /** start 阶段的参数摘要（data.args，已 stringify + 截断），end 阶段无。 */
  argsPreview: string
  /** 该工具调用的原始 payload（start→args / end→output），展开后展示。 */
  payload: unknown
  /** 是否 end 阶段（返回结果）。start=调用中，end=已返回。 */
  isEnd: boolean
  /** end 阶段配对到同名 start 后算出的耗时（ms）；start 或未配对 end 无此值。 */
  elapsedMs?: number
  /** 时间戳（用于排序 + 展示）。 */
  timestamp: number
}

/** 任意值 → 字符串预览（截断长内容，避免摘要行撑爆气泡）。null/undefined → 空串。 */
function toPreview(v: unknown, max = 80): string {
  if (v == null) return ''
  const s = typeof v === 'string' ? v : JSON.stringify(v)
  if (s.length <= max) return s
  return s.slice(0, max) + '…'
}

/** 毫秒 → 人类可读耗时：<1s 显示 ms，否则保留 1 位小数秒。 */
function formatElapsed(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

/** 工具名 → Tag 颜色（与 WorkerTrace 工具卡视觉呼应：start 绿 / end 灰）。 */
function toolTagColor(isEnd: boolean): string {
  return isEnd ? 'default' : 'green'
}

/**
 * ST-01 ChatMessageBubble：聊天消息气泡，支持流式闪烁光标 + 工具调用摘要行 + 可折叠详情。
 *
 * 在 ChatPanel 现有「头像 + 气泡 + 时间」结构基础上，增强气泡内部：
 *
 *  1. **流式闪烁光标**（PL-08 接缝预留）：`isStreaming=true` 时气泡加淡蓝描边（.chat-bubble--streaming）
 *     + 正文尾部追加 `<span class="chat-streaming-cursor">`（1s step-end 闪烁），让用户感知「正在生成」。
 *     ST-02 会把 BusEventContext.streaming[task_id] 接入 content + isStreaming；ST-04 会在 task_complete
 *     时把流式气泡定稿为持久化消息（content 换成定稿文本、isStreaming=false）。本组件只负责渲染，
 *     不关心流式数据来源——纯展示接缝。
 *
 *  2. **工具调用摘要行**（M11 task_tool 事件接缝预留）：`toolEvents` 非空时，气泡顶部渲染工具摘要区
 *     （.chat-tool-block）——每条工具一行：🛠 工具名（monospace）+ 参数预览（截断 + title 全文）+
 *     阶段 Tag（调用中 绿 / 已返回 灰）。ST-03 会把 task_tool 事件按 agent+task 过滤后传入。
 *
 *  3. **可折叠详情**：每个工具摘要行可点击展开/收起，展开后显示该工具的 args（start 阶段）或 output
 *     （end 阶段）payload，深色 code 块（.chat-tool-payload，与 WorkerTrace payload 视觉一致）。
 *     多工具独立折叠（每行一个展开开关，互不影响）。
 *
 * 设计接缝：本组件是「纯展示组件」——不订阅 WS、不拉数据、不解析事件归属。所有数据（content/toolEvents/
 * isStreaming/senderName）由父组件传入。这样 ST-02/03/04 各任务可独立把数据源接入本组件的 props，
 * 而不必反复改本组件内部逻辑。组件只把「流式光标」「工具摘要行」「折叠详情」三件事渲染好。
 *
 * 头像/左右对齐/气泡底色沿用 ChatPanel.css 全局 .chat-bubble / .chat-bubble--self / --other（全局
 * class，import ChatPanel.css 即注册）。本组件只补专有样式（ChatMessageBubble.css）。
 */
export default function ChatMessageBubble({
  senderId,
  senderName,
  avatar,
  content,
  reasoning,
  reasoningTokens,
  timestamp,
  toolEvents = [],
  thinkEvents = [],
  artifactFiles = [],
  groupId,
  isStreaming = false,
  isFailed = false,
  isUser = false,
  renderContent,
  statusLine,
  actionGroup,
}: ChatMessageBubbleProps) {
  // 多工具独立折叠：key=事件 id 的 Set。点击行 toggle 该工具展开/收起。
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  // ST-06（task 22）：正在下载的产物文件 key（file.path）。下载期间该文件按钮 loading，
  // 其余文件按钮禁用——与 TaskPage 同款单下载串行（避免并发下载多文件挤占带宽/混淆进度）。
  const [downloading, setDownloading] = useState<string | null>(null)

  // ST-06（task 22）：下载单个产物文件——复用 PL-12 groupApi.downloadFile + saveBlob，
  // 与 TaskPage 交付物卡同入口同逻辑（GET /api/groups/{id}/files/{name} → Blob → a.download）。
  // 无 groupId 时禁用按钮（前置守卫，不报错）；失败 toast 提示。串行：downloading 非空时其余禁用。
  const handleArtifactDownload = async (file: ArtifactFile) => {
    if (!groupId) {
      message.warning('未选择群组，无法下载')
      return
    }
    const key = file.path || file.name
    setDownloading(key)
    try {
      const blob = await groupApi.downloadFile(groupId, file.path || file.name)
      saveBlob(blob, file.name)
      message.success(`已下载 ${file.name}`)
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e))
    } finally {
      setDownloading(null)
    }
  }

  // toolEvents → 按时间序的摘要行（每条 task_tool 一行）。
  // 先按时间序排，再按工具名 LIFO 配对 start/end 算耗时：end 弹出最近同名未配对 start，
  // 差值即该次调用耗时（嵌套同名调用按内层先闭合）；clamp 0 防时钟倒序产生负值。
  const toolRows = useMemo<ToolRow[]>(() => {
    const sorted = [...toolEvents].sort((a, b) => a.timestamp - b.timestamp)
    const pending: Record<string, number[]> = {}
    return sorted.map((e) => {
      const data = (e.data || {}) as Record<string, unknown>
      const isEnd = data['phase'] === 'end'
      const name = String(data['name'] || '(unknown)')
      const payload = isEnd ? data['output'] : data['args']
      let elapsedMs: number | undefined
      if (isEnd) {
        const stack = pending[name]
        if (stack && stack.length > 0) {
          elapsedMs = Math.max(0, e.timestamp - stack.pop()!)
        }
      } else {
        ;(pending[name] || (pending[name] = [])).push(e.timestamp)
      }
      return {
        key: e.id,
        name,
        argsPreview: isEnd ? '' : toPreview(data['args']),
        payload,
        isEnd,
        elapsedMs,
        timestamp: e.timestamp,
      }
    })
  }, [toolEvents])

  const toggleExpand = (key: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const hasTools = toolRows.length > 0
  const hasContent = content && content.length > 0
  const hasReasoning = !!(reasoning && reasoning.length > 0)
  // execute 轮判定：气泡携带工具调用或 ReAct 思考事件（hasTools || hasThinks）。
  // 用户要求「一个 turn 一个气泡展示完整过程」——execute 轮思考过程需整轮保持可见，
  // 不能像 chat 轮那样「思考结束让位给正文」自动收起。注意：brain reasoning 阶段
  // toolEvents/thinkEvents 尚未到达 → 此时 isExecuteTurn=false，走下面 chat 分支照常
  // 自动展开思考；brain 正文开始流时 hasContent=true → chat 分支会收起（旧 bug 现象：
  // execute 轮用户看着思考折起来）；当 toolEvents/thinkEvents 到达后 isExecuteTurn 翻 true
  // → effect 重跑（isExecuteTurn 在 deps 里）进入 execute 分支把 reasoning 重新展开并锁定，
  // 整轮不再自动收起。短暂的「先收起再展开」flicker 可接受（events 到达即恢复）。
  const isExecuteTurn = toolRows.length > 0 || thinkEvents.length > 0

  // 思考折叠区主动展开/收起态——按 turn 类型分叉：
  //
  //  · **execute 轮**（isExecuteTurn=true，有工具/思考事件）→ 整轮保持展开，让用户看完整过程
  //    （思考 + 执行步骤 + 最终答案）。brain reasoning 阶段 isExecuteTurn 尚为 false → 走 chat
  //    分支照常展开；正文开始流时 chat 分支会收起（旧 bug）；toolEvents/thinkEvents 到达后翻 true
  //    → 本分支重展开并锁定，定稿后（isStreaming=false）仍保持展开供回顾。
  //
  //  · **chat 轮**（无工具/思考，纯协调者/worker 对话回复）→ 保留原有「思考结束让位给正文」自动
  //    收起行为：流式思考活跃期（isStreaming 且有 reasoning 且正文尚未流出）自动展开，让用户看见
  //    思考逐字流出；正文 content 一开始流即收起，让位给回复正文。这是协调者 CHAT 路径的既有体感，
  //    不回归。
  //
  // 「思考结束」信号（仅 chat 轮判定）= 正文开始流（hasContent）。推理模型先流 reasoning_content、
  // 再流可见 content——reasoning 阶段 content 空，content 非空即标志思考结束、正文开始 → 收起。
  // reasoning 本身不清空（落盘用），故不能靠「reasoning 是否存在」判定，要靠「正文是否已开始」。
  // 非推理模型无 reasoning → reasoningActive 恒 false → 从不展开思考区（无思考可展，正确）。
  //
  // 用户手动展开/收起（点标题）优先——手动操作后 5s 内不自动覆盖（尊重用户意图）；5s 后若思考
  // 仍在流（新 delta 到达触发 effect 重跑），恢复自动展开。
  const reasoningActive = isStreaming && hasReasoning && !hasContent
  const [reasoningExpanded, setReasoningExpanded] = useState(false)
  const [userToggledAt, setUserToggledAt] = useState(0)
  useEffect(() => {
    // execute 轮：整轮保持展开（含定稿回顾），不随正文流自动收起。
    if (isExecuteTurn) {
      // 5s 内用户手动 toggle 过 → 尊重用户意图，不自动覆盖
      if (Date.now() - userToggledAt < 5000) return
      setReasoningExpanded(true)
      return
    }
    // chat 轮：思考活跃期展开、正文开始流时收起让位。
    if (!reasoningActive) {
      // 非流式期（定稿气泡 isStreaming=false）不清空用户手动展开——让定稿气泡手动展开的
      // 历史思考保持展开。流式期思考刚结束（reasoningActive=false）才自动收起。
      if (isStreaming) setReasoningExpanded(false)
      return
    }
    // 用户在最近 5s 内手动 toggle 过 → 尊重用户意图，不自动覆盖
    if (Date.now() - userToggledAt < 5000) return
    setReasoningExpanded(true)
  }, [reasoning, reasoningActive, userToggledAt, isStreaming, isExecuteTurn])
  const toggleReasoning = () => {
    setUserToggledAt(Date.now())
    setReasoningExpanded((v) => !v)
  }
  // ST-06（task 21 数据管道）：worker 任务产物文件列表（task_complete data.artifact.files[]）。
  // 仅 finalizedBubbles 传入（task 21 从 task_complete 事件 data.artifact 提取）；流式/协调者
  // 气泡不传（默认空数组）。hasArtifacts 让「既无工具也无内容也无思考也无产物」的防御兜底放行，
  // 使纯产物定稿气泡（content 空但有产物文件，如 worker 全程用工具产出文件最终回复为空）仍能渲染。
  // 失败/取消/超时路径后端不透传 artifact（bus.py 仅成功路径写 data.artifact），失败气泡自然无下载卡。
  const hasArtifacts = artifactFiles.length > 0
  // ST-05（task 18 归并 / task 19 渲染）：worker ReAct 中间思考事件（task_think，data.phase
  // 'thinking'|'final'）。thinkEvents 由父组件（ChatPanel.thinkEventsByTask）按 task_id 过滤后
  // 传入。task 18 完成归并管道（prop + 守卫）；本任务（task 19）渲染为气泡内折叠区。
  // hasThinks 让「既无工具也无内容也无思考」的防御兜底放行，使纯思考气泡（content 为空但有
  // think 事件）仍能渲染——流式 worker 可能在 task_token 到达前先流 task_think（thinking phase）。
  const hasThinks = thinkEvents.length > 0
  // task_think → 折叠块项：每条思考事件一个 Collapse item（按时间序），phase 区分中间推理
  // （thinking，工具调用前的模型思考片段）与最终答案（final，task_answer）。复用 reasoning 折叠区
  // 视觉（同色系/Collapse ghost size=small/同 pre 样式）——与协调者思考过程折叠区观感一致，
  // 用户一眼认出「这是模型的思考」。标题带 phase 标签 + 字符数（worker think 无后端 token 统计，
  // 用字符数近似，区别于协调者 reasoning 的 token 数——协调者有 stats 推真值，worker 无）。
  const thinkRows = useMemo(() => {
    const sorted = [...thinkEvents].sort((a, b) => a.timestamp - b.timestamp)
    return sorted.map((e) => {
      const data = (e.data || {}) as Record<string, unknown>
      const phase = data['phase'] === 'final' ? 'final' : 'thinking'
      const text = e.content || ''
      return {
        key: e.id || `think-${e.timestamp}`,
        phase,
        text,
      }
    })
  }, [thinkEvents])
  // ReAct think 折叠块主动展开/收起态——用户要求「一个 turn 一个气泡展示完整过程」：
  // 流式期（isStreaming=true）自动展开让用户看见 ReAct 实时思考；定稿后（isStreaming=false）
  // 保持展开供回顾（用户明确抱怨过收起——要看过程）。用 controlled activeKey 实现「默认全展开」：
  //  · 默认所有 think 行都展开（thinkCollapsedKeys 空 Set）——流式 + 定稿均如此；
  //  · 用户可手动收起单个行（点标题 toggle）——本地 Set 记录手动收起的 key，覆盖默认展开；
  //  · 新到达的 think 行（流式期 ReAct 中间步陆续到达）不在收起集合里 → 自动展开，
  //    满足「流式期 auto-EXPAND 让用户看见实时思考」。
  // 判断：原先 think Collapse 无 activeKey（uncontrolled，默认收起）——用户得手动点开才看得见
  // 思考，且新到达行默认收起，体感「过程被藏起来」。改为 controlled 后默认全展开，过程一目了然。
  const [thinkCollapsedKeys, setThinkCollapsedKeys] = useState<Set<string>>(new Set())
  const thinkActiveKeys = thinkRows
    .map((r) => r.key)
    .filter((k) => !thinkCollapsedKeys.has(k))
  const onThinkCollapseChange = (keys: string[]) => {
    // diff 当前 activeKey 集合 vs 全部 key，找被收起的 → 加入 collapsed Set；被展开的 → 移出。
    setThinkCollapsedKeys((prev) => {
      const next = new Set(prev)
      for (const r of thinkRows) {
        if (!keys.includes(r.key)) next.add(r.key)
        else next.delete(r.key)
      }
      return next
    })
  }
  // 折叠区标题「思考过程（N tokens）」的 token 数：优先用后端 stats 推的真实 reasoning_tokens；
  // 流式前 ~200ms 首个 stats 未到时用 reasoning.length//3 临时估算（与后端 live_reasoning_tokens
  // 同启发式）。用 token 不用字符数——与状态行「↓ N tokens」同单位。
  const reasoningTokenLabel =
    reasoningTokens && reasoningTokens > 0
      ? reasoningTokens
      : Math.max(1, Math.ceil((reasoning?.length || 0) / 3))
  // 既无工具也无内容也无推理也无思考也无产物且非流式 → 不该渲染气泡（父组件应已过滤，此为防御兜底）
  if (!hasTools && !hasContent && !hasReasoning && !hasThinks && !hasArtifacts && !isStreaming) return null

  return (
    <div
      className="chat-msg"
      style={{ flexDirection: isUser ? 'row-reverse' : 'row' }}
      data-sender={senderId}
    >
      {avatar}
      <div className="chat-bubble-wrap">
        {actionGroup}
        <div className={`chat-sender-name ${isUser ? 'chat-sender-name--right' : ''}`}>
          {senderName}
        </div>
        <Bubble
          placement={isUser ? 'end' : 'start'}
          variant="borderless"
          streaming={isStreaming}
          content={content}
          contentRender={(c) => (
            <div className={hasTools ? 'chat-bubble-content' : undefined}>
              {renderContent ? renderContent(String(c)) : String(c)}
              {isStreaming && <span className="chat-streaming-cursor" />}
            </div>
          )}
          classNames={{
            root: [
              'chat-bubble',
              isUser ? 'chat-bubble--self' : 'chat-bubble--other',
              isStreaming ? 'chat-bubble--streaming' : '',
              isFailed ? 'chat-bubble--failed' : '',
            ]
              .filter(Boolean)
              .join(' '),
          }}
          header={
            hasReasoning || hasThinks || hasTools ? (
              <>
                {/* 推理过程折叠区（气泡顶部，工具摘要之上）—— 推理模型在可见 content 前流出的内部思维链。
                    用 antd Collapse（项目约定：有现成开源组件就不手写）。主动展开/收起按 turn 类型分叉：
                      · execute 轮（hasTools || hasThinks）→ 整轮保持展开（含定稿回顾），让用户看完整过程；
                      · chat 轮（无工具/思考）→ 旧有体感：思考期展开，正文开始流时收起让位。
                    用户手动点标题展开/收起优先——手动操作后 5s 内不自动覆盖（尊重用户意图）。 */}
                {hasReasoning && (
                  <div style={{ marginBottom: 6 }}>
                    <Collapse
                      size="small"
                      ghost
                      activeKey={reasoningExpanded ? ['reasoning'] : []}
                      onChange={() => toggleReasoning()}
                      items={[{
                        key: 'reasoning',
                        label: (
                          <span style={{ color: '#faad14', fontSize: 12, display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                            <BulbOutlined style={{ fontSize: 12 }} />
                            思考过程（{reasoningTokenLabel} tokens）
                          </span>
                        ),
                        children: (
                          <pre
                            style={{
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
                            }}
                          >
                            {reasoning}
                          </pre>
                        ),
                      }]}
                    />
                  </div>
                )}

                {/* ST-05（task 19）：worker ReAct 思考折叠区——task_think 事件按 task_id 归并（task 18）
                    后渲染为气泡内折叠块。换成 @ant-design/x ThoughtChain（多 item 时间线模型匹配 ReAct
                    多步思考，比手写 Collapse items 更贴合开源组件抽象）。
                      · phase=thinking（中间推理）→ 标签「思考」；
                      · phase=final（task_answer 最终答案）→ 标签「结论」。
                    受控展开：expandedKeys=thinkActiveKeys（默认全展开——流式期 auto-EXPAND 让用户看见
                    实时思考；定稿后保持展开供回顾），onExpand=onThinkCollapseChange 记入 thinkCollapsedKeys
                    Set 覆盖默认展开；新到达的 think 行不在收起集合里 → 自动展开。
                    视觉沿用 reasoning 折叠区色系 #faad14 + BulbOutlined，让用户一眼认出「这是模型的思考」。
                    位置在 reasoning 折叠区之下、工具摘要之上：reasoning 是协调者流式推理（coordReasoning），
                    think 是 worker ReAct 思考（task_think），两者来源不同但视觉同区，按气泡类型择一渲染
                    （worker 气泡无 reasoning、有 think；协调者气泡有 reasoning、无 think）。 */}
                {hasThinks && (
                  <div style={{ marginBottom: 6 }}>
                    <ThoughtChain
                      items={thinkRows.map((row) => ({
                        key: row.key,
                        icon: <BulbOutlined style={{ color: '#faad14', fontSize: 12 }} />,
                        title: (
                          <span style={{ color: '#faad14', fontSize: 12, display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                            {row.phase === 'final' ? '结论' : '思考'}
                            {row.text ? `（${row.text.length} 字）` : ''}
                          </span>
                        ),
                        content: row.text ? (
                          <pre
                            style={{
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
                            }}
                          >
                            {row.text}
                          </pre>
                        ) : (
                          <span style={{ color: '#bfbfbf', fontSize: 12 }}>（空）</span>
                        ),
                        collapsible: true,
                      }))}
                      expandedKeys={thinkActiveKeys}
                      onExpand={onThinkCollapseChange}
                    />
                  </div>
                )}

                {/* 工具调用摘要区（气泡顶部）——手写 div + CaretRightOutlined + Tag + payload pre。
                    这是工具调用展示，不强行套 ThoughtChain（工具是 args/output 数据结构 + 配对 start/end
                    算耗时的数据编排逻辑，不是通用展示组件，自己写是对的——use-open-source-not-handrolled
                    的例外条款）。 */}
                {hasTools && (
                  <div className="chat-tool-block">
                    {toolRows.map((row) => {
                      const isOpen = expanded.has(row.key)
                      return (
                        <div key={row.key} style={{ marginBottom: 4 }}>
                          <div
                            className="chat-tool-row-label"
                            onClick={() => toggleExpand(row.key)}
                            style={{ cursor: 'pointer' }}
                            title={isOpen ? '点击收起详情' : '点击展开详情'}
                          >
                            <CaretRightOutlined
                              style={{
                                fontSize: 10,
                                transition: 'transform 0.2s',
                                transform: isOpen ? 'rotate(90deg)' : 'rotate(0deg)',
                                flexShrink: 0,
                              }}
                            />
                            <ToolOutlined style={{ fontSize: 12, opacity: 0.7 }} />
                            <Tag color={toolTagColor(row.isEnd)} style={{ margin: 0, fontSize: 11 }}>
                              {row.isEnd ? '返回' : '调用'}
                            </Tag>
                            <span className="chat-tool-row-name">{row.name}</span>
                            {row.argsPreview && (
                              <span className="chat-tool-row-args" title={toPreview(row.payload, 500)}>
                                {row.argsPreview}
                              </span>
                            )}
                            {row.isEnd && row.elapsedMs != null && (
                              <span className="chat-tool-row-elapsed">
                                耗时 {formatElapsed(row.elapsedMs)}
                              </span>
                            )}
                          </div>
                          {isOpen && row.payload != null && (
                            <div>
                              <div className="chat-tool-payload-label">
                                {row.isEnd ? '输出' : '参数'}
                              </div>
                              <pre className="chat-tool-payload">
                                {typeof row.payload === 'string'
                                  ? row.payload
                                  : JSON.stringify(row.payload, null, 2)}
                              </pre>
                            </div>
                          )}
                        </div>
                      )
                    })}
                  </div>
                )}
              </>
            ) : undefined
          }
          footer={
            /* ST-06（task 21 数据管道 / task 22 渲染）：worker 任务产物下载卡。
               task_complete 事件 data.artifact.files[]（bus.py emit_task_completed 仅成功路径透传
               scan_workspace_artifacts manifest）经 ChatPanel.finalizedBubbles 提取 → artifactFiles
               prop 传入。每文件一张小卡：按扩展名图标 + 文件名（截断 + tooltip 全 path）+ 大小 +
               下载按钮。位置在正文之下（产物是任务收尾后产出）；失败/取消/超时任务无 artifact（后端不
               透传），失败气泡自然无下载卡（语义正确——失败不留可用产物）。 */
            hasArtifacts ? (
              <div className="chat-artifact-block">
                {artifactFiles.map((f) => {
                  const key = f.path || f.name
                  const isLoading = downloading === key
                  const disabled = !groupId || (downloading !== null && downloading !== key)
                  return (
                    <div key={key} className="chat-artifact-card">
                      {fileIconFor(f.name, { fontSize: 14, flexShrink: 0 })}
                      <Tooltip title={f.path || f.name}>
                        <span className="chat-artifact-name">{f.name}</span>
                      </Tooltip>
                      {f.size > 0 && <span className="chat-artifact-size">{humanSize(f.size)}</span>}
                      <Tooltip title={!groupId ? '未选群组，无法下载' : ''}>
                        <Button
                          className="chat-artifact-download"
                          type="link"
                          size="small"
                          icon={<DownloadOutlined />}
                          loading={isLoading}
                          disabled={disabled}
                          onClick={() => handleArtifactDownload(f)}
                        />
                      </Tooltip>
                    </div>
                  )
                })}
              </div>
            ) : undefined
          }
        />
        <div className={`chat-timestamp ${isUser ? 'chat-timestamp--right' : ''}`}>
          {new Date(timestamp).toLocaleTimeString()}
        </div>
        {statusLine && <div className="chat-status-line">{statusLine}</div>}
      </div>
    </div>
  )
}

import { useEffect, useMemo, useRef, useState } from 'react'
import { Bubble, ThoughtChain } from '@ant-design/x'
import { Collapse, Tooltip, Button, message, Timeline, Popover } from 'antd'
import { ToolOutlined, BulbOutlined, DownloadOutlined } from '@ant-design/icons'
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
  // 工具调用整组折叠（外层 Collapse「工具调用 (N)」）——展开策略：
  //  · 流式中（isStreaming=true）默认收起（过程信息按需查看，不撑高气泡）；
  //  · 回答完成（isStreaming=false）默认展开（用户要看到工具调用全过程）。
  // 懒初始化按 isStreaming 决定，避免历史消息回显首帧 flash；完成后由 autoExpandOnDone effect 接管。
  const [toolBlockExpanded, setToolBlockExpanded] = useState(() => !isStreaming)
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

  const toggleToolBlock = () => {
    setToolBlockExpanded((v) => !v)
  }

  const hasTools = toolRows.length > 0
  const hasContent = content && content.length > 0
  const hasReasoning = !!(reasoning && reasoning.length > 0)
  // execute 轮判定：气泡携带工具调用或 ReAct 思考事件（hasTools || hasThinks）。
  // （仅作「气泡是 execute 轮还是 chat 轮」的语义标签 + effect 依赖项；思考折叠策略已统一为
  // 「过程默认收起」，但保留 isExecuteTurn 作语义标签供未来扩展，避免回归时丢失上下文。）
  const isExecuteTurn = toolRows.length > 0 || thinkEvents.length > 0

  // 思考折叠区主动展开/收起态——用户反馈「think 步多+工具多全展开太散像竖排步骤列表」→
  // 默认收起，让最终回答（Bubble.content）一眼可见；想看过程再手动点开。
  //
  // 统一规则（不再按 execute/chat 轮分叉）：
  //  · **思考活跃期**（isStreaming + reasoning + 正文未流出）→ 自动展开，让用户看见实时思考逐字流出；
  //  · **其他时候**（正文开始流 / 已定稿 / execute 轮定稿后回顾）→ 自动收起，让位给最终回答。
  //
  // 这替换了旧逻辑里「execute 轮整轮保持展开（含定稿回顾）」——用户明确抱怨全展开太散，
  // 故 execute 轮定稿后也默认收起，过程信息降级为「按需查看」。
  //
  // 用户手动展开/收起（点标题）优先——手动操作后 5s 内不自动覆盖（尊重用户意图）；5s 后若思考
  // 仍在流（新 delta 到达触发 effect 重跑），恢复自动收起。
  const reasoningActive = isStreaming && hasReasoning && !hasContent
  const [reasoningExpanded, setReasoningExpanded] = useState(() => !isStreaming && hasReasoning)
  const [userToggledAt, setUserToggledAt] = useState(0)
  useEffect(() => {
    // 5s 内用户手动 toggle 过 → 尊重用户意图，不自动覆盖
    if (Date.now() - userToggledAt < 5000) return
    // 流式态：思考活跃期展开、其他时候（含定稿后）收起
    if (isStreaming) {
      setReasoningExpanded(reasoningActive)
    }
    // 完成态：由 autoExpandOnDoneRef effect 接管首次展开，不在此处反复重置（避免覆盖用户收起操作）
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
  // ReAct think 折叠块主动展开/收起态——用户反馈「think 步多全展开太散像竖排步骤列表」→
  // 默认只展开最后一条 think（通常是最关键/最接近结论的中间步），其余收起。这样既能让用户一眼
  // 看到最终回答（Bubble.content，think 之外的正文），又保留了「过程一目了然」的最低限度可见性——
  // 最后一条 think 像目录里的「当前焦点」，想看完整过程再点开其余项。
  //
  // 受控展开机制（保留）：expandedKeys=thinkActiveKeys（直接计算：默认含 lastThinkKey，叠加
  // userExpandedThinks 显式展开项，扣 userCollapsedThinks 显式收起项）。onExpand=onThinkCollapseChange
  // 把用户的折叠/展开意图分别记入 userExpanded/userCollapsed 两个 Set，覆盖默认策略。
  //  · 默认应展开（最后一条）但 keys 不含 → 用户收起它 → userCollapsedThinks.add
  //  · 默认应收起（非最后一条）但 keys 含 → 用户展开它 → userExpandedThinks.add
  //  · 当前态 == 默认态 → 该 key 无用户覆盖，从对应 Set 移除（避免 stale 残留）
  // 流式期 thinkRows 动态增长，新到达的行若非最后一条默认收起；最后一条随增长自动成为展开焦点。
  const [userExpandedThinks, setUserExpandedThinks] = useState<Set<string>>(new Set())
  const [userCollapsedThinks, setUserCollapsedThinks] = useState<Set<string>>(new Set())
  const lastThinkKey = thinkRows.length > 0 ? thinkRows[thinkRows.length - 1].key : null
  const thinkActiveKeys = thinkRows
    .map((r) => r.key)
    .filter((k) => {
      // 用户显式收起 → 不展开
      if (userCollapsedThinks.has(k)) return false
      // 用户显式展开 → 展开
      if (userExpandedThinks.has(k)) return true
      // 默认：流式中只展开最后一条（过程焦点）；回答完成（!isStreaming）全展开（看完整过程）
      return isStreaming ? k === lastThinkKey : true
    })
  const onThinkCollapseChange = (keys: string[]) => {
    // diff 当前 activeKey 集合 vs 默认策略，把用户的折叠意图记入 userExpanded/userCollapsed Set：
    //  · 默认应展开但 keys 不含 → 用户收起它 → userCollapsed.add
    //  · 默认应收起但 keys 含 → 用户展开它 → userExpanded.add
    //  · 当前态 == 默认态 → 该 key 无用户覆盖，从对应 Set 移除（避免 stale 残留）。
    // 默认策略：流式中只展开最后一条；完成态全展开。
    setUserExpandedThinks((prev) => {
      const next = new Set(prev)
      for (const r of thinkRows) {
        const defaultExpanded = isStreaming ? r.key === lastThinkKey : true
        const currentExpanded = keys.includes(r.key)
        if (currentExpanded && !defaultExpanded) {
          next.add(r.key)
        } else {
          next.delete(r.key)
        }
      }
      return next
    })
    setUserCollapsedThinks((prev) => {
      const next = new Set(prev)
      for (const r of thinkRows) {
        const defaultExpanded = isStreaming ? r.key === lastThinkKey : true
        const currentExpanded = keys.includes(r.key)
        if (!currentExpanded && defaultExpanded) {
          next.add(r.key)
        } else {
          next.delete(r.key)
        }
      }
      return next
    })
  }
  // 回答完成时自动展开三个折叠区（仅一次，不覆盖后续用户操作）。
  // 触发条件：!isStreaming（回答完成/历史消息回显）且 autoExpandOnDoneRef.current=false。
  // 首次进入完成态时：
  //  · toolBlockExpanded → true
  //  · reasoningExpanded → true（仅当 hasReasoning，避免空壳展开）
  //  · thinkActiveKeys → 全部 think key（由 thinkActiveKeys 计算逻辑的 !isStreaming 分支自动实现）
  // 然后置 autoExpandOnDoneRef.current=true，之后即使用户收起某区，也不再自动重置（尊重用户意图）。
  // 流式→完成 transition：isStreaming 由 true 转 false 时，effect 重跑，首次 false 触发展开。
  // 历史消息回显：mount 时 isStreaming=false，effect 首跑即触发展开（同步 setState 在 commit 前生效）。
  const autoExpandOnDoneRef = useRef(false)
  useEffect(() => {
    if (!isStreaming && !autoExpandOnDoneRef.current) {
      autoExpandOnDoneRef.current = true
      setToolBlockExpanded(true)
      if (hasReasoning) {
        setReasoningExpanded(true)
      }
      // think 默认全展开由 thinkActiveKeys 的 !isStreaming 分支自动实现，无需在此 setState
    }
  }, [isStreaming, hasReasoning])
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
                    用 antd Collapse（项目约定：有现成开源组件就不手写）。默认收起——让最终回答一眼可见，
                    想看模型「怎么想的」再手动点开。仅思考活跃期（isStreaming + reasoning + 正文未流出）
                    自动展开让用户看见实时思考逐字流出，正文一开始流即收起让位。
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
                    受控展开：expandedKeys=thinkActiveKeys（默认只展开最后一条 think——既给用户「过程焦点」
                    一眼可见，又不让多步全展开散成竖排步骤列表）；onExpand=onThinkCollapseChange 把用户
                    折叠意图记入 userExpanded/userCollapsed Set 覆盖默认策略。
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

                {/* 工具调用折叠区——外层 AntD Collapse 标题行「🔧 工具调用 (N) · 总耗时 Xs」默认收起；
                    展开后用 AntD Timeline 渲染树形缩进明细（每条 tool 一个 Timeline item，dot 用 ▸
                    让展开的明细有「├─」树形视觉对齐用户草图）。仍保留受控展开 state：
                    展开某条 tool 详情用 Popover（点 chip 弹出 args/output payload）——chip 是横向
                    紧凑标签，整组明细竖排成树形列表（步骤语义，竖排可接受）。 */}
                {hasTools && (
                  <div className="chat-tool-block">
                    <Collapse
                      size="small"
                      ghost
                      activeKey={toolBlockExpanded ? ['tools'] : []}
                      onChange={() => toggleToolBlock()}
                      items={[{
                        key: 'tools',
                        label: (
                          <span style={{ color: '#8c8c8c', fontSize: 12, display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                            <ToolOutlined style={{ fontSize: 12 }} />
                            工具调用 ({toolRows.length})
                          </span>
                        ),
                        children: (
                          <Timeline
                            className="chat-tool-timeline"
                            items={toolRows.map((row) => ({
                              key: row.key,
                              color: row.isEnd ? 'gray' : 'green',
                              dot: (
                                <span className="chat-tool-timeline-dot">
                                  {row.isEnd ? '↳' : '├─'}
                                </span>
                              ),
                              children: (
                                <span className="chat-tool-timeline-row">
                                  <ToolOutlined style={{ fontSize: 11, opacity: 0.7, flexShrink: 0 }} />
                                  <span className="chat-tool-row-name">{row.name}</span>
                                  {row.isEnd ? (
                                    <span className="chat-tool-row-elapsed">
                                      · {formatElapsed(row.elapsedMs ?? 0)}
                                    </span>
                                  ) : (
                                    <span className="chat-tool-row-elapsed">· 调用中</span>
                                  )}
                                  {row.payload != null && (
                                    <Popover
                                      placement="rightTop"
                                      title={row.isEnd ? '输出' : '参数'}
                                      content={
                                        <pre className="chat-tool-payload">
                                          {typeof row.payload === 'string'
                                            ? row.payload
                                            : JSON.stringify(row.payload, null, 2)}
                                        </pre>
                                      }
                                      trigger="click"
                                      overlayClassName="chat-tool-payload-popover"
                                    >
                                      <span className="chat-tool-detail-trigger" title="点击查看详情">
                                        详情
                                      </span>
                                    </Popover>
                                  )}
                                  {row.argsPreview && (
                                    <span className="chat-tool-row-args" title={toPreview(row.payload, 500)}>
                                      {row.argsPreview}
                                    </span>
                                  )}
                                </span>
                              ),
                            }))}
                          />
                        ),
                      }]}
                    />
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

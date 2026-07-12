import { useMemo, useState } from 'react'
import { Collapse, Tag } from 'antd'
import { CaretRightOutlined, ToolOutlined, BulbOutlined } from '@ant-design/icons'
import type { TraceEvent } from '../services/api'
import './ChatMessageBubble.css'

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
  isStreaming = false,
  isFailed = false,
  isUser = false,
  renderContent,
  statusLine,
}: ChatMessageBubbleProps) {
  // 多工具独立折叠：key=事件 id 的 Set。点击行 toggle 该工具展开/收起。
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

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
  // 折叠区标题「思考过程（N tokens）」的 token 数：优先用后端 stats 推的真实 reasoning_tokens；
  // 流式前 ~200ms 首个 stats 未到时用 reasoning.length//3 临时估算（与后端 live_reasoning_tokens
  // 同启发式）。用 token 不用字符数——与状态行「↓ N tokens」同单位。
  const reasoningTokenLabel =
    reasoningTokens && reasoningTokens > 0
      ? reasoningTokens
      : Math.max(1, Math.ceil((reasoning?.length || 0) / 3))
  // 既无工具也无内容也无推理且非流式 → 不该渲染气泡（父组件应已过滤，此为防御兜底）
  if (!hasTools && !hasContent && !hasReasoning && !isStreaming) return null

  return (
    <div
      className="chat-msg"
      style={{ flexDirection: isUser ? 'row-reverse' : 'row' }}
      data-sender={senderId}
    >
      {avatar}
      <div className="chat-bubble-wrap">
        <div className={`chat-sender-name ${isUser ? 'chat-sender-name--right' : ''}`}>
          {senderName}
        </div>
        <div
          className={[
            'chat-bubble',
            isUser ? 'chat-bubble--self' : 'chat-bubble--other',
            isStreaming ? 'chat-bubble--streaming' : '',
            isFailed ? 'chat-bubble--failed' : '',
          ]
            .filter(Boolean)
            .join(' ')}
        >
          {/* 推理过程折叠区（气泡顶部，工具摘要之上）—— 推理模型在可见 content 前流出的内部思维链。
              用 antd Collapse（项目约定：有现成开源组件就不手写），默认收起；展开后显示完整推理
              文本（流式期来自 coordReasoning 实时累加，定稿后来自持久化 data.reasoning）。
              Collapse 自管展开态，无需本地 state。 */}
          {hasReasoning && (
            <div style={{ marginBottom: 6 }}>
              <Collapse
                size="small"
                ghost
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

          {/* 工具调用摘要区（气泡顶部） */}
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

          {/* 正文内容（流式时尾追加闪烁光标） */}
          {(hasContent || isStreaming) && (
            <div className={hasTools ? 'chat-bubble-content' : undefined}>
              {renderContent ? renderContent(content) : content}
              {isStreaming && <span className="chat-streaming-cursor" />}
            </div>
          )}
        </div>
        <div className={`chat-timestamp ${isUser ? 'chat-timestamp--right' : ''}`}>
          {new Date(timestamp).toLocaleTimeString()}
        </div>
        {statusLine && <div className="chat-status-line">{statusLine}</div>}
      </div>
    </div>
  )
}

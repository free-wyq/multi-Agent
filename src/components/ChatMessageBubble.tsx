import { useEffect, useMemo, useRef, useState } from 'react'
import { Bubble } from '@ant-design/x'
import { Collapse, Tooltip, Button, message, Timeline, Popover, Card, Descriptions, List, Table, Empty, Typography, Tag, Space, Badge, Divider } from 'antd'
import { ToolOutlined, BulbOutlined, DownloadOutlined, TableOutlined, UnorderedListOutlined, ProfileOutlined, ReloadOutlined, CheckCircleOutlined } from '@ant-design/icons'
import type { TraceEvent } from '../services/api'
import { groupApi } from '../services/api'
import { fileIconFor, saveBlob, humanSize } from '../lib/fileIcon'
import { renderMarkdown } from '../lib/renderMarkdown'
// 卡片段解析/切片抽到 lib（[任务10d] 先抽纯函数再测，作为后续卡片重构安全网）——
// parseCards/splitContentByCards 纯函数无 React 依赖，单测锁住契约。
import { splitContentByCards, type ParsedCard } from '../lib/cardSegments'
import BubbleCopyButton from './BubbleCopyButton'
import './ChatMessageBubble.css'

// ════════════════════════════════════════════════════════════════════════════
// 需求2-前端：结构化结果卡片段（[需求2-设计] commit 9df5116 单真源
// `docs/structured-result-card-schema.md`，[需求2-后端] commit b9a0597 提示词/解析已落）
//
// 线格式：markdown fenced code block + `card` info string 包 JSON payload。
//   ```card
//   {"icon":"🔥","title":"百度热搜 Top 5","kind":"table",
//    "columns":["排名","标题","热度"],
//    "rows":[["1","神舟二十号","9821"],...]}
//   ```
// 卡片是 `content` 子串，走现有 _unified_reply → persist_agent_reply → emit_message_added
// 全程透传（不改 DB / 不加事件 / 不改 message dict）。本组件负责「把 content 里的 ```card```
// 块切出来渲染成 AntD 卡片，剩余散文按原路径渲染」。
//
// CARD_RE 必须与后端 `backend/llm/card_fragment.py CARD_FRAGMENT_RE` byte-identical
// （/```card\s*\n([\s\S]*?)```/g）——后端 count_card_fragments 计数与前端 parseCards 解析对同一
// content 的「块数」判定必须一致（后端日志验证 LLM 是否遵守 CARD_OUTPUT_CONTRACT，前端渲染
// 同一份 content），否则两边对不齐。模式语义：三反引号 + 字面 `card` info string + 换行，
// 非贪婪捕获（[\s\S] 跨换行）到闭合三反引号。
// ════════════════════════════════════════════════════════════════════════════

// 解析/切片逻辑（CARD_RE / parseCards / splitContentByCards / ParsedCard / CardPayload /
// ContentSegment）已抽到 `src/lib/cardSegments.ts`（[任务10d]）——组件只保留渲染层
// （StructuredCard：kv/list/table 分支 + 未知 kind 降级 code 块）。lib 纯函数由单测锁契约，
// 是后续卡片重构（任务7a 换 antd Card / 任务4 持久化气泡复用切卡）的安全网。
// CARD_RE 与后端 `backend/llm/card_fragment.py CARD_FRAGMENT_RE` byte-identical——后端
// count_card_fragments 计数与前端 parseCards 解析对同一 content 的「块数」判定必须一致。
// ════════════════════════════════════════════════════════════════════════════

/** 安全取 string：payload 里值理论上全是 string，但容错——数字/布尔强转 string，对象/数组 JSON 化。 */
function asString(v: unknown): string {
  if (v == null) return ''
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  return JSON.stringify(v)
}

/** 需求2-前端 结构化结果卡渲染子组件。
 *  三 kind 分支（设计 §4）：
 *   - kv    → AntD Descriptions（键值表，size="small" column=1）
 *   - list  → AntD List（bullet 竖排，每项前 `•`）
 *   - table → AntD Table（size="small" pagination=false，columns/rows 全 string）
 *  未知 kind → 普通代码块（设计 §5：不崩，显示原始 JSON，便于调提示词）。
 *  字段缺失/类型错 → 容错：items 非数组当空；rows 行长≠columns 截断补空（设计 §5）。
 *  外层 AntD Card（size="small"）包标题+图标，与 ModelCard 同款视觉（聊天流内卡片）。 */
function StructuredCard({ card }: { card: ParsedCard }) {
  // 解析失败 → 降级普通代码块（保留围栏原文，让用户看到 LLM 产出的原始 JSON）
  if (!card.json) {
    return (
      <pre className="chat-card-fallback" title="卡片 JSON 解析失败，已降级为代码块">
        {card.raw}
      </pre>
    )
  }
  const { icon, title, kind } = card.json
  const iconStr = asString(icon)
  const titleStr = asString(title)

  // 渲染卡片头部：图标 + 标题（任一非空才渲染头部；都空则无头部卡——设计 §5 允许空标题卡）
  const header = (iconStr || titleStr) ? (
    <span className="chat-card-title">
      {iconStr && <span className="chat-card-icon">{iconStr}</span>}
      {titleStr && <span className="chat-card-label">{titleStr}</span>}
    </span>
  ) : null

  let body: React.ReactNode
  if (kind === 'kv') {
    // items: [{label, value}]——非数组/元素非对象当空
    const items = Array.isArray(card.json.items) ? card.json.items : []
    const descItems = items
      .map((it) => {
        if (typeof it !== 'object' || it === null || Array.isArray(it)) return null
        const obj = it as { label?: unknown; value?: unknown }
        return {
          key: `${asString(obj.label)}-${asString(obj.value)}`,
          label: asString(obj.label) || '—',
          children: asString(obj.value) || '—',
        }
      })
      .filter((x): x is { key: string; label: string; children: string } => x !== null)
    body = descItems.length > 0 ? (
      <Descriptions size="small" column={1} colon={false} items={descItems} />
    ) : (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无键值项" />
    )
  } else if (kind === 'list') {
    // items: [string]——非数组当空
    const items = Array.isArray(card.json.items) ? card.json.items : []
    const strs = items.map(asString).filter((s) => s.length > 0)
    body = strs.length > 0 ? (
      <List
        size="small"
        dataSource={strs}
        renderItem={(s) => (
          <List.Item style={{ padding: '2px 0', borderBlockEnd: 'none' }}>
            <span className="chat-card-list-item"><span className="chat-card-bullet">•</span>{s}</span>
          </List.Item>
        )}
      />
    ) : (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无列表项" />
    )
  } else if (kind === 'table') {
    // columns: [string], rows: [[string]]——行长≠columns 截断补空（设计 §5）
    const colsRaw = Array.isArray(card.json.columns) ? card.json.columns : []
    const cols = colsRaw.map(asString)
    const rowsRaw = Array.isArray(card.json.rows) ? card.json.rows : []
    const rows = rowsRaw.map((r) => {
      if (!Array.isArray(r)) return cols.map(() => '')
      // 截断/补齐到 columns 长度
      const cells = r.map(asString)
      while (cells.length < cols.length) cells.push('')
      return cells.slice(0, cols.length)
    })
    body = cols.length > 0 ? (
      <Table
        size="small"
        pagination={false}
        rowKey={(_r, idx) => String(idx)}
        columns={cols.map((c, i) => ({ title: c, dataIndex: String(i), key: String(i) }))}
        dataSource={rows.map((cells) => {
          const row: Record<string, string> = {}
          cells.forEach((cell, i) => { row[String(i)] = cell })
          return row
        })}
        scroll={{ x: 'max-content' }}
      />
    ) : (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无表头" />
    )
  } else {
    // 未知 kind → 降级普通代码块（设计 §5：不崩，显示原始 JSON）
    return (
      <pre className="chat-card-fallback" title={`未知卡片 kind: ${kind ?? '(缺)'}`}>
        {card.raw}
      </pre>
    )
  }

  // kind 图标（标题旁小标，区分三 kind 视觉）
  const kindIcon =
    kind === 'kv' ? <ProfileOutlined /> :
    kind === 'list' ? <UnorderedListOutlined /> :
    kind === 'table' ? <TableOutlined /> : null

  return (
    <Card
      size="small"
      className="chat-card-block"
      title={
        header ? (
          <span className="chat-card-title">
            {kindIcon && <span className="chat-card-kind-icon">{kindIcon}</span>}
            {iconStr && <span className="chat-card-icon">{iconStr}</span>}
            {titleStr}
          </span>
        ) : (
          kindIcon ? <span className="chat-card-kind-icon">{kindIcon}</span> : undefined
        )
      }
    >
      {body}
    </Card>
  )
}

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
  /** 完成态元数据（「📝 最终生成」Timeline item 用）。
   *  ChatPanel 在持久化/finalized 气泡路径把已有的 stats（extractCoordStats 或流式 coordStats
   *  落地的真值）传进来——字数从 content.length 自算，tokens/耗时/model/reasoningTokens 从 stats 取。
   *  未传（undefined）→ 完成态不渲染「📝 最终生成」item（流式气泡 / finalized 过渡气泡无 stats 时
   *  自然不显示，向后兼容）。协调者流式气泡已有顶部 statusLine，传 finalStats 后📝 item 仅显示
   *  字数 + 耗时（避免与顶部 statusLine 的 tokens 重复，由渲染层自行选择字段）。 */
  finalStats?: { elapsedMs?: number; tokens?: number; reasoningTokens?: number; model?: string }
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
  /** 需求2-前端：操作栏「重新生成」回调。父组件（ChatPanel）注入——后端「按 reply_id 重跑」
   *  端点已就绪（[需求2-后端] POST /api/messages/regenerate?replyId=...），ChatPanel 把
   *  handleRegenerate(replyId) 闭包注入此处。未传（undefined）→ 操作栏的「重新生成」按钮
   *  disabled + tooltip「重新生成（开发中）」兜底（如 finalized/execute-announce 气泡无 reply_id）。
   *  传则渲染 AntD Button（type=text + ReloadOutlined），点击调用。语义：重新触发该气泡对应回复
   *  的生成流程——后端回查该回复的 reply_id，取其前最近 user_input 作原 prompt 重发，新回复经
   *  现有 WS 事件流到达（历史回复不删，regenerate 是追加）。 */
  onRegenerate?: () => void
  /** [需求2-后端] 「重新生成」按钮 loading 态——regeneratingReplyIds.has(reply_id) 时转 loading。
   *  父组件注入：点按钮时把 reply_id 加入集合，请求返回后清。loading 期间按钮转菊花禁用点击，
   *  防连点重复触发。未传 → 不显示 loading（按需注入，finalized/无 reply_id 气泡不需 loading）。 */
  regenerating?: boolean
  /** 持久化气泡回放复用 ChatMessageBubble 时，调用方若自带 actionGroup（hover 顶部操作按钮组），
   *  可传 true 抑制 footer 内置的「复制 + 重新生成」操作栏，避免与 actionGroup 重复。
   *  - 流式气泡 / finalized 气泡不传（默认 false）→ footer 按 (onRegenerate || content) 条件渲染，
   *    原行为零变。
   *  - 持久化气泡传 true + actionGroup → 操作按钮全归 actionGroup（hover 顶部），footer 只渲染产物卡。 */
  hideFooterAction?: boolean
  /** 持久化气泡复用 ChatMessageBubble 时，把「追问引导 chip」等底部附加内容渲染在 statusLine 之后、
   *  chat-bubble-wrap 内部（与原手写持久化气泡的视觉位置一致——气泡外、wrap 内）。
   *  流式 / finalized 气泡不传（undefined → 不渲染），原行为零变。 */
  footerExtra?: React.ReactNode
}

/** 单条 task_tool 事件 → 摘要行数据。 */
interface ToolRow {
  key: string
  /** 工具名（run_command / write_file / ...），来自 data.name。 */
  name: string
  /** start 阶段的参数摘要（data.args，已 stringify + 截断），完成态也带（从配对 start 取）。
   *  渲染层完成态与执行中都显示（让用户一眼看到「执行了什么命令」，而非只 run_command + 耗时）。 */
  argsPreview: string
  /** 该工具调用的完整参数（data.args，未截断）——「详情」Popover 点开看完整命令用。
   *  完成态从配对的 start 取；孤儿 start 从自身取。 */
  args: unknown
  /** 该工具调用的输出（data.output，仅 end 阶段有）——「详情」Popover 点开看返回结果用。 */
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
  finalStats,
  isFailed = false,
  isUser = false,
  renderContent,
  statusLine,
  actionGroup,
  onRegenerate,
  regenerating,
  hideFooterAction = false,
  footerExtra,
}: ChatMessageBubbleProps) {
  // 工具调用整组折叠（外层 Collapse「🔍 执行过程详情」）——展开策略：
  //  · 流式中（isStreaming=true）默认展开（让用户看见思考/工具过程实时流出）；
  //  · 回答完成（isStreaming=false）默认展开（持久化气泡 mount 即完成态，过程详情一目了然）。
  // 懒初始化按 isStreaming 决定（避免历史消息回显首帧 flash）；完成后由 autoExpandOnDone effect 接管。
  // 重命名 toolBlockExpanded → processPanelExpanded（语义从「工具折叠」扩为「过程详情面板」，
  // 因为现在三块合并成一个统一面板）。受控机制 + 用户手动 toggle 5s 内不覆盖语义保留。
  const [processPanelExpanded, setProcessPanelExpanded] = useState(() => !isStreaming)
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

  // toolEvents → 按时间序的摘要行（start/end 配对成「一个工具调用 = 一个 row」）。
  // LIFO 配对：end 弹出最近同名未配对 start 合并成一个 row（isEnd=true + elapsedMs + args 来自
  // start + payload 来自 end.output）。嵌套同名调用按内层先闭合；clamp 0 防时钟倒序产生负值。
  // 孤儿 start（流式期 end 未到 / trace 缺 end）→ 单独产 🔄 执行中 row。这样完成态每条工具调用
  // 只出现一次（✅ + 耗时 + 参数 + 👁️输出），不会 start🔄 / end✅ 重复两条（原每事件一行的回归）。
  const toolRows = useMemo<ToolRow[]>(() => {
    const sorted = [...toolEvents].sort((a, b) => a.timestamp - b.timestamp)
    const pendingStacks: Record<string, TraceEvent[]> = {}
    const rows: ToolRow[] = []
    for (const e of sorted) {
      const data = (e.data || {}) as Record<string, unknown>
      const isEnd = data['phase'] === 'end'
      const name = String(data['name'] || '(unknown)')
      if (isEnd) {
        const stack = pendingStacks[name]
        const startEv = stack && stack.length > 0 ? stack.pop()! : null
        const startData = startEv ? ((startEv.data || {}) as Record<string, unknown>) : null
        rows.push({
          key: e.id,
          name,
          argsPreview: startData ? toPreview(startData['args']) : '',
          // 完成态也带 args（从配对 start 取）——渲染层需显命令参数，否则完成态只显 run_command+耗时，
          // 看不到「执行了什么」。args 是未截断完整参数，argsPreview 是截断预览。
          args: startData ? startData['args'] : undefined,
          payload: data['output'],
          isEnd: true,
          elapsedMs: startEv ? Math.max(0, e.timestamp - startEv.timestamp) : undefined,
          timestamp: startEv ? startEv.timestamp : e.timestamp,
        })
      } else {
        ;(pendingStacks[name] || (pendingStacks[name] = [])).push(e)
      }
    }
    // 剩余未配对的 start（流式期 end 未返回）→ 🔄 执行中 row
    for (const name of Object.keys(pendingStacks)) {
      for (const startEv of pendingStacks[name]) {
        const data = (startEv.data || {}) as Record<string, unknown>
        rows.push({
          key: startEv.id,
          name: String(data['name'] || '(unknown)'),
          argsPreview: toPreview(data['args']),
          args: data['args'],
          payload: data['args'],
          isEnd: false,
          timestamp: startEv.timestamp,
        })
      }
    }
    rows.sort((a, b) => a.timestamp - b.timestamp)
    return rows
  }, [toolEvents])

  const toggleProcessPanel = () => {
    setProcessPanelExpanded((v) => !v)
  }

  const hasTools = toolRows.length > 0
  const hasContent = content && content.length > 0
  const hasReasoning = !!(reasoning && reasoning.length > 0)
  // 需求2-前端：content 内的 ```card``` 围栏块数（无卡片=0，纯散文回复）。流式期 content 逐字
  // 增长，每帧重算 parseCards 是 O(content.length) 扫描——但内容普遍 <2KB（卡片占大头，纯文本不长），
  // 且 memo 历史 content 不变的气泡后，重算只在当前流式气泡，可接受（与 HighlightMessage 同款判断）。
  // hasCards 让「有卡片但正文散文被卡片挤到空」的回复仍渲染（content 有值但 prose 段为空时，
  // splitContentByCards 会只产卡片段，text 段过滤掉——hasCards 保底 contentRender 仍走卡片分支）。
  const contentSegments = useMemo(
    () => splitContentByCards(content || ''),
    [content],
  )
  const hasCards = contentSegments.some((s) => s.type === 'card')

  // 思考折叠展开策略（已简化）：
  //  · 思考活跃期（isStreaming && reasoning && 正文未流出）→ 展开全文，让用户看见实时思考逐字流出；
  //  · 其他时候（正文开始流 / 已定稿 / execute 轮定稿后回顾）→ 折叠成单行摘要，由 antd
  //    Typography.Paragraph ellipsis 接管「行尾展开图标」交互，不再手搓 click toggle + slice 截断。
  //
  // 这替换了旧逻辑里 reasoningExpanded / userToggledAt / thinkActiveKeys / userExpandedThinks /
  // userCollapsedThinks / onThinkCollapseChange 一整套手搓受控折叠态——antd Paragraph ellipsis
  // 原生支持「rows:1 + expandable:'icon' + onEllipsis」，自动截断 + 出展开图标，更干净。
  const reasoningActive = isStreaming && hasReasoning && !hasContent
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
        // 带出 timestamp 供 processItems 按真实时序与工具交错排序（ReAct 是 think→tool→think→tool
        // 交替，非「所有思考在前所有工具在后」）。单位毫秒，与 toolRows.timestamp 同。
        timestamp: e.timestamp,
      }
    })
  }, [thinkEvents])
  // 回答完成时自动展开过程详情面板（仅一次，不覆盖后续用户操作）。
  // 触发条件：!isStreaming（回答完成/历史消息回显）且 autoExpandOnDoneRef.current=false。
  // 首次进入完成态时 processPanelExpanded → true（思考折叠已交 antd Paragraph ellipsis 接管，
  // 不再需要 setReasoningExpanded / thinkActiveKeys 等手搓态）。
  // 流式→完成 transition：isStreaming 由 true 转 false 时 effect 重跑，首次 false 触发展开。
  // 历史消息回显：mount 时 isStreaming=false，effect 首跑即触发展开（同步 setState 在 commit 前生效）。
  const autoExpandOnDoneRef = useRef(false)
  useEffect(() => {
    if (!isStreaming && !autoExpandOnDoneRef.current) {
      autoExpandOnDoneRef.current = true
      setProcessPanelExpanded(true)
    }
  }, [isStreaming])
  // 流式前 ~200ms 首个 stats 未到时用 reasoning.length//3 临时估算（与后端 live_reasoning_tokens
  // 同启发式）。用 token 不用字符数——与状态行「↓ N tokens」同单位。
  const reasoningTokenLabel =
    reasoningTokens && reasoningTokens > 0
      ? reasoningTokens
      : Math.max(1, Math.ceil((reasoning?.length || 0) / 3))
  // 既无工具也无内容也无推理也无思考也无产物且非流式 → 不该渲染气泡（父组件应已过滤，此为防御兜底）
  if (!hasTools && !hasContent && !hasReasoning && !hasThinks && !hasArtifacts && !isStreaming) return null

  // ─────────────────────────────────────────────────────────────────────────
  // 统一「🔍 执行过程详情」面板：把原三块（reasoning Collapse + think ThoughtChain +
  // tool Collapse+Timeline）合并成一个 Timeline，按事件时序升序串起。每条事件一个
  // Timeline item：
  //   · dot   = antd Outlined 小号图标 + 状态色（BulbOutlined 思考琥珀 / ToolOutlined 工具
  //             成功绿·执行中琥珀 / CheckCircleOutlined 最终生成蓝），不用 emoji 堆叠。
  //   · color = 与 dot 同步的状态色字符串（思考 '#faad14' / 工具 isEnd?'#52c41a':'#faad14' /
  //             最终 '#1677ff'），让 Timeline 轴线段着色与 dot 一致。
  //   · children = 一行轻量内容：Space + Typography.Text + Tag + Popover，
  //             不用 Descriptions bordered 表格（多工具嵌多表视觉臃肿）。
  //   · 思考项正文用 Typography.Paragraph ellipsis={{rows:1, expandable:'icon'}} 原生折叠，
  //             不再手搓 chat-process-summary div + onClick + slice(0,60) 截断。
  //
  // 事件来源（三态）：
  //   · 协调者 reasoning 字符串 → 单个思考 item（无 per-event timestamp，时间戳用气泡 timestamp）
  //   · worker thinkEvents → 每条 think 一个思考 item（有 timestamp，phase 区分思考/结论）
  //   · toolEvents start/end 配对 → 每个工具调用一个工具 item（已配对算 elapsedMs）
  //   · 完成态 !isStreaming && hasContent → 一个「最终生成」item（仅元数据，不重复正文）
  //
  // 排序：按事件 timestamp 升序——协调者 reasoning 无 timestamp，用 0（最早位）；
  // worker think 各自带 timestamp；tool start/end 配对后用 start 时间。最终生成放最末（Infinity）。
  // 同 timestamp 的事件保持稳定顺序（思考在前、工具在后），不强行二次排序避免抖动。
  // ─────────────────────────────────────────────────────────────────────────
  type ProcessItem = {
    /** 排序键（时间戳，秒级）。协调者 reasoning 无 timestamp 用 0（最早）。最终生成用 Infinity（最末）。 */
    sortKey: number
    /** Timeline item 唯一 key（React 列表 key）。 */
    itemKey: string
    /** Timeline dot 节点（antd Outlined 小号图标，状态色 inline）。 */
    dot: React.ReactNode
    /** Timeline color（思考琥珀 / 工具成功绿·执行中琥珀 / 最终生成蓝）。 */
    color: string
    /** Timeline children 节点（Space+Typography.Text+Tag+Popover 一行轻量）。 */
    children: React.ReactNode
  }

  const processItems: ProcessItem[] = []

  // ── 1. 思考阶段 item ──
  // 协调者气泡用 reasoning 字符串（无 per-event timestamp）；worker 气泡用 thinkEvents 每条一个 item。
  // 流式活跃期（isActive）→ Paragraph ellipsis={false} 展开全文逐字流出；
  // 完成态 → Paragraph ellipsis={{rows:1, expandable:'icon'}} 让 antd 接管折叠展开。
  if (hasReasoning) {
    // 协调者 reasoning：单 item。
    const isActive = reasoningActive
    processItems.push({
      sortKey: 0,
      itemKey: 'reasoning',
      dot: <BulbOutlined style={{ color: '#faad14', fontSize: 12 }} />,
      color: '#faad14',
      children: (
        <div className="chat-process-item">
          <Space size={6} style={{ marginBottom: 2 }}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              思考
            </Typography.Text>
            <Tag color="orange" bordered={false} style={{ fontSize: 11 }}>
              {reasoningTokenLabel} tokens
            </Tag>
            {isActive && <Badge status="processing" />}
          </Space>
          <Typography.Paragraph
            ellipsis={isActive ? false : { rows: 1, expandable: 'collapsible' }}
            style={{ margin: 0, fontSize: 12, color: '#595959' }}
          >
            {reasoning}
          </Typography.Paragraph>
        </div>
      ),
    })
  }

  if (hasThinks) {
    // worker think：每条 think 一个 item，按时间序。流式最后一条 isActive 展开全文，
    // 其余完成态由 antd Paragraph ellipsis 接管折叠。
    const sorted = [...thinkRows]
    sorted.forEach((row, idx) => {
      const isActive = isStreaming && idx === sorted.length - 1
      processItems.push({
        // 用 think 事件的真实 timestamp（毫秒）做 sortKey——ReAct 是 think→tool→think→tool
        // 交替，think 项应按真实发生时间与工具项交错排，而非全堆在最前。
        sortKey: row.timestamp || 0,
        itemKey: `think-${row.key}`,
        dot: <BulbOutlined style={{ color: '#faad14', fontSize: 12 }} />,
        color: '#faad14',
        children: (
          <div className="chat-process-item">
            <Space size={6} style={{ marginBottom: 2 }}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {row.phase === 'final' ? '结论' : '思考'}
              </Typography.Text>
              <Tag color="orange" bordered={false} style={{ fontSize: 11 }}>
                {row.text.length} 字
              </Tag>
              {isActive && <Badge status="processing" />}
            </Space>
            <Typography.Paragraph
              ellipsis={isActive ? false : { rows: 1, expandable: 'collapsible' }}
              style={{ margin: 0, fontSize: 12, color: '#595959' }}
            >
              {row.text}
            </Typography.Paragraph>
          </div>
        ),
      })
    })
  }

  // ── 2. 工具调用 item（复用 toolRows useMemo 计算：start/end LIFO 配对算耗时）──
  // 一行轻量：工具名(code) + 状态Tag(success/processing) + 详情Link(Popover) + 参数预览(secondary ellipsis)。
  // 不用 Descriptions bordered 表格——多工具嵌多表视觉臃肿。
  toolRows.forEach((row) => {
    processItems.push({
      sortKey: row.timestamp || 0,
      itemKey: `tool-${row.key}`,
      dot: <ToolOutlined style={{ color: row.isEnd ? '#52c41a' : '#faad14', fontSize: 12 }} />,
      color: row.isEnd ? '#52c41a' : '#faad14',
      children: (
        <div className="chat-process-item">
          <Space size={6} wrap>
            <Typography.Text code style={{ fontSize: 12 }}>{row.name}</Typography.Text>
            <Tag color={row.isEnd ? 'success' : 'processing'} bordered={false} style={{ fontSize: 11 }}>
              {row.isEnd ? formatElapsed(row.elapsedMs ?? 0) : '执行中'}
            </Tag>
            {/* 命令参数预览：完成态（isEnd）也从配对 start 的 args 取，执行中从自身 args 取。
                截断 + ellipsis 防撑爆气泡，让用户一眼看到「执行了什么」（如 curl 'https://...'）。 */}
            {row.argsPreview && (
              <Typography.Text type="secondary" ellipsis style={{ fontSize: 11, maxWidth: 320 }} title={toPreview(row.args, 500)}>
                {row.argsPreview}
              </Typography.Text>
            )}
            {/* 「详情」Popover：完成态含「参数 + 输出」两段（args 来自 start、output 来自 end）；
                执行中只有「参数」一段（output 未返回）。点开看完整 payload，不撑爆行。 */}
            {(row.args != null || row.payload != null) && (
              <Popover
                placement="rightTop"
                title={row.isEnd ? '工具调用详情' : '参数'}
                content={
                  <div className="chat-tool-payload-wrap">
                    {row.args != null && (
                      <>
                        <div className="chat-tool-payload-label">参数</div>
                        <pre className="chat-tool-payload">
                          {typeof row.args === 'string'
                            ? row.args
                            : JSON.stringify(row.args, null, 2)}
                        </pre>
                      </>
                    )}
                    {row.payload != null && (
                      <>
                        <div className="chat-tool-payload-label">输出</div>
                        <pre className="chat-tool-payload">
                          {typeof row.payload === 'string'
                            ? row.payload
                            : JSON.stringify(row.payload, null, 2)}
                        </pre>
                      </>
                    )}
                  </div>
                }
                trigger="click"
                overlayClassName="chat-tool-payload-popover"
              >
                <Typography.Link style={{ fontSize: 11 }}>详情</Typography.Link>
              </Popover>
            )}
          </Space>
        </div>
      ),
    })
  })

  // ── 3. 最终生成 item（仅完成态 !isStreaming && hasContent && finalStats）──
  // 只放元数据：字数 + [可选] tokens/耗时/model/推理 tokens。不重复渲染 content 正文（正文在 Bubble.content 里）。
  // 用 antd Divider type="vertical" 做段间分隔，不用 emoji。
  if (!isStreaming && hasContent && finalStats) {
    processItems.push({
      sortKey: Infinity,
      itemKey: 'final-generation',
      dot: <CheckCircleOutlined style={{ color: '#1677ff', fontSize: 12 }} />,
      color: '#1677ff',
      children: (
        <div className="chat-process-item">
          <Space size={8} split={<Divider type="vertical" style={{ margin: 0 }} />}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>最终生成</Typography.Text>
            <Typography.Text style={{ fontSize: 12 }}>{content.length} 字</Typography.Text>
            {finalStats.elapsedMs != null && finalStats.elapsedMs > 0 && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>{formatElapsed(finalStats.elapsedMs)}</Typography.Text>
            )}
            {finalStats.tokens != null && finalStats.tokens > 0 && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>↓ {finalStats.tokens}</Typography.Text>
            )}
            {finalStats.reasoningTokens != null && finalStats.reasoningTokens > 0 && (
              <Typography.Text style={{ fontSize: 12, color: '#faad14' }}>含 {finalStats.reasoningTokens} 推理</Typography.Text>
            )}
            {finalStats.model && (
              <Typography.Text style={{ fontSize: 12, color: '#722ed1' }}>{finalStats.model}</Typography.Text>
            )}
          </Space>
        </div>
      ),
    })
  }

  // 按 sortKey 升序稳定排序（思考在前 0、工具按 timestamp、📝 在末 Infinity）。
  // 稳定排序保序：相同 sortKey 的项保持插入顺序（思考在前、think 在 reasoning 之后）。
  processItems.sort((a, b) => a.sortKey - b.sortKey)

  const hasProcessItems = processItems.length > 0

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
          contentRender={(c) => {
            // 需求2-前端：content 含 ```card``` 围栏块时，按段切——散文段走 markdown 渲染（renderMarkdown），
            //  卡片段走 StructuredCard（围栏逻辑零改动）。无卡片时走原路径（renderContent 兜底 / markdown）。
            //  流式光标：仅当存在散文尾部段时追加到末尾散文后；纯卡片或空散文时追加到整段末尾。
            //  Markdown 接入：原 fallback `String(c)` 改为 `renderMarkdown(String(c))`——LLM 输出的
            //  `##` / `**` / `-` / ``` ``` ``` / `>` 等 markdown 符号不再裸奔直出。
            //  renderContent prop 保留兼容（未来 @mention 高亮等可注入），默认 fallback 走 markdown。
            //  用户气泡（isUser）：ChatPanel 的持久化用户气泡仍直出 content（见 ChatPanel.tsx isUser 分支）；
            //  本组件 ChatMessageBubble 主要服务于 AI 气泡（streaming/finalized/coordinator），
            //  统一走 markdown——若父组件传 isUser=true 也走 markdown（用户若输入 `**bold**` 也会被渲染，
            //  合理行为，不强分 AI/用户）。
            if (!hasCards) {
              return (
                <div className={hasTools ? 'chat-bubble-content' : undefined}>
                  {renderContent ? renderContent(String(c)) : renderMarkdown(String(c))}
                  {isStreaming && <span className="chat-streaming-cursor" />}
                </div>
              )
            }
            return (
              <div className={hasTools ? 'chat-bubble-content' : undefined}>
                {contentSegments.map((seg, i) => {
                  if (seg.type === 'text') {
                    // 散文段：末尾段（最后一段 text）流式时追光标
                    const isLast = i === contentSegments.length - 1
                    return (
                      <span key={`seg-${i}`} className="chat-card-prose">
                        {renderContent ? renderContent(seg.text) : renderMarkdown(seg.text)}
                        {isStreaming && isLast && <span className="chat-streaming-cursor" />}
                      </span>
                    )
                  }
                  return <StructuredCard key={`seg-${i}`} card={seg.card} />
                })}
                {/* 全是卡片段、无尾部散文时，流式光标兜底放末尾 */}
                {isStreaming && contentSegments.every((s) => s.type === 'card') && (
                  <span className="chat-streaming-cursor" />
                )}
              </div>
            )
          }}
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
            hasProcessItems ? (
              <div className="chat-tool-block">
                <Collapse
                  size="small"
                  ghost
                  activeKey={processPanelExpanded ? ['process'] : []}
                  onChange={() => toggleProcessPanel()}
                  items={[{
                    key: 'process',
                    label: (
                      <span style={{ color: '#8c8c8c', fontSize: 12, display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                        🔍 执行过程详情 ({processItems.length})
                      </span>
                    ),
                    children: (
                      <Timeline
                        className="chat-process-timeline"
                        items={processItems.map((item) => ({
                          key: item.itemKey,
                          color: item.color,
                          dot: item.dot,
                          children: item.children,
                        }))}
                      />
                    ),
                  }]}
                />
              </div>
            ) : undefined
          }
          footer={
            /* ST-06（task 21 数据管道 / task 22 渲染）：worker 任务产物下载卡。
               task_complete 事件 data.artifact.files[]（bus.py emit_task_completed 仅成功路径透传
               scan_workspace_artifacts manifest）经 ChatPanel.finalizedBubbles 提取 → artifactFiles
               prop 传入。每文件一张小卡：按扩展名图标 + 文件名（截断 + tooltip 全 path）+ 大小 +
               下载按钮。位置在正文之下（产物是任务收尾后产出）；失败/取消/超时任务无 artifact（后端不
               透传），失败气泡自然无下载卡（语义正确——失败不留可用产物）。

               需求2-前端（操作栏）：当 onRegenerate 传入或 content 非空（可复制）时，footer 同时
               承载操作栏（复制 + 重新生成）。复制复用 BubbleCopyButton（与 ChatPanel hover 操作组
               同款）；「重新生成」仅当父组件注入 onRegenerate 时渲染（后端 regenerate 端点未就绪前
               父组件不传，按钮自然不出现——不会留空响应占位）。
               流式生成中（isStreaming=true）不显示操作栏——内容还在变，复制/重生成都无意义；
               仅完成态（isStreaming=false）且非用户气泡（isUser=false，用户自己的消息不需重生成）
               才显示。产物下载卡与操作栏可共存（先产物后操作栏，竖排）。 */
            hasArtifacts || (!isStreaming && !isUser && (onRegenerate || content) && !hideFooterAction) ? (
              <>
                {hasArtifacts && (
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
                )}
                {!isStreaming && !isUser && (onRegenerate || content) && !hideFooterAction && (
                  <div className="chat-action-bar">
                    <BubbleCopyButton content={content} />
                    {/* 「重新生成」：onRegenerate 传入（后端 regenerate 端点就绪后 ChatPanel 注入）→
                        可点；未传入 → disabled + tooltip 标记「开发中」（finalized/execute-announce
                        气泡无 reply_id 时 disabled 兜底）。始终渲染按钮本体——满足「新增重新生成
                        Button」契约，禁用态明确传达「暂未支持」而非留空占位。
                        [需求2-后端]：onRegenerate 闭包调 messageApi.regenerate(replyId)，后端回查
                        data.reply_id → 取前最近 user_input 重发 → 新回复经 WS 事件流到达。
                        regenerating=true 时按钮转菊花禁用防连点。 */}
                    <Tooltip title={onRegenerate ? '重新生成' : '重新生成（开发中）'}>
                      <Button
                        type="text"
                        size="small"
                        className="chat-action-regenerate"
                        icon={<ReloadOutlined />}
                        disabled={!onRegenerate || regenerating}
                        loading={regenerating}
                        onClick={onRegenerate}
                      />
                    </Tooltip>
                  </div>
                )}
              </>
            ) : undefined
          }
        />
        <div className={`chat-timestamp ${isUser ? 'chat-timestamp--right' : ''}`}>
          {new Date(timestamp).toLocaleTimeString()}
        </div>
        {statusLine && <div className="chat-status-line">{statusLine}</div>}
        {footerExtra}
      </div>
    </div>
  )
}

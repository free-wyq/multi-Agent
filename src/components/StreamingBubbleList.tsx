import { useMemo } from 'react'
import {
  type AgentDefinition,
  type AgentStatusInfo,
  type CoordStats,
  type Conversation,
  type Group,
  type TraceEvent,
} from '../services/api'
import ChatMessageBubble, { type ArtifactFile } from './ChatMessageBubble'
import { ChatAvatar, extractFinalizedArtifacts } from './PersistentBubbleList'
import BubbleSpeakButton from './BubbleSpeakButton'
import BubbleCopyButton from './BubbleCopyButton'

/**
 * StreamingBubbleList — [任务9b] 流式 + 协调者流式 + 定稿过渡气泡列表。
 *
 * 职责单一：渲染 ChatPanel 的三条「非持久化」气泡路径——
 *  1. streamingBubbles（worker 流式）：executing agent 的 streaming[task_id] 逐字渲染。
 *  2. coordinatorStreamingBubbles（协调者流式）：coordinator_token 按 reply_id 累积的
 *     delta + coordinator_stats 状态行（"model · Ns · ↓ N tokens（含 N 推理）· thinking"）。
 *  3. finalizedBubbles（定稿过渡）：task_complete/failed 收尾后持久化回复落地前的过渡气泡，
 *     持久化回复（_reply）落地后据 repliedTaskIds.has(task_id) 自动退场。
 *
 * 从 ChatPanel.tsx 抽出（9a 已抽走持久化气泡 flatMap）。纯展示 + 自管三条 memo，
 * 数据全由父注入（agentStatuses/streaming/coordStreaming/coordReasoning/coordStats/
 * events/chatMessages/repliedTaskIds/agents/group/ttsEnabled）。与 PersistentBubbleList
 * 「纯展示、props 注入」约定一致。
 *
 * 三条 memo 本应各是 ChatPanel 内联 const——抽到本组件统一持有：toolEventsByTask /
 * thinkEventsByTask（events 按 taskId 归并，三条气泡各取对应行，useMemo 稳引用）+ finalizedBubbles
 * （task_complete/failed → FinalizedBubble[]，退场判定读 repliedTaskIds state）。
 *
 * 行为零变：所有渲染逻辑、注释、样式 class 逐字从 ChatPanel 搬来，仅把闭包捕获的
 * ChatPanel state/context 改成 props 传入。退场 state (repliedTaskIds) 由 useBubbleRetire
 * hook 持有，markReplied/reset 由 ChatPanel logs effect/切群 effect 调用（hook 真源在
 * ChatPanel，repliedTaskIds 透传进本组件只读）。
 */
export interface StreamingBubbleListProps {
  /** 当前会话 id（无群组时空，本组件返 null）。 */
  chatGroupId: string | null
  /** 当前会话（群/单聊）——coordinatorStreamingBubbles 取 group?.coordinator_id 兜底 senderId。 */
  group: Group | Conversation | null
  /** 全部智能体（头像角色色 + 发送者名解析）。 */
  agents: AgentDefinition[]
  /** agent 状态表（streamingBubbles 据 executing + current_task_id 过滤）。 */
  agentStatuses: Record<string, AgentStatusInfo>
  /** worker 流式缓冲 streaming[task_id]（PL-08 逐字增量拼接）。 */
  streaming: Record<string, string>
  /** 协调者流式缓冲 coordStreaming[reply_id].{content,senderId}。 */
  coordStreaming: Record<string, { content: string; senderId: string }>
  /** 协调者推理缓冲 coordReasoning[reply_id]（推理模型流出的内部思维链）。 */
  coordReasoning: Record<string, string>
  /** 协调者流式统计 coordStats[reply_id]（状态行渲染）。 */
  coordStats: Record<string, CoordStats>
  /** 全局 TraceEvent 流（cap 500），按 taskId 归并 tool/think 行挂到流式/定稿气泡。 */
  events: TraceEvent[]
  /** 已落库的持久化消息（finalizedBubbles 兜底退场分支读 chatMessages 时间戳比较）。 */
  chatMessages: { sender_id: string; created_at: string }[]
  /** 已退场定稿气泡的 task_id 集合（useBubbleRetire 持有，finalizedBubbles 据此判退场）。 */
  repliedTaskIds: Set<string>
  /** TTS 是否启用（定稿气泡 hover 操作组显朗读按钮）。 */
  ttsEnabled: boolean
}

/** ST-02 worker 流式气泡数据：executing agent 的 streaming[task_id] 逐字渲染。 */
export interface StreamingBubble {
  agentId: string
  agentName: string
  taskId: string
  content: string
}

/** 协调者流式气泡数据：coordinator_token 按 reply_id 累积。 */
export interface CoordinatorStreamingBubble {
  replyId: string
  content: string
  senderId: string
  reasoning: string
  stats: CoordStats | undefined
  toolEvents: TraceEvent[]
  thinkEvents: TraceEvent[]
}

/** ST-04 定稿气泡数据：task_complete/failed 收尾后持久化回复落地前的过渡气泡。 */
export interface FinalizedBubble {
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
   *  失败气泡自然无下载卡（语义正确，失败不留产物）。 */
  artifactFiles: ArtifactFile[]
}

/**
 * 渲染流式 + 协调者流式 + 定稿过渡气泡。原 ChatPanel 三段 .map 块逐字搬来——
 * 行为零变（ST-02 / 协调者流式 / ST-04 三段注释 + 样式 + 字段全保留）。
 */
export default function StreamingBubbleList({
  chatGroupId,
  group,
  agents,
  agentStatuses,
  streaming,
  coordStreaming,
  coordReasoning,
  coordStats,
  events,
  chatMessages,
  repliedTaskIds,
  ttsEnabled,
}: StreamingBubbleListProps) {
  // ST-02：流式 token 接入聊天气泡逐字渲染。
  // BusEventContext.streaming[task_id] 是 PL-08 逐字增量拼接的「正在生成」缓冲。
  // 对每个 executing 且有 current_task_id 的 agent，若其 task 缓冲非空，在消息流末尾追加一条
  // 流式气泡（ChatMessageBubble isStreaming=true），content=streaming[taskId]，尾部闪烁光标。
  // 已被 logs 收尾事件（task_complete/failed/dispatch，见 useBusEvent 清缓冲逻辑）收编为持久
  // 气泡的 task 不再展示流式气泡——streaming[tid] 被清空后 streamings 自然过滤掉。
  // 多 agent 同时执行时各占一条流式气泡（按 agentStatuses 顺序）。
  const streamingBubbles: StreamingBubble[] = chatGroupId
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

  // ST-03：task_tool 事件接入聊天气泡——按 task 聚合工具摘要行。
  // events 是全局 TraceEvent 流（useBusEvent cap 500），按 taskId 分组 kind==='tool'
  // 事件；流式气泡按其 current_task_id 取对应工具行，渲染在气泡顶部（ChatMessageBubble
  // toolEvents）。task 与执行 worker 1:1，按 taskId 过滤即该 agent 当前任务的全部工具调用。
  // useMemo 稳住引用：task_tool 远少于 task_token，但分组仍 memo 避免每帧重算波及子组件。
  // vh63：声明顺序在 coordinatorStreamingBubbles 之前——后者按 reply_id 取本表挂 ReAct 工具行
  // 到单聊一个 turn 一个气泡，const TDZ 要求先声明后引用（否则 ReferenceError）。
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

  // 协调者流式气泡：coordinator_token 按 reply_id 累积的 delta，配合 coordinator_stats
  // 渲染 Claude-Code 风格状态行（"model · Ns · ↓ N tokens（含 N 推理）· thinking"）。
  // 与 worker 流式气泡区别：协调者无 task_id（不经 create_react_agent），按 reply_id 归并。
  // Bug A：senderId 从 coordStreaming[reply_id].senderId 取（事件携带，coordinator_token→
  // coordinator_id；worker 单聊/脑回路 task_token→worker agent_id），不再硬编码「群主(协调者)」。
  // 缺省回退 coordinator（防事件缺 sender_id 的陈旧/异常路径）。
  // phase="done" 时 useBusEvent 清空 coordStreaming[reply_id] → 气泡自然退场，
  // 由随后落地的持久化 agent_reply 接管（同 worker streaming→finalized 模式）。
  // reasoning 取 coordReasoning[reply_id]——推理模型在可见 content 前流出的内部思维链，
  // 传给 ChatMessageBubble 渲染默认折叠的「思考过程」区，用户可自行展开/收起。
  // vh63：execute ReAct 的工具调用 + 中间思考也按 reply_id 归并到同一气泡
  // （后端 _run_worker_task on_log 用 turn_reply_id=brain reply_id 作 task_id 槽），
  // 故这里取 toolEventsByTask[replyId]/thinkEventsByTask[replyId] 挂到气泡上——
  // 一个 turn 一个气泡：思考过程 + 执行步骤 + 最终答案全收。
  const coordinatorStreamingBubbles: CoordinatorStreamingBubble[] = chatGroupId
    ? Object.entries(coordStreaming).map(([replyId, entry]) => ({
        replyId,
        content: entry.content,
        senderId: entry.senderId || group?.coordinator_id || 'coordinator',
        reasoning: coordReasoning[replyId] || '',
        stats: coordStats[replyId],
        toolEvents: toolEventsByTask[replyId] || [],
        thinkEvents: thinkEventsByTask[replyId] || [],
      }))
    : []

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
  // 事件，前端 ChatPanel logs 桥接把 log.taskId 回填到 chatMessages 的
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
  const finalizedBubbles: FinalizedBubble[] = useMemo(() => {
    const out: FinalizedBubble[] = []
    const seen = new Set<string>()
    // 缺陷5 修复：退场判定读 repliedTaskIds state（reply 落地 effect setRepliedTaskIds 回填的
    // task_id 集合），并把它纳入 memo deps——reply 落地触发 setRepliedTaskIds → 重渲染 →
    // memo 重算 → repliedTaskIds.has(e.taskId) 命中 → finalized bubble 退场。修掉了 B23
    // ref 时序 bug（emit_task_completed 先于 _reply announce，ref 在 commit 后回填，memo
    // 在 render 期算时 ref 仍空 → finalized 卡住不退场）。集合查 O(1) vs chatMessages.some
    // O(n)。reload-safe：切群/重连回灌从 DB 拉 chatMessages 时，logs effect 重扫历史
    // agent_reply 也 setRepliedTaskIds（历史 agent_reply 带 task_id 同样入集合），故 reload
    // 后退场状态与 live 一致。
    for (const e of events) {
      if (e.kind !== 'complete' && e.kind !== 'failed') continue
      if (!e.taskId || seen.has(e.taskId)) continue
      seen.add(e.taskId)
      // 仍在流式（缓冲未清）→ 流式气泡自己渲染，不定稿
      if (streaming[e.taskId]) continue
      // 持久化回复已落地 → 已被替换，不再渲染定稿气泡。
      // B22：主路径按 task_id 精确匹配（repliedTaskIds.has，reload-safe，不依赖时钟同步）。
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
  }, [events, streaming, agentStatuses, repliedTaskIds, chatMessages])
  // 缺陷5 修复：deps 加回 repliedTaskIds（reply 落地 setRepliedTaskIds 触发重算退场）+
  // chatMessages（兜底时间戳分支需读最新 chatMessages）。B23 原把 chatMessages 移出 deps 避
  // 高频重算，但那是为配 ref-as-truth；既改回 state，repliedTaskIds 必须进 deps 才能在 reply
  // 落地时重算退场（这正是 B23 ref 方案卡住不退场的根因）。chatMessages 也须进 deps：兜底分支
  // 读它做时间戳比较，若不进 deps 会在 chatMessages 更新后仍读到旧引用 → 兜底退场失效。perf
  // 可接受：chatMessages 每条新消息确实触发重算，但 finalizedBubbles memo 主体是 O(events)
  // 遍历（events cap 500，且只在 kind=complete/failed 时入 out），单次 <1ms；高频聊天的 perf
  // 瓶颈在持久化气泡渲染而非本 memo（见 [[chat-lag-jitter-fix-2026-07-23]]）——持久化气泡现
  // 复用 ChatMessageBubble（任务4），其 props 浅比较可短路历史气泡重渲染。

  return (
    <>
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
        // Bug A：senderId/名/头像从事件携带的 senderId 解析（coordinator_token→coordinator_id；
        // worker 单聊/脑回路 task_token→worker agent_id），不再硬编码「群主(协调者)」。
        // coordinatorId 气泡（id===group.coordinator_id 或 fallback 'coordinator'）显「群主(协调者)」，
        // 其他 id 走 SenderName 查 agents 取 agent 名——worker 推理流式由此冠到正确 worker 头像下。
        //
        // 单聊例外（Path C bug）：单聊 ConversationEntity 的 coordinator_id 镜像 agent_id
        // （即唯一 agent 自兼协调者），若仍按「senderId===coordinator_id → 群主(协调者)」判，
        // 该 agent 的脑回路/execute 流式会被误冠成「群主(协调者)」紫色头像，与同 agent 的
        // task_token/log 流（显本名）拆成两个气泡。单聊里只有一个 agent，不存在「协调者 vs
        // 成员」的区分——一律按 senderId 查 agents 取本名。Conversation 有 agent_id 字段、
        // Group 没有，据此区分单聊/群聊。
        const senderId = b.senderId
        const isSingleChat = !!(group && 'agent_id' in group)
        const isCoordinatorBubble =
          !isSingleChat &&
          (senderId === (group?.coordinator_id ?? 'coordinator') ||
            senderId === 'coordinator')
        const senderName = isCoordinatorBubble
          ? '群主(协调者)'
          : (agents.find((a) => a.id === senderId)?.name ?? senderId.slice(0, 8) + '...')
        return (
          <ChatMessageBubble
            key={`coord-streaming-${b.replyId}`}
            senderId={senderId}
            senderName={senderName}
            avatar={<ChatAvatar id={senderId} agents={agents} />}
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
            toolEvents={b.toolEvents}
            thinkEvents={b.thinkEvents}
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
              {ttsEnabled && <BubbleSpeakButton content={b.content} />}
            </div>
          }
        />
      ))}
    </>
  )
}

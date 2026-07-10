import { useEffect, useRef } from 'react'
import { Badge, Collapse, Empty, Tag } from 'antd'
import { useBusEvent } from '../hooks/useBusEvent'

interface WorkerTraceProps {
  agentId: string
  agentName: string
  groupId: string
}

/** 状态 → 徽标颜色 */
const STATUS_BADGE: Record<string, { status: 'success' | 'processing' | 'default' | 'error'; color: string; label: string }> = {
  idle: { status: 'success', color: '#52c41a', label: '空闲' },
  executing: { status: 'processing', color: '#1677ff', label: '执行中' },
  offline: { status: 'default', color: '#d9d9d9', label: '离线' },
  failed: { status: 'error', color: '#ff4d4f', label: '失败' },
}

interface ToolCard {
  key: string
  name: string
  phase: 'start' | 'end'
  content: string
  payload: unknown
  timestamp: number
}

/**
 * Worker 执行追踪面板：渲染单个智能体的工具卡片 + 思考文本。
 *
 * 工具卡片：filter events kind==='tool'，按时间序，每张卡含工具名、phase（start/end）、
 * 可折叠 args（output）。
 * 思考文本：filter kind in ('think','answer')，浅色块。
 */
export default function WorkerTrace({ agentId, agentName, groupId }: WorkerTraceProps) {
  const { events, agentStatuses } = useBusEvent(groupId)
  const containerRef = useRef<HTMLDivElement>(null)

  const status = agentStatuses[agentId]?.status || 'idle'
  const statusInfo = STATUS_BADGE[status] ?? STATUS_BADGE.idle

  // 工具事件按时间序
  const toolEvents = events.filter(
    (e) => e.kind === 'tool' && e.agentId === agentId,
  )

  // 思考/答案文本
  const thinkEvents = events.filter(
    (e) => (e.kind === 'think' || e.kind === 'answer') && e.agentId === agentId,
  )

  // 自动滚底
  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight
    }
  }, [toolEvents.length, thinkEvents.length])

  // 把 start/end 配对成 ToolCard
  const cards: ToolCard[] = toolEvents.map((e) => {
    const data = (e.data || {}) as Record<string, unknown>
    const phase: 'start' | 'end' = data['phase'] === 'end' ? 'end' : 'start'
    return {
      key: e.id,
      name: String(data['name'] || '(unknown)'),
      phase,
      content: e.content || '',
      payload: phase === 'start' ? data['args'] : data['output'],
      timestamp: e.timestamp,
    }
  })

  return (
    <div ref={containerRef} style={{ maxHeight: 520, overflowY: 'auto', paddingRight: 4 }}>
      {/* 状态徽标 */}
      <div style={{ marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
        <Badge status={statusInfo.status as any} />
        <span style={{ color: statusInfo.color, fontWeight: 600 }}>{statusInfo.label}</span>
        <Tag color="blue">{agentName}</Tag>
        {agentStatuses[agentId]?.current_task_id && (
          <span style={{ fontSize: 12, color: '#999' }}>
            task: {agentStatuses[agentId].current_task_id.slice(0, 8)}...
          </span>
        )}
      </div>

      {/* 工具卡片 */}
      {cards.length === 0 && thinkEvents.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="暂无执行记录"
          style={{ margin: '24px 0' }}
        />
      ) : (
        <>
          {cards.length > 0 && (
            <div style={{ marginBottom: 8, fontSize: 12, color: '#999', fontWeight: 600 }}>
              工具调用 ({cards.length})
            </div>
          )}
          <Collapse
            size="small"
            items={cards.map((c) => ({
              key: c.key,
              label: (
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span
                    style={{
                      display: 'inline-block',
                      width: 8,
                      height: 8,
                      borderRadius: '50%',
                      background: c.phase === 'start' ? '#52c41a' : '#bfbfbf',
                      flexShrink: 0,
                    }}
                  />
                  <span style={{ fontWeight: 600, fontFamily: 'monospace' }}>{c.name}</span>
                  <span style={{ fontSize: 11, color: '#999' }}>
                    {c.phase === 'start' ? '调用' : '返回'} ·{' '}
                    {new Date(c.timestamp).toLocaleTimeString()}
                  </span>
                </div>
              ),
              children: (
                <pre
                  style={{
                    background: '#1e1e1e',
                    color: '#c9d1d9',
                    padding: 8,
                    borderRadius: 4,
                    fontSize: 12,
                    fontFamily: 'monospace',
                    margin: 0,
                    overflowX: 'auto',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-all',
                    maxHeight: 200,
                    overflowY: 'auto',
                  }}
                >
                  {typeof c.payload === 'string'
                    ? c.payload
                    : JSON.stringify(c.payload, null, 2)}
                </pre>
              ),
            }))}
          />
        </>
      )}

      {/* 思考文本 */}
      {thinkEvents.length > 0 && (
        <>
          <div style={{ margin: '16px 0 8px', fontSize: 12, color: '#999', fontWeight: 600 }}>
            思考链 ({thinkEvents.length})
          </div>
          {thinkEvents.map((e) => {
            const data = (e.data || {}) as Record<string, unknown>
            const phase = String(data['phase'] || '')
            return (
              <div
                key={e.id}
                style={{
                  background: phase === 'final' ? '#f0f5ff' : '#fafafa',
                  borderLeft: `3px solid ${phase === 'final' ? '#1677ff' : '#d9d9d9'}`,
                  padding: '8px 12px',
                  borderRadius: 4,
                  marginBottom: 6,
                  fontSize: 13,
                  color: '#333',
                  lineHeight: 1.6,
                  whiteSpace: 'pre-wrap',
                }}
              >
                <span style={{ fontSize: 11, color: '#999', marginRight: 6 }}>
                  [{phase === 'final' ? '最终答案' : '思考'} ·{' '}
                  {new Date(e.timestamp).toLocaleTimeString()}]
                </span>
                {e.content || '(空)'}
              </div>
            )
          })}
        </>
      )}
    </div>
  )
}

import { Collapse, Empty, Tag, Timeline } from 'antd'
import { useBusEvent } from '../hooks/useBusEvent'
import type { PlanStep } from '../services/api'

interface LeaderPanelProps {
  groupId: string
}

/** 计划步骤状态 → 徽标 */
function stepBadge(status: string): { color: string; label: string } {
  switch (status) {
    case 'completed': return { color: 'green', label: '已完成' }
    case 'dispatched': return { color: 'blue', label: '已派发' }
    case 'failed': return { color: 'red', label: '失败' }
    case 'pending':
    default: return { color: 'default', label: '待执行' }
  }
}

/**
 * Leader（协调者）面板：展示思考链 + 协作计划 + 派工时间线。
 *
 * - 思考链：filter events kind==='coord_think'，Collapse 展开看 action/content
 * - 协作计划：plan 各步（agent_name → instruction + 状态徽标）
 * - 派工时间线：filter events kind in ('dispatch','complete','failed')，按 task_id 串
 */
export default function LeaderPanel({ groupId }: LeaderPanelProps) {
  const { events, plan } = useBusEvent(groupId)

  const thinkEvents = events.filter((e) => e.kind === 'coord_think')
  const timelineEvents = events.filter(
    (e) => e.kind === 'dispatch' || e.kind === 'complete' || e.kind === 'failed',
  )

  const hasAnything = thinkEvents.length > 0 || (plan && plan.length > 0) || timelineEvents.length > 0

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {!hasAnything && (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="暂无协调者活动记录"
          style={{ margin: '24px 0' }}
        />
      )}

      {/* 思考链 */}
      {thinkEvents.length > 0 && (
        <Collapse
          size="small"
          defaultActiveKey={[thinkEvents[thinkEvents.length - 1]?.id]}
          items={[{
            key: 'think',
            label: `协调者思考链 (${thinkEvents.length})`,
            children: (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {thinkEvents.map((e) => {
                  const data = (e.data || {}) as Record<string, unknown>
                  const action = String(data['action'] || '')
                  return (
                    <div
                      key={e.id}
                      style={{
                        background: '#fafafa',
                        borderLeft: '3px solid #722ed1',
                        padding: '8px 12px',
                        borderRadius: 4,
                        fontSize: 13,
                        color: '#333',
                        lineHeight: 1.6,
                      }}
                    >
                      <div style={{ marginBottom: 4 }}>
                        <Tag color="purple">{action}</Tag>
                        <span style={{ fontSize: 11, color: '#999' }}>
                          {new Date(e.timestamp).toLocaleTimeString()}
                        </span>
                      </div>
                      <div style={{ whiteSpace: 'pre-wrap' }}>{e.content || '(空)'}</div>
                    </div>
                  )
                })}
              </div>
            ),
          }]}
        />
      )}

      {/* 协作计划 */}
      {plan && plan.length > 0 && (
        <Collapse
          size="small"
          defaultActiveKey={['plan']}
          items={[{
            key: 'plan',
            label: `协作计划 (${plan.length} 步)`,
            children: (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {plan.map((step: PlanStep) => {
                  const badge = stepBadge(step.status)
                  return (
                    <div
                      key={`step-${step.step}`}
                      style={{
                        padding: '8px 12px',
                        background: '#fafafa',
                        borderRadius: 4,
                        border: '1px solid #f0f0f0',
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                        <span style={{ fontWeight: 600, color: '#1677ff' }}>步骤 {step.step}</span>
                        <Tag color={badge.color}>{badge.label}</Tag>
                        <span style={{ fontSize: 12, color: '#666' }}>
                          {step.agent_name || step.agent_id}
                        </span>
                      </div>
                      <div style={{ fontSize: 13, color: '#333', whiteSpace: 'pre-wrap' }}>
                        {step.instruction}
                      </div>
                      {step.depends_on && step.depends_on.length > 0 && (
                        <div style={{ fontSize: 11, color: '#999', marginTop: 4 }}>
                          依赖: 步骤 {step.depends_on.join(', ')}
                        </div>
                      )}
                      {step.result && (
                        <div style={{ fontSize: 12, color: '#666', marginTop: 4, whiteSpace: 'pre-wrap' }}>
                          结果: {step.result}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            ),
          }]}
        />
      )}

      {/* 派工时间线 */}
      {timelineEvents.length > 0 && (
        <Collapse
          size="small"
          defaultActiveKey={['timeline']}
          items={[{
            key: 'timeline',
            label: `派工时间线 (${timelineEvents.length})`,
            children: (
              <Timeline
                items={timelineEvents.map((e) => {
                  const data = (e.data || {}) as Record<string, unknown>
                  const isDispatch = e.kind === 'dispatch'
                  const isComplete = e.kind === 'complete'
                  const color = isDispatch ? 'blue' : isComplete ? 'green' : 'red'
                  const agentName = String(data['agent_name'] || e.agentId)
                  const step = data['step'] != null ? `步骤 ${data['step']}` : ''
                  const label = isDispatch
                    ? `派发 ${step} → ${agentName}`
                    : isComplete
                      ? `完成 ${step} ${agentName}`
                      : `失败 ${step} ${agentName}`
                  return {
                    color,
                    children: (
                      <div>
                        <div style={{ fontWeight: 600 }}>{label}</div>
                        <div style={{ fontSize: 12, color: '#999' }}>
                          {new Date(e.timestamp).toLocaleTimeString()}
                          {e.taskId ? ` · task ${String(e.taskId).slice(0, 8)}` : ''}
                        </div>
                        {e.content && !isComplete && (
                          <div style={{ fontSize: 12, color: '#666', marginTop: 2, whiteSpace: 'pre-wrap' }}>
                            {e.content.length > 120 ? e.content.slice(0, 120) + '...' : e.content}
                          </div>
                        )}
                      </div>
                    ),
                  }
                })}
              />
            ),
          }]}
        />
      )}
    </div>
  )
}

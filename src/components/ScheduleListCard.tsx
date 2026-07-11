import { Card, Empty, Tag } from 'antd'
import { ClockCircleOutlined } from '@ant-design/icons'
import type { ScheduledTask } from '../services/api'

interface ScheduleListCardProps {
  /** 全部定时任务（scheduledTaskApi.list）。 */
  tasks: ScheduledTask[]
}

/** schedule_type → Tag 颜色 + 标签（与 SchedulePage SCHEDULE_META 对齐，全应用一致）。 */
const SCHEDULE_META: Record<string, { color: string; label: string }> = {
  cron: { color: 'geekblue', label: 'cron' },
  interval: { color: 'blue', label: '定间隔' },
  once: { color: 'purple', label: '一次性' },
}

/**
 * 频率摘要：把 schedule_type + 相关字段拼成一句话（如「每 3600 秒」「cron: 0 8 * * *」「定时: ISO」）。
 * 纯展示文案，调度真源在后端 APScheduler job。与 SchedulePage.scheduleSummary 同算法。
 */
function scheduleSummary(t: ScheduledTask): string {
  if (t.schedule_type === 'cron') {
    return t.cron ? `cron: ${t.cron}` : 'cron（未配置表达式）'
  }
  if (t.schedule_type === 'once') {
    return t.run_at ? `定时: ${t.run_at}` : '一次性（未配置时刻）'
  }
  // interval
  const secs = t.interval_seconds || 0
  if (secs <= 0) return '定间隔（未配置秒数）'
  if (secs % 86400 === 0) return `每 ${secs / 86400} 天`
  if (secs % 3600 === 0) return `每 ${secs / 3600} 小时`
  if (secs % 60 === 0) return `每 ${secs / 60} 分钟`
  return `每 ${secs} 秒`
}

/**
 * SC-10 `/schedule` 结果卡片：内联展示定时任务列表。
 *
 * 数据来自 `GET /api/scheduled-tasks`（scheduledTaskApi.list）——全部定时任务（cron/interval/once
 * 三类调度），区别于 SchedulePage 管理页（CRUD + 立即执行/暂停/恢复 + 执行历史抽屉）。/schedule 是
 * 「一眼看配了哪些定时任务、各什么频率、启没启用、派给谁」的列表概览，不带操作（操作走 SchedulePage）。
 *
 * 设计：
 *  - 每任务一行：name（粗）+ schedule_type Tag（cron geekblue/interval blue/once purple，颜色同
 *    SchedulePage）+ enabled 状态 Tag（启用 success 绿 / 禁用 default 灰）。
 *  - 第二行：频率摘要（scheduleSummary 同 SchedulePage 算法，cron→表达式 / interval→每 N 秒分时天
 *    / once→定时时刻）+ content 预览（派发内容单行 ellipsis 截断）+ 目标 agent_id（slice 12 字符）。
 *  - 顶部摘要：总数 + 启用/禁用计数。
 *  - 空列表 Empty simple 占位。
 *
 * 与 SchedulePage 区别：SchedulePage 是管理页（创建 Modal + 立即执行/暂停/恢复按钮 + 执行历史抽屉）；
 * /schedule 卡片是聊天流内只读列表快照（任务名/类型/状态/频率 + 派发内容预览 + 目标 agent），轻量浏览不操作。
 */
export default function ScheduleListCard({ tasks }: ScheduleListCardProps) {
  const enabledCount = tasks.filter((t) => t.enabled).length
  const disabledCount = tasks.length - enabledCount

  return (
    <Card
      size="small"
      style={{ marginBottom: 12, borderColor: '#ffd591' }}
      title={
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <ClockCircleOutlined style={{ color: '#fa8c16' }} />
          <Tag color="orange" style={{ margin: 0 }}>定时任务</Tag>
          <span style={{ fontSize: 13, color: '#666' }}>
            共 {tasks.length} 个
            {tasks.length > 0 && (
              <>{`（启用 ${enabledCount} · 禁用 ${disabledCount}）`}</>
            )}
          </span>
        </span>
      }
    >
      {tasks.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="暂无定时任务"
          style={{ margin: '8px 0' }}
        />
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {tasks.map((t) => {
            const meta = SCHEDULE_META[t.schedule_type] ?? {
              color: 'default',
              label: t.schedule_type,
            }
            const content = (t.content || '').trim()
            return (
              <div
                key={t.id}
                style={{
                  padding: '8px 10px',
                  background: '#fafafa',
                  borderRadius: 4,
                  border: '1px solid #f0f0f0',
                }}
              >
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    flexWrap: 'wrap',
                  }}
                >
                  <span style={{ fontWeight: 600, fontSize: 13, color: '#333' }}>
                    {t.name}
                  </span>
                  <Tag color={meta.color} style={{ margin: 0 }}>{meta.label}</Tag>
                  <Tag color={t.enabled ? 'success' : 'default'} style={{ margin: 0 }}>
                    {t.enabled ? '启用' : '禁用'}
                  </Tag>
                </div>
                <div
                  style={{
                    fontSize: 12,
                    color: '#999',
                    marginTop: 4,
                    display: 'flex',
                    gap: 12,
                    flexWrap: 'wrap',
                  }}
                >
                  <span>{scheduleSummary(t)}</span>
                  {content && (
                    <span
                      style={{
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                        maxWidth: 240,
                      }}
                      title={content}
                    >
                      → {content}
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 11, color: '#bbb', marginTop: 2 }}>
                  目标 agent: {t.agent_id.slice(0, 12)}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </Card>
  )
}

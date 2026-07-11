import { Card, Empty, Tag } from 'antd'
import {
  DashboardOutlined,
  RobotOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import type { AgentStatusInfo, PlanStep } from '../services/api'

interface StatusCardProps {
  /** 当前聚焦群组 id（用于标题 + 判空提示）。null = 未选群。 */
  groupId: string | null
  /** 全部 agent 实时状态快照（来自 BusEventContext.agentStatuses）。 */
  agentStatuses: Record<string, AgentStatusInfo>
  /** 当前驻留计划（来自 BusEventContext.plan），null = 无计划。 */
  plan: PlanStep[] | null
  /** 流式 token 缓冲（task_id → 正在生成的文本），非空 = 有任务正在流式输出。 */
  streaming: Record<string, string>
}

/** status → Tag 颜色 + 中文标签（与 MonitorPage STATUS_TAG 对齐，全应用一致）。 */
const STATUS_TAG: Record<string, { color: string; label: string }> = {
  idle: { color: 'default', label: '空闲' },
  executing: { color: 'processing', label: '执行中' },
  offline: { color: 'default', label: '离线' },
}

/**
 * SC-07 `/status` 结果卡片：纯本地聚合运行状态（不调 LLM 不调 api）。
 *
 * 数据全部来自 BusEventContext 快照（ChatPanel 经 ctx.busState 注入）——agentStatuses
 * （各 agent 的 idle/executing/offline）、plan（驻留计划步骤）、streaming（正在流式生成的
 * task_id → 文本缓冲）。/status 是「看一眼当前在跑什么」的命令，零网络开销即时反馈。
 *
 * 设计：
 *  - 顶部摘要行：群组 id + agent 总数 + 各状态计数（执行中 N · 空闲 N · 离线 N）+ 流式任务数。
 *  - agent 状态列表：每个 agent 一行——name/role + status Tag（颜色同 MonitorPage）+ 若 executing
 *    则展示 current_task_id（正在跑的任务）。按 executing 优先排序（活跃的排前面，用户最关心）。
 *  - 计划摘要：若有驻留 plan，展示总步数 + 各状态计数（待执行/已派发/已完成/失败）。
 *  - 未选群：Empty 占位提示（/status 在无群组上下文时无状态可聚合）。
 *
 * 与 MonitorPage 区别：MonitorPage 是独立监控页（完整 trace + 停止按钮 + 详情），
 * /status 卡片是聊天流内轻量快照（一眼掌握「谁在跑、跑到哪」），不重复 trace 细节。
 */
export default function StatusCard({
  groupId,
  agentStatuses,
  plan,
  streaming,
}: StatusCardProps) {
  const agents = Object.values(agentStatuses)
  const executing = agents.filter((a) => a.status === 'executing')
  const idle = agents.filter((a) => a.status === 'idle')
  const offline = agents.filter((a) => a.status === 'offline')
  const streamingCount = Object.keys(streaming).filter((k) => streaming[k]).length

  // executing 优先（活跃的最关心），其余保持原序
  const sortedAgents = [...agents].sort((a, b) => {
    if (a.status === 'executing' && b.status !== 'executing') return -1
    if (b.status === 'executing' && a.status !== 'executing') return 1
    return 0
  })

  // 计划状态计数
  const planCount = (status: string) =>
    plan?.filter((s) => s.status === status).length ?? 0

  return (
    <Card
      size="small"
      style={{ marginBottom: 12, borderColor: '#69b1ff' }}
      title={
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <DashboardOutlined style={{ color: '#1677ff' }} />
          <Tag color="blue" style={{ margin: 0 }}>运行状态</Tag>
          <span style={{ fontSize: 13, color: '#666' }}>
            {groupId ? `${agents.length} 个智能体` : '未选会话'}
          </span>
        </span>
      }
    >
      {!groupId || agents.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={groupId ? '暂无智能体状态' : '未选会话，无状态可聚合'}
          style={{ margin: '8px 0' }}
        />
      ) : (
        <>
          {/* 摘要行 */}
          <div
            style={{
              display: 'flex',
              gap: 16,
              flexWrap: 'wrap',
              marginBottom: 12,
              padding: '8px 10px',
              background: '#f5faff',
              borderRadius: 4,
              border: '1px solid #e6f4ff',
              fontSize: 12,
            }}
          >
            <span>
              <ThunderboltOutlined style={{ color: '#1677ff', marginRight: 4 }} />
              执行中 <b style={{ color: '#1677ff' }}>{executing.length}</b>
            </span>
            <span>空闲 <b>{idle.length}</b></span>
            <span style={{ color: '#999' }}>离线 <b>{offline.length}</b></span>
            {streamingCount > 0 && (
              <span style={{ color: '#722ed1' }}>
                流式生成 <b>{streamingCount}</b>
              </span>
            )}
          </div>

          {/* agent 状态列表 */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            {sortedAgents.map((a) => {
              const tag = STATUS_TAG[a.status] ?? STATUS_TAG.idle
              return (
                <div
                  key={a.id}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    padding: '4px 8px',
                    background: a.status === 'executing' ? '#e6f4ff' : '#fafafa',
                    borderRadius: 4,
                    border: '1px solid #f0f0f0',
                  }}
                >
                  <RobotOutlined style={{ color: '#722ed1', fontSize: 12 }} />
                  <span style={{ fontWeight: 600, fontSize: 13 }}>{a.name}</span>
                  <span style={{ fontSize: 11, color: '#999' }}>{a.role}</span>
                  <Tag color={tag.color} style={{ marginInlineStart: 'auto', margin: 0 }}>
                    {tag.label}
                  </Tag>
                  {a.status === 'executing' && a.current_task_id && (
                    <Tag
                      color="purple"
                      style={{ margin: 0, fontSize: 10 }}
                    >
                      {a.current_task_id.slice(0, 12)}
                    </Tag>
                  )}
                </div>
              )
            })}
          </div>

          {/* 计划摘要 */}
          {plan && plan.length > 0 && (
            <div
              style={{
                marginTop: 12,
                padding: '8px 10px',
                background: '#faf5ff',
                borderRadius: 4,
                border: '1px solid #d3adf7',
                fontSize: 12,
                color: '#666',
              }}
            >
              <span style={{ color: '#722ed1', fontWeight: 600 }}>
                驻留计划 · {plan.length} 步
              </span>
              ：待执行 {planCount('pending')} · 已派发 {planCount('dispatched')} · 已完成{' '}
              {planCount('completed')}
              {planCount('failed') > 0 && (
                <span style={{ color: '#cf1322' }}> · 失败 {planCount('failed')}</span>
              )}
            </div>
          )}
        </>
      )}
    </Card>
  )
}

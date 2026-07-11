import { useEffect, useState } from 'react'
import { Tooltip, theme } from 'antd'

import { configApi, groupApi, type Group, type LlmConfig } from '../services/api'
import { useBusEventContext } from '../contexts/BusEventContext'

/**
 * Statusbar — 底部状态栏（L1-03 新增），常驻 Layout 底部 26px。
 *
 * 三段信息（全部从已有数据派生，不新增后端契约、不改 useBusEvent 脊柱）：
 *  1. 当前会话：从 BusEventContext.groupId + 本地缓存的 groups 列表解析群名（groupApi.list
 *     挂载时拉一次，groupId 变化只重新 find 名字，不重拉）。前导圆点：绿=已订阅群组，
 *     灰=未选会话。圆点语义=「有无活跃群组订阅」，不声称 socket 级连接态——useBusEvent
 *     未暴露 connected，避免为加一个状态点而改 hook 脊柱（WS-02 命中复用分支早返回静态对象，
 *     加字段会回归）。这是最稳妥取舍。
 *  2. 执行中 worker 数：从 agentStatuses 派生（count status==='executing'）——零额外请求，
 *     WS 实时驱动，Statusbar 随事件自动刷新。
 *  3. 当前模型：configApi.get() 挂载时拉一次（脱敏配置，model 字段）。模型热切换由后续
 *     阶段5 Header 下拉接 /model 逻辑时回调刷新，本阶段拉一次即静态展示。
 *
 * 独立组件、单一职责、纯展示——Layout 只负责把它放底栏。挂载在 BusEventProvider 内
 * （App: ConfigProvider → BusEventProvider → RouterProvider → Layout → Statusbar），
 * useBusEventContext 可用。
 *
 * 数据拉取失败静默（后端未起时 Statusbar 仍渲染，显示占位而非崩溃）——状态栏是辅助信息，
 * 不应因拉取失败阻断主界面。
 */
export default function Statusbar() {
  const { groupId, agentStatuses } = useBusEventContext()

  const [groups, setGroups] = useState<Group[]>([])
  const [config, setConfig] = useState<LlmConfig | null>(null)

  // 挂载拉一次群组列表 + 模型配置（轻量 GET，失败静默）
  useEffect(() => {
    groupApi.list().then(setGroups).catch(() => {})
    configApi.get().then(setConfig).catch(() => {})
  }, [])

  const groupName = groups.find((g) => g.id === groupId)?.name
  const executingCount = Object.values(agentStatuses).filter(
    (a) => a.status === 'executing',
  ).length
  const subscribed = !!groupId

  const {
    token: { colorBgContainer },
  } = theme.useToken()

  return (
    <div
      style={{
        height: 26,
        flexShrink: 0,
        padding: '0 16px',
        background: colorBgContainer,
        borderTop: '1px solid #f0f0f0',
        display: 'flex',
        alignItems: 'center',
        gap: 16,
        fontSize: 12,
        color: '#8c8c8c',
        userSelect: 'none',
      }}
    >
      <Tooltip title={subscribed ? '当前聚焦群组' : '未选会话'}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span
            style={{
              width: 7,
              height: 7,
              borderRadius: '50%',
              background: subscribed ? '#52c41a' : '#d9d9d9',
              display: 'inline-block',
            }}
          />
          {groupName || '未选会话'}
        </span>
      </Tooltip>
      <span style={{ color: '#d9d9d9' }}>·</span>
      <span>
        {executingCount > 0 ? (
          <>
            <span style={{ color: '#1677ff' }}>{executingCount}</span> worker 执行中
          </>
        ) : (
          '无执行中任务'
        )}
      </span>
      {config && (
        <>
          <span style={{ color: '#d9d9d9' }}>·</span>
          <span>{config.model}</span>
        </>
      )}
    </div>
  )
}

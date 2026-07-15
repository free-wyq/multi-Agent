import { useEffect, useState } from 'react'
import { Card, Empty, Select, Spin, Tabs, Tag, message } from 'antd'
import {
  RobotOutlined,
  CrownOutlined,
} from '@ant-design/icons'
import {
  groupApi,
  type Group,
  type GroupMember,
} from '../services/api'
import { useBusEventContext } from '../contexts/BusEventContext'
import LeaderPanel from '../components/LeaderPanel'
import WorkerTrace from '../components/WorkerTrace'
import StopTaskButton from '../components/StopTaskButton'

/** 状态 → 徽标颜色 */
const STATUS_TAG: Record<string, { color: string; label: string }> = {
  idle: { color: 'default', label: '空闲' },
  executing: { color: 'processing', label: '执行中' },
  offline: { color: 'default', label: '离线' },
}

export default function MonitorPage() {
  const [groups, setGroups] = useState<Group[]>([])
  // WS-05：selectedGroup 改由 BusEventContext（App 顶层 provider）持有——本页消费全局共享
  // WS 的 agentStatuses，并经 setSelectedGroup（= ctx.setGroupId）切全局聚焦群组。这样
  // LeaderPanel/WorkerTrace 的 useBusEvent(groupId) 命中 WS-02 共享分支，零冗余 WS。
  const {
    groupId: selectedGroup,
    setGroupId: setSelectedGroup,
    agentStatuses,
  } = useBusEventContext()
  const [members, setMembers] = useState<GroupMember[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    groupApi
      .list()
      .then((data) => {
        setGroups(data)
        if (data.length > 0) setSelectedGroup(data[0].id)
      })
      .catch(() => message.error('获取群组失败'))
  }, [])

  useEffect(() => {
    if (!selectedGroup) {
      setMembers([])
      return
    }
    setLoading(true)
    groupApi
      .listMembers(selectedGroup)
      .then(setMembers)
      .catch(() => setMembers([]))
      .finally(() => setLoading(false))
  }, [selectedGroup])

  const chatGroup = groups.find((g) => g.id === selectedGroup)
  const coordinatorId = chatGroup?.coordinator_id || ''

  // 标签页 = 群成员（不含 coordinator，coordinator 单列在 LeaderPanel）
  const workerMembers = members.filter((m) => m.agent_id !== coordinatorId)

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 16,
        height: '100%',
        minHeight: 0,
        overflowY: 'auto',
        padding: 20,
        background: 'var(--surface-main)',
      }}
    >
      {/* L4-05：迁 /monitor 全屏路由，根容器加 height:100%+overflowY:auto 接通高度链。
          原 SH-05 降级为抽屉 Tab 时移除了页级 h2，全屏路由下 Select 独占顶部右对齐。 */}
      <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center' }}>
        <Select
          style={{ width: 240 }}
          placeholder="选择群组"
          value={selectedGroup}
          onChange={setSelectedGroup}
          options={groups.map((g) => ({ value: g.id, label: g.name }))}
        />
      </div>

      {!selectedGroup ? (
        <Empty description="请选择一个群组查看监控" />
      ) : loading ? (
        <div style={{ textAlign: 'center', padding: 40 }}><Spin /></div>
      ) : (
        <>
          {/* Leader（协调者）面板 */}
          <Card
            title={
              <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <CrownOutlined style={{ color: '#722ed1' }} />
                协调者（Leader）
                {coordinatorId && agentStatuses[coordinatorId] && (
                  <Tag color={STATUS_TAG[agentStatuses[coordinatorId].status]?.color || 'default'}>
                    {STATUS_TAG[agentStatuses[coordinatorId].status]?.label || agentStatuses[coordinatorId].status}
                  </Tag>
                )}
              </span>
            }
            extra={
              /* PL-11 / task-26：协调者 executing 时展示停止按钮。task-26 起按钮调
                 groupApi.stopTurn 群图整回合硬停，不依赖 current_task_id——但监控页是
                 per-agent 视图，协调者作为 Leader 卡片仍按 executing+current_task_id 判定
                 （驻留 dispatch/handle_reply 路径有 task_id），保持原精确入口。 */
              coordinatorId &&
              agentStatuses[coordinatorId]?.status === 'executing' &&
              agentStatuses[coordinatorId]?.current_task_id ? (
                <StopTaskButton
                  groupId={selectedGroup}
                  agentName={agentStatuses[coordinatorId]?.name}
                />
              ) : null
            }
            size="small"
          >
            <LeaderPanel />
          </Card>

          {/* Worker 标签页 */}
          <Card
            title={
              <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <RobotOutlined />
                子智能体（Worker）
              </span>
            }
            size="small"
          >
            {workerMembers.length === 0 ? (
              <Empty description="该群组暂无成员智能体" />
            ) : (
              <Tabs
                items={workerMembers.map((m) => {
                  const status = agentStatuses[m.agent_id]?.status || 'idle'
                  const statusInfo = STATUS_TAG[status] ?? STATUS_TAG.idle
                  const isExecuting = status === 'executing'
                  const currentTaskId = agentStatuses[m.agent_id]?.current_task_id || ''
                  return {
                    key: m.agent_id,
                    label: (
                      <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <RobotOutlined />
                        {m.alias || m.agent_name}
                        <Tag color={statusInfo.color} style={{ marginInlineStart: 4, fontSize: 10 }}>
                          {statusInfo.label}
                        </Tag>
                        {/* PL-11 / task-26：执行中 Tab 标签内联停止按钮。task-26 起调
                            groupApi.stopTurn 群图整回合硬停（去中心化闲聊回合也能停），
                            按 isExecuting+currentTaskId 判定保持 per-worker 精确入口。 */}
                        {isExecuting && currentTaskId && (
                          <span
                            // 阻止点击停止按钮时冒泡到 Tabs 切换
                            onClick={(e) => e.stopPropagation()}
                            onMouseDown={(e) => e.stopPropagation()}
                          >
                            <StopTaskButton
                              groupId={selectedGroup}
                              agentName={m.alias || m.agent_name}
                            />
                          </span>
                        )}
                      </span>
                    ),
                    children: (
                      <WorkerTrace
                        agentId={m.agent_id}
                        agentName={m.alias || m.agent_name}
                      />
                    ),
                  }
                })}
              />
            )}
          </Card>
        </>
      )}
    </div>
  )
}

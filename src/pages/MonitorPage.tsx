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
import { useBusEvent } from '../hooks/useBusEvent'
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
  const [selectedGroup, setSelectedGroup] = useState<string | null>(null)
  const [members, setMembers] = useState<GroupMember[]>([])
  const [loading, setLoading] = useState(false)
  const { agentStatuses } = useBusEvent(selectedGroup)

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
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* 群组选择 */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h2 style={{ margin: 0 }}>执行监控</h2>
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
              /* PL-11：协调者 executing 时展示停止按钮（停止其当前 dispatch/handle_reply 任务） */
              coordinatorId &&
              agentStatuses[coordinatorId]?.status === 'executing' &&
              agentStatuses[coordinatorId]?.current_task_id ? (
                <StopTaskButton
                  taskId={agentStatuses[coordinatorId].current_task_id}
                  groupId={selectedGroup}
                  agentName={agentStatuses[coordinatorId]?.name}
                />
              ) : null
            }
            size="small"
          >
            <LeaderPanel groupId={selectedGroup} />
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
                        {/* PL-11：执行中 Tab 标签内联停止按钮，点击直接停止该 worker 当前任务 */}
                        {isExecuting && currentTaskId && (
                          <span
                            // 阻止点击停止按钮时冒泡到 Tabs 切换
                            onClick={(e) => e.stopPropagation()}
                            onMouseDown={(e) => e.stopPropagation()}
                          >
                            <StopTaskButton
                              taskId={currentTaskId}
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
                        groupId={selectedGroup}
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

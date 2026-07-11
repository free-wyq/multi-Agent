import { useEffect, useState } from 'react'
import { Button, Empty, Tooltip, Typography } from 'antd'
import { SettingOutlined } from '@ant-design/icons'

import { useSelection } from '../contexts/SelectionContext'
import { useBusEventContext } from '../contexts/BusEventContext'
import { groupApi, type GroupMember } from '../services/api'
import ChatPanel from './ChatPanel'
import GroupInfoDrawer from './GroupInfoDrawer'
import StopTaskButton from './StopTaskButton'
import { AgentEditButton } from './AgentDetailPanel'

const { Text } = Typography

/**
 * ChatView — 右侧内容区（布局重构 2026-07-11）。
 *
 * 始终渲染 ChatPanel（单聊/群聊都收敛到 groupId + ChatPanel），由左栏 Sidebar 触发的
 * SelectionContext 决定当前 groupId。上方统一标题区（ChatPanel 传 hideHeader 隐藏自带头部，
 * 避免双头部）：
 *  - 单聊（activeGroup.config.single_chat===true）：显 agent 名 + 角色副标，不显 ⚙群信息。
 *  - 群聊：显 group.name + 成员数 + ⚙群信息按钮（开 GroupInfoDrawer）+ 执行中时 StopTaskButton。
 *
 * 数据加载：members 随 groupId 切换拉取（groupApi.listMembers）；agents/groups 由
 * SelectionContext 集中持有复用。GroupInfoDrawer 的 onChanged 调 refreshAll 同步群组/成员变更。
 */
export default function ChatView() {
  const { agents, activeGroup, refreshAll } = useSelection()
  const { groupId, agentStatuses } = useBusEventContext()

  const [members, setMembers] = useState<GroupMember[]>([])
  const [membersLoading, setMembersLoading] = useState(false)
  const [infoOpen, setInfoOpen] = useState(false)

  // 切 groupId 时拉成员（消息由 ChatPanel 自管）。
  useEffect(() => {
    if (!groupId) {
      setMembers([])
      return
    }
    setMembersLoading(true)
    groupApi
      .listMembers(groupId)
      .then(setMembers)
      .catch(() => setMembers([]))
      .finally(() => setMembersLoading(false))
  }, [groupId])

  // 未选群：占位引导。
  if (!groupId || !activeGroup) {
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', background: '#fff' }}>
        <Empty description="请在左侧选择智能体或群组开始对话" />
      </div>
    )
  }

  const isSingleChat = !!activeGroup.config?.single_chat
  // 单聊：群主即被选 agent；群聊：成员数 +1（含用户）。
  const singleAgent = isSingleChat
    ? agents.find((a) => a.id === activeGroup.coordinator_id)
    : null

  // 执行中 agent（StopTaskButton 入口，群聊头部）。
  const executingAgent = groupId
    ? Object.values(agentStatuses).find((a) => a.status === 'executing' && a.current_task_id)
    : undefined

  return (
    <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', background: '#fff', overflow: 'hidden' }}>
      {/* 标题区 */}
      <div
        style={{
          height: 48,
          flexShrink: 0,
          padding: '0 20px',
          borderBottom: '1px solid #f0f0f0',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, overflow: 'hidden' }}>
          <Text strong style={{ fontSize: 15, flexShrink: 0 }}>
            {isSingleChat ? (singleAgent?.name ?? '单聊') : activeGroup.name}
          </Text>
          {isSingleChat ? (
            singleAgent?.role && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {singleAgent.role}
              </Text>
            )
          ) : (
            <Text type="secondary" style={{ fontSize: 13, flexShrink: 0 }}>
              ( {members.length + 1} )
            </Text>
          )}
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          {executingAgent && groupId && !isSingleChat && (
            <StopTaskButton
              taskId={executingAgent.current_task_id!}
              groupId={groupId}
              agentName={executingAgent.name}
            />
          )}
          {isSingleChat && singleAgent && (
            <AgentEditButton
              agent={singleAgent}
              onUpdated={() => refreshAll()}
              small
            />
          )}
          {!isSingleChat && (
            <Tooltip title="群信息">
              <Button
                type="text"
                icon={<SettingOutlined />}
                size="small"
                onClick={() => setInfoOpen(true)}
              />
            </Tooltip>
          )}
        </div>
      </div>

      {/* 聊天主区：ChatPanel 隐藏自带头部（本组件已画统一标题区） */}
      <ChatPanel
        group={activeGroup}
        agents={agents}
        members={members}
        loading={membersLoading}
        hideHeader
        onOpenInfo={() => setInfoOpen(true)}
      />

      {/* 群信息抽屉（仅群聊有，单聊无群管理） */}
      {!isSingleChat && (
        <GroupInfoDrawer
          open={infoOpen}
          onClose={() => setInfoOpen(false)}
          group={activeGroup}
          groupId={groupId}
          members={members}
          membersLoading={membersLoading}
          agents={agents}
          onChanged={refreshAll}
        />
      )}
    </div>
  )
}

import { useEffect, useState } from 'react'
import { Button, Empty, Switch, Tag, Tooltip, Typography } from 'antd'
import { TeamOutlined } from '@ant-design/icons'

import { useSelection } from '../contexts/SelectionContext'
import { useBusEventContext } from '../contexts/BusEventContext'
import { useSettings } from '../contexts/SettingsContext'
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
  const { agents, activeGroup, activeConversation, refreshAll } = useSelection()
  const { groupId, agentStatuses, coordStreaming } = useBusEventContext()
  const { tts, updateTts } = useSettings()

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
  if (!groupId || !(activeGroup || activeConversation)) {
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'var(--surface-raised)' }}>
        <Empty description="请在左侧选择智能体或群组开始对话" />
      </div>
    )
  }

  // Path C：单聊由独立 ConversationEntity 承载（不再读 config.single_chat）。
  const isSingleChat = !!activeConversation
  // 单聊：被选 agent；群聊：null（群聊标题显 group.name + 成员数）。
  const singleAgent = isSingleChat
    ? agents.find((a) => a.id === activeConversation?.agent_id)
    : null

  // 执行中 agent（StopTaskButton 入口，群聊头部）。task-25：放宽为识别去中心化回合的
  // 活跃发言人——闲聊/@人/成语接龙回合发言人不走驻留引擎 executing 状态机（无
  // current_task_id），活跃信号体现在协调者流式缓冲 coordStreaming。任一命中即渲染停止按钮
  // （task-26 起按钮调 groupApi.stopTurn 群图整回合硬停，不依赖 task_id）。coordStreaming
  // 是全局 Map（跨群组 reply_id），但 useBusEvent 按当前 groupId 订阅 WS，非本群事件不会进
  // 该 Map，故 length>0 即「本群有活跃流式」。
  const coordinatorId = activeGroup?.coordinator_id ?? activeConversation?.coordinator_id ?? ''
  const hasActiveStream =
    !!groupId &&
    (Object.keys(coordStreaming).length > 0 ||
      Object.values(agentStatuses).some(
        (a) => a.status === 'executing' && a.current_task_id,
      ))
  const executingAgent = groupId
    ? Object.values(agentStatuses).find(
        (a) => a.status === 'executing' && a.current_task_id,
      ) ??
      // 去中心化回合：无 executing agent 但有活跃流式 → 取协调者作停止按钮的代理发言人
      // （stopTurn 是回合级停止，不依赖具体 task_id，用协调者 id 作展示锚点即可）。
      (hasActiveStream && coordinatorId
        ? {
            id: coordinatorId,
            name: agentStatuses[coordinatorId]?.name || '协调者',
            role: agentStatuses[coordinatorId]?.role || 'coordinator',
            status: 'executing' as const,
            current_task_id: null,
          }
        : undefined)
    : undefined

  return (
    <div style={{ flex: 1, minWidth: 0, minHeight: 0, display: 'flex', flexDirection: 'column', background: 'var(--surface-raised)', overflow: 'hidden' }}>
      {/* 标题区。浮起白底 + 底部细线，与下方消息区（同白底但有滚动列表）靠投影/分隔线区分。 */}
      <div
        style={{
          height: 48,
          flexShrink: 0,
          padding: '0 20px',
          borderBottom: '1px solid var(--border-soft)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, overflow: 'hidden' }}>
          <Text strong style={{ fontSize: 15, flexShrink: 0 }}>
            {isSingleChat ? (singleAgent?.name ?? activeConversation?.name ?? '单聊') : (activeGroup?.name ?? '群聊')}
          </Text>
          {/* 协作模式 Tag（仅群聊显示，单聊不显）。中心化橙 / 去中心化紫。
              config.collaboration_mode 缺省时兜底 centralized（老群组兼容）。 */}
          {!isSingleChat && activeGroup && (
            <Tag
              color={
                (activeGroup.config?.collaboration_mode as string) === 'decentralized'
                  ? 'purple'
                  : 'orange'
              }
              style={{ fontSize: 10, lineHeight: '14px', padding: '0 4px', flexShrink: 0, margin: 0 }}
            >
              {(activeGroup.config?.collaboration_mode as string) === 'decentralized'
                ? '去中心化'
                : '中心化'}
            </Tag>
          )}
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
          {/* 自动朗读开关：高频一键切当前会话的「新回复自动朗读」。
              总开关 tts.enabled 关时灰禁（需先去设置开启语音功能）。 */}
          <Tooltip title={tts.enabled ? (tts.autoPlay ? '关闭自动朗读' : '开启自动朗读（新回复读出来）') : '请先在设置-语音朗读中开启总开关'}>
            <Switch
              size="small"
              checked={tts.enabled && tts.autoPlay}
              disabled={!tts.enabled}
              onChange={(v) => updateTts({ autoPlay: v })}
            />
          </Tooltip>
          <Tooltip title="自动朗读">
            <span style={{ fontSize: 12, color: '#999', userSelect: 'none', cursor: 'help' }}>朗读</span>
          </Tooltip>
          {executingAgent && groupId && !isSingleChat && (
            <StopTaskButton
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
                icon={<TeamOutlined />}
                size="small"
                onClick={() => setInfoOpen(true)}
              />
            </Tooltip>
          )}
        </div>
      </div>

      {/* 聊天主区：ChatPanel 隐藏自带头部（本组件已画统一标题区）
          Path C：单聊 ConversationEntity 也有 coordinator_id（镜像 agent_id），
          ChatPanel 读 group.coordinator_id 渲染流式气泡 sender 不破，故单聊传
          activeConversation（形状兼容，coordinator_id 字段镜像 agent_id）。 */}
      <ChatPanel
        group={activeGroup ?? activeConversation ?? null}
        agents={agents}
        members={members}
        loading={membersLoading}
        hideHeader
        onOpenInfo={() => setInfoOpen(true)}
      />

      {/* 群信息抽屉（仅群聊有，单聊无群管理） */}
      {!isSingleChat && activeGroup && (
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

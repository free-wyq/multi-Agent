import { useEffect, useState } from 'react'
import {
  Avatar,
  Button,
  Divider,
  Drawer,
  Empty,
  Popconfirm,
  Spin,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import {
  DeleteOutlined,
  DownloadOutlined,
  FileOutlined,
  RobotOutlined,
} from '@ant-design/icons'

import {
  conversationApi,
  taskApi,
  type AgentDefinition,
  type Conversation,
  type GroupFile,
  type Task,
} from '../services/api'
import { AgentEditButton } from './AgentDetailPanel'
import { findSourceTask } from './GroupInfoDrawer'
import { fileIconFor, humanSize, saveBlob } from '../lib/fileIcon'

const { Text } = Typography

interface ConversationInfoDrawerProps {
  /** 抽屉开关（ChatView 单聊标题区 ⚙ 按钮触发）。 */
  open: boolean
  /** 关闭回调。 */
  onClose: () => void
  /** 当前单聊会话（null/未选时抽屉内容不渲染）。 */
  conversation: Conversation | null
  /** 当前单聊 conversation_id（工作区 key）。 */
  conversationId: string | null
  /** 被选单聊 agent（activeConversation.agent_id 解析；null 时身份段缺省）。 */
  agent: AgentDefinition | null
  /** 会话/agent 变更后通知父刷新（删除会话后调 refreshAll 同步左栏）。 */
  onChanged?: () => void
}

/**
 * 任务14c ConversationInfoDrawer：单聊会话设置抽屉。
 *
 * 群聊有 GroupInfoDrawer（群公告/Leader 策略/成员/能力概况/文件 Tab + 群管理 Modal），
 * 单聊是独立 ConversationEntity（Path C 单聊分实体），无群管理语义——故单聊另起一个
 * 轻量抽屉，不复用 GroupInfoDrawer（后者强依赖 group/members，单聊无群主/成员概念）。
 *
 * 顶部 Tabs 两页：
 *  - 「详情」：会话身份（agent 头像/名/角色/定位/system_prompt 摘要 + 运行参数）+
 *    编辑入口（复用 AgentEditButton，与单聊标题区编辑同源）。
 *  - 「文件」：按 conversation_id 列该会话工作区全部产物（文件名/大小/修改时间/来源
 *    task）+ 下载。数据源 conversationApi.listFiles（任务14a 后端补的
 *    GET /api/conversations/{id}/files，key 无关 crud.list_files）；来源 task 反查复用
 *    GroupInfoDrawer 导出的 findSourceTask（basename 匹配 Task.artifact_path/
 *    artifact.files[].path 最后段）；下载复用 conversationApi.downloadFile + saveBlob
 *    （与群聊 groupApi.downloadFile + TaskPage/ChatMessageBubble 同入口同逻辑）。
 *
 * 与 GroupInfoDrawer「文件」Tab 的关系：结构镜像（同一 FilesTabContent 渲染逻辑），
 * 仅数据源 key 不同（conversation_id vs group_id）——工作区 key 无关是 C2「共享底层」
 * 的体现，单聊/群聊同 crud.list_files 服务。
 */
export default function ConversationInfoDrawer({
  open,
  onClose,
  conversation,
  conversationId,
  agent,
  onChanged,
}: ConversationInfoDrawerProps) {
  const [activeTab, setActiveTab] = useState<'detail' | 'files'>('detail')
  const [convFiles, setConvFiles] = useState<GroupFile[]>([])
  const [filesLoading, setFilesLoading] = useState(false)
  const [convTasks, setConvTasks] = useState<Task[]>([])
  const [downloading, setDownloading] = useState<string | null>(null)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [deleting, setDeleting] = useState(false)

  // 文件列表：抽屉开 + 有 conversationId 时加载（开门才需，避免常驻轮询）
  useEffect(() => {
    if (!open || !conversationId) {
      setConvFiles([])
      return
    }
    setFilesLoading(true)
    conversationApi
      .listFiles(conversationId)
      .then(setConvFiles)
      .catch(() => setConvFiles([]))
      .finally(() => setFilesLoading(false))
  }, [open, conversationId])

  // 任务列表：用于「文件」Tab 反查来源 task（taskApi.list 传 conversation_id，
  // 后端 list_tasks 按 conversation_id 过滤——单聊即 conversation_id 入该列）。
  useEffect(() => {
    if (!open || !conversationId) {
      setConvTasks([])
      return
    }
    taskApi
      .list(conversationId)
      .then(setConvTasks)
      .catch(() => setConvTasks([]))
  }, [open, conversationId])

  // 串行下载（与 GroupInfoDrawer/TaskPage/ChatMessageBubble 同款单下载互斥）
  const handleFileDownload = async (fileName: string) => {
    if (!conversationId) {
      message.warning('未选择会话，无法下载')
      return
    }
    setDownloading(fileName)
    try {
      const blob = await conversationApi.downloadFile(conversationId, fileName)
      saveBlob(blob, fileName)
      message.success(`已下载 ${fileName}`)
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e))
    } finally {
      setDownloading(null)
    }
  }

  // 删除会话：conversationApi.delete（同时清理会话实体 + 工作区）。删除后 onChanged
  // 刷新左栏让会话消失，onClose 关抽屉。
  const handleDelete = async () => {
    if (!conversation) return
    setDeleting(true)
    try {
      await conversationApi.delete(conversation.id)
      message.success('会话已删除')
      setDeleteOpen(false)
      onChanged?.()
      onClose()
    } catch (e) {
      message.error(`删除失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setDeleting(false)
    }
  }

  return (
    <Drawer
      title="会话信息"
      placement="right"
      open={open}
      onClose={onClose}
      width={360}
      styles={{ body: { padding: 0 } }}
    >
      {conversation && (
        <div style={{ padding: '16px 16px 0' }}>
          {/* 会话头部：agent 头像 + 名称 + 创建时间 */}
          <div style={{ textAlign: 'center', padding: '12px 0 20px' }}>
            <Avatar
              size={64}
              style={{
                background: '#722ed1',
                verticalAlign: 'middle',
              }}
              src={undefined}
            >
              {agent?.icon_emoji ? (
                <span style={{ fontSize: 32 }}>{agent.icon_emoji}</span>
              ) : (
                <RobotOutlined style={{ fontSize: 32 }} />
              )}
            </Avatar>
            <div style={{ fontSize: 16, fontWeight: 600, marginTop: 12 }}>
              {agent?.name ?? conversation.name ?? '单聊'}
            </div>
            <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
              {conversation.name}
            </Text>
            <Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: 2 }}>
              创建于 {conversation.created_at ? new Date(conversation.created_at).toLocaleString() : '—'}
            </Text>
          </div>

          <Divider style={{ margin: '0' }} />

          {/* 顶部 Tabs——「详情」(会话身份 + 编辑) + 「文件」(会话产物管理)。
              用 antd v6 Tabs items API（与 GroupInfoDrawer/MonitorPage/SkillPage 一致）。
              activeTab 受控便于后续事件驱动跳转文件 Tab。 */}
          <Tabs
            size="small"
            activeKey={activeTab}
            onChange={(k) => setActiveTab(k as 'detail' | 'files')}
            style={{ marginTop: 4 }}
            items={[
              {
                key: 'detail',
                label: '详情',
                children: (
                  <>
                    {/* 会话身份（agent 画像） */}
                    <div style={{ padding: '12px 0' }}>
                      {agent ? (
                        <>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
                            <Tag color="purple" style={{ margin: 0 }}>{agent.role}</Tag>
                            <Text strong style={{ fontSize: 13 }}>{agent.name}</Text>
                          </div>
                          {agent.description && (
                            <div style={{ fontSize: 12, color: '#999', marginBottom: 6 }}>
                              {agent.description}
                            </div>
                          )}
                          {agent.system_prompt && (
                            <Tooltip title={agent.system_prompt}>
                              <div style={{
                                fontSize: 12, color: '#999', background: '#f5f5f5',
                                padding: '8px 12px', borderRadius: 4, maxHeight: 80, overflow: 'hidden',
                                whiteSpace: 'pre-wrap',
                              }}>
                                {agent.system_prompt.slice(0, 200)}
                                {agent.system_prompt.length > 200 ? '…' : ''}
                              </div>
                            </Tooltip>
                          )}
                          {/* 运行参数 */}
                          {(agent.model || agent.max_turns != null) && (
                            <div style={{ fontSize: 12, color: '#666', marginTop: 8, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                              {agent.model && (
                                <span>模型: <Tag color="orange" style={{ margin: 0 }}>{agent.model}</Tag></span>
                              )}
                              {agent.max_turns != null && (
                                <span>最大轮次: <Tag style={{ margin: 0 }}>{agent.max_turns}</Tag></span>
                              )}
                            </div>
                          )}
                          {/* 编辑入口：复用 AgentEditButton，与单聊标题区编辑同源
                              （打开同一 Modal，挂载技能/MCP + 改 model/身份字段）。 */}
                          <div style={{ marginTop: 12 }}>
                            <AgentEditButton agent={agent} onUpdated={() => onChanged?.()} small />
                          </div>
                        </>
                      ) : (
                        <Empty
                          image={Empty.PRESENTED_IMAGE_SIMPLE}
                          description="未关联智能体"
                          style={{ margin: '8px 0' }}
                        />
                      )}
                    </div>

                    <Divider style={{ margin: '0' }} />

                    {/* 危险操作：删除会话（清会话实体 + 工作区） */}
                    <div style={{ padding: '12px 0' }}>
                      <div style={{ fontSize: 12, color: '#999', marginBottom: 8 }}>会话管理</div>
                      <Popconfirm
                        title="删除该单聊会话？"
                        description="将删除会话记录与其工作区产物文件，不可恢复。"
                        okText="删除"
                        okButtonProps={{ danger: true, loading: deleting }}
                        cancelText="取消"
                        open={deleteOpen}
                        onConfirm={handleDelete}
                        onCancel={() => setDeleteOpen(false)}
                      >
                        <Button
                          danger
                          block
                          icon={<DeleteOutlined />}
                          onClick={() => setDeleteOpen(true)}
                        >
                          删除会话
                        </Button>
                      </Popconfirm>
                    </div>
                  </>
                ),
              },
              {
                key: 'files',
                label: (
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                    文件
                    {convFiles.length > 0 && (
                      <Tag
                        color="orange"
                        style={{ marginInlineStart: 2, fontSize: 10, lineHeight: '16px', padding: '0 4px' }}
                      >
                        {convFiles.length}
                      </Tag>
                    )}
                  </span>
                ),
                children: (
                  <ConversationFilesTabContent
                    conversationId={conversationId}
                    convFiles={convFiles}
                    filesLoading={filesLoading}
                    convTasks={convTasks}
                    downloading={downloading}
                    onDownload={handleFileDownload}
                  />
                ),
              },
            ]}
          />
        </div>
      )}
    </Drawer>
  )
}

// ── 任务14c：单聊会话文件管理 Tab 内容组件 ──────────────────────────────────
// 结构镜像 GroupInfoDrawer 的 FilesTabContent（同一渲染逻辑），仅数据源 key 不同
// （conversation_id vs group_id）。独立定义避免跨组件传 props 链 + 命名混淆。

interface ConversationFilesTabContentProps {
  conversationId: string | null
  convFiles: GroupFile[]
  filesLoading: boolean
  convTasks: Task[]
  downloading: string | null
  onDownload: (fileName: string) => void
}

/**
 * 任务14c：单聊「文件」Tab——该会话工作区全部产物（文件名/大小/修改时间/来源 task）+ 下载。
 *
 * 数据源 conversationApi.listFiles(conversationId)（任务14a 后端补的
 * GET /api/conversations/{id}/files，key 无关 crud.list_files，返工作区顶层文件
 * name/size/modified_at）。来源 task 由 findSourceTask（GroupInfoDrawer 导出复用）
 * best-effort basename 匹配。下载复用 conversationApi.downloadFile + saveBlob。
 */
function ConversationFilesTabContent({
  conversationId,
  convFiles,
  filesLoading,
  convTasks,
  downloading,
  onDownload,
}: ConversationFilesTabContentProps) {
  if (filesLoading) {
    return (
      <div style={{ textAlign: 'center', padding: 32 }}>
        <Spin size="small" />
      </div>
    )
  }
  if (convFiles.length === 0) {
    return (
      <div style={{
        fontSize: 13, color: '#b0b0b0',
        border: '1px dashed #d0d7de',
        padding: '24px 16px',
        borderRadius: 8, textAlign: 'center', display: 'flex',
        alignItems: 'center', justifyContent: 'center', gap: 8,
        margin: '16px 0',
      }}>
        <FileOutlined style={{ fontSize: 16, color: '#b0b0b0' }} />
        会话暂无产物文件（智能体执行任务后自动写入工作区）
      </div>
    )
  }
  return (
    <div style={{ padding: '8px 0 16px' }}>
      <div style={{ fontSize: 12, color: '#999', marginBottom: 8 }}>
        共 {convFiles.length} 个文件 · 点击下载（{conversationId ? '就绪' : '未选会话'}）
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {convFiles.map((file) => {
          const sourceTask = findSourceTask(file.name, convTasks)
          return (
            <div
              key={file.name}
              style={{
                display: 'flex', alignItems: 'center', gap: 10,
                padding: '10px', borderRadius: 8,
                border: '1px solid #f0f0f0',
                background: '#fff',
                transition: 'border-color 0.18s ease',
                flexShrink: 0,
              }}
              onMouseEnter={(e) => {
                (e.currentTarget as HTMLDivElement).style.borderColor = '#722ed1'
              }}
              onMouseLeave={(e) => {
                (e.currentTarget as HTMLDivElement).style.borderColor = '#f0f0f0'
              }}
            >
              <div style={{
                width: 34, height: 34, borderRadius: 6,
                background: '#f5f7fa',
                display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
              }}>
                {fileIconFor(file.name, { fontSize: 17 })}
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <Tooltip title={file.name}>
                  <div style={{
                    fontSize: 13, fontWeight: 500, color: '#1f2937',
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>
                    {file.name}
                    {sourceTask && (
                      <Tag
                        color="gold"
                        style={{ marginInlineStart: 6, fontSize: 10, lineHeight: '14px', padding: '0 4px' }}
                      >
                        主产物
                      </Tag>
                    )}
                  </div>
                </Tooltip>
                <div style={{ fontSize: 11, color: '#999', marginTop: 2, display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                  <span>{humanSize(file.size)}</span>
                  <span>·</span>
                  <span>{file.modified_at ? new Date(file.modified_at).toLocaleString() : ''}</span>
                </div>
                <Tooltip title={sourceTask ? `任务：${sourceTask.title}` : '未关联任务（智能体直写或无 task 记录）'}>
                  <div style={{
                    fontSize: 11, color: sourceTask ? '#722ed1' : '#b0b0b0',
                    marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>
                    {sourceTask ? `来源任务：${sourceTask.title}` : '独立产物（无任务关联）'}
                  </div>
                </Tooltip>
              </div>
              <Tooltip title={!conversationId ? '未选择会话' : '下载'}>
                <Button
                  type="primary"
                  size="small"
                  ghost
                  icon={<DownloadOutlined />}
                  loading={downloading === file.name}
                  disabled={!conversationId || (downloading !== null && downloading !== file.name)}
                  onClick={() => onDownload(file.name)}
                />
              </Tooltip>
            </div>
          )
        })}
      </div>
    </div>
  )
}

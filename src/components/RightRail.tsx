import { useEffect, useState } from 'react'
import { Avatar, Button, Empty, List, Spin, Tag, Tooltip, message } from 'antd'
import {
  TeamOutlined,
  ScheduleOutlined,
  ThunderboltOutlined,
  ApartmentOutlined,
  FolderOpenOutlined,
  FileOutlined,
  DownloadOutlined,
  CrownOutlined,
  RobotOutlined,
} from '@ant-design/icons'

import {
  groupApi,
  taskApi,
  type AgentDefinition,
  type Group,
  type GroupFile,
  type GroupMember,
  type Task,
  type TaskStatus,
} from '../services/api'
import { useBusEventContext } from '../contexts/BusEventContext'
import LeaderPanel from './LeaderPanel'
import WorkerTrace from './WorkerTrace'
import PlanConfirmCard from './PlanConfirmCard'
import MemberCapabilityOverview, {
  type DrawerMemberItem,
} from './MemberCapabilityOverview'

/** 任务状态 → 徽标色（与 TaskPage STATUS_COLOR 对齐，视觉一致）。 */
const TASK_STATUS_COLOR: Record<TaskStatus, string> = {
  submitted: '#999',
  working: '#1677ff',
  completed: '#52c41a',
  failed: '#ff4d4f',
  canceled: '#d9d9d9',
  input_required: '#faad14',
}

/** 文件大小格式化（与 GroupInfoDrawer 同逻辑，右栏文件 tab 复用）。 */
function formatFileSize(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  if (size < 1024 * 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(2)} MB`
  return `${(size / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

interface RightRailProps {
  /** 当前群组（null/未选群时各 tab 显示占位）。 */
  group: Group | null
  /** 全部智能体（成员 tab 解析能力 + 群主标识 + Worker tab 渲染）。 */
  agents: AgentDefinition[]
  /** 当前群成员（成员 tab 列表 + Worker tab 派发）。由父 ChatPage 透传，复用避免重复请求。 */
  members: GroupMember[]
  /** 成员加载中态。 */
  membersLoading?: boolean
}

/**
 * L3-02 RightRail 右栏上下文：tabs 成员/计划/执行/任务/文件，随 BusEventContext.groupId 切换。
 *
 * 三栏布局的右栏（ChatShell rightRail slot 注入），承载会话级上下文——切会话时各 tab
 * 内容跟随当前 groupId 刷新。这是 plan 诊断的根因 #5（会话级上下文无处安放）的解法。
 *
 * 五 tab（随 L3-03~07 逐步充实，本任务建骨架 + 基础内容）：
 *  - 成员（TeamOutlined）：成员列表（含群主 Tag）+ 能力概况入口。只读展示，编辑走 GroupInfoDrawer。
 *  - 计划（ScheduleOutlined）：PlanConfirmCard 协作计划步骤展示 + 确认/修改/直接执行
 *    入口（M12 计划确认闭环，复用 ChatPanel 顶部同源组件）。plan 为空时占位引导。
 *  - 执行（ThunderboltOutlined）：LeaderPanel 思考链 + 各 Worker WorkerTrace。Worker 按
 *    当前群成员派发（非群主成员各一 Tab/块）。监控复用，无新逻辑。
 *  - 任务（ApartmentOutlined）：当前会话任务列表（taskApi.list(groupId)），紧凑卡片 +
 *    状态色。DAG 缩略图留 L3 后续（需 ReactFlow，右栏 340px 不宜全图，先列表）。
 *  - 文件（FolderOpenOutlined）：群文件列表（groupApi.listFiles）+ 下载。复用 GroupInfoDrawer
 *    同源的 listFiles/downloadFile，但右栏常驻（不依赖抽屉开关）。
 *
 * 数据来源：
 *  - plan/events/agentStatuses/streaming：BusEventContext（全应用共享 WS，随 groupId 切换自动刷新）
 *  - members/agents：父 ChatPage 透传（复用，避免右栏重复请求）
 *  - tasks/groupFiles：本组件按 groupId 自加载（右栏专属，切会话重拉）
 *
 * 未选群（groupId null）时各 tab 显示「请选择会话」占位，不渲染内容。
 */
export default function RightRail({
  group,
  agents,
  members,
  membersLoading,
}: RightRailProps) {
  const { groupId } = useBusEventContext()

  // 任务 tab：按 groupId 加载当前会话任务（右栏专属，切会话重拉）
  const [tasks, setTasks] = useState<Task[]>([])
  const [tasksLoading, setTasksLoading] = useState(false)

  // 文件 tab：群文件列表（右栏常驻，不依赖抽屉开关）
  const [groupFiles, setGroupFiles] = useState<GroupFile[]>([])
  const [filesLoading, setFilesLoading] = useState(false)
  const [downloading, setDownloading] = useState<string | null>(null)

  useEffect(() => {
    if (!groupId) {
      setTasks([])
      setGroupFiles([])
      return
    }
    // 并发拉任务 + 文件（切会话时一起刷新）
    setTasksLoading(true)
    setFilesLoading(true)
    taskApi
      .list(groupId)
      .then(setTasks)
      .catch(() => setTasks([]))
      .finally(() => setTasksLoading(false))
    groupApi
      .listFiles(groupId)
      .then(setGroupFiles)
      .catch(() => setGroupFiles([]))
      .finally(() => setFilesLoading(false))
  }, [groupId])

  const handleDownload = async (file: GroupFile) => {
    if (!groupId) {
      message.warning('请先选择会话')
      return
    }
    setDownloading(file.name)
    try {
      const blob = await groupApi.downloadFile(groupId, file.name)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = file.name
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      setTimeout(() => URL.revokeObjectURL(url), 1000)
      message.success(`已下载 ${file.name}`)
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e))
    } finally {
      setDownloading(null)
    }
  }

  // 群主 id（成员 tab 标识群主 + Worker tab 排除群主）
  const coordinatorId = group?.coordinator_id || ''
  const workerMembers = members.filter((m) => m.agent_id !== coordinatorId)

  // 紧凑 tab 项（图标 + 中文，右栏 340px 宽度友好）
  const tabItems = [
    {
      key: 'members',
      label: (
        <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <TeamOutlined /> 成员
        </span>
      ),
      children: (
        <MembersTab
          group={group}
          members={members}
          membersLoading={membersLoading}
          coordinatorId={coordinatorId}
          agents={agents}
        />
      ),
    },
    {
      key: 'plan',
      label: (
        <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <ScheduleOutlined /> 计划
        </span>
      ),
      children: <PlanTab groupId={groupId} />,
    },
    {
      key: 'exec',
      label: (
        <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <ThunderboltOutlined /> 执行
        </span>
      ),
      children: <ExecTab workerMembers={workerMembers} />,
    },
    {
      key: 'tasks',
      label: (
        <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <ApartmentOutlined /> 任务
        </span>
      ),
      children: (
        <TasksTab tasks={tasks} loading={tasksLoading} />
      ),
    },
    {
      key: 'files',
      label: (
        <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <FolderOpenOutlined /> 文件
        </span>
      ),
      children: (
        <FilesTab
          files={groupFiles}
          loading={filesLoading}
          downloading={downloading}
          onDownload={handleDownload}
        />
      ),
    },
  ]

  return (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      {/*
        右栏 Tabs：tabPosition=top 横向标签（5 tab 图标+中文，340px 宽横向排得下）。
        本任务 L3-02 先用 antd Tabs 承载 5 tab + 基础内容；L3-03~07 充实各 tab。
        非受控 activeKey（默认 members），切会话不重置 tab（用户关注的上下文维度保持）。
      */}
      <TabsInline items={tabItems} />
    </div>
  )
}

/**
 * 内联 Tabs 包装：antd v6 Tabs 需从 antd 导入，此处延迟 import 避免顶部 import 块过长。
 * 实际直接用 antd Tabs（下方 import），本包装仅组织结构。
 */
import { Tabs } from 'antd'
import type { TabsProps } from 'antd'

function TabsInline({ items }: { items: TabsProps['items'] }) {
  return (
    <Tabs
      size="small"
      tabPosition="top"
      defaultActiveKey="members"
      items={items}
      style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}
      tabBarStyle={{ margin: '0 12px' }}
    />
  )
}

// ── 成员 tab ──────────────────────────────────────────────

function MembersTab({
  group,
  members,
  membersLoading,
  coordinatorId,
  agents,
}: {
  group: Group | null
  members: GroupMember[]
  membersLoading?: boolean
  coordinatorId: string
  agents: AgentDefinition[]
}) {
  if (!group) {
    return <EmptyState text="请选择会话查看成员" />
  }
  if (membersLoading) {
    return (
      <div style={{ textAlign: 'center', padding: 24 }}>
        <Spin size="small" />
      </div>
    )
  }
  // 群主置顶：构造含 isCoordinator 标记的列表（群主 + 普通成员）
  const coordinator = members.find((m) => m.agent_id === coordinatorId)
  const others = members.filter((m) => m.agent_id !== coordinatorId)
  const ordered: DrawerMemberItem[] = [
    ...(coordinator ? [{ ...coordinator, isCoordinator: true }] : []),
    ...others.map((m) => ({ ...m, isCoordinator: false })),
  ]

  return (
    <div style={{ padding: '8px 12px', overflowY: 'auto' }}>
      <List
        size="small"
        dataSource={ordered}
        renderItem={(item) => (
          <List.Item style={{ padding: '8px 0' }}>
            <List.Item.Meta
              avatar={
                <Avatar
                  size="small"
                  icon={item.isCoordinator ? <CrownOutlined /> : <RobotOutlined />}
                  style={{ background: item.isCoordinator ? '#722ed1' : '#1677ff' }}
                />
              }
              title={
                <span style={{ fontSize: 13 }}>
                  {item.alias || item.agent_name}
                  {item.isCoordinator && (
                    <Tag color="purple" style={{ marginLeft: 4, fontSize: 10, lineHeight: '14px', padding: '0 4px' }}>
                      群主
                    </Tag>
                  )}
                </span>
              }
              description={
                <span style={{ fontSize: 11, color: '#999' }}>{item.agent_role}</span>
              }
            />
          </List.Item>
        )}
      />

      {/* L3-03：成员能力概况（只读展示，技能/MCP 映射由组件内聚加载）。
          编辑（增删成员/换群主）走 GroupInfoDrawer，右栏只读——底部提示引导。 */}
      <MemberCapabilityOverview members={ordered} agents={agents} />

      <div style={{ fontSize: 11, color: '#bbb', textAlign: 'center', padding: '8px 0' }}>
        成员管理请点聊天头部群信息按钮
      </div>
    </div>
  )
}

// ── 计划 tab ──────────────────────────────────────────────

function PlanTab({ groupId }: { groupId: string | null }) {
  const { plan } = useBusEventContext()
  if (!groupId) return <EmptyState text="请选择会话查看计划" />

  // L3-04：计划 tab 聚焦「协作计划步骤」展示 + 确认/修改/直接执行入口。
  // 复用 PlanConfirmCard（M12 计划确认闭环组件）——它含完整计划步骤列表 +
  // 确认/修改/直接执行按钮（planApi.confirm/modify/direct），与 ChatPanel 顶部
  // 的计划卡同源（都接 plan + groupId）。右栏常驻展示计划，用户无需滚到聊天顶部。
  // plan 为空（无驻留计划）时显示占位，引导用户发消息触发 Leader 拆解。
  if (!plan || plan.length === 0) {
    return <EmptyState text="暂无协作计划（发消息触发群主拆解）" />
  }

  return (
    <div style={{ padding: '8px 12px', overflowY: 'auto' }}>
      <PlanConfirmCard groupId={groupId} plan={plan} />
    </div>
  )
}

// ── 执行 tab ──────────────────────────────────────────────

function ExecTab({
  workerMembers,
}: {
  workerMembers: GroupMember[]
}) {
  const { groupId, events } = useBusEventContext()
  if (!groupId) return <EmptyState text="请选择会话查看执行追踪" />

  // L3-05：执行 tab = LeaderPanel 思考链 + 各 Worker WorkerTrace。
  // - LeaderPanel 整块复用（含思考链 + 协作计划 + 派工时间线三段 Collapse）——思考链是
  //   协调者决策过程，时间线是派工起点，都与「执行」相关；计划段虽与计划 tab 重复，但
  //   Collapse 可折叠，用户自选展开，整块复用最稳（不拆 LeaderPanel 内部）。
  // - Worker 部分按当前群成员派发（非群主成员各一 WorkerTrace）。无 worker 时该部分
  //   占位，但不跳过 LeaderPanel——群主可能正在思考（coord_think）尚未派工，
  //   思考链仍该展示。events 全空（无任何活动）时整体占位。
  const hasLeaderActivity = events.some(
    (e) => e.kind === 'coord_think' || e.kind === 'dispatch' || e.kind === 'complete' || e.kind === 'failed',
  )

  if (!hasLeaderActivity && workerMembers.length === 0) {
    return <EmptyState text="暂无执行活动（发消息触发群主拆解）" />
  }

  return (
    <div style={{ padding: '8px 12px', overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={{ fontSize: 12, color: '#999', marginBottom: 4 }}>
        协调者思考链
      </div>
      <LeaderPanel />
      <div style={{ fontSize: 12, color: '#999', margin: '8px 0 4px' }}>
        子智能体执行追踪（{workerMembers.length}）
      </div>
      {workerMembers.length === 0 ? (
        <EmptyState text="该会话暂无子智能体" />
      ) : (
        workerMembers.map((m) => (
          <WorkerTrace
            key={m.agent_id}
            agentId={m.agent_id}
            agentName={m.alias || m.agent_name}
          />
        ))
      )}
    </div>
  )
}

// ── 任务 tab ──────────────────────────────────────────────

function TasksTab({
  tasks,
  loading,
}: {
  tasks: Task[]
  loading: boolean
}) {
  const { groupId } = useBusEventContext()
  if (!groupId) return <EmptyState text="请选择会话查看任务" />
  if (loading) {
    return (
      <div style={{ textAlign: 'center', padding: 24 }}>
        <Spin size="small" />
      </div>
    )
  }
  if (tasks.length === 0) return <EmptyState text="暂无任务（发消息触发群主拆解派工）" />

  // L3-06：任务 tab = DAG 缩略 + 任务卡片列表。
  // - DAG 缩略：纯 SVG 依赖图（按 dag_order 纵向排布 + dependencies 连线），右栏 340px
  //   宽不宜用 ReactFlow（全图交互需 /tasks 路由）。轻量 SVG 既体现任务依赖关系，
  //   又不引入 ReactFlow 重量级渲染 + 高度链复杂度（右栏 tab 内 flex 高度受限）。
  // - 任务卡片列表：状态色边框 + 标题 + 派发 agent，紧凑可滚动（DAG 缩略只示拓扑，
  //   详情看卡片）。DAG 与列表互补：DAG 看依赖结构，列表看状态/派发。
  return (
    <div style={{ padding: '8px 12px', overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 8 }}>
      <TaskDagMini tasks={tasks} />
      <div style={{ fontSize: 12, color: '#999', margin: '4px 0' }}>
        任务列表（{tasks.length}）
      </div>
      {tasks.map((t) => {
        const color = TASK_STATUS_COLOR[t.status] || '#d9d9d9'
        return (
          <div
            key={t.id}
            style={{
              borderLeft: `3px solid ${color}`,
              padding: '6px 10px',
              background: '#fafafa',
              borderRadius: 4,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 2 }}>
              <span style={{ fontSize: 13, fontWeight: 500, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {t.title}
              </span>
              <Tag color={color} style={{ margin: 0, fontSize: 10, padding: '0 4px' }}>
                {t.status}
              </Tag>
            </div>
            <div style={{ fontSize: 11, color: '#999' }}>
              {t.assigned_agent_id ? `派发: ${t.assigned_agent_id.slice(0, 12)}` : '待分配'}
            </div>
          </div>
        )
      })}
    </div>
  )
}

/**
 * L3-06 任务 DAG 缩略图：纯 SVG 依赖关系图。
 *
 * 右栏 340px 宽不宜用 ReactFlow（需全屏稳定容器，/tasks 路由才合适）。这里用轻量 SVG：
 *  - 节点：按 dag_order（无则按数组序）纵向排布，状态色圆点 + 任务序号。
 *  - 连线：dependencies 画贝塞尔曲线连接依赖任务 → 当前任务。
 *  - 无依赖的根任务在顶部，依赖链向下展开，体现 DAG 拓扑。
 *
 * 紧凑设计：节点宽度自适应右栏，高度按任务数线性增长（每节点 28px 行高），最多展示
 * 前 20 个任务（超出滚动，避免任务极多时 SVG 撑爆右栏）。点击节点暂无交互（详情看
 * 下方卡片列表），保持缩略图纯展示职责。
 */
function TaskDagMini({ tasks }: { tasks: Task[] }) {
  // 按 dag_order 排序（null 视为 0 置顶），无 dag_order 时按数组原序
  const sorted = [...tasks].sort(
    (a, b) => (a.dag_order ?? 0) - (b.dag_order ?? 0),
  )
  // 任务 id → 在 sorted 中的索引（连线定位用）
  const idToIndex = new Map<string, number>()
  sorted.forEach((t, i) => idToIndex.set(t.id, i))

  // 最多展示前 20 个（避免任务过多 SVG 撑爆）
  const visible = sorted.slice(0, 20)
  const ROW_H = 28
  const DOT_R = 6
  const svgHeight = Math.max(visible.length * ROW_H + 16, 60)
  const svgWidth = 300

  return (
    <div
      style={{
        background: '#fafafa',
        borderRadius: 6,
        padding: 8,
        border: '1px solid #f0f0f0',
      }}
    >
      <div style={{ fontSize: 12, color: '#666', marginBottom: 4, display: 'flex', alignItems: 'center', gap: 4 }}>
        <ApartmentOutlined style={{ color: '#1677ff' }} />
        依赖图（DAG 缩略）
        {sorted.length > visible.length && (
          <span style={{ color: '#bbb', fontSize: 11 }}>· 仅前 {visible.length}/{sorted.length}</span>
        )}
      </div>
      <svg
        width="100%"
        height={svgHeight}
        viewBox={`0 0 ${svgWidth} ${svgHeight}`}
        style={{ display: 'block' }}
      >
        {/* 连线（先画，节点覆盖在上） */}
        {visible.map((t, i) => {
          const deps = t.dependencies || []
          return deps.map((depId) => {
            const fromIdx = idToIndex.get(depId)
            if (fromIdx === undefined || fromIdx >= visible.length) return null
            const fromY = fromIdx * ROW_H + ROW_H / 2 + 8
            const toY = i * ROW_H + ROW_H / 2 + 8
            const fromX = 24 + DOT_R
            const toX = 24
            // 贝塞尔曲线：从依赖节点右侧 → 当前节点左侧
            return (
              <path
                key={`${depId}->${t.id}`}
                d={`M ${fromX} ${fromY} C ${fromX + 20} ${fromY}, ${toX - 20} ${toY}, ${toX} ${toY}`}
                stroke="#d9d9d9"
                strokeWidth={1.5}
                fill="none"
              />
            )
          })
        })}
        {/* 节点 */}
        {visible.map((t, i) => {
          const color = TASK_STATUS_COLOR[t.status] || '#d9d9d9'
          const y = i * ROW_H + ROW_H / 2 + 8
          return (
            <g key={t.id}>
              <circle cx={24} cy={y} r={DOT_R} fill={color} stroke="#fff" strokeWidth={1.5} />
              <text
                x={24 + DOT_R + 8}
                y={y + 4}
                fontSize={11}
                fill="#333"
              >
                {String(t.title).length > 22
                  ? String(t.title).slice(0, 22) + '…'
                  : t.title}
              </text>
            </g>
          )
        })}
      </svg>
    </div>
  )
}

// ── 文件 tab ──────────────────────────────────────────────

function FilesTab({
  files,
  loading,
  downloading,
  onDownload,
}: {
  files: GroupFile[]
  loading: boolean
  downloading: string | null
  onDownload: (file: GroupFile) => void
}) {
  const { groupId } = useBusEventContext()
  if (!groupId) return <EmptyState text="请选择会话查看群文件" />
  if (loading) {
    return (
      <div style={{ textAlign: 'center', padding: 24 }}>
        <Spin size="small" />
      </div>
    )
  }
  if (files.length === 0) {
    return <EmptyState text="暂无群文件（任务完成后自动扫描工作区产物）" />
  }
  // L3-07：文件 tab = 群文件列表 + 下载。复用 GroupInfoDrawer 同源 listFiles/downloadFile
  //（数据由 RightRail 顶层 effect 按 groupId 加载，onDownload 回调调 groupApi.downloadFile）。
  // 文件项视觉与 GroupInfoDrawer 文件块一致：彩色类型图标方块 + 文件名 + 大小·修改时间 + 下载按钮。
  return (
    <div style={{ padding: '8px 12px', overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 4 }}>
      {files.map((file) => {
        const ext = file.name.split('.').pop()?.toLowerCase() ?? ''
        const isCode = ['py', 'js', 'ts', 'tsx', 'css', 'html', 'json', 'yaml', 'sql', 'md'].includes(ext)
        const isDoc = ['doc', 'docx', 'pdf', 'txt'].includes(ext)
        const iconColor = isCode ? '#10b981' : isDoc ? '#f59e0b' : '#8c8c8c'
        return (
          <div
            key={file.name}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              padding: '6px 8px',
              borderRadius: 4,
              background: '#fafafa',
            }}
          >
            {/* 彩色类型图标方块（与 GroupInfoDrawer 文件块一致：code绿/doc黄/其他灰） */}
            <div
              style={{
                width: 28,
                height: 28,
                borderRadius: 6,
                background: isCode ? '#d1fae5' : isDoc ? '#fef3c7' : '#f0f0f0',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexShrink: 0,
              }}
            >
              <FileOutlined style={{ color: iconColor, fontSize: 14 }} />
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 12, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {file.name}
              </div>
              <div style={{ fontSize: 10, color: '#bbb' }}>
                {formatFileSize(file.size)}
                {file.modified_at && ` · ${new Date(file.modified_at).toLocaleDateString()}`}
              </div>
            </div>
            <Tooltip title="下载">
              <Button
                type="text"
                size="small"
                icon={<DownloadOutlined />}
                loading={downloading === file.name}
                disabled={downloading !== null && downloading !== file.name}
                onClick={() => onDownload(file)}
              />
            </Tooltip>
          </div>
        )
      })}
    </div>
  )
}

// ── 占位 ──────────────────────────────────────────────────

function EmptyState({ text }: { text: string }) {
  return (
    <div style={{ padding: '24px 12px' }}>
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={text}
        style={{ margin: 0 }}
      />
    </div>
  )
}

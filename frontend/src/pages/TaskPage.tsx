import { useEffect, useMemo, useState } from 'react'
import {
  Button,
  Card,
  Space,
  message,
  Empty,
  Select,
  Tag,
} from 'antd'
import {
  DashboardOutlined,
  ClockCircleOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  PauseCircleOutlined,
} from '@ant-design/icons'
import type { Node, Edge } from 'reactflow'
import ReactFlow, { Background, Controls, MiniMap } from 'reactflow'
import 'reactflow/dist/style.css'

import { groupApi, taskApi, type Task, type TaskStatus } from '../services/api'
import LogPanel from '../components/LogPanel'

const STATUS_ICON: Record<TaskStatus, React.ReactNode> = {
  submitted: <ClockCircleOutlined />,
  working: <DashboardOutlined style={{ color: '#1677ff' }} />,
  completed: <CheckCircleOutlined style={{ color: '#52c41a' }} />,
  failed: <CloseCircleOutlined style={{ color: '#ff4d4f' }} />,
  canceled: <CloseCircleOutlined />,
  input_required: <PauseCircleOutlined style={{ color: '#faad14' }} />,
}

const STATUS_COLOR: Record<TaskStatus, string> = {
  submitted: '#999',
  working: '#1677ff',
  completed: '#52c41a',
  failed: '#ff4d4f',
  canceled: '#d9d9d9',
  input_required: '#faad14',
}

function buildNodesEdges(tasks: Task[]): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = tasks.map((t, i) => ({
    id: t.id,
    position: { x: i * 220, y: (t.dependencies || []).length * 100 },
    data: {
      label: (
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontWeight: 600 }}>{t.title}</div>
          <Tag color={STATUS_COLOR[t.status] || 'default'}>
            {STATUS_ICON[t.status]} {t.status}
          </Tag>
        </div>
      ),
    },
    style: {
      borderColor: STATUS_COLOR[t.status] || '#d9d9d9',
      borderWidth: 2,
      width: 180,
    },
  }))

  const edges: Edge[] = []
  tasks.forEach((t) => {
    (t.dependencies || []).forEach((dep) => {
      edges.push({ id: `${dep}->${t.id}`, source: dep, target: t.id, animated: true })
    })
  })

  return { nodes, edges }
}

export default function TaskPage() {
  const [groups, setGroups] = useState<{ id: string; name: string }[]>([])
  const [selectedGroup, setSelectedGroup] = useState<string | null>(null)
  const [tasks, setTasks] = useState<Task[]>([])
  const [loading, setLoading] = useState(false)
  const [expandedTask, setExpandedTask] = useState<string | null>(null)

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
    if (!selectedGroup) return
    setLoading(true)
    taskApi
      .list(selectedGroup)
      .then(setTasks)
      .catch(() => message.error('获取任务失败'))
      .finally(() => setLoading(false))
  }, [selectedGroup])

  const { nodes, edges } = useMemo(() => buildNodesEdges(tasks), [tasks])

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h2 style={{ margin: 0 }}>任务看板</h2>
        <Select
          style={{ width: 240 }}
          placeholder="选择群组"
          value={selectedGroup}
          onChange={setSelectedGroup}
          options={groups.map((g) => ({ value: g.id, label: g.name }))}
        />
      </div>

      {/* DAG 图 */}
      <Card title="任务依赖图" loading={loading} style={{ height: 360 }}>
        {tasks.length === 0 ? (
          <Empty description="暂无任务" />
        ) : (
          <div style={{ height: 300 }}>
            <ReactFlow nodes={nodes} edges={edges} fitView>
              <MiniMap />
              <Controls />
              <Background />
            </ReactFlow>
          </div>
        )}
      </Card>

      {/* 智能体状态卡片 */}
      <Card title="执行状态">
        {tasks.length === 0 && !loading ? (
          <Empty description="暂无任务" />
        ) : (
          <Space wrap>
            {tasks.map((t) => (
              <Card
                key={t.id}
                size="small"
                style={{
                  width: 240,
                  borderLeft: `4px solid ${STATUS_COLOR[t.status] || '#d9d9d9'}`,
                }}
                title={
                  <Space>
                    {STATUS_ICON[t.status]}
                    {t.title}
                  </Space>
                }
              >
                <p style={{ margin: '0 0 4px' }}>
                  状态：<Tag color={STATUS_COLOR[t.status]}>{t.status}</Tag>
                </p>
                <p style={{ margin: '0 0 4px' }}>
                  智能体：
                  {t.assigned_agent_id ?? '待分配'}
                </p>
                <Button
                  size="small"
                  onClick={() => setExpandedTask(expandedTask === t.id ? null : t.id)}
                >
                  {expandedTask === t.id ? '收起日志 ▲' : '查看日志 ▼'}
                </Button>
                {expandedTask === t.id && <LogPanel taskId={t.id} groupId={selectedGroup ?? undefined} />}
              </Card>
            ))}
          </Space>
        )}
      </Card>

      {/* 交付物 */}
      <Card title="交付物">
        {tasks.filter((t) => t.artifact_path).length === 0 ? (
          <Empty description="暂无交付物" />
        ) : (
          <Space wrap>
            {tasks
              .filter((t) => t.artifact_path)
              .map((t) => (
                <Button key={t.id} type="link">
                  📦 {t.title} - {t.artifact_path}
                </Button>
              ))}
          </Space>
        )}
      </Card>
    </div>
  )
}

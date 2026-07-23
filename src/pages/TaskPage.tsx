import { useEffect, useMemo, useState } from 'react'
import {
  Button,
  Card,
  Space,
  Tooltip,
  Tag,
  message,
  Empty,
  Select,
} from 'antd'
import {
  DashboardOutlined,
  ClockCircleOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  PauseCircleOutlined,
  DownloadOutlined,
  PaperClipOutlined,
} from '@ant-design/icons'
import type { Node, Edge } from 'reactflow'
import ReactFlow, { Background, Controls, MiniMap } from 'reactflow'
import 'reactflow/dist/style.css'

import { groupApi, taskApi, type Task, type TaskStatus } from '../services/api'
import { fileIconFor, saveBlob, humanSize } from '../lib/fileIcon'
import LogPanel from '../components/LogPanel'

const STATUS_ICON: Record<TaskStatus, React.ReactNode> = {
  submitted: <ClockCircleOutlined />,
  working: <DashboardOutlined style={{ color: '#F26522' }} />,
  completed: <CheckCircleOutlined style={{ color: '#52c41a' }} />,
  failed: <CloseCircleOutlined style={{ color: '#ff4d4f' }} />,
  canceled: <CloseCircleOutlined />,
  input_required: <PauseCircleOutlined style={{ color: '#faad14' }} />,
}

const STATUS_COLOR: Record<TaskStatus, string> = {
  submitted: '#999',
  working: '#F26522',
  completed: '#52c41a',
  failed: '#ff4d4f',
  canceled: '#d9d9d9',
  input_required: '#faad14',
}

/** PL-12: an artifact file entry extracted from Task.artifact.manifest. */
interface ArtifactFile {
  name: string
  path: string
  size: number
  modified_at: string
}

/** PL-12: extract the artifact manifest's file list from a task, if any.
 *
 * The backend ``scan_workspace_artifacts`` records ``artifact`` as
 * ``{"files": [{name, path, size, modified_at}, ...]}``. Falls back to a
 * single synthetic entry built from ``artifact_path`` when the manifest is
 * absent (older tasks / coordinator-only), so the primary file is always
 * downloadable even without the full manifest. */
function extractArtifacts(t: Task): ArtifactFile[] {
  const manifest = t.artifact as { files?: ArtifactFile[] } | null
  if (manifest && Array.isArray(manifest.files) && manifest.files.length > 0) {
    return manifest.files
  }
  if (t.artifact_path) {
    const segs = t.artifact_path.split('/')
    return [
      {
        name: segs[segs.length - 1],
        path: t.artifact_path,
        size: 0,
        modified_at: '',
      },
    ]
  }
  return []
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
  const [downloading, setDownloading] = useState<string | null>(null)

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

  /** PL-12: all (task, file) pairs across selected group — flat list of
   * downloadable artifacts. Newest task first; within a task, manifest order
   * (already newest-file-first from scan_workspace_artifacts). */
  const artifactEntries = useMemo(() => {
    const out: { task: Task; file: ArtifactFile }[] = []
    for (const t of tasks) {
      for (const f of extractArtifacts(t)) {
        out.push({ task: t, file: f })
      }
    }
    return out
  }, [tasks])

  const handleDownload = async (groupId: string, file: ArtifactFile) => {
    if (!groupId) {
      message.warning('请先选择群组')
      return
    }
    const key = file.path
    setDownloading(key)
    try {
      const blob = await groupApi.downloadFile(groupId, file.path)
      saveBlob(blob, file.name)
      message.success(`已下载 ${file.name}`)
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e))
    } finally {
      setDownloading(null)
    }
  }

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        minHeight: 0,
        gap: 16,
        padding: 16,
      }}
    >
      {/* SH-05：降级为 SettingsDrawer Tab，页级 h2「任务看板」与 Tab 标题「任务」重复，移除；
          Select 独占该行右对齐（原与 h2 space-between，现 flex-end）。 */}
      <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', flexShrink: 0 }}>
        <Select
          style={{ width: 240 }}
          placeholder="选择群组"
          value={selectedGroup}
          onChange={setSelectedGroup}
          options={groups.map((g) => ({ value: g.id, label: g.name }))}
        />
      </div>

      {/* DAG 图
          L1-04：去写死 height:300 / Card height:360 —— 改 flex 弹性容器。
          ReactFlow 要求父容器有确定 width+height（否则报 "parent container needs
          width and a height"）。全屏路由下（/tasks）外层高度链已通（Layout: height:100%
          → Content flex:1 minHeight:0 → 本页 height:100%），DAG 卡片 flex:1 填充剩余
          高度，内部 ReactFlow 容器 flex:1 + minHeight:0 随卡片宽度变化自适应，抽屉
          开合/窗口缩放不再断图。Empty 占位时卡片塌缩为内容高度（flex:0 auto）。 */}
      <Card
        title="任务依赖图"
        loading={loading}
        styles={{ body: { flex: 1, minHeight: 0, padding: 0 } }}
        style={{
          flex: tasks.length === 0 ? '0 0 auto' : 1,
          minHeight: 0,
          display: 'flex',
          flexDirection: 'column',
        }}
      >
        {tasks.length === 0 ? (
          <Empty description="暂无任务" style={{ padding: 24 }} />
        ) : (
          <div style={{ flex: 1, minHeight: 0 }}>
            <ReactFlow nodes={nodes} edges={edges} fitView>
              <MiniMap />
              <Controls />
              <Background />
            </ReactFlow>
          </div>
        )}
      </Card>

      {/* 智能体状态卡片
          L1-04：下方状态卡 + 交付物卡设 flexShrink:0 + overflow auto，DAG 卡 flex:1 抢高度
          时这两张卡按内容高度不缩，溢出由外层 padding 容器滚动（body auto）。 */}
      <Card title="执行状态" style={{ flexShrink: 0 }}>
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
                {t.artifact_path && (
                  <p style={{ margin: '0 0 4px', fontSize: 12, color: '#8c8c8c' }}>
                    <PaperClipOutlined /> {t.artifact_path}
                  </p>
                )}
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

      {/* 交付物 — PL-12 产物文件卡片 + 下载入口 */}
      <Card
        style={{ flexShrink: 0 }}
        title={
          <span>
            <PaperClipOutlined /> 交付物
            {artifactEntries.length > 0 && (
              <Tag color="orange" style={{ marginInlineStart: 8 }}>
                {artifactEntries.length} 个文件
              </Tag>
            )}
          </span>
        }
      >
        {artifactEntries.length === 0 ? (
          <Empty description="暂无交付物（任务完成后自动扫描工作区产物）" />
        ) : (
          <Space wrap>
            {artifactEntries.map(({ task, file }) => {
              const key = `${task.id}:${file.path}`
              const isPrimary = file.path === task.artifact_path
              return (
                <Card
                  key={key}
                  size="small"
                  style={{
                    width: 280,
                    borderLeft: `4px solid ${STATUS_COLOR[task.status] || '#d9d9d9'}`,
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                    {fileIconFor(file.name, { fontSize: 18 })}
                    <Tooltip title={file.path}>
                      <span style={{ fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {file.name}
                      </span>
                    </Tooltip>
                    {isPrimary && <Tag color="gold" style={{ marginInlineStart: 0 }}>主产物</Tag>}
                  </div>
                  <div style={{ fontSize: 12, color: '#8c8c8c', marginBottom: 4 }}>
                    来自任务：{task.title}
                  </div>
                  <div style={{ fontSize: 12, color: '#8c8c8c', marginBottom: 8 }}>
                    {file.size > 0 && <span>{humanSize(file.size)} · </span>}
                    <Tooltip title={file.path}>
                      <span>{file.path}</span>
                    </Tooltip>
                  </div>
                  <Button
                    type="primary"
                    size="small"
                    icon={<DownloadOutlined />}
                    loading={downloading === file.path}
                    disabled={downloading !== null && downloading !== file.path}
                    onClick={() => selectedGroup && handleDownload(selectedGroup, file)}
                  >
                    下载
                  </Button>
                </Card>
              )
            })}
          </Space>
        )}
      </Card>
    </div>
  )
}

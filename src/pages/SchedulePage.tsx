import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Button,
  Card,
  DatePicker,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Timeline,
  Tooltip,
  message,
} from 'antd'
import {
  CalendarOutlined,
  ClockCircleOutlined,
  DeleteOutlined,
  FieldTimeOutlined,
  HistoryOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import dayjs, { type Dayjs } from 'dayjs'
import {
  agentApi,
  groupApi,
  scheduledTaskApi,
  type AgentDefinition,
  type Group,
  type ScheduledTask,
  type ScheduledTaskCreatePayload,
  type ScheduleType,
  type ScheduledTaskRun,
} from '../services/api'

/* ── 调度类型 → Tag 配色 + 中文标签 ──
 * 三类对齐后端 engine/scheduler._build_trigger 的分支（cron/once/默认 interval）。 */
const SCHEDULE_META: Record<
  ScheduleType,
  { color: string; label: string }
> = {
  cron: { color: 'geekblue', label: 'cron 表达式' },
  interval: { color: 'blue', label: '定间隔' },
  once: { color: 'purple', label: '一次性' },
}

/* TM-03 调度类型 Segmented 选项（图标 + 文案）。
 * 三类对齐后端 _build_trigger 分支，顺序 cron→interval→once 与 SCHEDULE_META 一致。 */
const SCHEDULE_TYPE_OPTIONS = [
  {
    value: 'interval' as ScheduleType,
    label: (
      <Space size={4}>
        <FieldTimeOutlined />
        <span>定间隔</span>
      </Space>
    ),
  },
  {
    value: 'cron' as ScheduleType,
    label: (
      <Space size={4}>
        <ClockCircleOutlined />
        <span>cron 表达式</span>
      </Space>
    ),
  },
  {
    value: 'once' as ScheduleType,
    label: (
      <Space size={4}>
        <CalendarOutlined />
        <span>一次性</span>
      </Space>
    ),
  },
]

/* cron 常用预设（点击直接填入 cron 表达式，降低手写门槛）。
 * 与官方 crontab 五段式一致（分 时 日 月 周），后端 CronTrigger.from_crontab 直接解析。 */
const CRON_PRESETS: { label: string; value: string }[] = [
  { label: '每小时整点', value: '0 * * * *' },
  { label: '每天 00:00', value: '0 0 * * *' },
  { label: '每天 08:00', value: '0 8 * * *' },
  { label: '每天 12:00', value: '0 12 * * *' },
  { label: '工作日 09:00', value: '0 9 * * 1-5' },
]

/* 频率摘要：把 schedule_type + 相关字段拼成一句话（如「每 3600 秒」「0 8 * * *」「2026-07-11 09:00」）。
 * 这是纯展示文案，不参与调度——调度真源在后端 APScheduler job。 */
function scheduleSummary(t: ScheduledTask): string {
  if (t.schedule_type === 'cron') {
    return t.cron ? `cron: ${t.cron}` : 'cron（未配置表达式）'
  }
  if (t.schedule_type === 'once') {
    return t.run_at ? `定时: ${t.run_at}` : '一次性（未配置时刻）'
  }
  // interval
  const secs = t.interval_seconds || 0
  if (secs <= 0) return '定间隔（未配置秒数）'
  if (secs % 86400 === 0) return `每 ${secs / 86400} 天`
  if (secs % 3600 === 0) return `每 ${secs / 3600} 小时`
  if (secs % 60 === 0) return `每 ${secs / 60} 分钟`
  return `每 ${secs} 秒`
}

/* 空时间戳兜底（后端新创建任务 created_at/updated_at 必填，但首屏 list 可能含历史脏数据）。 */
function fmtTime(ts: string | null | undefined): string {
  if (!ts) return '—'
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  return d.toLocaleString('zh-CN', { hour12: false })
}

/* ScheduledTaskRun.status → Tag 配色（对齐后端 pending|running|success|failed 四态）。 */
const RUN_STATUS_META: Record<string, { color: string; label: string }> = {
  pending: { color: 'default', label: '待执行' },
  running: { color: 'processing', label: '执行中' },
  success: { color: 'success', label: '成功' },
  failed: { color: 'error', label: '失败' },
}

export default function SchedulePage() {
  const [tasks, setTasks] = useState<ScheduledTask[]>([])
  const [agents, setAgents] = useState<AgentDefinition[]>([])
  const [groups, setGroups] = useState<Group[]>([])
  const [loading, setLoading] = useState(false)

  /* 单卡 action loading（按 task id 标记，防重复点击，互不阻塞）。 */
  const [actingIds, setActingIds] = useState<Set<string>>(new Set())

  /* TM-07 历史抽屉：historyDrawer.task 指定当前抽屉绑定任务，runs 缓存该任务历史。 */
  const [historyDrawer, setHistoryDrawer] = useState<{
    task: ScheduledTask | null
    runs: ScheduledTaskRun[]
    loading: boolean
  }>({ task: null, runs: [], loading: false })

  /* TM-02/03 创建任务表单 Modal。
   * 字段：name（必填）/ content 派发内容（必填）/ group_id 群组（必填，scheduler 据此
   * push 到正确引擎 inbox）/ agent_id 目标智能体（必填）/ schedule_type 调度类型（cron/
   * interval/once，Segmented 切换，Form.useWatch 监听驱动条件字段显隐）/ 对应调度参数
   * （cron→cron 表达式 / interval→数字+单位换算 interval_seconds / once→日期时刻 run_at）/
   * enabled 创建后启用。
   * TM-03 把 TM-02 的「定间隔频率」升级为三种调度类型可选，按类型分流提交字段
   * （cron/interval/once 各传对应字段，不相关字段 omit 不传）。 */
  const [createOpen, setCreateOpen] = useState(false)
  const [createLoading, setCreateLoading] = useState(false)
  /* 频率用「数字 + 单位」录入，提交时换算成 interval_seconds（秒）。单位固定枚举，
   * 默认「小时」——定时巡检/晨报类任务按小时排是最常见场景。 */
  type FreqUnit = 'seconds' | 'minutes' | 'hours' | 'days'
  const FREQ_UNIT_SECONDS: Record<FreqUnit, number> = {
    seconds: 1,
    minutes: 60,
    hours: 3600,
    days: 86400,
  }
  type CreateFormValues = {
    name: string
    content: string
    group_id: string
    agent_id: string
    schedule_type: ScheduleType
    freq_value: number
    freq_unit: FreqUnit
    cron: string
    run_at: Dayjs | null
    enabled: boolean
  }
  const [form] = Form.useForm<CreateFormValues>()
  /* 监听 schedule_type 驱动条件字段显隐（与 McpPage useWatch transport 同模式）。 */
  const scheduleType = Form.useWatch('schedule_type', form) ?? 'interval'

  const openCreate = () => {
    form.resetFields()
    form.setFieldsValue({
      schedule_type: 'interval',
      freq_value: 1,
      freq_unit: 'hours',
      cron: '',
      run_at: null,
      enabled: true,
    })
    setCreateOpen(true)
  }

  /* 提交创建：按 schedule_type 组装 ScheduledTaskCreatePayload 调 scheduledTaskApi.create。
   * 三种类型分流字段：
   *   · interval → interval_seconds = freq_value × FREQ_UNIT_SECONDS[freq_unit]（cron/run_at 不传）
   *   · cron     → cron 表达式（interval_seconds/run_at 不传）
   *   · once     → run_at = run_at.toISOString()（interval_seconds/cron 不传）
   * 后端全可选，前端按类型 omit 不相关字段（与 McpPage transport 分流同语义）。
   * 后端 create 后若 enabled 自动 add_job 注册 APScheduler，返回落库 ScheduledTask。 */
  const handleCreate = async () => {
    let values: CreateFormValues
    try {
      values = await form.validateFields()
    } catch {
      return // 字段校验失败，Form 已标红，不重复 message
    }
    // 类型相关字段提交前校验（条件必填，Form.Item required 动态绑定已兜底，此处二次防御）
    const stype = values.schedule_type ?? 'interval'
    if (stype === 'interval') {
      const intervalSeconds =
        (values.freq_value || 0) * FREQ_UNIT_SECONDS[values.freq_unit || 'hours']
      if (intervalSeconds <= 0) {
        message.error('定间隔频率必须大于 0')
        return
      }
    } else if (stype === 'cron') {
      const cron = (values.cron || '').trim()
      if (!cron) {
        message.error('请输入 cron 表达式')
        return
      }
    } else if (stype === 'once') {
      if (!values.run_at) {
        message.error('请选择一次性触发时刻')
        return
      }
    }

    setCreateLoading(true)
    try {
      const payload: ScheduledTaskCreatePayload = {
        name: values.name,
        content: values.content,
        agent_id: values.agent_id,
        group_id: values.group_id,
        schedule_type: stype,
        enabled: values.enabled ?? true,
      }
      if (stype === 'interval') {
        payload.interval_seconds =
          (values.freq_value || 0) * FREQ_UNIT_SECONDS[values.freq_unit || 'hours']
      } else if (stype === 'cron') {
        payload.cron = (values.cron || '').trim()
      } else if (stype === 'once') {
        payload.run_at = values.run_at!.toISOString()
      }
      const task = await scheduledTaskApi.create(payload)
      message.success(`已创建定时任务「${task.name}」`)
      setCreateOpen(false)
      form.resetFields()
      await fetchAll()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '创建定时任务失败')
    } finally {
      setCreateLoading(false)
    }
  }

  /* agent id → name / group id → name，列表卡片展示目标智能体/群组用。 */
  const agentNameMap = useMemo(() => {
    const m = new Map<string, string>()
    agents.forEach((a) => m.set(a.id, a.name))
    return m
  }, [agents])

  const groupNameMap = useMemo(() => {
    const m = new Map<string, string>()
    groups.forEach((g) => m.set(g.id, g.name))
    return m
  }, [groups])

  const fetchAll = useCallback(async () => {
    setLoading(true)
    try {
      const [taskList, agentList, groupList] = await Promise.all([
        scheduledTaskApi.list(),
        agentApi.list(),
        groupApi.list(),
      ])
      setTasks(taskList)
      setAgents(agentList)
      setGroups(groupList)
    } catch (e) {
      message.error(e instanceof Error ? e.message : '获取定时任务列表失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchAll()
  }, [fetchAll])

  /* TM-04 立即执行：force fire，即使 paused 也跑（后端 _fire force=True 跳过 enabled 检查）。 */
  const handleRunNow = async (task: ScheduledTask) => {
    setActingIds((prev) => new Set(prev).add(task.id))
    try {
      await scheduledTaskApi.runNow(task.id)
      message.success(`已触发「${task.name}」立即执行`)
      // 派发后拉一次历史（若抽屉正开着这条任务，刷新 runs）
      if (historyDrawer.task?.id === task.id) {
        openHistory(task)
      }
    } catch (e) {
      message.error(e instanceof Error ? e.message : '立即执行失败')
    } finally {
      setActingIds((prev) => {
        const next = new Set(prev)
        next.delete(task.id)
        return next
      })
    }
  }

  /* TM-05 暂停/恢复：toggle enabled。后端返回更新后的 ScheduledTask（enabled 翻转 + job 重建），
   * 前端拿返回值局部更新 state（与 mcpApi.enable/disable 同模式）。 */
  const handleToggle = async (task: ScheduledTask) => {
    setActingIds((prev) => new Set(prev).add(task.id))
    try {
      const next = task.enabled
        ? await scheduledTaskApi.pause(task.id)
        : await scheduledTaskApi.resume(task.id)
      if (next) {
        setTasks((prev) => prev.map((t) => (t.id === task.id ? next : t)))
        message.success(`已${next.enabled ? '恢复' : '暂停'}「${next.name}」`)
      }
    } catch (e) {
      message.error(e instanceof Error ? e.message : '操作失败')
    } finally {
      setActingIds((prev) => {
        const next = new Set(prev)
        next.delete(task.id)
        return next
      })
    }
  }

  /* TM-06 删除：后端先 remove_job 再删库。Popconfirm 二次确认。 */
  const handleDelete = async (task: ScheduledTask) => {
    try {
      await scheduledTaskApi.delete(task.id)
      message.success(`已删除「${task.name}」`)
      // 若抽屉正绑这条任务，一并关闭
      if (historyDrawer.task?.id === task.id) {
        setHistoryDrawer({ task: null, runs: [], loading: false })
      }
      fetchAll()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '删除失败')
    }
  }

  /* TM-07 历史抽屉：拉取该任务的 ScheduledTaskRun[]（按时间倒序）。 */
  const openHistory = async (task: ScheduledTask) => {
    setHistoryDrawer({ task, runs: [], loading: true })
    try {
      const runs = await scheduledTaskApi.history(task.id)
      setHistoryDrawer({ task, runs, loading: false })
    } catch (e) {
      message.error(e instanceof Error ? e.message : '获取执行历史失败')
      setHistoryDrawer({ task, runs: [], loading: false })
    }
  }

  return (
    <div
      style={{
        maxWidth: 1200,
        margin: '0 auto',
        height: '100%',
        minHeight: 0,
        overflowY: 'auto',
        padding: 16,
      }}
    >
      {/* L4-04：迁 /schedule 全屏路由，根容器加 height:100%+overflowY:auto 接通高度链。
          原 SH-05 降级为抽屉 Tab 时移除了页级 h2，全屏路由下保留 maxWidth 居中可读。 */}

      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: 16,
        }}
      >
        <span style={{ color: '#999', fontSize: 13 }}>
          已配置 {tasks.length} 个定时任务，按计划向智能体派发 prompt
        </span>
        <Space>
          <Tooltip title="重新拉取列表">
            <Button icon={<ReloadOutlined />} onClick={fetchAll} disabled={loading} />
          </Tooltip>
          {/* TM-02 创建定时任务：Modal 表单（名称/内容/目标 Agent/频率）。 */}
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={openCreate}
          >
            新建任务
          </Button>
        </Space>
      </div>

      <Spin spinning={loading}>
        {tasks.length === 0 && !loading ? (
          <Empty description="暂无定时任务，点击「新建任务」配置调度" />
        ) : (
          <Space wrap align="start">
            {tasks.map((task) => {
              const meta = SCHEDULE_META[task.schedule_type] ?? {
                color: 'default',
                label: task.schedule_type,
              }
              const acting = actingIds.has(task.id)
              return (
                <Card
                  key={task.id}
                  style={{ width: 360 }}
                  title={
                    <Space>
                      <ClockCircleOutlined />
                      <span style={{ fontWeight: 600 }}>{task.name}</span>
                      <Tag color={meta.color}>{meta.label}</Tag>
                      {task.enabled ? (
                        <Tag color="success">启用中</Tag>
                      ) : (
                        <Tag color="default">已暂停</Tag>
                      )}
                    </Space>
                  }
                  actions={[
                    <Tooltip title="立即执行一次（跳过调度，即使暂停也跑）" key="run">
                      <Button
                        type="text"
                        icon={<ThunderboltOutlined />}
                        loading={acting}
                        onClick={() => handleRunNow(task)}
                      >
                        立即执行
                      </Button>
                    </Tooltip>,
                    <Tooltip
                      title={task.enabled ? '暂停调度' : '恢复调度'}
                      key="toggle"
                    >
                      <Button
                        type="text"
                        icon={
                          task.enabled ? (
                            <PauseCircleOutlined />
                          ) : (
                            <PlayCircleOutlined />
                          )
                        }
                        loading={acting}
                        onClick={() => handleToggle(task)}
                      >
                        {task.enabled ? '暂停' : '恢复'}
                      </Button>
                    </Tooltip>,
                    <Tooltip title="执行历史" key="history">
                      <Button
                        type="text"
                        icon={<HistoryOutlined />}
                        onClick={() => openHistory(task)}
                      >
                        历史
                      </Button>
                    </Tooltip>,
                    <Popconfirm
                      key="delete"
                      title="确认删除该定时任务？"
                      description="删除后将取消调度并从列表移除"
                      onConfirm={() => handleDelete(task)}
                      okText="删除"
                      cancelText="取消"
                      okButtonProps={{ danger: true }}
                    >
                      <Button type="text" danger icon={<DeleteOutlined />}>
                        删除
                      </Button>
                    </Popconfirm>,
                  ]}
                >
                  {/* 调度摘要 */}
                  <div style={{ marginBottom: 8 }}>
                    <span style={{ fontSize: 12, color: '#999', marginRight: 6 }}>
                      调度:
                    </span>
                    <span style={{ fontSize: 13, fontFamily: 'monospace' }}>
                      {scheduleSummary(task)}
                    </span>
                  </div>

                  {/* 目标智能体 + 群组 */}
                  <div style={{ marginBottom: 8, fontSize: 13 }}>
                    <span style={{ color: '#999', marginRight: 6 }}>目标:</span>
                    <Tag style={{ marginInlineEnd: 4 }}>
                      {agentNameMap.get(task.agent_id) ?? (task.agent_id || '未指定')}
                    </Tag>
                    {task.group_id && (
                      <Tag color="blue" style={{ marginInlineEnd: 0 }}>
                        {groupNameMap.get(task.group_id) ?? task.group_id}
                      </Tag>
                    )}
                  </div>

                  {/* 派发内容预览（截断） */}
                  {task.content && (
                    <Tooltip title={task.content} placement="topLeft">
                      <div
                        style={{
                          fontSize: 12,
                          color: '#666',
                          marginBottom: 8,
                          whiteSpace: 'pre-wrap',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          display: '-webkit-box',
                          WebkitLineClamp: 2,
                          WebkitBoxOrient: 'vertical',
                        }}
                      >
                        派发内容：{task.content}
                      </div>
                    </Tooltip>
                  )}

                  {/* 创建时间（列表态可展示的时间信息；下次执行/最近执行见历史抽屉） */}
                  <div style={{ fontSize: 12, color: '#999' }}>
                    创建于 {fmtTime(task.created_at)}
                    {task.updated_at && task.updated_at !== task.created_at && (
                      <> · 更新于 {fmtTime(task.updated_at)}</>
                    )}
                  </div>
                </Card>
              )
            })}
          </Space>
        )}
      </Spin>

      {/* ── TM-07 执行历史抽屉 ──
       * ScheduledTaskRun 时间线（pending→running→success|failed），
       * 立即执行/调度触发后可重开抽屉刷新。 */}
      <Drawer
        title={
          historyDrawer.task ? (
            <Space>
              <HistoryOutlined />
              <span>执行历史 - {historyDrawer.task.name}</span>
            </Space>
          ) : (
            '执行历史'
          )
        }
        open={!!historyDrawer.task}
        onClose={() =>
          setHistoryDrawer({ task: null, runs: [], loading: false })
        }
        width={520}
      >
        <Spin spinning={historyDrawer.loading}>
          {historyDrawer.runs.length === 0 && !historyDrawer.loading ? (
            <Empty description="暂无执行记录（调度触发或立即执行后会记录到这里）" />
          ) : (
            <Timeline
              items={historyDrawer.runs.map((run) => {
                const sm =
                  RUN_STATUS_META[run.status] ?? {
                    color: 'default',
                    label: run.status,
                  }
                return {
                  color: sm.color,
                  children: (
                    <div>
                      <Space size={8}>
                        <Tag color={sm.color}>{sm.label}</Tag>
                        <span style={{ fontSize: 12, color: '#999' }}>
                          {run.id.slice(0, 8)}
                        </span>
                      </Space>
                      <div style={{ fontSize: 12, color: '#999', marginTop: 4 }}>
                        开始 {fmtTime(run.started_at)}
                        {run.finished_at && (
                          <> · 完成 {fmtTime(run.finished_at)}</>
                        )}
                      </div>
                      {run.result && (
                        <div
                          style={{
                            fontSize: 12,
                            color: '#666',
                            marginTop: 4,
                            whiteSpace: 'pre-wrap',
                          }}
                        >
                          {run.result}
                        </div>
                      )}
                    </div>
                  ),
                }
              })}
            />
          )}
        </Spin>
      </Drawer>

      {/* ── TM-02/03 创建定时任务 Modal ──
       * Form.useForm + Modal 自带 footer（onOk=handleCreate）。
       * 字段：name（必填）/ content 派发内容（必填）/ group_id 群组（必填 Select，选定后联动
       * agent_id 候选为该群组成员）/ agent_id 目标智能体（必填 Select）/ schedule_type 调度类型
       * （Segmented cron/interval/once，Form.useWatch 驱动条件字段显隐）/ 对应调度参数
       * （cron→cron 表达式 Input + 预设 / interval→数字+单位换算 interval_seconds
       * / once→DatePicker 时刻 run_at）/ enabled 创建后启用 Switch。
       * TM-03 在 TM-02 的定间隔频率基础上升级为三种调度类型可选，按类型分流提交字段
       * （不相关字段 omit，与 McpPage transport 分流同语义）。 */}
      <Modal
        open={createOpen}
        title="新建定时任务"
        onCancel={() => setCreateOpen(false)}
        confirmLoading={createLoading}
        okText="创建"
        cancelText="取消"
        onOk={handleCreate}
        destroyOnClose
        width={560}
      >
        <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
          <Form.Item
            name="name"
            label="任务名称"
            rules={[{ required: true, message: '请输入任务名称' }]}
          >
            <Input placeholder="如：每日晨报" autoComplete="off" />
          </Form.Item>

          <Form.Item
            name="content"
            label="派发内容"
            rules={[{ required: true, message: '请输入派发内容' }]}
            tooltip="每次调度触发时向目标智能体发送的 prompt"
          >
            <Input.TextArea
              rows={3}
              placeholder="如：生成今日工作晨报并发送"
            />
          </Form.Item>

          <Form.Item
            name="group_id"
            label="群组"
            rules={[{ required: true, message: '请选择群组' }]}
            tooltip="scheduler 据此把任务 push 到正确引擎 inbox"
          >
            <Select
              placeholder="选择群组"
              options={groups.map((g) => ({ value: g.id, label: g.name }))}
            />
          </Form.Item>

          <Form.Item
            name="agent_id"
            label="目标智能体"
            rules={[{ required: true, message: '请选择目标智能体' }]}
            tooltip="该智能体在每次调度触发时接收 prompt"
          >
            <Select
              placeholder="选择智能体"
              showSearch
              optionFilterProp="label"
              options={agents.map((a) => ({
                value: a.id,
                label: `${a.name}（${a.role}）`,
              }))}
            />
          </Form.Item>

          {/* ── TM-03 调度类型选择（cron/interval/once）──
           * Segmented 三选一，Form.useWatch 监听 scheduleType 驱动下方条件字段。
           * block 撑满宽度，图标+文案，顺序与 SCHEDULE_META 一致。 */}
          <Form.Item
            name="schedule_type"
            label="调度类型"
            rules={[{ required: true }]}
            tooltip="定间隔 / cron 表达式 / 一次性定时，对应后端 _build_trigger 三分支"
          >
            <Segmented block options={SCHEDULE_TYPE_OPTIONS} />
          </Form.Item>

          {/* ── interval：数字 + 单位 → interval_seconds ── */}
          {scheduleType === 'interval' && (
            <Form.Item
              label="定间隔频率"
              required
              tooltip="每隔指定时间触发一次，提交时换算成 interval_seconds（秒）"
            >
              <Space.Compact style={{ width: '100%' }}>
                <Form.Item
                  name="freq_value"
                  noStyle
                  rules={[{ required: true, message: '请输入频率数值' }]}
                >
                  <InputNumber
                    min={1}
                    style={{ width: '60%' }}
                    placeholder="1"
                  />
                </Form.Item>
                <Form.Item name="freq_unit" noStyle>
                  <Select
                    style={{ width: '40%' }}
                    options={[
                      { value: 'seconds', label: '秒' },
                      { value: 'minutes', label: '分钟' },
                      { value: 'hours', label: '小时' },
                      { value: 'days', label: '天' },
                    ]}
                  />
                </Form.Item>
              </Space.Compact>
            </Form.Item>
          )}

          {/* ── cron：cron 表达式 Input + 预设快捷 ── */}
          {scheduleType === 'cron' && (
            <Form.Item
              name="cron"
              label="cron 表达式"
              rules={[
                { required: true, message: '请输入 cron 表达式' },
                {
                  validator: (_, value: string) => {
                    if (!value || !value.trim()) return Promise.resolve()
                    const parts = value.trim().split(/\s+/)
                    if (parts.length !== 5) {
                      return Promise.reject(
                        new Error('cron 表达式需为五段式：分 时 日 月 周（如 0 8 * * *）')
                      )
                    }
                    return Promise.resolve()
                  },
                },
              ]}
              tooltip="五段式：分 时 日 月 周（如 0 8 * * * 表示每天 08:00）"
              extra={
                <Space size={4} wrap style={{ marginTop: 4 }}>
                  <span style={{ fontSize: 12, color: '#999' }}>快捷：</span>
                  {CRON_PRESETS.map((p) => (
                    <Tag
                      key={p.value}
                      style={{ cursor: 'pointer' }}
                      onClick={() => form.setFieldValue('cron', p.value)}
                    >
                      {p.label}
                    </Tag>
                  ))}
                </Space>
              }
            >
              <Input
                placeholder="0 8 * * *"
                autoComplete="off"
                style={{ fontFamily: 'monospace' }}
              />
            </Form.Item>
          )}

          {/* ── once：DatePicker 时刻 → run_at(ISO8601) ── */}
          {scheduleType === 'once' && (
            <Form.Item
              name="run_at"
              label="触发时刻"
              rules={[{ required: true, message: '请选择触发时刻' }]}
              tooltip="到指定时刻触发一次后不再重复（后端 DateTrigger）"
              extra={
                <span style={{ fontSize: 12, color: '#999' }}>
                  到点触发一次即结束，提交时转为 ISO8601 传给后端
                </span>
              }
            >
              <DatePicker
                showTime
                style={{ width: '100%' }}
                placeholder="选择触发时刻"
                disabledDate={(d) => d && d.isBefore(dayjs().startOf('day'))}
              />
            </Form.Item>
          )}

          <Form.Item name="enabled" label="创建后启用" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

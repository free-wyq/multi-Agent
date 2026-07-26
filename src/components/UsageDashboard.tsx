/**
 * UsageDashboard：Token 用量仪表盘（PRD 3.6 · 任务15b）。
 *
 * 数据源：``GET /api/usage?start=&end=&model=&group_by=``（usageApi.report）——后端
 * SQLite JSON1 + GROUP BY 聚合 ``messages.data`` 的 tokens/elapsed_ms/reasoning_tokens
 * （任务15a）。本组件纯展示 + 过滤，不自研图表：全部用 antd 组件拼装——
 *
 *  - DatePicker.RangePicker：时间区间（start 含 / end 不含，与后端 created_at 字典序比较对齐）；
 *  - Select：模型过滤（选项来自一次无过滤 group_by=model 拉取的 distinct model 列表）；
 *  - Segmented：聚合维度 model/day/conversation/agent；
 *  - Statistic KPI 行：Tokens / Reasoning / Messages / 耗时 四个头条数；
 *  - Table 明细：每行一组，含 antd Progress 占比列（该组 tokens 占总 tokens 的百分比）；
 *  - Alert：标注 execute 路径未计入口径（设计取舍，见 usageApi 注释）。
 *
 * 数据形态选择（dataviz）：
 *  - 「占比」是单 ratio 对总量 → meter 形态 → antd Progress 单色填充（品牌橙 #F26522，
 *    sequential magnitude 语义，非 categorical）。组身份由 ``key`` 文本列承载（直接标签），
 *    颜色不编码身份——故不分配 categorical 色槽，避免「颜色跟随实体」的反模式。
 *  - 组数可能 >7（多日/多 agent）→ 主形态是 Table（非彩色图），Progress 作为同表内占比列。
 *  - 头条合计 4 个数 → KPI 行（stat tiles），非 grouped bar。
 *
 * 布局：根容器 height:100%+overflowY:auto（与 McpPage/SkillPage 同款，便于塞进 SettingsModal
 * 右侧内容区或独立路由页）。过滤器单行 Space，KPI 行 Col 栅格，明细 Table size="small"。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Col,
  DatePicker,
  Empty,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
  Progress,
} from 'antd'
import type { TableProps } from 'antd'
import {
  ClockCircleOutlined,
  ReloadOutlined,
  MessageOutlined,
  ThunderboltOutlined,
  BulbOutlined,
} from '@ant-design/icons'
import dayjs, { type Dayjs } from 'dayjs'
import { usageApi, type UsageGroupBy, type UsageRow } from '../services/api'

/** 品牌橙：Progress 填充 + KPI 强调色，与全应用 #F26522 一致（Layout/MODELCard/SessionsCard 同款）。 */
const BRAND = '#F26522'

/** 聚合维度 Segmented 选项（图标 + 文案，与 SchedulePage SCHEDULE_TYPE_OPTIONS 同风格）。 */
const GROUP_BY_OPTIONS: { label: React.ReactNode; value: UsageGroupBy }[] = [
  { value: 'model', label: <span>按模型</span> },
  { value: 'day', label: <span>按日期</span> },
  { value: 'conversation', label: <span>按会话</span> },
  { value: 'agent', label: <span>按智能体</span> },
]

/** 维度 → key 列标题（让 Table 列头随维度切换显示语义化名称）。 */
const KEY_LABEL: Record<UsageGroupBy, string> = {
  model: '模型',
  day: '日期',
  conversation: '会话',
  agent: '智能体',
}

/**
 * 毫秒 → 可读时长。聚合值可能很大（多小时），故支持 s/m/h 三档。
 *  - <1s → "Nms"
 *  - <1m → "N.Ns"
 *  - <1h → "Mm Ss"
 *  - ≥1h → "Hh Mm"
 * 与 ChatPanel 流式气泡的 elapsed_ms 格式化同思路，但覆盖更大聚合值。
 */
function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return '0ms'
  if (ms < 1000) return `${Math.round(ms)}ms`
  const totalSec = ms / 1000
  if (totalSec < 60) return `${totalSec.toFixed(1)}s`
  const totalMin = Math.floor(totalSec / 60)
  const remSec = Math.round(totalSec - totalMin * 60)
  if (totalMin < 60) return `${totalMin}m ${remSec}s`
  const totalHr = Math.floor(totalMin / 60)
  const remMin = totalMin - totalHr * 60
  return `${totalHr}h ${remMin}m`
}

/** 整数 → 千分位字符串（Statistic value 用 number，但部分自定义渲染需要字符串）。 */
function withCommas(n: number): string {
  return n.toLocaleString('en-US')
}

/** 区间预设：RangePicker ranges 属性（与 antd 文档同款，dayjs 实例）。 */
const RANGE_PRESETS: Record<string, () => [Dayjs, Dayjs]> = {
  今天: () => [dayjs().startOf('day'), dayjs().endOf('day')],
  '近 7 天': () => [dayjs().subtract(6, 'day').startOf('day'), dayjs().endOf('day')],
  '近 30 天': () => [dayjs().subtract(29, 'day').startOf('day'), dayjs().endOf('day')],
  本月: () => [dayjs().startOf('month'), dayjs().endOf('month')],
}

export default function UsageDashboard() {
  // ── 过滤 state ──
  // 默认近 30 天（[start, end) 半开区间：start=当天 0 点，end=次日 0 点——与后端
  // created_at < end 语义对齐，覆盖选区最后一天全天）。
  const [range, setRange] = useState<[Dayjs, Dayjs] | null>([
    dayjs().subtract(29, 'day').startOf('day'),
    dayjs().add(1, 'day').startOf('day'),
  ])
  const [modelFilter, setModelFilter] = useState<string | undefined>(undefined)
  const [groupBy, setGroupBy] = useState<UsageGroupBy>('model')

  // ── 数据 state ──
  const [report, setReport] = useState<UsageRow[]>([])
  const [totals, setTotals] = useState({
    tokens: 0,
    elapsed_ms: 0,
    reasoning_tokens: 0,
    messages: 0,
  })
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 模型选项：一次性无过滤 group_by=model 拉取 distinct model 列表（独立于区间过滤，
  // 让 Select 选项不被当前区间收缩——用户切区间后仍能选到历史用过的模型）。
  const [modelOptions, setModelOptions] = useState<string[]>([])

  /** 把 dayjs 区间转成后端期望的 ISO-8601 字符串对（start 含 / end 不含）。 */
  const rangeToIso = useCallback((r: [Dayjs, Dayjs] | null): {
    start?: string
    end?: string
  } => {
    if (!r) return {}
    return {
      start: r[0].startOf('day').toISOString(),
      end: r[1].add(1, 'day').startOf('day').toISOString(),
    }
  }, [])

  /** 拉模型选项（无过滤，全局 distinct）。仅 mount 时跑一次。 */
  useEffect(() => {
    let cancelled = false
    usageApi
      .report(undefined, undefined, undefined, 'model')
      .then((rep) => {
        if (cancelled) return
        setModelOptions(rep.rows.map((r) => r.key).filter(Boolean))
      })
      .catch(() => {
        // 选项拉取失败不阻塞主面板——Select 仍可手输或留空。
      })
    return () => {
      cancelled = true
    }
  }, [])

  /** 拉聚合报告（区间 + 模型 + 维度变化时重跑）。 */
  const fetchReport = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const { start, end } = rangeToIso(range)
      const rep = await usageApi.report(start, end, modelFilter, groupBy)
      setReport(rep.rows)
      setTotals(rep.totals)
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setError(msg)
      message.error(`用量数据加载失败：${msg}`)
    } finally {
      setLoading(false)
    }
  }, [range, modelFilter, groupBy, rangeToIso])

  useEffect(() => {
    void fetchReport()
  }, [fetchReport])

  /** 占比：某组 tokens 占总 tokens 的百分比（0-100）。总数为 0 时返 0（避免 NaN）。 */
  const sharePct = useCallback(
    (row: UsageRow): number => {
      if (totals.tokens <= 0) return 0
      return (row.tokens / totals.tokens) * 100
    },
    [totals.tokens],
  )

  /** Table 列定义（随 groupBy 切 key 列标题）。 */
  const columns: TableProps<UsageRow>['columns'] = useMemo(
    () => [
      {
        title: KEY_LABEL[groupBy],
        dataIndex: 'key',
        width: 200,
        ellipsis: true,
        render: (key: string) => (
          <Tooltip title={key || '（未标注）'}>
            <span style={{ fontFamily: 'monospace', fontSize: 12 }}>
              {key || <span style={{ color: '#ccc' }}>（未标注）</span>}
            </span>
          </Tooltip>
        ),
      },
      {
        title: 'Tokens',
        dataIndex: 'tokens',
        width: 110,
        align: 'right' as const,
        sorter: (a, b) => a.tokens - b.tokens,
        defaultSortOrder: 'descend' as const,
        render: (v: number) => withCommas(v),
      },
      {
        title: '占比',
        key: 'share',
        width: 180,
        // 单色 Progress：sequential magnitude 语义，颜色不编码身份（身份由 key 列承载）。
        // 品牌橙填充 + 浅灰 track，showInfo 显百分比，size="small" 控高度。
        render: (_v, row) => (
          <Progress
            percent={Math.round(sharePct(row) * 10) / 10}
            size="small"
            strokeColor={BRAND}
            showInfo
          />
        ),
      },
      {
        title: '推理 Tokens',
        dataIndex: 'reasoning_tokens',
        width: 110,
        align: 'right' as const,
        sorter: (a, b) => a.reasoning_tokens - b.reasoning_tokens,
        render: (v: number) => withCommas(v),
      },
      {
        title: '耗时',
        dataIndex: 'elapsed_ms',
        width: 100,
        align: 'right' as const,
        sorter: (a, b) => a.elapsed_ms - b.elapsed_ms,
        render: (v: number) => formatDuration(v),
      },
      {
        title: '消息数',
        dataIndex: 'messages',
        width: 90,
        align: 'right' as const,
        sorter: (a, b) => a.messages - b.messages,
        render: (v: number) => withCommas(v),
      },
    ],
    [groupBy, sharePct],
  )

  const hasData = report.length > 0

  return (
    <div
      style={{
        maxWidth: 1100,
        margin: '0 auto',
        height: '100%',
        minHeight: 0,
        overflowY: 'auto',
        padding: 16,
      }}
    >
      {/* 标题 + 刷新 */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: 16,
        }}
      >
        <div>
          <span style={{ fontSize: 16, fontWeight: 600 }}>Token 用量</span>
          <span style={{ fontSize: 13, color: '#999', marginLeft: 8 }}>
            聚合 chat/ask 路径智能体回复的 token 与耗时统计
          </span>
        </div>
        <Tooltip title="重新拉取">
          <ReloadOutlined
            onClick={() => void fetchReport()}
            style={{ cursor: 'pointer', color: '#999' }}
          />
        </Tooltip>
      </div>

      {/* 过滤器单行：区间 + 模型 + 维度 */}
      <Space wrap size={[12, 12]} style={{ marginBottom: 16 }}>
        <DatePicker.RangePicker
          value={range}
          onChange={(r) => setRange(r as [Dayjs, Dayjs] | null)}
          ranges={RANGE_PRESETS}
          allowClear
          placeholder={['开始日期', '结束日期']}
        />
        <Select
          allowClear
          placeholder="全部模型"
          style={{ width: 200 }}
          value={modelFilter}
          onChange={(v) => setModelFilter(v ?? undefined)}
          options={modelOptions.map((m) => ({ label: m, value: m }))}
          showSearch
        />
        <Segmented
          options={GROUP_BY_OPTIONS}
          value={groupBy}
          onChange={(v) => setGroupBy(v as UsageGroupBy)}
        />
      </Space>

      {/* execute 路径未计入提示（设计取舍口径标注，任务15c 要求） */}
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="统计口径：仅含对话回复的 token 用量"
        description="execute 任务路径的模板公告（如「任务完成」）与工具调用回合未携带流式统计，不计入本仪表盘。协调者 node_chat 与 worker 脑回路回复已统计。"
      />

      {/* 错误态 */}
      {error && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message="数据加载失败"
          description={error}
        />
      )}

      <Spin spinning={loading}>
        {/* KPI 行：四个头条数（Tokens / 推理 / 消息 / 耗时） */}
        <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
          <Col xs={12} sm={6}>
            <Statistic
              title="Tokens 总量"
              value={totals.tokens}
              valueStyle={{ color: BRAND, fontWeight: 600 }}
              prefix={<ThunderboltOutlined />}
            />
          </Col>
          <Col xs={12} sm={6}>
            <Statistic
              title="推理 Tokens"
              value={totals.reasoning_tokens}
              prefix={<BulbOutlined />}
            />
          </Col>
          <Col xs={12} sm={6}>
            <Statistic
              title="回复数"
              value={totals.messages}
              prefix={<MessageOutlined />}
            />
          </Col>
          <Col xs={12} sm={6}>
            <Statistic
              title="总耗时"
              value={formatDuration(totals.elapsed_ms)}
              prefix={<ClockCircleOutlined />}
            />
          </Col>
        </Row>

        {/* 明细 Table（含 Progress 占比列） */}
        {hasData ? (
          <Table<UsageRow>
            size="small"
            rowKey="key"
            columns={columns}
            dataSource={report}
            pagination={{ pageSize: 10, showSizeChanger: false, size: 'small' }}
            scroll={{ x: 760 }}
          />
        ) : (
          <Empty
            description={
              !loading && !error
                ? '当前过滤条件下无用量数据'
                : '加载中…'
            }
            style={{ margin: '32px 0' }}
          />
        )}

        {/* 当前过滤态摘要（让用户一眼看到生效中的条件） */}
        {(range || modelFilter) && (
          <div style={{ marginTop: 12, fontSize: 12, color: '#999' }}>
            <Tag color="default" style={{ marginRight: 8 }}>
              维度：{KEY_LABEL[groupBy]}
            </Tag>
            {range && (
              <Tag color="default" style={{ marginRight: 8 }}>
                区间：{range[0].format('YYYY-MM-DD')} ~ {range[1].format('YYYY-MM-DD')}
              </Tag>
            )}
            {modelFilter && (
              <Tag color="default" style={{ marginRight: 8 }}>
                模型：{modelFilter}
              </Tag>
            )}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              共 {report.length} 组
            </Typography.Text>
          </div>
        )}
      </Spin>
    </div>
  )
}

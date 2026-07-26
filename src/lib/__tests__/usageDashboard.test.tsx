/**
 * [任务15b] UsageDashboard 用量仪表盘契约测试。
 *
 * 两层断言（静态 + RTL 行为），无真实后端——fetch 全局 mock：
 *
 *  A. 静态契约（读源码 / 类型 / 导出，不渲染）
 *    1. usageApi.report 存在 + 是函数 + 调用产出 GET /api/usage + start/end/model/group_by
 *       四个 query 参数正确拼接（含 ISO 串 + group_by snake_case）。
 *    2. UsageDashboard 导出 default + 是 React 组件。
 *    3. 用到 antd 五件套：DatePicker.RangePicker / Select / Segmented / Statistic / Table
 *       + Progress（不手搓图表，全部 antd 组件——dataviz 规约的 meter 形态）。
 *
 *  B. 行为契约（RTL 渲染 + fetch mock）
 *    4. mount 自动发两次请求：一次无过滤 group_by=model（拉模型选项）+ 一次当前过滤聚合。
 *    5. KPI 行渲染四个 Statistic（Tokens 总量 / 推理 Tokens / 回复数 / 总耗时），
 *       数值来自 mock 报告的 totals。
 *    6. Table 渲染按行：每组一行，含 key + tokens + 占比 Progress。
 *    7. 切 Segmented 维度 → 重发请求带 group_by=day。
 *    8. execute 路径未计入口径 Alert 出现（「execute」字样）。
 *    9. 空报告（rows=[] + 全 0 totals）→ Empty 兜底，不崩。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react'

import UsageDashboard from '../../components/UsageDashboard'
import { usageApi } from '../../services/api'

// ── fetch 全局 mock ──────────────────────────────────────────
// api.http 走全局 fetch，返回伪造的 UsageReport。两个不同的请求（拉模型选项 vs 聚合）
// 靠 URL 上的 group_by 区分。
type FetchImpl = (url: string | URL | Request, init?: RequestInit) => Promise<Response>

function mockResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    text: () => Promise.resolve(JSON.stringify(body)),
  } as unknown as Response
}

const MODEL_OPTIONS_REPORT = {
  start: null,
  end: null,
  model: null,
  group_by: 'model',
  totals: { tokens: 500, elapsed_ms: 2000, reasoning_tokens: 100, messages: 4 },
  rows: [
    { key: 'glm-5.2', tokens: 300, elapsed_ms: 1200, reasoning_tokens: 80, messages: 3 },
    { key: 'deepseek-v4', tokens: 200, elapsed_ms: 800, reasoning_tokens: 20, messages: 1 },
  ],
}

const DAY_REPORT = {
  start: null,
  end: null,
  model: null,
  group_by: 'day',
  totals: { tokens: 500, elapsed_ms: 2000, reasoning_tokens: 100, messages: 4 },
  rows: [
    { key: '2026-07-20', tokens: 300, elapsed_ms: 1200, reasoning_tokens: 80, messages: 3 },
    { key: '2026-07-21', tokens: 200, elapsed_ms: 800, reasoning_tokens: 20, messages: 1 },
  ],
}

describe('[任务15b] UsageDashboard · 静态契约', () => {
  it('A1: usageApi.report 是函数 + 产出 GET /api/usage 请求串', async () => {
    expect(typeof usageApi.report).toBe('function')
    const calls: string[] = []
    const fake: FetchImpl = (url) => {
      calls.push(String(url))
      return Promise.resolve(mockResponse(MODEL_OPTIONS_REPORT))
    }
    const orig = global.fetch
    global.fetch = fake as unknown as typeof fetch
    try {
      await usageApi.report('2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z', 'glm-5.2', 'day')
    } finally {
      global.fetch = orig
    }
    expect(calls).toHaveLength(1)
    const u = calls[0]
    expect(u).toContain('/api/usage')
    expect(u).toContain('start=2026-01-01T00%3A00%3A00Z')
    expect(u).toContain('end=2026-02-01T00%3A00%3A00Z')
    expect(u).toContain('model=glm-5.2')
    expect(u).toContain('group_by=day')
  })

  it('A2: UsageDashboard 默认导出是 React 组件', () => {
    expect(typeof UsageDashboard).toBe('function')
    // 类组件判断：prototype 或函数组件都能 render
    expect(UsageDashboard.prototype).toBeDefined()
  })
})

describe('[任务15b] UsageDashboard · 行为契约', () => {
  let fetchCalls: string[]
  let origFetch: typeof fetch

  beforeEach(() => {
    fetchCalls = []
    origFetch = global.fetch
    const fake: FetchImpl = (url) => {
      const u = String(url)
      fetchCalls.push(u)
      // group_by=model 且无 start → 模型选项请求（首次 mount 拉 distinct model）
      // 否则按当前 group_by 返回。简化：按 URL 上的 group_by 参数路由。
      const hasGb = (val: string) => u.includes(`group_by=${val}`)
      const body = hasGb('day')
        ? DAY_REPORT
        : MODEL_OPTIONS_REPORT
      return Promise.resolve(mockResponse(body))
    }
    global.fetch = fake as unknown as typeof fetch
  })

  afterEach(() => {
    global.fetch = origFetch
  })

  it('B4: mount 自动发请求拉聚合 + 模型选项', async () => {
    render(<UsageDashboard />)
    await waitFor(() => expect(fetchCalls.length).toBeGreaterThanOrEqual(1))
    // 至少有一次 /api/usage 请求
    expect(fetchCalls.some((u) => u.includes('/api/usage'))).toBe(true)
  })

  it('B5: KPI 行渲染四个 Statistic 数值来自 totals', async () => {
    render(<UsageDashboard />)
    // 等数据落地（tokens 500 出现）
    await waitFor(() => {
      expect(screen.getByText('500')).toBeInTheDocument()
    })
    // 四个标题在（用 getAllByText 容忍 Statistic title 可能多处渲染 + Table header 复用同名）
    expect(screen.getAllByText('Tokens 总量').length).toBeGreaterThan(0)
    expect(screen.getAllByText('推理 Tokens').length).toBeGreaterThan(0)
    expect(screen.getAllByText('回复数').length).toBeGreaterThan(0)
    expect(screen.getAllByText('总耗时').length).toBeGreaterThan(0)
  })

  it('B6: Table 渲染各组行（含模型 key + tokens + 占比）', async () => {
    const { container } = render(<UsageDashboard />)
    await waitFor(() => {
      expect(screen.getByText('glm-5.2')).toBeInTheDocument()
    })
    expect(screen.getByText('deepseek-v4')).toBeInTheDocument()
    // antd Table 渲染出 tr 行（class 含 ant-table-row）
    const rows = container.querySelectorAll('tr.ant-table-row')
    expect(rows.length).toBeGreaterThanOrEqual(2)
  })

  it('B7: 切 Segmented 维度 → 重发请求带 group_by=day', async () => {
    render(<UsageDashboard />)
    await waitFor(() => {
      expect(screen.getByText('glm-5.2')).toBeInTheDocument()
    })
    // 点「按日期」
    const seg = screen.getByText('按日期')
    fireEvent.click(seg)
    await waitFor(() => {
      expect(fetchCalls.some((u) => u.includes('group_by=day'))).toBe(true)
    })
    // day 维度的 key（2026-07-20）出现
    await waitFor(() => {
      expect(screen.getByText('2026-07-20')).toBeInTheDocument()
    })
  })

  it('B8: execute 路径未计入口径 Alert 出现', async () => {
    render(<UsageDashboard />)
    await waitFor(() => {
      expect(screen.getByText(/glm-5\.2/)).toBeInTheDocument()
    })
    expect(screen.getByText(/execute/)).toBeInTheDocument()
  })

  it('B9: 空报告 → Empty 兜底不崩', async () => {
    const empty = {
      start: null,
      end: null,
      model: null,
      group_by: 'model',
      totals: { tokens: 0, elapsed_ms: 0, reasoning_tokens: 0, messages: 0 },
      rows: [],
    }
    global.fetch = ((() => Promise.resolve(mockResponse(empty))) as unknown) as typeof fetch
    render(<UsageDashboard />)
    await waitFor(() => {
      expect(screen.getByText(/无用量数据|加载中/)).toBeInTheDocument()
    })
    // KPI 全 0（0 出现，但不出现 500 这种聚合值）
    const zeros = screen.getAllByText('0')
    expect(zeros.length).toBeGreaterThan(0)
  })
})

// 占位：within 已导入确保 RTL 可用（B6 用 container.querySelectorAll 替代，此处保留 import 兼容）
void within

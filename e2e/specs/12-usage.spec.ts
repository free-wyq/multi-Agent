/**
 * 任务20c 流程12 — Token 用量仪表盘（UsageDashboard 渲染 + 数据回流）。
 *
 * 覆盖：
 *  - 设置弹窗 → 「用户信息」导航项 → UsageDashboard 渲染（标题「Token 用量」+
 *    execute 口径 Alert + KPI Statistic 行 + 明细 Table）。
 *  - 产生用量：先去单聊发一条消息让 worker brain 回复（mock LLM 秒回，
 *    persist_agent_reply 落 data={tokens,model:...,elapsed_ms,...}）。
 *  - 回设置 → 刷新仪表盘 → KPI「Tokens 总量」>0 + Table 显 e2e-mock-model 行。
 *
 * 后端 /api/usage 聚合 messages.data 的 tokens/model/elapsed_ms（任务15a）。
 * execute 路径（任务完成 announce）data=None 不计入——这是设计口径（任务15c
 * Alert 标注），故本测只发普通 chat 消息触发 worker brain 回复（计入口径）。
 *
 * 数据回流契约：单聊发消息 → brain chat_completion_stream → mock SSE →
 * ContentExtractor → emit_task_token → persist_agent_reply(data={tokens,...})
 * → /api/usage 聚合命中。本测锁「发消息产生用量 → 仪表盘显数据」端到端链路。
 *
 * 隔离：用量数据落 e2e 临时 DB 的 messages 表，不主动清理（messages 关联会话，
 * globalSetup cleanupStaleTestEntities 会清非 seed 会话及其消息）。
 */
import { expect, test } from '@playwright/test'
import { sendAndWaitReply } from './helpers'

// 打开设置弹窗并切到「用户信息」导航项（UsageDashboard 所在）。
async function openUsageDashboard(page: import('@playwright/test').Page): Promise<void> {
  await page.locator('.ant-layout-sider').getByText('用户信息', { exact: true }).click()
  await expect(page.locator('.ant-modal-title', { hasText: '设置' })).toBeVisible()
  // 「用户信息」是 initialKey='user' 默认项，但点「用户信息」条本身已切到 user 项。
  // 兜底再点一次导航项确保 activeKey='user'。
  await page.locator('.ant-modal').getByText('用户信息', { exact: true }).click()
  // UsageDashboard 渲染——标题「Token 用量」（在 UsageDashboard 顶部 span，
  // font-weight:600，与 Alert 的 ant-alert-title 文本不同）用 exact 匹配避免 strict。
  await expect(
    page.locator('.ant-modal').getByText('Token 用量', { exact: true }),
  ).toBeVisible({ timeout: 10_000 })
}

test.describe('流程12 — Token 用量仪表盘', () => {
  test('仪表盘渲染 + execute 口径 Alert 显', async ({ page }) => {
    await page.goto('/')
    await openUsageDashboard(page)

    // 标题 + 副标题。
    await expect(
      page.locator('.ant-modal').getByText('Token 用量', { exact: true }),
    ).toBeVisible()
    await expect(
      page.locator('.ant-modal').getByText(/聚合 chat\/ask 路径/),
    ).toBeVisible()

    // execute 口径 Alert（设计取舍标注，任务15c 要求）。
    await expect(
      page.locator('.ant-modal').getByText(/统计口径：仅含对话回复的 token 用量/),
    ).toBeVisible()

    // KPI Statistic 行四个标题在——Statistic 的 title 在 .ant-statistic-title 内。
    // Table 列标题也含同名（measure-row 镜像 + thead），用 .ant-statistic-title 精确定位。
    await expect(
      page.locator('.ant-modal .ant-statistic-title', { hasText: 'Tokens 总量' }).first(),
    ).toBeVisible()
    await expect(
      page.locator('.ant-modal .ant-statistic-title', { hasText: '推理 Tokens' }).first(),
    ).toBeVisible()
    await expect(
      page.locator('.ant-modal .ant-statistic-title', { hasText: '回复数' }).first(),
    ).toBeVisible()
    await expect(
      page.locator('.ant-modal .ant-statistic-title', { hasText: '总耗时' }).first(),
    ).toBeVisible()
  })

  test('发消息产生用量 → 刷新仪表盘 → KPI tokens>0 + Table 显 mock model', async ({
    page,
  }) => {
    await page.goto('/')
    // 先开单聊发消息产生用量（worker brain 回复落 data={tokens,model,elapsed_ms}）。
    await page.locator('.ant-layout-sider').getByText('前端工程师', { exact: true }).click()
    await expect(page.getByPlaceholder(/输入消息/)).toBeVisible({ timeout: 5000 })
    await sendAndWaitReply(page, 'e2e 用量探针消息', '[e2e-mock]', 30_000)

    // 切到设置弹窗的用户信息项看仪表盘。
    await openUsageDashboard(page)

    // 刷新按钮（ReloadOutlined icon-only，用 tooltip title 定位）。
    await page
      .locator('.ant-modal')
      .locator('span[aria-label="reload"]')
      .click()
      .catch(async () => {
        // ReloadOutlined 的 aria-label 可能是 "reload" 或图标名，兜底用 tooltip。
        await page.locator('.ant-modal').getByRole('tooltip').click().catch(async () => {
          // 退化：直接点 UsageDashboard 顶部的 ReloadOutlined（用 anticon class）。
          await page.locator('.ant-modal .anticon-reload').click()
        })
      })

    // KPI「Tokens 总量」数值 > 0——Statistic value 在 .ant-statistic-content-value 内。
    // 给后端聚合 + 前端刷新一点时间（mock 秒回但 persist + aggregate 有开销）。
    await expect(
      async () => {
        const val = await page
          .locator('.ant-modal')
          .locator('.ant-statistic')
          .filter({ hasText: 'Tokens 总量' })
          .locator('.ant-statistic-content-value')
          .first()
          .innerText()
        // 去掉千分位逗号转数字。
        const num = parseInt(val.replace(/[,]/g, '').trim(), 10)
        expect(num).toBeGreaterThan(0)
      },
    ).toPass({ timeout: 30_000 })

    // 明细 Table 显 mock model 行（group_by=model 默认，key 列是 model 名）。
    // e2e-mock-model 是 globalSetup 写的 active provider model。
    await expect(
      page.locator('.ant-modal .ant-table').getByText('e2e-mock-model').first(),
    ).toBeVisible({ timeout: 15_000 })
  })
})

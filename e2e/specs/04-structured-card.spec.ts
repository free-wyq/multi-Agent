/**
 * 任务20b 流程4 — 结构化卡片渲染。
 *
 * 覆盖：mock LLM 发「[卡片]」关键词 → 返回带 ```card 围栏的 table payload →
 * 前端 ChatMessageBubble 的 splitContentByCards 解析 → StructuredCard 渲染 antd Table。
 *
 * 卡片 JSON（playwright_mock_llm_server._card_table_payload）：
 *   {icon:"🔥", title:"e2e 演示榜单", kind:"table",
 *    columns:["排名","标题","热度"],
 *    rows:[["1","e2e 第一条","100"],["2","e2e 第二条","80"]]}
 *
 * 断言「页面里出现 antd Table 且含表头/数据」即证 card 围栏解析 + StructuredCard
 * table 分支渲染全通。
 */
import { expect, test } from '@playwright/test'
import { sendAndWaitReply } from './helpers'

test.describe('流程4 — 结构化卡片', () => {
  test('发卡片关键词 → mock 回 ```card 围栏 → 前端渲染 antd Table', async ({ page }) => {
    await page.goto('/')
    // 开 seed agent「前端工程师」单聊。
    await page.locator('.ant-layout-sider').getByText('前端工程师', { exact: true }).click()
    await expect(page.getByText('前端工程师', { exact: true }).first()).toBeVisible()
    await expect(page.getByPlaceholder(/输入消息/)).toBeVisible({ timeout: 5000 })

    // 发「[卡片]」关键词触发 mock _card_table_payload。
    await sendAndWaitReply(page, '请生成[卡片]榜单', '[e2e-mock]', 30_000)

    // StructuredCard 渲染：.chat-card-block 容器 + 内嵌 antd Table。
    await expect(page.locator('.chat-card-block').first()).toBeVisible({
      timeout: 10_000,
    })
    // 卡片标题「e2e 演示榜单」——在 .chat-card-title span 内（与 kind-icon + emoji 并列）。
    await expect(page.locator('.chat-card-title').first()).toContainText('e2e 演示榜单')
    // 卡片图标 emoji 🔥（在 .chat-card-icon span 内）。
    await expect(page.locator('.chat-card-icon').first()).toHaveText('🔥')
    // Table 表头列「排名」「热度」——antd Table 渲染了 measure-row（隐藏的镜像列）+
    // 真实 thead，故同名 columnheader 有多个，用 .first() 取其一。
    await expect(
      page.locator('.chat-card-block').getByRole('columnheader', { name: '排名' }).first(),
    ).toBeVisible()
    await expect(
      page.locator('.chat-card-block').getByRole('columnheader', { name: '热度' }).first(),
    ).toBeVisible()
    // Table 数据行——「e2e 第一条」「e2e 第二条」（cell 文本，measure-row 镜像致多命中，用 first）。
    await expect(
      page.locator('.chat-card-block').getByText('e2e 第一条').first(),
    ).toBeVisible()
    await expect(
      page.locator('.chat-card-block').getByText('e2e 第二条').first(),
    ).toBeVisible()
  })
})

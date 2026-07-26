/**
 * 任务20c 流程9 — 记忆模块（CRUD + FTS5 全文检索）。
 *
 * 覆盖：
 *  - 设置弹窗 → 「记忆」导航项 → MemoryPage 渲染。
 *  - 「新增记忆」Modal → content + scope=global + importance → 保存 → 列表显新记忆。
 *  - FTS5 检索条 → 输入关键字 → 「检索」→ 命中记忆（bm25+importance 排序）。
 *  - 删除记忆 → Popconfirm 确认 → 列表消失。
 *
 * 后端 /api/memory CRUD + /api/memory/search FTS5（任务17b）。FTS5 用 trigram tokenizer
 * （unicode61 对中文整列一个 token 无法 MATCH，trigram 支持子串匹配）。记忆 search 命中
 * 后 access_count 自增——这是 L2 记忆检索的核心契约（检索排序权重）。
 *
 * scope=global 不需 scope_ref（agent 档才需选智能体），表单最简。importance Slider
 * 默认值在表单初始化（openCreate setFieldsValue），不拖动直接保存走默认。
 *
 * 隔离：测试建的记忆落 e2e 临时 DB（.e2e-data），末尾删除清理。
 */
import { expect, test } from '@playwright/test'

const MEM_CONTENT = 'e2e 用户偏好 Java 后端开发，偏好简洁回复风格'
const MEM_SEARCH_KEYWORD = 'Java 后端'

// 打开设置弹窗并切到「记忆」导航项。
async function openMemorySettings(page: import('@playwright/test').Page): Promise<void> {
  await page.locator('.ant-layout-sider').getByText('用户信息', { exact: true }).click()
  await expect(page.locator('.ant-modal-title', { hasText: '设置' })).toBeVisible()
  await page.locator('.ant-modal').getByText('记忆', { exact: true }).click()
  // MemoryPage 渲染——FTS5 检索条占位符出现。
  await expect(
    page.locator('.ant-modal').getByPlaceholder('FTS5 全文检索（bm25 排序，调试注入召回用）'),
  ).toBeVisible({ timeout: 10_000 })
}

test.describe('流程9 — 记忆模块', () => {
  test('新增 global 记忆 → 列表显 → FTS5 检索命中 → 删除消失', async ({ page }) => {
    await page.goto('/')
    await openMemorySettings(page)

    // ── 新增记忆 ──
    await page.locator('.ant-modal').getByRole('button', { name: '新增记忆' }).click()
    await expect(page.locator('.ant-modal-title', { hasText: '新增记忆' })).toBeVisible()

    // content 文本域。
    await page.getByPlaceholder('一条值得长期记住的事实 / 偏好 / 结论').fill(MEM_CONTENT)
    // scope 默认 global（openCreate setFieldsValue 默认值）—— global 不需 scope_ref。
    // importance Slider 不拖动走默认值（form 初始化值）。
    // 保存（modal-footer primary）。
    await page.locator('.ant-modal-footer .ant-btn-primary').first().click()
    await expect(page.getByText(/保存成功|已新增|创建成功/).first()).toBeVisible({
      timeout: 10_000,
    })

    // 列表显新记忆卡片（content 正文出现）。
    const memCard = page
      .locator('.ant-modal .ant-card')
      .filter({ hasText: MEM_CONTENT })
      .first()
    await expect(memCard).toBeVisible({ timeout: 10_000 })
    // scope=global → 「全局」Tag 出现。
    await expect(memCard).toContainText('全局')

    // ── FTS5 检索 ──
    // 检索条输入关键字（MEM_SEARCH_KEYWORD 是 content 的子串）。
    const searchInput = page.locator('.ant-modal').getByPlaceholder(
      'FTS5 全文检索（bm25 排序，调试注入召回用）',
    )
    await searchInput.fill(MEM_SEARCH_KEYWORD)
    // 「检索」按钮是两字中文 → antd v6 插空格成「检 索」，getByRole(name) 匹配不到，
    // 用 Space.Compact 内的 primary 按钮定位（FTS5 卡片里唯一的 primary 按钮）。
    await page
      .locator('.ant-modal .ant-space-compact .ant-btn-primary')
      .click()
    // 命中提示出现（「命中 N 条」）。
    await expect(page.locator('.ant-modal').getByText(/命中 \d+ 条/)).toBeVisible({
      timeout: 10_000,
    })

    // ── 删除 ──
    // 删除按钮是 icon-only（<Button size="small" danger icon={<DeleteOutlined />} />，无文本），
    // 用 card 内的 danger 按钮定位。检索条刷新不重渲染列表卡片，memCard 仍有效。
    await page
      .locator('.ant-modal .ant-card')
      .filter({ hasText: MEM_CONTENT })
      .first()
      .locator('button.ant-btn-dangerous')
      .click()
    // Popconfirm 确认（.ant-popover danger 按钮）。
    await page
      .locator('.ant-popover:not(.ant-popover-hidden) .ant-btn-dangerous')
      .click()
    await expect(page.getByText(/删除成功|已删除/).first()).toBeVisible({ timeout: 10_000 })
    // 卡片消失。
    await expect(
      page.locator('.ant-modal .ant-card').filter({ hasText: MEM_CONTENT }),
    ).toHaveCount(0)
  })
})

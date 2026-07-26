/**
 * 任务20b 流程7 — slash 命令（自动补全 + 执行 + 系统卡片渲染）。
 *
 * 覆盖：
 *  - 输入框打 `/` → SlashAutocomplete 下拉出现（header「斜杠命令」+ 9 命令列表）。
 *  - 上下键导航 + Enter 选中 → 输入框填 `/name `。
 *  - 执行命令（/model 无参）→ handler 调 configApi.get → renderCard(ModelCard) →
 *    聊天流出现 slash_card 系统卡片。
 *  - /status 纯本地聚合 → StatusCard 渲染。
 *
 * 不调 LLM（/model /status 走 configApi / 本地 busState），故本测不依赖 mock 时序，
 * 最稳。
 */
import { expect, test } from '@playwright/test'

test.describe('流程7 — slash 命令', () => {
  test('输入 / 弹补全下拉，含 9 命令', async ({ page }) => {
    await page.goto('/')
    await page.locator('.ant-layout-sider').getByText('前端工程师', { exact: true }).click()
    await expect(page.getByPlaceholder(/输入消息/)).toBeVisible({ timeout: 5000 })

    const input = page.getByPlaceholder(/输入消息/)
    await input.click()
    await input.fill('/')
    await page.waitForTimeout(500)

    // 下拉出现——header「斜杠命令」+ 命令项（[data-slash-idx]）。
    await expect(page.getByText('斜杠命令')).toBeVisible({ timeout: 5000 })
    // 9 个命令项（data-slash-idx 0~8）。
    await expect(page.locator('[data-slash-idx]')).toHaveCount(9)
  })

  test('键盘导航选 /model → 执行 → ModelCard 系统卡片出现', async ({ page }) => {
    await page.goto('/')
    await page.locator('.ant-layout-sider').getByText('前端工程师', { exact: true }).click()
    await expect(page.getByPlaceholder(/输入消息/)).toBeVisible({ timeout: 5000 })

    const input = page.getByPlaceholder(/输入消息/)
    await input.click()
    await input.fill('/')
    await expect(page.getByText('斜杠命令')).toBeVisible({ timeout: 5000 })

    // 键盘导航：ArrowDown 选第二个命令（/model，索引 1）→ Enter 选中填入输入框。
    await input.press('ArrowDown') // 索引 1（/model，第二项，索引0是/new）
    await input.press('Enter')
    // 输入框应变成 `/model `（含尾空格）。
    await expect(input).toHaveValue(/\/model\s*/, { timeout: 5000 })

    // 再 Enter 执行命令（输入框是 `/model ` 整行 → parseSlashCommand 命中 → handleModel）。
    await input.press('Enter')

    // /model 无参 → configApi.get → renderCard(ModelCard) → 聊天流出现系统卡片。
    // ModelCard 含「模型」标签 + 当前 model（mock 的 e2e-mock-model）。
    await expect(page.getByText('e2e-mock-model').first()).toBeVisible({ timeout: 10_000 })
  })

  test('鼠标点选 /status → 执行 → StatusCard 系统卡片出现', async ({ page }) => {
    await page.goto('/')
    await page.locator('.ant-layout-sider').getByText('前端工程师', { exact: true }).click()
    await expect(page.getByPlaceholder(/输入消息/)).toBeVisible({ timeout: 5000 })

    const input = page.getByPlaceholder(/输入消息/)
    await input.click()
    await input.fill('/')
    await expect(page.getByText('斜杠命令')).toBeVisible({ timeout: 5000 })

    // 鼠标点选 /status 命令项（含 /status 文本的 data-slash-idx 项）。
    await page
      .locator('[data-slash-idx]')
      .filter({ hasText: '/status' })
      .click()
    // 输入框填 `/status `。
    await expect(input).toHaveValue(/\/status\s*/, { timeout: 5000 })
    // Enter 执行。
    await input.press('Enter')

    // /status → handleStatus → renderCard(StatusCard)。
    // StatusCard 标题含「运行状态」Tag + 「N 个智能体」。
    // 不依赖 model 文本（StatusCard 渲染 agent 状态聚合，未必显 model 名）。
    await expect(page.getByText('运行状态').first()).toBeVisible({ timeout: 10_000 })
    // 智能体计数（agents.length，单聊 worker 引擎初始化后至少 1 个 agent 状态）。
    await expect(page.getByText(/个智能体/).first()).toBeVisible({ timeout: 10_000 })
  })
})

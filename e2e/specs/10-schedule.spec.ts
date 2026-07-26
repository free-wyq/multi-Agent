/**
 * 任务20c 流程10 — 定时任务（CRUD + 立即执行 + 执行历史）。
 *
 * 覆盖：
 *  - 设置弹窗 → 「定时任务」导航项 → SchedulePage 渲染。
 *  - 「新建任务」Modal → name + content + group_id(选 seed 演示协作组) +
 *    agent_id(选 seed 协调者) + schedule_type=interval(默认) → 创建 → 卡片显。
 *  - 卡片「立即执行」→ 触发 _fire（force=True 跳过 enabled 检查）→ scheduledTaskRun
 *    落库 pending→running→success（mock LLM 秒回，run 快速成功）。
 *  - 卡片「历史」→ 抽屉显 Timeline + success Tag。
 *  - 删除任务 → Popconfirm → 卡片消失。
 *
 * 后端 /api/scheduled-tasks CRUD + /run + /pause /resume + /runs（任务18a/b/c）。
 * scheduler fire 时 push_task 到 agent 的 inbox，agent 是群图里的节点——任务18b e2e
 * 已锁「真源=_task_queues[group_id]」（不建 engine 不调 LLM）。本测锁 UI 链路 + 立即执行
 * 触发后 history 抽屉显 success（run 走真 engine，mock LLM 让 brain 秒回 chat 探针串，
 * run status 转 success）。
 *
 * 18b 契约测试的 AsyncIOScheduler 跨 asyncio.run RuntimeError 坑（每段末 shutdown_scheduler）
 * 不影响本测——本测经 HTTP 调后端，后端单一 event loop 跑 scheduler，无跨 loop 问题。
 *
 * 隔离：测试建的任务落 e2e 临时 DB，末尾删除清理（删任务后端 remove_job 取消调度）。
 */
import { expect, test } from '@playwright/test'
import { SEED_GROUP_NAME } from './helpers'

const TASK_NAME = 'e2e定时探针任务'
const TASK_CONTENT = 'e2e 定时任务派发内容探针'

// 打开设置弹窗并切到「定时任务」导航项。
async function openScheduleSettings(page: import('@playwright/test').Page): Promise<void> {
  await page.locator('.ant-layout-sider').getByText('用户信息', { exact: true }).click()
  await expect(page.locator('.ant-modal-title', { hasText: '设置' })).toBeVisible()
  await page.locator('.ant-modal').getByText('定时任务', { exact: true }).click()
  // SchedulePage 渲染——顶部说明含「定时任务」。
  await expect(page.locator('.ant-modal').getByText(/个定时任务/)).toBeVisible({
    timeout: 10_000,
  })
}

test.describe('流程10 — 定时任务', () => {
  test('新建 interval 任务 → 卡片显 → 立即执行 → 历史显 success → 删除', async ({ page }) => {
    await page.goto('/')
    await openScheduleSettings(page)

    // ── 新建任务 ──
    await page.locator('.ant-modal').getByRole('button', { name: '新建任务' }).click()
    await expect(page.locator('.ant-modal-title', { hasText: '新建定时任务' })).toBeVisible()

    // 任务名称。
    await page.getByPlaceholder('如：每日晨报').fill(TASK_NAME)
    // 派发内容。
    await page.getByPlaceholder('如：生成今日工作晨报并发送').fill(TASK_CONTENT)

    // 群组 + 智能体 Select——antd Select 的 placeholder 是 .ant-select-selection-placeholder
    // 元素（非 input placeholder 属性），getByPlaceholder 匹配不到。点 .ant-select 根
    // 容器即可开下拉（antd select 整个 selector 区可点）。
    // 创建 Modal 是 portaled 到 body 的独立 .ant-modal（dbg 验证 body 下 2 个 modal：
    // [SettingsModal, 创建Modal]），用 title 文本「新建定时任务」过滤精确定位本 Modal。
    // Modal 里 .ant-select 顺序：group_id（0）、agent_id（1）、freq_unit（2，interval 默认显）。
    const createModal = page
      .locator('.ant-modal')
      .filter({ hasText: '新建定时任务' })
      .last()

    // 群组 Select（第 1 个）。
    await createModal.locator('.ant-select').nth(0).click()
    await page
      .locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option')
      .filter({ hasText: SEED_GROUP_NAME })
      .click()
    await page.waitForTimeout(400)

    // 目标智能体 Select（第 2 个）——选 seed 协调者（首个 option）。
    await createModal.locator('.ant-select').nth(1).click()
    await page
      .locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option')
      .first()
      .click()
    await page.waitForTimeout(300)
    // 关下拉（点创建 Modal 标题区收起，避免遮挡）——body 下 2 个 .ant-modal-title
    // （设置 + 新建定时任务），用 .last() 取创建 Modal 的标题。
    await page.locator('.ant-modal-title').last().click()
    await page.waitForTimeout(300)

    // schedule_type 默认 interval（openCreate 设 schedule_type:'interval' + freq_value:1 + freq_unit:'hours'）。
    // 不改调度参数，默认 1 小时间隔——立即执行会 force fire 不依赖调度。
    // 创建。
    await page.locator('.ant-modal-footer .ant-btn-primary').first().click()
    await expect(page.getByText(/已创建定时任务/).first()).toBeVisible({ timeout: 10_000 })

    // 卡片显（标题含任务名 + interval Tag + 启用中 Tag）。
    const card = page
      .locator('.ant-modal .ant-card')
      .filter({ hasText: TASK_NAME })
      .first()
    await expect(card).toBeVisible({ timeout: 10_000 })
    await expect(card).toContainText('定间隔')
    await expect(card).toContainText('启用中')

    // ── 立即执行 ──
    await card.getByRole('button', { name: '立即执行' }).click()
    await expect(page.getByText(/已触发.*立即执行/).first()).toBeVisible({ timeout: 10_000 })

    // ── 历史 ──
    await card.getByRole('button', { name: '历史' }).click()
    // 抽屉打开（Drawer title 含「执行历史」）。
    await expect(page.locator('.ant-drawer-title', { hasText: '执行历史' })).toBeVisible()
    // 等待 run 完成（pending→running→success，mock LLM 秒回但 engine invoke 有开销）。
    // 抽屉 Timeline 显 success Tag。重试等待（run 可能还没落库或还在 running）。
    await expect(
      page.locator('.ant-drawer').getByText('成功', { exact: true }).first(),
    ).toBeVisible({ timeout: 30_000 })

    // 关抽屉——antd Drawer 关闭按钮是 icon-only（CloseOutlined），用 .ant-drawer-close 定位。
    await page.locator('.ant-drawer-close').click()
    // 抽屉关闭动画（给 antd Drawer 退出动画时间）。
    await page.waitForTimeout(600)

    // ── 删除 ──
    // 抽屉关闭后重新定位卡片（Drawer 关闭不重渲染列表，但 card 引用重新取更稳）。
    const card2 = page
      .locator('.ant-modal .ant-card')
      .filter({ hasText: TASK_NAME })
      .first()
    await card2.getByRole('button', { name: '删除' }).click()
    // Popconfirm 确认（antd v6 渲染为 .ant-popover，danger 按钮）。
    await page
      .locator('.ant-popover:not(.ant-popover-hidden) .ant-btn-dangerous')
      .click()
    // 删除成功 toast——SchedulePage handleDelete message.success(`已删除「${task.name}」`)。
    await expect(page.getByText(/已删除「/).first()).toBeVisible({ timeout: 10_000 })
    await expect(
      page.locator('.ant-modal .ant-card').filter({ hasText: TASK_NAME }),
    ).toHaveCount(0)
  })
})

/**
 * 任务20b 流程3 — 群聊协作（建群 + 派工 + 计划确认闭环）。
 *
 * 覆盖：
 *  - 侧栏「多智能体」分组「新建群组」→ CreateGroupModal → 填群名 + 选群主 + 选成员
 *    + 协作模式 → 创建 → 群出现在侧栏。
 *  - 选群 → ChatView 渲染（标题=群名 + 中心化 Tag + 成员数）。
 *  - 发「派工」关键词消息 → coordinator 走 dispatch 路径（mock _coordinator_dispatch_payload
 *    回 action=dispatch + 真 plan）→ PlanConfirmCard 渲染计划步骤。
 *  - 点「确认继续」→ resume → dispatch_next fan-out 到 agent 节点 → worker brain 回复
 *    （mock 默认 chat 探针串）→ 回复气泡出现。
 *
 * 这是计划确认闭环（PL-02）的端到端验证——coordinator interrupt → PlanConfirmCard
 * → planApi.confirm → Command(resume) → node_dispatch_next → Send → agent 节点 →
 * worker brain → emit_task_token → WS → 前端流式/定稿。
 *
 * mock LLM 关键词分流：「派工」→ _coordinator_dispatch_payload（action=dispatch +
 * plan=[{agent_id:agent_frontend_1,...}]）。派工后 worker 收到的 instruction 是
 * 「实现登录页面表单与校验」（不含 card/execute/handoff/派工 关键词），故 worker brain
 * 走默认 chat 回 DEFAULT_REPLY_TEXT 探针串。
 */
import { expect, test } from '@playwright/test'

// 测试建的群名——便于 globalSetup cleanupStaleTestEntities 识别清理（按 name 段）。
const E2E_GROUP_NAME = 'e2e测试协作组'

test.describe('流程3 — 群聊协作', () => {
  test('建群 → 选群 → 发派工消息 → 计划卡 → 确认 → worker 回复', async ({ page }) => {
    await page.goto('/')
    // 侧栏「多智能体」分组默认展开，点「新建群组」按钮。
    await page.getByRole('button', { name: '新建群组' }).click()
    await expect(page.locator('.ant-modal-title', { hasText: '新建群组' })).toBeVisible()

    // 填群名。
    await page.getByPlaceholder('如：登录重构攻坚组').fill(E2E_GROUP_NAME)
    // 选群主——seed「协调者」。Select showSearch，开下拉点选项。
    await page.locator('.ant-modal .ant-select').first().click()
    await page
      .locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option')
      .first()
      .waitFor({ state: 'visible', timeout: 5000 })
    await page
      .locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option')
      .first()
      .click()

    // 选成员——mode="multiple"，是第二个 Select。点「前端工程师」+「后端工程师」。
    const memberSelect = page.locator('.ant-modal .ant-select').nth(1)
    await memberSelect.click()
    await page
      .locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option')
      .first()
      .waitFor({ state: 'visible', timeout: 5000 })
    // 选下拉里前两项成员（前端工程师 / 后端工程师）。
    const memberOpts = page.locator(
      '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option',
    )
    await memberOpts.nth(0).click()
    await memberOpts.nth(1).click()
    // 关下拉（点 Modal 标题区收起，避免遮挡）。
    await page.locator('.ant-modal-title').click()
    await page.waitForTimeout(300)

    // 提交创建。
    await page.locator('.ant-modal-footer .ant-btn-primary').click()
    await expect(page.getByText('群组已创建')).toBeVisible({ timeout: 5000 })

    // 群出现在侧栏——点它进入群聊。
    await page.locator('.ant-layout-sider').getByText(E2E_GROUP_NAME, { exact: true }).click()
    // ChatView 标题显群名 + 中心化 Tag。
    await expect(page.getByText(E2E_GROUP_NAME, { exact: true }).first()).toBeVisible({
      timeout: 5000,
    })
    await expect(page.getByText('中心化', { exact: true })).toBeVisible()

    // 发「派工」关键词消息触发 coordinator dispatch 路径。
    const input = page.getByPlaceholder(/输入消息/)
    await input.click()
    await input.fill('请派工完成登录功能')
    await input.press('Enter')

    // PlanConfirmCard 应出现（coordinator emit_coordinator_plan → WS → 前端 plan state）。
    // 卡片标题含「协调者计划」+「共 1 步」。
    await expect(page.getByText('协调者计划').first()).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText(/共 1 步/).first()).toBeVisible({ timeout: 10_000 })

    // 点「确认继续」按钮（PlanConfirmCard 内的 primary 按钮）。
    await page.getByRole('button', { name: '确认继续' }).click()
    await expect(page.getByText('已确认，计划开始派发')).toBeVisible({ timeout: 5000 })

    // fan-out 后 worker brain 回复——mock 默认回 DEFAULT_REPLY_TEXT 探针串。
    await expect(
      page.locator('.chat-msg').filter({ hasText: '[e2e-mock]' }).first(),
    ).toBeVisible({ timeout: 30_000 })
  })
})

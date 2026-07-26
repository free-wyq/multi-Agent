/**
 * 任务20c 流程11 — IM 渠道（CRUD + 测试探针 + 启停）。
 *
 * 覆盖：
 *  - 设置弹窗 → 「即时消息」导航项 → ImChannelPanel 渲染。
 *  - 「添加渠道」Modal → name + platform=wechat(默认) + target_kind=single(默认) +
 *    target_conversation_id(选 seed 单聊会话) → 保存 → 卡片显。
 *  - 卡片「测试」→ 触发后端 mock 出站探针（adapter.send_outbound → logger.info）→
 *    返 ok=True + 内联「最近测试：✓ 探针成功」。
 *  - 删除渠道 → Popconfirm → 卡片消失。
 *
 * 后端 /api/im-channels CRUD + /enable /disable /test（任务19c）。test 探针触发
 * adapter.send_outbound，mock 阶段走 logger.info（任务19b 三 adapter），返 ImChannelTestResult
 * {ok:true, platform, target, error:null}。任务19e 契约测试已锁 mock 出站日志 +
 * disable 后不再投递——本测锁 UI 链路（CRUD + test 探针 + 内联结果展示）。
 *
 * target_conversation_id 必须是已存在的单聊会话（route_direct_message 会用它）。
 * 测试前先用 API 建一个单聊会话（selectAgent 等价），让 Modal 的目标 Select 有选项。
 *
 * 隔离：测试建的渠道落 e2e 临时 DB，末尾删除清理。
 */
import { expect, test } from '@playwright/test'
import { E2E_API_BASE, SEED_AGENT_IDS } from './helpers'

const CHANNEL_NAME = 'e2e测试IM渠道'

// 打开设置弹窗并切到「即时消息」导航项。
async function openImSettings(page: import('@playwright/test').Page): Promise<void> {
  await page.locator('.ant-layout-sider').getByText('用户信息', { exact: true }).click()
  await expect(page.locator('.ant-modal-title', { hasText: '设置' })).toBeVisible()
  await page.locator('.ant-modal').getByText('即时消息', { exact: true }).click()
  // ImChannelPanel 渲染——顶部说明「已配置 N 个 IM 渠道」出现即证页面挂载。
  await expect(
    page.locator('.ant-modal').getByText(/已配置 \d+ 个 IM 渠道/),
  ).toBeVisible({ timeout: 10_000 })
}

test.describe('流程11 — IM 渠道', () => {
  test('添加 wechat 单聊渠道 → 卡片显 → 测试探针成功 → 删除消失', async ({
    page,
    request,
  }) => {
    // 前置：建一个单聊会话作为 target_conversation_id 候选（Modal 的目标 Select 读它）。
    const conv = await request.post(`${E2E_API_BASE}/api/conversations`, {
      data: { agent_id: SEED_AGENT_IDS.frontend },
    })
    expect(conv.ok()).toBeTruthy()
    const convId = (await conv.json()).id

    try {
      await page.goto('/')
      await openImSettings(page)

      // ── 添加渠道 ──
      await page.locator('.ant-modal').getByRole('button', { name: '添加渠道' }).click()
      await expect(page.locator('.ant-modal-title', { hasText: '添加 IM 渠道' })).toBeVisible()

      // 渠道名称。
      await page.getByPlaceholder('如：客服微信群').fill(CHANNEL_NAME)
      // platform 默认 wechat（openCreate setFieldsValue platform:'wechat'）。
      // target_kind 默认 single（openCreate setFieldsValue target_kind:'single'）。

      // 投递目标 Select——选刚建的单聊会话。antd Select placeholder 是
      // .ant-select-selection-placeholder 元素（非 input placeholder），getByPlaceholder
      // 匹配不到。创建 Modal（portaled 独立 .ant-modal，title=添加 IM 渠道）里 .ant-select
      // 顺序：target_conversation_id(0)、target_agent_id(1)——platform/target_kind 是
      // Segmented（非 Select）。点第 1 个 Select 开下拉选首个 option（刚建的会话）。
      const imCreateModal = page
        .locator('.ant-modal')
        .filter({ hasText: '添加 IM 渠道' })
        .last()
      await imCreateModal.locator('.ant-select').nth(0).click()
      await page
        .locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option')
        .first()
        .click()
      // 关下拉。
      await page.locator('.ant-modal-title').last().click()
      await page.waitForTimeout(300)

      // 平台凭证可空（mock adapter 不验凭证），不填 config 子表单。
      // enabled 默认 false（openCreate setFieldsValue enabled:false）——不启用，仅建渠道
      // 测 CRUD + test 探针（test 不要求 enabled）。保存。
      await page.locator('.ant-modal-footer .ant-btn-primary').first().click()
      await expect(page.getByText(/已创建/).first()).toBeVisible({ timeout: 10_000 })

      // 卡片显（标题含渠道名 + 企业微信 Tag）。
      const card = page
        .locator('.ant-modal .ant-card')
        .filter({ hasText: CHANNEL_NAME })
        .first()
      await expect(card).toBeVisible({ timeout: 10_000 })
      await expect(card).toContainText('企业微信')

      // ── 测试探针 ──
      await card.getByRole('button', { name: '测试' }).click()
      // 内联「最近测试：✓ 探针成功」（adapter 加载 + logger.info 出站行触发，秒回）。
      await expect(card.getByText(/✓ 探针成功/)).toBeVisible({ timeout: 15_000 })

      // ── 删除 ──
      await card.getByRole('button', { name: '删除' }).click()
      await page
        .locator('.ant-popover:not(.ant-popover-hidden) .ant-btn-dangerous')
        .click()
      await expect(page.getByText(/已删除|删除成功/).first()).toBeVisible({ timeout: 10_000 })
      await expect(
        page.locator('.ant-modal .ant-card').filter({ hasText: CHANNEL_NAME }),
      ).toHaveCount(0)
    } finally {
      // 清理会话（渠道已在测试里删，会话留着无用）。
      await request.delete(`${E2E_API_BASE}/api/conversations/${convId}`).catch(() => {})
    }
  })
})

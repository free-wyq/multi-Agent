/**
 * 任务20b 流程2 — 单聊对话（选 agent → 发消息 → 收 mock 回复）。
 *
 * 覆盖：
 *  - 侧栏「智能体」分组里点 seed agent → selectAgent find-or-create 单聊会话 →
 *    ChatView 渲染（标题=agent 名 + 角色）。
 *  - 输入框打字 → Enter 发送 → 用户气泡出现（optimistic）→ mock LLM 回复气泡出现。
 *
 * LLM mock 化：active provider 已被 globalSetup 指向 mock（playwright_mock_llm_server.py）。
 * mock 默认回 DEFAULT_REPLY_TEXT 探针串（包在 strict JSON content 字段里）。断言
 * 「页面里出现探针串」即证 路径3（route_direct_message → resident worker engine →
 * brain chat_completion_stream → mock SSE → ContentExtractor → emit_task_token →
 * WS → 前端流式气泡→定稿）全通。
 *
 * 单聊回复走 worker brain graph（coordinator_id="" → is_coordinator=False → worker graph），
 * 故探针串来自 _brain_chat_payload（worker 的 chat action content）。
 */
import { expect, test } from '@playwright/test'
import { SEED_AGENT_NAMES, sendAndWaitReply } from './helpers'

test.describe('流程2 — 单聊对话', () => {
  test('选 seed agent 开单聊，发消息收到 mock 回复', async ({ page }) => {
    await page.goto('/')
    // 侧栏「智能体」分组默认展开（Sidebar openKeys=['agents','groups']）。
    // 点 seed agent「前端工程师」→ selectAgent find-or-create 单聊 → 切到对话视图。
    // SidebarItem 是自定义 div，title 文本即 agent.name；侧栏列表里点它。
    // 用 getByText 在侧栏范围（左侧 Sider）内定位，避免与正文同名元素冲突。
    await page.locator('.ant-layout-sider').getByText('前端工程师', { exact: true }).click()

    // ChatView 标题应显 agent 名「前端工程师」+ 角色。
    // 标题是 antd Typography.Text strong（非 heading），用 getByText 精确匹配。
    await expect(page.getByText('前端工程师', { exact: true }).first()).toBeVisible({
      timeout: 5000,
    })

    // 输入框可见（chatGroupId 已设 → ChatPanel 渲染输入区）。
    const input = page.getByPlaceholder(/输入消息/)
    await expect(input).toBeVisible({ timeout: 5000 })

    // 发消息并等 mock 回复探针串出现（mock 默认回 DEFAULT_REPLY_TEXT）。
    // mock 把可见正文包在 {"action":"chat","content":"[e2e-mock] ...","reasoning":"..."}。
    await sendAndWaitReply(
      page,
      '你好，请介绍一下自己',
      '[e2e-mock]',
      30_000,
    )

    // 用户气泡（自己发的消息）也应出现。
    await expect(
      page.locator('.chat-msg').filter({ hasText: '你好，请介绍一下自己' }),
    ).toBeVisible()
  })

  test('切到另一个 seed agent 开新单聊，对话独立', async ({ page }) => {
    await page.goto('/')
    // 先开「前端工程师」单聊发一条。
    await page.locator('.ant-layout-sider').getByText('前端工程师', { exact: true }).click()
    await expect(page.getByText('前端工程师', { exact: true }).first()).toBeVisible()
    const input = page.getByPlaceholder(/输入消息/)
    await input.click()
    await input.fill('给前端的私聊消息')
    await input.press('Enter')
    // 等回复。
    await expect(page.locator('.chat-msg').filter({ hasText: '[e2e-mock]' }).first()).toBeVisible({
      timeout: 30_000,
    })

    // 切到「后端工程师」——侧栏点它，开新单聊会话。
    await page.locator('.ant-layout-sider').getByText('后端工程师', { exact: true }).click()
    await expect(page.getByText('后端工程师', { exact: true }).first()).toBeVisible({
      timeout: 5000,
    })
    // 新会话应是空的（不含上一会话的消息）——「给前端的私聊消息」不应在新会话里。
    // ChatPanel 切 groupId 会清空 chatMessages 重新拉，给一点时间。
    await page.waitForTimeout(1500)
    await expect(
      page.locator('.chat-msg').filter({ hasText: '给前端的私聊消息' }),
    ).toHaveCount(0)
  })
})

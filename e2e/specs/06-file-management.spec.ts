/**
 * 任务20b 流程6 — 文件管理（群信息/会话信息抽屉的「文件」Tab）。
 *
 * 覆盖：
 *  - 单聊会话信息抽屉 → 「文件」Tab → 列该会话工作区产物（task14c）。
 *  - 群聊群信息抽屉 → 「文件」Tab → 列群工作区产物（task14b）。
 *
 * 文件来源：worker execute 路径调 file_write 工具落盘工作区。但 mock LLM 不发
 * tool_calls（brain=chat 即止）——execute 路径的 ReAct 工具调用由真实 MCP echo
 * fixture 触发，20b 不覆盖真工具链路。故本测用「直接在工作区目录放一个文件」
 * 模拟产物存在，验证「抽屉文件 Tab 能列出 + 显示文件名/大小」——锁 UI 链路
 * （drawer open → list_files → 渲染），不锁工具落盘（那是 20c/16b 的范畴）。
 *
 * 直接落盘 .e2e-data/workspaces/<id>/ 下放一个文件（与 crud.list_files 的
 * workspace_path 同目录），drawer 打开应列出它。
 */
import { expect, test } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'
import { E2E_API_BASE, SEED_AGENT_IDS } from './helpers'

test.describe('流程6 — 文件管理', () => {
  test('单聊会话信息抽屉 → 文件 Tab 列工作区产物', async ({ page, request }) => {
    // 先通过 API 开单聊会话（selectAgent 等价），拿 conversation_id。
    const conv = await request.post(`${E2E_API_BASE}/api/conversations`, {
      data: { agent_id: SEED_AGENT_IDS.frontend },
    })
    expect(conv.ok()).toBeTruthy()
    const convBody = await conv.json()
    const convId = convBody.id

    // 在该会话工作区目录直接放一个文件（模拟 task 产物）。
    // workspace_path = DATA_DIR/workspaces/<conversation_id>/。
    const wsDir = path.join('.e2e-data', 'workspaces', convId)
    fs.mkdirSync(wsDir, { recursive: true })
    const fileName = 'e2e-artifact.md'
    fs.writeFileSync(path.join(wsDir, fileName), '# e2e 产物\n单聊文件管理探针。', 'utf-8')

    try {
      await page.goto('/')
      // 侧栏点该 agent 开单聊（find-or-create 会复用刚建的会话）。
      await page.locator('.ant-layout-sider').getByText('前端工程师', { exact: true }).click()
      await expect(page.getByText('前端工程师', { exact: true }).first()).toBeVisible()

      // 点「会话信息」抽屉按钮（ChatView 标题栏右侧 InfoCircleOutlined 按钮）。
      // antd Tooltip 的 title 不进按钮 aria-label——按钮是 icon-only（无文本）。
      // 用 .anticon-info-circle 精确定位（单聊会话信息按钮的图标）。
      await page.locator('.anticon-info-circle').click()
      // 抽屉打开（Drawer title="会话信息"）。
      await expect(page.locator('.ant-drawer-title', { hasText: '会话信息' })).toBeVisible()

      // 切到「文件」Tab。
      await page.locator('.ant-drawer').getByRole('tab', { name: '文件' }).click()

      // 文件列表应含刚放的文件「e2e-artifact.md」。
      await expect(
        page.locator('.ant-drawer').getByText('e2e-artifact.md'),
      ).toBeVisible({ timeout: 10_000 })
    } finally {
      fs.rmSync(wsDir, { recursive: true, force: true })
      await request.delete(`${E2E_API_BASE}/api/conversations/${convId}`).catch(() => {})
    }
  })

  test('群聊群信息抽屉 → 文件 Tab 列工作区产物', async ({ page, request }) => {
    // 建 e2e 群（用 seed coordinator + frontend member）。
    const grp = await request.post(`${E2E_API_BASE}/api/groups`, {
      data: {
        name: 'e2e文件管理群',
        coordinator_id: SEED_AGENT_IDS.coordinator,
        member_ids: [SEED_AGENT_IDS.frontend],
        config: { collaboration_mode: 'centralized' },
      },
    })
    expect(grp.ok()).toBeTruthy()
    const g = await grp.json()
    const gid = g.id

    // 在群工作区放一个文件。
    const wsDir = path.join('.e2e-data', 'workspaces', gid)
    fs.mkdirSync(wsDir, { recursive: true })
    const fileName = 'e2e-group-artifact.txt'
    fs.writeFileSync(path.join(wsDir, fileName), '群聊文件管理探针', 'utf-8')

    try {
      await page.goto('/')
      // 选刚建的群——侧栏点它。
      await page.locator('.ant-layout-sider').getByText('e2e文件管理群', { exact: true }).click()
      await expect(page.getByText('e2e文件管理群', { exact: true }).first()).toBeVisible()

      // 点「群信息」抽屉按钮（ChatView 标题栏 TeamOutlined 按钮，icon-only）。
      // .anticon-team 在侧栏群组项 + ChatView header 都有（多处用 TeamOutlined），
      // 限定 ChatView 标题栏范围（header 的 button 内）+ 取包裹它的 button。
      // 侧栏的 .anticon-team 在 SidebarItem 里非按钮包裹；header 的是 button>icon。
      await page
        .locator('.ant-layout-content button:has(.anticon-team)')
        .click()
      await expect(page.locator('.ant-drawer-title', { hasText: '群信息' })).toBeVisible()

      // 切到「文件」Tab。
      await page.locator('.ant-drawer').getByRole('tab', { name: '文件' }).click()

      // 文件列表含「e2e-group-artifact.txt」。
      await expect(
        page.locator('.ant-drawer').getByText('e2e-group-artifact.txt'),
      ).toBeVisible({ timeout: 10_000 })
    } finally {
      fs.rmSync(wsDir, { recursive: true, force: true })
      await request.delete(`${E2E_API_BASE}/api/groups/${gid}`).catch(() => {})
    }
  })
})

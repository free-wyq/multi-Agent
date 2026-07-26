/**
 * 任务20c 流程8 — MCP 连接管理（CRUD + 启用 + 自省工具 + 挂载到 Agent）。
 *
 * 覆盖：
 *  - 设置弹窗 → 「MCP」导航项 → McpPage 渲染（seed 无 MCP 连接 → Empty 占位）。
 *  - 「添加连接」Modal → stdio transport → 填 name + command=python3 +
 *    args=echo_mcp_server.py 脚本绝对路径 → 创建 → 卡片出现。
 *  - 卡片「启用」（默认 createOpen 已 enabled=true，但显式点一次 toggle 验路径）。
 *  - 展开「暴露工具」Collapse → 自省真 spawn echo server → 显 echo 工具 Tag。
 *  - 删除 → Popconfirm 确认 → 卡片消失。
 *
 * 不走真 ReAct 工具调用（mock LLM 不发 tool_calls，brain=chat 即止）——真 ReAct
 * 工具链路由任务16b 的契约测试覆盖（FakeReactChatModel + 真 echo fixture）。
 * 本测锁 MCP 管理 UI 链路（CRUD + 自省 + 挂载入口），不锁 execute 路径。
 *
 * 命令白名单：command 必须在 {npx,uvx,python,python3,node,uv} 内（api/mcp.py
 * DEFAULT_STDIO_COMMAND_WHITELIST）。echo fixture 路径是绝对路径（含 /），但白名单
 * 只校验 command 段（不含空格+在白名单），args 段不校验路径——故 command=python3 +
 * args=[<repo>/backend/tests/fixtures/echo_mcp_server.py] 可过白名单直接落库
 * （与任务16c 修白名单补 python3 后的生产行为一致）。
 *
 * 隔离：测试建的 MCP 连接落 e2e 临时 DB（.e2e-data）。echo server 子进程由
 * langchain-mcp-adapters 在自省时 spawn，测试结束删连接即停子进程（adapter
 * 的 context manager 退出会 terminate）。echo fixture 无副作用无网络，确定性。
 */
import { expect, test } from '@playwright/test'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'
import { E2E_API_BASE, SEED_AGENT_IDS } from './helpers'

// echo MCP fixture 脚本绝对路径（任务16a）——自省会真 spawn 它暴露 echo 工具。
// ESM 无 __dirname，用 fileURLToPath(import.meta.url) 推（与 playwright.config.ts 同款）。
const __dirname = path.dirname(fileURLToPath(import.meta.url))
const ECHO_FIXTURE = path.join(
  __dirname,
  '..',
  '..',
  'backend',
  'tests',
  'fixtures',
  'echo_mcp_server.py',
)

// 测试建连接的稳定 name——便于 globalSetup cleanupStaleTestEntities 识别（虽然
// MCP 连接不在 cleanup 范围，但本测末尾自行清理）。e2e 临时 DB 不残留。
const MCP_NAME = 'e2e-echo-mcp'

// 打开设置弹窗并切到「MCP」导航项的复用步骤。
async function openMcpSettings(page: import('@playwright/test').Page): Promise<void> {
  // 侧栏左下角「用户信息」条（Avatar + 文本「用户信息」）打开设置弹窗。
  await page.locator('.ant-layout-sider').getByText('用户信息', { exact: true }).click()
  await expect(page.locator('.ant-modal-title', { hasText: '设置' })).toBeVisible()
  // 左侧导航点「MCP」。
  await page.locator('.ant-modal').getByText('MCP', { exact: true }).click()
  // McpPage 渲染——顶部说明「已配置 N 个 MCP 连接」出现即证页面挂载。
  await expect(
    page.locator('.ant-modal').getByText(/已配置 \d+ 个 MCP 连接/),
  ).toBeVisible({ timeout: 10_000 })
}

test.describe('流程8 — MCP 连接管理', () => {
  test('添加 stdio echo 连接 → 卡片显 → 自省 echo 工具 → 删除消失', async ({ page }) => {
    // 前置：echo fixture 脚本必须存在（自省要 spawn 它）。
    expect(fs.existsSync(ECHO_FIXTURE)).toBe(true)

    await page.goto('/')
    await openMcpSettings(page)

    // 记录初始连接数（globalSetup 不建 MCP，应为 0 或仅历史残留；用 name 过滤断言）。
    // 「添加连接」按钮打开 Modal。
    await page.locator('.ant-modal').getByRole('button', { name: '添加连接' }).click()
    await expect(page.locator('.ant-modal-title', { hasText: '添加 MCP 连接' })).toBeVisible()

    // 连接名称。
    await page.getByPlaceholder('如：文件系统 MCP').fill(MCP_NAME)
    // transport 默认 stdio（openCreate 设 transport:'stdio'），不用切。
    // 启动命令——白名单内（python3）。
    await page.getByPlaceholder('npx').fill('python3')
    // 参数——echo fixture 脚本绝对路径，每行一个参数（这里只一行）。
    await page
      .getByPlaceholder(/-y\b/)
      .fill(ECHO_FIXTURE)

    // 创建（antd 中文两字按钮插空格 → 用 modal-footer primary 定位）。
    await page.locator('.ant-modal-footer .ant-btn-primary').first().click()
    // 成功 toast + 卡片出现。
    await expect(page.getByText(`已添加「${MCP_NAME}」`)).toBeVisible({ timeout: 10_000 })

    // 卡片标题显连接名 + stdio Tag + 命令预览含 python3。
    const card = page
      .locator('.ant-modal .ant-card')
      .filter({ hasText: MCP_NAME })
      .first()
    await expect(card).toBeVisible()
    await expect(card).toContainText('stdio')

    // ── 自省工具：展开「暴露工具」Collapse ──
    // echo fixture 暴露单个 echo 工具，展开后真 spawn echo server 自省，显 echo Tag。
    // 自省会真起子进程，给足超时（langchain-mcp-adapters spawn + JSON-RPC 握手 ~5-10s）。
    await card.getByText('暴露工具').click()
    await expect(card.getByText('echo', { exact: true })).toBeVisible({ timeout: 30_000 })

    // ── 删除 ──
    await card.getByRole('button', { name: '删除' }).click()
    // Popconfirm 确认（antd v6 渲染为 .ant-popover，danger 按钮）。
    await page
      .locator('.ant-popover:not(.ant-popover-hidden) .ant-btn-dangerous')
      .click()
    await expect(page.getByText(`已删除「${MCP_NAME}」`)).toBeVisible({ timeout: 10_000 })
    // 卡片消失。
    await expect(
      page.locator('.ant-modal .ant-card').filter({ hasText: MCP_NAME }),
    ).toHaveCount(0)
  })

  test('挂载 MCP 到 seed agent（API 直调，验后端挂载链路）', async ({ request }) => {
    // 走 API 建 echo MCP 连接 + 挂载到 seed agent_frontend_1，验 mount_mcp 后端链路
    // （后端 crud.mount_mcp 把 mcp_id 加进 agent.mounted_mcp，返回更新后的 AgentDefinition）。
    // 这条链路前端 UI 入口在 AgentPage「挂载 MCP」交互（较深），用 API 直调更稳，
    // 覆盖「挂载 + 卸载」后端真路径（与任务16b 契约测试同源，但本测跨进程打到真后端）。
    const created = await request.post(`${E2E_API_BASE}/api/mcp`, {
      data: {
        name: MCP_NAME,
        transport: 'stdio',
        command: 'python3',
        args: [ECHO_FIXTURE],
        enabled: true,
      },
    })
    expect(created.ok()).toBeTruthy()
    const conn = await created.json()
    const mcpId = conn.id

    try {
      // 挂载到 seed agent_frontend_1。
      const mountResp = await request.post(`${E2E_API_BASE}/api/mcp/${mcpId}/mount`, {
        data: { agentId: SEED_AGENT_IDS.frontend },
      })
      expect(mountResp.ok()).toBeTruthy()
      const agent = await mountResp.json()
      // mounted_mcp 应含新 mcp_id。
      expect(agent?.mounted_mcp ?? []).toContain(mcpId)

      // 卸载。
      const unmountResp = await request.post(
        `${E2E_API_BASE}/api/mcp/${mcpId}/unmount`,
        { data: { agentId: SEED_AGENT_IDS.frontend } },
      )
      expect(unmountResp.ok()).toBeTruthy()
      const agent2 = await unmountResp.json()
      expect(agent2?.mounted_mcp ?? []).not.toContain(mcpId)
    } finally {
      // 清理：删 MCP 连接（后端级联从 agent.mounted_mcp 移除引用）。
      await request.delete(`${E2E_API_BASE}/api/mcp/${mcpId}`).catch(() => {})
    }
  })
})

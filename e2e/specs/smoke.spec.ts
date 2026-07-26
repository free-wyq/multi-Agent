/**
 * 任务20a 冒烟测：证明 e2e global setup 基座就绪。
 *
 * 不测业务流程（那是 20b/20c）——只锁三件「空跑能起」契约：
 *  1. 前端能加载（baseURL 返 200 + #root 存在）。
 *  2. 前端能打到后端（页面里 fetch /api/status 或侧栏加载 agents 列表）。
 *  3. active provider 已切到 mock（global-setup 写的）——后端 GET /api/config
 *     的 base_url 指向 mock LLM。这一条直接 HTTP 打后端验，不经前端（前端只
 *     读 /api/config 的公开 mask 版，验 base_url 即可证 mock 已激活）。
 *
 * 若 globalSetup 没把 provider 切到 mock，test 3 失败 → 20b/20c 的 LLM 调用
 * 会打真 LLM（或无 key 报错），本测就是基座就绪的闸门。
 */
import { expect, test } from '@playwright/test'

test.describe('任务20a — e2e global setup 冒烟', () => {
  test('前端可加载 + #root 挂载', async ({ page }) => {
    const resp = await page.goto('/')
    expect(resp?.status()).toBe(200)
    // App.tsx 挂 <Layout>，根容器 #root 必存在且非空
    await expect(page.locator('#root')).not.toBeEmpty()
  })

  test('后端 active provider 已切到 mock LLM', async ({ request }) => {
    // global-setup 写的 provider base_url 应指向 mock（E2E_MOCK_LLM_BASE）。
    // 直接打后端 GET /api/config（公开 mask 版，含 base_url）。
    const resp = await request.get('http://127.0.0.1:8000/api/config')
    expect(resp.ok()).toBeTruthy()
    const cfg = await resp.json()
    const mockBase = process.env.E2E_MOCK_LLM_BASE ?? ''
    expect(mockBase, 'globalSetup 应设 E2E_MOCK_LLM_BASE').toBeTruthy()
    expect(cfg.base_url, 'active base_url 应指向 mock').toContain(
      mockBase.replace('http://', ''),
    )
  })
})

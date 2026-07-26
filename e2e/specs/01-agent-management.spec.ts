/**
 * 任务20b 流程1 — 智能体管理（CRUD + 模板雇佣）。
 *
 * 覆盖：
 *  - 顶部 Segmented 切到「智能体广场」→ AgentPage 加载，渲染 seed 智能体卡片。
 *  - 「新建智能体」Modal 表单创建 → 新卡片出现。
 *  - 卡片「编辑」打开 Modal → 改名 → 「保存」→ 卡片名更新。
 *  - 卡片「删除」Popconfirm → 确认 → 卡片消失。
 *  - AG-11 模板广场展开 → 「雇佣」预设角色 → 落库为新员工。
 *
 * 不依赖 LLM（agentApi.create/update/delete/hireTemplate 都不调 LLM，
 * 只有 AG-01 「自然语言生成」才调 LLM——mock 也能回，但本测聚焦 CRUD，
 * 不走生成路径，减少对 mock 时序的耦合）。
 *
 * 隔离：本测创建的 agent 落 e2e 临时 DB（.e2e-data）。run 间不残留——
 * CI 每次新起 server + 新临时 DB；本地复用开发 DB 时由 test 末尾清理
 * （删测试建的 agent，避免开发 DB 攒测试数据）。
 */
import { expect, test } from '@playwright/test'

const SEED_AGENT_NAMES = ['协调者', '前端工程师', '后端工程师']

test.describe('流程1 — 智能体管理', () => {
  test('切到智能体广场，seed 智能体渲染', async ({ page }) => {
    await page.goto('/')
    // 顶部 Segmented 切到智能体广场。
    await page.getByRole('radiogroup').getByText('智能体广场').click()
    // AgentPage 标题。
    await expect(page.getByRole('heading', { name: '智能体管理' })).toBeVisible()
    // seed 三个智能体卡片名都出现（卡片标题是 .agent-template-name h4）。
    for (const name of SEED_AGENT_NAMES) {
      await expect(page.getByText(name, { exact: true }).first()).toBeVisible()
    }
  })

  test('新建智能体 → 卡片出现 → 编辑改名 → 删除消失', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('radiogroup').getByText('智能体广场').click()
    await expect(page.getByRole('heading', { name: '智能体管理' })).toBeVisible()

    // 等列表加载完——seed agent 名出现作为就绪信号（h4.agent-template-name 精确匹配）。
    await expect(
      page.locator('h4.agent-template-name', { hasText: '前端工程师' }),
    ).toBeVisible({ timeout: 10000 })

    // ── 新建 ──
    await page.getByRole('button', { name: '新建智能体' }).first().click()
    await expect(page.locator('.ant-modal-title', { hasText: '新建智能体' })).toBeVisible()
    const nameInput = page.getByPlaceholder('如：前端开发小新')
    await nameInput.fill('e2e测试智能体')
    // 角色必选——开下拉，点首个可见 option（antd v6 下拉在 .ant-select-dropdown:not(hidden)）。
    await page.locator('.ant-modal .ant-select').first().click()
    const roleOpt = page
      .locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option')
      .first()
    await roleOpt.waitFor({ state: 'visible', timeout: 5000 })
    await roleOpt.click()
    // Modal 底部「创建」按钮（与卡片「新建」区分：在 modal-footer 内 + primary）。
    // antd 中文会在两字按钮文字间插空格 → "创 建"，用 primary class 定位最稳。
    await page.locator('.ant-modal-footer .ant-btn-primary').click()

    // 成功 toast + 新卡片出现（h4.agent-template-name 精确匹配，避免 strict mode 多命中）。
    await expect(page.getByText('创建成功')).toBeVisible({ timeout: 5000 })
    await expect(
      page.locator('h4.agent-template-name', { hasText: 'e2e测试智能体' }),
    ).toBeVisible({ timeout: 5000 })

    // ── 编辑改名 ──
    // 定位新智能体卡片（h4 含 e2e测试智能体 的卡片），点其「编辑」按钮。
    const newCard = page
      .locator('.ant-card')
      .filter({ has: page.locator('h4.agent-template-name', { hasText: 'e2e测试智能体' }) })
      .first()
    await newCard.getByRole('button', { name: '编辑' }).click()
    await expect(page.locator('.ant-modal-title', { hasText: '编辑智能体' })).toBeVisible()
    // 名称 Input 现有值 e2e测试智能体，改填新名。
    const editNameInput = page.getByPlaceholder('如：前端开发小新')
    await editNameInput.fill('e2e改名智能体')
    await page.locator('.ant-modal-footer .ant-btn-primary').click()
    await expect(page.getByText('更新成功')).toBeVisible({ timeout: 5000 })
    await expect(
      page.locator('h4.agent-template-name', { hasText: 'e2e改名智能体' }),
    ).toBeVisible({ timeout: 5000 })
    // 旧名消失。
    await expect(
      page.locator('h4.agent-template-name', { hasText: 'e2e测试智能体' }),
    ).toHaveCount(0)

    // ── 删除 ──
    const renamedCard = page
      .locator('.ant-card')
      .filter({ has: page.locator('h4.agent-template-name', { hasText: 'e2e改名智能体' }) })
      .first()
    await renamedCard.getByRole('button', { name: '删除' }).click()
    // Popconfirm 弹「确认删除该智能体?」+ danger 删除按钮。
    // antd v6 Popconfirm 渲染为 .ant-popover（非 .ant-popconfirm），确认按钮是 danger primary。
    await expect(page.getByText('确认删除该智能体?')).toBeVisible()
    await page
      .locator('.ant-popover:not(.ant-popover-hidden) .ant-btn-dangerous')
      .click()
    await expect(page.getByText('删除成功')).toBeVisible({ timeout: 5000 })
    await expect(
      page.locator('h4.agent-template-name', { hasText: 'e2e改名智能体' }),
    ).toHaveCount(0)
  })

  test('AG-11 模板广场 → 雇佣预设角色 → 落库为新员工', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('radiogroup').getByText('智能体广场').click()
    await expect(page.getByRole('heading', { name: '智能体管理' })).toBeVisible()

    // 展开角色模板广场。
    await page.getByRole('button', { name: '角色模板广场' }).click()
    // 模板广场卡片渲染（catalog 含「后端开发工程师」模板）。
    const tplCard = page
      .locator('.agent-template-card')
      .filter({ hasText: '后端开发工程师' })
      .first()
    await expect(tplCard).toBeVisible({ timeout: 5000 })

    // 点该模板的「雇佣」按钮。
    await tplCard.getByRole('button', { name: '雇佣' }).click()
    // 雇佣调 hireTemplate（DB create 无 LLM），成功 toast + 员工列表刷新。
    await expect(page.getByText(/已雇佣.*加入团队/)).toBeVisible({ timeout: 5000 })

    // 员工网格里出现新 agent（模板名「后端开发工程师」name 落库，h4.agent-template-name）。
    // seed agent 名是「后端工程师」(role=backend_engineer)，模板名「后端开发工程师」
    // (role 也是 backend_engineer)——文本不同，精确匹配模板名命中新雇佣卡片。
    // 雇佣后员工卡片 h4 带 title 属性（与模板广场卡 h4 无 title 区分）；但两者 h4 文本
    // 都是「后端开发工程师」，用 .first() 取其一即可（刚雇佣，渲染在员工网格）。
    await expect(
      page.locator('h4.agent-template-name', { hasText: '后端开发工程师' }).first(),
    ).toBeVisible({ timeout: 5000 })

    // 清理：删掉雇佣的 agent（避免 .e2e-data 攒测试数据）。
    // 定位员工网格里（非模板广场）h4 含「后端开发工程师」的卡片：员工卡片 h4 带 title
    // 属性（Tooltip 用），模板广场卡 h4 无 title——靠 title 属性区分。
    const hiredCard = page
      .locator('.ant-card')
      .filter({
        has: page.locator('h4.agent-template-name[title]', {
          hasText: '后端开发工程师',
        }),
      })
      .first()
    await hiredCard.getByRole('button', { name: '删除' }).click()
    await page
      .locator('.ant-popover:not(.ant-popover-hidden) .ant-btn-dangerous')
      .click()
    await expect(page.getByText('删除成功')).toBeVisible({ timeout: 5000 })
  })
})

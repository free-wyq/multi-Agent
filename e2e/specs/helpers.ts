/**
 * 任务20b e2e 共享工具——specs 复用的常量与辅助函数。
 *
 * 单一真源：后端 API base 由 playwright.config.ts 顶层设的 process.env.E2E_API_BASE
 * （http://127.0.0.1:8766，与开发态 8000 错开）。specs 不应硬编码端口——统一读这里。
 *
 * 同时暴露 seed 实体常量（id/name）供 specs 断言与清理复用，与 backend/store/seed.py
 * 对齐。
 */

/** e2e 后端 API base（playwright.config.ts 顶层设 process.env.E2E_API_BASE）。 */
export const E2E_API_BASE =
  process.env.E2E_API_BASE ?? 'http://127.0.0.1:8766'

/** seed agent 名（seed.py 硬编码）——specs 断言 seed 列表用。 */
export const SEED_AGENT_NAMES = ['协调者', '前端工程师', '后端工程师']

/** seed 群组名（seed.py 硬编码 group_demo_1.name）。 */
export const SEED_GROUP_NAME = '演示协作组'

/** seed agent id（seed.py 硬编码）——specs 选 seed agent 开单聊/加群用。 */
export const SEED_AGENT_IDS = {
  coordinator: 'agent_coord_1',
  frontend: 'agent_frontend_1',
  backend: 'agent_backend_1',
} as const

/** seed 群组 id（seed.py 硬编码 group_demo_1）。 */
export const SEED_GROUP_ID = 'group_demo_1'

/**
 * 等待一个 agent_reply 持久化气泡出现（含探针串）。
 *
 * mock LLM 的 worker brain / coordinator chat 回复都把可见正文塞进 strict JSON 的
 * content 字段（见 playwright_mock_llm_server.py）。ContentExtractor 增量解码出 content
 * 值后，持久化的 agent_reply.content 就是探针串。断言「页面里出现探针串」即证
 * LLM mock 链路（后端→mock→解析→落盘→WS 推→前端渲染）全通。
 *
 * 默认 30s 超时——engine 冷启动 + 首轮 LLM（mock 秒回但 httpx 连接 + brain graph
 * invoke 有开销）+ WS 推送 + React 渲染，给足余量。mock 环境实际 5-10s 内完成。
 */
export async function expectReply(
  page: import('@playwright/test').Page,
  probe: string,
  timeoutMs = 30_000,
): Promise<void> {
  await page
    .locator('.chat-msg')
    .filter({ hasText: probe })
    .first()
    .waitFor({ state: 'visible', timeout: timeoutMs })
}

/**
 * 在聊天输入框发消息并等待回复探针出现。
 *
 * 封装「聚焦输入框→填入→Enter 发送→等回复」四步，specs 复用。
 * 输入框是 antd Input.TextArea，placeholder 含「输入消息」（非收束态）。
 */
export async function sendAndWaitReply(
  page: import('@playwright/test').Page,
  message: string,
  probe?: string,
  timeoutMs = 30_000,
): Promise<void> {
  const input = page.getByPlaceholder(/输入消息/)
  await input.click()
  await input.fill(message)
  await input.press('Enter')
  await expectReply(page, probe ?? message, timeoutMs)
}

/**
 * Playwright global setup (任务20a).
 *
 * 在所有 worker/test 启动前跑一次。职责：
 *  1. 分配一个空闲端口给 mock LLM server，spawn 它（aiohttp OpenAI 兼容 stub）。
 *  2. 直接 HTTP 调 FastAPI（webServer 已起）写一个 is_active 的 llm_providers 行，
 *     base_url 指向 mock LLM——让后端零代码改动，所有 LLM 调用（worker brain /
 *     coordinator / ReAct）都打到 mock。
 *  3. 健康检查 mock + provider 落库生效（GET /api/config 的 base_url 已切到 mock）。
 *  4. 把 mock server 句柄挂到 globalSetupScope，globalTeardown 里 kill。
 *
 * 为什么不改 .env：.env 是共享的开发态配置（真 LLM），e2e 不能污染它。用 DB 行
 * + activate（POST /api/providers is_active=true）覆盖 active cache，且 DB 是
 * webServer 起的 uvicorn 进程的临时 DATA_DIR（见 playwright.config.ts webServer
 * env MULTI_AGENT_DATA_DIR），与开发态 ~/.local/share/multi-agent 物理隔离。
 *
 * 「空跑能起」契约：本 setup 不依赖任何测试前置数据——即使空跑（无 .spec）
 * 也能把 mock + provider 立起来并健康检查通过，证明 20b/20c 的 LLM mock 基座就绪。
 */
import { spawn, type ChildProcess } from 'node:child_process'
import { createServer } from 'node:net'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const REPO = path.resolve(__dirname, '..')
const BACKEND_DIR = path.join(REPO, 'backend')

/** 抢一个 OS 分配的空闲端口（bind→close→return，与 im/mc e2e 同款 _free_port）。 */
function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createServer()
    srv.unref()
    srv.on('error', reject)
    srv.listen(0, '127.0.0.1', () => {
      const addr = srv.address()
      const port = typeof addr === 'object' && addr ? addr.port : 0
      srv.close(() => resolve(port))
    })
  })
}

/** 等 url 返 200，最多 timeoutMs。webServer 起慢 / mock 起慢时复用。 */
async function waitHealthy(url: string, timeoutMs = 15000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const resp = await fetch(url)
      if (resp.ok) return
    } catch {
      // 还没起，继续轮询
    }
    await new Promise((r) => setTimeout(r, 300))
  }
  throw new Error(`[e2e global-setup] ${url} 未在 ${timeoutMs}ms 内健康`)
}

let mockProc: ChildProcess | null = null

/** 删开发 DB 里历史 e2e 残留的 mock provider 行（端口已废，留着误导列表）。 */
async function cleanupStaleMockProviders(apiBase: string): Promise<void> {
  let providers: Array<{ id: string; name: string }> = []
  try {
    const r = await fetch(`${apiBase}/api/providers`)
    if (r.ok) providers = (await r.json()) as Array<{ id: string; name: string }>
  } catch {
    return // 后端没起或返回异常——非致命，跳过清理
  }
  for (const p of providers) {
    if (p.name === 'e2e-mock-llm') {
      try {
        await fetch(`${apiBase}/api/providers/${p.id}`, { method: 'DELETE' })
      } catch {
        // 删失败不阻断——最多留一条过期行，下次再清
      }
    }
  }
}

export default async function globalSetup(): Promise<void> {
  // ── 1. spawn mock LLM server ──────────────────────────────────────────
  const port = await freePort()
  process.env.E2E_MOCK_LLM_PORT = String(port)
  mockProc = spawn(
    process.execPath, // node
    [path.join(__dirname, 'run-mock-llm.mjs'), String(port)],
    {
      cwd: BACKEND_DIR,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: {
        ...process.env,
        E2E_MOCK_LLM_PORT: String(port),
        PYTHONPATH: BACKEND_DIR,
      },
    },
  )
  // 把 mock 的 stderr 透到 e2e 日志，便于排错（spawn 失败 / 端口占用等）。
  mockProc.stdout?.on('data', (d) => process.stderr.write(`[mock-llm] ${d}`))
  mockProc.stderr?.on('data', (d) => process.stderr.write(`[mock-llm] ${d}`))

  const mockBase = `http://127.0.0.1:${port}`
  await waitHealthy(`${mockBase}/health`, 15000)
  // 暴露给测试用（经 process.env，test 里读 process.env.E2E_MOCK_LLM_BASE）
  process.env.E2E_MOCK_LLM_BASE = mockBase

  // ── 2. 写 active provider 行（base_url 指向 mock）──────────────────────
  // webServer 起的 uvicorn 已在临时 DATA_DIR init_db（空库 + seed demo）。
  // 直接 POST 一个 is_active=true 的 provider：crud.create_provider 会
  // deactivate 其他 + _refresh_active_cache → get_config() 返回 mock base_url。
  const apiBase = process.env.E2E_API_BASE ?? 'http://127.0.0.1:8000'
  await waitHealthy(`${apiBase}/health`, 20000)

  // 清理历史 e2e 残留的 mock provider 行：reuseExistingServer=true 时若复用
  // 开发态 8000 server（开发 DB），上一轮 e2e 写的 e2e-mock-llm 行（端口已废）
  // 会留在表里。先 GET 全量，DELETE 所有 name=e2e-mock-llm 的旧行，再写新的，
  // 避免开发 DB 攒一堆过期 mock 行（端口号 ephemeral，留着无意义且误导 /api/providers
  // 列表）。globalTeardown 不删——active provider 保留到下一轮 e2e 复用更稳
  // （若 globalTeardown 删了，下一轮 globalSetup 会重新建，无副作用）。
  await cleanupStaleMockProviders(apiBase)

  const resp = await fetch(`${apiBase}/api/providers`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name: 'e2e-mock-llm',
      provider: 'openai',
      model: 'e2e-mock-model',
      base_url: `${mockBase}/v1`,
      api_key: 'sk-e2e-mock',
      temperature: 0.0,
      max_tokens: 4096,
      is_active: true,
    }),
  })
  if (!resp.ok) {
    const text = await resp.text().catch(() => '<no body>')
    throw new Error(
      `[e2e global-setup] POST /api/providers 失败 ${resp.status}: ${text}`,
    )
  }

  // ── 3. 校验 active cache 已切到 mock ──────────────────────────────────
  const cfgResp = await fetch(`${apiBase}/api/config`)
  if (!cfgResp.ok) throw new Error('[e2e global-setup] GET /api/config 失败')
  const cfg = (await cfgResp.json()) as { base_url?: string; model?: string }
  if (!cfg.base_url || !cfg.base_url.includes(`127.0.0.1:${port}`)) {
    throw new Error(
      `[e2e global-setup] active provider base_url 未切到 mock: ${cfg.base_url}`,
    )
  }
  // eslint-disable-next-line no-console
  console.log(
    `[e2e global-setup] mock LLM @ ${mockBase}/v1 已激活（model=${cfg.model}）`,
  )
}

// globalTeardown：kill mock LLM 子进程。
//
// 不删 active provider 行：reuseExistingServer=true 时若复用开发 server，删了
// 会让开发态 active provider 变成「无 active」（get_config 退 env 兜底）；下一轮
// e2e globalSetup 会先 cleanupStaleMockProviders + 重新 POST 覆盖，故无需在此删。
// 复用开发 server 的副作用：开发态的 active provider 被 e2e 切到 mock 后，e2e
// 跑完开发态若有人手动用会打 mock（mock 已 teardown）→ 报错。这是 reuseExistingServer
// 的已知权衡（CI 干净环境无此问题，因每次新起 server + 临时 DB）。
export async function globalTeardown(): Promise<void> {
  if (mockProc) {
    mockProc.kill('SIGTERM')
    // 给一点时间优雅退出，避免端口残留；不强等 kill -9。
    await new Promise((r) => setTimeout(r, 200))
    mockProc = null
  }
}

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

// seed 实体 id 前缀——cleanupStaleTestEntities 保留这些，删其他（e2e 测试残留）。
// 与 backend/store/seed.py 的硬编码 id 对齐：agent_coord_1 / agent_frontend_1 /
// agent_backend_1 / group_demo_1 / member_1..3 / task_demo_1 / msg_demo_1。
const SEED_IDS = new Set([
  'agent_coord_1',
  'agent_frontend_1',
  'agent_backend_1',
  'group_demo_1',
])

/**
 * 任务20b：清理上一轮 e2e 残留的测试 agent/group/conversation。
 *
 * .e2e-data 是持久 DB（不在 config 顶层 rmSync，避免上一轮 run 被中断留下僵尸
 * uvicorn 持有 data.db 文件句柄时 rmSync 删文件 → 新 uvicorn 报「unable to open
 * database file」的竞态）。持久 DB 的副作用：上一轮失败测试可能留下未删的测试实体
 * （如 e2e改名智能体），seed_demo_data 仅在表空时播种故 seed 跳过（不造成功能问题），
 * 但残留同名元素会让 getByText strict mode 命中多个。
 *
 * 保留 seed 实体（id 在 SEED_IDS 里 或 name 是 seed agent 名），删其他测试 agent/
 * group/conversation——让每轮 e2e 起点一致。删 group 前先清其 messages（外键）。
 * best-effort：任何 DELETE 失败不阻断 globalSetup（最坏退化为残留测试数据，测试
 * 可能 flaky 但不崩）。
 */
async function cleanupStaleTestEntities(apiBase: string): Promise<void> {
  // ── agents：删非 seed 的 ──
  try {
    const r = await fetch(`${apiBase}/api/agents`)
    if (r.ok) {
      const agents = (await r.json()) as Array<{ id: string; name: string }>
      for (const a of agents) {
        if (SEED_IDS.has(a.id)) continue
        // seed agent 名（协调者/前端工程师/后端工程师）也保留——防 id 体系变更。
        if (
          a.name === '协调者' ||
          a.name === '前端工程师' ||
          a.name === '后端工程师'
        )
          continue
        await fetch(`${apiBase}/api/agents/${a.id}`, { method: 'DELETE' }).catch(
          () => {},
        )
      }
    }
  } catch {
    /* 非致命 */
  }

  // ── groups：删非 seed 的（先清消息避免外键）──
  try {
    const r = await fetch(`${apiBase}/api/groups`)
    if (r.ok) {
      const groups = (await r.json()) as Array<{ id: string; name: string }>
      for (const g of groups) {
        if (SEED_IDS.has(g.id)) continue
        if (g.name === '演示协作组') continue
        // 清该群消息（DELETE /api/messages?conversationId=g.id）。
        await fetch(
          `${apiBase}/api/messages?conversationId=${encodeURIComponent(g.id)}`,
          { method: 'DELETE' },
        ).catch(() => {})
        await fetch(`${apiBase}/api/groups/${g.id}`, { method: 'DELETE' }).catch(
          () => {},
        )
      }
    }
  } catch {
    /* 非致命 */
  }

  // ── conversations（单聊会话，独立实体）：全删（seed 无单聊会话）──
  try {
    const r = await fetch(`${apiBase}/api/conversations`)
    if (r.ok) {
      const convs = (await r.json()) as Array<{ id: string }>
      for (const c of convs) {
        // 清消息（外键）。
        await fetch(
          `${apiBase}/api/messages?conversationId=${encodeURIComponent(c.id)}`,
          { method: 'DELETE' },
        ).catch(() => {})
        await fetch(`${apiBase}/api/conversations/${c.id}`, {
          method: 'DELETE',
        }).catch(() => {})
      }
    }
  } catch {
    /* 非致命 */
  }

  // ── memories（L2 长期记忆，独立实体）：全删（seed 无记忆）──
  // 记忆是测试数据（任务20c 流程9 建固定 content 的记忆），上一轮失败测试可能留下
  // 同 content 记忆 → 本轮新建第二条 → 删一条后仍剩一条 → toHaveCount(0) 失败。
  // seed 无记忆，全删安全。best-effort。
  try {
    const r = await fetch(`${apiBase}/api/memory`)
    if (r.ok) {
      const mems = (await r.json()) as Array<{ id: string }>
      for (const m of mems) {
        await fetch(`${apiBase}/api/memory/${m.id}`, { method: 'DELETE' }).catch(
          () => {},
        )
      }
    }
  } catch {
    /* 非致命 */
  }

  // ── scheduled-tasks（定时任务，独立实体）：全删（seed 无定时任务）──
  // 流程10 建固定 name 的定时任务，上一轮失败测试残留同名任务 → 本轮新建第二条 →
  // 卡片 filter hasText 命中多个 → strict mode / toHaveCount 失败。seed 无定时任务，
  // 全删安全（后端 DELETE 先 remove_job 取消调度再删库，无副作用）。best-effort。
  try {
    const r = await fetch(`${apiBase}/api/scheduled-tasks`)
    if (r.ok) {
      const tasks = (await r.json()) as Array<{ id: string }>
      for (const t of tasks) {
        await fetch(`${apiBase}/api/scheduled-tasks/${t.id}`, {
          method: 'DELETE',
        }).catch(() => {})
      }
    }
  } catch {
    /* 非致命 */
  }

  // ── im-channels（IM 渠道，独立实体）：全删（seed 无 IM 渠道）──
  // 流程11 建固定 name 的渠道，同理残留致 strict mode 多命中。seed 无渠道，全删安全。
  try {
    const r = await fetch(`${apiBase}/api/im-channels`)
    if (r.ok) {
      const chs = (await r.json()) as Array<{ id: string }>
      for (const c of chs) {
        await fetch(`${apiBase}/api/im-channels/${c.id}`, { method: 'DELETE' }).catch(
          () => {},
        )
      }
    }
  } catch {
    /* 非致命 */
  }

  // ── mcp connections（MCP 连接，独立实体）：全删（seed 无 MCP 连接）──
  // 流程8 建固定 name 的 echo MCP 连接，残留致 strict mode 多命中。seed 无 MCP 连接，
  // 全删安全（后端级联从 agent.mounted_mcp 移除引用）。best-effort。
  try {
    const r = await fetch(`${apiBase}/api/mcp`)
    if (r.ok) {
      const conns = (await r.json()) as Array<{ id: string }>
      for (const c of conns) {
        await fetch(`${apiBase}/api/mcp/${c.id}`, { method: 'DELETE' }).catch(
          () => {},
        )
      }
    }
  } catch {
    /* 非致命 */
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

  // 任务20b：清理上一轮 e2e 残留的测试 agent/group/conversation（如 e2e改名智能体）。
  // .e2e-data 是持久 DB（不在 config 顶层 rmSync，避免文件句柄竞态），故上一轮
  // 失败测试可能留下未删的测试实体；seed_demo_data 仅在表空时播种，若表非空 seed 跳过
  // 不会造成功能问题，但残留的「e2e测试智能体」「e2e改名智能体」等会让 getByText
  // strict mode 命中多个同名元素。本清理保留 seed 实体（协调者/前端工程师/后端工程师
  // /演示协作组 + 其 members/task/message），删其他——让每轮 e2e 起点一致。
  await cleanupStaleTestEntities(apiBase)

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

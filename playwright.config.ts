/**
 * Playwright config (任务20a) — e2e global setup + webServer。
 *
 * webServer（任务20a 核心）：起两个 server——
 *  1. FastAPI 后端（``uvicorn main:app``）—— cwd=backend，env 注入临时 DATA_DIR
 *     （与开发态 ``~/.local/share/multi-agent`` 物理隔离，e2e 不污染开发数据）+
 *     ``reuseExistingServer`` 让本地已起的开发态 8000 端口 server 被复用（开发态
 *     打开时省一次冷启动）。
 *  2. Vite 前端 dev server（``--port 5174``，与开发态 5173 错开）—— env 注入
 *     ``VITE_API_BASE`` 让前端打到 e2e 后端（默认仍是 localhost:8000，本任务后端
 *     就用 8000，故 VITE_API_BASE 其实可不设；但显式写出契约更清晰，且 20b/20c
 *     若把后端换端口时只改这一处）。
 *
 * globalSetup：spawn mock LLM server + 写 active provider 行指向 mock
 * （见 ``global-setup.ts``）。在 webServer 就绪后跑（Playwright 先起 webServer
 * 再跑 globalSetup）。
 *
 * 空跑契约：``npm run e2e`` 不带任何 .spec 也能起（``passWithNoTests`` 等价的
 * Playwright 行为：无 test 时 globalSetup 仍跑，证明 mock + provider 基座就绪）。
 */
import { defineConfig, devices } from '@playwright/test'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const REPO = __dirname
const BACKEND_DIR = path.join(REPO, 'backend')

// 任务20b：e2e 后端用独立端口 8766（与开发态 8000 错开）+ reuseExistingServer:false
// 强制每次新起 uvicorn + 临时 DB（.e2e-data）——不复用开发态 server。
// 复用开发 server 的致命问题：开发 DB schema 与代码漂移（如 task11a 删了
// agents.allowed_tools/denied_tools 列但 SQLite 不自动迁移），开发 DB 仍带 NOT NULL
// 旧列 → e2e 写入（建 agent）触发 NOT NULL constraint 500。只有 fresh DB 的
// Base.metadata.create_all 才生成正确 schema。读类测试（20a smoke）复用 dev server
// 能过，但 20b 起的写类流程必须 fresh DB。
//
// .e2e-data 持久化（不在 config 里 rmSync）：首跑 init_db 建 fresh DB（正确 schema
// + seed demo），后续 run 复用（seed 跳过，agents 已在）。**不在 config 顶层 rmSync**
// ——曾用 fs.rmSync 每轮抹 .e2e-data，但若上一轮 run 被中断（timeout/kill）留下僵尸
// uvicorn 持有 data.db 文件句柄，rmSync 删文件后新 uvicorn 起来查 DB 报
// 「unable to open database file」（Linux 文件被 unlink 但句柄未释放）。改为 globalSetup
// 里走 API 清理（DELETE 非 seed 的 agents/groups/conversations）——无文件竞态，且能
// 清掉上一轮失败测试残留的测试数据，避免 strict mode 多命中。

// E2E_API_BASE：globalSetup 与 spec 读这个 env 打后端（避免硬编码 8766）。
// 在 config 顶层设 process.env，globalSetup（同进程）与各 spec（worker 继承）都能读。
const E2E_BACKEND = 'http://127.0.0.1:8766'
process.env.E2E_API_BASE = E2E_BACKEND

export default defineConfig({
  testDir: './e2e/specs',
  testMatch: /.*\.spec\.ts/,

  // globalSetup 在 webServer 起来后跑——spawn mock LLM + 写 active provider。
  // 路径相对本配置文件（playwright.config.ts 在 repo root）→ e2e/ 下。
  globalSetup: './e2e/global-setup.ts',

  // 超时：e2e 含 LLM mock（秒回）+ 真链路 turn，单测 60s 足够；首个测给 90s
  // （engine 冷启动 + 首轮 LLM 调用）。
  timeout: 60_000,
  expect: { timeout: 10_000 },

  // 失败重跑一次（LLM/WS 偶发抖动）。
  retries: process.env.CI ? 2 : 0,
  workers: 1, // 串行：e2e 共享一个后端 + 临时 DB，并发会串状态。

  reporter: [['list']],

  use: {
    baseURL: 'http://127.0.0.1:5174',
    // headless：CI 无头；本地也默认无头（开发机可能无显示）。需要看 UI 时
    // ``PWDEBUG=1 npx playwright test`` 调试。
    headless: true,
    actionTimeout: 15_000,
    navigationTimeout: 15_000,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  webServer: [
    {
      // ── 后端 FastAPI（独立 8766，不复用开发态 8000）──
      // reuseExistingServer:false：每次新起 → MULTI_AGENT_DATA_DIR env 生效 →
      // fresh .e2e-data DB（Base.metadata.create_all 生成正确 schema，无开发
      // DB 的 stale NOT NULL 列）。CI 与本地一致行为（本地开发态 8000 server
      // 仍独立跑，互不干扰）。
      command:
        'python3 -m uvicorn main:app --host 127.0.0.1 --port 8766',
      cwd: BACKEND_DIR,
      url: `${E2E_BACKEND}/health`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        // 临时 DATA_DIR：与开发态 ~/.local/share/multi-agent 物理隔离。
        MULTI_AGENT_DATA_DIR: path.join(REPO, '.e2e-data'),
      },
    },
    {
      // ── 前端 Vite dev server（5174，与开发态 5173 错开）──
      // strictPort 让端口被占时直接报错而非换端口（e2e 要确定性）。
      // cwd=REPO，用 ``node <repo>/node_modules/vite/bin/vite.js`` 显式绝对路径
      // （cwd 下无 node_modules 时 node 解析不到 vite——本仓 node_modules 在 REPO
      // 根，命令里写绝对路径最稳，不依赖 cwd 的 node_modules 解析）。
      command: `node ${path.join(REPO, 'node_modules', 'vite', 'bin', 'vite.js')} --port 5174 --strictPort`,
      cwd: REPO,
      url: 'http://127.0.0.1:5174',
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      env: {
        // 前端打到 e2e 后端（8766）。
        VITE_API_BASE: E2E_BACKEND,
      },
    },
  ],
})

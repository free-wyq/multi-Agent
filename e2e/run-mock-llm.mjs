// Playwright global-setup 用的 mock LLM 启动器（任务20a）。
//
// 单独的 .mjs 而非直接 spawn ``python3``：让 global-setup.ts 用 ``node`` spawn
// 本文件（node 跨平台路径稳定），本文件再 spawn python 跑 mock server。
//
// 直接跑 ``backend/tests/playwright_mock_llm_server.py`` 而非 ``python3 -m``：
// ``tests/`` 无 ``__init__.py``（非 package），``-m playwright_mock_llm_server``
// 解析不到模块。直接 .py 运行时文件是自包含的（仅 ``from aiohttp import web``
// 一个外部 import，无内部 package 依赖），故直接路径最稳。
import { spawn } from 'node:child_process'
import path from 'node:path'

const port = process.argv[2]
if (!port) {
  console.error('[run-mock-llm] missing port arg')
  process.exit(2)
}

const script = path.join(process.cwd(), 'tests', 'playwright_mock_llm_server.py')
const child = spawn('python3', [script], {
  cwd: process.cwd(),
  stdio: ['ignore', 'pipe', 'pipe'],
  env: {
    ...process.env,
    E2E_MOCK_LLM_PORT: port,
  },
})

// 透传子进程 stdout/stderr → 让 global-setup 捕获的 mockProc.stdout.on 能拿到。
child.stdout.on('data', (d) => process.stdout.write(d))
child.stderr.on('data', (d) => process.stderr.write(d))

// 子进程退出 → 本启动器也退出（同码），让 global-setup 的 mockProc 退出事件触发。
child.on('exit', (code) => {
  process.exit(code ?? 0)
})

// 收到 SIGTERM/SIGINT → 转发给 python 子进程（优雅关 aiohttp web.run_app）。
for (const sig of ['SIGTERM', 'SIGINT']) {
  process.on(sig, () => {
    try {
      child.kill(sig)
    } catch {
      // ignore
    }
  })
}

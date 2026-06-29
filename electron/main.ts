import { app, BrowserWindow } from 'electron'
import * as path from 'path'
import * as fs from 'fs'

// ═══════════════════════════════════════════════════════════════
// 跨平台数据目录初始化（必须在所有业务模块加载之前）
// Windows: C:\Users\xxx\AppData\Roaming\multi-agent\
// macOS:   ~/Library/Application Support/multi-agent/
// Linux:   ~/.config/multi-agent/
// ═══════════════════════════════════════════════════════════════
const DATA_DIR = app.getPath('userData')
process.env.MULTI_AGENT_DATA_DIR = DATA_DIR

// 预创建标准子目录
for (const sub of ['config', 'groups', 'logs']) {
  fs.mkdirSync(path.join(DATA_DIR, sub), { recursive: true })
}

// 确保最先加载 .env（env 文件中未定义的 key 会被设置进 process.env）
import './load-env'

// ═══════════════════════════════════════════════════════════════
// WSL2 / Linux 中文输入法修复
// ═══════════════════════════════════════════════════════════════
if (process.platform === 'linux') {
  // 1. 补齐中文输入法环境变量（优先读取 shell 已有配置，否则 fallback）
  if (!process.env.GTK_IM_MODULE) {
    process.env.GTK_IM_MODULE = 'ibus'
  }
  if (!process.env.QT_IM_MODULE) {
    process.env.QT_IM_MODULE = 'ibus'
  }
  if (!process.env.XMODIFIERS) {
    process.env.XMODIFIERS = '@im=ibus'
  }

  // 2. 强制 Chromium 使用 X11 后端（WSLg Wayland IME 支持极差）
  app.commandLine.appendSwitch('ozone-platform', 'x11')
  app.commandLine.appendSwitch('enable-features', 'UseOzonePlatform')

  // 3. 禁用 GPU 加速，避免 d3d12 驱动崩溃导致白屏
  app.disableHardwareAcceleration()
}

let mainWindow: BrowserWindow | null = null

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1024,
    minHeight: 680,
    title: 'Multi-Agent 协作平台',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  })

  // 开发模式加载 Vite dev server，生产模式加载构建产物
  if (process.env.VITE_DEV_SERVER_URL) {
    mainWindow.loadURL(process.env.VITE_DEV_SERVER_URL)
    mainWindow.webContents.openDevTools()
  } else {
    mainWindow.loadFile(path.join(__dirname, '../dist/index.html'))
  }

  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

app.whenReady().then(async () => {
  // 1. 加载持久化数据
  const { initPersistence } = await import('../main/store/persistence')
  await initPersistence()

  // 2. 加载共享状态中心队列
  const { sharedState } = await import('../main/store/shared-state')
  await sharedState.loadAll()

  // 3. 初始化事件总线
  const { eventBus } = await import('../main/bus/event-bus')
  eventBus.initialize()

  // 4. 初始化 Store
  const { store } = await import('../main/store/store')
  await store.loadFromPersistence()

  // 5. 注册所有 IPC handlers
  const { registerAllHandlers } = await import('../main/ipc-handlers')
  registerAllHandlers()

  // 6. 启动 AgentEngine 注册表（含 coordinator）
  const { agentRegistry } = await import('../main/agent-engine/registry')
  await agentRegistry.loadFromStore()

  // 7. 创建窗口
  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow()
    }
  })
})

app.on('window-all-closed', async () => {
  // 关闭所有 AgentEngine
  const { agentRegistry } = await import('../main/agent-engine/registry')
  await agentRegistry.shutdownAll()

  // 最终刷盘
  const { flushPersistence } = await import('../main/store/persistence')
  await flushPersistence()

  if (process.platform !== 'darwin') {
    app.quit()
  }
})

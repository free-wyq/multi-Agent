import { app, BrowserWindow } from 'electron'
import * as path from 'path'

// WSL2/Linux 环境禁用 GPU 加速，避免 d3d12 驱动崩溃
if (process.platform === 'linux') {
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

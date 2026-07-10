import { app, BrowserWindow } from 'electron'
import { spawn, type ChildProcess } from 'child_process'
import * as http from 'http'
import * as path from 'path'
import * as fs from 'fs'

// ═══════════════════════════════════════════════════════════════
// 跨平台数据目录（Electron 托管 Python，统一传给后端）
// Windows: C:\Users\xxx\AppData\Roaming\multi-agent\
// macOS:   ~/Library/Application Support/multi-agent/
// Linux:   ~/.config/multi-agent/
// ═══════════════════════════════════════════════════════════════
const DATA_DIR = app.getPath('userData')
process.env.MULTI_AGENT_DATA_DIR = DATA_DIR
fs.mkdirSync(path.join(DATA_DIR, 'logs'), { recursive: true })

// ═══════════════════════════════════════════════════════════════
// WSL2 / Linux 中文输入法修复 + 白屏规避
// ═══════════════════════════════════════════════════════════════
if (process.platform === 'linux') {
  if (!process.env.GTK_IM_MODULE) process.env.GTK_IM_MODULE = 'ibus'
  if (!process.env.QT_IM_MODULE) process.env.QT_IM_MODULE = 'ibus'
  if (!process.env.XMODIFIERS) process.env.XMODIFIERS = '@im=ibus'
  app.commandLine.appendSwitch('ozone-platform', 'x11')
  app.commandLine.appendSwitch('enable-features', 'UseOzonePlatform')
  app.disableHardwareAcceleration()
}

let pythonProcess: ChildProcess | null = null
let mainWindow: BrowserWindow | null = null

function isDev(): boolean {
  return !!process.env.VITE_DEV_SERVER_URL || !app.isPackaged
}

function pythonCommand(): string {
  if (process.platform === 'win32') return 'python'
  return 'python3'
}

function packagedServerName(): string {
  return process.platform === 'win32' ? 'multi-agent-server.exe' : 'multi-agent-server'
}

function startPythonServer(): ChildProcess {
  const dev = isDev()

  if (dev) {
    const cmd = pythonCommand()
    const args = ['-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', '8000']
    const cwd = path.join(__dirname, '..', 'backend')
    const proc = spawn(cmd, args, {
      cwd,
      env: { ...process.env, PYTHONUNBUFFERED: '1' },
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    pipeLogs(proc)
    return proc
  }

  // 生产：spawn PyInstaller 打包的可执行
  const serverPath = path.join(process.resourcesPath, packagedServerName())
  const proc = spawn(serverPath, ['--host', '127.0.0.1', '--port', '8000'], {
    cwd: path.dirname(serverPath),
    env: { ...process.env, PYTHONUNBUFFERED: '1' },
    stdio: ['pipe', 'pipe', 'pipe'],
  })
  pipeLogs(proc)
  return proc
}

function pipeLogs(proc: ChildProcess) {
  proc.stdout?.on('data', (d) => console.log(`[python] ${d.toString().trimEnd()}`))
  proc.stderr?.on('data', (d) => console.error(`[python:err] ${d.toString().trimEnd()}`))
  proc.on('exit', (code) => {
    console.log(`[python] exited with code ${code}`)
    pythonProcess = null
  })
}

function waitForPythonReady(maxRetries = 30, interval = 500): Promise<void> {
  return new Promise((resolve, reject) => {
    let tries = 0
    const check = () => {
      const req = http.get('http://127.0.0.1:8000/health', (res) => {
        if (res.statusCode === 200) {
          resolve()
        } else {
          retry()
        }
        res.resume()
      })
      req.on('error', retry)
      req.setTimeout(1000, () => {
        req.destroy()
        retry()
      })
    }
    const retry = () => {
      tries += 1
      if (tries >= maxRetries) {
        reject(new Error('Python server failed to start within timeout'))
        return
      }
      setTimeout(check, interval)
    }
    check()
  })
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1024,
    minHeight: 680,
    title: 'Multi-Agent 协作平台',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  })

  if (process.env.VITE_DEV_SERVER_URL) {
    mainWindow.loadURL(process.env.VITE_DEV_SERVER_URL)
    mainWindow.webContents.openDevTools()
  } else {
    mainWindow.loadFile(path.join(__dirname, '..', 'dist', 'index.html'))
  }

  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

async function killPython() {
  if (!pythonProcess) return
  const proc = pythonProcess
  pythonProcess = null
  try {
    proc.kill('SIGTERM')
  } catch {
    /* ignore */
  }
  await new Promise((r) => setTimeout(r, 500))
  try {
    if (!proc.killed) proc.kill('SIGKILL')
  } catch {
    /* ignore */
  }
}

app.whenReady().then(async () => {
  pythonProcess = startPythonServer()
  try {
    await waitForPythonReady()
  } catch (e) {
    console.error(e)
  }
  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', async () => {
  await killPython()
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', async () => {
  await killPython()
})

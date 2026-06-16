/**
 * Settings IPC Handlers
 */

import { ipcMain } from 'electron'
import * as path from 'path'
import * as fs from 'fs'
import { app } from 'electron'
import { SETTINGS_GET, SETTINGS_SAVE } from '../../src/ipc/channels'
import type { AppSettings } from '../store/types'

const SETTINGS_FILE = path.join(app.getPath('userData'), 'settings.json')

function readSettings(): AppSettings {
  try {
    if (fs.existsSync(SETTINGS_FILE)) {
      return JSON.parse(fs.readFileSync(SETTINGS_FILE, 'utf-8'))
    }
  } catch {
    // 文件损坏，返回默认
  }
  return {
    llm: {
      apiKey: process.env.OPENAI_API_KEY || process.env.ANTHROPIC_API_KEY || '',
      baseUrl: process.env.OPENAI_BASE_URL || 'https://api.openai.com/v1',
      model: process.env.LLM_MODEL || 'glm-5.1',
      temperature: 0,
      maxTokens: 4096,
    },
  }
}

function writeSettings(settings: AppSettings): void {
  fs.writeFileSync(SETTINGS_FILE, JSON.stringify(settings, null, 2), 'utf-8')
}

export function registerSettingsHandlers(): void {
  ipcMain.handle(SETTINGS_GET, () => {
    return readSettings()
  })

  ipcMain.handle(SETTINGS_SAVE, (_event, settings: AppSettings) => {
    writeSettings(settings)
  })
}

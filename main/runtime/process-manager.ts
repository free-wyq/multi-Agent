/**
 * 进程管理 + Claude Code CLI 跨平台检测
 *
 * 替代 Docker 容器管理：
 * - 检测 Claude Code CLI 路径
 * - 跨平台支持（macOS / Windows / Linux）
 */

import { execSync } from 'child_process'
import * as path from 'path'
import * as fs from 'fs'
import * as os from 'os'

let _claudeCodePath: string | null = null

/**
 * 检测 Claude Code CLI 路径
 * 优先级：环境变量 > 平台默认路径 > which/where
 */
export function findClaudeCode(): string {
  if (_claudeCodePath) return _claudeCodePath

  // 1. 环境变量
  const envPath = process.env.CLAUDE_CODE_PATH
  if (envPath && fs.existsSync(envPath)) {
    _claudeCodePath = envPath
    return _claudeCodePath
  }

  // 2. PATH 中查找
  try {
    const command = os.platform() === 'win32' ? 'where claude' : 'which claude'
    const found = execSync(command, { encoding: 'utf-8' }).trim().split('\n')[0]
    if (found && fs.existsSync(found)) {
      _claudeCodePath = found
      return _claudeCodePath
    }
  } catch {
    // not found in PATH
  }

  // 3. 平台默认路径
  const platform = os.platform()
  const homeDir = os.homedir()
  const defaultPaths: string[] = []

  if (platform === 'darwin') {
    defaultPaths.push(
      '/usr/local/bin/claude',
      path.join(homeDir, '.claude', 'bin', 'claude'),
    )
  } else if (platform === 'linux') {
    defaultPaths.push(
      '/usr/local/bin/claude',
      '/usr/bin/claude',
      path.join(homeDir, '.local', 'bin', 'claude'),
    )
  } else if (platform === 'win32') {
    defaultPaths.push(
      path.join(process.env.LOCALAPPDATA || '', 'claude', 'claude.exe'),
      path.join(homeDir, 'AppData', 'Local', 'claude', 'claude.exe'),
    )
  }

  for (const p of defaultPaths) {
    if (fs.existsSync(p)) {
      _claudeCodePath = p
      return _claudeCodePath
    }
  }

  throw new Error(
    'Claude Code CLI not found. Please install it or set CLAUDE_CODE_PATH environment variable.',
  )
}

/**
 * 设置 Claude Code CLI 路径
 */
export function setClaudeCodePath(p: string): void {
  _claudeCodePath = p
}

/**
 * 获取当前工作目录（跨平台）
 */
export function getGroupWorkDir(groupId: string): string {
  return path.join(process.cwd(), 'data', 'group_files', groupId)
}

/**
 * Claude Code Runtime
 *
 * 替代 Docker 容器内 Claude Code：
 * - spawn 本地 Claude Code CLI 进程
 * - 实时捕获 stdout/stderr 发布为日志
 * - 进程退出后更新任务状态
 */

import { spawn, ChildProcess } from 'child_process'
import * as path from 'path'
import * as fs from 'fs'
import { findClaudeCode, getGroupWorkDir } from './process-manager'
import { generateClaudeMd, generateSettingsJson } from './config-generator'
import { store } from '../store/store'
import { eventBus, CHANNEL_PREFIX } from '../bus/event-bus'
import type { AgentDefinition } from '../store/types'

export interface AgentResult {
  success: boolean
  exitCode: number | null
  output: string
}

export class ClaudeCodeRuntime {
  private groupId: string
  private agentDef: AgentDefinition
  private process: ChildProcess | null = null
  private workDir: string

  constructor(groupId: string, agentDef: AgentDefinition) {
    this.groupId = groupId
    this.agentDef = agentDef
    this.workDir = getGroupWorkDir(groupId)
  }

  /**
   * 启动：确保工作目录存在，生成配置文件
   */
  async start(): Promise<void> {
    // 确保工作目录
    if (!fs.existsSync(this.workDir)) {
      fs.mkdirSync(this.workDir, { recursive: true })
    }

    // 生成 CLAUDE.md
    const claudeMd = generateClaudeMd(
      this.agentDef.name,
      this.agentDef.role,
      this.agentDef.extra_skills,
      this.agentDef.system_prompt,
    )
    fs.writeFileSync(path.join(this.workDir, 'CLAUDE.md'), claudeMd, 'utf-8')

    // 生成 settings.json
    const settingsJson = generateSettingsJson(
      this.agentDef.name,
      this.agentDef.role,
      this.agentDef.allowed_tools,
      this.agentDef.denied_tools,
    )
    fs.writeFileSync(path.join(this.workDir, 'settings.json'), settingsJson, 'utf-8')

    // 确保子目录
    for (const dir of ['shared', 'output', '.agenticx/tasks', '.agenticx/results']) {
      const fullDir = path.join(this.workDir, dir)
      if (!fs.existsSync(fullDir)) {
        fs.mkdirSync(fullDir, { recursive: true })
      }
    }
  }

  /**
   * 执行任务：spawn Claude Code CLI 进程
   */
  async execute(taskContent: string, taskId: string): Promise<AgentResult> {
    await this.start() // 确保环境就绪

    const claudePath = findClaudeCode()
    let output = ''

    return new Promise<AgentResult>((resolve) => {
      // 写入任务文件
      const taskFile = path.join(this.workDir, '.agenticx', 'tasks', `${taskId}.json`)
      fs.writeFileSync(taskFile, JSON.stringify({
        task_id: taskId,
        content: taskContent,
        agent: this.agentDef.name,
        role: this.agentDef.role,
      }, null, 2), 'utf-8')

      // spawn Claude Code CLI
      const isWin = process.platform === 'win32'
      this.process = spawn(claudePath, [
        '--print',
        taskContent,
      ], {
        cwd: this.workDir,
        env: {
          ...process.env,
          CLAUDE_MD: 'CLAUDE.md',
        },
        stdio: ['pipe', 'pipe', 'pipe'],
        shell: isWin,
      })

      // 捕获 stdout
      this.process.stdout?.on('data', (data: Buffer) => {
        const text = data.toString()
        output += text

        // 逐行发布日志
        for (const line of text.split('\n')) {
          if (line.trim()) {
            this._publishLog(taskId, line)
          }
        }
      })

      // 捕获 stderr
      this.process.stderr?.on('data', (data: Buffer) => {
        const text = data.toString()
        output += text

        for (const line of text.split('\n')) {
          if (line.trim()) {
            this._publishLog(taskId, `[stderr] ${line}`)
          }
        }
      })

      // 进程退出
      this.process.on('close', (code) => {
        this.process = null
        resolve({
          success: code === 0,
          exitCode: code,
          output: output.trim(),
        })
      })

      // 进程错误
      this.process.on('error', (err) => {
        this.process = null
        resolve({
          success: false,
          exitCode: -1,
          output: `Process error: ${err.message}`,
        })
      })
    })
  }

  /**
   * 停止进程
   */
  stop(): void {
    if (this.process && !this.process.killed) {
      this.process.kill()
      this.process = null
    }
  }

  /**
   * 发布日志到事件总线
   */
  private _publishLog(taskId: string, line: string): void {
    try {
      const channel = `${CHANNEL_PREFIX}${this.groupId}`
      eventBus.publish(channel, {
        id: crypto.randomUUID(),
        group_id: this.groupId,
        task_id: taskId,
        sender_id: this.agentDef.id,
        receiver_id: 'broadcast',
        type: 'task_log',
        content: line,
        timestamp: new Date().toISOString(),
      })
    } catch {
      // 日志发布失败不影响主流程
    }
  }
}

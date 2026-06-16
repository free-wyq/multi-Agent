/**
 * Runtime IPC Handlers
 */

import { ipcMain } from 'electron'
import { store } from '../store/store'
import { ClaudeCodeRuntime } from '../runtime/claude-code-runtime'
import { RUNTIME_START, RUNTIME_STOP, RUNTIME_EXECUTE, RUNTIME_GET_LOGS } from '../../src/ipc/channels'

// 活跃的运行时实例
const activeRuntimes = new Map<string, ClaudeCodeRuntime>()

export function registerRuntimeHandlers(): void {
  ipcMain.handle(RUNTIME_START, (_event, agentId: string, groupId: string) => {
    const agentDef = store.getAgent(agentId)
    if (!agentDef) throw new Error(`Agent ${agentId} not found`)

    const instance = store.createInstance(agentId, groupId)
    const runtime = new ClaudeCodeRuntime(groupId, agentDef)
    activeRuntimes.set(instance.id, runtime)

    store.updateInstance(instance.id, { status: 'running' })
    return instance
  })

  ipcMain.handle(RUNTIME_STOP, (_event, instanceId: string) => {
    const runtime = activeRuntimes.get(instanceId)
    if (runtime) {
      runtime.stop()
      activeRuntimes.delete(instanceId)
    }
    store.updateInstance(instanceId, { status: 'stopped' })
  })

  ipcMain.handle(RUNTIME_EXECUTE, async (_event, instanceId: string, taskId: string) => {
    const runtime = activeRuntimes.get(instanceId)
    if (!runtime) throw new Error(`Runtime ${instanceId} not found`)

    const task = store.getTask(taskId)
    if (!task) throw new Error(`Task ${taskId} not found`)

    store.updateTask(taskId, { status: 'working' })
    store.updateInstance(instanceId, { status: 'running', current_task_id: taskId })

    const result = await runtime.execute(task.description || task.title, taskId)

    // 更新任务状态
    store.updateTask(taskId, {
      status: result.success ? 'completed' : 'failed',
      exit_code: result.exitCode ?? undefined,
      result_summary: result.output?.substring(0, 500),
    })

    store.updateInstance(instanceId, { status: 'idle', current_task_id: undefined })
    return result
  })

  ipcMain.handle(RUNTIME_GET_LOGS, (_event, instanceId: string) => {
    // 返回空日志（日志通过事件总线实时推送）
    return ''
  })
}

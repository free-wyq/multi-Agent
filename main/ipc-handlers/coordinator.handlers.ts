/**
 * Coordinator IPC Handlers
 */

import { ipcMain } from 'electron'
import { store } from '../store/store'
import { coordinatorWorkflow } from '../coordinator/workflow'
import { COORDINATOR_SUBMIT, COORDINATOR_GET_DAG, COORDINATOR_GET_STATUS } from '../../src/ipc/channels'

export function registerCoordinatorHandlers(): void {
  ipcMain.handle(COORDINATOR_SUBMIT, async (_event, groupId: string, requirement: string) => {
    // 异步执行工作流，立即返回
    coordinatorWorkflow.run(groupId, requirement).catch(err => {
      console.error('Coordinator workflow error:', err)
    })
    return { status: 'started', group_id: groupId }
  })

  ipcMain.handle(COORDINATOR_GET_DAG, (_event, groupId: string) => {
    const tasks = store.listTasksByGroup(groupId)
    const nodes = tasks.map(t => ({
      id: t.id,
      label: t.title,
      agent_id: t.assigned_agent_id,
      status: t.status,
      dag_order: t.dag_order,
    }))

    const edges: { source: string; target: string }[] = []
    for (const t of tasks) {
      for (const depId of t.dependencies) {
        edges.push({ source: depId, target: t.id })
      }
    }

    return { nodes, edges }
  })

  ipcMain.handle(COORDINATOR_GET_STATUS, (_event, groupId: string) => {
    const tasks = store.listTasksByGroup(groupId)
    const statusCounts: Record<string, number> = {}
    for (const t of tasks) {
      statusCounts[t.status] = (statusCounts[t.status] || 0) + 1
    }
    return {
      group_id: groupId,
      total_tasks: tasks.length,
      status_counts: statusCounts,
    }
  })
}

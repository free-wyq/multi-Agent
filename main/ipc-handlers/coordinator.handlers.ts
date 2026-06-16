/**
 * Coordinator IPC Handlers（A2A 架构改造后）
 *
 * COORDINATOR_SUBMIT 不再直接调用 coordinatorWorkflow.run()，
 * 而是通过 sharedState.pushTask() 扔字条给 coordinator 引擎的收件箱，
 * 由 coordinator engine 主动消费后执行工作流。
 */

import { ipcMain } from 'electron'
import { store } from '../store/store'
import { sharedState } from '../store/shared-state'
import { COORDINATOR_SUBMIT, COORDINATOR_GET_DAG, COORDINATOR_GET_STATUS } from '../../src/ipc/channels'

export function registerCoordinatorHandlers(): void {
  ipcMain.handle(COORDINATOR_SUBMIT, async (_event, groupId: string, requirement: string) => {
    const group = store.getGroup(groupId)
    if (!group || !group.coordinator_id) {
      throw new Error(`Group ${groupId} has no coordinator`)
    }

    // A2A：扔字条给 coordinator 引擎的收件箱
    sharedState.pushTask({
      group_id: groupId,
      sender_id: 'user',
      receiver_id: group.coordinator_id,
      content: requirement,
      data: { type: 'coordinator_submit' },
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

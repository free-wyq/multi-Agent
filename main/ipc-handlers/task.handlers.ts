/**
 * Task IPC Handlers
 */

import { ipcMain } from 'electron'
import { store } from '../store/store'
import {
  TASK_LIST, TASK_GET, TASK_CREATE, TASK_UPDATE, TASK_DELETE, TASK_READY,
} from '../../src/ipc/channels'

import type { TaskCreatePayload } from '../store/types'

export function registerTaskHandlers(): void {
  ipcMain.handle(TASK_LIST, (_event, groupId?: string) => {
    if (groupId) return store.listTasksByGroup(groupId)
    return Array.from({ length: 0 }) // 不传 groupId 暂返回空
  })

  ipcMain.handle(TASK_GET, (_event, id: string) => {
    return store.getTask(id) || null
  })

  ipcMain.handle(TASK_CREATE, (_event, data: TaskCreatePayload) => {
    return store.createTask(data)
  })

  ipcMain.handle(TASK_UPDATE, (_event, id: string, data: Partial<TaskCreatePayload>) => {
    return store.updateTask(id, data)
  })

  ipcMain.handle(TASK_DELETE, (_event, id: string) => {
    store.deleteTask(id)
  })

  ipcMain.handle(TASK_READY, (_event, groupId: string) => {
    return store.getReadyTasks(groupId)
  })
}

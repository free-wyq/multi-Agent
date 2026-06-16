/**
 * Agent IPC Handlers
 */

import { ipcMain } from 'electron'
import { store } from '../store/store'
import { AGENT_LIST, AGENT_GET, AGENT_CREATE, AGENT_UPDATE, AGENT_DELETE } from '../../src/ipc/channels'

export function registerAgentHandlers(): void {
  ipcMain.handle(AGENT_LIST, () => {
    return store.listAgents()
  })

  ipcMain.handle(AGENT_GET, (_event, id: string) => {
    return store.getAgent(id) || null
  })

  ipcMain.handle(AGENT_CREATE, (_event, data: unknown) => {
    return store.createAgent(data as never)
  })

  ipcMain.handle(AGENT_UPDATE, (_event, id: string, data: unknown) => {
    return store.updateAgent(id, data as never)
  })

  ipcMain.handle(AGENT_DELETE, (_event, id: string) => {
    store.deleteAgent(id)
  })
}

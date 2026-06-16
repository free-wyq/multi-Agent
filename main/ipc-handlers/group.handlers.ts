/**
 * Group IPC Handlers
 */

import { ipcMain } from 'electron'
import { store } from '../store/store'
import { agentRegistry } from '../agent-engine/registry'
import {
  GROUP_LIST, GROUP_GET, GROUP_CREATE, GROUP_UPDATE, GROUP_DELETE,
  GROUP_LIST_MEMBERS, GROUP_ADD_MEMBER, GROUP_REMOVE_MEMBER,
  GROUP_LIST_FILES,
} from '../../src/ipc/channels'

import type { GroupCreatePayload } from '../store/types'

export function registerGroupHandlers(): void {
  ipcMain.handle(GROUP_LIST, () => {
    return store.listGroups()
  })

  ipcMain.handle(GROUP_GET, (_event, id: string) => {
    return store.getGroup(id) || null
  })

  ipcMain.handle(GROUP_CREATE, (_event, data: GroupCreatePayload) => {
    const group = store.createGroup(data)

    // 启动 coordinator 引擎（群主）
    if (group.coordinator_id) {
      const coordinator = store.getAgent(group.coordinator_id)
      if (coordinator) {
        agentRegistry.addEngine(group.id, coordinator)
      }
    }

    // 启动子 agent 引擎
    if (data.member_ids) {
      for (const agentId of data.member_ids) {
        const agent = store.getAgent(agentId)
        if (agent) {
          agentRegistry.addEngine(group.id, agent)
        }
      }
    }
    return group
  })

  ipcMain.handle(GROUP_UPDATE, (_event, id: string, data: Partial<GroupCreatePayload>) => {
    return store.updateGroup(id, data)
  })

  ipcMain.handle(GROUP_DELETE, (_event, id: string) => {
    store.deleteGroup(id)
  })

  // ── Members ────────────────────────────────────────────────

  ipcMain.handle(GROUP_LIST_MEMBERS, (_event, groupId: string) => {
    return store.listGroupMembers(groupId)
  })

  ipcMain.handle(GROUP_ADD_MEMBER, (_event, groupId: string, agentId: string, alias?: string) => {
    const member = store.addMember(groupId, agentId, alias)
    // 启动该成员的 AgentEngine
    const agent = store.getAgent(agentId)
    if (agent) {
      agentRegistry.addEngine(groupId, agent)
    }
    return member
  })

  ipcMain.handle(GROUP_REMOVE_MEMBER, (_event, groupId: string, memberId: string) => {
    const members = store.listGroupMembers(groupId)
    const member = members.find(m => m.id === memberId)
    store.removeMember(groupId, memberId)
    // 停止该成员的 AgentEngine
    if (member) {
      agentRegistry.removeEngine(groupId, member.agent_id)
    }
  })

  // ── Files ──────────────────────────────────────────────────

  ipcMain.handle(GROUP_LIST_FILES, (_event, groupId: string) => {
    return store.listGroupFiles(groupId)
  })
}

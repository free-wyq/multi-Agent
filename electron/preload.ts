import { contextBridge, ipcRenderer } from 'electron'

// 暴露给 Renderer 进程的 API
contextBridge.exposeInMainWorld('electronAPI', {
  // ── Agent ──────────────────────────────────────────────────
  agentList: () => ipcRenderer.invoke('AGENT_LIST'),
  agentGet: (id: string) => ipcRenderer.invoke('AGENT_GET', id),
  agentCreate: (data: unknown) => ipcRenderer.invoke('AGENT_CREATE', data),
  agentUpdate: (id: string, data: unknown) => ipcRenderer.invoke('AGENT_UPDATE', id, data),
  agentDelete: (id: string) => ipcRenderer.invoke('AGENT_DELETE', id),

  // ── Group ──────────────────────────────────────────────────
  groupList: () => ipcRenderer.invoke('GROUP_LIST'),
  groupGet: (id: string) => ipcRenderer.invoke('GROUP_GET', id),
  groupCreate: (data: unknown) => ipcRenderer.invoke('GROUP_CREATE', data),
  groupUpdate: (id: string, data: unknown) => ipcRenderer.invoke('GROUP_UPDATE', id, data),
  groupDelete: (id: string) => ipcRenderer.invoke('GROUP_DELETE', id),

  // ── Group Member ───────────────────────────────────────────
  groupListMembers: (groupId: string) => ipcRenderer.invoke('GROUP_LIST_MEMBERS', groupId),
  groupAddMember: (groupId: string, agentId: string, alias?: string) =>
    ipcRenderer.invoke('GROUP_ADD_MEMBER', groupId, agentId, alias),
  groupRemoveMember: (groupId: string, memberId: string) =>
    ipcRenderer.invoke('GROUP_REMOVE_MEMBER', groupId, memberId),

  // ── Group File ─────────────────────────────────────────────
  groupListFiles: (groupId: string) => ipcRenderer.invoke('GROUP_LIST_FILES', groupId),

  // ── Task ───────────────────────────────────────────────────
  taskList: (groupId?: string) => ipcRenderer.invoke('TASK_LIST', groupId),
  taskGet: (id: string) => ipcRenderer.invoke('TASK_GET', id),
  taskCreate: (data: unknown) => ipcRenderer.invoke('TASK_CREATE', data),
  taskUpdate: (id: string, data: unknown) => ipcRenderer.invoke('TASK_UPDATE', id, data),
  taskDelete: (id: string) => ipcRenderer.invoke('TASK_DELETE', id),
  taskReady: (groupId: string) => ipcRenderer.invoke('TASK_READY', groupId),

  // ── Message ────────────────────────────────────────────────
  messageListByGroup: (groupId: string, limit?: number) =>
    ipcRenderer.invoke('MESSAGE_LIST_BY_GROUP', groupId, limit),
  messageListByTask: (taskId: string, limit?: number) =>
    ipcRenderer.invoke('MESSAGE_LIST_BY_TASK', taskId, limit),
  messageSend: (data: unknown) => ipcRenderer.invoke('MESSAGE_SEND', data),
  messageClearByGroup: (groupId: string) =>
    ipcRenderer.invoke('MESSAGE_CLEAR_BY_GROUP', groupId),

  // ── Coordinator ────────────────────────────────────────────
  coordinatorSubmit: (groupId: string, requirement: string) =>
    ipcRenderer.invoke('COORDINATOR_SUBMIT', groupId, requirement),
  coordinatorGetDag: (groupId: string) =>
    ipcRenderer.invoke('COORDINATOR_GET_DAG', groupId),
  coordinatorGetStatus: (groupId: string) =>
    ipcRenderer.invoke('COORDINATOR_GET_STATUS', groupId),

  // ── Runtime ───────────────────────────────────────────────
  runtimeStart: (agentId: string, groupId: string) =>
    ipcRenderer.invoke('RUNTIME_START', agentId, groupId),
  runtimeStop: (instanceId: string) =>
    ipcRenderer.invoke('RUNTIME_STOP', instanceId),
  runtimeExecute: (instanceId: string, taskId: string) =>
    ipcRenderer.invoke('RUNTIME_EXECUTE', instanceId, taskId),
  runtimeGetLogs: (instanceId: string) =>
    ipcRenderer.invoke('RUNTIME_GET_LOGS', instanceId),

  // ── 实时事件 ──────────────────────────────────────────────
  onBusEvent: (groupId: string, callback: (data: unknown) => void) => {
    const channel = `bus-event:${groupId}`
    const handler = (_event: Electron.IpcRendererEvent, data: unknown) => callback(data)
    ipcRenderer.on(channel, handler)
    return () => ipcRenderer.removeListener(channel, handler)
  },
  offBusEvent: (groupId: string, callback: (data: unknown) => void) => {
    const channel = `bus-event:${groupId}`
    ipcRenderer.removeListener(channel, callback as never)
  },

  // ── Settings ───────────────────────────────────────────────
  getSettings: () => ipcRenderer.invoke('SETTINGS_GET'),
  saveSettings: (settings: unknown) => ipcRenderer.invoke('SETTINGS_SAVE', settings),
})

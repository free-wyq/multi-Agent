export {}

declare global {
  interface Window {
    electronAPI: {
      // Agent
      agentList: () => Promise<import('../main/store/types').AgentDefinition[]>
      agentGet: (id: string) => Promise<import('../main/store/types').AgentDefinition | null>
      agentCreate: (data: unknown) => Promise<import('../main/store/types').AgentDefinition>
      agentUpdate: (id: string, data: unknown) => Promise<import('../main/store/types').AgentDefinition>
      agentDelete: (id: string) => Promise<void>

      // Group
      groupList: () => Promise<import('../main/store/types').Group[]>
      groupGet: (id: string) => Promise<import('../main/store/types').Group | null>
      groupCreate: (data: unknown) => Promise<import('../main/store/types').Group>
      groupUpdate: (id: string, data: unknown) => Promise<import('../main/store/types').Group>
      groupDelete: (id: string) => Promise<void>

      // Group Member
      groupListMembers: (groupId: string) => Promise<import('../main/store/types').GroupMemberWithAgent[]>
      groupAddMember: (groupId: string, agentId: string, alias?: string) => Promise<import('../main/store/types').GroupMember>
      groupRemoveMember: (groupId: string, memberId: string) => Promise<void>

      // Group File
      groupListFiles: (groupId: string) => Promise<import('../main/store/types').GroupFile[]>

      // Task
      taskList: (groupId?: string) => Promise<import('../main/store/types').Task[]>
      taskGet: (id: string) => Promise<import('../main/store/types').Task | null>
      taskCreate: (data: unknown) => Promise<import('../main/store/types').Task>
      taskUpdate: (id: string, data: unknown) => Promise<import('../main/store/types').Task>
      taskDelete: (id: string) => Promise<void>
      taskReady: (groupId: string) => Promise<import('../main/store/types').Task[]>

      // Message
      messageListByGroup: (groupId: string, limit?: number) => Promise<import('../main/store/types').Message[]>
      messageListByTask: (taskId: string, limit?: number) => Promise<import('../main/store/types').Message[]>
      messageSend: (data: unknown) => Promise<import('../main/store/types').Message>
      messageClearByGroup: (groupId: string) => Promise<void>

      // Coordinator
      coordinatorSubmit: (groupId: string, requirement: string) => Promise<unknown>
      coordinatorGetDag: (groupId: string) => Promise<unknown>
      coordinatorGetStatus: (groupId: string) => Promise<unknown>

      // Runtime
      runtimeStart: (agentId: string, groupId: string) => Promise<unknown>
      runtimeStop: (instanceId: string) => Promise<void>
      runtimeExecute: (instanceId: string, taskId: string) => Promise<unknown>
      runtimeGetLogs: (instanceId: string) => Promise<string>

      // 实时事件
      onBusEvent: (groupId: string, callback: (data: unknown) => void) => () => void
      offBusEvent: (groupId: string, callback: (data: unknown) => void) => void

      // Settings
      getSettings: () => Promise<unknown>
      saveSettings: (settings: unknown) => Promise<void>
    }
  }
}

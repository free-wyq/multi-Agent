/**
 * 内存状态管理 + JSON 文件持久化
 *
 * - 每个实体类型一个 Map<string, T>
 * - 提供统一 CRUD 方法
 * - 写操作自动触发持久化
 */

import { v4 as uuid } from 'uuid'
import type {
  AgentDefinition, AgentCreatePayload,
  Group, GroupCreatePayload,
  GroupMember, GroupMemberWithAgent,
  GroupFile,
  Task, TaskCreatePayload, TaskStatus,
  Message, MessageCreatePayload,
  AgentInstance,
} from './types'
import { persistence } from './persistence'

class AppStore {
  private agents = new Map<string, AgentDefinition>()
  private instances = new Map<string, AgentInstance>()
  private groups = new Map<string, Group>()
  private members = new Map<string, GroupMember>()
  private tasks = new Map<string, Task>()
  private messages = new Map<string, Message>()

  // ── 启动加载 ──────────────────────────────────────────────

  async loadFromPersistence(): Promise<void> {
    const data = await persistence.loadAll()
    for (const item of data.agents) this.agents.set(item.id, item)
    for (const item of data.groups) this.groups.set(item.id, item)
    for (const item of data.members) this.members.set(item.id, item)
    for (const item of data.tasks) this.tasks.set(item.id, item)
    for (const item of data.messages) this.messages.set(item.id, item)
  }

  // ── Agent CRUD ────────────────────────────────────────────

  listAgents(): AgentDefinition[] {
    return Array.from(this.agents.values())
  }

  getAgent(id: string): AgentDefinition | undefined {
    return this.agents.get(id)
  }

  createAgent(payload: AgentCreatePayload): AgentDefinition {
    const now = new Date().toISOString()
    const agent: AgentDefinition = {
      id: uuid(),
      name: payload.name,
      role: payload.role,
      system_prompt: payload.system_prompt || '',
      skills: payload.skills || [],
      extra_skills: payload.extra_skills || [],
      allowed_tools: [],
      denied_tools: [],
      startup_strategy: 'on_demand',
      model: 'glm-5.1',
      max_turns: 50,
      description: payload.description,
      created_at: now,
      updated_at: now,
    }
    this.agents.set(agent.id, agent)
    persistence.scheduleSave('agents', this.listAgents())
    return agent
  }

  updateAgent(id: string, payload: Partial<AgentDefinition>): AgentDefinition {
    const agent = this.agents.get(id)
    if (!agent) throw new Error(`Agent ${id} not found`)
    Object.assign(agent, payload, { updated_at: new Date().toISOString() })
    this.agents.set(id, agent)
    persistence.scheduleSave('agents', this.listAgents())
    return agent
  }

  deleteAgent(id: string): void {
    this.agents.delete(id)
    persistence.scheduleSave('agents', this.listAgents())
  }

  // ── Group CRUD ────────────────────────────────────────────

  listGroups(): Group[] {
    return Array.from(this.groups.values())
  }

  getGroup(id: string): Group | undefined {
    return this.groups.get(id)
  }

  createGroup(payload: GroupCreatePayload): Group {
    const now = new Date().toISOString()
    const group: Group = {
      id: uuid(),
      name: payload.name,
      coordinator_id: payload.coordinator_id || '',
      description: payload.description,
      status: 'active',
      created_at: now,
      updated_at: now,
    }
    this.groups.set(group.id, group)

    // 如果建群时指定了成员，自动添加
    if (payload.member_ids?.length) {
      for (const agentId of payload.member_ids) {
        this._addMemberInternal(group.id, agentId)
      }
    }

    persistence.scheduleSave('groups', this.listGroups())
    return group
  }

  updateGroup(id: string, payload: Partial<Group>): Group {
    const group = this.groups.get(id)
    if (!group) throw new Error(`Group ${id} not found`)
    Object.assign(group, payload, { updated_at: new Date().toISOString() })
    this.groups.set(id, group)
    persistence.scheduleSave('groups', this.listGroups())
    return group
  }

  deleteGroup(id: string): void {
    this.groups.delete(id)
    // 级联删除成员
    for (const [mid, m] of this.members) {
      if (m.group_id === id) this.members.delete(mid)
    }
    // 级联删除任务
    for (const [tid, t] of this.tasks) {
      if (t.group_id === id) this.tasks.delete(tid)
    }
    // 级联删除消息
    for (const [mid, m] of this.messages) {
      if (m.group_id === id) this.messages.delete(mid)
    }
    persistence.scheduleSave('groups', this.listGroups())
    persistence.scheduleSave('members', this.listMembers())
    persistence.scheduleSave('tasks', this.listTasksByGroup(id))
  }

  // ── GroupMember ───────────────────────────────────────────

  listMembers(): GroupMember[] {
    return Array.from(this.members.values())
  }

  listGroupMembers(groupId: string): GroupMemberWithAgent[] {
    const result: GroupMemberWithAgent[] = []
    for (const m of this.members.values()) {
      if (m.group_id === groupId) {
        const agent = this.agents.get(m.agent_id)
        result.push({
          ...m,
          agent_name: agent?.name || '未知',
          agent_role: agent?.role || 'unknown',
        })
      }
    }
    return result
  }

  addMember(groupId: string, agentId: string, alias?: string): GroupMember {
    const member = this._addMemberInternal(groupId, agentId, alias)
    persistence.scheduleSave('members', this.listMembers())
    return member
  }

  removeMember(groupId: string, memberId: string): void {
    const member = this.members.get(memberId)
    if (member && member.group_id === groupId) {
      this.members.delete(memberId)
      persistence.scheduleSave('members', this.listMembers())
    }
  }

  private _addMemberInternal(groupId: string, agentId: string, alias?: string): GroupMember {
    const member: GroupMember = {
      id: uuid(),
      group_id: groupId,
      agent_id: agentId,
      alias,
      joined_at: new Date().toISOString(),
    }
    this.members.set(member.id, member)
    return member
  }

  // ── Group Files ────────────────────────────────────────────

  listGroupFiles(groupId: string): GroupFile[] {
    return persistence.listFiles(groupId)
  }

  // ── Task CRUD ─────────────────────────────────────────────

  listTasksByGroup(groupId: string): Task[] {
    return Array.from(this.tasks.values()).filter(t => t.group_id === groupId)
  }

  getTask(id: string): Task | undefined {
    return this.tasks.get(id)
  }

  createTask(payload: TaskCreatePayload): Task {
    const now = new Date().toISOString()
    const task: Task = {
      id: uuid(),
      group_id: payload.group_id,
      title: payload.title,
      description: payload.description,
      status: 'submitted',
      assigned_agent_id: payload.assigned_agent_id,
      dependencies: payload.dependencies || [],
      dag_order: payload.dag_order,
      created_at: now,
    }
    this.tasks.set(task.id, task)
    persistence.scheduleSave('tasks', Array.from(this.tasks.values()))
    return task
  }

  updateTask(id: string, payload: Partial<Task>): Task {
    const task = this.tasks.get(id)
    if (!task) throw new Error(`Task ${id} not found`)

    // 状态转换自动设置时间戳
    if (payload.status === 'working' && !task.started_at) {
      payload.started_at = new Date().toISOString()
    }
    if ((payload.status === 'completed' || payload.status === 'failed' || payload.status === 'canceled') && !task.completed_at) {
      payload.completed_at = new Date().toISOString()
    }

    Object.assign(task, payload)
    this.tasks.set(id, task)
    persistence.scheduleSave('tasks', Array.from(this.tasks.values()))
    return task
  }

  deleteTask(id: string): void {
    this.tasks.delete(id)
    persistence.scheduleSave('tasks', Array.from(this.tasks.values()))
  }

  getReadyTasks(groupId: string): Task[] {
    const groupTasks = this.listTasksByGroup(groupId)
    return groupTasks.filter(task => {
      if (task.status !== 'submitted') return false
      if (!task.dependencies.length) return true
      return task.dependencies.every(depId => {
        const dep = this.tasks.get(depId)
        return dep?.status === 'completed'
      })
    })
  }

  // ── Message CRUD ──────────────────────────────────────────

  listMessagesByGroup(groupId: string, limit?: number): Message[] {
    let msgs = Array.from(this.messages.values())
      .filter(m => m.group_id === groupId)
      .sort((a, b) => a.created_at.localeCompare(b.created_at))
    if (limit) msgs = msgs.slice(-limit)
    return msgs
  }

  listMessagesByTask(taskId: string, limit?: number): Message[] {
    let msgs = Array.from(this.messages.values())
      .filter(m => m.task_id === taskId)
      .sort((a, b) => a.created_at.localeCompare(b.created_at))
    if (limit) msgs = msgs.slice(-limit)
    return msgs
  }

  createMessage(payload: MessageCreatePayload): Message {
    const msg: Message = {
      id: uuid(),
      group_id: payload.group_id,
      task_id: payload.task_id,
      sender_id: payload.sender_id,
      receiver_id: payload.receiver_id || 'broadcast',
      type: payload.type || 'user_input',
      content: payload.content,
      data: payload.data,
      created_at: new Date().toISOString(),
    }
    this.messages.set(msg.id, msg)
    persistence.scheduleSave('messages', Array.from(this.messages.values()))
    return msg
  }

  clearMessagesByGroup(groupId: string): void {
    for (const [id, m] of this.messages) {
      if (m.group_id === groupId) this.messages.delete(id)
    }
    persistence.scheduleSave('messages', Array.from(this.messages.values()))
  }

  // ── AgentInstance ─────────────────────────────────────────

  createInstance(definitionId: string, groupId: string): AgentInstance {
    const instance: AgentInstance = {
      id: uuid(),
      definition_id: definitionId,
      status: 'idle',
      work_dir: `data/group_files/${groupId}`,
      created_at: new Date().toISOString(),
    }
    this.instances.set(instance.id, instance)
    return instance
  }

  updateInstance(id: string, payload: Partial<AgentInstance>): AgentInstance {
    const instance = this.instances.get(id)
    if (!instance) throw new Error(`Instance ${id} not found`)
    Object.assign(instance, payload)
    this.instances.set(id, instance)
    return instance
  }

  getInstance(id: string): AgentInstance | undefined {
    return this.instances.get(id)
  }

  listInstancesByGroup(groupId: string): AgentInstance[] {
    return Array.from(this.instances.values())
      .filter(i => i.work_dir?.includes(groupId))
  }

  // ── 辅助 ─────────────────────────────────────────────────

  getGroupRoles(groupId: string): { id: string; role: string; name: string }[] {
    const memberAgentIds = Array.from(this.members.values())
      .filter(m => m.group_id === groupId)
      .map(m => m.agent_id)

    const result: { id: string; role: string; name: string }[] = []
    for (const agentId of memberAgentIds) {
      const agent = this.agents.get(agentId)
      if (agent) {
        result.push({ id: agent.id, role: agent.role, name: agent.name })
      }
    }
    return result
  }

  resolveRoleMapping(groupId: string): Record<string, string> {
    const roles = this.getGroupRoles(groupId)
    const map: Record<string, string> = {}
    for (const r of roles) {
      map[r.role] = r.id
    }
    return map
  }
}

export const store = new AppStore()

/**
 * Agent Engine 注册表
 *
 * 进化后：
 * - 仍然管理引擎生命周期
 * - routeMessage 保留向后兼容（内部已被 engine.pushMessage 重写为写 sharedState）
 * - loadFromStore 同时加载子 agent 和 coordinator
 */

import { AgentEngine } from './engine'
import { store } from '../store/store'

class AgentRegistry {
  private engines = new Map<string, Map<string, AgentEngine>>()

  /**
   * 从 store 加载所有群成员（含 coordinator）并创建引擎
   */
  async loadFromStore(): Promise<void> {
    const groups = store.listGroups()
    for (const group of groups) {
      // 1. 加载 coordinator 引擎（群主）
      if (group.coordinator_id) {
        const coordinator = store.getAgent(group.coordinator_id)
        if (coordinator) {
          this.addEngine(group.id, coordinator)
        }
      }

      // 2. 加载子 agent 引擎
      const members = store.listGroupMembers(group.id)
      for (const member of members) {
        const agent = store.getAgent(member.agent_id)
        if (agent) {
          this.addEngine(group.id, agent)
        }
      }
    }
  }

  /**
   * 为群组添加一个智能体引擎
   */
  addEngine(groupId: string, agentDef: { id: string; name: string; role: string; system_prompt: string }): AgentEngine {
    if (!this.engines.has(groupId)) {
      this.engines.set(groupId, new Map())
    }

    const groupEngines = this.engines.get(groupId)!
    if (groupEngines.has(agentDef.id)) {
      return groupEngines.get(agentDef.id)!
    }

    const engine = new AgentEngine(agentDef as never, groupId)
    engine.start()
    groupEngines.set(agentDef.id, engine)
    return engine
  }

  /**
   * 移除一个智能体引擎
   */
  removeEngine(groupId: string, agentId: string): void {
    const groupEngines = this.engines.get(groupId)
    if (!groupEngines) return

    const engine = groupEngines.get(agentId)
    if (engine) {
      engine.stop()
      groupEngines.delete(agentId)
    }

    if (!groupEngines.size) {
      this.engines.delete(groupId)
    }
  }

  /**
   * 获取引擎
   */
  getEngine(groupId: string, agentId: string): AgentEngine | undefined {
    return this.engines.get(groupId)?.get(agentId)
  }

  /**
   * 路由消息到指定智能体（保留兼容，内部已改为写 sharedState）
   */
  routeMessage(targetAgentId: string, message: Record<string, unknown>, groupId: string): void {
    const engine = this.getEngine(groupId, targetAgentId)
    if (engine) {
      engine.pushMessage(message)
    } else {
      console.warn(`AgentEngine not found: ${targetAgentId} in group ${groupId}`)
    }
  }

  /**
   * 关闭所有引擎
   */
  async shutdownAll(): Promise<void> {
    for (const [, groupEngines] of this.engines) {
      for (const [, engine] of groupEngines) {
        engine.stop()
      }
    }
    this.engines.clear()
  }

  /**
   * 获取群组内所有引擎
   */
  getGroupEngines(groupId: string): AgentEngine[] {
    return Array.from(this.engines.get(groupId)?.values() || [])
  }
}

export const agentRegistry = new AgentRegistry()

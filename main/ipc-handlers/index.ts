/**
 * IPC Handlers 统一注册入口
 */

import { registerAgentHandlers } from './agent.handlers'
import { registerGroupHandlers } from './group.handlers'
import { registerTaskHandlers } from './task.handlers'
import { registerMessageHandlers } from './message.handlers'
import { registerCoordinatorHandlers } from './coordinator.handlers'
import { registerRuntimeHandlers } from './runtime.handlers'
import { registerSettingsHandlers } from './settings.handlers'

export function registerAllHandlers(): void {
  registerAgentHandlers()
  registerGroupHandlers()
  registerTaskHandlers()
  registerMessageHandlers()
  registerCoordinatorHandlers()
  registerRuntimeHandlers()
  registerSettingsHandlers()
}

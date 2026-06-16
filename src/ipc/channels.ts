/**
 * IPC 通道名常量
 */

// Agent
export const AGENT_LIST = 'AGENT_LIST'
export const AGENT_GET = 'AGENT_GET'
export const AGENT_CREATE = 'AGENT_CREATE'
export const AGENT_UPDATE = 'AGENT_UPDATE'
export const AGENT_DELETE = 'AGENT_DELETE'

// Group
export const GROUP_LIST = 'GROUP_LIST'
export const GROUP_GET = 'GROUP_GET'
export const GROUP_CREATE = 'GROUP_CREATE'
export const GROUP_UPDATE = 'GROUP_UPDATE'
export const GROUP_DELETE = 'GROUP_DELETE'

// Group Member
export const GROUP_LIST_MEMBERS = 'GROUP_LIST_MEMBERS'
export const GROUP_ADD_MEMBER = 'GROUP_ADD_MEMBER'
export const GROUP_REMOVE_MEMBER = 'GROUP_REMOVE_MEMBER'

// Group File
export const GROUP_LIST_FILES = 'GROUP_LIST_FILES'

// Task
export const TASK_LIST = 'TASK_LIST'
export const TASK_GET = 'TASK_GET'
export const TASK_CREATE = 'TASK_CREATE'
export const TASK_UPDATE = 'TASK_UPDATE'
export const TASK_DELETE = 'TASK_DELETE'
export const TASK_READY = 'TASK_READY'

// Message
export const MESSAGE_LIST_BY_GROUP = 'MESSAGE_LIST_BY_GROUP'
export const MESSAGE_LIST_BY_TASK = 'MESSAGE_LIST_BY_TASK'
export const MESSAGE_SEND = 'MESSAGE_SEND'
export const MESSAGE_CLEAR_BY_GROUP = 'MESSAGE_CLEAR_BY_GROUP'

// Coordinator
export const COORDINATOR_SUBMIT = 'COORDINATOR_SUBMIT'
export const COORDINATOR_GET_DAG = 'COORDINATOR_GET_DAG'
export const COORDINATOR_GET_STATUS = 'COORDINATOR_GET_STATUS'

// Runtime
export const RUNTIME_START = 'RUNTIME_START'
export const RUNTIME_STOP = 'RUNTIME_STOP'
export const RUNTIME_EXECUTE = 'RUNTIME_EXECUTE'
export const RUNTIME_GET_LOGS = 'RUNTIME_GET_LOGS'

// Settings
export const SETTINGS_GET = 'SETTINGS_GET'
export const SETTINGS_SAVE = 'SETTINGS_SAVE'

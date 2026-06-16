/**
 * JSON 文件持久化
 *
 * - 每个实体类型一个 JSON 文件
 * - 写入 500ms 防抖，原子写入（先 .tmp 再 rename）
 * - 启动时加载填充内存
 */

import * as fs from 'fs'
import * as path from 'path'
import type { AgentDefinition, Group, GroupMember, Task, Message, GroupFile, GroupQueueSnapshot } from './types'

const DATA_DIR = path.join(process.cwd(), 'data')

// 防抖定时器
const _debounceTimers = new Map<string, NodeJS.Timeout>()

// 队列防抖定时器（按 groupId）
const _queueDebounceTimers = new Map<string, NodeJS.Timeout>()

// 确保数据目录存在
function ensureDataDir(): void {
  if (!fs.existsSync(DATA_DIR)) {
    fs.mkdirSync(DATA_DIR, { recursive: true })
  }
  const groupFilesDir = path.join(DATA_DIR, 'group_files')
  if (!fs.existsSync(groupFilesDir)) {
    fs.mkdirSync(groupFilesDir, { recursive: true })
  }
}

/**
 * 原子写入 JSON 文件
 * 先写 .tmp 再 rename，防止崩溃时数据丢失
 */
function atomicWrite(filePath: string, data: unknown): void {
  const tmpPath = filePath + '.tmp'
  const json = JSON.stringify(data, null, 2)
  fs.writeFileSync(tmpPath, json, 'utf-8')
  fs.renameSync(tmpPath, filePath)
}

/**
 * 读取 JSON 文件，文件不存在或损坏时返回空数组
 */
function readJsonFile<T>(filePath: string): T[] {
  try {
    if (!fs.existsSync(filePath)) return []
    const content = fs.readFileSync(filePath, 'utf-8')
    return JSON.parse(content) as T[]
  } catch {
    return []
  }
}

/**
 * 防抖写入：500ms 内多次写入只执行最后一次
 */
export function scheduleSave(entityName: string, data: unknown): void {
  const existing = _debounceTimers.get(entityName)
  if (existing) clearTimeout(existing)

  const timer = setTimeout(() => {
    ensureDataDir()
    const filePath = path.join(DATA_DIR, `${entityName}.json`)
    try {
      atomicWrite(filePath, data)
    } catch (err) {
      console.error(`Failed to save ${entityName}:`, err)
    }
    _debounceTimers.delete(entityName)
  }, 500)

  _debounceTimers.set(entityName, timer)
}

/**
 * 队列持久化：按 groupId 存储到 data/queues/{groupId}.json
 */
export function scheduleSaveQueue(groupId: string, data: GroupQueueSnapshot): void {
  const existing = _queueDebounceTimers.get(groupId)
  if (existing) clearTimeout(existing)

  const timer = setTimeout(() => {
    const queueDir = path.join(DATA_DIR, 'queues')
    if (!fs.existsSync(queueDir)) {
      fs.mkdirSync(queueDir, { recursive: true })
    }
    const filePath = path.join(queueDir, `${groupId}.json`)
    try {
      atomicWrite(filePath, data)
    } catch (err) {
      console.error(`Failed to save queue for group ${groupId}:`, err)
    }
    _queueDebounceTimers.delete(groupId)
  }, 500)

  _queueDebounceTimers.set(groupId, timer)
}

export async function loadAllQueues(): Promise<GroupQueueSnapshot[]> {
  const queueDir = path.join(DATA_DIR, 'queues')
  if (!fs.existsSync(queueDir)) return []

  const snapshots: GroupQueueSnapshot[] = []
  try {
    const files = fs.readdirSync(queueDir).filter(f => f.endsWith('.json'))
    for (const file of files) {
      try {
        const content = fs.readFileSync(path.join(queueDir, file), 'utf-8')
        snapshots.push(JSON.parse(content) as GroupQueueSnapshot)
      } catch {
        // 忽略损坏的队列文件
      }
    }
  } catch {
    return []
  }
  return snapshots
}

/**
 * 立即刷盘（应用退出时调用）
 */
export async function flushPersistence(): Promise<void> {
  // 清除所有实体防抖定时器
  for (const [, timer] of _debounceTimers) {
    clearTimeout(timer)
  }
  _debounceTimers.clear()

  // 清除所有队列防抖定时器
  for (const [, timer] of _queueDebounceTimers) {
    clearTimeout(timer)
  }
  _queueDebounceTimers.clear()
}

/**
 * 启动时加载所有数据
 */
export async function initPersistence(): Promise<void> {
  ensureDataDir()
}

export async function loadAll(): Promise<{
  agents: AgentDefinition[]
  groups: Group[]
  members: GroupMember[]
  tasks: Task[]
  messages: Message[]
}> {
  ensureDataDir()
  return {
    agents: readJsonFile<AgentDefinition>(path.join(DATA_DIR, 'agents.json')),
    groups: readJsonFile<Group>(path.join(DATA_DIR, 'groups.json')),
    members: readJsonFile<GroupMember>(path.join(DATA_DIR, 'members.json')),
    tasks: readJsonFile<Task>(path.join(DATA_DIR, 'tasks.json')),
    messages: readJsonFile<Message>(path.join(DATA_DIR, 'messages.json')),
  }
}

/**
 * 列出群组文件
 */
export function listFiles(groupId: string): GroupFile[] {
  const groupDir = path.join(DATA_DIR, 'group_files', groupId)
  if (!fs.existsSync(groupDir)) return []

  try {
    return fs.readdirSync(groupDir).map(name => {
      const filePath = path.join(groupDir, name)
      const stat = fs.statSync(filePath)
      return {
        name,
        size: stat.size,
        modified_at: stat.mtime.toISOString(),
      }
    })
  } catch {
    return []
  }
}

export const persistence = {
  scheduleSave,
  scheduleSaveQueue,
  flushPersistence,
  initPersistence,
  loadAll,
  loadAllQueues,
  listFiles,
}

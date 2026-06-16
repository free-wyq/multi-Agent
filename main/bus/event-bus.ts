/**
 * 进程内事件总线
 *
 * 替代 Redis Pub/Sub：
 * - 基于 Node.js EventEmitter
 * - 频道命名保持 agenticx:group:{groupId}
 * - 支持发布、订阅、取消订阅、发布+持久化
 * - 消息自动转发到 Electron Renderer 进程
 */

import { EventEmitter } from 'events'
import { BrowserWindow } from 'electron'
import { store } from '../store/store'

const CHANNEL_PREFIX = 'agenticx:group:'

type BusHandler = (message: Record<string, unknown>) => void

class EventBus {
  private emitter = new EventEmitter()
  private initialized = false

  // 防止 EventEmitter 内存泄漏警告
  constructor() {
    this.emitter.setMaxListeners(100)
  }

  initialize(): void {
    this.initialized = true
  }

  /**
   * 发布消息到频道
   */
  publish(channel: string, message: Record<string, unknown>): void {
    if (!this.initialized) return
    this.emitter.emit(channel, message)

    // 同时转发到所有 BrowserWindow（Renderer 进程）
    const groupId = channel.replace(CHANNEL_PREFIX, '')
    const windows = BrowserWindow.getAllWindows()
    for (const win of windows) {
      if (!win.isDestroyed()) {
        win.webContents.send(`bus-event:${groupId}`, message)
      }
    }
  }

  /**
   * 订阅频道
   */
  subscribe(channel: string, handler: BusHandler): void {
    this.emitter.on(channel, handler)
  }

  /**
   * 取消订阅
   */
  unsubscribe(channel: string, handler: BusHandler): void {
    this.emitter.off(channel, handler)
  }

  /**
   * 发布 + 持久化到 Message 表
   * 即使 DB 写入失败，消息仍会发布
   */
  publishAndPersist(
    channel: string,
    params: {
      group_id: string
      task_id?: string
      sender_id: string
      receiver_id: string
      type: string
      content?: string
      data?: Record<string, unknown>
    },
  ): Record<string, unknown> {
    const message: Record<string, unknown> = {
      id: crypto.randomUUID(),
      group_id: params.group_id,
      task_id: params.task_id || null,
      sender_id: params.sender_id,
      receiver_id: params.receiver_id,
      type: params.type,
      content: params.content,
      data: params.data,
      timestamp: new Date().toISOString(),
    }

    // 持久化到 store（内存 + JSON 文件）
    try {
      store.createMessage({
        group_id: params.group_id,
        task_id: params.task_id,
        sender_id: params.sender_id,
        receiver_id: params.receiver_id,
        type: params.type,
        content: params.content,
        data: params.data,
      })
    } catch (err) {
      console.warn('Failed to persist message:', err)
      // 仍然继续发布
    }

    // 发布到事件总线
    this.publish(channel, message)
    return message
  }

  /**
   * 获取频道名
   */
  getChannel(groupId: string): string {
    return `${CHANNEL_PREFIX}${groupId}`
  }
}

export const eventBus = new EventBus()
export { CHANNEL_PREFIX }

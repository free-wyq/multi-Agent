import { useEffect, useRef, useState } from 'react'

export interface LogEntry {
  agentId: string
  agentName: string
  taskId: string
  message: string
  timestamp: number
}

export interface TaskStatusEvent {
  taskId: string
  status: string
  groupId: string
  agentId?: string
  updatedAt: string
}

export function useMockWebSocket(enabled: boolean) {
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [statusEvents, setStatusEvents] = useState<TaskStatusEvent[]>([])
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    if (!enabled) return

    const mockAgents = [
      { id: 'agent-1', name: '前端开发' },
      { id: 'agent-2', name: '后端开发' },
      { id: 'agent-3', name: '测试工程师' },
    ]
    const mockTasks = ['task-1', 'task-2', 'task-3']
    const templates = [
      '正在解析需求...',
      '安装依赖中...',
      '编译项目...',
      '执行测试用例...',
      '写入文件 /workspace/shared/login.py',
      '任务完成，退出码 0',
    ]

    intervalRef.current = setInterval(() => {
      const agent = mockAgents[Math.floor(Math.random() * mockAgents.length)]
      const task = mockTasks[Math.floor(Math.random() * mockTasks.length)]
      const msg = templates[Math.floor(Math.random() * templates.length)]
      const entry: LogEntry = {
        agentId: agent.id,
        agentName: agent.name,
        taskId: task,
        message: msg,
        timestamp: Date.now(),
      }
      setLogs((prev) => [...prev.slice(-200), entry])

      if (Math.random() > 0.7) {
        const statuses = ['working', 'completed', 'failed']
        const evt: TaskStatusEvent = {
          taskId: task,
          status: statuses[Math.floor(Math.random() * statuses.length)],
          groupId: 'group-1',
          agentId: agent.id,
          updatedAt: new Date().toISOString(),
        }
        setStatusEvents((prev) => [...prev.slice(-50), evt])
      }
    }, 1500)

    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [enabled])

  return { logs, statusEvents }
}

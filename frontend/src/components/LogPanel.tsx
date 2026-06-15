import { useEffect, useRef } from 'react'
import { Tag } from 'antd'
import { useMockWebSocket } from '../hooks/useWebSocket'

interface LogPanelProps {
  taskId?: string
  agentId?: string
}

export default function LogPanel({ taskId, agentId }: LogPanelProps) {
  const { logs } = useMockWebSocket(true)
  const containerRef = useRef<HTMLDivElement>(null)

  const filtered = logs.filter((l) => {
    if (taskId && l.taskId !== taskId) return false
    if (agentId && l.agentId !== agentId) return false
    return true
  })

  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight
    }
  }, [filtered.length])

  if (filtered.length === 0) {
    return (
      <div
        style={{
          marginTop: 8,
          padding: 12,
          background: '#f6f8fa',
          borderRadius: 4,
          minHeight: 60,
          fontFamily: 'monospace',
          fontSize: 12,
          color: '#999',
        }}
      >
        等待日志...
      </div>
    )
  }

  return (
    <div
      ref={containerRef}
      style={{
        marginTop: 8,
        padding: 12,
        background: '#1e1e1e',
        borderRadius: 4,
        maxHeight: 240,
        overflowY: 'auto',
        fontFamily: 'monospace',
        fontSize: 12,
        color: '#c9d1d9',
        lineHeight: 1.6,
      }}
    >
      {filtered.map((l, idx) => (
        <div key={idx}>
          <span style={{ color: '#8b949e' }}>
            [{new Date(l.timestamp).toLocaleTimeString()}]
          </span>{' '}
          <Tag color="blue" style={{ fontSize: 10, lineHeight: '14px' }}>
            {l.agentName}
          </Tag>{' '}
          {l.message}
        </div>
      ))}
    </div>
  )
}

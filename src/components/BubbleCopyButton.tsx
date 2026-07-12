/**
 * BubbleCopyButton — 单条气泡 hover 复制按钮。
 *
 * 与 BubbleSpeakButton 同列：绝对定位到气泡右上角，hover 父级 .chat-bubble-wrap 显隐。
 * 点击把消息纯文本复制到剪贴板（navigator.clipboard，纯前端）。用户和 agent 消息均可复制。
 *
 * 复制成功用 antd message.success 提示「已复制」（短暂），无需常驻图标变化。
 */
import { useState } from 'react'
import { Button, Tooltip, message } from 'antd'
import { CopyOutlined, CheckOutlined } from '@ant-design/icons'

interface BubbleCopyButtonProps {
  /** 待复制文本（气泡正文，纯文本）。 */
  content: string
}

export default function BubbleCopyButton({ content }: BubbleCopyButtonProps) {
  const [copied, setCopied] = useState(false)

  const handleClick = async () => {
    if (!content) return
    try {
      await navigator.clipboard.writeText(content)
      setCopied(true)
      message.success('已复制', 1)
      // 1.2s 后复位图标，让 hover 隐藏后下次 hover 是默认复制态
      setTimeout(() => setCopied(false), 1200)
    } catch (e) {
      message.error(`复制失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  return (
    <Tooltip title={copied ? '已复制' : '复制'}>
      <Button
        type="text"
        size="small"
        className="bubble-action-btn"
        onClick={handleClick}
        style={{ color: copied ? '#52c41a' : undefined }}
        icon={copied ? <CheckOutlined /> : <CopyOutlined />}
      />
    </Tooltip>
  )
}

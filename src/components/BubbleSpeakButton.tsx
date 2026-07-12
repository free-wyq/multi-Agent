/**
 * BubbleSpeakButton — 单条气泡 hover 朗读按钮。
 *
 * hover 父级 .chat-bubble-wrap 时显隐（CSS 控 opacity），点击用 useTts 朗读该条消息 content。
 * 朗读中按钮变「停止」图标，再点 stop()。tts.enabled=false 时父组件不渲染本按钮。
 *
 * 视觉：绝对定位到气泡右上角（.chat-bubble-wrap 需 position:relative，见 ChatPanel.css）。
 * 用 antd Tooltip + Button(text/icon) 与现有气泡工具按钮风格一致。
 */
import { Button, Tooltip } from 'antd'
import { SoundOutlined, PauseOutlined } from '@ant-design/icons'

import { useTts } from '../hooks/useTts'

interface BubbleSpeakButtonProps {
  /** 待朗读文本（气泡正文，纯文本）。 */
  content: string
}

export default function BubbleSpeakButton({ content }: BubbleSpeakButtonProps) {
  const { speak, stop, speaking } = useTts()

  const handleClick = () => {
    if (speaking) {
      stop()
    } else {
      speak(content)
    }
  }

  return (
    <Tooltip title={speaking ? '停止朗读' : '朗读'}>
      <Button
        type="text"
        size="small"
        className={`bubble-speak-btn${speaking ? ' is-speaking' : ''}`}
        onClick={handleClick}
        // 朗读中高亮，提示当前正在读的是这条
        style={{ color: speaking ? '#0A5ACF' : undefined }}
        icon={speaking ? <PauseOutlined /> : <SoundOutlined />}
      />
    </Tooltip>
  )
}

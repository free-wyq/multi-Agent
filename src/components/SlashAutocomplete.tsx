import { useEffect, useRef } from 'react'
import type { SlashCommand } from '../lib/slashCommands'

interface SlashAutocompleteProps {
  /** 当前匹配到的候选命令列表（由父组件用 matchSlashCommands(query) 计算后传入）。
   *  非空时下拉展示；空数组时父组件应不渲染本组件（本组件也对空列表返回 null）。 */
  commands: SlashCommand[]
  /** 当前高亮项索引（父组件持有 + 键盘导航更新）。越界/无高亮时传 -1。 */
  activeIndex: number
  /** 选中某命令的回调（鼠标点击或键盘 Enter 触发）。父组件负责把 `/name ` 填入输入框并关下拉。 */
  onSelect: (cmd: SlashCommand) => void
  /** 鼠标悬停某项时同步父组件高亮索引（hover 即定位，键盘与鼠标统一）。 */
  onHover: (index: number) => void
}

/**
 * SC-02 slash 命令自动补全下拉。
 *
 * 输入框输入 `/`（+ 可选前缀）时由父组件（ChatPanel，SC-11 接入）渲染于输入框上方，
 * 展示父组件用 matchSlashCommands(query) 算好的候选命令，键盘上下导航 + Enter 选中、
 * 鼠标点击/悬停选中。
 *
 * 与 ChatPanel 既有 @mention 下拉（ChatPanel.tsx 行 401-441）视觉对齐：同样绝对定位贴
 * 输入框上方（bottom:100%）、白底圆角阴影、hover/active 项 #e6f4ff 高亮——保持聊天区
 * 补全交互一致，零学习成本。
 *
 * 职责边界（纯展示 + 事件上抛）：
 *  - 本组件不持状态、不调 matchSlashCommands——候选列表由父组件算好传入（父掌握输入框文本，
 *    才能算 query）。这样输入文本→query→候选 的数据流单向、单一真源在父。
 *  - activeIndex 也由父持有：键盘导航（ArrowUp/Down/Enter/Escape）发生在输入框 onKeyDown
 *    （父组件），故高亮索引必须父持有才能在 keydown 时更新并回传本组件渲染高亮。
 *  - 本组件只做：①渲染候选 ②鼠标 click→onSelect ③鼠标 hover→onHover 同步索引。
 *
 * 设计：受控组件（commands/activeIndex 入，onSelect/onHover 出）。键盘逻辑在父——避免
 * 输入框与下拉各自监听 keydown 冲突（输入框是焦点持有者，keydown 必先到它）。
 */
export default function SlashAutocomplete({
  commands,
  activeIndex,
  onSelect,
  onHover,
}: SlashAutocompleteProps) {
  const listRef = useRef<HTMLDivElement>(null)

  // activeIndex 变化时滚动到可视区（键盘导航到列表底部时自动滚出，需手动带入视区）。
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(
      `[data-slash-idx="${activeIndex}"]`,
    )
    el?.scrollIntoView({ block: 'nearest' })
  }, [activeIndex])

  if (commands.length === 0) return null

  return (
    <div
      ref={listRef}
      style={{
        position: 'absolute',
        bottom: '100%',
        left: 16,
        marginBottom: 4,
        background: '#fff',
        border: '1px solid #f0f0f0',
        borderRadius: 6,
        boxShadow: '0 4px 12px rgba(0,0,0,0.1)',
        zIndex: 100,
        maxHeight: 240,
        overflowY: 'auto',
        width: 320,
      }}
    >
      <div
        style={{
          padding: '4px 12px',
          fontSize: 11,
          color: '#999',
          borderBottom: '1px solid #f5f5f5',
          position: 'sticky',
          top: 0,
          background: '#fff',
        }}
      >
        斜杠命令 · ↑↓ 选择 · Enter 执行 · Esc 取消
      </div>
      {commands.map((cmd, idx) => {
        const active = idx === activeIndex
        return (
          <div
            key={cmd.name}
            data-slash-idx={idx}
            onClick={() => onSelect(cmd)}
            onMouseEnter={() => onHover(idx)}
            style={{
              padding: '8px 12px',
              cursor: 'pointer',
              background: active ? '#e6f4ff' : '#fff',
              display: 'flex',
              flexDirection: 'column',
              gap: 2,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <code
                style={{
                  fontSize: 13,
                  fontWeight: 600,
                  color: '#1677ff',
                  background: '#f0f5ff',
                  padding: '1px 6px',
                  borderRadius: 4,
                }}
              >
                {cmd.usage}
              </code>
            </div>
            <div style={{ fontSize: 12, color: '#666' }}>{cmd.description}</div>
          </div>
        )
      })}
    </div>
  )
}

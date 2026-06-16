import { useMemo } from 'react'

/**
 * 基于 name 的确定性 3D 机器人头像
 * 同名 → 同头像；风格参考：圆润 3D 机器人，有高光、阴影、立体感
 */

function hashStr(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) {
    h = ((h << 5) - h + s.charCodeAt(i)) | 0
  }
  return Math.abs(h)
}

function pick(hash: number, index: number, count: number): number {
  return (hash >> (index * 3 + 4)) % count
}

interface Props {
  name: string
  size?: number
  baseColor?: string
}

export default function AgentAvatar({ name, size = 48, baseColor }: Props) {
  const h = useMemo(() => hashStr(name), [name])

  /* ── 形状变量 ── */
  const headW = 44 + pick(h, 0, 4) * 3       // 44-53
  const headH = 40 + pick(h, 1, 4) * 3       // 40-49
  const headRx = 12 + pick(h, 2, 4)          // 12-15 圆角
  const earStyle = pick(h, 3, 4)             // 0 圆耳 1 方耳 2 无耳 3 天线
  const eyeStyle = pick(h, 4, 4)             // 0 圆 1 椭圆 2 LED 3 弯月
  const mouthStyle = pick(h, 5, 4)           // 0 微笑 1 横线 2 方嘴 3 圆嘴
  const deco = pick(h, 6, 4)                 // 0 无 1 额灯 2 螺丝 3 条纹
  const cheeks = pick(h, 7, 2) === 0         // 腮红
  const chinBump = pick(h, 8, 2) === 0       // 下巴凸起

  /* ── 颜色 ── */
  const hue = ((h & 0xff) * 1.41) % 360
  const c1 = baseColor ?? `hsl(${hue}, 65%, 55%)`
  const c2 = baseColor ?? `hsl(${hue}, 55%, 42%)`
  const cLight = baseColor ?? `hsl(${hue}, 72%, 72%)`
  const accent = `hsl(${(hue + 140) % 360}, 75%, 60%)`

  const cx = 50
  const cy = 52

  /* ── 渲染眼睛 ── */
  const renderEye = (ox: number) => {
    const ex = cx + ox
    const ey = cy - 2
    const ew = 5 + pick(h, 9, 3)           // 5-7
    switch (eyeStyle) {
      case 0: // 圆
        return (
          <g>
            <ellipse cx={ex} cy={ey} rx={ew} ry={ew} fill="#e8edf5" />
            <ellipse cx={ex} cy={ey} rx={ew - 1.2} ry={ew - 1.2} fill="#1e1b4b" />
            <circle cx={ex - 1.5} cy={ey - 1.5} r={1.8} fill="rgba(255,255,255,0.7)" />
          </g>
        )
      case 1: // 椭圆
        return (
          <g>
            <ellipse cx={ex} cy={ey} rx={ew + 1} ry={ew - 1} fill="#e8edf5" />
            <ellipse cx={ex} cy={ey} rx={ew} ry={ew - 2} fill="#1e1b4b" />
            <circle cx={ex - 1.5} cy={ey - 1} r={1.5} fill="rgba(255,255,255,0.6)" />
          </g>
        )
      case 2: // LED 发光
        return (
          <g>
            <circle cx={ex} cy={ey} r={ew + 1} fill="#1a1a2e" />
            <circle cx={ex} cy={ey} r={ew - 0.5} fill={accent} className="avatar-eye-led" />
            <circle cx={ex - 1} cy={ey - 1} r={1.5} fill="rgba(255,255,255,0.5)" />
          </g>
        )
      default: // 弯月
        return (
          <g>
            <path d={`M${ex - ew},${ey + 1} Q${ex},${ey - ew - 2} ${ex + ew},${ey + 1}`}
              fill="none" stroke="#1e1b4b" strokeWidth={2.5} strokeLinecap="round" />
          </g>
        )
    }
  }

  /* ── 渲染嘴 ── */
  const renderMouth = () => {
    const my = cy + 10
    switch (mouthStyle) {
      case 0: // 微笑
        return <path d={`M${cx - 6},${my} Q${cx},${my + 7} ${cx + 6},${my}`}
          fill="none" stroke="#1e1b4b" strokeWidth={2} strokeLinecap="round" />
      case 1: // 横线
        return <line x1={cx - 5} y1={my + 1} x2={cx + 5} y2={my + 1}
          stroke="#1e1b4b" strokeWidth={2} strokeLinecap="round" />
      case 2: // 方嘴
        return <rect x={cx - 4} y={my - 1} width={8} height={5} rx={1.5} fill="#1e1b4b" />
      default: // 圆嘴
        return <ellipse cx={cx} cy={my + 1} rx={3} ry={3} fill="#1e1b4b" />
    }
  }

  /* ── 渲染耳朵/天线 ── */
  const renderEars = () => {
    const earW = 7
    const earH = 12
    const leftX = cx - headW / 2 - earW + 2
    const rightX = cx + headW / 2 - 2
    const earY = cy - earH / 2 + 2

    switch (earStyle) {
      case 0: // 圆耳
        return (
          <>
            <rect x={leftX} y={earY} width={earW} height={earH} rx={4} fill={c2} />
            <rect x={rightX} y={earY} width={earW} height={earH} rx={4} fill={c2} />
            <rect x={leftX + 1.5} y={earY + 2} width={earW - 3} height={3} rx={1.5} fill={cLight} opacity={0.5} />
            <rect x={rightX + 1.5} y={earY + 2} width={earW - 3} height={3} rx={1.5} fill={cLight} opacity={0.5} />
          </>
        )
      case 1: // 方耳
        return (
          <>
            <rect x={leftX} y={earY - 2} width={earW + 1} height={earH + 4} rx={2} fill={c2} />
            <rect x={rightX} y={earY - 2} width={earW + 1} height={earH + 4} rx={2} fill={c2} />
          </>
        )
      case 2: // 无耳
        return null
      case 3: // 天线
        return (
          <g className="avatar-antenna">
            <line x1={cx - 8} y1={cy - headH / 2 + 2} x2={cx - 10} y2={cy - headH / 2 - 10}
              stroke={c2} strokeWidth={2.5} strokeLinecap="round" />
            <circle cx={cx - 10} cy={cy - headH / 2 - 11} r={3} fill={accent} className="avatar-antenna-tip" />
            <line x1={cx + 8} y1={cy - headH / 2 + 2} x2={cx + 10} y2={cy - headH / 2 - 10}
              stroke={c2} strokeWidth={2.5} strokeLinecap="round" />
            <circle cx={cx + 10} cy={cy - headH / 2 - 11} r={3} fill={accent} className="avatar-antenna-tip" />
          </g>
        )
      default:
        return null
    }
  }

  /* ── 渲染额头装饰 ── */
  const renderDeco = () => {
    const dy = cy - headH / 2 + 7
    switch (deco) {
      case 1: // 额灯
        return <circle cx={cx} cy={dy} r={3} fill={accent} className="avatar-forehead-light" />
      case 2: // 螺丝
        return (
          <g>
            <circle cx={cx} cy={dy} r={2.5} fill="#555" />
            <line x1={cx - 1.5} y1={dy} x2={cx + 1.5} y2={dy} stroke="#333" strokeWidth={0.8} />
          </g>
        )
      case 3: // 条纹
        return (
          <>
            <line x1={cx - 10} y1={dy} x2={cx - 4} y2={dy} stroke={cLight} strokeWidth={2} strokeLinecap="round" opacity={0.6} />
            <line x1={cx + 4} y1={dy} x2={cx + 10} y2={dy} stroke={cLight} strokeWidth={2} strokeLinecap="round" opacity={0.6} />
          </>
        )
      default:
        return null
    }
  }

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 100 100"
      className="agent-avatar-svg"
      aria-label={name}
    >
      <defs>
        {/* 脸部 3D 渐变 */}
        <linearGradient id={`face-${h}`} x1="0" y1="0" x2="0.3" y2="1">
          <stop offset="0%" stopColor={cLight} />
          <stop offset="50%" stopColor={c1} />
          <stop offset="100%" stopColor={c2} />
        </linearGradient>
        {/* 高光渐变 */}
        <linearGradient id={`hi-${h}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="rgba(255,255,255,0.45)" />
          <stop offset="100%" stopColor="rgba(255,255,255,0)" />
        </linearGradient>
        {/* 阴影 */}
        <filter id={`shadow-${h}`}>
          <feDropShadow dx="0" dy="2" stdDeviation="2.5" floodColor="rgba(0,0,0,0.18)" />
        </filter>
      </defs>

      <g className="avatar-float">
        {/* ── 耳朵（在头部后面） ── */}
        {renderEars()}

        {/* ── 下巴凸起 ── */}
        {chinBump && (
          <ellipse
            cx={cx} cy={cy + headH / 2 - 2}
            rx={headW / 3} ry={6}
            fill={`url(#face-${h})`}
            filter={`url(#shadow-${h})`}
          />
        )}

        {/* ── 头部阴影 ── */}
        <rect
          x={cx - headW / 2} y={cy - headH / 2}
          width={headW} height={headH}
          rx={headRx} ry={headRx}
          fill="rgba(0,0,0,0.1)"
          transform="translate(1.5, 2.5)"
        />

        {/* ── 头部主体（3D 渐变） ── */}
        <rect
          x={cx - headW / 2} y={cy - headH / 2}
          width={headW} height={headH}
          rx={headRx} ry={headRx}
          fill={`url(#face-${h})`}
          stroke={c2}
          strokeWidth={1}
        />

        {/* ── 头顶高光 ── */}
        <rect
          x={cx - headW / 2 + 4} y={cy - headH / 2 + 2}
          width={headW - 8} height={headH * 0.35}
          rx={headRx - 2} ry={headRx - 2}
          fill={`url(#hi-${h})`}
        />

        {/* ── 边缘反光 ── */}
        <rect
          x={cx - headW / 2 + 2} y={cy - headH / 2 + 1}
          width={5} height={headH * 0.5}
          rx={2.5}
          fill="rgba(255,255,255,0.12)"
        />

        {/* ── 眼睛 ── */}
        {renderEye(-9)}
        {renderEye(9)}

        {/* ── 嘴 ── */}
        {renderMouth()}

        {/* ── 腮红 ── */}
        {cheeks && (
          <>
            <ellipse cx={cx - 14} cy={cy + 6} rx={4} ry={2.5} fill="rgba(255,160,160,0.35)" />
            <ellipse cx={cx + 14} cy={cy + 6} rx={4} ry={2.5} fill="rgba(255,160,160,0.35)" />
          </>
        )}

        {/* ── 额头装饰 ── */}
        {renderDeco()}
      </g>
    </svg>
  )
}

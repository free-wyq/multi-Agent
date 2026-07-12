/**
 * SettingsContext — 纯前端偏好的持久化单一真源。
 *
 * 背景：项目此前零前端持久化（无 localStorage / 无 useLocalStorage / 无状态库）。语音朗读
 * 是首个「纯前端偏好」设置（总开关/自动朗读/音色/语速/音量/音调），需建立持久化基座。
 *
 * 设计：仿现有 BusEventContext / SelectionContext 的 createContext + Provider + throw 版消费 hook
 * 模式。localStorage 读写就地内联在此（不单开 useLocalStorage hook 文件——目前仅此一处需持久化）。
 *
 * key 命名空间：ma.tts（单个 JSON 存全部 tts 字段，避免多 key 分散）。
 * useState 惰性初始化读 localStorage（JSON.parse + 字段兜底）；setter 同步 setItem。
 *
 * 挂载位置：App.tsx 的 ConfigProvider 内、BusEventProvider 外——偏好是比群组 WS 更外层的全局态。
 */
import { createContext, useCallback, useContext, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

/** TTS 偏好字段。与 lib/tts.ts SpeakOptions 对齐，多 enabled/autoPlay 两个开关。 */
export interface TtsSettings {
  /** 总开关。false → 整个语音功能禁用（hover 按钮/自动朗读都不触发、标题栏开关灰禁）。 */
  enabled: boolean
  /** 自动朗读新 agent_reply。仅在 enabled=true 时生效。 */
  autoPlay: boolean
  /** 音色 voiceURI（null = 系统默认）。来自 speechSynthesis.getVoices()[i].voiceURI。 */
  voiceURI: string | null
  /** 语速 0.5~2，默认 1。 */
  rate: number
  /** 音量 0~1，默认 1。 */
  volume: number
  /** 音调 0~2，默认 1。 */
  pitch: number
}

/** 默认 TTS 配置（字段兜底 + localStorage 缺字段时合并）。 */
export const DEFAULT_TTS_SETTINGS: TtsSettings = {
  enabled: false,
  autoPlay: false,
  voiceURI: null,
  rate: 1,
  volume: 1,
  pitch: 1,
}

/** localStorage key。单 JSON 存全部字段。 */
const STORAGE_KEY = 'ma.tts'

/** 从 localStorage 读 TTS 偏好（字段缺失用默认补齐，非法 JSON 回落默认）。 */
function loadTts(): TtsSettings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return { ...DEFAULT_TTS_SETTINGS }
    const parsed = JSON.parse(raw) as Partial<TtsSettings>
    return { ...DEFAULT_TTS_SETTINGS, ...parsed }
  } catch {
    return { ...DEFAULT_TTS_SETTINGS }
  }
}

/** 写 TTS 偏好到 localStorage（best-effort，隐私模式/磁盘满静默）。 */
function saveTts(s: TtsSettings): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(s))
  } catch {
    /* 静默：持久化失败不阻断当前会话的内存态使用 */
  }
}

export interface SettingsContextValue {
  /** TTS 偏好（内存态，随 updateTts 更新并同步 localStorage）。 */
  tts: TtsSettings
  /** 合并更新 TTS 偏好（partial 与现态浅合并后同步 localStorage）。 */
  updateTts: (partial: Partial<TtsSettings>) => void
  /** 重置为默认（清 localStorage + 回默认值）。 */
  resetTts: () => void
}

const SettingsContext = createContext<SettingsContextValue | null>(null)

export interface SettingsProviderProps {
  children: ReactNode
}

export function SettingsProvider({ children }: SettingsProviderProps) {
  const [tts, setTts] = useState<TtsSettings>(loadTts)

  const updateTts = useCallback((partial: Partial<TtsSettings>) => {
    setTts((prev) => {
      const next = { ...prev, ...partial }
      saveTts(next)
      return next
    })
  }, [])

  const resetTts = useCallback(() => {
    const next = { ...DEFAULT_TTS_SETTINGS }
    saveTts(next)
    setTts(next)
  }, [])

  const value = useMemo<SettingsContextValue>(
    () => ({ tts, updateTts, resetTts }),
    [tts, updateTts, resetTts],
  )

  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>
}

/** 消费前端偏好。必须在 <SettingsProvider> 内使用——裸用即 throw，尽早暴露接线错误。 */
export function useSettings(): SettingsContextValue {
  const ctx = useContext(SettingsContext)
  if (!ctx) {
    throw new Error('useSettings 必须在 <SettingsProvider> 内使用')
  }
  return ctx
}

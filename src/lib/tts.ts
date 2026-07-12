/**
 * tts.ts — Web Speech API 封装（纯函数，无 React 依赖）。
 *
 * 纯前端 TTS：window.speechSynthesis + SpeechSynthesisUtterance。零 npm 依赖、零后端改动，
 * Electron renderer 进程原生可用。音色/语速/音量/音调由调用方传入，本模块只负责把文本变成声音。
 *
 * 设计为纯函数（非 hook）：voices 异步加载、speak/cancel 是命令式 API，包在函数里更直观，
 * 也方便 useTts 与其它地方复用。React 侧的状态订阅（voices/speaking）在 useTts 里用 hook 接。
 */

/** TTS 配置：与 SettingsContext 的 TtsSettings 字段对齐（这里只取引擎需要的那几个）。 */
export interface SpeakOptions {
  /** 音色 voiceURI（null = 系统默认）。来自 speechSynthesis.getVoices()[i].voiceURI。 */
  voiceURI?: string | null
  /** 语速 0.5~2，默认 1。 */
  rate?: number
  /** 音量 0~1，默认 1。 */
  volume?: number
  /** 音调 0~2，默认 1。 */
  pitch?: number
}

/** 是否支持 Web Speech API（speechSynthesis 在 window 上且为对象）。
 *  Electron renderer / Chromium 系浏览器恒为 true；部分精简 webview 可能无语音引擎。 */
export function isTtsSupported(): boolean {
  return typeof window !== 'undefined' && 'speechSynthesis' in window
}

/**
 * 列出可用音色，中文优先排序。
 *
 * voices 异步加载——首次调用可能返回空数组，需配合 subscribeVoices 监听 onvoiceschanged 后再取。
 * 中文优先：lang 以 'zh' 开头的排前，便于用户在中文对话场景直接选到中文音色。
 */
export function listVoices(): SpeechSynthesisVoice[] {
  if (!isTtsSupported()) return []
  const voices = window.speechSynthesis.getVoices()
  return [...voices].sort((a, b) => {
    const az = a.lang?.toLowerCase().startsWith('zh') ? 0 : 1
    const bz = b.lang?.toLowerCase().startsWith('zh') ? 0 : 1
    if (az !== bz) return az - bz
    return a.name.localeCompare(b.name)
  })
}

/**
 * 订阅音色列表变化（voices 异步加载完成后触发）。
 *
 * 返回取消订阅函数。Electron/Linux 上 voices 常在页面加载后延迟填充，组件用 useState 初值空 + 监听
 * onvoiceschanged 刷新。注意该事件在某些浏览器只触发一次，故组件挂载时也应主动 listVoices() 兜底。
 */
export function subscribeVoices(cb: () => void): () => void {
  if (!isTtsSupported()) return () => {}
  const synth = window.speechSynthesis
  const handler = () => cb()
  synth.addEventListener?.('voiceschanged', handler)
  // 兜底：部分实现无 addEventListener，回落到 onvoiceschanged 赋值
  if (!synth.addEventListener) {
    synth.onvoiceschanged = handler
    return () => {
      synth.onvoiceschanged = null
    }
  }
  return () => {
    synth.removeEventListener?.('voiceschanged', handler)
  }
}

/**
 * 朗读文本。先 cancel 再 speak——新朗读打断旧的，符合「读最新一条」直觉（连续多条回复只读最后一条）。
 *
 * onstart/onend 回调用于跟踪 speaking 态（按钮变停止图标）。cancel 会触发当前 utterance 的 onend，
 * 故切到新朗读时旧 onend 也会被调用——useTts 用 utterance 引用比对过滤。
 */
export function speak(text: string, opts: SpeakOptions = {}, onState?: { onStart?: () => void; onEnd?: () => void }): void {
  if (!isTtsSupported() || !text) return
  const synth = window.speechSynthesis
  synth.cancel()
  const u = new SpeechSynthesisUtterance(text)
  if (opts.voiceURI) {
    const voice = synth.getVoices().find((v) => v.voiceURI === opts.voiceURI)
    if (voice) u.voice = voice
  }
  if (opts.rate != null) u.rate = opts.rate
  if (opts.volume != null) u.volume = opts.volume
  if (opts.pitch != null) u.pitch = opts.pitch
  if (onState?.onStart) u.onstart = () => onState.onStart!()
  if (onState?.onEnd) u.onend = () => onState.onEnd!()
  synth.speak(u)
}

/** 停止当前朗读（cancel）。 */
export function stopSpeak(): void {
  if (!isTtsSupported()) return
  window.speechSynthesis.cancel()
}

/**
 * stt.ts — Web Speech API SpeechRecognition 封装（纯函数，无 React 依赖）。
 *
 * 与 lib/tts.ts 对称：TTS 是输出侧（文本→声音），STT 是输入侧（声音→文本）。
 * 纯前端语音识别：window.SpeechRecognition / window.webkitSpeechRecognition。
 * 零 npm 依赖、零后端改动，Electron renderer / Chromium 系原生可用。
 *
 * Chromium 实现走 webkit 前缀（标准 SpeechRecognition 名义存在但实现仍是前缀那套），
 * 故工厂同时探测两者。lang/interimResults/continuous 由调用方传入，本模块只负责
 * 把麦克风音频变成文本结果回调。
 *
 * 设计为纯函数（非 hook）：SpeechRecognition 是命令式事件 API，包在工厂函数里更直观，
 * 也方便 useStt 与其它地方复用。React 侧的状态订阅（listening/interim）在 useStt 里
 * 用 hook 接，与 tts.ts → useTts 的分层一致。
 */

/** STT 配置：lang 与识别模式。默认 zh-CN 单句模式 + interim 实时回填。 */
export interface RecognizeOptions {
  /** 识别语言 BCP-47，默认 'zh-CN'。 */
  lang?: string
  /** 是否连续识别（true=持续听直到主动停 / false=单句模式识别完自动停）。默认 false。
   *  单句模式更符合「说一句填进输入框」的直觉，且能耗/权限感知更低。 */
  continuous?: boolean
  /** 是否返回 interim（中间）结果。true 时 onInterim 实时回填接近实时转写体验。默认 true。 */
  interimResults?: boolean
}

/** 识别结果回调集合。 */
export interface RecognizeHandlers {
  /** interim（中间）结果——用户还在说、未定稿的实时文本。用于输入框实时预览。 */
  onInterim?: (text: string) => void
  /** final（定稿）结果——一句话说完的最终文本。用于追加到输入框（不自动发送）。 */
  onResult?: (text: string) => void
  /** 出错（含权限拒绝 not-allowed / no-speech 等）。 */
  onError?: (error: string, message: string) => void
  /** 识别结束（自然停 or 主动 stop 触发）。 */
  onEnd?: () => void
}

/**
 * 是否支持 Web Speech API SpeechRecognition。
 * Electron renderer / Chromium 系恒为 true（走 webkit 前缀）；Firefox 等不支持。
 */
export function isSttSupported(): boolean {
  if (typeof window === 'undefined') return false
  return 'SpeechRecognition' in window || 'webkitSpeechRecognition' in window
}

/** 取 SpeechRecognition 构造器（标准名优先，回落 webkit 前缀）。 */
function getRecognitionCtor(): { new (): any } | null {
  if (typeof window === 'undefined') return null
  const w = window as any
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null
}

/**
 * 创建一个 SpeechRecognition 实例（不自动启动）。
 *
 * 返回 null = 当前环境不支持（调用方应灰禁 UI）。handlers 在返回的实例上挂 onresult/onerror/onend，
 * 由 start() 触发识别。interim 累积：onresult 事件可能多次触发，每次 results 里 isFinal=false 的是
 * interim（实时刷新），isFinal=true 的是定稿（累积追加）。
 */
export function createRecognition(
  opts: RecognizeOptions = {},
  handlers: RecognizeHandlers = {},
): any | null {
  const Ctor = getRecognitionCtor()
  if (!Ctor) return null

  const rec = new Ctor()
  rec.lang = opts.lang ?? 'zh-CN'
  rec.continuous = opts.continuous ?? false
  rec.interimResults = opts.interimResults ?? true
  rec.maxAlternatives = 1

  // 累积 final 文本：单句模式下一次 onresult 可能含多个 final segment，拼成一句。
  let finalText = ''

  rec.onresult = (event: any) => {
    let interim = ''
    // event.results 是类数组，遍历到 event.resultIndex 之后的变化项。
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const result = event.results[i]
      const transcript = result[0]?.transcript ?? ''
      if (result.isFinal) {
        finalText += transcript
      } else {
        interim += transcript
      }
    }
    if (interim && handlers.onInterim) handlers.onInterim(interim)
    if (finalText && handlers.onResult) handlers.onResult(finalText.trim())
  }

  rec.onerror = (event: any) => {
    // 常见 error：not-allowed（拒绝麦克风权限）/ no-speech（没检测到语音）/ aborted（主动中止）。
    const error = event?.error ?? 'unknown'
    const msg = event?.message ?? error
    if (handlers.onError) handlers.onError(error, msg)
  }

  rec.onend = () => {
    if (handlers.onEnd) handlers.onEnd()
  }

  return rec
}

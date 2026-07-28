/// <reference types="vite/client" />

/* ── Web Speech API: SpeechRecognition 主构造器 ambient 声明 ──
 * 背景：TS lib.dom.d.ts（~6.0.2）声明了 SpeechRecognition 的子类型
 * （SpeechRecognitionEvent / SpeechRecognitionResult / SpeechRecognitionResultList /
 *  SpeechRecognitionAlternative / SpeechRecognitionErrorEvent），但缺少主构造器
 * interface SpeechRecognition 与 declare var SpeechRecognition / webkitSpeechRecognition。
 * Chromium 实现走 webkitSpeechRecognition 前缀。这里补最小 ambient 声明，使 lib/stt.ts
 * 能以类型安全方式 new SpeechRecognition()。
 * 字段用 any 兜底——事件子类型已由 lib.dom 提供，构造器本体只需保证可 new + 可挂
 * onresult/onerror/onend/lang/continuous/interimResults/maxAlternatives + start()/stop()/abort()。 */
interface SpeechRecognition extends EventTarget {
  lang: string
  continuous: boolean
  interimResults: boolean
  maxAlternatives: number
  onresult: ((event: SpeechRecognitionEvent) => void) | null
  onerror: ((event: SpeechRecognitionErrorEvent) => void) | null
  onend: ((event: Event) => void) | null
  start(): void
  stop(): void
  abort(): void
}

declare var SpeechRecognition: {
  prototype: SpeechRecognition
  new (): SpeechRecognition
}

declare var webkitSpeechRecognition: {
  prototype: SpeechRecognition
  new (): SpeechRecognition
}

interface Window {
  SpeechRecognition?: { prototype: SpeechRecognition; new (): SpeechRecognition }
  webkitSpeechRecognition?: { prototype: SpeechRecognition; new (): SpeechRecognition }
}

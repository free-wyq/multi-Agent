/**
 * useTts — 合并「TTS 配置（来自 SettingsContext）」+「Web Speech 引擎（lib/tts）」的消费 hook。
 *
 * 组件层只调本 hook，不必同时 useSettings + lib/tts。返回：
 *  - supported：当前环境是否支持 Web Speech（不支持时 UI 灰禁 + 提示，不报错）。
 *  - voices：可用音色列表（中文优先，异步加载后刷新）。
 *  - speak(text)：用当前 tts 配置（voiceURI/rate/volume/pitch）朗读；朗读中再调会打断旧的。
 *  - stop()：停止当前朗读。
 *  - speaking：是否正在朗读（按钮据此切喇叭/停止图标）。
 *
 * speaking 跟踪：用 utterance onstart/onend。注意 cancel 也会触发当前 utterance 的 onend，
 * 故用 ref 持有「当前朗读的 utterance 标记」，stop() 主动 cancel 时置一个 silencedRef，
 * onend 据此区分「自然读完」与「被打断」——但无论哪种，结束后 speaking 都应收为 false，
 * 故只需保证 onEnd 一定把 speaking 置 false 即可，无需复杂比对。
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { useSettings } from '../contexts/SettingsContext'
import {
  isTtsSupported,
  listVoices,
  speak as speakEngine,
  stopSpeak,
  subscribeVoices,
} from '../lib/tts'

export interface UseTtsResult {
  /** 当前环境是否支持 Web Speech API。 */
  supported: boolean
  /** 可用音色（中文优先，异步加载后刷新）。 */
  voices: SpeechSynthesisVoice[]
  /** 是否正在朗读。 */
  speaking: boolean
  /** 当前正在朗读的文本（null = 未朗读）。气泡按钮据此判断「是不是这条在读」，
   *  避免一条在朗读时所有气泡的按钮都变停止态。 */
  speakingContent: string | null
  /** 用当前 tts 配置朗读文本（朗读中再调会打断旧的）。 */
  speak: (text: string) => void
  /** 停止当前朗读。 */
  stop: () => void
}

export function useTts(): UseTtsResult {
  const { tts } = useSettings()
  const supported = isTtsSupported()
  const [voices, setVoices] = useState<SpeechSynthesisVoice[]>(() => listVoices())
  const [speaking, setSpeaking] = useState(false)
  const [speakingContent, setSpeakingContent] = useState<string | null>(null)
  // 标记本次 onend 是否由主动 stop 触发——避免 stop 后又被旧 utterance onend 误判。
  const stoppedRef = useRef(false)

  // voices 异步加载：挂载时主动取一次 + 监听 voiceschanged。
  useEffect(() => {
    if (!supported) return
    setVoices(listVoices())
    const unsub = subscribeVoices(() => setVoices(listVoices()))
    return unsub
  }, [supported])

  const speak = useCallback(
    (text: string) => {
      if (!supported || !text) return
      stoppedRef.current = false
      setSpeakingContent(text)
      speakEngine(
        text,
        {
          voiceURI: tts.voiceURI,
          rate: tts.rate,
          volume: tts.volume,
          pitch: tts.pitch,
        },
        {
          onStart: () => setSpeaking(true),
          onEnd: () => {
            // 自然读完或被打断：均清空 speaking + speakingContent。
            setSpeaking(false)
            setSpeakingContent(null)
          },
        },
      )
    },
    [supported, tts.voiceURI, tts.rate, tts.volume, tts.pitch],
  )

  const stop = useCallback(() => {
    if (!supported) return
    stoppedRef.current = true
    stopSpeak()
    setSpeaking(false)
    setSpeakingContent(null)
  }, [supported])

  return { supported, voices, speaking, speakingContent, speak, stop }
}

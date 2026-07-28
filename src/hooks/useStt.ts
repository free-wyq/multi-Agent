/**
 * useStt — 合并「STT 引擎（lib/stt）」的消费 hook。与 useTts 对称（useTts=输出侧朗读，useStt=输入侧识别）。
 *
 * 组件层只调本 hook，不必直接操作 lib/stt。返回：
 *  - supported：当前环境是否支持 SpeechRecognition（不支持时 UI 灰禁 + 提示，不报错）。
 *  - listening：是否正在录音识别（按钮据此切麦克风/停止图标）。
 *  - interim：当前 interim（中间）文本——用户还在说、未定稿的实时预览，组件拼到输入框末尾展示。
 *  - start()：开始录音识别（再点 / stop 会停）。
 *  - stop()：停止录音识别。
 *  - onResult(cb)：注册最终识别结果回调。cb 收到的 finalText 由组件追加到 chatInput
 *    （**填入输入框待发，不自动发送**——与追问 chip / insertMention 一致，给用户确认机会）。
 *
 * interim 与 final 分离：interim 是临时的（用户还在说会变），只在 listening 期间拼到输入框显示；
 * final 是定稿的，回调里才真正 setChatInput 追加。这样不会把 interim 误存进 chatInput 状态。
 *
 * 卸载时自动 stop + abort，避免录音进程悬挂。
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { createRecognition, isSttSupported } from '../lib/stt'

export interface UseSttResult {
  /** 当前环境是否支持 SpeechRecognition。 */
  supported: boolean
  /** 是否正在录音识别。 */
  listening: boolean
  /** 当前 interim（中间）文本（null/空 = 无 interim）。 */
  interim: string
  /** 开始录音识别。正在听时再调会先停旧的再开（避免多实例）。 */
  start: () => void
  /** 停止录音识别。 */
  stop: () => void
  /** 注册最终识别结果回调（返回取消注册函数）。cb 收到 finalText。 */
  onResult: (cb: (finalText: string) => void) => () => void
}

export function useStt(): UseSttResult {
  const supported = isSttSupported()
  const [listening, setListening] = useState(false)
  const [interim, setInterim] = useState('')

  // 持有当前 recognition 实例——start 创建、stop/abort 销毁、卸载清理。
  const recognitionRef = useRef<any>(null)
  // 最终结果回调注册表（用 ref 持有，避免闭包陈旧；组件 onResult 注册最新 cb）。
  const resultCbRef = useRef<((finalText: string) => void) | null>(null)

  const clearRecognition = useCallback(() => {
    const rec = recognitionRef.current
    if (rec) {
      try {
        rec.onresult = null
        rec.onerror = null
        rec.onend = null
        rec.abort()
      } catch {
        // abort 抛错忽略——best-effort 清理
      }
      recognitionRef.current = null
    }
  }, [])

  const start = useCallback(() => {
    if (!supported) return
    // 已在听则先清理旧的（避免多实例并发抢麦克风）。
    if (recognitionRef.current) clearRecognition()

    const rec = createRecognition(
      // 单句模式 + interim 实时回填：说一句自动停，interim 实时预览，final 追加待发。
      { lang: 'zh-CN', continuous: false, interimResults: true },
      {
        onInterim: (text) => setInterim(text),
        onResult: (text) => {
          // final 定稿——回调组件注册的处理器（追加 chatInput）。
          if (resultCbRef.current) resultCbRef.current(text)
          // final 到达后清空 interim（已并入 final）。
          setInterim('')
        },
        onError: (error) => {
          // not-allowed = 拒绝麦克风权限；no-speech = 没检测到语音（常见，静默处理）。
          // abort 类（主动 stop 触发）不报。其余错误抛给控制台便于排查。
          if (error !== 'no-speech' && error !== 'aborted') {
            // eslint-disable-next-line no-console
            console.warn('[useStt] recognition error:', error)
          }
        },
        onEnd: () => {
          setListening(false)
          setInterim('')
          recognitionRef.current = null
        },
      },
    )
    if (!rec) return
    recognitionRef.current = rec
    setInterim('')
    try {
      rec.start()
      setListening(true)
    } catch {
      // start 可能抛 InvalidStateError（已启动），忽略。
      setListening(false)
    }
  }, [supported, clearRecognition])

  const stop = useCallback(() => {
    const rec = recognitionRef.current
    if (rec) {
      try {
        rec.stop()
      } catch {
        // ignore
      }
    }
    setListening(false)
    setInterim('')
  }, [])

  const onResult = useCallback((cb: (finalText: string) => void) => {
    resultCbRef.current = cb
    return () => {
      if (resultCbRef.current === cb) resultCbRef.current = null
    }
  }, [])

  // 卸载清理——停止录音释放麦克风，避免进程悬挂。
  useEffect(() => {
    return () => {
      clearRecognition()
    }
  }, [clearRecognition])

  return { supported, listening, interim, start, stop, onResult }
}

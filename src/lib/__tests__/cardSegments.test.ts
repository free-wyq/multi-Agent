/**
 * [任务10d] cardSegments 纯函数单测（parseCards / splitContentByCards）。
 *
 * 这两个函数原内联在 ChatMessageBubble.tsx 未导出，无法测；[任务10d] 抽到 lib 后既可单测，
 * 又是后续卡片重构（任务7a 换 antd Card / 任务4 持久化气泡复用切卡）的安全网——重构期契约
 * 由测试锁住，回归即时暴露。
 *
 * 设计单真源：docs/structured-result-card-schema.md §3（线格式）/§5（字段边界）/§6（解析契约）。
 * 后端对齐：backend/llm/card_fragment.py CARD_FRAGMENT_RE 必须与本模块 CARD_RE byte-identical
 * （前后端对「何为一张卡片」判定一致）。本测锁住正则源串防漂移。
 *
 * parseCards 不分支 kind（kv/list/table 解析同路径，渲染才分支），故 kind 维度只在「能否解析」
 * 层面测；splitContentByCards 测切片几何（散文段 + 卡片段交替 + 空段跳过 + 重构不变式）。
 */
import { describe, it, expect } from 'vitest'

import {
  CARD_RE,
  parseCards,
  splitContentByCards,
  type ParsedCard,
  type ContentSegment,
} from '../cardSegments'

// ─── 测试夹具：三种 kind 的合法 card 块（含围栏）──────────────────────────────
const KV_CARD = '```card\n{"icon":"⚙️","title":"配置","kind":"kv","items":[{"label":"k","value":"v"}]}\n```'
const LIST_CARD = '```card\n{"title":"清单","kind":"list","items":["a","b"]}\n```'
const TABLE_CARD = '```card\n{"title":"销量排名","kind":"table","columns":["排名","标题"],"rows":[["1","x"]]}\n```'
// 非法 JSON 块（设计 §6 降级用例）——单引号串避免模板字面量里转义反引号的解析坑
const BAD_CARD = '```card\n坏块\n```'

// ─── CARD_RE 正则源串锁（防漂移——与后端 CARD_FRAGMENT_RE byte-identical）──────────
describe('CARD_RE · 正则源串锁（与后端 card_fragment.py byte-identical）', () => {
  it('源串为 ```card\\s*\\n([\\s\\S]*?)``` 且带 g flag', () => {
    // 锁源串：后端 re.compile(r"```card\s*\n([\s\S]*?)```") 必须与此字符串相等
    expect(CARD_RE.source).toBe('```card\\s*\\n([\\s\\S]*?)```')
    expect(CARD_RE.flags).toBe('g')
  })

  it('matches 三反引号 + card info string + 换行 + 闭合三反引号', () => {
    expect(CARD_RE.test('```card\n{"kind":"kv"}\n```')).toBe(true)
  })

  it('不匹配 ```cards（复数，info string 必须字面 card）', () => {
    // card 后紧跟 s → \s* 匹配空，\n 期望换行但遇 s → 不匹配
    expect(parseCards('```cards\n{"kind":"kv"}\n```')).toHaveLength(0)
  })

  it('不匹配 ```python 等非 card 围栏', () => {
    expect(parseCards('```python\nprint(1)\n```')).toHaveLength(0)
  })

  it('card 后允许多个空白再换行（\\s* 容错）', () => {
    // card + 空格/tab + \n 仍匹配（\s* 贪婪吃空白）
    expect(parseCards('```card \t\n{"kind":"kv"}\n```')).toHaveLength(1)
  })
})

// ─── parseCards · 空内容与无卡片 ─────────────────────────────────────────────
describe('parseCards · 空内容与无卡片', () => {
  it('空串返回 []', () => {
    expect(parseCards('')).toEqual([])
  })

  it('纯散文（无围栏块）返回 []', () => {
    expect(parseCards('今天天气不错，无任何卡片。')).toEqual([])
  })

  it('含普通代码块（非 card info string）不误匹配', () => {
    expect(parseCards('说明如下：\n```json\n{"a":1}\n```\n结束。')).toEqual([])
  })
})

// ─── parseCards · 合法卡片解析 ───────────────────────────────────────────────
describe('parseCards · 合法卡片解析', () => {
  it('单张 table 卡片 → 一个 ParsedCard，json 非 null + raw + 字符区间', () => {
    const content = TABLE_CARD
    const cards = parseCards(content)
    expect(cards).toHaveLength(1)
    const c = cards[0]
    expect(c.json).not.toBeNull()
    expect(c.json?.kind).toBe('table')
    expect(c.json?.title).toBe('销量排名')
    expect(c.raw).toBe(content) // 整个 content 就是这一个 card 块
    expect(c.start).toBe(0)
    expect(c.end).toBe(content.length)
  })

  it('raw === content.slice(start, end)（区间与 raw 自洽）', () => {
    const content = `前散文\n${KV_CARD}\n后散文`
    const c = parseCards(content)[0]
    expect(c.raw).toBe(content.slice(c.start, c.end))
    expect(c.raw).toBe(KV_CARD) // raw 精确是围栏块原文
  })

  it('三种 kind（kv/list/table）解析同路径——都产出 json 非 null', () => {
    for (const card of [KV_CARD, LIST_CARD, TABLE_CARD]) {
      const c = parseCards(card)[0]
      expect(c.json).not.toBeNull()
      expect(c.json).toHaveProperty('kind')
    }
  })

  it('字段缺失的合法 object 仍解析（kind/title 可缺）', () => {
    // 顶层是 object 即合法，缺字段不降级（渲染层容错）
    const c = parseCards('```card\n{}\n```')[0]
    expect(c.json).toEqual({})
    expect(c.json).not.toBeNull()
  })

  it('多张卡片按文档顺序返回', () => {
    const content = `${KV_CARD}\n${LIST_CARD}\n${TABLE_CARD}`
    const cards = parseCards(content)
    expect(cards).toHaveLength(3)
    expect(cards[0].json?.kind).toBe('kv')
    expect(cards[1].json?.kind).toBe('list')
    expect(cards[2].json?.kind).toBe('table')
  })

  it('多卡片 start/end 单调递增且不重叠', () => {
    const content = `散文1\n${KV_CARD}\n中间\n${TABLE_CARD}\n尾散文`
    const cards = parseCards(content)
    expect(cards).toHaveLength(2)
    expect(cards[0].start).toBeLessThan(cards[0].end)
    expect(cards[0].end).toBeLessThanOrEqual(cards[1].start)
    expect(cards[1].start).toBeLessThan(cards[1].end)
  })
})

// ─── parseCards · 优雅降级（设计 §6：非法 JSON 不静默丢弃，保留 raw 走 code 块）────
describe('parseCards · 优雅降级（设计 §6 不静默丢弃）', () => {
  it('非法 JSON → json:null 但保留 raw（降级 code 块渲染）', () => {
    const content = '```card\n这不是合法JSON\n```'
    const cards = parseCards(content)
    expect(cards).toHaveLength(1)
    expect(cards[0].json).toBeNull()
    expect(cards[0].raw).toBe(content) // raw 保留围栏原文
    expect(cards[0].start).toBe(0)
    expect(cards[0].end).toBe(content.length)
  })

  it('顶层 JSON 数组 → json:null（schema 要求顶层 object）', () => {
    const content = '```card\n[1, 2, 3]\n```'
    expect(parseCards(content)[0].json).toBeNull()
  })

  it('顶层 JSON 数字 → json:null', () => {
    expect(parseCards('```card\n42\n```')[0].json).toBeNull()
  })

  it('顶层 JSON 字符串 → json:null', () => {
    expect(parseCards('```card\n"hello"\n```')[0].json).toBeNull()
  })

  it('顶层 JSON null → json:null', () => {
    expect(parseCards('```card\nnull\n```')[0].json).toBeNull()
  })

  it('顶层 JSON 布尔 → json:null', () => {
    expect(parseCards('```card\ntrue\n```')[0].json).toBeNull()
  })

  it('合法卡片与非法块混排——各自独立判定（合法者 json 非 null，非法者 null）', () => {
    const content = `${KV_CARD}\n${BAD_CARD}\n${TABLE_CARD}`
    const cards = parseCards(content)
    expect(cards).toHaveLength(3)
    expect(cards[0].json).not.toBeNull()
    expect(cards[1].json).toBeNull()
    expect(cards[2].json).not.toBeNull()
  })
})

// ─── parseCards · g-flag 共享单例无状态泄漏 ────────────────────────────────────
describe('parseCards · g-flag 共享单例无状态泄漏', () => {
  it('连续调用两次同一输入结果一致（CARD_RE.lastIndex 不残留）', () => {
    const content = `${KV_CARD}\n${TABLE_CARD}`
    const first = parseCards(content)
    const second = parseCards(content)
    expect(second).toEqual(first)
    expect(second).toHaveLength(2)
  })

  it('先解析多卡片再解析少卡片，不多吐（lastIndex 重置生效）', () => {
    // 若 lastIndex 未重置，第二次可能从残留偏移开始漏匹配
    parseCards(`${KV_CARD}\n${LIST_CARD}\n${TABLE_CARD}`)
    const second = parseCards(KV_CARD)
    expect(second).toHaveLength(1)
  })

  it('先解析少卡片再解析多卡片，不少吐', () => {
    parseCards(KV_CARD)
    const second = parseCards(`${KV_CARD}\n${LIST_CARD}\n${TABLE_CARD}`)
    expect(second).toHaveLength(3)
  })
})

// ─── splitContentByCards · 切片几何 ───────────────────────────────────────────
describe('splitContentByCards · 切片几何', () => {
  it('空内容 → 单段 text（透传空串）', () => {
    expect(splitContentByCards('')).toEqual([{ type: 'text', text: '' }])
  })

  it('纯散文（无卡片）→ 单段 text 透传', () => {
    const prose = '今天天气不错，无任何卡片。'
    expect(splitContentByCards(prose)).toEqual([{ type: 'text', text: prose }])
  })

  it('单张卡片无散文 → 仅卡片段', () => {
    const segs = splitContentByCards(KV_CARD)
    expect(segs).toHaveLength(1)
    expect(segs[0].type).toBe('card')
    if (segs[0].type === 'card') {
      expect(segs[0].card.json?.kind).toBe('kv')
    }
  })

  it('散文 + 卡片 → [text, card]（含分隔换行保留在散文尾）', () => {
    const segs = splitContentByCards(`前散文\n${KV_CARD}`)
    expect(segs).toHaveLength(2)
    expect(segs[0]).toEqual({ type: 'text', text: '前散文\n' })
    expect(segs[1].type).toBe('card')
  })

  it('卡片 + 散文 → [card, text]（含分隔换行保留在散文首）', () => {
    const segs = splitContentByCards(`${TABLE_CARD}\n后散文`)
    expect(segs).toHaveLength(2)
    expect(segs[0].type).toBe('card')
    expect(segs[1]).toEqual({ type: 'text', text: '\n后散文' })
  })

  it('散文 + 卡片 + 散文 → [text, card, text]（换行随相邻散文保留）', () => {
    const segs = splitContentByCards(`前散文\n${LIST_CARD}\n后散文`)
    expect(segs).toHaveLength(3)
    expect(segs[0]).toEqual({ type: 'text', text: '前散文\n' })
    expect(segs[1].type).toBe('card')
    expect(segs[2]).toEqual({ type: 'text', text: '\n后散文' })
  })

  it('多卡片 + 中间散文 → [card, text, card]（中间段含前后换行）', () => {
    const segs = splitContentByCards(`${KV_CARD}\n中间散文\n${TABLE_CARD}`)
    expect(segs).toHaveLength(3)
    expect(segs[0].type).toBe('card')
    expect(segs[1]).toEqual({ type: 'text', text: '\n中间散文\n' })
    expect(segs[2].type).toBe('card')
  })

  it('相邻卡片（无散文间隔）→ [card, card]（空 text 段跳过）', () => {
    const segs = splitContentByCards(`${KV_CARD}${TABLE_CARD}`)
    expect(segs).toHaveLength(2)
    expect(segs.every((s) => s.type === 'card')).toBe(true)
  })

  it('卡片在 content 最开头 → 不产空首段 text', () => {
    const segs = splitContentByCards(`${KV_CARD}尾部散文`)
    expect(segs).toHaveLength(2)
    expect(segs[0].type).toBe('card')
    expect(segs[1]).toEqual({ type: 'text', text: '尾部散文' })
  })

  it('卡片在 content 最末尾 → 不产空尾段 text', () => {
    const segs = splitContentByCards(`头部散文${KV_CARD}`)
    expect(segs).toHaveLength(2)
    expect(segs[0]).toEqual({ type: 'text', text: '头部散文' })
    expect(segs[1].type).toBe('card')
  })

  it('非法 JSON 卡片仍作为 card 段保留（降级由渲染层处理，切片不丢）', () => {
    const segs = splitContentByCards(`前散文\n${BAD_CARD}\n后散文`)
    expect(segs).toHaveLength(3)
    expect(segs[1].type).toBe('card')
    if (segs[1].type === 'card') {
      expect(segs[1].card.json).toBeNull() // 降级标记
    }
  })
})

// ─── splitContentByCards · 重构不变式（强性质测试）──────────────────────────────
describe('splitContentByCards · 重构不变式', () => {
  // 不变式：把 text 段的 text 与 card 段的 card.raw 按顺序拼接，必须还原原 content。
  // 这锁住「切片无丢失/无重叠/无错位」——任何切片 bug 都会破坏拼接 == content。
  const reconstruct = (segs: ContentSegment[]): string =>
    segs.map((s) => (s.type === 'text' ? s.text : s.card.raw)).join('')

  it('单卡片无散文：拼接 == content', () => {
    const content = KV_CARD
    expect(reconstruct(splitContentByCards(content))).toBe(content)
  })

  it('散文 + 卡片 + 散文：拼接 == content', () => {
    const content = `前散文\n${KV_CARD}\n后散文`
    expect(reconstruct(splitContentByCards(content))).toBe(content)
  })

  it('多卡片 + 多段散文：拼接 == content', () => {
    const content = `开头\n${KV_CARD}\n中间1\n${LIST_CARD}\n中间2\n${TABLE_CARD}\n结尾`
    expect(reconstruct(splitContentByCards(content))).toBe(content)
  })

  it('含非法 JSON 块的复杂内容：拼接 == content（降级块也参与切片）', () => {
    const content = `散文A\n${KV_CARD}\n散文B\n${BAD_CARD}\n散文C`
    expect(reconstruct(splitContentByCards(content))).toBe(content)
  })

  it('相邻卡片无间隔：拼接 == content', () => {
    const content = `${KV_CARD}${LIST_CARD}${TABLE_CARD}`
    expect(reconstruct(splitContentByCards(content))).toBe(content)
  })
})

// ─── ParsedCard / ContentSegment 类型导出可达性（编译期保证，运行期占位）──────────
describe('类型导出可达性', () => {
  it('ParsedCard / ContentSegment 类型可被引用（编译期锁，运行期 no-op）', () => {
    // 这两个 type 仅用于类型层；运行期断言它们对应的运行值结构正确即可
    const c: ParsedCard = { json: null, raw: '', start: 0, end: 0 }
    const s: ContentSegment = { type: 'text', text: '' }
    expect(c.json).toBeNull()
    expect(s.type).toBe('text')
  })
})

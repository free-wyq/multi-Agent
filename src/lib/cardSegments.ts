/**
 * 需求2-前端：结构化结果卡片段（纯函数抽取自 `ChatMessageBubble.tsx`）。
 *
 * 抽离目的（[任务10d]）：`parseCards` / `splitContentByCards` 是无副作用纯函数（不触 DOM、不
 * 依赖 React/antd），原本内联在组件文件里无法单测（未导出）。抽到 lib 后既可单测，又作为后续
 * 卡片重构（任务7a 手写卡换 antd Card / 任务4 持久化气泡复用切卡逻辑）的安全网——重构期契约
 * 由测试锁住，回归即时暴露。
 *
 * 设计单真源：``docs/structured-result-card-schema.md`` §3/§6；与后端
 * ``backend/llm/card_fragment.py CARD_FRAGMENT_RE`` byte-identical（同源正则，前后端对「何为一张
 * 卡片」判定一致）。本模块是卡片段「解析+切片」的单一真源；``followUpSuggestions.ts`` 的卡片
 * title/kind 扫描是「找标题造追问」的另一关注点，独立定义轻量正则，不强依赖本模块（见其头注）。
 *
 * 线格式：markdown fenced code block + ``card`` info string 包 JSON payload。
 *   ```card
 *   {"icon":"🔥","title":"百度热搜 Top 5","kind":"table",
 *    "columns":["排名","标题","热度"],
 *    "rows":[["1","神舟二十号","9821"],...]}
 *   ```
 * 卡片是 ``content`` 子串，走 _unified_reply → persist_agent_reply → emit_message_added
 * 全程透传（不改 DB / 不加事件）。本模块只负责「把 content 里的 ```card``` 块切出来 + 解析 JSON
 * payload + 剩余散文按段返回」；渲染（Descriptions/List/Table 降级）仍在组件层。
 */

/** 与后端 `llm.card_fragment.CARD_FRAGMENT_RE` byte-identical 的 card 围栏正则。
 *  `g` flag 用于 matchAll 全局扫描；调用前需手动重置 lastIndex（matchAll 自管，不重置）。 */
export const CARD_RE = /```card\s*\n([\s\S]*?)```/g

/** card payload JSON schema（设计单真源 docs/structured-result-card-schema.md §4）。
 *  字段全 optional——前端容错（缺字段当空/降级），后端提示词负责产出合法结构。
 *  值类型统一 string（数字也 stringify，如 "9821" 而非 9821——避免渲染时数字不显示/排序歧义）。
 *  - kind=kv:    items: Array<{label:string, value:string}>
 *  - kind=list:  items: Array<string>
 *  - kind=table: columns: Array<string>, rows: Array<Array<string>>
 *  未知 kind → 整块降级为普通代码块（不崩，显示原始 JSON）。 */
export interface CardPayload {
  icon?: string
  title?: string
  kind?: 'kv' | 'list' | 'table' | string
  items?: Array<{ label: string; value: string }> | Array<string> | unknown
  columns?: string[] | unknown
  rows?: Array<Array<string>> | unknown
}

/** 解析 content，返回各 card 块的 payload + 字符区间。
 *  非法 JSON 的块：按设计 §6「降级为普通代码块渲染，不静默丢弃」——返回 raw 字段（原 ```card...```
 *  原文），渲染时当普通 code 块走（让用户看到原始 JSON 便于调提示词，而非吞掉）。
 *
 *  对齐后端 `extract_card_payloads` 的优雅降级：后端 skip 非法 JSON 块（不 surface 为卡片），
 *  前端则把非法块保留为普通代码块（同一语义的两种表现——都不当卡片解析，都不崩）。 */
export interface ParsedCard {
  /** 解析成功的合法 payload（json 非 null）；失败块为 null（用 raw 走 code 块降级）。 */
  json: CardPayload | null
  /** 解析失败时保留的原 ```card...``` 原文（含围栏），降级为普通 code 块显示。 */
  raw: string
  /** content 内字符区间 [start, end)——用于切片剔除卡片段、剩余走散文渲染。 */
  start: number
  end: number
}

/** 解析 content，返回各 ```card``` 块的 payload + 字符区间。
 *  纯函数：无 DOM / 无 React 依赖；非法 JSON 与顶层非 object 块降级为 `{ json: null }`
 *  （保留 raw，渲染层走 code 块降级，不静默丢弃——设计 §6 契约）。
 *  matchAll 自管 lastIndex，但 CARD_RE 带 g flag 是模块级共享单例——matchAll 内部
 *  用副本迭代不会污染外部，仍显式 reset 防被前次 exec 残留。 */
export function parseCards(content: string): ParsedCard[] {
  const out: ParsedCard[] = []
  if (!content) return out
  CARD_RE.lastIndex = 0
  for (const m of content.matchAll(CARD_RE)) {
    const raw = m[0]
    const start = m.index ?? 0
    const end = start + raw.length
    try {
      const json = JSON.parse(m[1]) as CardPayload
      // 顶层非 object（如裸 JSON 数组/数字）→ 降级 code 块（schema 要求顶层 object）
      if (typeof json !== 'object' || json === null || Array.isArray(json)) {
        out.push({ json: null, raw, start, end })
      } else {
        out.push({ json, raw, start, end })
      }
    } catch {
      // 非法 JSON：降级为普通代码块（不静默丢弃——设计 §6 契约）
      out.push({ json: null, raw, start, end })
    }
  }
  return out
}

/** 把 content 切成「散文段 + 卡片段」交替列表（按出现顺序）。
 *  卡片块在 content 内的字符区间被剔除，剩余片段按散文渲染；卡片插回原位置。
 *  设计 §3：worker 可在回复里穿插任意段散文 + 多张卡片，前端按出现顺序渲染。
 *  无卡片 → 单段 text（原 content 透传）；空散文段跳过（不产空 text 段）。 */
export type ContentSegment =
  | { type: 'text'; text: string }
  | { type: 'card'; card: ParsedCard }

export function splitContentByCards(content: string): ContentSegment[] {
  const cards = parseCards(content)
  if (cards.length === 0) return [{ type: 'text', text: content }]
  const segs: ContentSegment[] = []
  let cursor = 0
  for (const card of cards) {
    // 卡片前的散文段（可能为空串——跳过空段避免渲染空白）
    if (card.start > cursor) {
      const text = content.slice(cursor, card.start)
      if (text) segs.push({ type: 'text', text })
    }
    segs.push({ type: 'card', card })
    cursor = card.end
  }
  // 最后一个卡片之后的尾部散文
  if (cursor < content.length) {
    const text = content.slice(cursor)
    if (text) segs.push({ type: 'text', text })
  }
  return segs
}

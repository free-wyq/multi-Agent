# 结构化结果卡片段 schema（需求2 设计契约）

> 单一真源：worker LLM 在 `content` 里输出的结构化结果卡片格式契约。
> 锁定任务 `[需求2-设计]`。后端提示词/解析见 `[需求2-后端]`，前端渲染见 `[需求2-前端]`。
> 回归用例：百度今日热搜 demo（[[chat-card-design-todo-2026-07-26]] 场景1）。

## 1. 设计目标

让 worker 的最终回复能内嵌**结构化结果卡**（标题+图标+键值表/列表/表格），渲染成 AntD
`Card`/`Descriptions`/`Table`/`List`，而不是把「排名/热度/标题」挤成一坨纯文本。典型场景：

- 百度热搜 Top30 榜单（排名+标题+热度值+链接 → `table`）
- 天气概况（气温/风力/降水 → `kv`）
- 穿衣建议（bullet 项 → `list`）
- 任何「字段固定、可表格化」的产出

## 2. 约束与决策

- **不引入新事件类型、不改 DB schema、不加新列**：卡片数据是 `content` 文本的一部分，
  经现有 `_unified_reply` → `persist_agent_reply` → `emit_message_added` 全程透传。
  `data` 字段仍只载流式 run-stats（`reply_id/elapsed_ms/tokens/...`），不塞卡片。
  理由：卡片是「展示层增强」，持久化层无感；reload/重连后 `messageApi.listByGroup`
  取回的 `content` 仍带卡片标记块，前端解析渲染即可，无需额外反序列化路径。
- **用 markdown fenced code block + `card` info string 包 JSON**，而非裸 JSON 或
  自创 XML 标签。理由：(a) markdown 原生——前端不解析时降级成普通代码块（可读、不破版），
  不会出现裸 JSON 一坨乱码；(b) 比 XML 标签短、转义少；(c) 解析正则简单稳定
  （`/```card\s*\n([\s\S]*?)```/g`）；(d) 与现有 `content` 渲染链（markdown 文本）天然兼容，
  卡片块外的散文仍按普通文本渲染。
- **卡片在 `content` 中的位置**：worker 可在回复里穿插任意段散文 + 多张卡片。前端按出现顺序
  渲染：散文段 → 卡片 → 散文段 → 卡片……。卡片块本身不计入「正文摘要」（截断预览时跳过 card 块）。
- **不强制每条回复都出卡片**：仅当产出 genuinely 结构化（榜单/表格/固定字段键值）时才用；
  纯散文回复（写文章、翻译、闲聊）照常纯文本，不强转卡片。提示词须明示这条边界，避免 LLM
  把所有回复都套进卡片（过度结构化反而难读）。

## 3. 线格式（wire format）

在 `content` 文本中插入：

````
```card
{
  "icon": "🌤️",
  "title": "北京·明天 天气概况",
  "kind": "kv",
  "items": [
    {"label": "气温", "value": "12°C ~ 22°C"},
    {"label": "风力", "value": "北风 3级"},
    {"label": "降水", "value": "无"}
  ]
}
```
````

- 围栏语言标记固定为 `card`（小写，不可变）。
- 围栏内是单个 JSON 对象（`JSON.parse` 须能解析）。
- 一条回复可含 0..N 个 `card` 块；块与块之间可夹散文。
- JSON 须标准转义（值里的 `"`、换行按 JSON 规则转义）；围栏三个反引号不会出现在 JSON 内，
  故不会与围栏冲突。

## 4. payload JSON schema

```jsonc
{
  "icon":  "string (emoji 或字符，渲染在标题前；可空串)",
  "title": "string (卡片标题；可空串表示无标题卡)",
  "kind":  "kv | list | table",     // 三种卡片形态
  // kind=kv:键值表(AntD Descriptions)
  "items": [{"label": "string", "value": "string"}],
  // kind=list:bullet 列表(AntD List)
  "items": ["string"],
  // kind=table:表格(AntD Table,带表头)
  "columns": ["string"],
  "rows":    [["string"]]
}
```

### 4.1 `kind=kv` —— 键值表（Descriptions）

`items: [{label, value}]`。渲染：AntD `Descriptions size="small" column=1`，
每项一行 label 左 value 右。适用：天气概况、配置摘要、单实体属性。

```json
{"icon":"🌤️","title":"北京·明天 天气概况","kind":"kv","items":[
  {"label":"气温","value":"12°C ~ 22°C"},
  {"label":"风力","value":"北风 3级"},
  {"label":"降水","value":"无"}
]}
```

### 4.2 `kind=list` —— 列表（List）

`items: [string]`。渲染：AntD `List` 竖排 bullet，每项前一个 `•`。适用：穿衣建议、
待办清单、要点罗列。

```json
{"icon":"💡","title":"穿衣建议","kind":"list","items":[
  "长袖+薄外套",
  "早晚偏凉加件风衣",
  "无需雨具"
]}
```

### 4.3 `kind=table` —— 表格（Table）

`columns: [string]`（表头）+ `rows: [[string]]`（每行一个数组，长度须等于 columns 长度）。
渲染：AntD `Table size="small" pagination=false`，列宽自适应。适用：热搜榜单、对比表、
多行多列数据。**百度热搜回归用例主用此 kind**。

```json
{"icon":"🔥","title":"百度热搜 Top 5","kind":"table",
 "columns":["排名","标题","热度"],
 "rows":[
   ["1","神舟二十号成功对接","9821"],
   ["2","北方多地降温","8740"],
   ["3","AI 新突破","7612"],
   ["4","某赛事夺冠","6450"],
   ["5","城市新规落地","5310"]
 ]}
```

## 5. 字段语义与边界

| 字段 | 类型 | 必填 | 缺省 | 语义 |
|---|---|---|---|---|
| `icon` | string | 否 | `""` | 标题前缀图标，emoji 或单字符。空串=无图标 |
| `title` | string | 否 | `""` | 卡片标题。空串=无标题卡（仍渲染 body） |
| `kind` | enum | 是 | — | 卡片形态，决定 body 渲染分支 |
| `items` | array | kv/list 必填 | — | kv 为 `[{label,value}]`，list 为 `[string]` |
| `columns` | array | table 必填 | — | 表头字符串数组 |
| `rows` | array | table 必填 | — | 行数组，每行 string 数组，长度=columns 长度 |

- **不支持的 `kind` 值**：前端按「未知 kind → 整块降级为普通代码块」渲染（不崩，显示原始 JSON）。
- **字段缺失/类型错**：前端解析时容错——`items` 非数组则当空；`rows` 行长≠columns 则截断补空。
  容错在前端做（[需求2-前端]），后端提示词负责产出合法结构。
- **值类型**：所有 `label`/`value`/`columns`/`rows` 元素统一 **string**。数字也 stringify
  （如 `"9821"` 而非 `9821`），避免前端渲染时 `9821` 不显示或排序歧义。后端提示词须强调这点。
- **`link` 字段（热搜链接）**：本契约 v1 **不内置** href 字段（避免渲染分支膨胀）。热搜场景
  的「百度搜索链接」暂作为 `table` 的一列纯文本 URL（前端 v1 不转超链），或并入标题文本。
  若后续需要可点链接，v2 加 `links?: string[]` 与 `rows` 同长对齐——留扩展位，不在 v1 实现。

## 6. 解析契约（前端 [需求2-前端] 实现依据）

```ts
const CARD_RE = /```card\s*\n([\s\S]*?)```/g
function parseCards(content: string): Array<{json: CardPayload, start: number, end: number}> {
  const out = []
  for (const m of content.matchAll(CARD_RE)) {
    try { out.push({ json: JSON.parse(m[1]), start: m.index, end: m.index + m[0].length }) }
    catch { /* 非法 JSON：前端降级为普通代码块渲染（不抛） */ }
  }
  return out
}
```

- 卡片块在 `content` 中的字符区间 `[start, end)` 被剔除，剩余片段按散文渲染，
  卡片按原顺序插回对应位置。
- 解析失败的块 **保留为普通 ` ``` ` 代码块** 渲染（用户能看到原始 JSON，便于调试提示词），
  不静默丢弃。

## 7. 后端契约（[需求2-后端] 实现依据）

- **提示词注入**：worker brain prompt（`backend/llm/prompts.py build_brain_prompt`）
  末尾追加一段「结构化卡片输出契约」：当产出为榜单/表格/固定字段键值时，用 ` ```card ` 围栏
  输出 JSON 卡片段；纯散文（写作、翻译、闲聊）照常纯文本，不强套卡片。附 3 种 kind 最小示例。
- **不改 `_unified_reply` / `persist_agent_reply` 签名**：卡片是 `content` 子串，走现有透传。
  `data` 仍只载 run-stats，不混卡片。
- **execute 路径**：`registry._run_worker_task` 的 announce（`任务完成 🎉` 前缀）是模板文本，
  不含卡片。若 worker 在 execute 路径的最终输出里自带了 card 块（LLM 产物），透传到 `content`
  即可——announce 模板 `f"任务完成 🎉\n{full_output}"` 已把 `full_output` 拼在后面，card 块随之
  进 `content`，前端解析照常。无需 registry 改动。
- **校验（可选，v1 不强求）**：后端可在 `_unified_reply` 加一个轻量 `card` 块数统计日志
  （`re.findall(CARD_RE, content)` 长度），便于观测 LLM 是否按契约产出卡片。不阻断、不解析。

## 8. 与现有气泡层的分层

气泡四层（[[chat-card-design-todo-2026-07-26]] mockup）：

1. **思考态文案**（reasoning live stream）——已有，不改。
2. **折叠执行日志**（工具调用折叠区，[[codebuddy-style-bubble-process]]）——已有，不改。
3. **结构化结果卡**（本契约）——新增，渲染在正文区，介于散文与产物卡之间。
4. **产物文件下载卡**（`artifactFiles`，ST-06）——已有，不改，位置在正文之下。

本契约只管第 3 层；第 1/2/4 层各自独立，互不干涉。

## 9. 回归用例预期（[需求2-回归]）

百度今日热搜 demo，后端工程师 worker 跑完抓取解析后，最终回复 `content` 须含一张
`kind=table` 的 `card` 块（Top30，columns=["排名","标题","热度"]，rows 30 行）。
前端渲染：标题「🔥 百度热搜 Top 30」+ 表格。同时仍保留产物文件下载卡（`baidu_hotsearch.json`
等）。回归断言：`parseCards(content)` 至少 1 张、`kind==="table"`、`rows.length===30`。

## 10. 版本

- v1（2026-07-26）：kv / list / table 三 kind，markdown `card` 围栏 + JSON payload，
  不改 schema/事件。`link`/`stat`（KPI 数字大卡）留 v2。

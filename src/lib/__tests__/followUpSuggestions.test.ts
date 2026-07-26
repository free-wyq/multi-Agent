/**
 * [任务10c] followUpSuggestions.generateFollowUps 纯函数单测。
 *
 * generateFollowUps 是纯前端规则追问生成（零后端 / 零 LLM / 零 DOM），
 * 内部分层降级：卡片感知 → 关键词感知 → 通用兜底，去重 + 截断到 max。
 * 内部 scanContent 不导出，通过 generateFollowUps 入参（含 ```card 块 + 散文）间接覆盖。
 */
import { describe, it, expect } from 'vitest'

import { generateFollowUps } from '../followUpSuggestions'

describe('generateFollowUps · 空内容与边界', () => {
  it('空串返回 []（防御 !content）', () => {
    expect(generateFollowUps('')).toEqual([])
  })

  it('仅空白返回 []（trim 后空）', () => {
    expect(generateFollowUps('   ')).toEqual([])
    expect(generateFollowUps('\n\t  \n')).toEqual([])
  })

  it('max=0 即便非空内容也返回 []（slice(0,0)）', () => {
    expect(generateFollowUps('操作步骤如下', 0)).toEqual([])
  })

  it('max=1 仅保留首条（截断生效）', () => {
    // 纯兜底场景，首条是「详细说说？」
    expect(generateFollowUps('你好', 1)).toEqual(['详细说说？'])
  })

  it('max=2 截断到 2', () => {
    expect(generateFollowUps('你好', 2)).toEqual(['详细说说？', '举个例子？'])
  })
})

describe('generateFollowUps · 通用兜底（无卡片无关键词命中）', () => {
  it('简短散文仅命中兜底 3 条', () => {
    expect(generateFollowUps('你好')).toEqual([
      '详细说说？',
      '举个例子？',
      '还有什么需要注意的？',
    ])
  })

  it('未命中任何关键词的散文走兜底（无「步骤/对比/注意/原因」等触发词）', () => {
    expect(generateFollowUps('今天天气不错')).toEqual([
      '详细说说？',
      '举个例子？',
      '还有什么需要注意的？',
    ])
  })
})

describe('generateFollowUps · 关键词感知', () => {
  it('含「步骤」→「具体怎么做？」排首位，兜底被截断', () => {
    expect(generateFollowUps('操作步骤如下')).toEqual([
      '具体怎么做？',
      '详细说说？',
      '举个例子？',
    ])
  })

  it('含「方法/流程/怎么」均触发「具体怎么做？」（同问去重只一条）', () => {
    // 多个同义关键词命中同一条追问，去重后只一条
    expect(generateFollowUps('这个方法流程怎么做')).toEqual([
      '具体怎么做？',
      '详细说说？',
      '举个例子？',
    ])
  })

  it('含「对比/区别」→「举个例子？」，且与兜底「举个例子？」去重不重复', () => {
    // 关键词先 push '举个例子？'，兜底再 push 同串被 seen 去重跳过
    const res = generateFollowUps('对比两个方案的区别')
    expect(res).toContain('举个例子？')
    expect(res.filter((s) => s === '举个例子？')).toHaveLength(1)
    // 顺序：关键词「举个例子？」在前，兜底「详细说说？」次之
    expect(res).toEqual(['举个例子？', '详细说说？', '还有什么需要注意的？'])
  })

  it('含「注意/风险」→「还有什么要注意的？」（与兜底「还有什么需要注意的？」不同串都保留）', () => {
    const res = generateFollowUps('注意这里的风险')
    expect(res[0]).toBe('还有什么要注意的？')
    // 「还有什么要注意的？」与「还有什么需要注意的？」字面不同，不去重
    // 截断到 3 后兜底「还有什么需要注意的？」被截掉
    expect(res).toEqual(['还有什么要注意的？', '详细说说？', '举个例子？'])
  })

  it('含「原因/为什么/原理」→「详细解释下原因？」', () => {
    expect(generateFollowUps('解释下原理')).toEqual([
      '详细解释下原因？',
      '详细说说？',
      '举个例子？',
    ])
  })

  it('多类关键词同时命中按声明顺序入列', () => {
    // 步骤 + 对比 + 注意 + 原因 四类全中 → 4 条关键词追问 + 兜底，截断到 3
    const res = generateFollowUps('步骤对比注意原因')
    expect(res).toEqual([
      '具体怎么做？', // 步骤类
      '举个例子？', // 对比类
      '还有什么要注意的？', // 注意类
    ])
    // 第 4 条「详细解释下原因？」被 max=3 截断
    expect(res).not.toContain('详细解释下原因？')
  })

  it('max=10 时四类关键词 + 兜底全保留（去重后共 7 条）', () => {
    const res = generateFollowUps('步骤对比注意原因', 10)
    expect(res).toEqual([
      '具体怎么做？',
      '举个例子？',
      '还有什么要注意的？',
      '详细解释下原因？',
      '详细说说？',
      '还有什么需要注意的？',
    ])
  })
})

describe('generateFollowUps · 卡片感知', () => {
  it('table 卡片有 title → 首条「<title> 的详情？」+「第一名具体是什么？」', () => {
    const content = '```card\n{"title":"销量排名","kind":"table"}\n```'
    expect(generateFollowUps(content)).toEqual([
      '销量排名 的详情？',
      '第一名具体是什么？',
      '详细说说？',
    ])
  })

  it('list 卡片 →「逐条展开说说？」', () => {
    const content = '```card\n{"title":"清单","kind":"list"}\n```'
    expect(generateFollowUps(content)).toEqual([
      '清单 的详情？',
      '逐条展开说说？',
      '详细说说？',
    ])
  })

  it('kv 卡片 →「各项分别详细说明？」', () => {
    const content = '```card\n{"title":"配置","kind":"kv"}\n```'
    expect(generateFollowUps(content)).toEqual([
      '配置 的详情？',
      '各项分别详细说明？',
      '详细说说？',
    ])
  })

  it('卡片有 kind 无 title → 不造 title 详情，仅按 kind 造追问', () => {
    const content = '```card\n{"kind":"table"}\n```'
    expect(generateFollowUps(content)).toEqual([
      '第一名具体是什么？',
      '详细说说？',
      '举个例子？',
    ])
  })

  it('多卡片仅取首个 title 造追问', () => {
    const content = [
      '```card',
      '{"title":"首个标题","kind":"table"}',
      '```',
      '```card',
      '{"title":"第二标题","kind":"list"}',
      '```',
    ].join('\n')
    const res = generateFollowUps(content)
    // cardTitles[0] = '首个标题'；cardKinds 同时含 table + list
    expect(res[0]).toBe('首个标题 的详情？')
    expect(res).toContain('第一名具体是什么？')
    expect(res).toContain('逐条展开说说？')
    expect(res).not.toContain('第二标题 的详情？')
  })

  it('非 JSON 卡片块容错跳过（不抛错，不提取 title/kind）', () => {
    const content = '```card\n这不是合法JSON\n```'
    // 无 title/kind 提取，prose 去卡片段后为空，走纯兜底
    expect(generateFollowUps(content)).toEqual([
      '详细说说？',
      '举个例子？',
      '还有什么需要注意的？',
    ])
  })

  it('卡片内 JSON 的关键词不触发散文关键词扫描（prose 已去卡片块）', () => {
    // title 值含「步骤」但属于 title 拼接，非散文关键词；散文部分为空
    const content = '```card\n{"title":"步骤X","kind":"table"}\n```'
    const res = generateFollowUps(content)
    expect(res).toContain('步骤X 的详情？')
    // 关键词「步骤」不应触发「具体怎么做？」——因 prose 去卡片段后为空
    expect(res).not.toContain('具体怎么做？')
  })
})

describe('generateFollowUps · 卡片 + 散文混合', () => {
  it('卡片 title 详情 + 散文关键词追问 + 兜底，按优先级截断到 3', () => {
    // table 卡片（title+kind）+ 散文含「步骤」
    const content = [
      '```card',
      '{"title":"销量排名","kind":"table"}',
      '```',
      '操作步骤如下。',
    ].join('\n')
    expect(generateFollowUps(content)).toEqual([
      '销量排名 的详情？', // 卡片 title 感知
      '第一名具体是什么？', // table kind 感知
      '具体怎么做？', // 散文「步骤」关键词
    ])
  })

  it('卡片块被移除后散文关键词仍能扫描到', () => {
    // 卡片在前，散文「对比」在后，prose 去卡片段后保留「对比」
    const content = [
      '```card',
      '{"title":"方案","kind":"kv"}',
      '```',
      '对比两方案的优劣。',
    ].join('\n')
    const res = generateFollowUps(content)
    expect(res[0]).toBe('方案 的详情？')
    expect(res).toContain('各项分别详细说明？')
    expect(res).toContain('举个例子？')
  })

  it('真实回复场景：表格卡片 + 散文说明 + 注意事项', () => {
    const content = [
      '本月销售情况如下：',
      '```card',
      '{"title":"销售榜","kind":"table"}',
      '```',
      '注意东部区域有下滑风险。',
    ].join('\n')
    const res = generateFollowUps(content, 5)
    expect(res[0]).toBe('销售榜 的详情？')
    expect(res).toContain('第一名具体是什么？')
    expect(res).toContain('还有什么要注意的？')
    expect(res.length).toBeLessThanOrEqual(5)
  })
})

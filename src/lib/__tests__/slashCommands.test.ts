/**
 * [任务10b] slashCommands 纯函数单测。
 *
 * 覆盖 SLASH_COMMANDS 完整性 / getSlashCommand / matchSlashCommands / parseSlashCommand
 * 四个纯导出（不触 handler 副作用、不依赖 DOM/api）。
 *
 * 注：.task.md 写作 parseSlashInput，实际导出名是 parseSlashCommand，按真实签名测。
 */
import { describe, it, expect } from 'vitest'

import {
  SLASH_COMMANDS,
  getSlashCommand,
  matchSlashCommands,
  parseSlashCommand,
} from '../slashCommands'

// 期望的命令清单（与 SLASH_COMMANDS 注册表声明一一对应，锁序锁量防漏注册）
const EXPECTED_NAMES = [
  'new',
  'model',
  'status',
  'tools',
  'skills',
  'sessions',
  'agent',
  'mcp',
  'schedule',
] as const

describe('SLASH_COMMANDS 注册表完整性', () => {
  it('命令数量与期望一致（9 条）', () => {
    expect(SLASH_COMMANDS).toHaveLength(EXPECTED_NAMES.length)
  })

  it('按注册顺序 name 锁定（防重排/漏注册）', () => {
    expect(SLASH_COMMANDS.map((c) => c.name)).toEqual([...EXPECTED_NAMES])
  })

  it('每条命令 name 唯一（无重复注册）', () => {
    const names = SLASH_COMMANDS.map((c) => c.name)
    expect(new Set(names).size).toBe(names.length)
  })

  it('每条命令元数据字段齐全且非空（name/description/usage），handler 为函数', () => {
    for (const cmd of SLASH_COMMANDS) {
      expect(typeof cmd.name).toBe('string')
      expect(cmd.name.length).toBeGreaterThan(0)
      expect(typeof cmd.description).toBe('string')
      expect(cmd.description.length).toBeGreaterThan(0)
      expect(typeof cmd.usage).toBe('string')
      expect(cmd.usage.length).toBeGreaterThan(0)
      expect(typeof cmd.handler).toBe('function')
    }
  })

  it('name 全小写无前导 /（命名约定）', () => {
    for (const cmd of SLASH_COMMANDS) {
      expect(cmd.name).toBe(cmd.name.toLowerCase())
      expect(cmd.name.startsWith('/')).toBe(false)
    }
  })

  it('usage 均以 / 开头（含前导斜杠）', () => {
    for (const cmd of SLASH_COMMANDS) {
      expect(cmd.usage.startsWith('/')).toBe(true)
    }
  })
})

describe('getSlashCommand', () => {
  it('已注册名精确命中', () => {
    const cmd = getSlashCommand('model')
    expect(cmd).toBeDefined()
    expect(cmd?.name).toBe('model')
  })

  it('命中对象包含完整元数据', () => {
    const cmd = getSlashCommand('new')
    expect(cmd).toMatchObject({
      name: 'new',
      usage: '/new',
    })
    expect(typeof cmd?.handler).toBe('function')
  })

  it('未注册名返回 undefined', () => {
    expect(getSlashCommand('nonexistent')).toBeUndefined()
  })

  it('大小写敏感：大写名不命中（注册表全小写）', () => {
    expect(getSlashCommand('MODEL')).toBeUndefined()
    expect(getSlashCommand('Model')).toBeUndefined()
  })

  it('空串 / 仅空白不命中', () => {
    expect(getSlashCommand('')).toBeUndefined()
    expect(getSlashCommand('   ')).toBeUndefined()
  })

  it('每条注册命令都能被自身 name 查回（遍历一致性）', () => {
    for (const cmd of SLASH_COMMANDS) {
      expect(getSlashCommand(cmd.name)?.name).toBe(cmd.name)
    }
  })
})

describe('matchSlashCommands（自动补全过滤）', () => {
  it('空 query 返回全部命令（输入 / 立即展示完整菜单）', () => {
    expect(matchSlashCommands('')).toEqual(SLASH_COMMANDS)
  })

  it('仅空白 query 也视作空（trim 后空）返回全部', () => {
    expect(matchSlashCommands('   ')).toEqual(SLASH_COMMANDS)
  })

  it('前缀 mo 仅命中 model', () => {
    const res = matchSlashCommands('mo')
    expect(res.map((c) => c.name)).toEqual(['model'])
  })

  it('前缀 s 命中 status/skills/sessions/schedule（保持注册原序）', () => {
    const res = matchSlashCommands('s')
    expect(res.map((c) => c.name)).toEqual([
      'status',
      'skills',
      'sessions',
      'schedule',
    ])
  })

  it('大小写不敏感：MO 与 mo 等价', () => {
    expect(matchSlashCommands('MO')).toEqual(matchSlashCommands('mo'))
  })

  it('完整名作为前缀也能命中自身', () => {
    expect(matchSlashCommands('model').map((c) => c.name)).toEqual(['model'])
  })

  it('无匹配前缀返回空数组', () => {
    expect(matchSlashCommands('xyz')).toEqual([])
  })

  it('返回的是原对象引用（非拷贝，便于上层直接渲染）', () => {
    const res = matchSlashCommands('mo')
    expect(res[0]).toBe(SLASH_COMMANDS[1]) // model 是注册表第 2 条
  })
})

describe('parseSlashCommand（输入行解析）', () => {
  it('标准 /name args 解析', () => {
    expect(parseSlashCommand('/model gpt-4')).toEqual({
      name: 'model',
      args: 'gpt-4',
    })
  })

  it('无参命令 args 为空串', () => {
    expect(parseSlashCommand('/status')).toEqual({ name: 'status', args: '' })
  })

  it('多空格分隔 + 尾随空格 → args trim', () => {
    expect(parseSlashCommand('/model  glm-4.6 ')).toEqual({
      name: 'model',
      args: 'glm-4.6',
    })
  })

  it('参数含多段空格也保留中间内容（仅首尾 trim）', () => {
    expect(parseSlashCommand('/tools some agent')).toEqual({
      name: 'tools',
      args: 'some agent',
    })
  })

  it('行首空格不影响解析（trimStart）', () => {
    expect(parseSlashCommand('  /new')).toEqual({ name: 'new', args: '' })
  })

  it('name 不含前导 /', () => {
    const parsed = parseSlashCommand('/mcp')
    expect(parsed).not.toBeNull()
    expect(parsed?.name).toBe('mcp')
  })

  // —— 以下均为非命令输入，返回 null ——
  it('单独 / 返回 null', () => {
    expect(parseSlashCommand('/')).toBeNull()
  })

  it('/ 后紧跟空格返回 null（/ mcp 非命令）', () => {
    expect(parseSlashCommand('/ mcp')).toBeNull()
  })

  it('非 / 开头返回 null', () => {
    expect(parseSlashCommand('hello /world')).toBeNull()
    expect(parseSlashCommand('plain text')).toBeNull()
  })

  it('空串返回 null', () => {
    expect(parseSlashCommand('')).toBeNull()
  })

  it('仅空白返回 null', () => {
    expect(parseSlashCommand('    ')).toBeNull()
  })

  it('未注册命令名仍按 slash 解析（解析层不校验注册表，查表交给 getSlashCommand）', () => {
    expect(parseSlashCommand('/unknown foo')).toEqual({
      name: 'unknown',
      args: 'foo',
    })
  })
})

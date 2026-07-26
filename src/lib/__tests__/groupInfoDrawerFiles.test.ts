/**
 * [任务14b] GroupInfoDrawer「文件」Tab 纯函数单测。
 *
 * GroupInfoDrawer.tsx 导出的 findSourceTask 是「文件」Tab 反查来源 task 的纯函数
 * （basename 匹配 Task.artifact_path / artifact.files[].path 最后段）。它无 React/DOM/api
 * 依赖，可独立测。组件渲染层（Tabs/FilesTabContent）涉及 antd + api + BusContext，
 * 不在本纯函数测范围（与 slashCommands/cardSegments 同策略：只测纯导出）。
 *
 * 覆盖：
 *  - 主产物 artifact_path basename 命中 → 返该 task
 *  - manifest files[].path basename 命中 → 返该 task
 *  - 子目录路径（out/result.md）basename 对齐 → 命中
 *  - 无任何 task 产出该文件 → 返 null（独立产物）
 *  - 多 task 命中同文件名 → 取 completed_at 最近（倒序优先）
 */
import { describe, it, expect } from 'vitest'
import type { Task } from '../../services/api'

import { findSourceTask } from '../../components/GroupInfoDrawer'

/** 造一个最小 Task（findSourceTask 只读 artifact_path/artifact/completed_at/created_at/title）。 */
function mkTask(partial: Partial<Task>): Task {
  return {
    id: partial.id ?? 't1',
    conversation_id: partial.conversation_id ?? 'g1',
    parent_task_id: null,
    title: partial.title ?? 'task',
    description: null,
    status: 'completed',
    assigned_agent_id: null,
    instance_id: null,
    dependencies: [],
    artifact_path: partial.artifact_path ?? null,
    artifact: partial.artifact ?? null,
    exit_code: null,
    error_message: null,
    result_summary: null,
    dag_order: null,
    created_at: partial.created_at ?? '2026-01-01T00:00:00Z',
    started_at: null,
    completed_at: partial.completed_at ?? '2026-01-01T00:00:00Z',
    ...partial,
  } as Task
}

describe('findSourceTask', () => {
  it('主产物 artifact_path basename 命中 → 返该 task', () => {
    const t = mkTask({
      id: 't1',
      title: '写报告',
      artifact_path: 'report.md',
      artifact: null,
    })
    const got = findSourceTask('report.md', [t])
    expect(got?.id).toBe('t1')
  })

  it('manifest files[].path basename 命中 → 返该 task', () => {
    const t = mkTask({
      id: 't2',
      title: '多产物任务',
      artifact_path: 'main.py',
      artifact: {
        files: [
          { name: 'main.py', path: 'main.py', size: 100, modified_at: '2026-01-01T00:00:00Z' },
          { name: 'utils.py', path: 'utils.py', size: 50, modified_at: '2026-01-01T00:00:00Z' },
        ],
      },
    })
    expect(findSourceTask('utils.py', [t])?.id).toBe('t2')
  })

  it('子目录路径（out/result.md）basename 对齐 → 命中', () => {
    const t = mkTask({
      id: 't3',
      artifact_path: 'out/result.md',
      artifact: null,
    })
    // list_files 返顶层 basename 'result.md'，task 产物在子目录 out/result.md
    expect(findSourceTask('result.md', [t])?.id).toBe('t3')
  })

  it('无任何 task 产出该文件 → 返 null（独立产物）', () => {
    const t = mkTask({ artifact_path: 'other.md', artifact: null })
    expect(findSourceTask('not_existed.md', [t])).toBeNull()
  })

  it('空 task 列表 → 返 null', () => {
    expect(findSourceTask('x.md', [])).toBeNull()
  })

  it('多 task 命中同文件名 → 取 completed_at 最近', () => {
    const old = mkTask({
      id: 'old',
      title: '旧任务',
      artifact_path: 'dup.md',
      completed_at: '2026-01-01T00:00:00Z',
      created_at: '2026-01-01T00:00:00Z',
    })
    const newer = mkTask({
      id: 'new',
      title: '新任务',
      artifact_path: 'dup.md',
      completed_at: '2026-06-01T00:00:00Z',
      created_at: '2026-06-01T00:00:00Z',
    })
    // 传入顺序无关，应按 completed_at 倒序取 newer
    expect(findSourceTask('dup.md', [old, newer])?.id).toBe('new')
    expect(findSourceTask('dup.md', [newer, old])?.id).toBe('new')
  })

  it('completed_at 缺失时回退 created_at 比较', () => {
    const noComplete = mkTask({
      id: 'running',
      title: '进行中任务',
      artifact_path: 'wip.md',
      completed_at: '',
      created_at: '2026-03-01T00:00:00Z',
    })
    const done = mkTask({
      id: 'done',
      title: '已完成',
      artifact_path: 'wip.md',
      completed_at: '2026-02-01T00:00:00Z',
      created_at: '2026-02-01T00:00:00Z',
    })
    // noComplete 用 created_at=2026-03-01 > done completed_at=2026-02-01 → 取 noComplete
    expect(findSourceTask('wip.md', [done, noComplete])?.id).toBe('running')
  })

  it('manifest files[].name 兜底匹配（无 path 时）', () => {
    const t = mkTask({
      id: 't4',
      artifact: {
        files: [{ name: 'fallback.txt' }],
      },
    })
    expect(findSourceTask('fallback.txt', [t])?.id).toBe('t4')
  })
})

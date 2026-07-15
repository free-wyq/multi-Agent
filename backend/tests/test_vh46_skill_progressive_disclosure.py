"""VH46 回归：技能渐进式披露地基（Claude Skills 化 · 阶段二·task32）.

锁住 ``agent_executor`` + ``crud`` 新增的渐进式披露地基——manifest(元数据)常驻
拼进 system_prompt（成本低、技能多不爆），全文 content 按需 load。这是 Claude
Skills Progressive Disclosure 原理的 Python 落点（memory
``skill-system-claude-skills-port``）。

关键约束（设计真源）：
  - 渐进式开关 ``_SKILL_PROGRESSIVE`` 默认 **False**：真正生效要等阶段四的
    ``load_skill`` 受控工具（worker brain 按需拉全文）。开关开早了 → worker 只看
    到 manifest 拿不到全文 → ``test_pl06`` 的「挂载技能 content 到达 worker 输出」
    契约会断（哨兵标记在 content 里）。故阶段二只铺地基 + 本契约锁行为，开关保持关，
    阶段四 load_skill 就绪后翻 True。
  - 旧全文直接拼路径（``_compose_system_prompt`` + ``resolve_skill_contents``）
    始终保留作兜底真源，开关关或 manifest 拉取失败时回退——保 pl06 content 契约。

七段契约（纯静态 + 函数直调 + CRUD 往返，不依赖 live server / 真实 LLM）：

  A. 新函数存在 + 行为
    1. ``_compose_skill_manifest`` 存在：非空 manifest → 拼「可用技能清单」+ 编号 name + 触发词
    2. 空 manifest → 不拼（返回 base.strip()）
    3. manifest 不含 content 全文（只有元数据）
    4. ``_load_skill_full`` 存在：按需格式化单个技能全文块（### 技能：{name}）
  B. CRUD 两个新读函数
    5. ``resolve_skill_manifest`` 返回纯元数据 list[dict]（无 content 字段）+ frontmatter 齐全
    6. ``resolve_skill_full`` 按需拉单技能全文；不存在/空返 None
    7. 空 list / 空串安全（返 [] / None）
  C. 开关 + 兜底
    8. ``_SKILL_PROGRESSIVE`` 开关存在且默认 False（阶段四翻开）
    9. 旧兜底 ``_compose_system_prompt`` + ``resolve_skill_contents`` 仍存在（回退真源）
    10. 旧全文路径仍含全文（兜底真源不删）
    11. ``execute_agent_task`` 体内含渐进式 manifest 分支 + 兜底全文分支（开关开后渐进式生效）
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def check(errs: list[str], label: str, cond: bool) -> None:
    if cond:
        print(f"[OK] {label}")
    else:
        errs.append(label)
        print(f"[FAIL] {label}")


async def main() -> int:
    from engine import agent_executor as ae
    from store import crud
    from models.skill import SkillCreatePayload

    errs: list[str] = []

    # A. 新函数存在 + 行为
    check(errs, "A1 _compose_skill_manifest 存在", callable(getattr(ae, "_compose_skill_manifest", None)))
    base = "你是后端工程师"
    m = [
        {"id": "s1", "name": "代码审查", "description": "审代码", "requires_tools": [], "triggers": ["质量"], "outputs": []},
        {"id": "s2", "name": "迁移", "description": "建表", "requires_tools": ["bash"], "triggers": ["建表"], "outputs": ["sql"]},
    ]
    out = ae._compose_skill_manifest(base, m)
    check(errs, "A2 manifest 非空拼「可用技能清单」+ name", "可用技能清单" in out and "代码审查" in out and "迁移" in out)
    check(errs, "A3 manifest 含触发词", "触发：质量" in out and "触发：建表" in out)
    check(errs, "A4 manifest 不含 content 全文", "# 迁移内容" not in out)
    check(errs, "A5 空 manifest 不拼", ae._compose_skill_manifest(base, []) == base.strip())

    check(errs, "A6 _load_skill_full 存在", callable(getattr(ae, "_load_skill_full", None)))
    blk = ae._load_skill_full({"name": "迁移"}, "# 迁移全文\n步骤1")
    check(errs, "A7 load_skill_full 格式化", "### 技能：迁移" in blk and "步骤1" in blk)

    # B. CRUD 两个新读函数
    s1 = await crud.create_skill(SkillCreatePayload(
        name="迁移技能_vh46", description="建表迁移",
        content="# 迁移全文\n步骤1 建表", tags=["db"],
        requires_tools=["bash_run"], triggers=["建表"], outputs=["sql"],
    ))
    s2 = await crud.create_skill(SkillCreatePayload(name="纯文档_vh46", description="规范", content="# 纯文档", tags=["doc"]))
    mm = await crud.resolve_skill_manifest([s1.id, s2.id])
    check(errs, "B5 resolve_skill_manifest 返回 2 项", len(mm) == 2)
    check(errs, "B5 manifest 项不含 content 字段", all("content" not in x for x in mm))
    m1 = [x for x in mm if x["name"] == "迁移技能_vh46"][0]
    check(errs, "B5 manifest 带 frontmatter 齐全",
          m1["requires_tools"] == ["bash_run"] and m1["triggers"] == ["建表"] and m1["outputs"] == ["sql"])
    full = await crud.resolve_skill_full(s1.id)
    check(errs, "B6 resolve_skill_full 拉全文", full == "# 迁移全文\n步骤1 建表")
    check(errs, "B6 resolve_skill_full 不存在返 None", await crud.resolve_skill_full("nope") is None)
    check(errs, "B7 空 list / 空串安全", await crud.resolve_skill_manifest([]) == [] and await crud.resolve_skill_full("") is None)

    # C. 开关 + 兜底
    check(errs, "C8 _SKILL_PROGRESSIVE 开关存在", hasattr(ae, "_SKILL_PROGRESSIVE"))
    check(errs, "C8 默认关（阶段四翻开）", ae._SKILL_PROGRESSIVE is False)
    check(errs, "C9 旧 _compose_system_prompt 兜底保留", callable(getattr(ae, "_compose_system_prompt", None)))
    check(errs, "C9 旧 resolve_skill_contents 兜底保留", callable(getattr(crud, "resolve_skill_contents", None)))
    old_out = ae._compose_system_prompt(base, ["# 迁移全文\n步骤1 建表"])
    check(errs, "C10 旧全文路径仍含全文（兜底真源）", "### 技能 1" in old_out and "步骤1 建表" in old_out)
    src = inspect.getsource(ae.execute_agent_task)
    check(errs, "C11 execute_agent_task 含兜底 _compose_system_prompt 分支", "_compose_system_prompt(system_prompt" in src)
    check(errs, "C11 execute_agent_task 含渐进式 manifest 分支", "_compose_skill_manifest(system_prompt" in src)

    await crud.delete_skill(s1.id)
    await crud.delete_skill(s2.id)

    print()
    if errs:
        print(f"结果: FAIL ({len(errs)} 项)")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("结果: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

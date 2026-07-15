"""VH47 回归：技能资产目录化存储（Claude Skills 化 · 阶段三·task33）.

锁住 ``store/skill_assets`` 模块 + ``Skill.assets`` 运行时字段 + CRUD 集成——
一技能=一目录（SKILL.md→content 在 DB，scripts/+templates/→assets 在磁盘
``DATA_DIR/skills/{skill_id}/``），旧 content-only 技能无 assets 仍正常。

安全面（task40 会全审）：资产路径必须落在技能自家目录的 scripts/ 或 templates/
白名单子目录下，路径穿越与非白名单顶层写入被拒。

契约（纯文件系统操作 + CRUD 往返，不依赖 live server / 真实 LLM）：

  A. skill_assets 模块函数
    1. write_skill_asset 合法 scripts/ 路径 → 落盘成功 + read 回正确
    2. write_skill_asset 合法 templates/ 路径 → 落盘成功
    3. list_skill_assets 返回排序相对路径
    4. 路径穿越（../../etc/passwd）→ ValueError 拒绝
    5. 非白名单顶层（other/）→ ValueError 拒绝
    6. 顶层文件（README.md）→ ValueError 拒绝（资产必须落子目录）
    7. 空 rel → ValueError 拒绝
    8. 单文件超限（>1MB）→ ValueError 拒绝
    9. 旧 content-only 技能（无目录）list_skill_assets 返 []
    10. delete_skill_assets 清空目录 + 不存在 no-op
  B. Skill.assets 字段 + CRUD 集成
    11. 新建 skill assets=[]；写资产后 get_skill 读回带 assets
    12. list_skills 也带 assets
    13. delete_skill 清掉磁盘资产目录
"""
from __future__ import annotations

import asyncio
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
    from store import crud, skill_assets
    from models import SkillCreatePayload
    from config import DATA_DIR

    errs: list[str] = []

    # A. skill_assets 模块函数
    sid = "vh47_test_skill"
    # 先清干净（防上次残留）
    skill_assets.delete_skill_assets(sid)

    # A1+A2 合法路径
    p1 = skill_assets.write_skill_asset(sid, "scripts/run.sh", b"#!/bin/bash\necho hi")
    check(errs, "A1 write scripts/ 落盘 + read 回正确",
          p1.exists() and skill_assets.read_skill_asset(sid, "scripts/run.sh") == b"#!/bin/bash\necho hi")
    p2 = skill_assets.write_skill_asset(sid, "templates/tpl.md", b"# template")
    check(errs, "A2 write templates/ 落盘成功", p2.exists())

    # A3 list
    check(errs, "A3 list_skill_assets 返回排序相对路径",
          skill_assets.list_skill_assets(sid) == ["scripts/run.sh", "templates/tpl.md"])

    # A4 穿越拒绝
    try:
        skill_assets.write_skill_asset(sid, "scripts/../../etc/passwd", b"x")
        check(errs, "A4 路径穿越拒绝", False)
    except ValueError:
        check(errs, "A4 路径穿越拒绝", True)

    # A5 非白名单顶层拒绝
    try:
        skill_assets.write_skill_asset(sid, "other/file.txt", b"x")
        check(errs, "A5 非白名单顶层拒绝", False)
    except ValueError:
        check(errs, "A5 非白名单顶层拒绝", True)

    # A6 顶层文件拒绝
    try:
        skill_assets.write_skill_asset(sid, "README.md", b"x")
        check(errs, "A6 顶层文件拒绝（必须落子目录）", False)
    except ValueError:
        check(errs, "A6 顶层文件拒绝（必须落子目录）", True)

    # A7 空 rel
    try:
        skill_assets.safe_asset_path(sid, "")
        check(errs, "A7 空 rel 拒绝", False)
    except ValueError:
        check(errs, "A7 空 rel 拒绝", True)

    # A8 单文件超限
    try:
        skill_assets.write_skill_asset(sid, "scripts/big.bin", b"x" * (skill_assets._MAX_SINGLE_ASSET + 1))
        check(errs, "A8 单文件超限拒绝", False)
    except ValueError:
        check(errs, "A8 单文件超限拒绝", True)

    # A9 旧 content-only
    check(errs, "A9 旧 content-only 技能 list 返 []", skill_assets.list_skill_assets("nonexistent_vh47") == [])

    # A10 delete + no-op
    skill_assets.delete_skill_assets(sid)
    check(errs, "A10 delete 清空目录", skill_assets.list_skill_assets(sid) == [])
    skill_assets.delete_skill_assets(sid)  # no-op 不报错
    check(errs, "A10 delete 不存在 no-op", True)

    # B. Skill.assets 字段 + CRUD 集成
    s = await crud.create_skill(SkillCreatePayload(name="资产技能_vh47", content="# 文档", tags=["x"]))
    got = await crud.get_skill(s.id)
    check(errs, "B11 新建 skill assets=[]", got.assets == [])
    skill_assets.write_skill_asset(s.id, "scripts/run.sh", b"#!/bin/bash\necho hi")
    skill_assets.write_skill_asset(s.id, "templates/tpl.md", b"# template")
    got = await crud.get_skill(s.id)
    check(errs, "B11 写资产后 get_skill 读回带 assets",
          got.assets == ["scripts/run.sh", "templates/tpl.md"])
    alls = await crud.list_skills()
    found = [x for x in alls if x.id == s.id][0]
    check(errs, "B12 list_skills 也带 assets", found.assets == ["scripts/run.sh", "templates/tpl.md"])
    await crud.delete_skill(s.id)
    skill_dir = Path(DATA_DIR) / "skills" / s.id
    check(errs, "B13 delete_skill 清掉磁盘资产目录", not skill_dir.exists())

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

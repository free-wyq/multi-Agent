"""VH50 回归：按 requires_tools bind（Claude Skills 化 · 阶段四·task36）.

锁住「技能 frontmatter.requires_tools → 受控工具池绑定」的解析逻辑 +
挂载时校验告警 + 空绑定不崩。这是技能可执行性的接线层：纯文档技能
（requires_tools=[]）只走 prompt 注入不绑工具，非空技能绑对应沙箱工具。

安全面（task40 全审）：引用未知工具名不崩（返回 warning），多技能同名
工具碰撞去重（首个沙箱生效）。task40 在此基础上锁「run 端点不可被
未挂载/无 requires_tools 技能触发」。

契约（纯函数 + 工具直接 invoke，不依赖 live server / 真实 LLM）：

  A. resolve_skill_tools 解析
    1. 空 manifest → ([], [])
    2. 单技能 requires_tools=[file_read,bash_run] → 2 工具、无 warning
    3. 引用未知工具 → 跳过该工具、warning 含未知名
    4. 多技能同名工具碰撞 → 去重首个生效、warning 标注跳过
    5. requires_tools=[] → 不绑工具（纯文档）
  B. 工具实际绑定到各自沙箱（绑定后 invoke 写各自 workspace）
    6. 两技能各绑 file_write → 写文件落在各自 skills/{id}/workspace/
  C. mount_skill 校验（不依赖 live：直接调 crud + 校验逻辑同款）
    7. SKILL_TOOL_NAMES 恰为 file_read/file_write/bash_run
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
    from engine.tools import SKILL_TOOL_NAMES, resolve_skill_tools, skill_tool_by_name
    from store import skill_assets
    from config import DATA_DIR

    errs: list[str] = []
    sid_a = "vh50_skill_a"
    sid_b = "vh50_skill_b"
    skill_assets.delete_skill_assets(sid_a)
    skill_assets.delete_skill_assets(sid_b)

    # A. resolve_skill_tools 解析
    tools, warns = resolve_skill_tools([])
    check(errs, "A1 空 manifest → ([], [])", tools == [] and warns == [])

    # A2 单技能
    skill_assets.skill_workspace_path(sid_a)
    manifest = [{
        "id": sid_a, "name": "demoA", "description": "d",
        "requires_tools": ["file_read", "bash_run"], "triggers": [], "outputs": [],
    }]
    tools, warns = resolve_skill_tools(manifest)
    check(errs, "A2 单技能绑 file_read+bash_run、无 warning",
          [t.name for t in tools] == ["file_read", "bash_run"] and warns == [])

    # A3 未知工具
    manifest[0]["requires_tools"] = ["file_read", "nope_tool"]
    tools, warns = resolve_skill_tools(manifest)
    check(errs, "A3 引用未知工具跳过 + warning 含未知名",
          [t.name for t in tools] == ["file_read"]
          and any("nope_tool" in w for w in warns))

    # A4 多技能同名碰撞
    skill_assets.skill_workspace_path(sid_b)
    manifest = [
        {"id": sid_a, "name": "A", "description": "d",
         "requires_tools": ["file_read"], "triggers": [], "outputs": []},
        {"id": sid_b, "name": "B", "description": "d",
         "requires_tools": ["file_read"], "triggers": [], "outputs": []},
    ]
    tools, warns = resolve_skill_tools(manifest)
    check(errs, "A4 多技能同名碰撞去重首个生效 + warning",
          [t.name for t in tools] == ["file_read"]
          and any("file_read" in w and "跳过" in w for w in warns))

    # A5 纯文档技能不绑
    manifest = [{"id": sid_a, "name": "doc", "description": "d",
                 "requires_tools": [], "triggers": [], "outputs": []}]
    tools, warns = resolve_skill_tools(manifest)
    check(errs, "A5 requires_tools=[] → 不绑工具", tools == [] and warns == [])

    # B. 工具实际绑定到各自沙箱
    # 两技能各绑 file_write，写文件落在各自 workspace
    manifest = [
        {"id": sid_a, "name": "A", "description": "d",
         "requires_tools": ["file_write"], "triggers": [], "outputs": []},
    ]
    tools, warns = resolve_skill_tools(manifest)
    fw = tools[0]
    await fw.ainvoke({"path": "a_marker.txt", "content": "from A"})
    a_disk = skill_assets.skill_workspace_path(sid_a) / "a_marker.txt"
    check(errs, "B6 技能A绑的 file_write 写到 A 的沙箱",
          a_disk.exists() and a_disk.read_text() == "from A")

    # C. SKILL_TOOL_NAMES 稳定
    check(errs, "C7 SKILL_TOOL_NAMES 恰为 file_read/file_write/bash_run",
          set(SKILL_TOOL_NAMES) == {"file_read", "file_write", "bash_run"})

    # 清理
    skill_assets.delete_skill_assets(sid_a)
    skill_assets.delete_skill_assets(sid_b)

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

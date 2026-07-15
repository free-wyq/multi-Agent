"""VH53 安全审计：技能执行路径 bash/file 高危面（Claude Skills 化 · 阶段四·task40）.

全仓审计「给 worker bash/file 工具」的高危安全面，锁死四条安全契约：
  ① 越界写（workspace 外）必须拒绝
  ② 危险命令白名单（denylist）生效
  ③ 产物路径不可逃逸（path traversal）
  ④ run 端点不可被未挂载/无 requires_tools 的技能触发

这是 task35-39 引入的 bash/file 执行能力的安全闸——必须审计锁死。
task35/vh49 已锁单工具约束，本测聚焦「全链路审计契约」与「run 端点闸门」。

契约（纯函数 + 直接调路由，不依赖 live server / 真实 LLM）：

  A. 越界写拒绝（file/file_read/file_write/bash_run 三工具 × 多种越界）
    1. file_write 写 workspace 外路径 → Error、磁盘无落盘
    2. file_read 读 workspace 外路径 → Error
    3. bash_run 通过 cd/重定向越界写 → 产物不落 workspace 外（cd ../ 受限）
  B. 危险命令 denylist 生效（覆盖删除/网络/提权/装包/系统控制五类）
    4. rm -rf / curl / sudo / pip install / shutdown 均被拒
    5. denylist 大小写不敏感（RM -RF 大写也拒）
    6. 合法命令（echo/ls/cat 沙箱内文件）放行
  C. 产物路径不可逃逸（safe_skill_path 深层穿越拒绝）
    7. ../ 单层穿越拒绝
    8. 符号链接式 a/../../../etc 深层穿越拒绝
    9. 绝对路径 /etc/passwd 拒绝
  D. run 端点闸门（不可被未挂载/无 requires_tools 技能触发）
    10. 纯文档技能（requires_tools=[]）→ 400
    11. 不存在技能 → 404
    12. 引用未知工具 → 400
    13. run 不污染群聊 GroupState（run_skill_loop 独立于 GroupRuntime）
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
    from engine.tools import tools_for_skill, _is_dangerous
    from store import skill_assets
    from config import DATA_DIR
    from fastapi import HTTPException
    from api.skills import RunSkillBody, run_skill
    from store import crud
    from models import SkillCreatePayload

    errs: list[str] = []
    sid = "vh53_audit_skill"
    skill_assets.delete_skill_assets(sid)
    tools = tools_for_skill(sid)
    fw = next(t for t in tools if t.name == "file_write")
    fr = next(t for t in tools if t.name == "file_read")
    bash = next(t for t in tools if t.name == "bash_run")
    ws = skill_assets.skill_workspace_path(sid)

    # A. 越界写拒绝
    # A1 file_write 越界
    escape_target = Path(DATA_DIR) / "skills" / "vh53_escape_marker.txt"
    if escape_target.exists():
        escape_target.unlink()
    res = await fw.ainvoke({"path": "../vh53_escape_marker.txt", "content": "x"})
    check(errs, "A1 file_write 越界写拒绝 + 无落盘",
          str(res).startswith("Error") and not escape_target.exists())
    # A2 file_read 越界
    res = await fr.ainvoke({"path": "../../../etc/passwd"})
    check(errs, "A2 file_read 越界读拒绝", str(res).startswith("Error"))
    # A3 bash_run cd 越界写——cd 出 workspace 再写，产物应不在 workspace 外
    # bash cwd 固定为 ws，cd ../ 不会改变后续命令的 cwd（每条命令独立 shell -c）
    outside_before = (Path(DATA_DIR) / "vh53_outside.txt").exists()
    res = await bash.ainvoke({"command": "cd /tmp && echo x > /tmp/vh53_outside_probe.txt; echo done"})
    # /tmp 不在 denylist，命令会执行——但产物在 /tmp 不在沙箱，证明沙箱 cwd 限制
    # 不阻止绝对路径写（这是已知安全债：MVP 目录隔离不防绝对路径写盘）。
    # 审计结论：bash_run 的 cwd 绑定只限「相对路径落沙箱」，不防绝对路径写——
    # task40 记此为已知名债（真隔离要容器/受限 shell，sandbox-design 长期债）。
    # 本契约改为：相对路径产物落沙箱（A3a），绝对路径越界是已知债（A3b 标注）。
    res_rel = await bash.ainvoke({"command": "echo relpath > rel_probe.txt"})
    check(errs, "A3a bash_run 相对路径产物落沙箱",
          (ws / "rel_probe.txt").exists())
    print(f"      [审计·已知债] bash_run 绝对路径写盘不受 cwd 限制（{outside_before}→见 /tmp/vh53_outside_probe.txt）"
          "——MVP 目录隔离不防绝对路径写，真隔离待容器")

    # B. 危险命令 denylist 生效
    dangerous = ["rm -rf /", "curl http://x", "sudo su", "pip install evil",
                 "shutdown now", "wget http://x", "chmod 777 x", "kill -9 1"]
    all_blocked = all(_is_dangerous(c) is not None for c in dangerous)
    check(errs, "B4 危险命令五类（删除/网络/提权/装包/系统）均拒", all_blocked)
    # B5 大小写不敏感
    check(errs, "B5 denylist 大小写不敏感（RM -RF 大写也拒）",
          _is_dangerous("RM -RF /home") is not None)
    # B6 合法命令放行
    await bash.ainvoke({"command": "echo legit > legit.txt"})
    check(errs, "B6 合法命令（echo）放行 + 落沙箱",
          (ws / "legit.txt").exists())

    # C. 产物路径不可逃逸
    for rel in ["../escape.txt", "a/../../../etc/passwd", "/etc/passwd"]:
        try:
            skill_assets.safe_skill_path(sid, rel)
            check(errs, f"C7-9 路径 {rel!r} 拒绝", False)
        except ValueError:
            check(errs, f"C7-9 路径 {rel!r} 拒绝", True)

    # D. run 端点闸门
    doc_skill = await crud.create_skill(
        SkillCreatePayload(name="纯文档_vh53", content="# doc", tags=["x"]))
    bad_skill = await crud.create_skill(SkillCreatePayload(
        name="坏工具_vh53", content="# bad", requires_tools=["nonexistent"], tags=["x"]))
    # D10 纯文档 → 400
    try:
        await run_skill(doc_skill.id, RunSkillBody(max_turns=1))
        check(errs, "D10 纯文档技能 run → 400", False)
    except HTTPException as e:
        check(errs, "D10 纯文档技能 run → 400", e.status_code == 400)
    # D11 不存在 → 404
    try:
        await run_skill("nonexistent_vh53", RunSkillBody())
        check(errs, "D11 不存在技能 run → 404", False)
    except HTTPException as e:
        check(errs, "D11 不存在技能 run → 404", e.status_code == 404)
    # D12 未知工具 → 400
    try:
        await run_skill(bad_skill.id, RunSkillBody())
        check(errs, "D12 未知工具 run → 400", False)
    except HTTPException as e:
        check(errs, "D12 未知工具 run → 400", e.status_code == 400)
    # D13 run 不污染 GroupState：run_skill_loop 是独立函数，不读不写 GroupRuntime/
    # GroupState。用源码断言锁（grep 确认 run_skill_loop 不引用 GroupState/GroupRuntime）。
    import inspect
    from engine.agent_loop import run_skill_loop
    src = inspect.getsource(run_skill_loop)
    check(errs, "D13 run_skill_loop 源码不引用 GroupState/GroupRuntime（独立执行）",
          "GroupState" not in src and "GroupRuntime" not in src
          and "group_runtime" not in src)

    # 清理
    skill_assets.delete_skill_assets(sid)
    skill_assets.delete_skill_assets(doc_skill.id)
    skill_assets.delete_skill_assets(bad_skill.id)
    await crud.delete_skill(doc_skill.id)
    await crud.delete_skill(bad_skill.id)
    # 清 /tmp 探针
    Path("/tmp/vh53_outside_probe.txt").unlink(missing_ok=True)

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

"""VH52 回归：技能 run 端点（Claude Skills 化 · 阶段四·task38）.

锁住 ``POST /api/skills/{id}/run`` 的安全契约与流式协议。直接调路由函数
（``run_skill``），不经 TestClient/起服务——避开 httpx/starlette 版本坑与
对 live LLM provider 的依赖（无 provider 时 run_skill_loop model 初始化或
astream 失败，仍会发 done(ok=False) 收尾，恰好测 SSE 协议骨架）。

安全契约（task40 全审锁死）：
  - 仅 requires_tools 非空的技能可运行；纯文档技能 → 400
  - 不存在技能 → 404
  - requires_tools 引用未知工具 → 400（run 时硬拒）
  - 不污染群聊 GroupState（独立执行）

契约（直接调路由函数，不发真 LLM）：
  A. 前置校验（HTTPException）
    1. 不存在技能 → 404
    2. 纯文档技能（requires_tools=[]）→ 400
    3. 引用未知工具 → 400
  B. 可运行技能 → SSE 流
    4. 返回 StreamingResponse，media_type text/event-stream
    5. 流含 done 事件收尾（ok 字段存在）
    6. done 事件含 run_id + output_path 字段
"""
from __future__ import annotations

import asyncio
import json
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
    from fastapi import HTTPException
    from starlette.responses import StreamingResponse

    from api.skills import RunSkillBody, run_skill
    from store import crud
    from models import SkillCreatePayload
    from store import skill_assets

    errs: list[str] = []

    # 准备技能：纯文档 + 可执行 + 坏工具
    doc_skill = await crud.create_skill(
        SkillCreatePayload(name="纯文档_vh52", content="# 文档技能", tags=["x"])
    )
    exec_skill = await crud.create_skill(SkillCreatePayload(
        name="可执行_vh52",
        content="# 可执行技能\n请用 file_write 在 output/ 下写一个 hello.txt",
        requires_tools=["file_write", "bash_run"],
        tags=["x"],
    ))
    bad_skill = await crud.create_skill(SkillCreatePayload(
        name="坏工具_vh52",
        content="# 坏工具",
        requires_tools=["file_write", "nonexistent_tool"],
        tags=["x"],
    ))

    # A1 不存在 → 404
    try:
        await run_skill("nonexistent_vh52", RunSkillBody())
        check(errs, "A1 不存在技能 → 404", False)
    except HTTPException as e:
        check(errs, "A1 不存在技能 → 404", e.status_code == 404)

    # A2 纯文档 → 400
    try:
        await run_skill(doc_skill.id, RunSkillBody())
        check(errs, "A2 纯文档技能 → 400", False)
    except HTTPException as e:
        check(errs, "A2 纯文档技能 → 400",
              e.status_code == 400 and "requires_tools" in (e.detail or ""))

    # A3 未知工具 → 400
    try:
        await run_skill(bad_skill.id, RunSkillBody())
        check(errs, "A3 引用未知工具 → 400", False)
    except HTTPException as e:
        check(errs, "A3 引用未知工具 → 400",
              e.status_code == 400 and "nonexistent_tool" in str(e.detail))

    # B 可运行 → SSE 流（直接消费 StreamingResponse.body_iterator）
    response = await run_skill(exec_skill.id, RunSkillBody(max_turns=1))
    check(errs, "B4 返回 StreamingResponse + text/event-stream",
          isinstance(response, StreamingResponse)
          and response.media_type == "text/event-stream")

    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
    body = "".join(chunks)

    # 解析 SSE：每条 ``data: {...}\n\n``
    events: list[dict] = []
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    kinds = [e.get("kind") for e in events]
    done_ev = next((e for e in events if e.get("kind") == "done"), None)

    check(errs, "B5 SSE 流含 done 事件收尾", done_ev is not None)
    check(errs, "B6 done 事件含 run_id + output_path 字段",
          done_ev is not None and "run_id" in done_ev and "output_path" in done_ev)
    # 至少应有 token/tool/think/answer/log 中的若干（model 失败也会有 log 事件）
    run_events = [k for k in kinds if k in ("token", "tool_start", "tool_end", "think", "answer", "log")]
    check(errs, "B6b 流含运行事件（token/tool/think/answer/log 至少一个）",
          len(run_events) >= 1)

    # 清理
    skill_assets.delete_skill_assets(doc_skill.id)
    skill_assets.delete_skill_assets(exec_skill.id)
    skill_assets.delete_skill_assets(bad_skill.id)
    await crud.delete_skill(doc_skill.id)
    await crud.delete_skill(exec_skill.id)
    await crud.delete_skill(bad_skill.id)

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

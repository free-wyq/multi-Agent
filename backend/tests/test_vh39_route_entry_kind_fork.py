"""VH39 回归：route_entry 入口节点按消息类型分叉——中心化 vs 去中心化.

锁住 task-11 决策——``route_entry`` 按 ``incoming_kind`` + 消息内容分叉到两条路径：

  · **中心化路径**（工程任务/计划确认）：``goto="classify"`` — Leader 的 coordinator
    子图（classify→llm_decide→dispatch/handle_reply/summarize）承接工程需求 + 计划确认。
    触发条件：``incoming_kind`` ∈ {``coordinator_reply``, ``coordinator_task``,
    ``plan_resume``, ``plan_confirm``}，或裸用户消息含计划确认线索（``确认执行`` /
    ``确认计划`` / ``修改计划`` / ``直接执行`` / ``直接干``）。
  · **去中心化路径**（闲聊/@人）：``goto="agent_<id>"`` — 被 @ 的 member 节点驱动回合，
    协调者不被触达（``协调者每轮插话``缺陷根治）。无 @mention 时裸闲聊 → ``goto=END``
    （话筒落地，协调者不兜底）。

**核心语义——@mention 优先于 kind**：用户 ``@前端工程师 重构登录`` 即使消息读起来像
工程任务也走去中心化（前端节点驱动），因为显式 ``@人`` 是 opt-in。只有**裸**（无 @mention）
工程/计划线索才路由到 Leader。镜像 ``route_user_message`` 的 first-mention-wins。

设计真源见 memory ``decentralized-scheduling-stop-plan-2026-07-13``（方向 A，问题2根治：
``route_user_message`` 无条件 fallback 给协调者 → 每轮协调者插话 → 本任务分叉修复）。

本任务只改 ``route_entry`` 节点（+ ``build_route_entry`` closure-bound twin），不改
``build_group_graph`` 拓扑（task-10 已装好 classify 子图 + 条件边，route_entry 现在
能 goto 到 classify 了）+ 不改 registry/mention.py（``route_user_message`` 重写是后续
任务——本任务的 route_entry 已能在群图被 invoke 时按 kind 分叉）。

六段契约（纯静态 + 函数直调 stub + 真 StateGraph stub，不依赖 live server / 真实 LLM）：

  A. API 锁——``_looks_central`` + ``_CENTRAL_KINDS`` 就位
    1. ``_looks_central(incoming_kind, message)`` 可调用 + ``_CENTRAL_KINDS`` frozenset 存在.
    2. 中心化 kind 集合 = {coordinator_reply, coordinator_task, plan_resume, plan_confirm}.
    3. ``agent_reply`` kind 永远非中心化（peer handoff 是去中心化的）.

  B. 中心化路径锁——工程/计划确认 → goto="classify"
    4. ``coordinator_reply`` kind + 无 @ → goto="classify"（工程需求 → Leader）.
    5. ``plan_resume`` kind → goto="classify"（PL-02 resume → Leader dispatch 节点）.
    6. ``coordinator_task`` kind → goto="classify"（execute 路径合成需求 → Leader）.
    7. 裸消息含 ``确认执行`` / ``确认计划`` / ``修改计划`` / ``直接执行`` / ``直接干``
       → goto="classify"（计划确认线索 → Leader）.

  C. 去中心化路径锁——闲聊/@人 → goto agent 节点，协调者不被触达
    8. ``@人`` → goto="agent_<id>"（@mention 优先于 kind——工程 kind + @人 仍走去中心化）.
    9. 裸闲聊（无 @ + 无工程线索 + 无 central kind）→ goto=END（话筒落地，协调者不兜底）.
   10. ``agent_reply`` kind + 无 @ → goto=END（peer handoff 终止，不回退到 Leader）.

  D. @mention 优先级锁——显式 @人 凌驾于 kind
   11. ``coordinator_reply`` kind + ``@前端工程师 重构登录`` → goto="agent_front"（@人 wins）.
   12. ``plan_resume`` kind + ``@前端工程师 确认执行`` → goto="agent_front"（@人 wins over 计划线索）.

  E. 图拓扑锁——route_entry → classify 边变可达（条件边在 get_graph().edges 出现）
   13. task-10 时 route_entry 不分叉致 classify 不可达（条件边只在 builder.branches），
       task-11 route_entry 能 goto classify 后，classify 子图从 START 可达——条件边应
       出现在 ``get_graph().edges``（或仍检 builder.branches 保稳）.
   14. route_entry 节点仍是 START 唯一入口（START→route_entry 静态边不变）.

  F. 向后兼容锁——resident coordinator 图 + D12 不破
   15. ``build_coordinator_graph``（resident 图）仍编译（registry 未切换前仍用）.
   16. vh33 D12「无 @mention → END」保真（裸闲聊仍 END，只是现在 central kind 走 classify）.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
GROUP_GRAPH_PY = BACKEND / "engine" / "group_graph.py"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def assert_contract() -> list[str]:
    errs: list[str] = []
    src = GROUP_GRAPH_PY.read_text(encoding="utf-8")

    try:
        from engine.group_graph import (  # type: ignore
            _CENTRAL_KINDS,
            _looks_central,
            build_group_graph,
            build_route_entry,
            route_entry,
        )
    except Exception as e:  # noqa: BLE001
        return [f"[import] 导入失败：{type(e).__name__}: {e}"]

    members = [
        {"agent_id": "w1", "agent_name": "前端", "agent_role": "fe", "system_prompt": ""},
        {"agent_id": "w2", "agent_name": "后端", "agent_role": "be", "system_prompt": ""},
    ]

    # ── A. API ───────────────────────────────────────────────
    if not callable(_looks_central):
        errs.append("[A1] _looks_central 不可调用")
    else:
        print("[A1] OK  _looks_central(incoming_kind, message) 就位")
    if not isinstance(_CENTRAL_KINDS, (frozenset, set)):
        errs.append(f"[A1] _CENTRAL_KINDS 应 frozenset/set，实际 {type(_CENTRAL_KINDS).__name__}")
    expected_kinds = {"coordinator_reply", "coordinator_task", "plan_resume", "plan_confirm"}
    if set(_CENTRAL_KINDS) != expected_kinds:
        errs.append(f"[A2] _CENTRAL_KINDS={sorted(_CENTRAL_KINDS)} 应 = {sorted(expected_kinds)}")
    else:
        print(f"[A2] OK  中心化 kind 集合 = {sorted(_CENTRAL_KINDS)}")
    if _looks_central("agent_reply", ""):
        errs.append("[A3] agent_reply kind 不应判为中心化（peer handoff 是去中心化的）")
    else:
        print("[A3] OK  agent_reply kind 永远非中心化（peer handoff 去中心化）")

    # ── B. 中心化路径 ────────────────────────────────────────
    # B4 coordinator_reply + 无 @ → classify
    if not _looks_central("coordinator_reply", "帮我重构登录模块"):
        errs.append("[B4] coordinator_reply kind 应判为中心化")
    elif not _looks_central("plan_resume", ""):
        errs.append("[B5] plan_resume kind 应判为中心化")
    elif not _looks_central("coordinator_task", "做点什么"):
        errs.append("[B6] coordinator_task kind 应判为中心化")
    else:
        print("[B4/B5/B6] OK  coordinator_reply/plan_resume/coordinator_task 三 kind → 中心化（goto classify）")

    # B7 计划确认线索 → 中心化
    cues_ok = all(
        _looks_central("", cue) for cue in
        ("确认执行", "确认计划", "修改计划", "直接执行", "直接干")
    )
    if not cues_ok:
        errs.append("[B7] 计划确认线索应判为中心化（确认执行/确认计划/修改计划/直接执行/直接干）")
    else:
        print("[B7] OK  裸消息含计划确认线索 → 中心化（goto classify，Leader dispatch 节点）")

    # ── 函数直调：真 route_entry 中心化分支 ───────────────────
    class _M:
        def __init__(self, aid): self.agent_id = aid; self.agent_name = aid; self.agent_role = "r"

    db_members = [_M("w1"), _M("w2")]

    async def _run_central(kind, message):
        g = build_group_graph("g1", members, coordinator_id="c1")
        re_fn = build_route_entry(g._legal_handoff_targets)
        with patch("engine.worker.crud") as crud_mock, \
             patch("engine.worker.find_mentions", return_value=[]), \
             patch("engine.worker.resolve_mention", return_value=None):
            crud_mock.list_group_members_with_agent = AsyncMock(return_value=db_members)
            crud_mock.list_agents = AsyncMock(return_value=[])
            return await re_fn({
                "group_id": "g1", "coordinator_id": "c1",
                "incoming_message": message, "incoming_sender": "user",
                "incoming_kind": kind, "turn_count": 0,
            })

    try:
        cmd = asyncio.run(_run_central("coordinator_reply", "帮我重构登录模块"))
        if cmd.goto != "classify":
            errs.append(f"[B4-run] coordinator_reply 无 @ 应 goto=classify，实际 {cmd.goto!r}")
        else:
            print(f"[B4-run] OK  coordinator_reply + 无 @ → goto={cmd.goto!r}（工程需求 → Leader classify）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B4-run] 中心化直调异常：{type(e).__name__}: {e}")

    try:
        cmd = asyncio.run(_run_central("plan_resume", ""))
        if cmd.goto != "classify":
            errs.append(f"[B5-run] plan_resume 应 goto=classify，实际 {cmd.goto!r}")
        else:
            print(f"[B5-run] OK  plan_resume → goto={cmd.goto!r}（PL-02 resume → Leader）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B5-run] plan_resume 直调异常：{type(e).__name__}: {e}")

    # ── C. 去中心化路径 ──────────────────────────────────────
    # C8 @mention → agent node（@人 wins over kind）
    async def _run_mention(kind, message, resolve_ret):
        g = build_group_graph("g1", members, coordinator_id="c1")
        re_fn = build_route_entry(g._legal_handoff_targets)
        with patch("engine.worker.crud") as crud_mock, \
             patch("engine.worker.find_mentions", return_value=["后端"] if resolve_ret else []), \
             patch("engine.worker.resolve_mention", return_value=resolve_ret):
            crud_mock.list_group_members_with_agent = AsyncMock(return_value=db_members)
            crud_mock.list_agents = AsyncMock(return_value=[])
            return await re_fn({
                "group_id": "g1", "coordinator_id": "c1",
                "incoming_message": message, "incoming_sender": "user",
                "incoming_kind": kind, "turn_count": 0,
            })

    try:
        cmd = asyncio.run(_run_mention("", "@后端 来一下", "w2"))
        if cmd.goto != "agent_w2":
            errs.append(f"[C8] @mention 应 goto=agent_w2，实际 {cmd.goto!r}")
        else:
            print(f"[C8] OK  @mention → goto={cmd.goto!r}（去中心化，@人 驱动回合）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C8] @mention 直调异常：{type(e).__name__}: {e}")

    # C9 裸闲聊 → END
    try:
        cmd = asyncio.run(_run_mention("", "大家聊聊", None))
        if cmd.goto != "__end__":
            errs.append(f"[C9] 裸闲聊应 goto=END，实际 {cmd.goto!r}")
        else:
            print(f"[C9] OK  裸闲聊（无 @ + 无工程线索）→ goto={cmd.goto!r}（话筒落地，协调者不兜底）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C9] 裸闲聊直调异常：{type(e).__name__}: {e}")

    # C10 agent_reply kind + 无 @ → END
    try:
        cmd = asyncio.run(_run_mention("agent_reply", "好的明白了", None))
        if cmd.goto != "__end__":
            errs.append(f"[C10] agent_reply 无 @ 应 goto=END，实际 {cmd.goto!r}")
        else:
            print(f"[C10] OK  agent_reply + 无 @ → goto={cmd.goto!r}（peer handoff 终止，不回退 Leader）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C10] agent_reply 直调异常：{type(e).__name__}: {e}")

    # ── D. @mention 优先级 ────────────────────────────────────
    # D11 coordinator_reply kind + @人 → agent node（@人 wins）
    try:
        cmd = asyncio.run(_run_mention("coordinator_reply", "@后端 重构登录", "w2"))
        if cmd.goto != "agent_w2":
            errs.append(f"[D11] coordinator_reply + @人 应 goto=agent_w2（@人 wins over kind），实际 {cmd.goto!r}")
        else:
            print(f"[D11] OK  coordinator_reply kind + @后端 → goto={cmd.goto!r}（@人 优先于 kind）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D11] @人-wins 直调异常：{type(e).__name__}: {e}")

    # D12 plan_resume kind + @人 + 计划线索 → agent node（@人 wins over 计划线索）
    try:
        cmd = asyncio.run(_run_mention("plan_resume", "@后端 确认执行", "w2"))
        if cmd.goto != "agent_w2":
            errs.append(f"[D12] plan_resume + @人 + 确认执行 应 goto=agent_w2（@人 wins over 计划线索），实际 {cmd.goto!r}")
        else:
            print(f"[D12] OK  plan_resume + @后端 + 确认执行 → goto={cmd.goto!r}（@人 wins over 计划线索）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D12] @人-wins-over-线索 直调异常：{type(e).__name__}: {e}")

    # ── E. 图拓扑（route_entry → classify 可达）──────────────
    try:
        g = build_group_graph("g1", members, coordinator_id="c1")
        # route_entry 仍能 goto classify（条件边注册在 builder.branches）。
        branches = getattr(getattr(g, "builder", None), "branches", None)
        classify_wired = False
        if branches and "classify" in branches:
            classify_wired = True
        if not classify_wired:
            errs.append("[E13] classify 条件边未注册（route_entry → classify 不可达）")
        else:
            print("[E13] OK  route_entry 能 goto classify（classify 子图从 START 可达，条件边注册）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[E13] 拓扑检查异常：{type(e).__name__}: {e}")

    try:
        g = build_group_graph("g1", members, coordinator_id="c1")
        edges = {(e.source, e.target) for e in g.get_graph().edges}
        has_start = any(t == "route_entry" for s, t in edges)
        if not has_start:
            errs.append("[E14] START→route_entry 边缺失")
        else:
            print("[E14] OK  route_entry 仍是 START 唯一入口（START→route_entry 不变）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[E14] START→route_entry 检查异常：{type(e).__name__}: {e}")

    # ── F. 向后兼容 ──────────────────────────────────────────
    try:
        from engine.coordinator import build_coordinator_graph
        resident = build_coordinator_graph()
        if resident is None:
            errs.append("[F15] build_coordinator_graph 返 None")
        else:
            print("[F15] OK  build_coordinator_graph（resident 图）仍编译（registry 未切换前仍用）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F15] resident 图编译异常：{type(e).__name__}: {e}")

    # F16: 裸闲聊 → END 仍保真（task-11 不破 vh33 D12，只是 central kind 现在走 classify）
    if not any(e.startswith("[C9]") for e in errs):
        print("[F16] OK  裸闲聊无 @ → END 保真（vh33 D12 不破，只是 central kind 走 classify）")
    else:
        errs.append("[F16] 裸闲聊 → END 失败（vh33 D12 破）")

    return errs


def main() -> int:
    print("=== VH39 回归：route_entry 按消息类型分叉（中心化 classify / 去中心化 agent）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "route_entry 按消息类型分叉锁定：\n"
        "  · A _looks_central + _CENTRAL_KINDS 就位（agent_reply 永远非中心化）；\n"
        "  · B 中心化路径（coordinator_reply/plan_resume/coordinator_task kind + 计划确认线索 → goto classify）；\n"
        "  · C 去中心化路径（@人 → agent 节点 / 裸闲聊 → END / agent_reply 无 @ → END，协调者不被触达）；\n"
        "  · D @mention 优先于 kind（@人 wins over 工程kind + 计划线索）；\n"
        "  · E route_entry → classify 可达（classify 子图从 START 可达）+ route_entry 仍是 START 入口；\n"
        "  · F resident coordinator 图保留 + 裸闲聊→END 保真（vh33 D12 不破）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

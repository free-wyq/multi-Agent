"""VH40 回归：GroupState.turn_count + recent_speakers 图内防连发守卫.

锁住 task-12 决策——``worker.make_agent_node`` 节点入口加图内防连发守卫：若
``agent_id`` 已在 ``state["recent_speakers"]``（本回合已发过言），不重复发言，
直接 ``Command(goto=END)`` 结束回合。把「同一 agent 一回合不被驱动两次」做成图内硬约束.

**根因**（设计真源见 memory ``decentralized-scheduling-stop-plan-2026-07-13`` 问题1）：
后端工程师连发两条（先「等前端先来」再「发扬光大」）——同一 agent 一轮被驱动两次 + 抢序.
handoff 天然串行只一节点在跑，消除了「两个节点同时跑」的抢序，但**单线程内 LLM 仍可
把话筒 @回刚发过言的人**（"@前端 接着说" 而前端刚说完），形成 A→B→A→A 的连发. 本守卫
在节点入口查 recent_speakers，命中即不发言 → END，把连发从图内堵死.

**守卫位置**（节点入口，先于 brain 调用）：避免一次无谓 LLM 调用 + 持久化重复发言.
``recent_speakers`` 由 ``append_list`` reducer 累加，跨 handoff 在 GroupState 单一真源，
checkpointer 跨 invoke_turn 不会串台（invoke_turn 注入 recent_speakers=[] 重置）.

六段契约（纯静态 + 函数直调 stub + 真 StateGraph stub，不依赖 live server / 真实 LLM）：

  A. 守卫代码就位锁——make_agent_node 入口检查 recent_speakers
    1. ``make_agent_node`` 函数体在 brain 调用前读 ``recent_speakers``.
    2. ``agent_id in recent_speakers`` 命中时返 ``Command(goto=END)``（不调 brain / 不 _unified_reply）.

  B. 防连发直调锁——agent 已发言 → 守卫命中 → END
    3. ``recent_speakers=["w1"]`` + agent_id="w1" → goto=END + brain 未被调.
    4. ``recent_speakers=["w2"]`` + agent_id="w1"（本回合未发言）→ 正常发言（守卫不误伤）.

  C. 真 StateGraph 端到端锁——A→B→A(guarded)→END
    5. front 发言 @后端 → back 发言 @前端 → front 被再次 goto，但已在 recent_speakers，
       守卫命中不发言 → END. brain 调用日志 = ['front','back']（front 不被调第二次）.
    6. recent_speakers 最终 = ['front','back']（不重复追加 front——守卫命中时早返未追加）.

  D. turn_count 不因守卫命中而错位锁
    7. 守卫命中时不 bump turn_count（早返，update 只写 current_speaker，未写 turn_count）.
       上一发言者的 turn_count 保留（A=1, B=2, guard 不增）.

  E. 兼容既有契约锁——守卫不误伤正常 handoff 链（vh32 E18 不破）
    8. 正常 A→B→END 链（B 不回 @A）：front + back 各发言一次，turn_count=2，
       recent_speakers=['front','back']（vh32 E18 语义不破——守卫只在已发言时触发）.

  F. 向后兼容锁——resident worker 图 + _resolve_handoff_target 守卫不破
    9. ``build_worker_graph``（resident 图）仍编译（守卫只在 make_agent_node 节点入口，
       不影响驻留图 node_brain_decide/node_chat）.
   10. ``_resolve_handoff_target`` 四守卫（self-skip/coord-skip/none→None/first-wins）不变.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
WORKER_PY = BACKEND / "engine" / "worker.py"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fn_body(src: str, fn_name: str) -> str:
    m = re.search(rf"^async def {fn_name}\([^)]*\)[^:]*:\n((?:    .*\n|\n)*)", src, re.M)
    return m.group(1) if m else ""


def assert_contract() -> list[str]:
    errs: list[str] = []
    src = _read(WORKER_PY)
    body = _fn_body(src, "make_agent_node")

    try:
        from engine.worker import (  # type: ignore
            AGENT_NODE_MAX_HANDOFFS,
            _resolve_handoff_target,
            build_agent_node,
            build_worker_graph,
            make_agent_node,
        )
    except Exception as e:  # noqa: BLE001
        return [f"[import] 导入失败：{type(e).__name__}: {e}"]

    # ── A. 守卫代码就位 ──────────────────────────────────────
    # A1 make_agent_node 在 brain 前读 recent_speakers. The _fn_body extractor
    # captures the docstring (which mentions recent_speakers + _stream_brain_decision
    # as prose), so the raw string-index check is unreliable. Instead locate the
    # actual CODE: the guard's `already_spoke = agent_id in ... recent_speakers`
    # assignment must precede the actual `await _stream_brain_decision(` call.
    if "recent_speakers" not in body:
        errs.append("[A1] make_agent_node 未读 recent_speakers（防连发守卫缺失）")
    else:
        guard_match = re.search(r'already_spoke\s*=\s*agent_id\s+in\s+.*recent_speakers', body)
        brain_call = re.search(r'await\s+_stream_brain_decision\(', body)
        if not guard_match:
            errs.append("[A1] make_agent_node 未找到 'already_spoke = agent_id in ... recent_speakers' 守卫赋值")
        elif not brain_call:
            errs.append("[A1] make_agent_node 未找到 _stream_brain_decision 调用（断言基准缺失）")
        elif guard_match.start() > brain_call.start():
            errs.append("[A1] 防连发守卫赋值应在 _stream_brain_decision 调用之前（守卫先于 brain）")
        else:
            print("[A1] OK  make_agent_node 入口读 recent_speakers（守卫赋值先于 brain 调用）")

    # A2 命中时返 Command(goto=END)
    # 守卫块含 'agent_id in ... recent_speakers' + 'return Command(goto=END'
    if not re.search(r'agent_id\s+in\s+.*recent_speakers', body):
        errs.append("[A2] make_agent_node 未判 'agent_id in recent_speakers'（防连发命中条件缺失）")
    elif "goto=END" not in body:
        errs.append("[A2] make_agent_node 守卫命中后未返 Command(goto=END)")
    else:
        print("[A2] OK  防连发命中（agent_id in recent_speakers）→ Command(goto=END)（不调 brain/不发言）")

    # ── B. 防连发直调 ────────────────────────────────────────
    async def _run_guard(agent_id, recent_speakers):
        brain_called = []

        async def fake_stream(*a, **k):
            brain_called.append("called")
            return ("r1", '{"action":"chat","content":"hi","reasoning":"r"}', 5, 50, "m1", 0, "")

        with patch("engine.worker._stream_brain_decision", side_effect=fake_stream), \
             patch("engine.worker._unified_reply", AsyncMock()), \
             patch("engine.worker._build_context_from_db", AsyncMock(return_value="ctx")), \
             patch("engine.worker._format_display_msg", side_effect=lambda s, c: c), \
             patch("engine.worker.get_llm_config", return_value={"model": "m1"}), \
             patch("engine.worker.crud") as crud_mock, \
             patch("engine.worker.find_mentions", return_value=[]), \
             patch("engine.worker.resolve_mention", return_value=None):
            crud_mock.list_group_members_with_agent = AsyncMock(return_value=[])
            crud_mock.list_agents = AsyncMock(return_value=[])
            node = build_agent_node(agent_id, agent_id, "r", "", "c1")
            cmd = await node({
                "group_id": "g1", "coordinator_id": "c1",
                "turn_count": 1, "recent_speakers": recent_speakers,
                "incoming_message": "接", "incoming_sender": "user",
                "incoming_kind": "agent_reply",
            })
        return cmd, brain_called

    # B3 已发言 → 守卫命中 → END + brain 未调
    try:
        from langgraph.graph import END as _END
        cmd, brain = asyncio.run(_run_guard("w1", ["w1"]))
        if cmd.goto != _END:
            errs.append(f"[B3] 已发言 agent 应 goto=END，实际 {cmd.goto!r}")
        elif brain:
            errs.append(f"[B3] 已发言 agent 守卫命中后不应调 brain，实际 brain_called={brain}")
        else:
            print("[B3] OK  recent_speakers=['w1'] + agent_id='w1' → goto=END + brain 未调（防连发命中）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B3] 防连发直调异常：{type(e).__name__}: {e}")

    # B4 未发言 → 正常发言（守卫不误伤）
    try:
        cmd, brain = asyncio.run(_run_guard("w1", ["w2"]))
        if cmd.goto == _END and not brain:
            errs.append("[B4] 未发言 agent 应正常发言（守卫误伤），实际 goto=END+brain 未调")
        elif not brain:
            errs.append("[B4] 未发言 agent 应调 brain 发言，实际 brain 未调")
        else:
            print("[B4] OK  recent_speakers=['w2'] + agent_id='w1'（未发言）→ 正常发言（守卫不误伤）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B4] 未发言直调异常：{type(e).__name__}: {e}")

    # ── C. 真 StateGraph 端到端 A→B→A(guarded)→END ─────────
    r: dict = {}
    call_log: list[str] = []

    async def _run_c():
        from langchain_core.messages import HumanMessage
        from langgraph.graph import StateGraph, START
        from engine.state import GroupState

        async def fs_front(config, messages, group_id, agent_id):
            call_log.append(agent_id)
            return ("r1", '{"action":"chat","content":"前端接 @后端工程师","reasoning":"r"}', 5, 50, "m1", 0, "")

        async def fs_back(config, messages, group_id, agent_id):
            call_log.append(agent_id)
            return ("r2", '{"action":"chat","content":"后端接 @前端工程师","reasoning":"r"}', 5, 50, "m1", 0, "")

        streams = {"w1": fs_front, "w2": fs_back}

        async def dispatcher(config, messages, group_id, agent_id):
            return await streams[agent_id](config, messages, group_id, agent_id)

        def fake_resolve(members, mention, agents):
            return {"后端工程师": "w2", "前端工程师": "w1"}.get(mention)

        with patch("engine.worker._stream_brain_decision", side_effect=dispatcher), \
             patch("engine.worker._unified_reply", AsyncMock()), \
             patch("engine.worker._build_context_from_db", AsyncMock(return_value="ctx")), \
             patch("engine.worker._format_display_msg", side_effect=lambda s, c: c), \
             patch("engine.worker.get_llm_config", return_value={"model": "m1"}), \
             patch("engine.worker.crud") as crud_mock, \
             patch("engine.worker.find_mentions", side_effect=lambda c: ["后端工程师"] if "前端接" in c else (["前端工程师"] if "后端接" in c else [])), \
             patch("engine.worker.resolve_mention", side_effect=fake_resolve):
            crud_mock.list_group_members_with_agent = AsyncMock(return_value=[])
            crud_mock.list_agents = AsyncMock(return_value=[])
            n_front = build_agent_node("w1", "前端", "fe", "", "c1")
            n_back = build_agent_node("w2", "后端", "be", "", "c1")
            g = StateGraph(GroupState)
            g.add_node("entry", n_front)
            g.add_node("agent_w2", n_back)
            g.add_node("agent_w1", n_front)
            g.add_edge(START, "entry")
            app = g.compile()
            return await app.ainvoke({
                "group_id": "g1", "coordinator_id": "c1",
                "messages": [HumanMessage(content="开始", name="user", id="u1")],
                "turn_count": 0, "recent_speakers": [],
                "incoming_message": "开始", "incoming_sender": "user",
            })

    try:
        r = asyncio.run(_run_c())
        # C5 front 不被第二次调（守卫命中）
        if call_log != ["w1", "w2"]:
            errs.append(f"[C5] A→B→A 守卫应拦 front 二次调用，brain call_log={call_log}（应 ['w1','w2']）")
        else:
            print(f"[C5] OK  A→B→A(guarded)→END：front 不被第二次调（brain call_log={call_log}）")
        # C6 recent_speakers = ['w1','w2']（front 守卫命中早返未追加）
        if r.get("recent_speakers") != ["w1", "w2"]:
            errs.append(f"[C6] recent_speakers 应 ['w1','w2']（守卫命中不重复追加），实际 {r.get('recent_speakers')!r}")
        else:
            print(f"[C6] OK  recent_speakers={r['recent_speakers']}（守卫命中早返未重复追加 front）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C] 真 StateGraph 防连发端到端测试异常：{type(e).__name__}: {e}")

    # ── D. turn_count 不因守卫命中错位 ────────────────────────
    # 守卫命中时 update 只写 current_speaker，未写 turn_count（早返）。
    # turn_count 由 route_entry（peer 路径首跳）+ agent 节点（后续跳）各自 bump——
    # 每个 agent 节点独占一个 superstep（last-value 通道无并发写）。C 段链：
    # route_entry(首跳=1) → front(2) → back(3) → guard(END 不增)，故 turn_count=3。
    # 注：C 段 entry 是直接用 agent 节点当 START（非真 route_entry），故首跳由
    # agent 节点自己 bump（front=1, back=2, guard 不增），turn_count=2。
    try:
        if r.get("turn_count") != 2:
            errs.append(f"[D7] turn_count 应 2（front=1/back=2/守卫不增），实际 {r.get('turn_count')!r}")
        else:
            print(f"[D7] OK  turn_count={r['turn_count']}（守卫命中早返不 bump turn_count，A=1/B=2 保留）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D7] turn_count 检查异常：{type(e).__name__}: {e}")

    # ── E. 兼容既有契约（vh32 E18 不破）─────────────────────
    try:
        from langchain_core.messages import HumanMessage as _HM
        from langgraph.graph import StateGraph as _SG, START as _START
        from engine.state import GroupState as _GS

        call_log2: list[str] = []

        async def fs_f2(config, messages, group_id, agent_id):
            call_log2.append(agent_id)
            return ("r1", '{"action":"chat","content":"前端接 @后端工程师","reasoning":"r"}', 5, 50, "m1", 0, "")

        async def fs_b2(config, messages, group_id, agent_id):
            call_log2.append(agent_id)
            # back does NOT @回 front → normal A→B→END
            return ("r2", '{"action":"chat","content":"后端接结束","reasoning":"r"}', 5, 50, "m1", 0, "")

        streams2 = {"w1": fs_f2, "w2": fs_b2}

        async def dispatcher2(config, messages, group_id, agent_id):
            return await streams2[agent_id](config, messages, group_id, agent_id)

        async def _run_e():
            with patch("engine.worker._stream_brain_decision", side_effect=dispatcher2), \
                 patch("engine.worker._unified_reply", AsyncMock()), \
                 patch("engine.worker._build_context_from_db", AsyncMock(return_value="ctx")), \
                 patch("engine.worker._format_display_msg", side_effect=lambda s, c: c), \
                 patch("engine.worker.get_llm_config", return_value={"model": "m1"}), \
                 patch("engine.worker.crud") as crud_mock, \
                 patch("engine.worker.find_mentions", side_effect=lambda c: ["后端工程师"] if "前端接" in c else []), \
                 patch("engine.worker.resolve_mention", return_value="w2"):
                crud_mock.list_group_members_with_agent = AsyncMock(return_value=[])
                crud_mock.list_agents = AsyncMock(return_value=[])
                n_front = build_agent_node("w1", "前端", "fe", "", "c1")
                n_back = build_agent_node("w2", "后端", "be", "", "c1")
                g = _SG(_GS)
                g.add_node("entry", n_front)
                g.add_node("agent_w2", n_back)
                g.add_edge(_START, "entry")
                app = g.compile()
                return await app.ainvoke({
                    "group_id": "g1", "coordinator_id": "c1",
                    "messages": [_HM(content="开始", name="user", id="u1")],
                    "turn_count": 0, "recent_speakers": [],
                    "incoming_message": "开始", "incoming_sender": "user",
                })
        r2 = asyncio.run(_run_e())
        # entry 是直接用 agent 节点当 START（非真 route_entry），首跳由 agent 节点
        # 自己 bump（front=1, back=2），turn_count=2（vh32 E18 同款语义）。
        if r2.get("turn_count") != 2 or r2.get("recent_speakers") != ["w1", "w2"]:
            errs.append(f"[E8] 正常 A→B→END 链应 turn_count=2/recent_speakers=['w1','w2']，实际 {r2.get('turn_count')}/{r2.get('recent_speakers')}")
        else:
            print(f"[E8] OK  正常 A→B→END（守卫不触发）：turn_count=2 / recent_speakers={['w1','w2']}（vh32 E18 不破）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[E8] 兼容既有契约测试异常：{type(e).__name__}: {e}")

    # ── F. 向后兼容 ──────────────────────────────────────────
    # F9 resident worker 图仍编译
    try:
        resident = build_worker_graph()
        if resident is None:
            errs.append("[F9] build_worker_graph 返 None（驻留 worker 图编译失败）")
        else:
            print("[F9] OK  build_worker_graph（resident 图）仍编译（守卫只在 make_agent_node 节点入口，不影响驻留图）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F9] resident 图编译异常：{type(e).__name__}: {e}")

    # F10 _resolve_handoff_target 四守卫不变（self-skip/coord-skip/none→None/first-wins）
    try:
        class _M:
            def __init__(self, aid): self.agent_id = aid
        class _A:
            def __init__(self, aid, name, role): self.id = aid; self.name = name; self.role = role
        members = [_M("w1"), _M("w2")]
        agents = [_A("w1", "前端", "fe"), _A("w2", "后端", "be"), _A("c1", "协调者", "coordinator")]

        async def _resolve(content, sender="w1"):
            with patch("engine.worker.crud") as crud_mock:
                crud_mock.list_group_members_with_agent = AsyncMock(return_value=members)
                crud_mock.list_agents = AsyncMock(return_value=agents)
                return await _resolve_handoff_target("g1", "c1", sender, content)

        r_self = asyncio.run(_resolve("@前端 继续"))
        r_coord = asyncio.run(_resolve("@协调者 你来"))
        r_none = asyncio.run(_resolve("接不上了"))
        r_first = asyncio.run(_resolve("@后端 和 @非成员"))
        if r_self is not None or r_coord is not None or r_none is not None or r_first != "w2":
            errs.append(f"[F10] _resolve_handoff_target 守卫破：self={r_self!r}/coord={r_coord!r}/none={r_none!r}/first={r_first!r}")
        else:
            print("[F10] OK  _resolve_handoff_target 四守卫不变（self-skip/coord-skip/none→None/first-wins）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F10] _resolve_handoff_target 守卫检查异常：{type(e).__name__}: {e}")

    return errs


def main() -> int:
    print("=== VH40 回归：GroupState.turn_count + recent_speakers 图内防连发守卫 ===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "图内防连发守卫锁定：\n"
        "  · A make_agent_node 入口读 recent_speakers（先于 brain）+ 命中返 Command(goto=END)；\n"
        "  · B 防连发直调（已发言→END+brain 未调 / 未发言→正常发言不误伤）；\n"
        "  · C 真 StateGraph A→B→A(guarded)→END（front 不被第二次调，recent_speakers 不重复追加）；\n"
        "  · D turn_count 不因守卫命中错位（早返不 bump，A=1/B=2 保留）；\n"
        "  · E 兼容既有契约（正常 A→B→END 链 turn_count=2/recent_speakers=[w1,w2]，vh32 E18 不破）；\n"
        "  · F resident worker 图 + _resolve_handoff_target 四守卫不变。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

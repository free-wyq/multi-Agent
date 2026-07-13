"""VH38 回归：build_group_graph(group) 装配 START→route_entry→{coordinator|agent}+handoff 边，编译通过.

锁住 task-10 决策——group_graph.py ``build_group_graph(group)`` 装配完整群图拓扑：
START→route_entry→{coordinator 子图（classify→…）| agent_<id> 节点（handoff）}→END，编译通过.

设计真源见 memory ``decentralized-scheduling-stop-plan-2026-07-13``（方向 A）。本任务把零散的
节点注册升级为**完整拓扑装配**——coordinator 子图用 GROUP twins（dispatch_next_group /
handle_reply_group / summarize_group）经 Send fan-out 到 agent 节点 + in-graph report-back，
route_after_* 条件边接线（语义保真），agent 节点 handoff 边动态（Command.goto 运行时定）.
任务字面 ``build_group_graph(group)`` 指首参为 Group 对象——build_group_graph 签名升级为多态
首参（Group 对象 OR group_id str），Group 对象时从 group.id / group.coordinator_id 读.

六段契约（纯静态 + 真 StateGraph stub，不依赖 live server / 真实 LLM）：

  A. 签名多态锁——build_group_graph(group, members, coordinator_id)
    1. ``build_group_graph`` 首参 ``group``（多态：Group 对象 OR group_id str）.
    2. Group 对象入参 → group_id=group.id + coordinator_id=group.coordinator_id（未显式覆盖时）.
    3. group_id str 入参（旧 3 参形式）仍编译通过（向后兼容 vh33-vh37 测试调用）.

  B. 完整拓扑锁——START→route_entry + coordinator 子图 + agent 节点共存一图
    4. 编译图含 ``route_entry`` + coordinator 子图节点（classify/llm_decide/chat/dispatch/
       dispatch_next_group/handle_reply_group/summarize_group）+ 每 member 一个 ``agent_<id>`` 节点.
    5. ``START→route_entry`` 静态边存在.
    6. coordinator 子图用 GROUP twins（dispatch_next_group/handle_reply_group/summarize_group），
       非 resident 命名（dispatch_next/handle_reply/summarize）——群图走 Send fan-out + in-graph
       report-back，不经 inbox notify 回路.

  C. 条件边接线锁——route_after_* 语义保真
    7. ``classify`` 经 ``route_after_classify`` 条件边 → {dispatch_next_group, handle_reply_group,
       llm_decide}（path map 路由到 GROUP twins）.
    8. ``llm_decide`` 经 ``route_after_llm_decide`` → {chat, dispatch}.
    9. ``dispatch`` 经 ``route_after_dispatch`` → {dispatch_next_group, END}.
   10. ``chat`` → END 静态边（resident dict-returning 节点走静态边）.

  D. 编译通过锁——无悬空节点 / 无未接线条件边
   11. ``g.compile(checkpointer=MemorySaver())`` 不抛（拓扑合法）.
   12. GROUP twin 节点（dispatch_next_group/handle_reply_group/summarize_group）返 Command(goto=)
       故无出边（LangGraph 跟 Command.goto 走）——这些节点不出现在静态 edges 里是正确的.

  E. 群图跑通锁——route_entry 仍工作（去中心化路径不破）
   13. 真 StateGraph ainvoke：route_entry @mention → agent 节点 handoff 链跑通（vh33 E15
       语义不破——route_entry 拓扑未改，只加 coordinator 子图）.
   14. route_entry 无 @mention → END（去中心化路径话筒落地，vh33 D12 不破）.

  F. 向后兼容锁——resident coordinator 图 + CoordinatorState 不破
   15. ``build_coordinator_graph``（resident 7 节点图）仍编译通过（GroupRuntime 切换前 registry 仍用）.
   16. 群图 + 驻留图共存无 import cycle（main 全量 import OK）.
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

    try:
        from engine.group_graph import build_group_graph, agent_node_name  # type: ignore
    except Exception as e:  # noqa: BLE001
        return [f"[import] 导入失败：{type(e).__name__}: {e}"]

    members = [
        {"agent_id": "w1", "agent_name": "W1", "agent_role": "fe", "system_prompt": ""},
        {"agent_id": "w2", "agent_name": "W2", "agent_role": "be", "system_prompt": ""},
    ]

    # ── A. 签名多态 ──────────────────────────────────────────
    sig = inspect.signature(build_group_graph)
    first_param = next(iter(sig.parameters), "")
    if first_param != "group":
        errs.append(f"[A1] build_group_graph 首参应 'group'（多态），实际 {first_param!r}")
    else:
        print("[A1] OK  build_group_graph(group, members, coordinator_id) 首参 group 多态")

    # A2 Group 对象入参 → group.id + group.coordinator_id
    try:
        class _FakeGroup:
            id = "g_grp_obj"
            coordinator_id = "c_grp_obj"
        g = build_group_graph(_FakeGroup(), members)
        if getattr(g, "_group_id", None) != "g_grp_obj":
            errs.append(f"[A2] Group 对象入参应 _group_id=g_grp_obj，实际 {getattr(g,'_group_id',None)!r}")
        elif getattr(g, "_coordinator_id", None) != "c_grp_obj":
            errs.append(f"[A2] Group 对象入参应 _coordinator_id=c_grp_obj，实际 {getattr(g,'_coordinator_id',None)!r}")
        else:
            print("[A2] OK  Group 对象入参 → group_id=group.id + coordinator_id=group.coordinator_id")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[A2] Group 对象入参测试异常：{type(e).__name__}: {e}")

    # A3 group_id str 入参（旧形式）仍编译
    try:
        g = build_group_graph("g1", members, coordinator_id="c1")
        if getattr(g, "_group_id", None) != "g1":
            errs.append(f"[A3] group_id str 入参应 _group_id=g1，实际 {getattr(g,'_group_id',None)!r}")
        else:
            print("[A3] OK  group_id str 入参（旧 3 参形式）仍编译通过（向后兼容）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[A3] group_id str 入参测试异常：{type(e).__name__}: {e}")

    # ── B. 完整拓扑 ──────────────────────────────────────────
    nodes = set(g.get_graph().nodes.keys())
    required_coord = {"route_entry", "classify", "llm_decide", "chat", "dispatch",
                      "dispatch_next_group", "handle_reply_group", "summarize_group"}
    required_agent = {agent_node_name("w1"), agent_node_name("w2")}
    missing = (required_coord | required_agent) - nodes
    if missing:
        errs.append(f"[B4] 群图缺节点 {sorted(missing)}（nodes={sorted(nodes)}）")
    else:
        print(f"[B4] OK  完整拓扑：route_entry + 7 coordinator 子节点（GROUP twins）+ 2 agent 节点共存一图")

    edges = {(e.source, e.target) for e in g.get_graph().edges}
    if ("__start__", "route_entry") not in edges and not any(s in ("__start__", "") and t == "route_entry" for s, t in edges):
        errs.append(f"[B5] 缺 START→route_entry 边（edges={sorted(edges)}）")
    else:
        print("[B5] OK  START→route_entry 静态边存在")

    # B6 GROUP twins（非 resident 命名）
    if "dispatch_next_group" not in nodes or "handle_reply_group" not in nodes or "summarize_group" not in nodes:
        errs.append("[B6] 群图应用 GROUP twins（dispatch_next_group/handle_reply_group/summarize_group）")
    elif "dispatch_next" in nodes or "handle_reply" in nodes or "summarize" in nodes:
        errs.append(f"[B6] 群图不应含 resident 命名节点（dispatch_next/handle_reply/summarize）——应用 GROUP twins")
    else:
        print("[B6] OK  coordinator 子图用 GROUP twins（Send fan-out + in-graph report-back，不经 inbox notify 回路）")

    # ── C. 条件边接线 ────────────────────────────────────────
    # C7 classify 条件边 → {dispatch_next_group, handle_reply_group, llm_decide}
    # ``get_graph().edges`` does reachability pruning from START, and since
    # route_entry's fan-out to the coordinator path is task-11 (route_entry
    # currently returns Command(goto=END) on the decentralized path), the
    # coordinator subgraph is wired-but-not-yet-reachable from START — so its
    # conditional edges do NOT appear in ``get_graph().edges``. They ARE
    # registered on the builder, though (``g.builder.branches``). We inspect
    # the builder's branch path-maps directly — this verifies the conditional
    # edges are wired (task-10's concern) regardless of route_entry reachability
    # (task-11's concern).
    builder_branches = getattr(g, "builder", None)
    if builder_branches is None or not getattr(builder_branches, "branches", None):
        errs.append("[C7] 编译图无 builder.branches（条件边未注册）")
    else:
        branches = builder_branches.branches
        # C7 classify → {dispatch_next_group, handle_reply_group, llm_decide}
        classify_ends = set()
        if "classify" in branches:
            for bspec in branches["classify"].values():
                classify_ends |= set(getattr(bspec, "ends", {}).values())
        expected_classify = {"dispatch_next_group", "handle_reply_group", "llm_decide"}
        missing_classify = expected_classify - classify_ends
        if missing_classify:
            errs.append(f"[C7] classify 条件边缺目标 {sorted(missing_classify)}（classify_ends={sorted(classify_ends)}）")
        else:
            print(f"[C7] OK  classify 经 route_after_classify 条件边 → {sorted(classify_ends)}（path map 路由到 GROUP twins）")

        # C8 llm_decide → {chat, dispatch}
        llm_ends = set()
        if "llm_decide" in branches:
            for bspec in branches["llm_decide"].values():
                llm_ends |= set(getattr(bspec, "ends", {}).values())
        if not ({"chat", "dispatch"} <= llm_ends):
            errs.append(f"[C8] llm_decide 条件边缺 chat/dispatch（llm_ends={sorted(llm_ends)}）")
        else:
            print(f"[C8] OK  llm_decide 经 route_after_llm_decide → {sorted(llm_ends)}")

        # C9 dispatch → {dispatch_next_group, END}
        dispatch_ends = set()
        if "dispatch" in branches:
            for bspec in branches["dispatch"].values():
                dispatch_ends |= set(getattr(bspec, "ends", {}).values())
        if "dispatch_next_group" not in dispatch_ends or "__end__" not in dispatch_ends:
            errs.append(f"[C9] dispatch 条件边缺 dispatch_next_group/END（dispatch_ends={sorted(dispatch_ends)}）")
        else:
            print(f"[C9] OK  dispatch 经 route_after_dispatch → {sorted(dispatch_ends)}")

    # C10 chat → END is a STATIC edge (resident dict-returning node). Static
    # edges are in builder.edges as (source, target) tuples — but chat is
    # unreachable from START until task-11, so it does NOT appear in
    # get_graph().edges (reachability-pruned). Check the builder's edge set
    # directly (pre-reachability).
    builder_edges = getattr(getattr(g, "builder", None), "edges", None)
    chat_static_to_end = ("chat", "__end__") in builder_edges if builder_edges is not None else False
    if not chat_static_to_end:
        errs.append("[C10] chat 缺 → END 静态边（builder.edges 无 ('chat','__end__')）")
    else:
        print("[C10] OK  chat → END 静态边（resident dict-returning 节点走静态边，builder.edges 含）")

    # ── D. 编译通过 ──────────────────────────────────────────
    # D11 compile 不抛（已 implicitly 验证——build_group_graph 返编译图）
    try:
        from langgraph.pregel import Pregel
        if not isinstance(g, Pregel):
            errs.append(f"[D11] build_group_graph 返回非编译图（{type(g).__name__}）")
        else:
            print("[D11] OK  g.compile(checkpointer=MemorySaver()) 编译通过（拓扑合法，无悬空节点）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D11] 编译图类型检查异常：{type(e).__name__}: {e}")

    # D12 GROUP twin 节点无出边（返 Command.goto，LangGraph 跟 goto 走）
    for twin in ("dispatch_next_group", "handle_reply_group", "summarize_group"):
        twin_out = {t for s, t in edges if s == twin}
        if twin_out:
            errs.append(f"[D12] {twin} 不应有静态出边（返 Command.goto）——实际 {sorted(twin_out)}")
    if not any(e.startswith("[D12]") for e in errs):
        print("[D12] OK  GROUP twin 节点无静态出边（返 Command.goto，LangGraph 跟 goto 走 Send fan-out / summarize_group / END）")

    # ── E. 群图跑通（route_entry 不破）──────────────────────
    try:
        async def _run_e13():
            from langchain_core.messages import HumanMessage
            from engine import coordinator as coord_mod
            from engine import worker as worker_mod
            async def fs_front(config, messages, group_id, agent_id):
                return ("r1", '{"action":"chat","content":"前端接 @后端工程师","reasoning":"r"}', 5, 50, "m1", 0, "")
            async def fs_back(config, messages, group_id, agent_id):
                return ("r2", '{"action":"chat","content":"后端接结束","reasoning":"r"}', 5, 50, "m1", 0, "")
            streams = {"w1": fs_front, "w2": fs_back}
            async def dispatcher(config, messages, group_id, agent_id):
                return await streams[agent_id](config, messages, group_id, agent_id)
            with patch.object(worker_mod, "_stream_brain_decision", side_effect=dispatcher), \
                 patch.object(worker_mod, "_unified_reply", AsyncMock()), \
                 patch.object(worker_mod, "_build_context_from_db", AsyncMock(return_value="ctx")), \
                 patch.object(worker_mod, "_format_display_msg", side_effect=lambda s, c: c), \
                 patch.object(worker_mod, "get_llm_config", return_value={"model": "m1"}), \
                 patch.object(worker_mod, "crud") as crud_mock, \
                 patch.object(worker_mod, "find_mentions", side_effect=lambda c: ["后端工程师"] if "前端接" in c or "开始" in c else []), \
                 patch.object(worker_mod, "resolve_mention", return_value="w2"):
                class _M:
                    def __init__(self, aid): self.agent_id = aid; self.agent_name = aid; self.agent_role = "r"
                crud_mock.list_group_members_with_agent = AsyncMock(return_value=[_M("w1"), _M("w2")])
                crud_mock.list_agents = AsyncMock(return_value=[])
                gg = build_group_graph("g1", members, coordinator_id="c1")
                return await gg.ainvoke({
                    "group_id": "g1", "coordinator_id": "c1",
                    "messages": [HumanMessage(content="开始接龙 @后端工程师", name="user", id="u1")],
                    "turn_count": 0, "recent_speakers": [],
                    "incoming_message": "开始接龙 @后端工程师", "incoming_sender": "user",
                }, config={"configurable": {"thread_id": "vh38-e13"}})
        r = asyncio.run(_run_e13())
        if len(r.get("messages", [])) < 2:
            errs.append(f"[E13] route_entry handoff 链应产 ≥2 msgs（vh33 E15 不破），实际 {len(r.get('messages', []))}")
        else:
            print(f"[E13] OK  route_entry @mention → agent handoff 链跑通（{len(r['messages'])} msgs，vh33 E15 拓扑不破）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[E13] route_entry handoff 链测试异常：{type(e).__name__}: {e}")

    # E14 route_entry 无 @mention → END（不破）
    try:
        async def _run_e14():
            from engine import worker as worker_mod
            with patch.object(worker_mod, "_resolve_handoff_target", return_value=None):
                gg = build_group_graph("g1", members, coordinator_id="c1")
                from langgraph.types import Command
                re_fn = None
                # route_entry is closure-bound; call via ainvoke
                return await gg.ainvoke({
                    "group_id": "g1", "coordinator_id": "c1",
                    "incoming_message": "大家好", "incoming_sender": "user",
                    "turn_count": 0,
                }, config={"configurable": {"thread_id": "vh38-e14"}})
        r = asyncio.run(_run_e14())
        # no @mention → END; turn_count bumped but no agent spoke
        if r.get("turn_count") != 1:
            errs.append(f"[E14] route_entry 无 @mention 应 bump turn_count=1 后 END，实际 {r.get('turn_count')}")
        elif len(r.get("messages", [])) != 0:
            errs.append(f"[E14] route_entry 无 @mention 应无 agent 发言，实际 {len(r.get('messages', []))} msgs")
        else:
            print("[E14] OK  route_entry 无 @mention → END（去中心化话筒落地，vh33 D12 不破）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[E14] route_entry no-@mention 测试异常：{type(e).__name__}: {e}")

    # ── F. 向后兼容 ──────────────────────────────────────────
    try:
        from engine.coordinator import build_coordinator_graph
        resident = build_coordinator_graph()
        if resident is None:
            errs.append("[F15] build_coordinator_graph 返 None（编译失败）")
        else:
            print("[F15] OK  build_coordinator_graph（resident 7 节点图）仍编译通过（registry 未切换前仍用）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F15] resident 图编译异常：{type(e).__name__}: {e}")

    try:
        import main  # noqa: F401
        print("[F16] OK  群图 + 驻留图共存编译无 import cycle（main 全量 import OK）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F16] main import 异常（import cycle？）：{type(e).__name__}: {e}")

    return errs


END_NAME = "__end__"


def main() -> int:
    print("=== VH38 回归：build_group_graph(group) 装配完整群图拓扑（去中心化 handoff 迁移·拓扑装配层）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "build_group_graph(group) 完整拓扑装配锁定：\n"
        "  · A 签名多态（group 首参：Group 对象 OR group_id str，Group 对象读 id/coordinator_id）；\n"
        "  · B 完整拓扑（route_entry + 7 coordinator 子节点 GROUP twins + agent 节点共存一图 + START→route_entry）；\n"
        "  · C 条件边接线（classify/llm_decide/dispatch 经 route_after_* → GROUP twins，chat→END）；\n"
        "  · D 编译通过（compile 不抛，GROUP twin 节点返 Command.goto 无静态出边）；\n"
        "  · E 群图跑通（route_entry @mention handoff 链 + 无 @→END，vh33 拓扑不破）；\n"
        "  · F 向后兼容（resident build_coordinator_graph 仍编译 + 无 import cycle）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

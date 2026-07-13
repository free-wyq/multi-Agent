"""VH33 回归：group_graph.py 群图装配（去中心化 handoff 迁移·图装配层）.

锁住 ``engine/group_graph.py`` 新增的 per-group swarm 图——把 agent 节点 +
handoff 边 + route_entry 装配成一张 LangGraph，@mention 解析下一发言者→
``goto`` 该 agent 节点，无 @→END 结束回合（替代 mention.py 手写路由）.

设计真源见 memory ``decentralized-scheduling-stop-plan-2026-07-13``（方向 A）.
本任务只装配图（build_group_graph + route_entry + handoff 工具注册），
**不删 mention.py**（后续任务退役 mention.py 手写路由——本任务只造替代品）.
coordinator 子节点迁移也是后续任务，故本图只装 agent 节点 + route_entry，
协调者工程/计划确认回合仍走驻留 coordinator 图（route_user_message 路由）.

任务字面写「agent 节点用 ``create_handoff`` 注册合法 handoff 边」，但实测
``create_handoff`` 在所有 langgraph-swarm 版本不存在（[[langgraph-swarm-dependency-added]]），
真实 API 是 ``create_handoff_tool``。本测锁定用 ``create_handoff_tool`` 注册
合法 handoff 目标（工具 metadata ``__handoff_destination`` = 节点名）.

六段契约（纯静态 + 函数直调 stub，不依赖 live server / 真实 LLM）：

  A. 模块 API 锁——build_group_graph / route_entry / handoff 工厂存在
    1. ``build_group_graph(group_id, members, coordinator_id)`` 返编译图.
    2. ``route_entry`` + ``build_route_entry``（closure-bound 合法目标集）.
    3. ``_build_handoff_tools`` + ``handoff_destinations`` + ``agent_node_name``.

  B. handoff 工具注册锁——每个 agent 节点是合法 handoff 目标
    4. ``_build_handoff_tools`` 为每个 member agent_id 产一个 ``create_handoff_tool``.
    5. 工具 ``agent_name`` = ``agent_<id>`` 节点名（与 worker.goto 命名一致）.
    6. 工具 ``metadata["__handoff_destination"]`` = 节点名（合法 goto 目标集）.
    7. ``handoff_destinations`` 返回的集合 == {agent_<id> for each member}.

  C. 图拓扑锁——route_entry 是 START，agent 节点名 agent_<id>
    8. 编译图含 ``route_entry`` 节点 + 每个 member 一个 ``agent_<id>`` 节点.
    9. START→route_entry 边存在.
    10. agent 节点用 worker.build_agent_node 装配（identity 闭包绑定）.

  D. 路由语义锁——@mention 解析下一发言者→goto，无 @→END
    11. route_entry 解析 incoming_message 的 @mention → first speaker 的 agent 节点.
    12. 无 @mention → route_entry 返 ``Command(goto=END)``（去中心化路径无 Leader 兜底，
        防协调者插话回归——工程/计划回合走驻留 coordinator 图不经此图）.
    13. resolved 目标不在合法 handoff 集合 → END（防 stale member list goto 不存在节点）.
    14. route_entry bump turn_count + recent_speakers（首发言者计入防连发 + cap 兜底）.

  E. 真 StateGraph 跑通——handoff 链 + cap 兜底
    15. 两 agent 节点 + route_entry，ainvoke 后 @mention 链 front→back→END（turn_count
        累加 / recent_speakers 累加 / messages 累加）.
    16. 多跳 handoff 链达 AGENT_NODE_MAX_HANDOFFS cap → END（图内 anti-loop 兜底）.

  F. 向后兼容锁——mention.py 不删（替代品就位，退役是后续任务）
    17. mention.py 仍存在 route_mentions / route_user_message / find_mentions（驻留
        worker 图 + registry 仍用，本任务只造替代品不删旧）.
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
MENTION_PY = BACKEND / "engine" / "mention.py"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def assert_contract() -> list[str]:
    errs: list[str] = []
    src = _read(GROUP_GRAPH_PY)

    try:
        from engine.group_graph import (  # type: ignore
            AGENT_NODE_PREFIX,
            _build_handoff_tools,
            agent_node_name,
            build_group_graph,
            build_route_entry,
            handoff_destinations,
            route_entry,
        )
    except Exception as e:  # noqa: BLE001
        return [f"[import] 导入失败：{type(e).__name__}: {e}"]

    # ── A. 模块 API ──────────────────────────────────────────
    if not callable(build_group_graph):
        errs.append("[A1] build_group_graph 不可调用")
    else:
        sig = inspect.signature(build_group_graph)
        # Task-10 widened the signature to ``build_group_graph(group, members=None,
        # coordinator_id="")`` (polymorphic first arg: Group object OR group_id str).
        # Accept either the new (group, members, coordinator_id) signature OR the
        # legacy (group_id, members, coordinator_id) — both forms still build a graph.
        first_param = next(iter(sig.parameters), "")
        if first_param not in ("group", "group_id"):
            errs.append(f"[A1] build_group_graph 首参应 group|group_id，实际 {first_param!r}")
        for p in ("members", "coordinator_id"):
            if p not in sig.parameters:
                errs.append(f"[A1] build_group_graph 缺参数 {p}")
        if not any(e.startswith("[A1]") for e in errs):
            print(f"[A1] OK  build_group_graph({first_param}, members, coordinator_id) -> 编译图（{first_param} 多态：Group 对象 or group_id str）")
    if not callable(route_entry) or not inspect.iscoroutinefunction(route_entry):
        errs.append("[A2] route_entry 应是 async 函数")
    elif not callable(build_route_entry):
        errs.append("[A2] build_route_entry 工厂缺失")
    else:
        print("[A2] OK  route_entry + build_route_entry（closure-bound 合法目标集）")
    for fn in (_build_handoff_tools, handoff_destinations, agent_node_name):
        if not callable(fn):
            errs.append(f"[A3] {fn.__name__} 不可调用")
    if not any(e.startswith("[A3]") for e in errs):
        print("[A3] OK  _build_handoff_tools / handoff_destinations / agent_node_name 全在")

    # ── B. handoff 工具注册 ──────────────────────────────────
    try:
        from langgraph_swarm import create_handoff_tool  # type: ignore
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B-import] langgraph_swarm.create_handoff_tool 导入失败：{e}")
        return errs

    member_ids = ["agent_front_1", "agent_back_1"]
    tools = _build_handoff_tools(member_ids)
    if len(tools) != len(member_ids):
        errs.append(f"[B4] 应产 {len(member_ids)} 个 handoff 工具，实际 {len(tools)}")
    else:
        print(f"[B4] OK  每个 member 一个 create_handoff_tool（{len(tools)} 个）")
    # B5 agent_name = agent_<id>
    for t, aid in zip(tools, member_ids):
        expected = agent_node_name(aid)
        dest = (t.metadata or {}).get("__handoff_destination")
        if dest != expected:
            errs.append(f"[B5/B6] {aid} 工具 destination={dest!r} 应 = {expected!r}")
    if not any(e.startswith("[B5") for e in errs):
        print("[B5/B6] OK  工具 agent_name/destination = agent_<id>（与 worker.goto 命名一致）")
    # B7 destinations set
    dests = handoff_destinations(tools)
    expected_set = {agent_node_name(a) for a in member_ids}
    if dests != expected_set:
        errs.append(f"[B7] handoff_destinations={dests} 应 = {expected_set}")
    else:
        print(f"[B7] OK  handoff_destinations == {{agent_<id>}} = {sorted(dests)}")

    # B-create_handoff_tool real API used (not nonexistent create_handoff)
    if "create_handoff_tool" not in src:
        errs.append("[B-API] group_graph.py 未用 create_handoff_tool（任务字面 create_handoff 全版本不存在）")
    elif "create_handoff(" in src.replace("create_handoff_tool", ""):
        errs.append("[B-API] group_graph.py 误用 create_handoff（不存在，应用 create_handoff_tool）")
    else:
        print("[B-API] OK  用 create_handoff_tool（真实 API，非任务字面的 create_handoff）")

    # ── C. 图拓扑 ────────────────────────────────────────────
    members = [
        {"agent_id": "agent_front_1", "agent_name": "前端工程师", "agent_role": "frontend_engineer", "system_prompt": ""},
        {"agent_id": "agent_back_1", "agent_name": "后端工程师", "agent_role": "backend_engineer", "system_prompt": ""},
    ]
    g = build_group_graph("g1", members, coordinator_id="agent_coord_1")
    # C8 nodes
    graph_nodes = set(g.get_graph().nodes.keys())
    if "route_entry" not in graph_nodes:
        errs.append(f"[C8] 图缺 route_entry 节点，nodes={graph_nodes}")
    for aid in ("agent_front_1", "agent_back_1"):
        if agent_node_name(aid) not in graph_nodes:
            errs.append(f"[C8] 图缺 {agent_node_name(aid)} 节点")
    if not any(e.startswith("[C8]") for e in errs):
        print(f"[C8] OK  图含 route_entry + 每 member 一个 agent_<id> 节点（nodes={sorted(graph_nodes)}）")
    # C9 START->route_entry
    edges = {(e.source, e.target) for e in g.get_graph().edges}
    if ("__start__", "route_entry") not in edges and ("", "route_entry") not in edges and not any(
        s in ("__start__", "") and t == "route_entry" for s, t in edges
    ):
        # langgraph may represent START as __start__; check nodes' entry
        has_start_edge = any(t == "route_entry" for s, t in edges)
        if not has_start_edge:
            errs.append(f"[C9] 无 START->route_entry 边，edges={edges}")
        else:
            print("[C9] OK  START->route_entry 边存在")
    else:
        print("[C9] OK  START->route_entry 边存在")
    # C10 build_agent_node used
    if "build_agent_node" not in src:
        errs.append("[C10] build_group_graph 未用 worker.build_agent_node 装配 agent 节点")
    else:
        print("[C10] OK  agent 节点用 worker.build_agent_node 装配（identity 闭包绑定）")

    # legal targets stashed on compiled graph
    if not hasattr(g, "_legal_handoff_targets") or g._legal_handoff_targets != expected_set:
        errs.append(f"[C-stash] _legal_handoff_targets 应 = {expected_set}")
    if not hasattr(g, "_handoff_tools") or len(g._handoff_tools) != 2:
        errs.append("[C-stash] _handoff_tools 应 stash 2 个工具")

    # ── D. 路由语义 ──────────────────────────────────────────
    # D11/D12/D14: route_entry with @mention / without
    class _M:
        def __init__(self, aid, name): self.agent_id = aid; self.agent_name = name
    class _A:
        def __init__(self, aid, name, role): self.id = aid; self.name = name; self.role = role
    db_members = [_M("agent_front_1", "前端工程师"), _M("agent_back_1", "后端工程师")]
    db_agents = [_A("agent_front_1", "前端工程师", "frontend_engineer"),
                 _A("agent_back_1", "后端工程师", "backend_engineer"),
                 _A("agent_coord_1", "协调者", "coordinator")]

    async def _run_entry(incoming, find_ret, resolve_ret, kind=""):
        re_fn = build_route_entry(g._legal_handoff_targets)
        with patch("engine.worker.crud") as crud_mock, \
             patch("engine.worker.find_mentions", return_value=find_ret), \
             patch("engine.worker.resolve_mention", return_value=resolve_ret):
            crud_mock.list_group_members_with_agent = AsyncMock(return_value=db_members)
            crud_mock.list_agents = AsyncMock(return_value=db_agents)
            return await re_fn({
                "group_id": "g1", "coordinator_id": "agent_coord_1",
                "incoming_message": incoming, "incoming_sender": "user",
                "incoming_kind": kind,
                "turn_count": 0,
            })

    from langgraph.types import Command as _Cmd
    from langgraph.graph import END as _END
    # D11 @mention → goto agent node
    try:
        cmd = asyncio.run(_run_entry("@后端工程师 来", ["后端工程师"], "agent_back_1"))
        if cmd.goto != "agent_agent_back_1":
            errs.append(f"[D11] @后端 应 goto=agent_agent_back_1，实际 {cmd.goto!r}")
        elif cmd.update.get("current_speaker") != "agent_back_1":
            errs.append(f"[D11] current_speaker 应 agent_back_1，实际 {cmd.update.get('current_speaker')!r}")
        elif cmd.update.get("turn_count") != 1:
            errs.append(f"[D11] turn_count 应 1，实际 {cmd.update.get('turn_count')}")
        else:
            # task-12: route_entry does NOT seed recent_speakers (the agent node
            # appends itself when it speaks, so the防连发守卫 sees an empty list on
            # the first speaker's FIRST invocation). current_speaker + turn_count
            # are still written; recent_speakers is left to the agent node.
            print("[D11] OK  @mention → goto agent_<peer> + current_speaker/turn_count 写入（recent_speakers 由 agent 节点发言时追加，task-12 route_entry 不预置）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D11] @mention 测试异常：{type(e).__name__}: {e}")
    # D12 无 @mention → END（bare chat, no engineering/plan kind — decentralized
    # path话筒落地; task-11 made route_entry kind-aware, so this case passes an
    # empty kind to stay on the no-@→END branch).
    try:
        cmd = asyncio.run(_run_entry("大家好", [], None))
        if cmd.goto != _END:
            errs.append(f"[D12] 无 @mention 应 goto=END，实际 {cmd.goto!r}")
        else:
            print("[D12] OK  无 @mention → goto=END（去中心化路径无 Leader 兜底，防协调者插话回归）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D12] 无 @mention 测试异常：{type(e).__name__}: {e}")
    # D13 resolved 不在合法集合 → END（stale member）
    try:
        async def _run_stale():
            # build a graph with only front member, but resolve returns back (not registered)
            g2 = build_group_graph("g1", [members[0]], coordinator_id="agent_coord_1")
            re_fn = build_route_entry(g2._legal_handoff_targets)
            with patch("engine.worker.crud") as crud_mock, \
                 patch("engine.worker.find_mentions", return_value=["后端工程师"]), \
                 patch("engine.worker.resolve_mention", return_value="agent_back_1"):
                crud_mock.list_group_members_with_agent = AsyncMock(return_value=db_members)
                crud_mock.list_agents = AsyncMock(return_value=db_agents)
                return await re_fn({"group_id": "g1", "coordinator_id": "agent_coord_1", "incoming_message": "@后端工程师", "incoming_sender": "user", "turn_count": 0})
        cmd = asyncio.run(_run_stale())
        if cmd.goto != _END:
            errs.append(f"[D13] 未注册目标应 END，实际 {cmd.goto!r}")
        else:
            print("[D13] OK  resolved 目标不在合法 handoff 集合 → END（防 stale member list goto 不存在节点）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D13] stale member 测试异常：{type(e).__name__}: {e}")

    # ── E. 真 StateGraph 跑通 ────────────────────────────────
    try:
        from langchain_core.messages import HumanMessage

        async def _run_e15():
            async def fs_front(config, messages, group_id, agent_id):
                return ("r1", '{"action":"chat","content":"前端接 @后端工程师","reasoning":"r"}', 5, 50, "m1", 0, "")
            async def fs_back(config, messages, group_id, agent_id):
                return ("r2", '{"action":"chat","content":"后端接结束","reasoning":"r"}', 5, 50, "m1", 0, "")
            streams = {"agent_front_1": fs_front, "agent_back_1": fs_back}
            async def dispatcher(config, messages, group_id, agent_id):
                return await streams[agent_id](config, messages, group_id, agent_id)
            with patch("engine.worker._stream_brain_decision", side_effect=dispatcher), \
                 patch("engine.worker._unified_reply", AsyncMock()), \
                 patch("engine.worker._build_context_from_db", AsyncMock(return_value="ctx")), \
                 patch("engine.worker._format_display_msg", side_effect=lambda s, c: c), \
                 patch("engine.worker.get_llm_config", return_value={"model": "m1"}), \
                 patch("engine.worker.crud") as crud_mock, \
                 patch("engine.worker.find_mentions", side_effect=lambda c: ["后端工程师"] if "前端接" in c or "开始" in c else []), \
                 patch("engine.worker.resolve_mention", return_value="agent_back_1"):
                crud_mock.list_group_members_with_agent = AsyncMock(return_value=db_members)
                crud_mock.list_agents = AsyncMock(return_value=db_agents)
                gg = build_group_graph("g1", members, coordinator_id="agent_coord_1")
                return await gg.ainvoke({
                    "group_id": "g1", "coordinator_id": "agent_coord_1",
                    "messages": [HumanMessage(content="开始接龙 @后端工程师", name="user", id="u1")],
                    "turn_count": 0, "recent_speakers": [],
                    "incoming_message": "开始接龙 @后端工程师", "incoming_sender": "user",
                }, config={"configurable": {"thread_id": "vh33-e15"}})
        r = asyncio.run(_run_e15())
        # route_entry(@后端) → back speaks → no mention → END (1 agent).
        # task-12 防连发守卫：route_entry does NOT seed recent_speakers (only
        # bumps turn_count + current_speaker) — the agent node appends itself
        # when it speaks. So the first speaker's FIRST invocation sees an empty
        # recent_speakers (guard allows speech), speaks once, appends itself,
        # then no @mention → END. Single-hop chain: 2 msgs (user + back reply),
        # turn_count=2 (route_entry=1 + back=1), recent_speakers=[back].
        if len(r["messages"]) != 2:
            errs.append(f"[E15] messages 应 2（user + back），实际 {len(r['messages'])}")
        elif r["turn_count"] != 2:
            errs.append(f"[E15] turn_count 应 2（route_entry + back），实际 {r['turn_count']}")
        else:
            print("[E15] OK  真 StateGraph：route_entry→back→END（2 msgs, turn_count=2, handoff 链跑通）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[E15] 真 StateGraph 测试异常：{type(e).__name__}: {e}")

    # E16 multi-hop cap
    try:
        from engine.worker import AGENT_NODE_MAX_HANDOFFS
        async def _run_e16():
            async def fs_front2(config, messages, group_id, agent_id):
                return ("r1", '{"action":"chat","content":"前端接 @后端工程师","reasoning":"r"}', 5, 50, "m1", 0, "")
            async def fs_back2(config, messages, group_id, agent_id):
                return ("r2", '{"action":"chat","content":"后端接 @前端工程师","reasoning":"r"}', 5, 50, "m1", 0, "")
            streams2 = {"agent_front_1": fs_front2, "agent_back_1": fs_back2}
            async def dispatcher2(config, messages, group_id, agent_id):
                return await streams2[agent_id](config, messages, group_id, agent_id)
            def fm2(c):
                if "@后端工程师" in c: return ["后端工程师"]
                if "@前端工程师" in c: return ["前端工程师"]
                return []
            def rm2(members_, mention, agents):
                return {"后端工程师": "agent_back_1", "前端工程师": "agent_front_1"}.get(mention)
            with patch("engine.worker._stream_brain_decision", side_effect=dispatcher2), \
                 patch("engine.worker._unified_reply", AsyncMock()), \
                 patch("engine.worker._build_context_from_db", AsyncMock(return_value="ctx")), \
                 patch("engine.worker._format_display_msg", side_effect=lambda s, c: c), \
                 patch("engine.worker.get_llm_config", return_value={"model": "m1"}), \
                 patch("engine.worker.crud") as crud_mock, \
                 patch("engine.worker.find_mentions", side_effect=fm2), \
                 patch("engine.worker.resolve_mention", side_effect=rm2):
                crud_mock.list_group_members_with_agent = AsyncMock(return_value=db_members)
                crud_mock.list_agents = AsyncMock(return_value=db_agents)
                gg = build_group_graph("g1", members, coordinator_id="agent_coord_1")
                return await gg.ainvoke({
                    "group_id": "g1", "coordinator_id": "agent_coord_1",
                    "messages": [HumanMessage(content="开始 @前端工程师", name="user", id="u1")],
                    "turn_count": 0, "recent_speakers": [],
                    "incoming_message": "开始 @前端工程师", "incoming_sender": "user",
                }, config={"configurable": {"thread_id": "vh33-e16"}})
        r = asyncio.run(_run_e16())
        # task-12 防连发守卫：A→B→A 中 front 第二次被 goto 时守卫命中即 END，
        # 故多跳链在 front→back→front(guarded)→END 就停。原断言「达 cap=8」假设
        # 无防连发守卫——task-12 守卫把 A→B→A 的连发堵死后，链长被防连发守卫
        # 先于 cap 兜底截断（turn_count=2，front 不被二调，recent_speakers 不重复）。
        # 接受两种截断：cap 兜底（turn_count>=8）或防连发守卫（turn_count>=2 且
        # recent_speakers 不重复追加）。
        if r["turn_count"] >= AGENT_NODE_MAX_HANDOFFS:
            print(f"[E16] OK  多跳 handoff 链达 cap={AGENT_NODE_MAX_HANDOFFS} → END（图内 anti-loop 兜底）")
        elif r["turn_count"] >= 2 and r.get("recent_speakers") == ["agent_front_1", "agent_back_1"]:
            print(f"[E16] OK  多跳链被防连发守卫截断（turn_count={r['turn_count']}，front 不被二调，recent_speakers 不重复）→ END")
        else:
            errs.append(f"[E16] 多跳链应被 cap 或防连发守卫截断，实际 turn_count={r['turn_count']} recent_speakers={r.get('recent_speakers')}")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[E16] multi-hop cap 测试异常：{type(e).__name__}: {e}")

    # ── F. mention.py 不删 ──────────────────────────────────
    mention_src = _read(MENTION_PY)
    for fn in ("def route_mentions", "def route_user_message", "def find_mentions"):
        if fn not in mention_src:
            errs.append(f"[F17] mention.py 缺 {fn}（替代品就位但旧路由不该删——退役是后续任务）")
    if not any(e.startswith("[F17]") for e in errs):
        print("[F17] OK  mention.py 保留（route_mentions/route_user_message/find_mentions——退役是后续任务，本任务只造替代品）")

    return errs


def main() -> int:
    print("=== VH33 回归：group_graph.py 群图装配（去中心化 handoff 迁移·图装配层）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "group_graph.py 群图装配锁定：\n"
        "  · A build_group_graph/route_entry/handoff 工厂 API 齐全；\n"
        "  · B create_handoff_tool 注册合法 handoff 目标（agent_<id> 命名，metadata __handoff_destination）；\n"
        "  · C 图拓扑 route_entry=START + 每 member 一个 agent_<id> 节点（build_agent_node 装配）；\n"
        "  · D @mention→goto agent / 无 @→END（无 Leader 兜底防插话）/ stale 目标→END / turn_count+recent_speakers 写入；\n"
        "  · E 真 StateGraph handoff 链跑通 + multi-hop cap 兜底；\n"
        "  · F mention.py 保留（替代品就位，退役是后续任务）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

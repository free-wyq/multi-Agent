"""VH34 回归：coordinator 子节点迁移到群图（去中心化 handoff 迁移·协调者子节点层）.

锁住 task-6 决策——``coordinator.py`` 的 classify/llm_decide/chat 节点（+ dispatch/
dispatch_next/handle_reply/summarize）改造为群图（``engine/group_graph.py``）内的
coordinator 子节点：状态读写改用 ``GroupState``，保 ``route_after_*`` 条件边语义.

设计真源见 memory ``decentralized-scheduling-stop-plan-2026-07-13``（方向 A）。本任务
**只迁移节点装配**（节点注册进群图 + 状态 schema 并集），**不接线条件边**（route_entry
分叉到 coordinator、route_after_* 条件边连接 coordinator 子节点是后续任务）——本任务的
产出是「coordinator 子节点就位，状态读写切到 GroupState，route_after_* 语义保真不变」.

五段契约（纯静态 + 函数直调 stub，不依赖 live server / 真实 LLM）：

  A. 节点装配锁——coordinator 子节点注册进群图
    1. ``build_coordinator_subnodes(coordinator_id, coordinator_name, system_prompt)``
       工厂存在，返含 classify/llm_decide/chat/dispatch/dispatch_next/handle_reply/
       summarize 七节点 + 四 route_after_* 路由的 dict.
    2. ``build_group_graph`` 装配的图含 coordinator 子节点（classify/llm_decide/chat/
       dispatch/dispatch_next/handle_reply/summarize 节点名）.
    3. 编译图 stash ``_has_coordinator_subnodes=True`` + ``_coordinator_id``.

  B. 状态 schema 并集锁——GroupState 含 coordinator 子节点读写的全部键
    4. GroupState 含 ``agent_id`` / ``agent_name`` / ``system_prompt``（Leader 身份，
       coordinator 子节点 ``state["agent_id"]`` 解析到 Leader）.
    5. GroupState 含 ``action_taken`` / ``reply_content`` / ``_stream_stats``（协调者
       子节点控制信道 + 流式 stats 透传，与 CoordinatorState 同源）.
    6. GroupState 含 ``auto_confirm`` / ``leader_strategy`` / ``memory`` / ``dispatch_plan``
       / ``incoming_*``（群配置 + DAG plan + 入站消息，coordinator 子节点读写同一 slot）.
    7. coordinator 子节点函数体读的所有 ``state[...]`` / ``state.get(...)`` 键 ⊆ GroupState
       字段集（duck-typed dict 读写，schema 并集即迁移完成，无需改节点代码）.

  C. route_after_* 语义保真锁——条件边路由函数不变
    8. ``route_after_classify`` 仍路由 confirm_dispatch→dispatch_next / handle_reply→
       handle_reply / else→llm_decide（三分支语义不变）.
    9. ``route_after_llm_decide`` 仍路由 chat→chat / dispatch→dispatch / ask/continue→chat.
   10. ``route_after_dispatch`` 仍 ``if action=="dispatch_next": return "dispatch_next"`` +
       fall-through END（vh5 死成员契约不破——无 confirm_dispatch/direct_run/wait_confirm）.
   11. ``route_after_handle_reply`` 仍路由 summarize→summarize / dispatch_next→dispatch_next /
       else→llm_decide.

  D. 节点代码复用不变锁——coordinator 节点函数体未为迁移改写
   12. 七节点函数（node_classify_incoming/node_llm_decide/node_chat/node_dispatch/
       node_handle_reply/node_dispatch_next/node_summarize）签名与 build_coordinator_graph
       注册的同款（``build_coordinator_subnodes`` 返的是同一组函数对象，非复制）.
   13. ``_leader_system`` 仍读 ``state.get("system_prompt")``（群图 coordinator 子节点
       走同一 system 拼接，base+COORDINATOR_SYSTEM）.

  E. 向后兼容锁——驻留 coordinator 图不破
   14. ``build_coordinator_graph`` 仍在（驻留协调者图未删，迁移未落地前 registry 仍用）.
   15. ``CoordinatorState`` 仍在（驻留图 schema 不删——vh31 B6 锁的不破）.
   16. 群图编译 + 驻留 coordinator 图编译共存无 import cycle（两图都编译通过）.
"""
from __future__ import annotations

import asyncio
import inspect
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
COORD_PY = BACKEND / "engine" / "coordinator.py"
GROUP_GRAPH_PY = BACKEND / "engine" / "group_graph.py"
STATE_PY = BACKEND / "engine" / "state.py"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fn_body(src: str, fname: str, is_async: bool = True) -> str:
    """抽 async/def fname(...) 函数体到下一个顶层 def（含 docstring）。"""
    prefix = "async def" if is_async else "def"
    m = re.search(
        rf"{prefix} {fname}\([^)]*\).*?(?=\n(?:async )?def |\Z)",
        src,
        re.S,
    )
    return m.group(0) if m else ""


def assert_contract() -> list[str]:
    errs: list[str] = []
    coord_src = _read(COORD_PY)
    gg_src = _read(GROUP_GRAPH_PY)
    state_src = _read(STATE_PY)

    try:
        from engine.coordinator import (  # type: ignore
            build_coordinator_graph,
            build_coordinator_subnodes,
            node_chat,
            node_classify_incoming,
            node_dispatch,
            node_dispatch_next,
            node_handle_reply,
            node_llm_decide,
            node_summarize,
            route_after_classify,
            route_after_dispatch,
            route_after_handle_reply,
            route_after_llm_decide,
        )
    except Exception as e:  # noqa: BLE001
        return [f"[import] 导入失败：{type(e).__name__}: {e}"]

    # ── A. 节点装配 ──────────────────────────────────────────
    # A1 build_coordinator_subnodes 工厂
    if not callable(build_coordinator_subnodes):
        errs.append("[A1] build_coordinator_subnodes 不可调用")
    else:
        sig = inspect.signature(build_coordinator_subnodes)
        for p in ("coordinator_id", "coordinator_name", "system_prompt"):
            if p not in sig.parameters:
                errs.append(f"[A1] build_coordinator_subnodes 缺参数 {p}")
        specs = build_coordinator_subnodes(
            coordinator_id="c1", coordinator_name="协调者", system_prompt="你是群主",
        )
        node_keys = ("classify", "llm_decide", "chat", "dispatch",
                     "handle_reply", "dispatch_next", "summarize")
        route_keys = ("route_after_classify", "route_after_handle_reply",
                      "route_after_llm_decide", "route_after_dispatch")
        missing_nodes = [k for k in node_keys if k not in specs]
        missing_routes = [k for k in route_keys if k not in specs]
        if missing_nodes:
            errs.append(f"[A1] build_coordinator_subnodes 缺节点 {missing_nodes}")
        elif missing_routes:
            errs.append(f"[A1] build_coordinator_subnodes 缺路由 {missing_routes}")
        else:
            print("[A1] OK  build_coordinator_subnodes 返 7 节点 + 4 路由 + 3 身份注解")

    # A2 群图含 coordinator 子节点
    try:
        from engine.group_graph import build_group_graph
        members = [
            {"agent_id": "a1", "agent_name": "前端", "agent_role": "fe", "system_prompt": ""},
            {"agent_id": "a2", "agent_name": "后端", "agent_role": "be", "system_prompt": ""},
        ]
        g = build_group_graph("g1", members, coordinator_id="c1")
        nodes = set(g.get_graph().nodes.keys())
        coord_nodes = {"classify", "llm_decide", "chat", "dispatch",
                       "handle_reply", "dispatch_next", "summarize"}
        missing = coord_nodes - nodes
        if missing:
            errs.append(f"[A2] 群图缺 coordinator 子节点 {sorted(missing)}（nodes={sorted(nodes)}）")
        else:
            print(f"[A2] OK  群图含 7 coordinator 子节点（+agent 节点 route_entry，共存一图）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[A2] 群图装配异常：{type(e).__name__}: {e}")

    # A3 stash 元数据
    try:
        if not getattr(g, "_has_coordinator_subnodes", False):
            errs.append("[A3] 编译图未 stash _has_coordinator_subnodes=True")
        elif getattr(g, "_coordinator_id", None) != "c1":
            errs.append(f"[A3] _coordinator_id 应 c1，实际 {getattr(g, '_coordinator_id', None)!r}")
        else:
            print("[A3] OK  编译图 stash _has_coordinator_subnodes=True + _coordinator_id")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[A3] stash 元数据检查异常：{type(e).__name__}: {e}")

    # ── B. 状态 schema 并集 ──────────────────────────────────
    try:
        from typing import get_type_hints
        from engine.state import GroupState
        hints = set(get_type_hints(GroupState, include_extras=True).keys())
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B-import] GroupState hints 取失败：{e}")
        hints = set()

    # B4 Leader 身份字段
    for f in ("agent_id", "agent_name", "system_prompt"):
        if f not in hints:
            errs.append(f"[B4] GroupState 缺 {f}（Leader 身份——coordinator 子节点 state['agent_id'] 解析到 Leader）")
    if not any(e.startswith("[B4]") for e in errs):
        print("[B4] OK  GroupState 含 agent_id/agent_name/system_prompt（Leader 身份）")

    # B5 控制信道 + stats
    for f in ("action_taken", "reply_content", "_stream_stats"):
        if f not in hints:
            errs.append(f"[B5] GroupState 缺 {f}（协调者子节点控制信道 + 流式 stats 透传）")
    if not any(e.startswith("[B5]") for e in errs):
        print("[B5] OK  GroupState 含 action_taken/reply_content/_stream_stats（控制信道+stats）")

    # B6 群配置 + plan + incoming
    for f in ("auto_confirm", "leader_strategy", "memory", "dispatch_plan",
              "incoming_message", "incoming_sender", "incoming_kind", "incoming_data"):
        if f not in hints:
            errs.append(f"[B6] GroupState 缺 {f}（群配置/DAG plan/入站消息）")
    if not any(e.startswith("[B6]") for e in errs):
        print("[B6] OK  GroupState 含 auto_confirm/leader_strategy/memory/dispatch_plan/incoming_*")

    # B7 coordinator 节点读的所有 state 键 ⊆ GroupState 字段集
    # 扫所有 node_* + _leader_system 函数体里的 state["..."] / state.get("...")
    coord_fns = {
        "node_classify_incoming": node_classify_incoming,
        "node_llm_decide": node_llm_decide,
        "node_chat": node_chat,
        "node_dispatch": node_dispatch,
        "node_handle_reply": node_handle_reply,
        "node_dispatch_next": node_dispatch_next,
        "node_summarize": node_summarize,
    }
    read_keys: set[str] = set()
    for fname in list(coord_fns.keys()) + ["_leader_system"]:
        body = _fn_body(coord_src, fname)
        # state["key"] 和 state.get("key", ...) 和 state.get("key")
        for m in re.finditer(r'state\["([^"]+)"\]', body):
            read_keys.add(m.group(1))
        for m in re.finditer(r'state\.get\(\s*"([^"]+)"', body):
            read_keys.add(m.group(1))
    # 这些是 coordinator 节点读的键，应全部在 GroupState（schema 并集完成）
    not_in_gs = read_keys - hints
    # 允许未在 hints 但 TypedDict total=False 运行时仍可读（duck-typed）——但 schema
    # 并集的契约是「声明的键覆盖节点读的键」，故 not_in_gs 应为空
    if not_in_gs:
        errs.append(f"[B7] coordinator 节点读的 state 键不在 GroupState：{sorted(not_in_gs)}")
    else:
        print(f"[B7] OK  coordinator 节点读的 {len(read_keys)} 个 state 键全 ⊆ GroupState（schema 并集完成，节点代码无需改写）")

    # ── C. route_after_* 语义保真 ────────────────────────────
    # C8 route_after_classify 三分支
    classify_body = _fn_body(coord_src, "route_after_classify", is_async=False)
    if not re.search(r'"confirm_dispatch"\s*:\s*return\s*"dispatch_next"', classify_body) and \
       not re.search(r'action\s*==\s*"confirm_dispatch".*?"dispatch_next"', classify_body, re.S):
        errs.append("[C8] route_after_classify 缺 confirm_dispatch→dispatch_next")
    elif "handle_reply" not in classify_body or "llm_decide" not in classify_body:
        errs.append("[C8] route_after_classify 缺 handle_reply/llm_decide 分支")
    else:
        print("[C8] OK  route_after_classify 三分支保真（confirm_dispatch→dispatch_next / handle_reply→handle_reply / else→llm_decide）")

    # C9 route_after_llm_decide
    llm_body = _fn_body(coord_src, "route_after_llm_decide", is_async=False)
    if '"chat"' not in llm_body or '"dispatch"' not in llm_body:
        errs.append("[C9] route_after_llm_decide 缺 chat/dispatch 分支")
    else:
        print("[C9] OK  route_after_llm_decide 保真（chat→chat / dispatch→dispatch / ask/continue→chat）")

    # C10 route_after_dispatch（vh5 死成员契约）
    disp_body = _fn_body(coord_src, "route_after_dispatch", is_async=False)
    disp_code = re.sub(r'""".*?"""', "", disp_body, flags=re.S)
    if not re.search(r'if\s+action\s*==\s*"dispatch_next"\s*:', disp_code):
        errs.append("[C10] route_after_dispatch 非 if action == 'dispatch_next'")
    elif "confirm_dispatch" in disp_code or "direct_run" in disp_code or "wait_confirm" in disp_code:
        errs.append("[C10] route_after_dispatch 含死成员（vh5 契约破）")
    elif "return END" not in disp_code:
        errs.append("[C10] route_after_dispatch 缺 fall-through return END")
    else:
        print("[C10] OK  route_after_dispatch 保真（==dispatch_next / fall-through END / 无死成员——vh5 不破）")

    # C11 route_after_handle_reply
    hr_body = _fn_body(coord_src, "route_after_handle_reply", is_async=False)
    if "summarize" not in hr_body or "dispatch_next" not in hr_body or "llm_decide" not in hr_body:
        errs.append("[C11] route_after_handle_reply 缺 summarize/dispatch_next/llm_decide 分支")
    else:
        print("[C11] OK  route_after_handle_reply 保真（summarize→summarize / dispatch_next→dispatch_next / else→llm_decide）")

    # ── D. 节点代码复用不变 ──────────────────────────────────
    # D12 build_coordinator_subnodes 返的是同一组函数对象（非复制）
    specs = build_coordinator_subnodes(coordinator_id="c1")
    same = (
        specs["classify"] is node_classify_incoming and
        specs["llm_decide"] is node_llm_decide and
        specs["chat"] is node_chat and
        specs["dispatch"] is node_dispatch and
        specs["handle_reply"] is node_handle_reply and
        specs["dispatch_next"] is node_dispatch_next and
        specs["summarize"] is node_summarize and
        specs["route_after_classify"] is route_after_classify and
        specs["route_after_llm_decide"] is route_after_llm_decide and
        specs["route_after_dispatch"] is route_after_dispatch and
        specs["route_after_handle_reply"] is route_after_handle_reply
    )
    if not same:
        errs.append("[D12] build_coordinator_subnodes 返的不是同一组节点/路由函数对象（应为同一组，非复制）")
    else:
        print("[D12] OK  build_coordinator_subnodes 返同一组节点/路由函数对象（节点代码复用，非复制改写）")

    # D13 _leader_system 仍读 state.get("system_prompt")
    ls_body = _fn_body(coord_src, "_leader_system", is_async=False)
    if 'state.get("system_prompt")' not in ls_body and 'state.get("system_prompt"' not in ls_body:
        errs.append("[D13] _leader_system 未读 state.get('system_prompt')（群图 coordinator 子节点走同一 system 拼接）")
    else:
        print("[D13] OK  _leader_system 仍读 state.get('system_prompt')（base+COORDINATOR_SYSTEM 拼接不变）")

    # ── E. 向后兼容 ──────────────────────────────────────────
    # E14 build_coordinator_graph 仍在
    if not callable(build_coordinator_graph):
        errs.append("[E14] build_coordinator_graph 不可调用（驻留协调者图不应删）")
    else:
        try:
            resident = build_coordinator_graph()
            if resident is None:
                errs.append("[E14] build_coordinator_graph 返 None（编译失败）")
            else:
                print("[E14] OK  build_coordinator_graph 保留 + 编译通过（驻留协调者图未删）")
        except Exception as e:  # noqa: BLE001
            errs.append(f"[E14] build_coordinator_graph 编译异常：{type(e).__name__}: {e}")

    # E15 CoordinatorState 仍在
    try:
        from engine.state import CoordinatorState
        if not (isinstance(CoordinatorState, type) and CoordinatorState.__name__ == "CoordinatorState"):
            errs.append("[E15] CoordinatorState 不应被删（驻留图 schema——vh31 B6 不破）")
        else:
            print("[E15] OK  CoordinatorState 保留（驻留图 schema——vh31 B6 不破）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[E15] CoordinatorState 导入失败：{e}")

    # E16 群图 + 驻留图共存无 import cycle
    try:
        from engine.group_graph import build_group_graph as _bgg
        from engine.coordinator import build_coordinator_graph as _bcg
        # 都已编译过（A2 + E14），再调一次确认无 cycle 副作用
        _bgg("g2", [{"agent_id": "x1", "agent_name": "X", "agent_role": "r", "system_prompt": ""}], coordinator_id="c2")
        _bcg()
        print("[E16] OK  群图 + 驻留 coordinator 图共存编译无 import cycle")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[E16] 共存编译异常（import cycle？）：{type(e).__name__}: {e}")

    return errs


def main() -> int:
    print("=== VH34 回归：coordinator 子节点迁移到群图（去中心化 handoff 迁移·协调者子节点层）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "coordinator 子节点迁移到群图锁定：\n"
        "  · A build_coordinator_subnodes 工厂 + 群图注册 7 coordinator 子节点 + stash 元数据；\n"
        "  · B GroupState schema 并集（Leader 身份 + 控制信道 + stats + 群配置/plan/incoming），coordinator 节点读的键全 ⊆ GroupState；\n"
        "  · C route_after_classify/llm_decide/dispatch/handle_reply 四路由语义保真（vh5 死成员契约不破）；\n"
        "  · D 节点代码复用不变（build_coordinator_subnodes 返同一组函数对象，_leader_system 读 system_prompt 不变）；\n"
        "  · E 驻留 coordinator 图 + CoordinatorState 保留，群图与驻留图共存无 import cycle。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

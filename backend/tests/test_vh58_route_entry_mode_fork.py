"""VH58 回归：route_entry 按 collaboration_mode 分流 + @群主 handoff 条件化.

锁住「群组协作模式」改造的 route_entry 两版（standalone + closure-bound）按
``collaboration_mode`` 分流 + ``_resolve_handoff_target`` @群主 条件化跳过：

  · **去中心化模式**：裸消息（无 @）→ 群主当首发（对标 LangGraph ``create_swarm``
    的 ``default_active_agent`` 兜底，goto=``agent_<coordinator_id>``）；@群主 合法
    handoff（coordinator 是普通 member 节点，非死胡同）。
  · **中心化模式**：维持现状——裸 coordinator_reply → classify（Leader 主导）；
    @群主 死胡同（coordinator 不是 handoff target，维持现状非本次修复）。

route_entry 两版严格同步（vh40 锁延续），改一处改两处。

五段契约（纯静态 + 函数直调 stub，不依赖 live server / 真实 LLM）：

  A. API 静态锁——route_entry 两版读 state["collaboration_mode"]
    1. standalone route_entry 函数体含 ``state.get("collaboration_mode"``.
    2. build_route_entry closure-bound 函数体含 ``state.get("collaboration_mode"``.
    3. _resolve_handoff_target 签名含 collaboration_mode 参数（默认 "centralized"）.

  B. 去中心化裸消息群主首发锁——decentralized + 无 @ → goto agent_<coordinator_id>
    4. standalone route_entry decentralized + coordinator_reply kind + 无 @ → goto=agent_c1.
    5. closure-bound route_entry decentralized + coordinator_reply kind + 无 @ → goto=agent_c1.

  C. 中心化裸消息 classify 锁——centralized + coordinator_reply + 无 @ → classify（不回归）
    6. standalone route_entry centralized + coordinator_reply + 无 @ → goto=classify.
    7. closure-bound route_entry centralized + coordinator_reply + 无 @ → goto=classify.

  D. @群主 handoff 条件化锁——_resolve_handoff_target
    8. centralized 模式 @群主 → None（死胡同维持现状，coord-skip）.
    9. decentralized 模式 @群主 → coordinator_id（合法 handoff，修死胡同）.
   10. centralized 模式 @普通成员 → 成员 id（现状不破）.
   11. decentralized 模式 @普通成员 → 成员 id（现状不破）.

  D2. centralized 用户消息 @群主 route_entry 层接管锁——task #60 修死胡同
   12. centralized + 用户消息（coordinator_reply kind）@群主 → goto=classify（route_entry
       层接管，不再死胡同 END）。
   13. centralized + worker 回复（agent_reply kind）@群主 → goto=END（worker 层死胡同维持，
       route_entry 不二次接管，话筒落地等用户下一条消息触发）。

  E. route_entry 两版同步锁——vh40 锁延续
   14. 两版 route_entry 都含 collaboration_mode 分流逻辑（decentralized 裸消息群主首发）.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
GROUP_GRAPH_PY = BACKEND / "engine" / "group_graph.py"
WORKER_PY = BACKEND / "engine" / "worker.py"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _fn_body(src: str, fn_name: str) -> str:
    """Extract a function body (first line after `:` to next top-level def)."""
    m = re.search(rf"^(?:async )?def {fn_name}\([^)]*\)[^:]*:\n", src, re.M)
    if not m:
        return ""
    start = m.end()
    # find next top-level def at indent 0
    rest = src[start:]
    lines = rest.split("\n")
    out: list[str] = []
    for ln in lines:
        if ln.startswith("def ") or ln.startswith("async def "):
            break
        out.append(ln)
    return "\n".join(out)


def assert_contract() -> list[str]:
    errs: list[str] = []
    src_graph = GROUP_GRAPH_PY.read_text(encoding="utf-8")
    src_worker = WORKER_PY.read_text(encoding="utf-8")

    try:
        from engine.group_graph import (  # type: ignore
            build_group_graph,
            build_route_entry,
            route_entry,
        )
        from engine.worker import _resolve_handoff_target  # type: ignore
    except Exception as e:  # noqa: BLE001
        return [f"[import] 导入失败：{type(e).__name__}: {e}"]

    members = [
        {"agent_id": "w1", "agent_name": "前端", "agent_role": "fe", "system_prompt": ""},
        {"agent_id": "w2", "agent_name": "后端", "agent_role": "be", "system_prompt": ""},
        # decentralized 模式 _resolve_members 纳入 coordinator，build_group_graph
        # 据此注册 agent_<coordinator_id> 节点 + handoff tool。为让 closure-bound
        # 版的 handoff_targets 含 agent_c1（群主首发判定需要），members 须含 c1。
        {"agent_id": "c1", "agent_name": "协调者", "agent_role": "coordinator", "system_prompt": ""},
    ]

    # ── A. API 静态 ───────────────────────────────────────────
    # A1 standalone route_entry 读 state["collaboration_mode"]
    body_standalone = _fn_body(src_graph, "route_entry")
    if "collaboration_mode" not in body_standalone:
        errs.append("[A1] standalone route_entry 未读 state['collaboration_mode']")
    else:
        print("[A1] OK  standalone route_entry 读 state['collaboration_mode']")

    # A2 closure-bound build_route_entry 内部 _route_entry 读 state["collaboration_mode"]
    # build_route_entry is a factory returning _route_entry; check the closure body
    # is present in the source (the function is defined inside build_route_entry).
    # Extract the _route_entry closure body via regex.
    closure_match = re.search(
        r"async def _route_entry\(state: GroupState\) -> Command:\n((?:    .*\n|\n)*)",
        src_graph, re.M,
    )
    if not closure_match:
        errs.append("[A2] build_route_entry 内部 _route_entry 闭包未找到")
    elif "collaboration_mode" not in closure_match.group(1):
        errs.append("[A2] closure-bound _route_entry 未读 state['collaboration_mode']")
    else:
        print("[A2] OK  closure-bound _route_entry 读 state['collaboration_mode']")

    # A3 _resolve_handoff_target 签名含 collaboration_mode
    try:
        import inspect
        sig = inspect.signature(_resolve_handoff_target)
        if "collaboration_mode" not in sig.parameters:
            errs.append(f"[A3] _resolve_handoff_target 签名缺 collaboration_mode 参数：{sig}")
        else:
            default = sig.parameters["collaboration_mode"].default
            if default != "centralized":
                errs.append(f"[A3] collaboration_mode 默认值应 'centralized'，实际 {default!r}")
            else:
                print(f"[A3] OK  _resolve_handoff_target 签名含 collaboration_mode='centralized' 默认")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[A3] _resolve_handoff_target 签名检查异常：{type(e).__name__}: {e}")

    # ── helper: build compiled graph + run route_entry standalone ──
    class _M:
        def __init__(self, aid): self.agent_id = aid; self.agent_name = aid; self.agent_role = "r"

    db_members = [_M("w1"), _M("w2")]

    async def _run_standalone(kind, message, mode):
        with patch("engine.worker.crud") as crud_mock, \
             patch("engine.worker.find_mentions", return_value=[]), \
             patch("engine.worker.resolve_mention", return_value=None):
            crud_mock.list_group_members_with_agent = AsyncMock(return_value=db_members)
            crud_mock.list_agents = AsyncMock(return_value=[])
            return await route_entry({
                "group_id": "g1", "coordinator_id": "c1",
                "incoming_message": message, "incoming_sender": "user",
                "incoming_kind": kind, "turn_count": 0,
                "collaboration_mode": mode,
            })

    async def _run_closure(kind, message, mode):
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
                "collaboration_mode": mode,
            })

    # ── B. 去中心化裸消息群主首发 ─────────────────────────────
    # B4 standalone decentralized + coordinator_reply + 无 @ → goto agent_c1
    try:
        cmd = asyncio.run(_run_standalone("coordinator_reply", "重构登录模块", "decentralized"))
        if cmd.goto != "agent_c1":
            errs.append(f"[B4] standalone decentralized + coordinator_reply 无 @ 应 goto=agent_c1（群主首发），实际 {cmd.goto!r}")
        else:
            print(f"[B4] OK  standalone decentralized + coordinator_reply 无 @ → goto={cmd.goto!r}（群主当首发，swarm default_active_agent）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B4] standalone decentralized 裸消息直调异常：{type(e).__name__}: {e}")

    # B5 closure-bound decentralized + coordinator_reply + 无 @ → goto agent_c1
    try:
        cmd = asyncio.run(_run_closure("coordinator_reply", "重构登录模块", "decentralized"))
        if cmd.goto != "agent_c1":
            errs.append(f"[B5] closure-bound decentralized + coordinator_reply 无 @ 应 goto=agent_c1（群主首发），实际 {cmd.goto!r}")
        else:
            print(f"[B5] OK  closure-bound decentralized + coordinator_reply 无 @ → goto={cmd.goto!r}（群主当首发，swarm default_active_agent）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B5] closure-bound decentralized 裸消息直调异常：{type(e).__name__}: {e}")

    # ── C. 中心化裸消息 classify（不回归）────────────────────
    # C6 standalone centralized + coordinator_reply + 无 @ → classify
    try:
        cmd = asyncio.run(_run_standalone("coordinator_reply", "重构登录模块", "centralized"))
        if cmd.goto != "classify":
            errs.append(f"[C6] standalone centralized + coordinator_reply 无 @ 应 goto=classify，实际 {cmd.goto!r}")
        else:
            print(f"[C6] OK  standalone centralized + coordinator_reply 无 @ → goto={cmd.goto!r}（Leader 主导，不回归）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C6] standalone centralized 裸消息直调异常：{type(e).__name__}: {e}")

    # C7 closure-bound centralized + coordinator_reply + 无 @ → classify
    try:
        cmd = asyncio.run(_run_closure("coordinator_reply", "重构登录模块", "centralized"))
        if cmd.goto != "classify":
            errs.append(f"[C7] closure-bound centralized + coordinator_reply 无 @ 应 goto=classify，实际 {cmd.goto!r}")
        else:
            print(f"[C7] OK  closure-bound centralized + coordinator_reply 无 @ → goto={cmd.goto!r}（Leader 主导，不回归）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C7] closure-bound centralized 裸消息直调异常：{type(e).__name__}: {e}")

    # ── D. @群主 handoff 条件化 ──────────────────────────────
    class _Member:
        def __init__(self, aid): self.agent_id = aid
    class _Agent:
        def __init__(self, aid, name, role):
            self.id = aid; self.name = name; self.role = role

    db_m = [_Member("w1"), _Member("w2")]
    db_a = [_Agent("w1", "前端", "fe"), _Agent("w2", "后端", "be"), _Agent("c1", "协调者", "coordinator")]

    async def _resolve(content, sender="w1", mode="centralized"):
        with patch("engine.worker.crud") as crud_mock:
            crud_mock.list_group_members_with_agent = AsyncMock(return_value=db_m)
            crud_mock.list_agents = AsyncMock(return_value=db_a)
            return await _resolve_handoff_target("g1", "c1", sender, content, mode)

    # D8 centralized @群主 → None（coord-skip 维持现状）
    try:
        r = asyncio.run(_resolve("@协调者 你来", mode="centralized"))
        if r is not None:
            errs.append(f"[D8] centralized @群主 应返 None（死胡同维持现状），实际 {r!r}")
        else:
            print(f"[D8] OK  centralized @群主 → None（coord-skip 维持现状，非本次修复）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D8] centralized @群主 检查异常：{type(e).__name__}: {e}")

    # D9 decentralized @群主 → coordinator_id（合法 handoff，修死胡同）
    try:
        r = asyncio.run(_resolve("@协调者 你来", mode="decentralized"))
        if r != "c1":
            errs.append(f"[D9] decentralized @群主 应返 'c1'（合法 handoff 修死胡同），实际 {r!r}")
        else:
            print(f"[D9] OK  decentralized @群主 → {r!r}（合法 handoff，修死胡同）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D9] decentralized @群主 检查异常：{type(e).__name__}: {e}")

    # D10 centralized @普通成员 → 成员 id（现状不破）
    try:
        r = asyncio.run(_resolve("@后端 来一下", mode="centralized"))
        if r != "w2":
            errs.append(f"[D10] centralized @后端 应返 'w2'（现状不破），实际 {r!r}")
        else:
            print(f"[D10] OK  centralized @后端 → {r!r}（现状不破）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D10] centralized @普通成员 检查异常：{type(e).__name__}: {e}")

    # D11 decentralized @普通成员 → 成员 id（现状不破）
    try:
        r = asyncio.run(_resolve("@后端 来一下", mode="decentralized"))
        if r != "w2":
            errs.append(f"[D11] decentralized @后端 应返 'w2'（现状不破），实际 {r!r}")
        else:
            print(f"[D11] OK  decentralized @后端 → {r!r}（现状不破）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D11] decentralized @普通成员 检查异常：{type(e).__name__}: {e}")

    # ── D2. centralized 用户消息 @群主 route_entry 层接管（task #60 修死胡同）──
    # D12 centralized + 用户消息（coordinator_reply kind）@群主 → goto=classify
    # （route_entry 层接管，不再死胡同 END）。_resolve_handoff_target 仍返 None（coord-skip），
    # 但 _message_mentions_coordinator 检测到用户消息 @群主 → route_entry goto classify。
    async def _run_user_at_coord(kind, message, mode):
        # standalone route_entry 直调（_message_mentions_coordinator 在 group_graph 模块内）
        with patch("engine.worker.crud") as crud_mock, \
             patch("engine.worker.find_mentions", return_value=[]), \
             patch("engine.worker.resolve_mention", return_value=None), \
             patch("engine.group_graph.crud") as gcrud_mock, \
             patch("engine.group_graph.find_mentions", return_value=["协调者"]), \
             patch("engine.group_graph.resolve_mention", return_value=None):
            crud_mock.list_group_members_with_agent = AsyncMock(return_value=db_members)
            crud_mock.list_agents = AsyncMock(return_value=[])
            gcrud_mock.list_group_members_with_agent = AsyncMock(return_value=db_members)
            gcrud_mock.list_agents = AsyncMock(return_value=db_a)
            return await route_entry({
                "group_id": "g1", "coordinator_id": "c1",
                "incoming_message": message, "incoming_sender": "user",
                "incoming_kind": kind, "turn_count": 0,
                "collaboration_mode": mode,
            })

    try:
        cmd = asyncio.run(_run_user_at_coord("coordinator_reply", "@协调者 帮我看下", "centralized"))
        if cmd.goto != "classify":
            errs.append(f"[D12] centralized + 用户消息 @群主 应 goto=classify（修死胡同），实际 {cmd.goto!r}")
        else:
            print(f"[D12] OK  centralized + 用户消息 @群主 → goto={cmd.goto!r}（route_entry 接管修死胡同，task #60）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D12] centralized 用户消息 @群主 直调异常：{type(e).__name__}: {e}")

    # D13 centralized + worker 回复（agent_reply kind）@群主 → goto=END
    # （worker 层死胡同维持，route_entry 不二次接管——话筒落地等用户下一条消息触发）。
    try:
        cmd = asyncio.run(_run_user_at_coord("agent_reply", "@协调者 你来", "centralized"))
        if cmd.goto != "__end__":
            errs.append(f"[D13] centralized + worker 回复(agent_reply) @群主 应 goto=END（死胡同维持），实际 {cmd.goto!r}")
        else:
            print(f"[D13] OK  centralized + worker 回复 @群主 → goto={cmd.goto!r}（死胡同维持，worker 层不接管）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D13] centralized worker @群主 直调异常：{type(e).__name__}: {e}")

    # E12 两版都含 decentralized 分流分支（群主当首发）
    try:
        has_standalone_decentralized = "collaboration_mode == \"decentralized\"" in body_standalone
        has_closure_decentralized = (
            closure_match is not None
            and "collaboration_mode == \"decentralized\"" in closure_match.group(1)
        )
        if not has_standalone_decentralized:
            errs.append("[E12] standalone route_entry 缺 decentralized 分流分支")
        elif not has_closure_decentralized:
            errs.append("[E12] closure-bound route_entry 缺 decentralized 分流分支")
        else:
            print("[E12] OK  两版 route_entry 都含 decentralized 分流分支（群主当首发，vh40 同步锁延续）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[E12] 两版同步检查异常：{type(e).__name__}: {e}")

    return errs


def main() -> int:
    print("=== VH58 回归：route_entry 按 collaboration_mode 分流 + @群主 handoff 条件化 ===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "route_entry 按 collaboration_mode 分流锁定：\n"
        "  · A 两版 route_entry 读 state['collaboration_mode'] + _resolve_handoff_target 签名含 mode 参数；\n"
        "  · B 去中心化裸消息 → 群主当首发（goto=agent_<coordinator_id>，swarm default_active_agent，两版同步）；\n"
        "  · C 中心化裸消息 → classify（Leader 主导，不回归，两版同步）；\n"
        "  · D @群主 centralized→None（死胡同维持）/ decentralized→coordinator_id（修死胡同）；\n"
        "  · D @普通成员 两模式均正常 handoff（现状不破）；\n"
        "  · D2 centralized 用户消息 @群主 → classify（route_entry 层接管修死胡同，task #60）；\n"
        "  · D2 centralized worker 回复 @群主 → END（死胡同维持，worker 层不接管）；\n"
        "  · E 两版同步锁（vh40 延续）——都含 decentralized 分流分支（群主当首发）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

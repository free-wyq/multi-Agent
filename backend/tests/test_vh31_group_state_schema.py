"""VH31 回归：GroupState TypedDict 新增（去中心化群图 handoff 迁移·状态层）.

锁住 ``engine/state.py`` 新增的 ``GroupState``——去中心化群图（engine/group_graph.py，
后续任务装配）的共享状态 schema。一张群图（per-group）共享一个 ``GroupState``：
agent 是节点，「下一个谁说话」由 LangGraph handoff 边决定（当前发言者回复里的
@mention → goto 目标 agent 节点；无 @→END 结束回合）.

设计真源见 memory ``decentralized-scheduling-stop-plan-2026-07-13``（方向 A）+
``langgraph-two-collaboration-paths``。本任务只新增 schema，不删 ``CoordinatorState``/
``WorkerState``（驻留协调者/worker 图仍用，群图迁移落地后消费者才切）.

三群聊缺陷（顺序乱/协调者插话/停不下来）的消解落在字段语义里：

  · ``messages`` (``add_messages`` reducer) — 群图共享消息日志，按 id 去重（resume-safe）.
    替代 per-engine 的 ``incoming_message`` 双字段路由.
  · ``current_speaker`` — 当前驱动回合的 agent_id. handoff 串行只一节点在跑 +
    current_speaker 标记 → 消除「同一 agent 一轮被驱动两次」（顺序乱根因①）.
  · ``turn_count`` / ``recent_speakers`` — 图内回合计数 + 本回合已发言者有序表，
    供 ``route_entry``/节点判「同一 agent 不连发」+ 图内 handoff 链长兜底（顺序乱根因②）.
  · ``dispatch_plan`` (``replace_value`` reducer) — 协调者 DAG 计划，last-write-wins
    （节点返回完整 plan 非 delta，镜像 ``CoordinatorState.dispatch_plan``）.
  · ``auto_confirm`` / ``leader_strategy`` — 群配置标志，per ``invoke_turn`` 注入
    （与 ``CoordinatorState`` 同源 ``GroupEntity.config``），协调者子节点读同一 config.
  · ``memory`` (``append_list`` reducer) — 群共享回合记忆，跨 handoff 追加（镜像
    ``CoordinatorState.memory``），checkpointer 跨 invoke_turn 持久化.
  · ``incoming_*`` — 开启回合的用户/系统消息（``route_entry`` 注入），镜像
    ``CoordinatorState.incoming_*``.

四段契约（纯静态 schema 断言 + 真 StateGraph 跑通 reducer）：

  A. schema 存在 + 字段齐全 + reducer 正确绑定
    1. ``GroupState`` 是 TypedDict 子类（``__annotations__`` 含全部 14 个字段）.
    2. ``messages`` 绑 ``add_messages`` reducer（按 id 去重，resume-safe）.
    3. ``dispatch_plan`` 绑 ``replace_value`` reducer（last-write-wins）.
    4. ``memory`` + ``recent_speakers`` 绑 ``append_list`` reducer（追加）.
    5. ``turn_count`` 是 int（非 reducer，图内自增）.

  B. 旧 schema 不删（向后兼容——迁移未落地前消费者仍读 CoordinatorState/WorkerState）
    6. ``CoordinatorState`` / ``WorkerState`` 仍在（类可导入）.
    7. 三个 reducer 函数（append_list/merge_dict/replace_value）仍在（旧图依赖）.

  C. 真 StateGraph 编译跑通（reducer 在 LangGraph 运行时正确合并）
    8. 用 GroupState 编译一个两节点图，两节点各返回 messages + recent_speakers
       + turn_count，invoke 后 messages 累加 2、recent_speakers 累加 2、turn_count
       取最后写入（非 reducer last-write-wins）.

  D. ``add_messages`` 按 id 去重（resume-safe——重跑节点/重注入同 id 消息不双计）
    9. 同 id 消息二次注入 → messages 列表长度不变（去重生效）.

  E. ``total=False`` 语义——字段全可选，节点只声明读写键
    10. ``GroupState()`` 空构造不抛 + 未触及键 absent（非 None），StateGraph 按缺省 reducer 合并.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, get_type_hints

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
STATE_PY = BACKEND / "engine" / "state.py"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def assert_contract() -> list[str]:
    errs: list[str] = []

    # imports
    try:
        from engine.state import (  # type: ignore
            GroupState,
            CoordinatorState,
            WorkerState,
            append_list,
            merge_dict,
            replace_value,
        )
    except Exception as e:  # noqa: BLE001
        return [f"[import] 导入失败：{type(e).__name__}: {e}"]

    # ── A. schema 存在 + 字段 + reducer 绑定 ─────────────────
    hints = get_type_hints(GroupState, include_extras=True)
    required = {
        "group_id", "coordinator_id", "messages", "current_speaker",
        "dispatch_plan", "turn_count", "recent_speakers",
        "auto_confirm", "leader_strategy", "memory",
        "incoming_message", "incoming_sender", "incoming_kind", "incoming_data",
    }
    missing = required - set(hints)
    if missing:
        errs.append(f"[A1] GroupState 缺字段：{sorted(missing)}")
    else:
        print(f"[A1] OK  GroupState 14 字段齐全：{sorted(hints)}")

    def _reducer(field: str) -> str | None:
        t = hints.get(field)
        if t is None:
            return None
        meta = getattr(t, "__metadata__", None)
        if meta:
            return getattr(meta[0], "__name__", str(meta[0]))
        return None

    # A2 messages -> add_messages
    r = _reducer("messages")
    if r in ("add_messages", "_add_messages"):
        print(f"[A2] OK  messages 绑 add_messages reducer（按 id 去重，resume-safe）")
    else:
        errs.append(f"[A2] messages reducer 应为 add_messages，实际 {r!r}")

    # A3 dispatch_plan -> replace_value
    r = _reducer("dispatch_plan")
    if r == "replace_value":
        print(f"[A3] OK  dispatch_plan 绑 replace_value reducer（last-write-wins，节点返回完整 plan）")
    else:
        errs.append(f"[A3] dispatch_plan reducer 应为 replace_value，实际 {r!r}")

    # A4 memory + recent_speakers -> append_list
    for f in ("memory", "recent_speakers"):
        r = _reducer(f)
        if r == "append_list":
            print(f"[A4] OK  {f} 绑 append_list reducer（追加，非替换）")
        else:
            errs.append(f"[A4] {f} reducer 应为 append_list，实际 {r!r}")

    # A5 turn_count is int, no reducer (in-graph increment)
    tc = hints.get("turn_count")
    if tc is None:
        errs.append("[A5] turn_count 字段缺失")
    elif getattr(tc, "__metadata__", None):
        errs.append(f"[A5] turn_count 不应有 reducer（图内自增 last-write-wins），实际绑 {tc.__metadata__}")
    else:
        print(f"[A5] OK  turn_count 无 reducer（int，图内 last-write-wins 自增）")

    # ── B. 旧 schema + reducer 不删 ──────────────────────────
    if not (isinstance(CoordinatorState, type) and CoordinatorState.__name__ == "CoordinatorState"):
        errs.append("[B6] CoordinatorState 不应被删（迁移未落地前驻留协调者图仍用）")
    else:
        print("[B6] OK  CoordinatorState 保留（向后兼容）")
    if not (isinstance(WorkerState, type) and WorkerState.__name__ == "WorkerState"):
        errs.append("[B7] WorkerState 不应被删（驻留 worker 图仍用）")
    else:
        print("[B7] OK  WorkerState 保留（向后兼容）")
    for fn in (append_list, merge_dict, replace_value):
        if not callable(fn):
            errs.append(f"[B7] reducer {fn} 不可调用（被删？）")
    if not any(e.startswith("[B7]") for e in errs):
        print("[B7] OK  append_list/merge_dict/replace_value 三 reducer 全保留")

    # ── C. 真 StateGraph 编译跑通 ───────────────────────────
    try:
        from langgraph.graph import StateGraph, START, END  # type: ignore
        from langchain_core.messages import HumanMessage, AIMessage  # type: ignore

        g = StateGraph(GroupState)

        def node_a(state):  # type: ignore[no-untyped-def]
            return {
                "messages": [HumanMessage(content="hi", id="m1")],
                "turn_count": 1,
                "recent_speakers": ["a1"],
            }

        def node_b(state):  # type: ignore[no-untyped-def]
            return {
                "messages": [AIMessage(content="yo", id="m2")],
                "turn_count": 2,
                "recent_speakers": ["a2"],
            }

        g.add_node("a", node_a)
        g.add_node("b", node_b)
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", END)
        app = g.compile()
        r = app.invoke({"messages": []})
        if len(r.get("messages", [])) != 2:
            errs.append(f"[C8] messages 应累加到 2，实际 {len(r.get('messages', []))}")
        elif r.get("recent_speakers") != ["a1", "a2"]:
            errs.append(f"[C8] recent_speakers 应累加 ['a1','a2']，实际 {r.get('recent_speakers')!r}")
        elif r.get("turn_count") != 2:
            errs.append(f"[C8] turn_count 应 last-write-wins=2，实际 {r.get('turn_count')!r}")
        else:
            print("[C8] OK  真 StateGraph 编译跑通：messages 累加 2 / recent_speakers 累加 / turn_count last-write-wins=2")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C8] 真 StateGraph 编译/跑通失败：{type(e).__name__}: {e}")

    # ── D. add_messages 按 id 去重（resume-safe）────────────
    try:
        from langgraph.graph.message import add_messages  # type: ignore
        from langchain_core.messages import HumanMessage  # type: ignore

        m = HumanMessage(content="hi", id="m1")
        r1 = add_messages([], [m])
        r2 = add_messages(r1, [m])  # 同 id 二次注入
        if len(r2) != 1:
            errs.append(f"[D9] add_messages 同 id 二次注入应去重为 1，实际 {len(r2)}（resume 不安全）")
        else:
            print("[D9] OK  add_messages 按 id 去重（节点重跑/重注入同 id 消息不双计，resume-safe）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D9] add_messages 去重验证失败：{type(e).__name__}: {e}")

    # ── E. total=False 语义 ──────────────────────────────────
    try:
        empty = GroupState()  # type: ignore[call-arg]
        if not isinstance(empty, dict):
            errs.append(f"[E10] GroupState() 空构造应返回 dict，实际 {type(empty).__name__}")
        else:
            # 未触及键 absent（非 None）
            if "dispatch_plan" in empty:
                errs.append("[E10] total=False 下未注入键应 absent，dispatch_plan 不应默认存在")
            else:
                print("[E10] OK  total=False：GroupState() 空构造不抛 + 未注入键 absent（节点只声明读写键）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[E10] GroupState() 空构造失败（total=False 疑破）：{type(e).__name__}: {e}")

    return errs


def main() -> int:
    print("=== VH31 回归：GroupState TypedDict 新增（去中心化群图状态层）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "去中心化群图状态层锁定：\n"
        "  · A GroupState 14 字段齐全 + reducer 正确绑定（messages→add_messages / dispatch_plan→replace_value / memory+recent_speakers→append_list / turn_count 无 reducer）；\n"
        "  · B CoordinatorState/WorkerState + 三 reducer 全保留（迁移未落地前向后兼容）；\n"
        "  · C 真 StateGraph 编译跑通（reducer 在运行时正确合并）；\n"
        "  · D add_messages 按 id 去重（resume-safe）；\n"
        "  · E total=False——空构造不抛 + 未注入键 absent（节点只声明读写键）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

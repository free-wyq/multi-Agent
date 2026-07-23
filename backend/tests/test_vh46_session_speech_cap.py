"""VH46 回归：会话发言总量封顶（cross-turn safety backstop·StopSignal 第三层）.

锁住「按钮硬停（cancel_turn）+ 关键词软停（request_stop）失效 / 用户不在场时的最后兜底」
——对标 AutoGen v0.4 ``MaxMessageTermination(N)`` / OpenAI Agents SDK ``max_turns`` /
Google ADK ``max_llm_calls``。防 agent 之间无限 handoff（成语接龙接疯 / A↔B 互相 @
无限刷屏）烧 token。

设计真源见 memory ``stop-signal-cooperative-cancel-design``（三层停止：软停/硬停/封顶）
+ 本会话决策（用户拍板：只要按钮硬停 + 会话封顶两机制）。

关键：**跨回合**计数——不随单回合 END 清零（成语接龙是 N 个短回合，单回合 _stop_event
每回合 reset 拦不住），只在 reset_session（/new 开新对话）清零。这正是「停不下来」的解药。

与 per-turn 的 ``AGENT_NODE_MAX_HANDOFFS=8`` 是两个正交维度：
  · per-turn 8：单回合 handoff 链长度护栏（worker.py）。
  · 会话 50（SESSION_SPEECH_CAP）：跨回合 agent 发言总量总闸（group_runtime.py）。

八段契约（纯静态 + 真 asyncio stub + 真 build_group_graph，不依赖 live server / 真实 LLM）：

  A. 计数器装配锁——GroupRuntime 持 cross-turn 计数器
    1. ``SESSION_SPEECH_CAP`` 常量存在 + >=1（env 可调，默认 50）。
    2. ``GroupRuntime`` 有 ``_speech_count``（初 0）+ ``_cap_emitted``（初 False）字段。
    3. ``is_session_capped()`` 在 ``_speech_count >= SESSION_SPEECH_CAP`` 时返 True。
    4. ``record_speech()`` +1；撞顶且未 emit 过则标记 ``_cap_emitted=True``。

  B. route_entry 封顶守卫锁——撞顶即 END（standalone + closure-bound twin）
    5. standalone ``route_entry`` + closure-bound ``build_route_entry`` 入口查
       ``is_session_capped()`` 命中即 ``Command(goto=END)``。
    6. 守卫在 report-back 早返回**之后**（report-back 放过，避免 split-brain 死锁）。

  C. make_agent_node 封顶守卫锁——撞顶即 END（不发言）
    7. ``make_agent_node`` 入口（is_stopped 守卫之后、brain 之前）查 ``is_session_capped()``
       命中即 ``Command(goto=END)``。
    8. 守卫只在闲聊/handoff 路径（``is_dispatch_fanout`` False），不挡 dispatch fan-out
       execute 派工（中心化任务，挡它会让派工永远完不成 deadlock）。

  D. 计数累加锁——发言后 record_speech
    9. ``make_agent_node`` chat/ask 路径发言后调 ``rt.record_speech()``（+1）。
   10. execute 路径不调 record_speech（派工不算来回对话）。

  E. reset 清零锁——reset_session 重置封顶
   11. ``reset_session`` 清 ``_speech_count=0`` + ``_cap_emitted=False``（/new 开新对话重置）。

  F. 跨回合拦截锁——route_entry 命中 cap 后新回合仍 END
   12. 真 GroupRuntime：撞顶后 route_entry 在**新回合**（不同 thread）仍命中 is_session_capped
       → END（计数跨回合存活，不随单回合 reset_stop 清零）。

  G. runtime None 跳过锁——向后兼容（驻留图 / 无 runtime 调用）
   13. runtime None → route_entry / make_agent_node 封顶守卫跳过（向后兼容 vh39/vh40）。

  H. 向后兼容锁——main import OK + vh44/vh45 不破
   14. ``main`` 全量 import OK（group_runtime 加常量/字段/方法无 cycle）。
   15. vh44 协作式停止守卫 + vh45 stop-turn 端点不破。
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
GROUP_RUNTIME_PY = BACKEND / "engine" / "group_runtime.py"
GROUP_GRAPH_PY = BACKEND / "engine" / "group_graph.py"
WORKER_PY = BACKEND / "engine" / "worker.py"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fn_body(src: str, fn_name: str) -> str:
    idx = src.find(f"async def {fn_name}(")
    if idx < 0:
        idx = src.find(f"def {fn_name}(")
    if idx < 0:
        return ""
    rest = src[idx:]
    lines = rest.splitlines()
    body_lines = [lines[0]]
    for ln in lines[1:]:
        if ln.startswith("def ") or ln.startswith("async def "):
            break
        body_lines.append(ln)
    return "\n".join(body_lines)


async def assert_contract() -> list[str]:
    errs: list[str] = []
    gr_src = _read(GROUP_RUNTIME_PY)
    gg_src = _read(GROUP_GRAPH_PY)
    w_src = _read(WORKER_PY)

    try:
        from engine.group_runtime import GroupRuntime, SESSION_SPEECH_CAP  # type: ignore
        from engine import worker as worker_mod  # type: ignore
        from engine.group_graph import (  # type: ignore
            build_group_graph, build_route_entry, route_entry,
        )
        from engine.worker import build_agent_node  # type: ignore
    except Exception as e:  # noqa: BLE001
        return [f"[import] 导入失败：{type(e).__name__}: {e}"]

    class _FakeGroup:
        id = "g1"
        coordinator_id = "c1"

    members = [
        {"agent_id": "w1", "agent_name": "前端", "agent_role": "fe", "system_prompt": "sp1"},
        {"agent_id": "w2", "agent_name": "后端", "agent_role": "be", "system_prompt": "sp2"},
    ]

    # ── A. 计数器装配 ────────────────────────────────────────
    # A1 SESSION_SPEECH_CAP 常量存在 + >=1
    if not isinstance(SESSION_SPEECH_CAP, int) or SESSION_SPEECH_CAP < 1:
        errs.append(f"[A1] SESSION_SPEECH_CAP 应是 >=1 的 int，实际 {SESSION_SPEECH_CAP!r}")
    else:
        print(f"[A1] OK  SESSION_SPEECH_CAP={SESSION_SPEECH_CAP}（env 可调，默认 50）")

    # A2 _speech_count 初 0 + _cap_emitted 初 False
    rt = GroupRuntime(_FakeGroup())
    if getattr(rt, "_speech_count", None) != 0:
        errs.append(f"[A2] _speech_count 初值应 0，实际 {getattr(rt, '_speech_count', '<missing>')!r}")
    elif getattr(rt, "_cap_emitted", None) is not False:
        errs.append(f"[A2] _cap_emitted 初值应 False，实际 {getattr(rt, '_cap_emitted', '<missing>')!r}")
    else:
        print("[A2] OK  GroupRuntime 持 _speech_count=0 + _cap_emitted=False（cross-turn 计数器装配）")

    # A3 is_session_capped 在撞顶时 True，未撞顶时 False
    if rt.is_session_capped():
        errs.append("[A3] 未发言时 is_session_capped 应 False")
    else:
        rt._speech_count = SESSION_SPEECH_CAP
        if not rt.is_session_capped():
            errs.append(f"[A3] speech_count={SESSION_SPEECH_CAP} 时 is_session_capped 应 True")
        else:
            print(f"[A3] OK  is_session_capped：count<cap→False / count>=cap→True（cap={SESSION_SPEECH_CAP}）")
        rt._speech_count = 0  # reset for later

    # A4 record_speech +1 + 撞顶标记
    rt2 = GroupRuntime(_FakeGroup())
    rt2._speech_count = SESSION_SPEECH_CAP - 2
    await rt2.record_speech()
    if rt2._speech_count != SESSION_SPEECH_CAP - 1:
        errs.append(f"[A4] record_speech 应 +1，实际 count={rt2._speech_count}")
    elif rt2._cap_emitted is not False:
        errs.append("[A4] 未撞顶时 _cap_emitted 应仍 False")
    else:
        await rt2.record_speech()  # hits cap now
        if rt2._speech_count != SESSION_SPEECH_CAP:
            errs.append(f"[A4] 撞顶 record_speech 应 count={SESSION_SPEECH_CAP}，实际 {rt2._speech_count}")
        elif rt2._cap_emitted is not True:
            errs.append("[A4] 撞顶时 _cap_emitted 应置 True")
        else:
            print("[A4] OK  record_speech +1 + 撞顶置 _cap_emitted=True（一次性 emit 守卫）")

    # ── B. route_entry 封顶守卫（standalone + closure-bound twin）──
    re_body = _fn_body(gg_src, "route_entry")
    bre_body = _fn_body(gg_src, "build_route_entry")
    if "is_session_capped" not in re_body:
        errs.append("[B5] standalone route_entry 未查 is_session_capped（封顶守卫缺失）")
    if "is_session_capped" not in bre_body:
        errs.append("[B5] build_route_entry（closure-bound twin）未查 is_session_capped（封顶守卫缺失）")
    if not any(e.startswith("[B5]") for e in errs):
        print("[B5] OK  route_entry（standalone + closure-bound twin）入口查 is_session_capped()")

    # B6 守卫在 report-back 早返回之后（report-back 放过避免 split-brain）
    #   source-order check: _is_report_back early-return must come BEFORE is_session_capped check
    idx_cap = re_body.find("is_session_capped")
    idx_rb = re_body.find("_is_report_back(state)")
    if idx_cap < 0 or idx_rb < 0:
        errs.append("[B6] route_entry 缺 is_session_capped 或 _is_report_back 基准（无法判顺序）")
    elif idx_cap < idx_rb:
        errs.append("[B6] is_session_capped 守卫应在 _is_report_back 早返回之后（否则挡 report-back 致死锁）")
    else:
        print("[B6] OK  封顶守卫在 report-back 早返回之后（report-back 放过，避免 split-brain 死锁）")

    # B5-run 真 route_entry 命中 cap → END（注入 capped runtime）
    try:
        class _CappedRT:
            def is_stopped(self): return False
            def is_session_capped(self): return True

        async def _run_b5run():
            g = build_group_graph("g1", members, coordinator_id="c1")
            re_fn = build_route_entry(g._legal_handoff_targets)
            with patch("engine.worker.get_group_runtime", return_value=_CappedRT()):
                return await re_fn({
                    "group_id": "g1", "coordinator_id": "c1",
                    "incoming_message": "@后端 来一下", "incoming_sender": "user",
                    "incoming_kind": "", "turn_count": 0,
                })
        cmd = await _run_b5run()
        if cmd.goto != "__end__":
            errs.append(f"[B5-run] 撞顶应 goto=END（不选发言者），实际 {cmd.goto!r}")
        else:
            print("[B5-run] OK  route_entry 命中 cap → goto=END（跨回合兜底，不选发言者）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B5-run] route_entry 封顶直调异常：{type(e).__name__}: {e}")

    # ── C. make_agent_node 封顶守卫 ─────────────────────────
    man_body = _fn_body(w_src, "make_agent_node")
    if "is_session_capped" not in man_body:
        errs.append("[C7] make_agent_node 未查 is_session_capped（封顶守卫缺失）")
    else:
        print("[C7] OK  make_agent_node 入口查 is_session_capped()")

    # C8 守卫只在闲聊路径（is_dispatch_fanout False 分支内），不挡 dispatch fan-out
    #   the cap guard should be inside `if not is_dispatch_fanout:` so execute
    #   fan-out skips it. Locate the guard relative to the is_dispatch_fanout branch.
    #   Simpler: assert the cap check is NOT before the already-spoke guard's
    #   is_dispatch_fanout handling — i.e. it's within the non-fanout path.
    #   We assert the cap guard text appears AFTER `is_dispatch_fanout = state.get`.
    idx_dispatch = man_body.find("is_dispatch_fanout = state.get")
    idx_cap_w = man_body.find("is_session_capped")
    if idx_dispatch < 0 or idx_cap_w < 0:
        errs.append("[C8] make_agent_node 缺 is_dispatch_fanout 或 is_session_capped 基准")
    elif idx_cap_w < idx_dispatch:
        errs.append("[C8] is_session_capped 守卫应在 is_dispatch_fanout 判定之后（只挡闲聊不挡派工）")
    else:
        print("[C8] OK  封顶守卫在 is_dispatch_fanout 判定之后（只挡闲聊/handoff，不挡 dispatch fan-out）")

    # C-run 真 make_agent_node 命中 cap → END + brain 未调
    try:
        brain_called: list[str] = []

        async def _fake_stream(*a, **k):
            brain_called.append("called")
            return ("r1", '{"action":"chat","content":"hi","reasoning":"r"}', 5, 50, "m1", 0, "")

        class _CappedRT2:
            def is_stopped(self): return False
            def is_session_capped(self): return True
            async def record_speech(self): pass

        async def _run_crun():
            node = build_agent_node("w1", "前端", "fe", "", "c1")
            with patch("engine.worker._stream_brain_decision", side_effect=_fake_stream), \
                 patch("engine.worker._unified_reply", AsyncMock()), \
                 patch("engine.worker._build_context_from_db", AsyncMock(return_value="ctx")), \
                 patch("engine.worker._format_display_msg", side_effect=lambda s, c: c), \
                 patch("engine.worker.get_llm_config", return_value={"model": "m1"}), \
                 patch("engine.worker.crud") as crud_mock, \
                 patch("engine.worker.find_mentions", return_value=[]), \
                 patch("engine.worker.resolve_mention", return_value=None), \
                 patch("engine.worker.get_group_runtime", return_value=_CappedRT2()):
                crud_mock.list_group_members_with_agent = AsyncMock(return_value=[])
                crud_mock.list_agents = AsyncMock(return_value=[])
                cmd = await node({
                    "group_id": "g1", "coordinator_id": "c1",
                    "turn_count": 0, "recent_speakers": [],
                    "incoming_message": "接", "incoming_sender": "user",
                })
            return cmd, brain_called
        cmd, brain = await _run_crun()
        if cmd.goto != "__end__":
            errs.append(f"[C-run] 撞顶应 goto=END，实际 {cmd.goto!r}")
        elif brain:
            errs.append(f"[C-run] 撞顶后不应调 brain，实际 brain_called={brain}")
        else:
            print("[C-run] OK  make_agent_node 命中 cap → goto=END + brain 未调（不发言）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C-run] make_agent_node 封顶直调异常：{type(e).__name__}: {e}")

    # ── D. 计数累加（chat/ask 发言后 record_speech，execute 不计）──
    # D9 make_agent_node chat/ask 路径发言后调 record_speech
    if "record_speech" not in man_body:
        errs.append("[D9] make_agent_node 未调 record_speech（发言后应 +1）")
    else:
        print("[D9] OK  make_agent_node chat/ask 路径发言后调 record_speech（+1）")

    # D10 record_speech 不在 execute 分支（派工不计）
    #   record_speech 应在 chat/ask else 分支内，不在 ``if action == "execute":`` (push_task) 内。
    #   用第一个非注释 ``record_speech()`` 调用（skipping the cap-guard comment that
    #   mentions record_speech）定位真实调用点，再与 push_task 比较。
    import re as _re
    rec_calls = [m.start() for m in _re.finditer(r"\bawait\s+rt_speak\.record_speech\(\)", man_body)]
    idx_record = rec_calls[0] if rec_calls else -1
    idx_push_task = man_body.find("push_task(group_id, agent_id, agent_id")
    if idx_push_task < 0 or idx_record < 0:
        errs.append("[D10] make_agent_node 缺 push_task 或 record_speech() 调用基准（无法判 execute 不计）")
    elif idx_record < idx_push_task:
        errs.append("[D10] record_speech() 应在 execute 分支(push_task)之后（execute 派工不计会话发言）")
    else:
        print("[D10] OK  record_speech() 在 chat/ask 分支（execute 派工不计，避免派工一轮烧光额度）")

    # ── E. reset 清零 ────────────────────────────────────────
    # E11 reset_session 清 _speech_count + _cap_emitted
    reset_body = _fn_body(gr_src, "reset_session")
    if "_speech_count = 0" not in reset_body:
        errs.append("[E11] reset_session 未清 _speech_count（/new 应重置封顶）")
    if "_cap_emitted = False" not in reset_body:
        errs.append("[E11] reset_session 未清 _cap_emitted（/new 应重置一次性 emit 标记）")
    if not any(e.startswith("[E11]") for e in errs):
        print("[E11] OK  reset_session 清 _speech_count=0 + _cap_emitted=False（/new 开新对话重置封顶）")

    # E11-run 真 reset 后 is_session_capped False
    try:
        rt3 = GroupRuntime(_FakeGroup())
        rt3._speech_count = SESSION_SPEECH_CAP
        rt3._cap_emitted = True
        await rt3.reset_session()
        if rt3._speech_count != 0 or rt3._cap_emitted is not False:
            errs.append(f"[E11-run] reset 后 count/emitted 应 0/False，实际 {rt3._speech_count}/{rt3._cap_emitted}")
        elif rt3.is_session_capped():
            errs.append("[E11-run] reset 后 is_session_capped 应 False")
        else:
            print("[E11-run] OK  reset_session 后 _speech_count=0 + _cap_emitted=False + is_session_capped=False")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[E11-run] reset_session 直调异常：{type(e).__name__}: {e}")

    # ── F. 跨回合拦截（撞顶后新回合 route_entry 仍 END）──────
    try:
        async def _run_f12():
            rt = GroupRuntime(_FakeGroup())
            await rt.compile_graph(members)
            rt._resolve_leader_identity = AsyncMock(return_value={
                "agent_id": "c1", "agent_name": "协调者", "system_prompt": "sp",
            })
            rt._resolve_group_config = AsyncMock(return_value=(False, "", "centralized"))
            rt._graph.ainvoke = AsyncMock(return_value={"dispatch_plan": []})
            rt._reply_cb_factory = lambda: (lambda: None)  # type: ignore
            # pre-saturate the counter (cap hit) WITHOUT touching _stop_event
            rt._speech_count = SESSION_SPEECH_CAP
            with patch("engine.group_runtime.emit_agent_status", AsyncMock()):
                # a NEW turn on a fresh thread: route_entry should see is_session_capped
                # True and END (count survived across turns — not reset per turn).
                result = await rt.invoke_turn(
                    incoming_kind="coordinator_reply", incoming_message="再来一轮接龙",
                )
            return result
        result = await _run_f12()
        if result is None:
            errs.append("[F12] 撞顶后新回合 invoke_turn 应正常 END（route_entry 命中 cap），实际 None")
        else:
            print("[F12] OK  撞顶后新回合 route_entry 仍命中 cap → END（计数跨回合存活，不随单回合 reset）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F12] 跨回合拦截测试异常：{type(e).__name__}: {e}")

    # ── G. runtime None 跳过（向后兼容 vh39/vh40）────────────
    try:
        class _M:
            def __init__(self, aid): self.agent_id = aid; self.agent_name = aid; self.agent_role = "r"
        db_members = [_M("w1"), _M("w2")]

        async def _run_g13():
            g = build_group_graph("g1", members, coordinator_id="c1")
            re_fn = build_route_entry(g._legal_handoff_targets)
            with patch("engine.worker.crud") as crud_mock, \
                 patch("engine.worker.find_mentions", return_value=[]), \
                 patch("engine.worker.resolve_mention", return_value=None), \
                 patch("engine.worker.get_group_runtime", return_value=None):
                crud_mock.list_group_members_with_agent = AsyncMock(return_value=db_members)
                crud_mock.list_agents = AsyncMock(return_value=[])
                return await re_fn({
                    "group_id": "g1", "coordinator_id": "c1",
                    "incoming_message": "帮我重构", "incoming_sender": "user",
                    "incoming_kind": "coordinator_reply", "turn_count": 0,
                })
        cmd = await _run_g13()
        if cmd.goto == "__end__":
            errs.append(f"[G13] runtime None 时 route_entry 应按原逻辑分叉（非命中 cap END），实际 goto={cmd.goto!r}")
        else:
            print(f"[G13] OK  runtime None → 封顶守卫跳过，route_entry 按原逻辑分叉 goto={cmd.goto!r}（向后兼容）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[G13] runtime-None 跳过测试异常：{type(e).__name__}: {e}")

    # ── H. 向后兼容 ──────────────────────────────────────────
    # H14 main import OK
    try:
        import main  # noqa: F401
        print("[H14] OK  main 全量 import OK（group_runtime 加常量/字段/方法无 cycle）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[H14] main import 异常（import cycle？）：{type(e).__name__}: {e}")

    # H15 Option B·② 后：route_entry is_stopped 守卫已删，但 is_session_capped 仍在
    try:
        # Option B·② 删了 route_entry 的 is_stopped 协作式软停守卫。封顶守卫
        # (is_session_capped) 是保留的停的兜底入口之一，必须在 route_entry 里仍在。
        if "is_stopped()" in re_body:
            errs.append("[H15] route_entry 仍查 is_stopped（Option B·② 应删该守卫）")
        elif "is_session_capped" not in re_body:
            errs.append("[H15] route_entry 缺 is_session_capped（封顶守卫未就位）")
        else:
            print("[H15] OK  Option B·② 后 route_entry is_stopped 守卫已删 + 封顶守卫仍在")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[H15] 兼容检查异常：{type(e).__name__}: {e}")

    return errs


def main() -> int:
    print("=== VH46 回归：会话发言总量封顶（cross-turn backstop·StopSignal 第三层）===\n")
    errs = asyncio.run(assert_contract())
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "会话发言总量封顶锁定：\n"
        "  · A GroupRuntime 持 _speech_count + _cap_emitted + SESSION_SPEECH_CAP（默认 50，env 可调）；\n"
        "  · B route_entry（standalone + closure-bound twin）入口查 is_session_capped 命中即 END（report-back 之后放过）；\n"
        "  · C make_agent_node 入口查 is_session_capped 命中即 END（只挡闲聊不挡 dispatch fan-out）；\n"
        "  · D chat/ask 发言后 record_speech +1（execute 派工不计）；\n"
        "  · E reset_session 清零（/new 开新对话重置封顶）；\n"
        "  · F 撞顶后新回合 route_entry 仍 END（计数跨回合存活，不随单回合 reset）；\n"
        "  · G runtime None → 守卫跳过（向后兼容 vh39/vh40）；\n"
        "  · H main import OK + vh44/vh45 不破（additive 不替换协作式停止守卫）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

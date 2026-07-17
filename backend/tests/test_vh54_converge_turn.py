"""VH54 回归：去中心化 @收束 回合收敛（converge-turn-design）.

锁住 Option B 删停关键词后去中心化「人工停止」入口空缺的柔性收口——一个**新回合**：
UI 开「收束」开关 + @某 agent → 那个 agent 回复一句 → **不 handoff** → 回合自然 END。

设计真源见 memory ``converge-turn-design``（@收束 是去中心化专属独立入口，不复用停止按钮
也不解析消息内容）+ ``stop-signal-cooperative-cancel-design`` + ``decentralized-framework-paradigms``.

三机制划清（防混淆根源）：
  · 停关键词「停」/ request_stop 软停 — Option B 删（不在本任务范围）。
  · 停止按钮 cancel_turn — 两路径共用硬切，留。
  · **@收束（本任务）** — UI 开关 + @人，新回合 agent 回一句即 END 不 handoff，仅去中心化。

落点 8 处垂直切片，本测试锁其中 6 项核心契约（纯静态 + 真 asyncio stub + 真
build_group_graph，不依赖 live server / 真实 LLM）：

  A. GroupState.converge 字段锁——存在 + 默认 False
    1. ``GroupState`` TypedDict 含 ``converge: bool`` 字段。
    2. ``invoke_turn`` 默认不注入 converge（converge=False 时初始 state 无 converge=True）。

  B. invoke_turn converge 参数锁——注入初始 state
    3. ``invoke_turn`` 签名含 ``converge: bool = False`` keyword 参数。
    4. ``converge=True`` 时 invoke_turn 初始 state 带 ``converge=True``（注入群图）。

  C. make_agent_node 收束守卫锁——converge=True 时跳 handoff 走 END
    5. ``make_agent_node`` 末端（_resolve_handoff_target 之后、``if next_speaker is None`` 之前）
       查 ``state.get("converge")`` 命中即 ``next_speaker = None``。
    6. 真 make_agent_node：converge=True + 有 @mention（next_speaker 本应非 None）→ 返
       ``Command(goto=END)``（不 handoff，goto 非 agent_<peer>）。

  D. converge=False 正常 handoff 锁——不受收束影响
    7. 真 make_agent_node：converge=False（或无 converge）+ 有 @mention → 返
       ``Command(goto=agent_<peer>)``（正常 handoff）。

  E. record_speech 收束仍计锁——发言计数不因收束跳过
    8. make_agent_node 在 converge=True 时仍调 ``record_speech``（收束回复算 1 条发言）。

  F. is_dispatch_fanout 不受 converge 影响锁——派工 fan-out 本就 END
    9. make_agent_node 收束守卫在 ``if not is_dispatch_fanout`` 内（派工 fan-out 路径
       本就 next_speaker=None 走 END，converge 守卫只挡闲聊/handoff 路径）。

  G. 入站 converge 透传 + 400 拒绝锁
   10. ``route_user_message(group_id, content, *, converge=...)`` 接受 converge 关键字参数
       透传到 invoke_turn（@mention 路径）。
   11. ``route_user_message(converge=True)`` 且消息无 @mention → raise ValueError（收束必须
       @ 收口对象），API 层 messages.send_message 转 400。

  H. 向后兼容锁——main import OK + vh32/vh40/vh44/vh46 不破
   12. ``main`` 全量 import OK（state/worker/group_runtime/mention/messages 加 converge 无 cycle）。
   13. converge=False（默认）时一切照旧——make_agent_node 正常 handoff，invoke_turn 不注入 converge。
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
STATE_PY = BACKEND / "engine" / "state.py"
GROUP_RUNTIME_PY = BACKEND / "engine" / "group_runtime.py"
WORKER_PY = BACKEND / "engine" / "worker.py"
MENTION_PY = BACKEND / "engine" / "mention.py"
MESSAGES_PY = BACKEND / "api" / "messages.py"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fn_body(src: str, fn_name: str) -> str:
    """Return one method/function body (def ... to next def at column 0).

    Only breaks on a *top-level* (column-0) ``def``/``async def`` so a nested
    closure function is INCLUDED in the body, not treated as the end of the
    outer function.
    """
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
    state_src = _read(STATE_PY)
    gr_src = _read(GROUP_RUNTIME_PY)
    w_src = _read(WORKER_PY)
    mention_src = _read(MENTION_PY)
    messages_src = _read(MESSAGES_PY)

    try:
        from engine.state import GroupState  # type: ignore
        from engine.group_runtime import GroupRuntime  # type: ignore
        from engine import worker as worker_mod  # type: ignore
        from engine.worker import build_agent_node, make_agent_node  # type: ignore
        from engine.mention import route_user_message  # type: ignore
    except Exception as e:  # noqa: BLE001
        return [f"[import] 导入失败：{type(e).__name__}: {e}"]

    members = [
        {"agent_id": "w1", "agent_name": "前端", "agent_role": "fe", "system_prompt": "sp1"},
        {"agent_id": "w2", "agent_name": "后端", "agent_role": "be", "system_prompt": "sp2"},
    ]

    # ── A. GroupState.converge 字段 ──────────────────────────
    # A1 GroupState 含 converge: bool
    if "converge" not in state_src or "converge: bool" not in state_src:
        errs.append("[A1] GroupState 缺 converge: bool 字段声明")
    else:
        # confirm it's actually in the GroupState class (not CoordinatorState/WorkerState)
        # by checking it appears after the GroupState class def.
        gs_idx = state_src.find("class GroupState(")
        if gs_idx < 0 or state_src.find("converge: bool", gs_idx) < 0:
            errs.append("[A1] converge: bool 不在 GroupState 类内（应在 GroupState TypedDict 中）")
        else:
            print("[A1] OK  GroupState TypedDict 含 converge: bool 字段")

    # A2 invoke_turn 默认不注入 converge=True（converge=False 时初始 state 无 converge=True）
    invoke_body = _fn_body(gr_src, "invoke_turn")
    if "converge: bool = False" not in invoke_body and "converge: bool=False" not in invoke_body:
        # signature line may be on the def — check the whole body including def line
        if "converge" not in invoke_body:
            errs.append("[A2] invoke_turn 体内无 converge 引用（参数缺失？）")
    # the guard `if converge: turn_input["converge"] = True` means converge=False
    # does NOT inject converge=True. Verify the guard is conditional.
    if 'turn_input["converge"] = True' not in invoke_body and \
       "turn_input['converge'] = True" not in invoke_body:
        errs.append("[A2] invoke_turn 未在 converge=True 时注入 turn_input['converge']=True")
    else:
        # verify it's guarded by `if converge:`
        if "if converge:" not in invoke_body and "if converge :" not in invoke_body:
            errs.append("[A2] turn_input['converge']=True 应在 `if converge:` 守卫内（默认 False 不注入）")
        else:
            print("[A2] OK  invoke_turn converge=False 默认不注入（仅 converge=True 时注入 turn_input）")

    # ── B. invoke_turn converge 参数 ─────────────────────────
    # B3 invoke_turn 签名含 converge: bool=False
    try:
        sig = inspect.signature(GroupRuntime.invoke_turn)
        if "converge" not in sig.parameters:
            errs.append("[B3] invoke_turn 签名缺 converge 参数")
        else:
            p = sig.parameters["converge"]
            if p.default is not False:
                errs.append(f"[B3] invoke_turn converge 默认应 False，实际 {p.default!r}")
            else:
                print("[B3] OK  invoke_turn(..., converge: bool = False) 签名就位（默认 False）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B3] invoke_turn 签名检查异常：{type(e).__name__}: {e}")

    # B4 真 invoke_turn(converge=True) 初始 state 带 converge=True
    #   patch _resolve_leader_identity/_resolve_group_config + capture turn_input via ainvoke mock
    try:
        class _FakeGroup:
            id = "g1"
            coordinator_id = "c1"

        async def _run_b4():
            rt = GroupRuntime(_FakeGroup())
            await rt.compile_graph(members)
            rt._resolve_leader_identity = AsyncMock(return_value={
                "agent_id": "c1", "agent_name": "协调者", "system_prompt": "sp",
            })
            rt._resolve_group_config = AsyncMock(return_value=(False, ""))
            captured = {}
            async def _fake_ainvoke(turn_input, config=None):
                captured["turn_input"] = dict(turn_input)
                return {"dispatch_plan": []}
            rt._graph.ainvoke = _fake_ainvoke  # type: ignore
            rt._reply_cb_factory = lambda: (lambda: None)  # type: ignore
            with patch("engine.group_runtime.emit_agent_status", AsyncMock()):
                await rt.invoke_turn(
                    incoming_kind="agent_reply",
                    incoming_message="@后端 收个尾",
                    incoming_sender="user",
                    converge=True,
                )
            return captured.get("turn_input")

        ti = await _run_b4()
        if ti is None:
            errs.append("[B4] converge=True invoke_turn 未捕获 turn_input（ainvoke 未被调？）")
        elif ti.get("converge") is not True:
            errs.append(f"[B4] converge=True 时初始 state 应带 converge=True，实际 {ti.get('converge')!r}")
        else:
            print("[B4] OK  invoke_turn(converge=True) 初始 state 带 converge=True（注入群图）")

        # B4b converge=False（默认）不注入 converge=True
        async def _run_b4b():
            rt = GroupRuntime(_FakeGroup())
            await rt.compile_graph(members)
            rt._resolve_leader_identity = AsyncMock(return_value={
                "agent_id": "c1", "agent_name": "协调者", "system_prompt": "sp",
            })
            rt._resolve_group_config = AsyncMock(return_value=(False, ""))
            captured = {}
            async def _fake_ainvoke(turn_input, config=None):
                captured["turn_input"] = dict(turn_input)
                return {"dispatch_plan": []}
            rt._graph.ainvoke = _fake_ainvoke  # type: ignore
            rt._reply_cb_factory = lambda: (lambda: None)  # type: ignore
            with patch("engine.group_runtime.emit_agent_status", AsyncMock()):
                await rt.invoke_turn(
                    incoming_kind="agent_reply",
                    incoming_message="@后端 正常 handoff",
                    incoming_sender="user",
                    # converge defaults False
                )
            return captured.get("turn_input")

        ti2 = await _run_b4b()
        if ti2 is not None and ti2.get("converge") is True:
            errs.append("[B4b] converge=False（默认）时不应注入 converge=True")
        else:
            print("[B4b] OK  converge=False（默认）不注入 converge=True（正常回合不受影响）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B4] invoke_turn converge 注入测试异常：{type(e).__name__}: {e}")

    # ── C. make_agent_node 收束守卫 ──────────────────────────
    # C5 make_agent_node 末端查 state.get("converge") 命中即 next_speaker=None
    man_body = _fn_body(w_src, "make_agent_node")
    if 'state.get("converge")' not in man_body and "state.get('converge')" not in man_body:
        errs.append("[C5] make_agent_node 未查 state.get('converge')（收束守卫缺失）")
    else:
        print("[C5] OK  make_agent_node 末端查 state.get('converge')（收束守卫就位）")

    # C5b 守卫位置：在 _resolve_handoff_target 之后、if next_speaker is None 之前
    idx_resolve = man_body.find("next_speaker = await _resolve_handoff_target")
    idx_converge = man_body.find('state.get("converge")')
    if idx_converge < 0:
        idx_converge = man_body.find("state.get('converge')")
    idx_end = man_body.find("if next_speaker is None:")
    if idx_resolve < 0 or idx_converge < 0 or idx_end < 0:
        errs.append("[C5b] make_agent_node 守卫位置基准缺失（_resolve_handoff_target/state.get converge/if next_speaker is None）")
    elif not (idx_resolve < idx_converge < idx_end):
        errs.append(f"[C5b] 收束守卫位置应 _resolve_handoff_target < state.get(converge) < if next_speaker is None，"
                    f"实际 {idx_resolve}/{idx_converge}/{idx_end}")
    else:
        print("[C5b] OK  收束守卫在 _resolve_handoff_target 之后、if next_speaker is None 之前（精准落点）")

    # C5c 守卫在 if not is_dispatch_fanout 内（不挡派工 fan-out）
    #   the converge guard must be within the non-fanout serial path. Assert it
    #   appears AFTER the `is_dispatch_fanout = state.get` evaluation (the guard
    #   only fires for the serial peer path; fan-out execute already ENDs).
    idx_dispatch = man_body.find("is_dispatch_fanout = state.get")
    if idx_dispatch < 0:
        errs.append("[C5c] make_agent_node 缺 is_dispatch_fanout 基准")
    elif idx_converge < idx_dispatch:
        errs.append("[C5c] 收束守卫应在 is_dispatch_fanout 判定之后（只挡闲聊不挡派工）")
    else:
        print("[C5c] OK  收束守卫在 is_dispatch_fanout 判定之后（只挡闲聊/handoff，不挡 dispatch fan-out）")

    # C6 真 make_agent_node：converge=True + 有 @mention → Command(goto=END)（不 handoff）
    try:
        brain_called: list[str] = []
        reply_called: list[str] = []
        record_called: list[str] = []

        async def _fake_stream(*a, **k):
            brain_called.append("called")
            return ("r1", '{"action":"chat","content":"收到，我收个尾","reasoning":"r"}', 5, 50, "m1", 0, "")

        async def _fake_unified_reply(*a, **k):
            reply_called.append("called")

        class _RT:
            def is_stopped(self): return False
            def is_session_capped(self): return False
            async def record_speech(self):
                record_called.append("called")

        async def _run_c6():
            node = build_agent_node("w1", "前端", "fe", "", "c1")
            with patch("engine.worker._stream_brain_decision", side_effect=_fake_stream), \
                 patch("engine.worker._unified_reply", side_effect=_fake_unified_reply), \
                 patch("engine.worker._build_context_from_db", AsyncMock(return_value="ctx")), \
                 patch("engine.worker._format_display_msg", side_effect=lambda s, c: c), \
                 patch("engine.worker.get_llm_config", return_value={"model": "m1"}), \
                 patch("engine.worker.crud") as crud_mock, \
                 patch("engine.worker.find_mentions", return_value=["后端"]), \
                 patch("engine.worker.resolve_mention", return_value="w2"), \
                 patch("engine.worker.get_group_runtime", return_value=_RT()):
                crud_mock.list_group_members_with_agent = AsyncMock(return_value=[])
                crud_mock.list_agents = AsyncMock(return_value=[])
                cmd = await node({
                    "group_id": "g1", "coordinator_id": "c1",
                    "turn_count": 0, "recent_speakers": [],
                    "incoming_message": "@后端 收个尾", "incoming_sender": "user",
                    "converge": True,  # 收束回合
                })
            return cmd

        cmd = await _run_c6()
        if cmd.goto != "__end__":
            errs.append(f"[C6] converge=True + 有 @mention 应 goto=END（不 handoff），实际 {cmd.goto!r}")
        elif not brain_called:
            errs.append("[C6] 收束回合 brain 应照跑（生成那句收尾回复），实际未调")
        elif not reply_called:
            errs.append("[C6] 收束回合 _unified_reply 应照调（emit 那句收尾回复），实际未调")
        else:
            print("[C6] OK  converge=True + @mention → goto=END（不 handoff）+ brain/reply 照跑（收尾回复已 emit）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C6] make_agent_node 收束直调异常：{type(e).__name__}: {e}")

    # ── D. converge=False 正常 handoff ───────────────────────
    # D7 真 make_agent_node：converge=False + 有 @mention → Command(goto=agent_<peer>)
    try:
        async def _fake_stream_d(*a, **k):
            return ("r1", '{"action":"chat","content":"hi @后端","reasoning":"r"}', 5, 50, "m1", 0, "")

        class _RT2:
            def is_stopped(self): return False
            def is_session_capped(self): return False
            async def record_speech(self): pass

        async def _run_d7():
            node = build_agent_node("w1", "前端", "fe", "", "c1")
            with patch("engine.worker._stream_brain_decision", side_effect=_fake_stream_d), \
                 patch("engine.worker._unified_reply", AsyncMock()), \
                 patch("engine.worker._build_context_from_db", AsyncMock(return_value="ctx")), \
                 patch("engine.worker._format_display_msg", side_effect=lambda s, c: c), \
                 patch("engine.worker.get_llm_config", return_value={"model": "m1"}), \
                 patch("engine.worker.crud") as crud_mock, \
                 patch("engine.worker.find_mentions", return_value=["后端"]), \
                 patch("engine.worker.resolve_mention", return_value="w2"), \
                 patch("engine.worker.get_group_runtime", return_value=_RT2()):
                crud_mock.list_group_members_with_agent = AsyncMock(return_value=[])
                crud_mock.list_agents = AsyncMock(return_value=[])
                cmd = await node({
                    "group_id": "g1", "coordinator_id": "c1",
                    "turn_count": 0, "recent_speakers": [],
                    "incoming_message": "@后端 来一下", "incoming_sender": "user",
                    "converge": False,  # 正常回合
                })
            return cmd

        cmd = await _run_d7()
        if cmd.goto != "agent_w2":
            errs.append(f"[D7] converge=False + @mention 应 goto=agent_w2（正常 handoff），实际 {cmd.goto!r}")
        else:
            print("[D7] OK  converge=False + @mention → goto=agent_w2（正常 handoff，收束不影响正常回合）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D7] 正常 handoff 测试异常：{type(e).__name__}: {e}")

    # ── E. record_speech 收束仍计 ────────────────────────────
    # E8 make_agent_node converge=True 时仍调 record_speech（收束回复算 1 条发言）
    #   The C6 run already captured record_called. Re-assert via that path:
    #   record_speech is called in the chat/ask else-branch (before the converge
    #   guard), so converge=True still counts. Confirm via the C6 capture.
    if not record_called:
        errs.append("[E8] 收束回合 record_speech 应照调（收束回复算 1 条发言），C6 捕获为空")
    else:
        print("[E8] OK  收束回合仍调 record_speech（发言计数不因收束跳过，受 SESSION_SPEECH_CAP 约束）")

    # ── F. is_dispatch_fanout 不受 converge 影响 ─────────────
    # F9 派工 fan-out 路径（incoming_kind=coordinator_task）本就 next_speaker=None → END，
    #    converge 守卫不在其路径上。静态断言：收束守卫文本只在 `if not is_dispatch_fanout`
    #    的闲聊/handoff 路径生效（C5c 已锁位置）。这里补一个动态断言：execute 路径
    #    （action=execute + coordinator_task kind）不因 converge=True 改变 goto（仍 END）。
    try:
        async def _fake_stream_f(*a, **k):
            return ("r1", '{"action":"execute","content":"跑迁移","reasoning":"r"}', 5, 50, "m1", 0, "")

        class _RT3:
            def is_stopped(self): return False
            def is_session_capped(self): return False
            async def record_speech(self): pass

        async def _run_f9():
            node = build_agent_node("w1", "前端", "fe", "", "c1")
            with patch("engine.worker._stream_brain_decision", side_effect=_fake_stream_f), \
                 patch("engine.worker._unified_reply", AsyncMock()), \
                 patch("engine.worker._build_context_from_db", AsyncMock(return_value="ctx")), \
                 patch("engine.worker._format_display_msg", side_effect=lambda s, c: c), \
                 patch("engine.worker.get_llm_config", return_value={"model": "m1"}), \
                 patch("engine.worker.crud") as crud_mock, \
                 patch("engine.worker.find_mentions", return_value=[]), \
                 patch("engine.worker.resolve_mention", return_value=None), \
                 patch("engine.worker.push_task", AsyncMock()), \
                 patch("engine.worker.get_group_runtime", return_value=_RT3()):
                crud_mock.list_group_members_with_agent = AsyncMock(return_value=[])
                crud_mock.list_agents = AsyncMock(return_value=[])
                cmd = await node({
                    "group_id": "g1", "coordinator_id": "c1",
                    "turn_count": 0, "recent_speakers": [],
                    "incoming_message": "跑迁移", "incoming_sender": "coordinator",
                    "incoming_kind": "coordinator_task",  # dispatch fan-out
                    "converge": True,  # even if converge were set, fan-out path must END
                })
            return cmd

        cmd = await _run_f9()
        if cmd.goto != "__end__":
            errs.append(f"[F9] dispatch fan-out + converge=True 应 goto=END（派工本就 END），实际 {cmd.goto!r}")
        else:
            print("[F9] OK  dispatch fan-out 路径不受 converge 影响（派工本就 END，收束守卫不挡派工）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F9] dispatch fan-out 测试异常：{type(e).__name__}: {e}")

    # ── G. 入站 converge 透传 + 400 拒绝 ─────────────────────
    # G10 route_user_message 接受 converge 关键字参数
    try:
        sig_rum = inspect.signature(route_user_message)
        if "converge" not in sig_rum.parameters:
            errs.append("[G10] route_user_message 签名缺 converge 参数")
        else:
            p = sig_rum.parameters["converge"]
            if p.default is not False:
                errs.append(f"[G10] route_user_message converge 默认应 False，实际 {p.default!r}")
            else:
                print("[G10] OK  route_user_message(..., *, converge: bool=False) 签名就位")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[G10] route_user_message 签名检查异常：{type(e).__name__}: {e}")

    # G10b route_user_message @mention 路径透传 converge 到 invoke_turn
    if "converge=converge" not in mention_src:
        errs.append("[G10b] route_user_message 未透传 converge=converge 到 invoke_turn（@mention 路径）")
    else:
        print("[G10b] OK  route_user_message @mention 路径透传 converge=converge 到 invoke_turn")

    # G11 route_user_message(converge=True) 且无 @mention → raise ValueError
    try:
        raised = False
        try:
            # patch crud + registry so the function reaches the no-mention branch
            with patch("engine.mention.crud") as crud_mock, \
                 patch("engine.mention.find_mentions", return_value=[]):
                # a group with a coordinator so it reaches the no-mention -> coordinator
                # branch where the converge guard raises.
                class _G:
                    coordinator_id = "c1"
                    config = {}
                crud_mock.get_group = AsyncMock(return_value=_G())
                await route_user_message("g1", "收个尾", converge=True)
        except ValueError:
            raised = True
        if not raised:
            errs.append("[G11] route_user_message(converge=True) 无 @mention 应 raise ValueError（收束必须 @ 收口对象）")
        else:
            print("[G11] OK  route_user_message(converge=True) 无 @mention → raise ValueError（收束必须 @ 收口对象）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[G11] route_user_message 收束拒绝测试异常：{type(e).__name__}: {e}")

    # G11b API messages.send_message 转 400
    if "raise HTTPException(status_code=400" not in messages_src and \
       "HTTPException(status_code=400" not in messages_src:
        errs.append("[G11b] messages.send_message 未把 ValueError 转 400 HTTPException")
    elif "converge" not in messages_src:
        errs.append("[G11b] messages.send_message 未读 converge 字段（API 层未透传）")
    else:
        print("[G11b] OK  messages.send_message 读 converge + ValueError → 400 HTTPException（服务端兜底）")

    # ── H. 向后兼容 ──────────────────────────────────────────
    # H12 main import OK
    try:
        import main  # noqa: F401
        print("[H12] OK  main 全量 import OK（state/worker/group_runtime/mention/messages 加 converge 无 cycle）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[H12] main import 异常（import cycle？）：{type(e).__name__}: {e}")

    # H13 MessageCreatePayload.converge 默认 False（向后兼容既有调用方）
    try:
        from models import MessageCreatePayload  # type: ignore
        payload = MessageCreatePayload(group_id="g1", sender_id="user", content="hi")
        if getattr(payload, "converge", None) is not False:
            errs.append(f"[H13] MessageCreatePayload.converge 默认应 False，实际 {getattr(payload, 'converge', '<missing>')!r}")
        else:
            print("[H13] OK  MessageCreatePayload.converge 默认 False（既有调用方不受影响）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[H13] MessageCreatePayload 兼容检查异常：{type(e).__name__}: {e}")

    return errs


def main() -> int:
    print("=== VH54 回归：去中心化 @收束 回合收敛（converge-turn-design）===\n")
    errs = asyncio.run(assert_contract())
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "@收束 回合收敛锁定：\n"
        "  · A GroupState.converge: bool 字段就位（默认 False，last-value channel）；\n"
        "  · B invoke_turn(..., converge: bool=False) 签名 + converge=True 注入初始 state；\n"
        "  · C make_agent_node 末端查 state.get('converge') 命中即 next_speaker=None（精准落点：_resolve_handoff_target 之后、if next_speaker is None 之前）；\n"
        "  · D converge=False 正常 handoff（goto=agent_<peer>，收束不影响正常回合）；\n"
        "  · E record_speech 收束仍计（发言计数不跳过，受 SESSION_SPEECH_CAP 约束）；\n"
        "  · F is_dispatch_fanout 路径不受 converge 影响（派工 fan-out 本就 END）；\n"
        "  · G route_user_message 透传 converge + 无 @ 拒绝(ValueError→400)；\n"
        "  · H main import OK + MessageCreatePayload.converge 默认 False（纯加性，不破既有契约）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

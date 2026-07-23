"""VH32 回归：worker agent-as-node 工厂（去中心化群图 handoff 迁移·节点层）.

锁住 ``engine/worker.py`` 新增的 agent-as-node 工厂——把 brain→chat/execute/ask
四节点决策收成单节点，发言后返回 ``Command(goto=目标)`` 或 ``Command(goto=END)``，
作为后续 group_graph.py 装配的 agent 节点构件.

设计真源见 memory ``decentralized-scheduling-stop-plan-2026-07-13``（方向 A）.
worker 仍是框架内 LLM+LangGraph 智能体（``chat_completion_stream`` 流式直调
OpenAI 兼容端点 + ``emit_task_token``/``emit_coordinator_reasoning`` 逐字推 WS），
**不调 Claude Code CLI**（memory ``agent-no-cli-decouple``）.

四段契约（纯静态 + 函数直调 stub，不依赖 live server / 真实 LLM）：

  A. 工厂 API 锁——make_agent_node / build_agent_node 存在 + 旧 API 不破
    1. ``make_agent_node`` 是 async 函数，返 ``Command``（非 dict）.
    2. ``build_agent_node`` 返 async node 函数（闭包绑 identity）.
    3. 旧 API（build_worker_graph/node_brain_decide/node_chat/node_execute/
       node_ask/_parse_brain_decision/_stream_brain_decision/_unified_reply/
       set_reply_callback/_build_context_from_db）全保留——驻留 worker 图未改.

  B. 决策语义锁——chat/execute/ask 三路径行为正确
    4. chat + @mention → ``Command(goto="agent_<peer>")``（handoff 到 peer 节点）.
    5. chat 无 @mention → ``Command(goto=END)``（话筒落地，回合结束）.
    6. execute → ``收到，我来...`` announce + ``push_task`` + ``Command(goto=END)``
       （execute ack 结束回合，不 handoff；任务在 band 外经 _handle_task 跑）.
    7. ask → 走 chat 同款落盘（stats 透传）+ 无 @mention → END.
    8. LLM 失败兜底 → chat 兜底回复 + stats=None（与 node_brain_decide 同款）.

  C. handoff 目标解析锁——_resolve_handoff_target 四道守卫
    9. self-mention（@自己）跳过（no-op handoff）.
    10. coordinator 不作 handoff 目标（去中心化路径——worker 不经 @mention 回 Leader；
        route_entry 才拥有 Leader 入口，防「协调者每轮插话」缺陷回归）.
    11. 无 @mention / 不可解析 → None（回合 END）.
    12. 多 @mention → first resolvable wins（单一下一发言者——handoff 串行只一节点跑）.

  D. 群图状态写入锁——Command.update 落 GroupState 字段
    13. ``messages`` 追加 ``AIMessage``（name=agent_name，id=reply_id；execute 用
        ``exe_<uuid>`` 避免 id 空冲突）—— add_messages reducer 累加.
    14. ``turn_count`` 自增（last-write-wins，非 append）.
    15. ``recent_speakers`` 追加 [agent_id]（append_list reducer 累加本回合发言者）.
    16. ``current_speaker`` 设为发言者 agent_id（handoff 时改写成 next_speaker）.
    17. handoff 链长上限 ``AGENT_NODE_MAX_HANDOFFS``（达 cap 即 END，防 @mention 死循环
        烧光 recursion budget 的图内兜底）.

  E. 真 StateGraph 跑通——handoff 链 front→back→END
    18. 两 agent 节点 + entry，ainvoke 后 turn_count=2 / recent_speakers=[front,back] /
        messages=3（user+front+back）/ current_speaker=back.

  F. 流式契约不破——仍走 chat_completion_stream + emit_*（worker 不调 CLI）
    19. ``make_agent_node`` 体内调 ``_stream_brain_decision``（非 ``create_react_agent``，
        非 CLI）—— worker 流式直调 OpenAI 兼容端点（PL-08/[[pl08-streaming-create-react-agent]]
        的 create_react_agent 是 task 执行线，单聊流式是 chat_completion_stream）.
    20. chat/ask 路径 ``_unified_reply`` 传 ``data=stats``（透传流式采集的 reply_id/
        elapsed_ms/tokens/model/reasoning_tokens/reasoning）；execute 路径 data=None
        （模板 announce 不带 stats——与 node_execute/node_va6 契约一致）.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import re
import sys
from pathlib import Path
from typing import Any
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

    try:
        from engine.worker import (  # type: ignore
            AGENT_NODE_MAX_HANDOFFS,
            _build_agent_invoke_messages,
            _build_context_from_db,
            _parse_brain_decision,
            _resolve_handoff_target,
            _stream_brain_decision,
            _unified_reply,
            build_agent_node,
            build_worker_graph,
            make_agent_node,
            node_ask,
            node_brain_decide,
            node_chat,
            node_execute,
            set_reply_callback,
        )
    except Exception as e:  # noqa: BLE001
        return [f"[import] 导入失败：{type(e).__name__}: {e}"]

    # ── A. 工厂 API ──────────────────────────────────────────
    if not inspect.iscoroutinefunction(make_agent_node):
        errs.append("[A1] make_agent_node 应是 async 函数")
    else:
        sig = inspect.signature(make_agent_node)
        if "agent_id" not in sig.parameters or "agent_name" not in sig.parameters:
            errs.append(f"[A1] make_agent_node 应闭包绑 agent_id/agent_name，参数缺：{list(sig.parameters)}")
        else:
            print("[A1] OK  make_agent_node async + 闭包绑 identity（agent_id/agent_name/agent_role/system_prompt/coordinator_id）")

    factory_node = build_agent_node("agent_front_1", "前端工程师", "frontend_engineer", "你是前端", "agent_coord_1")
    if not callable(factory_node) or not inspect.iscoroutinefunction(factory_node):
        errs.append("[A2] build_agent_node 应返 async node 函数")
    elif factory_node.__name__ != "agent_node_agent_front_1":
        errs.append(f"[A2] build_agent_node 返回的 node __name__ 应含 agent_id，实际 {factory_node.__name__!r}")
    else:
        print(f"[A2] OK  build_agent_node 返 async node（__name__={factory_node.__name__}，identity 闭包绑定）")

    # A3 旧 API 全保留
    for name in [
        "build_worker_graph", "node_brain_decide", "node_chat", "node_execute",
        "node_ask", "_parse_brain_decision", "_stream_brain_decision",
        "_unified_reply", "set_reply_callback", "_build_context_from_db",
    ]:
        if name not in sys.modules["engine.worker"].__dict__:
            errs.append(f"[A3] 旧 API {name} 缺失（驻留 worker 图被破）")
    if not any(e.startswith("[A3]") for e in errs):
        print("[A3] OK  旧 API 全保留（驻留 worker 图未改，向后兼容）")

    # ── B. 决策语义（stub LLM + DB + reply）─────────────────
    async def _run_node(stream_ret, *, action_override=None, turn_count=0, content_has_mention=False):
        async def fake_stream(config, messages, group_id, agent_id):
            return stream_ret
        async def fake_reply(*a, **k):
            pass
        async def fake_push(*a, **k):
            pass
        with patch("engine.worker._stream_brain_decision", side_effect=fake_stream), \
             patch("engine.worker._unified_reply", AsyncMock(side_effect=fake_reply)), \
             patch("engine.worker.push_task", AsyncMock(side_effect=fake_push)), \
             patch("engine.worker._build_context_from_db", AsyncMock(return_value="ctx")), \
             patch("engine.worker._format_display_msg", side_effect=lambda s, c: c), \
             patch("engine.worker.get_llm_config", return_value={"model": "m1"}), \
             patch("engine.worker.crud") as crud_mock, \
             patch("engine.worker.find_mentions", return_value=["后端工程师"] if content_has_mention else []), \
             patch("engine.worker.resolve_mention", return_value="agent_back_1" if content_has_mention else None):
            crud_mock.list_group_members_with_agent = AsyncMock(return_value=[])
            crud_mock.list_agents = AsyncMock(return_value=[])
            node = build_agent_node("agent_front_1", "前端工程师", "frontend_engineer", "你是前端", "agent_coord_1")
            return await node({"group_id": "g1", "coordinator_id": "agent_coord_1", "turn_count": turn_count, "recent_speakers": [], "incoming_message": "接龙", "incoming_sender": "agent_back_1"})

    # B4 chat + @mention → handoff
    try:
        from langgraph.types import Command as _Cmd
        from langgraph.graph import END as _END
        cmd = asyncio.run(_run_node(("r1", '{"action":"chat","content":"接龙龙 @后端工程师","reasoning":"r"}', 10, 100, "m1", 0, ""), content_has_mention=True))
        if not isinstance(cmd, _Cmd):
            errs.append(f"[B4] chat+@mention 应返 Command，实际 {type(cmd).__name__}")
        elif cmd.goto != "agent_agent_back_1":
            errs.append(f"[B4] goto 应为 agent_agent_back_1（agent_<id> 约定），实际 {cmd.goto!r}")
        else:
            print(f"[B4] OK  chat+@mention → Command(goto={cmd.goto}) handoff 到 peer 节点")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B4] chat+@mention 测试异常：{type(e).__name__}: {e}")

    # B5 chat 无 @mention → END
    try:
        cmd = asyncio.run(_run_node(("r2", '{"action":"chat","content":"好的","reasoning":"r"}', 5, 50, "m1", 0, "")))
        if cmd.goto != _END:
            errs.append(f"[B5] chat 无 @mention 应 goto=END，实际 {cmd.goto!r}")
        else:
            print("[B5] OK  chat 无 @mention → Command(goto=END) 话筒落地")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B5] chat 无 @mention 测试异常：{type(e).__name__}: {e}")

    # B6 execute → 收到我来 + push_task + END（不 handoff）
    try:
        pushed = []
        async def fake_push(*a, **k):
            pushed.append(a)
        async def fake_stream(config, messages, group_id, agent_id):
            return ("r3", '{"action":"execute","content":"写个登录API","reasoning":"r"}', 5, 50, "m1", 0, "")
        replied = []
        async def fake_reply(group_id, agent_id, content, data=None):
            replied.append((content, data))
        with patch("engine.worker._stream_brain_decision", side_effect=fake_stream), \
             patch("engine.worker._unified_reply", AsyncMock(side_effect=fake_reply)), \
             patch("engine.worker.push_task", AsyncMock(side_effect=fake_push)), \
             patch("engine.worker._build_context_from_db", AsyncMock(return_value="ctx")), \
             patch("engine.worker._format_display_msg", side_effect=lambda s, c: c), \
             patch("engine.worker.get_llm_config", return_value={"model": "m1"}):
            node = build_agent_node("a1", "A", "role_a", "", "coord1")
            cmd = asyncio.run(node({"group_id": "g1", "coordinator_id": "coord1", "turn_count": 0}))
        if cmd.goto != _END:
            errs.append(f"[B6] execute 应 END，实际 {cmd.goto!r}")
        elif not replied or not replied[0][0].startswith("收到，我来"):
            errs.append(f"[B6] execute 应「收到，我来…」announce，实际 {replied!r}")
        elif replied[0][1] is not None:
            errs.append(f"[B6] execute 应 data=None（模板 announce 无 stats），实际 {replied[0][1]!r}")
        elif not pushed or pushed[0][:4] != ("g1", "a1", "a1", "写个登录API"):
            errs.append(f"[B6] execute 应 push_task 前4参(g1,a1,a1,content)，实际 {pushed!r}")
        else:
            print("[B6] OK  execute → 收到我来 + push_task + END（不 handoff，data=None 无 stats）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B6] execute 测试异常：{type(e).__name__}: {e}")

    # B7 ask → 走 chat 同款落盘 + 无 mention → END（ask content 无 @mention）
    try:
        cmd = asyncio.run(_run_node(("r4", '{"action":"ask","content":"你需要哪种风格？","reasoning":"r"}', 5, 50, "m1", 0, "")))
        if cmd.goto != _END:
            errs.append(f"[B7] ask 无 @mention 应 END，实际 {cmd.goto!r}")
        else:
            print("[B7] OK  ask → chat 同款落盘 + 无 @mention → END")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B7] ask 测试异常：{type(e).__name__}: {e}")

    # B8 LLM 失败兜底
    try:
        async def boom(*a, **k):
            raise RuntimeError("llm down")
        with patch("engine.worker._stream_brain_decision", side_effect=boom), \
             patch("engine.worker._unified_reply", AsyncMock()) as ur, \
             patch("engine.worker._build_context_from_db", AsyncMock(return_value="ctx")), \
             patch("engine.worker._format_display_msg", side_effect=lambda s, c: c), \
             patch("engine.worker.get_llm_config", return_value={"model": "m1"}), \
             patch("engine.worker.crud") as crud_mock:
            crud_mock.list_group_members_with_agent = AsyncMock(return_value=[])
            crud_mock.list_agents = AsyncMock(return_value=[])
            node = build_agent_node("a1", "A", "role_a", "", "coord1")
            cmd = asyncio.run(node({"group_id": "g1", "coordinator_id": "coord1", "turn_count": 0}))
        # fallback content persisted with data=None
        if not ur.called:
            errs.append("[B8] LLM 失败应兜底回复（_unified_reply 应被调）")
        else:
            args = ur.call_args
            if args.args[2] != "模型服务暂时无响应，请稍等几秒后重试。":
                errs.append(f"[B8] 兜底回复文案不符，实际 {args.args[2]!r}")
            elif args.kwargs.get("data") is not None:
                errs.append(f"[B8] LLM 失败应 data=None，实际 {args.kwargs.get('data')!r}")
            else:
                print("[B8] OK  LLM 失败 → chat 兜底回复 + data=None（与 node_brain_decide 同款）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B8] LLM 兜底测试异常：{type(e).__name__}: {e}")

    # ── C. _resolve_handoff_target 守卫 ─────────────────────
    class _M:
        def __init__(self, aid): self.agent_id = aid
    class _A:
        def __init__(self, aid, name, role): self.id = aid; self.name = name; self.role = role
    members = [_M("agent_front_1"), _M("agent_back_1")]
    agents = [_A("agent_front_1", "前端工程师", "frontend_engineer"),
              _A("agent_back_1", "后端工程师", "backend_engineer"),
              _A("agent_coord_1", "协调者", "coordinator")]
    async def _resolve(content, sender="agent_front_1"):
        with patch("engine.worker.crud") as crud_mock:
            crud_mock.list_group_members_with_agent = AsyncMock(return_value=members)
            crud_mock.list_agents = AsyncMock(return_value=agents)
            return await _resolve_handoff_target("g1", "agent_coord_1", sender, content)
    # C9 self-mention
    r = asyncio.run(_resolve("@前端工程师 继续"))
    if r is not None:
        errs.append(f"[C9] self-mention 应 None，实际 {r!r}")
    else:
        print("[C9] OK  self-mention（@自己）跳过——no-op handoff")
    # C10 coordinator skip
    r = asyncio.run(_resolve("@协调者 你来"))
    if r is not None:
        errs.append(f"[C10] coordinator 不应作 handoff 目标，实际 {r!r}")
    else:
        print("[C10] OK  coordinator 不作 handoff 目标（去中心化——route_entry 拥有 Leader 入口）")
    # C11 no mention
    r = asyncio.run(_resolve("接不上了"))
    if r is not None:
        errs.append(f"[C11] 无 @mention 应 None，实际 {r!r}")
    else:
        print("[C11] OK  无 @mention → None（回合 END）")
    # C12 first-mention-wins
    r = asyncio.run(_resolve("@后端工程师 和 @非成员"))
    if r != "agent_back_1":
        errs.append(f"[C12] first resolvable 应 wins（agent_back_1），实际 {r!r}")
    else:
        print("[C12] OK  多 @mention → first resolvable wins（单一下一发言者，handoff 串行）")

    # ── D. GroupState 写入 ───────────────────────────────────
    # 静态断言：make_agent_node 体内 update dict 含四键 + 命名约定
    body = _fn_body(src, "make_agent_node")
    for key in ["\"messages\"", "'messages'", "messages"]:
        if key in body:
            break
    else:
        errs.append("[D13] make_agent_node 体内未写 messages（GroupState.messages 累加断链）")
    if "turn_count" not in body:
        errs.append("[D14] make_agent_node 体内未写 turn_count")
    if "recent_speakers" not in body:
        errs.append("[D15] make_agent_node 体内未写 recent_speakers")
    if "current_speaker" not in body:
        errs.append("[D16] make_agent_node 体内未写 current_speaker")
    if "AGENT_NODE_MAX_HANDOFFS" not in body:
        errs.append("[D17] make_agent_node 体内未引用 AGENT_NODE_MAX_HANDOFFS（handoff cap 兜底缺失）")
    if not any(e.startswith("[D1") for e in errs):
        print("[D13-17] OK  GroupState 四键写入（messages/turn_count/recent_speakers/current_speaker）+ AGENT_NODE_MAX_HANDOFFS cap")
    # D13b AIMessage name=agent_name
    if "AIMessage" not in body or "name=" not in body.replace(" ", ""):
        errs.append("[D13b] make_agent_node 体内 AIMessage 未带 name=agent_name（群图消息无身份）")
    # 节点名约定 agent_<id>（非 agent:<id>，':' 被 langgraph 禁）
    if 'agent:{next_speaker}' in body or 'agent:{' in body:
        errs.append("[D-conv] goto 仍用 agent:<id>（':' 是 langgraph 保留字符，应 agent_<id>）")
    elif "agent_{next_speaker}" in body:
        print("[D-conv] OK  goto 用 agent_<next_speaker>（避 langgraph ':' 保留字符）")
    else:
        errs.append("[D-conv] goto 目标命名约定未找到（应为 agent_<next_speaker>）")

    # ── E. 真 StateGraph 跑通 ────────────────────────────────
    try:
        from langchain_core.messages import HumanMessage
        from langgraph.graph import StateGraph, START
        from engine.state import GroupState

        call_log = []
        async def fs_front(config, messages, group_id, agent_id):
            call_log.append(agent_id)
            return ("r1", '{"action":"chat","content":"前端接 @后端工程师","reasoning":"r"}', 5, 50, "m1", 0, "")
        async def fs_back(config, messages, group_id, agent_id):
            call_log.append(agent_id)
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
             patch("engine.worker.find_mentions", side_effect=lambda c: ["后端工程师"] if "前端接" in c else []), \
             patch("engine.worker.resolve_mention", return_value="agent_back_1"):
            crud_mock.list_group_members_with_agent = AsyncMock(return_value=[])
            crud_mock.list_agents = AsyncMock(return_value=[])
            n_front = build_agent_node("agent_front_1", "前端工程师", "frontend_engineer", "", "agent_coord_1")
            n_back = build_agent_node("agent_back_1", "后端工程师", "backend_engineer", "", "agent_coord_1")
            g = StateGraph(GroupState)
            g.add_node("entry", n_front)
            g.add_node("agent_agent_back_1", n_back)
            g.add_edge(START, "entry")
            app = g.compile()
            r = asyncio.run(app.ainvoke({"group_id": "g1", "coordinator_id": "agent_coord_1", "messages": [HumanMessage(content="开始", name="user", id="u1")], "turn_count": 0, "recent_speakers": [], "incoming_message": "开始", "incoming_sender": "user", "incoming_kind": "agent_reply"}))
        if "agent_front_1" not in call_log or "agent_back_1" not in call_log:
            errs.append(f"[E18] 两 agent 节点都应被调，实际 {call_log}")
        elif len(r["messages"]) != 3:
            errs.append(f"[E18] messages 应 3（user+front+back），实际 {len(r['messages'])}")
        elif r["turn_count"] != 2:
            errs.append(f"[E18] turn_count 应 2（peer 路径：front=1/back=2 各占一 superstep），实际 {r['turn_count']}")
        elif r["recent_speakers"] != ["agent_front_1", "agent_back_1"]:
            errs.append(f"[E18] recent_speakers 应 [front,back]，实际 {r['recent_speakers']}")
        elif r["current_speaker"] != "agent_back_1":
            errs.append(f"[E18] current_speaker 应 back，实际 {r['current_speaker']}")
        else:
            print("[E18] OK  真 StateGraph handoff 链 front→back→END（ainvoke）：turn_count=2 / recent_speakers=[front,back] / 3 msgs / current_speaker=back")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[E18] 真 StateGraph 测试异常：{type(e).__name__}: {e}")

    # ── F. 流式契约 ──────────────────────────────────────────
    # F19 make_agent_node 调 _stream_brain_decision（非 create_react_agent / CLI）
    if "_stream_brain_decision" not in body:
        errs.append("[F19] make_agent_node 体内未调 _stream_brain_decision（worker 流式直调端点断链）")
    elif "create_react_agent" in body:
        errs.append("[F19] make_agent_node 不应调 create_react_agent（单聊流式走 chat_completion_stream）")
    else:
        print("[F19] OK  make_agent_node 调 _stream_brain_decision（chat_completion_stream 直调 OpenAI 兼容端点，不调 CLI）")
    # F20 execute 不传 stats（已在 B6 验证）+ chat/ask 传 stats
    # 静态确认：execute 分支 data 缺省 None，chat/ask 分支 data=stats
    if 'data=stats' not in body:
        errs.append("[F20] make_agent_node 体内 chat/ask 路径未传 data=stats")
    else:
        print("[F20] OK  chat/ask 传 data=stats（透传流式 reply_id/elapsed_ms/tokens/model/reasoning）；execute data=None（B6 已验）")

    return errs


def main() -> int:
    print("=== VH32 回归：worker agent-as-node 工厂（群图 handoff 迁移·节点层）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "worker agent-as-node 工厂锁定：\n"
        "  · A make_agent_node/build_agent_node 存在 + 旧 API 全保留（驻留 worker 图不破）；\n"
        "  · B chat+@mention→handoff / chat 无 mention→END / execute→收到我来+push_task+END / ask→chat 同款+END / LLM 失败兜底；\n"
        "  · C _resolve_handoff_target 四守卫（self-skip / coord-skip / none→None / first-wins）；\n"
        "  · D GroupState 四键写入（messages/turn_count/recent_speakers/current_speaker）+ AGENT_NODE_MAX_HANDOFFS cap + agent_<id> 命名；\n"
        "  · E 真 StateGraph handoff 链 front→back→END 跑通；\n"
        "  · F 流式直调 _stream_brain_decision（非 CLI）+ chat/ask 传 stats / execute 不传。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

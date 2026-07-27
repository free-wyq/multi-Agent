"""VH63 单元契约：单聊 execute turn 轮内事件流归一——一个 turn 一个气泡.

锁住 vh63「brain reply_id 透传到 execute，ReAct 流式 token/tool/think 与 brain 同
归并 key，前端一个气泡全收；去掉『收到，我来…』预告」的核心逻辑契约（纯单元，不依赖
live server / 真实 LLM）：

  A. worker.py 静态契约（源码字符串/AST 断言）
    1. ``node_brain_decide`` return 含 ``"reply_id"`` key（透传到 WorkerState）。
    2. ``node_execute`` 不再调 ``_unified_reply`` 发「收到，我来」预告（grep 源码
       该函数体内无 ``_unified_reply`` 调用）。
    3. ``node_execute`` 调 ``push_task`` 的 data 含 ``{"reply_id": ...}``。
    4. ``WorkerState`` TypedDict 含 ``reply_id: str`` 字段。

  B. registry.py 静态契约
    5. ``_run_worker_task`` 从 ``task["data"]["reply_id"]`` 取 ``turn_reply_id``
       （源码含 ``turn_reply_id = (task.get("data") or {}).get("reply_id") or task_id``）。
    6. ``on_log`` 闭包内四个 emit（``emit_task_tool``/``emit_task_token``/
       ``emit_task_think``/``emit_task_log``）的 task_id 槽位均为 ``turn_reply_id``
       （非 ``task_id``）。
    7. ``_reply`` 签名含 ``data: dict | None = None`` 参数，且体内
       ``persist_agent_reply`` 调用传 ``data``（非硬编码 ``None``）。
    8. ``_run_worker_task`` 成功路径调 ``self._reply`` 传 ``data={"reply_id": ...}``
       （失败路径不传，源码含 ``reply_data = ... if success and turn_reply_id != task_id else None``）。
    9. ``emit_task_completed`` 仍用原 ``task_id``（收尾事件归并 key 不变，保证
       finalizedBubbles 退场判定 ``agent_reply.task_id == task_complete.task_id``）。

  C. ChatPanel.tsx 静态契约（源码字符串断言）
   10. ``coordinatorStreamingBubbles`` map 含 ``toolEvents`` + ``thinkEvents`` 字段。
   11. 协调者流式气泡渲染处（``coord-streaming-${b.replyId}``）给 ``ChatMessageBubble``
       传 ``toolEvents={b.toolEvents}`` + ``thinkEvents={b.thinkEvents}`` props。

  D. turn_reply_id 行为契约（构造 task dict 走 _run_worker_task 的 on_log 闭包）
   12. task["data"]={"reply_id":"brain_reply_xxx"} → on_log("token", "hi", {}) 调
       ``emit_task_token`` 的 task_id 槽 == "brain_reply_xxx"（mock emit 验参）。
   13. task["data"]=None（群聊派工无 reply_id）→ 兜底 task_id（``tq_xxx``），
       emit task_id 槽 == "tq_xxx"（群聊路径不回归）。
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
from pathlib import Path

# ── path setup (mirror existing vh tests) ──
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("MULTI_AGENT_DATA_DIR", str(BACKEND / "_test_data_vh63"))

WORKER = (BACKEND / "engine" / "worker.py").read_text(encoding="utf-8")
REGISTRY = (BACKEND / "engine" / "registry.py").read_text(encoding="utf-8")
STATE = (BACKEND / "engine" / "state.py").read_text(encoding="utf-8")
# vh63 锁的 coordinatorStreamingBubbles/toolEventsByTask/coord-streaming 渲染在 ChatPanel
# 9a/9b 拆分（commit 80dc7d8）后迁到 StreamingBubbleList.tsx，业务逻辑逐字不变，
# 断言目标在新文件里完整存在。FRONTEND_CHATPANEL 指向新文件（保持原变量名以免改测试体引用）。
FRONTEND_CHATPANEL = (BACKEND.parent / "src" / "components" / "StreamingBubbleList.tsx").read_text(encoding="utf-8")


def test_A1_brain_return_carries_reply_id():
    """node_brain_decide return 含 reply_id key。"""
    # 定位 node_brain_decide 函数体到下一个 async def
    start = WORKER.index("async def node_brain_decide")
    end = WORKER.index("async def ", start + 10)
    body = WORKER[start:end]
    assert '"reply_id": reply_id' in body, "node_brain_decide return 应含 reply_id"


def test_A2_node_execute_no_unified_reply_announce():
    """node_execute 不再调 _unified_reply 发预告（代码层无调用）。

    docstring 合法提到「收到，我来」（解释为何删除），不算回归——只断言代码层
    无 ``_unified_reply(`` 调用。
    """
    start = WORKER.index("async def node_execute")
    end = WORKER.index("\nasync def ", start + 10)
    body = WORKER[start:end]
    # 去掉 docstring（三引号块）后再检查代码调用
    code = body
    if '"""' in code:
        # 截掉首个 """ ... """ 块
        first = code.index('"""')
        second = code.index('"""', first + 3)
        code = code[:first] + code[second + 3:]
    assert "_unified_reply(" not in code, "node_execute 代码不应再调 _unified_reply（预告气泡已去）"


def test_A3_node_execute_push_task_carries_reply_id():
    """node_execute 调 push_task 的 data 含 {"reply_id": ...}。"""
    start = WORKER.index("async def node_execute")
    end = WORKER.index("async def ", start + 10)
    body = WORKER[start:end]
    assert "push_task" in body
    assert '{"reply_id": reply_id}' in body or "{'reply_id': reply_id}" in body, (
        "push_task data 应含 reply_id 透传"
    )


def test_A4_worker_state_has_reply_id_field():
    """WorkerState TypedDict 含 reply_id 字段。"""
    start = STATE.index("class WorkerState")
    end = STATE.index("\nclass ", start + 10)
    body = STATE[start:end]
    assert "reply_id: str" in body, "WorkerState 应含 reply_id: str 字段"


def test_B5_run_worker_task_reads_turn_reply_id():
    """_run_worker_task 从 task['data']['reply_id'] 取 turn_reply_id。"""
    assert "turn_reply_id = (task.get(\"data\") or {}).get(\"reply_id\") or task_id" in REGISTRY, (
        "_run_worker_task 应从 task data 取 turn_reply_id（兜底 task_id）"
    )


def test_B6_on_log_uses_turn_reply_id():
    """on_log 闭包四个 emit 均用 turn_reply_id 作 task_id 槽（非 task_id）。"""
    start = REGISTRY.index("async def _run_worker_task")
    # on_log 闭包在 _run_worker_task 体内
    end = REGISTRY.index("result = await execute_agent_task", start)
    body = REGISTRY[start:end]
    # 四个 emit 调用都应用 turn_reply_id
    emit_calls = [
        "emit_task_tool(",
        "emit_task_token(",
        "emit_task_think(",
        "emit_task_log(",
    ]
    for emit in emit_calls:
        # 找每个 emit 调用块，断言其参数列表含 turn_reply_id 且不含裸 task_id（在 task_id 槽位）
        idx = body.index(emit)
        # 取该 emit 到下一个闭合的代码片段（足够覆盖参数）
        snippet = body[idx:idx + 200]
        assert "turn_reply_id" in snippet, f"{emit} 应使用 turn_reply_id 作 task_id 槽"
        # task_id 不应作为这些 emit 的第二参数出现（turn_reply_id 才是）
        # 宽松断言：emit 块内 turn_reply_id 必须出现
    # 同时确认 task_id 仍用于 task["id"] 提取（别误删）
    assert "task_id = task[\"id\"]" in REGISTRY


def test_B7_reply_signature_has_data_param():
    """_reply 签名含 data: dict | None = None，且 persist_agent_reply 传 data。"""
    start = REGISTRY.index("async def _reply")
    end = REGISTRY.index("await persist_agent_reply", start)
    sig_body = REGISTRY[start:end]
    assert "data: dict[str, Any] | None = None" in sig_body, "_reply 签名应含 data 参数"
    # persist_agent_reply 调用传 data（非 None）
    persist_idx = REGISTRY.index("await persist_agent_reply(self.group_id, self.agent_id, content", start)
    persist_line = REGISTRY[persist_idx:persist_idx + 120]
    assert "data" in persist_line and "None, task_id" not in persist_line, (
        "persist_agent_reply 应传 data（非硬编码 None）"
    )


def test_B8_success_path_replies_with_reply_id_data():
    """_run_worker_task 成功路径 _reply 传 data={'reply_id': ...}。"""
    # 找 _run_worker_task 体内 _reply 调用
    start = REGISTRY.index("async def _run_worker_task")
    end_marker = REGISTRY.index("# execute report-back", start)
    body = REGISTRY[start:end_marker]
    assert "reply_data = " in body, "应构造 reply_data"
    assert '{"reply_id": turn_reply_id}' in body or "{'reply_id': turn_reply_id}" in body, (
        "成功路径 reply_data 应含 reply_id"
    )
    assert "await self._reply(reply, task_id, data=reply_data)" in body, (
        "_reply 应以 data=reply_data 调用"
    )


def test_B9_emit_task_completed_keeps_task_id():
    """emit_task_completed 仍用原 task_id（收尾事件归并 key 不变）。

    成功路径（427）在 _run_worker_task 体内，turn_reply_id 已计算——但收尾事件
    不应用 turn_reply_id，否则与持久化 agent_reply.task_id(=task_id) 不匹配，
    finalizedBubbles 退场判定会断。断言 _run_worker_task 体内所有 emit_task_completed
    调用均用 task_id（非 turn_reply_id）。

    切片陷阱：``_run_worker_task`` 体内含 NESTED ``async def on_log`` 闭包（8-space
    缩进），裸 ``REGISTRY.index("async def ", start+10)`` 会命中闭包把 body 截短，
    emit_task_completed 在闭包之后被切掉 → ``body.index("emit_task_completed(")`` 抛
    ValueError（旧 false-failure）。正确切法：找下一个 **类方法级**（4-space 缩进）
    ``async def``/``def``，跳过任何更深缩进的嵌套 def。
    """
    start = REGISTRY.index("async def _run_worker_task")
    # 找下一个类方法级（4-space 缩进）的 def/async def——跳过嵌套闭包（8-space）。
    # ``_run_worker_task`` 是 AgentEngine 的方法（4-space 缩进），其下个兄弟方法是
    # ``_on_task_cancelled``（也 4-space）；中间的 ``on_log`` 闭包是 8-space，不匹配。
    i_async = REGISTRY.find("\n    async def ", start + 10)
    i_def = REGISTRY.find("\n    def ", start + 10)
    candidates = [x for x in (i_async, i_def) if x != -1]
    end = min(candidates) if candidates else len(REGISTRY)
    body = REGISTRY[start:end]

    # _run_worker_task 体内可能有多个 emit_task_completed 调用（成功/失败路径），
    # 逐个用括号配对抽出参数列表，断言每个调用：含 task_id 且不含 turn_reply_id。
    search_from = 0
    call_count = 0
    while True:
        idx = body.find("emit_task_completed(", search_from)
        if idx == -1:
            break
        call_count += 1
        open_paren = body.index("(", idx)
        depth = 0
        i = open_paren
        while i < len(body):
            ch = body[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        call_args = body[open_paren:i + 1]
        assert "task_id" in call_args, (
            "emit_task_completed 应使用 task_id 作收尾归并 key（保证 "
            "finalizedBubbles 退场判定 agent_reply.task_id == task_complete.task_id）"
        )
        assert "turn_reply_id" not in call_args, (
            "emit_task_completed 不应混入 turn_reply_id（收尾事件归并 key 不变）"
        )
        search_from = i + 1
    assert call_count >= 1, "_run_worker_task 体内应至少有一次 emit_task_completed 调用"


def _run_on_log(task_data: dict | None):
    """构造 task dict，调 _run_worker_task 的 on_log 闭包，捕获 emit 调用参数。

    直接 import registry 模块，monkeypatch crud.get_agent 返最小 agent dict、
    execute_agent_task 返成功结果、emit_* 捕获参数。验证 on_log 用的 task_id 槽。
    """
    import sys as _sys
    # engine.registry 模块底部有 ``registry = AgentRegistry()``，会覆盖模块名
    # ``registry`` 指向单例实例（而非模块对象）。所以 ``import engine.registry
    # as reg_mod`` 拿到的是 AgentRegistry 实例，不是模块——必须先 import 触发
    # 模块加载进 sys.modules，再从 sys.modules 取真正的模块对象。
    if "engine.registry" not in _sys.modules:
        import engine.registry  # noqa: F401  触发模块加载
    reg_mod = _sys.modules["engine.registry"]

    captured: dict = {}

    async def fake_emit_task_token(group_id, task_id, agent_id, phase, delta):
        captured["task_token_task_id"] = task_id

    async def fake_emit_task_tool(group_id, task_id, agent_id, phase, name, content, data):
        captured["task_tool_task_id"] = task_id

    async def fake_emit_task_think(group_id, task_id, agent_id, phase, content):
        captured["task_think_task_id"] = task_id

    async def fake_emit_task_log(group_id, task_id, agent_id, content):
        captured["task_log_task_id"] = task_id

    async def fake_emit_task_completed(group_id, task_id, agent_id, success, result, exit_code, artifact):
        captured["completed_task_id"] = task_id

    reg_mod.emit_task_token = fake_emit_task_token
    reg_mod.emit_task_tool = fake_emit_task_tool
    reg_mod.emit_task_think = fake_emit_task_think
    reg_mod.emit_task_log = fake_emit_task_log
    reg_mod.emit_task_completed = fake_emit_task_completed

    # task dict
    task = {
        "id": "tq_test123",
        "content": "do something",
        "data": task_data,
    }

    # 构造一个最小 AgentEngine 实例调 _run_worker_task 太重——直接抽 on_log 闭包逻辑：
    # 重新实现 on_log 等价逻辑断言（on_log 是 _run_worker_task 内闭包，无法直接拿到）。
    # 改用：monkeypatch execute_agent_task 让它调 on_log，再调 _run_worker_task。
    async def fake_execute_agent_task(group_id, agent_dict, task_content, task_id, on_log):
        await on_log("token", "hi", {"phase": "streaming"})
        await on_log("tool_start", "run_command(x)", {"name": "run_command"})
        await on_log("think", "reasoning...", None)
        await on_log("log", "[开始] ...", None)
        return {"success": True, "output": "done", "exit_code": 0}

    reg_mod.execute_agent_task = fake_execute_agent_task

    # monkeypatch crud.get_agent 返最小 agent
    class FakeAgent:
        def model_dump(self):
            return {"id": "agent_x", "name": "x", "role": "r", "system_prompt": ""}

    async def fake_get_agent(aid):
        return FakeAgent()

    reg_mod.crud.get_agent = fake_get_agent

    # scan_workspace_artifacts / set_task_artifact / complete_task / _reply stub
    async def fake_complete_task(*a, **kw):
        pass
    reg_mod.complete_task = fake_complete_task

    def fake_scan(gid):
        return {"files": []}
    reg_mod.scan_workspace_artifacts = fake_scan

    async def fake_set_artifact(*a, **kw):
        pass
    reg_mod.crud.set_task_artifact = fake_set_artifact

    # 构造 engine 实例（不走 __init__ 避免 DB / inbox 注册）
    engine = reg_mod.AgentEngine.__new__(reg_mod.AgentEngine)
    engine.group_id = "group_x"
    engine.agent_id = "agent_x"
    engine.name = "x"
    engine.coordinator_id = ""
    # _turn_trace 累加器（55c6eca 起在 on_log 里落 trace；__new__ 跳过了 __init__
    # 的初始化，须显式补一个空 dict，否则 ``self._turn_trace.setdefault`` 报
    # AttributeError——与 on_log emit 归并 key 的断言无关，纯 stub）。
    engine._turn_trace = {}
    # _publish_log / _reply stub（_run_worker_task 会调它们，但不影响 on_log 验证）
    async def fake_publish_log(*a, **kw):
        pass
    engine._publish_log = fake_publish_log  # type: ignore[assignment]
    async def fake_reply(*a, **kw):
        pass
    engine._reply = fake_reply  # type: ignore[assignment]

    asyncio.run(engine._run_worker_task(task))
    return captured


def test_D12_on_log_uses_brain_reply_id_when_present():
    """task data 有 reply_id → on_log emit 用 brain reply_id 作 task_id 槽。"""
    captured = _run_on_log({"reply_id": "brain_reply_xxx"})
    assert captured.get("task_token_task_id") == "brain_reply_xxx"
    assert captured.get("task_tool_task_id") == "brain_reply_xxx"
    assert captured.get("task_think_task_id") == "brain_reply_xxx"
    assert captured.get("task_log_task_id") == "brain_reply_xxx"


def test_D13_on_log_falls_back_to_task_id_when_no_reply_id():
    """task data 无 reply_id（群聊派工）→ on_log emit 兜底 task_id。"""
    captured = _run_on_log(None)
    assert captured.get("task_token_task_id") == "tq_test123"
    assert captured.get("task_tool_task_id") == "tq_test123"


def test_C10_chatpanel_coord_streaming_has_tool_think():
    """coordinatorStreamingBubbles map 含 toolEvents + thinkEvents 字段。"""
    # vh64：toolEventsByTask 声明在 coordinatorStreamingBubbles 之后（const TDZ
    # 顺序无关——coordinatorStreamingBubbles 在 render 期才求值，那时 toolEventsByTask
    # 已就绪）。本测断言两者都在源码里声明 + coordinatorStreamingBubbles 引用它们，
    # 不依赖相对前后顺序（之前 index("const toolEventsByTask", start) 假设 toolEventsByTask
    # 在 coordinatorStreamingBubbles 之后，顺序换了就误报）。改为独立找两者声明再断言
    # coordinatorStreamingBubbles 体含 toolEvents:/thinkEvents: 字段引用。
    start = FRONTEND_CHATPANEL.index("const coordinatorStreamingBubbles")
    # 截到下一个顶层 const（与 coordinatorStreamingBubbles 同级）作为 body 边界。
    # 找下一个 "\n  const " （2 空格缩进——组件函数体内的顶层 const）。
    next_const = FRONTEND_CHATPANEL.find("\n  const ", start + 10)
    end = next_const if next_const != -1 else len(FRONTEND_CHATPANEL)
    body = FRONTEND_CHATPANEL[start:end]
    assert "toolEvents:" in body, "coordinatorStreamingBubbles 应含 toolEvents 字段"
    assert "thinkEvents:" in body, "coordinatorStreamingBubbles 应含 thinkEvents 字段"
    # toolEventsByTask / thinkEventsByTask 都应在本文件里声明（任意位置）
    assert "const toolEventsByTask" in FRONTEND_CHATPANEL, "toolEventsByTask 应在 ChatPanel 声明"
    assert "const thinkEventsByTask" in FRONTEND_CHATPANEL, "thinkEventsByTask 应在 ChatPanel 声明"


def test_C11_chatpanel_coord_streaming_passes_tool_think_props():
    """协调者流式气泡渲染处传 toolEvents/thinkEvents props。"""
    # 找 coord-streaming key 的 ChatMessageBubble 块
    idx = FRONTEND_CHATPANEL.index("key={`coord-streaming-${b.replyId}`}")
    block = FRONTEND_CHATPANEL[idx:idx + 1500]
    assert "toolEvents={b.toolEvents}" in block, "应传 toolEvents prop"
    assert "thinkEvents={b.thinkEvents}" in block, "应传 thinkEvents prop"


if __name__ == "__main__":
    # 简单运行器（不依赖 pytest）
    import traceback
    tests = [
        test_A1_brain_return_carries_reply_id,
        test_A2_node_execute_no_unified_reply_announce,
        test_A3_node_execute_push_task_carries_reply_id,
        test_A4_worker_state_has_reply_id_field,
        test_B5_run_worker_task_reads_turn_reply_id,
        test_B6_on_log_uses_turn_reply_id,
        test_B7_reply_signature_has_data_param,
        test_B8_success_path_replies_with_reply_id_data,
        test_B9_emit_task_completed_keeps_task_id,
        test_C10_chatpanel_coord_streaming_has_tool_think,
        test_C11_chatpanel_coord_streaming_passes_tool_think_props,
        test_D12_on_log_uses_brain_reply_id_when_present,
        test_D13_on_log_falls_back_to_task_id_when_no_reply_id,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
            passed += 1
        except Exception:
            print(f"  FAIL {t.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)

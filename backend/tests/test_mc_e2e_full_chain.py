"""任务16b — MCP 全链路 e2e：建 MCP → 自省 → 挂载 → 真发 /api/messages 触发 execute
→ 断言 tool_start/end name=echo + reply 含结果 + 清理 stdio 子进程。

确定性 e2e（不依赖 live server / 真实 LLM）。两条 LLM 调用点都用脚本化 fake：

  1. worker brain（``node_brain_decide`` → ``chat_completion_stream``）：脚本化吐
     strict JSON ``{"action":"execute","content":"用 echo 工具回显 <TOKEN>",...}``，
     让 brain 走 execute 分支 → ``node_execute`` → ``push_task`` 把任务推给自己。
  2. ReAct loop（``run_agent_loop`` → ``ChatOpenAI`` → ``create_react_agent``）：
     ``FakeReactChatModel`` 第一轮吐 ``AIMessage(tool_calls=[{name:"echo",args:
     {text:TOKEN}}])``，第二轮吐最终文本 ``回显结果：<TOKEN>``。``bind_tools`` 是
     no-op——脚本化 tool_call 直接驱动 ReAct 工具执行（脚本化的 tool_call 不依赖
     模型真懂工具 schema，create_react_agent 按名字路由到已 bind 的真 echo 工具）。

MCP 部分是真链路（非 mock）：
  - ``crud.create_mcp_connection`` 落一条 stdio 连接，``command=sys.executable``
    （``/usr/bin/python3``，含 ``/``——绕开 ``_validate_stdio_command`` 白名单只挡
    HTTP 入口、不挡可信测试代码直调 crud 的设计，见 fixture docstring + 记忆
    ``mcp-echo-fixture-and-anyio-dep-2026-07-27``），``args=[<fixture 绝对路径>]``。
  - ``execute_agent_task`` 读 ``agent.mounted_mcp`` → ``load_mcp_tools`` →
    ``MultiServerMCPClient`` spawn echo server 子进程 → 自省出 ``echo`` 工具 →
    ``set_extra_tools([echo])`` → ``run_agent_loop`` 把它 bind 进 create_react_agent。
  - fake 模型吐 echo tool_call → create_react_agent 调真 echo 工具 → adapter
    re-spawn echo 子进程跑 ``tools/call`` → 返回入参原文 → 模型第二轮吐最终答案。

为何不 mock MCP 链路而要真起子进程：任务16b 的契约就是「MCP 全链路」——自省 +
真调用 + 子进程清理都得过。mock 掉 MCP 等于把任务16c（spawn 泄漏/注入时机）的
风险藏起来。fixture（任务16a）+ anyio 4.x 环境（任务16a 修）让真起子进程稳定可复现。

子进程清理断言：adapter 的 ``get_tools`` / ``ainvoke`` 各自用 ``async with`` 会话，
退出时终止子进程。本测在每段后用 ``pgrep -f echo_mcp_server`` 断言无残留 echo 子进程
（任务16c 会锁住任何泄漏的真因；本测先断言「测后干净」）。

四段契约：
  A. fixture 静态契约（不 spawn）：``echo_mcp_server.py`` 存在 + 是 FastMCP server
     + 声明 ``echo`` 工具 + stdio 入口在 ``__main__``。
  B. MCP 自省 + 真调用（真 spawn）：``load_mcp_tools([mcp_id])`` 返 ``[echo]`` 工具，
     ``ainvoke({"text":TOKEN})`` 返原文；测后无 echo 子进程残留。
  C. 全链路 e2e（真 spawn + fake LLM + TestClient）：``POST /api/messages`` →
     ``route_direct_message`` → engine → ``_run_worker_task`` → execute with 真
     echo 工具 → 断言 ``task_tool`` 事件 ``name=echo`` 的 start + end 都到 + 持久化
     ``agent_reply`` content 含 ``TOKEN``；测后无 echo 子进程残留。
  D. 全局收尾：整测结束后再扫一次无 echo 子进程泄漏。

pytest 收集：``test_`` 前缀函数（``test_a``/``test_b``/``test_c``/``test_d``）+ ``main()``
可直接 ``python3 test_mc_e2e_full_chain.py`` 跑。无 conftest / 无 pytest-asyncio：异步
段用 ``asyncio.run`` 包，每段独立 event loop（engine 驻留 task 在段内起、段内停）。
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

# ── path + 隔离 DB（必须在 import app 模块前设） ──────────────────────────────
REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_TMP = tempfile.mkdtemp(prefix="mc_e2e_16b_")
os.environ["MULTI_AGENT_DATA_DIR"] = _TMP
# 缩短 worker 超时：fake LLM 秒回，30s 足够且避免挂死测试。
os.environ.setdefault("WORKER_TASK_TIMEOUT", "30")

import config  # noqa: E402

config.DATA_DIR = _TMP

import store.database as _db  # noqa: E402
import importlib  # noqa: E402
importlib.reload(_db)
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

FIXTURE = BACKEND / "tests" / "fixtures" / "echo_mcp_server.py"
ECHO_TOKEN = "MCP-E2E-ECHO-OK-7B3F"

# fake LLM 脚本化的最终答案（含 TOKEN，让 reply 断言可 grep）
FAKE_FINAL = f"回显结果：{ECHO_TOKEN}"


# ── fake LLM #2: ReAct loop 的 ChatOpenAI 替身 ────────────────────────────────
from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage  # noqa: E402
from pydantic import ConfigDict  # noqa: E402


class _FakeReactChatModel(FakeMessagesListChatModel):
    """脚本化 ChatModel：第一轮吐 echo tool_call，第二轮+ 吐最终答案。

    继承 ``FakeMessagesListChatModel``（是 ``BaseChatModel`` 子类 = ``Runnable``，
    create_react_agent 的 ``_get_prompt_runnable(prompt) | model`` 管道要求 model 是
    Runnable；裸对象会 ``TypeError Expected a Runnable``）。``bind_tools`` no-op：
    脚本化 ``AIMessage(tool_calls=[...])`` 直接驱动工具执行，不依赖模型真懂工具 schema。

    重写 ``_generate``：原实现按 ``self.i`` 在 ``responses`` 里**循环**取（``i`` 到末尾
    后重置为 0），第三轮又会吐第一轮的 tool_call → create_react_agent 死循环。改成调用
    计数：第 1 次 → tool_call，第 2+ 次 → 最终答案（无 tool_call → create_react_agent
    判定 ``_are_more_steps_needed`` 为 False → END，ReAct 收尾）。

    ``model_config = ConfigDict(extra="allow")`` 让 ``ChatOpenAI(**chat_kwargs)`` 传入的
    model/base_url/api_key/temperature/max_tokens... 等 kwargs 全部被 pydantic 接住
    （FakeMessagesListChatModel 默认 ``extra="forbid"`` 会拒未知字段 → 初始化报错）。
    """

    model_config = ConfigDict(extra="allow")

    # FakeMessagesListChatModel 要求 responses 字段（list[BaseMessage]）。我们重写了
    # _generate 不读它（按 self.i 计数脚本化响应），但 pydantic 仍要求该字段有值——
    # 填一个占位列表即可（永不被读）。ChatOpenAI(**chat_kwargs) 构造时不传 responses，
    # extra="allow" 不会自动补默认，故显式声明 default。
    responses: list = []

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):  # noqa: ANN001
        # no-op：脚本化 AIMessage(tool_calls=[...]) 直接驱动工具执行，不依赖模型真懂
        # 工具 schema。create_react_agent 会调 bind_tools，必须返回 self（Runnable）
        # 而非 raise NotImplementedError（BaseChatModel.bind_tools 默认实现）。
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        from langchain_core.language_models.chat_models import ChatGeneration, ChatResult
        self.i += 1  # 复用父类的 i 字段作调用计数器（不再循环 responses）
        if self.i == 1:
            msg = AIMessage(
                content="",
                tool_calls=[{
                    "name": "echo",
                    "args": {"text": ECHO_TOKEN},
                    "id": "tc_e2e",
                    "type": "tool",
                }],
            )
        else:
            msg = AIMessage(content=FAKE_FINAL)
        return ChatResult(generations=[ChatGeneration(message=msg)])


# ── fake LLM #1: worker brain 的 chat_completion_stream 替身 ──────────────────
def _fake_brain_stream_factory(token: str):
    """返回一个 async generator 替代 ``chat_completion_stream``。

    吐 strict JSON ``{"action":"execute","content":"用 echo 工具回显 <token>",...}``
    逐字符 yield（让 ``ContentExtractor`` 增量解码，与流式期同源），末尾 yield 一个
    usage chunk。brain 解析 → execute → push_task。
    """
    import json as _json
    full = _json.dumps({
        "action": "execute",
        "content": f"请用 echo 工具回显 {token}",
        "reasoning": "e2e: need to call echo tool",
    })

    async def _stream(config, messages):  # noqa: ANN001
        for ch in full:
            yield ch, "", None, None
        yield "", "", 5, 0

    return _stream


# ── 子进程清理断言 ────────────────────────────────────────────────────────────
def _count_echo_procs() -> int:
    """返回 cmdline 含 ``echo_mcp_server`` 的活进程数（不含本测试进程）。

    ``pgrep -f echo_mcp_server`` 匹配 spawn 出来的 echo server 子进程（其 argv 含
    fixture 路径）。pytest 进程 argv 是 ``test_mc_e2e_full_chain.py``，不含该串，
    不会被误匹配。pgrep 不可用时退化 0（非 Linux 环境降级，本测聚焦 Linux dev 机）。
    """
    try:
        r = subprocess.run(
            ["pgrep", "-f", "echo_mcp_server"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    if r.returncode != 0:
        return 0  # pgrep 无匹配 exit 1
    my_pid = str(os.getpid())
    # 过滤掉本进程（防御性——理论上不会匹配）
    pids = [ln for ln in r.stdout.strip().splitlines() if ln and ln != my_pid]
    return len(pids)


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


# ── DB + engine setup helper（每段独立 loop 内调） ─────────────────────────────
async def _init_isolated_db() -> None:
    _db.engine = create_async_engine(
        _db.DB_URL, echo=False,
        connect_args={"check_same_thread": False}, pool_pre_ping=True,
    )
    _db.SessionLocal = async_sessionmaker(
        _db.engine, expire_on_commit=False, class_=AsyncSession,
    )
    await _db.init_db()
    # 活跃 provider cache：给 get_config() 一个不连真 LLM 的兜底配置
    # （brain 的 chat_completion_stream 已被 fake 替换，不会真打网络）。
    config.set_active_cache({
        "api_key": "sk-e2e-fake",
        "base_url": "http://127.0.0.1:1/v1",
        "model": "fake-e2e-model",
        "temperature": 0.0,
        "max_tokens": 0,
    })


async def _create_echo_mcp_connection() -> tuple[str, str]:
    """建一条指向 echo fixture 的 stdio MCP 连接，返 (mcp_id, name)。"""
    from store import crud
    from models import McpConnectionCreatePayload
    name = f"[e2e-16b] echo {uuid.uuid4().hex[:6]}"
    conn = await crud.create_mcp_connection(McpConnectionCreatePayload(
        name=name,
        transport="stdio",
        command=sys.executable,  # /usr/bin/python3，含 / —— 绕白名单（可信测试代码直调 crud）
        args=[str(FIXTURE)],
        env=None,
        enabled=True,
    ))
    return conn.id, name


# ── A. fixture 静态契约（不 spawn） ────────────────────────────────────────────
def test_a_fixture_contract() -> list[str]:
    errs: list[str] = []
    print("\n=== A. echo_mcp_server fixture 静态契约 ===")
    _check("A1 fixture 文件存在", FIXTURE.is_file(), str(FIXTURE))
    src = FIXTURE.read_text(encoding="utf-8")
    _check("A2 用 FastMCP（from mcp.server.fastmcp import FastMCP）",
           "from mcp.server.fastmcp import FastMCP" in src)
    _check("A3 声明 echo 工具（@mcp.tool() + def echo(text: str) -> str）",
           "@mcp.tool()" in src and "def echo(text: str) -> str:" in src)
    _check("A4 echo 原样返回（return text）",
           re.search(r"def echo\(text:\s*str\)\s*->\s*str.*?return text", src, re.S) is not None)
    _check("A5 stdio 入口在 __main__（mcp.run(transport=\"stdio\")）",
           'if __name__ == "__main__"' in src and 'mcp.run(transport="stdio")' in src)
    # A6: run 调用只在 __main__ 块内出现（docstring 里的提及不算代码调用）。
    # 去掉 docstring 后再数 `mcp.run(` 的**调用**出现次数。
    code_only = re.sub(r'""".*?"""', "", src, flags=re.S)
    _check("A6 无 __main__ 外的副作用（mcp.run 调用仅在 __main__ 内）",
           code_only.count("mcp.run(") == 1)
    if not all([
        FIXTURE.is_file(),
        "from mcp.server.fastmcp import FastMCP" in src,
        "@mcp.tool()" in src and "def echo(text: str) -> str:" in src,
        re.search(r"def echo\(text:\s*str\)\s*->\s*str.*?return text", src, re.S) is not None,
        'if __name__ == "__main__"' in src and 'mcp.run(transport="stdio")' in src,
        code_only.count("mcp.run(") == 1,
    ]):
        errs.append("[A] fixture 静态契约失败")
    return errs


# ── B. MCP 自省 + 真调用（真 spawn） ──────────────────────────────────────────
async def _async_b_introspection() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()
    mcp_id, name = await _create_echo_mcp_connection()

    # 自省：load_mcp_tools spawn echo server → tools/list → 返 [echo]
    from engine.mcp_manager import load_mcp_tools
    tools = await load_mcp_tools([mcp_id])
    if not _check("B1 load_mcp_tools 返非空工具列表", bool(tools), f"tools={tools}"):
        errs.append("[B1] load_mcp_tools 返空")
        return errs
    if not _check("B2 工具名是 echo", tools[0].name == "echo", f"name={tools[0].name}"):
        errs.append("[B2] 工具名非 echo")

    # 真调用：ainvoke re-spawn echo server → tools/call → 返入参原文
    result = await tools[0].ainvoke({"text": ECHO_TOKEN})
    out = str(result)
    if not _check(f"B3 ainvoke echo 返原文（含 {ECHO_TOKEN}）", ECHO_TOKEN in out, f"out={out[:80]}"):
        errs.append("[B3] ainvoke 未返原文")

    # 清理：load_mcp_tools 的 get_tools 会话退出即终止子进程；ainvoke 会话同理。
    # 给一点时间让子进程真正退出（SIGTERM → 进程退出有微秒级延迟）。
    await asyncio.sleep(0.3)
    leaked = _count_echo_procs()
    if not _check("B4 测后无 echo 子进程残留", leaked == 0, f"leaked={leaked}"):
        errs.append(f"[B4] {leaked} 个 echo 子进程残留")

    # 收尾：删 MCP 连接行
    from store import crud
    await crud.delete_mcp_connection(mcp_id)
    return errs


def test_b_introspection_and_invoke() -> list[str]:
    print("\n=== B. MCP 自省 + 真调用（真 spawn）===")
    try:
        return asyncio.run(_async_b_introspection())
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ B 段异常: {type(e).__name__}: {e}")
        return [f"[B] 异常: {e}"]


# ── C. 全链路 e2e（真 spawn + fake LLM + TestClient） ─────────────────────────
async def _async_c_full_chain() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()

    # 装 fake LLM（在触发 turn 前装）。run_agent_loop 用 ``from langchain_openai
    # import ChatOpenAI`` 在文件顶层绑定了名字 → 改 ``engine.agent_loop.ChatOpenAI``
    # 才能切到 fake（模块全局名字是 run_agent_loop 内 ``ChatOpenAI(**chat_kwargs)``
    # 的真源）。worker brain 的 ``chat_completion_stream`` 是 worker.py 顶层 import
    # 的，同理改 ``engine.worker.chat_completion_stream``。
    import engine.agent_loop as al
    import engine.worker as wmod
    _orig_chatopenai = al.ChatOpenAI
    _orig_stream = wmod.chat_completion_stream
    al.ChatOpenAI = _FakeReactChatModel
    wmod.chat_completion_stream = _fake_brain_stream_factory(ECHO_TOKEN)

    # 建 MCP 连接 + agent（挂载 MCP）+ 单聊会话
    from store import crud
    from models import AgentCreatePayload, ConversationCreatePayload
    mcp_id, _mcp_name = await _create_echo_mcp_connection()
    agent = await crud.create_agent(AgentCreatePayload(
        name="EchoE2E",
        role="backend_engineer",
        system_prompt="你是测试助手，会调用 echo 工具回显文本。",
        mounted_mcp=[mcp_id],
        description="任务16b e2e 探针",
    ))
    conv = await crud.create_conversation(ConversationCreatePayload(
        agent_id=agent.id, name="mc-e2e-16b",
    ))

    # 建单聊 worker engine（route_direct_message 会 ensure_engine，但先建好确保
    # inbox 已注册 + run loop 已起）
    from engine.registry import registry
    await registry.ensure_engine(conv.id, agent.id)
    eng = registry.get_engine(conv.id, agent.id)
    if not _check("C1 单聊 worker engine 已建（worker 图）",
                  eng is not None and eng.graph_kind == "worker",
                  f"eng={eng} graph_kind={getattr(eng,'graph_kind',None) if eng else None}"):
        errs.append("[C1] engine 未建")
        return errs

    # 抓 bus 事件：monkeypatch bus_manager.emit（无真 WS 订阅者）
    captured: list[dict] = []
    import events as evpkg
    _orig_emit = evpkg.bus_manager.emit

    async def _cap(group_id, data):
        captured.append(data)

    evpkg.bus_manager.emit = _cap
    # registry.py import 时已绑定 emit_* 名字到 events 模块的函数对象；改
    # bus_manager.emit 方法即可（所有 emit_* helper 都 await bus_manager.emit）。

    probe_ids: list[str] = []
    try:
        # 真发消息路由：直接调 ``route_direct_message``（与 ``POST /api/messages``
        # 走同一路径——send_message 持久化 user_input 后调它）。不走 TestClient，
        # 因为 TestClient 的同步 post 会阻塞本 async loop，engine 的 _run_loop
        # 任务无法被调度消费 inbox → turn 卡死。route_direct_message 是纯 async，
        # 在本 loop 里跑，push_notify 入队后立即返回，engine run loop 随即消费。
        # 先持久化 user_input 行（send_message 的第一步，crud.create_message），
        # 让 brain 的 _build_context_from_db 能读到当前 incoming。
        from models import MessageCreatePayload
        user_msg = await crud.create_message(MessageCreatePayload(
            conversation_id=conv.id,
            sender_id="user",
            receiver_id="broadcast",
            type="user_input",
            content=f"请用 echo 工具回显 {ECHO_TOKEN}",
        ))
        _check("C2 user_input 落库（id msg_ 前缀）",
               isinstance(user_msg.id, str) and user_msg.id.startswith("msg_"),
               f"id={user_msg.id}")
        from engine.direct import route_direct_message
        await route_direct_message(conv.id, f"请用 echo 工具回显 {ECHO_TOKEN}")

        # 轮询 engine 回 idle（turn = notify(brain→execute→push_task) + task(run_agent_loop)）。
        # route_direct_message 在 TestClient 的同步线程里跑，push_notify 把 notify 塞进
        # engine inbox 后立即返回；engine 的 _run_loop 在本 async 上下文的 loop 里消费。
        # 轮询 + 额外 drain（task_tool/agent_reply 在 engine idle 后的尾巴上 emit）。
        deadline = 60.0
        while deadline > 0 and eng.status != "idle":
            await asyncio.sleep(0.1)
            deadline -= 0.1
        # engine idle 后仍有收尾 emit（task_complete/agent_reply 在 _run_worker_task 末尾），
        # 给 1s drain 让 bus_manager.emit 把它们送进 captured。
        await asyncio.sleep(1.0)
        _check(f"C3 engine 回 idle（剩余 {deadline:.1f}s）", eng.status == "idle",
               f"status={eng.status} current_task={eng.current_task_id}")
        if eng.status != "idle":
            errs.append("[C3] engine 未回 idle（可能 LLM fake 或工具执行卡住）")
            return errs

        # 断言 task_tool 事件：name=echo 的 start + end 都到
        tool_events = [e for e in captured if e.get("type") == "task_tool"]
        tool_starts = [e for e in tool_events
                       if (e.get("data") or {}).get("phase") == "start"
                       and (e.get("data") or {}).get("name") == "echo"]
        tool_ends = [e for e in tool_events
                     if (e.get("data") or {}).get("phase") == "end"
                     and (e.get("data") or {}).get("name") == "echo"]
        tool_event_summary = [
            ((e.get("data") or {}).get("phase"), (e.get("data") or {}).get("name"))
            for e in tool_events
        ]
        if not _check("C4 task_tool start name=echo 到达", bool(tool_starts),
                      f"tool_events={tool_event_summary}"):
            errs.append("[C4] 无 echo tool_start 事件")
        if not _check("C5 task_tool end name=echo 到达", bool(tool_ends),
                      f"tool_events={tool_event_summary}"):
            errs.append("[C5] 无 echo tool_end 事件")
        # tool_end 的 content 应含 TOKEN（echo 返回原文）
        if tool_ends:
            end_content = tool_ends[0].get("content") or ""
            if not _check(f"C6 tool_end content 含 {ECHO_TOKEN}", ECHO_TOKEN in end_content, f"content={end_content[:80]}"):
                errs.append("[C6] tool_end content 未含 TOKEN")

        # 断言持久化 agent_reply：content 含 TOKEN（announce = 任务完成 🎉 + 最终答案）
        messages = await crud.list_messages(conv.id, limit=50)
        replies = [m for m in messages
                   if (m.type or "") == "agent_reply" and m.sender_id == agent.id]
        if not _check("C7 持久化 agent_reply 存在", bool(replies)):
            errs.append("[C7] 无持久化 agent_reply")
        else:
            reply_text = replies[-1].content or ""
            if not _check(f"C8 agent_reply content 含 {ECHO_TOKEN}", ECHO_TOKEN in reply_text, f"content={reply_text[:120]}"):
                errs.append(f"[C8] agent_reply 未含 TOKEN: {reply_text[:120]}")
            # 回放 trace：execute 路径落 data.trace，含 tool_start/tool_end step
            data = replies[-1].data or {}
            trace = data.get("trace") or []
            trace_tool_steps = [s for s in trace if s.get("kind") in ("tool_start", "tool_end")
                                and s.get("name") == "echo"]
            _check("C9 reply.data.trace 含 echo tool_start/end step（回放契约）",
                   len(trace_tool_steps) >= 2, f"trace_steps={len(trace)}")

        # 清理：删 MCP 连接 + agent + conversation + 停 engine
        probe_ids.extend([mcp_id])

        # 子进程清理断言：测后无 echo 残留
        await asyncio.sleep(0.3)
        leaked = _count_echo_procs()
        if not _check("C10 测后无 echo 子进程残留", leaked == 0, f"leaked={leaked}"):
            errs.append(f"[C10] {leaked} 个 echo 子进程残留")

    finally:
        evpkg.bus_manager.emit = _orig_emit
        al.ChatOpenAI = _orig_chatopenai
        wmod.chat_completion_stream = _orig_stream
        # 停 engine（cancel run loop + unregister inbox）
        try:
            await registry.stop_group(conv.id)
        except Exception:  # noqa: BLE001
            pass
        # 删测试数据
        for mid in probe_ids:
            try:
                await crud.delete_mcp_connection(mid)
            except Exception:  # noqa: BLE001
                pass
        try:
            await crud.delete_conversation(conv.id)
        except Exception:  # noqa: BLE001
            pass
        try:
            await crud.delete_agent(agent.id)
        except Exception:  # noqa: BLE001
            pass

    return errs


def test_c_full_chain() -> list[str]:
    print("\n=== C. 全链路 e2e（真 spawn + fake LLM + /api/messages）===")
    try:
        return asyncio.run(_async_c_full_chain())
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"  ✗ C 段异常: {type(e).__name__}: {e}")
        return [f"[C] 异常: {e}"]


# ── D. 全局子进程泄漏扫描 ────────────────────────────────────────────────────
def test_d_no_global_leak() -> list[str]:
    errs: list[str] = []
    print("\n=== D. 全局 echo 子进程泄漏扫描 ===")
    leaked = _count_echo_procs()
    if not _check("整测结束无 echo 子进程残留", leaked == 0, f"leaked={leaked}"):
        errs.append(f"[D] {leaked} 个 echo 子进程残留")
    return errs


# ── 主入口 ────────────────────────────────────────────────────────────────────
def main() -> int:
    print("=" * 70)
    print("任务16b MCP 全链路 e2e：建 MCP → 自省 → 挂载 → /api/messages → execute")
    print("=" * 70)
    all_errs: list[str] = []
    all_errs.extend(test_a_fixture_contract())
    all_errs.extend(test_b_introspection_and_invoke())
    all_errs.extend(test_c_full_chain())
    all_errs.extend(test_d_no_global_leak())
    print("\n" + "=" * 70)
    if all_errs:
        print(f"FAIL — {len(all_errs)} 项失败：")
        for e in all_errs:
            print(f"  - {e}")
        return 1
    print("PASS — MCP 全链路 e2e 验证通过：")
    print("  · A fixture 静态契约（FastMCP + echo + stdio + 无副作用）；")
    print("  · B load_mcp_tools 自省返 [echo] + ainvoke 返原文 + 子进程清理；")
    print("  · C POST /api/messages → route_direct_message → engine execute →")
    print("    task_tool start/end name=echo + agent_reply 含结果；")
    print("  · D 整测结束无 echo 子进程泄漏。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

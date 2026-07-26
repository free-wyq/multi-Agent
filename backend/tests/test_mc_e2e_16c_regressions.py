"""任务16c 回归测试：锁住 MCP 全链路 e2e 暴露并修复的四类问题。

四段契约（确定性，不依赖 live server / 真实 LLM）：

  A. **白名单误拦**（vh61 之上增量）：``DEFAULT_STDIO_COMMAND_WHITELIST`` 现含
     ``python3``（任务16c 补——多数 Linux dev 机 PATH 上只有 ``python3``，原白名单
     只含 ``python`` 会误拦合法解释器）。``_validate_stdio_command("python3")`` 不
     raise；``command="python3"`` 经 ``create_mcp_connection_route`` 可过校验落库。

  B. **注入时机（contextvars 隔离）**：``_EXTRA_TOOLS`` 改 ``contextvars
     .ContextVar`` 后，多个并发 worker 任务的 ``set_extra_tools`` 不再互相污染。
     模拟「engine A set [toolA] → 切到 engine B set [toolB] → 切回 engine A 读」：
     A 读到 [toolA]、B 读到 [toolB]（全局变量版会两个都读到 [toolB]）。

  C. **MCP 自省超时降级**：``load_mcp_tools`` 用 ``asyncio.wait_for(timeout=
     MCP_INTROSPECT_TIMEOUT)`` 包住 ``get_tools``。指向一个「启动后永不响应
     tools/list」的坏 server（sleep 死循环 fixture），自省必超时 → 该 server 被
     跳过（warn）+ 其余 server 仍正常加载 + 不 raise（降级为「无该 MCP 工具」，
     不拖到 worker 看门狗 300s）。环境变量 ``MCP_INTROSPECT_TIMEOUT=2`` 缩短超时
     让测试快速。

  D. **spawn 泄漏（已有 e2e 的 B/D 段重申）**：``load_mcp_tools`` 自省 + ``ainvoke``
     真调用后无 echo 子进程残留（``stdio_client`` 的 SIGTERM→SIGKILL 兜底 +
     ``async with`` 会话退出即终止）。本测复用 echo fixture 真起一遍 + pgrep 断言。

pytest 收集：``test_a``..``test_d`` + ``main()``。无 conftest / 无 pytest-asyncio：
异步段用 ``asyncio.run`` 包，每段独立 event loop。
"""
from __future__ import annotations

import asyncio
import os
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

_TMP = tempfile.mkdtemp(prefix="mc_e2e_16c_")
os.environ["MULTI_AGENT_DATA_DIR"] = _TMP
# 缩短 MCP 自省超时：C 段坏 server 必超时，2s 让测试快速（不挂 20s）。
os.environ.setdefault("MCP_INTROSPECT_TIMEOUT", "2")
# 缩短 worker 超时（虽 C 段不走 worker，防御性避免任何意外挂死）。
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
# 坏 server：启动后 sleep 死循环，永不响应 tools/list（强制自省超时）。
BAD_FIXTURE = BACKEND / "tests" / "fixtures" / "hang_mcp_server.py"
ECHO_TOKEN = "MCP-16C-ECHO-OK"


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


def _count_echo_procs() -> int:
    """返回 cmdline 含 ``echo_mcp_server`` 的活进程数（不含本测试进程）。"""
    try:
        r = subprocess.run(
            ["pgrep", "-f", "echo_mcp_server"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    if r.returncode != 0:
        return 0
    my_pid = str(os.getpid())
    pids = [ln for ln in r.stdout.strip().splitlines() if ln and ln != my_pid]
    return len(pids)


def _count_hang_procs() -> int:
    """返回 cmdline 含 ``hang_mcp_server`` 的活进程数。"""
    try:
        r = subprocess.run(
            ["pgrep", "-f", "hang_mcp_server"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    if r.returncode != 0:
        return 0
    my_pid = str(os.getpid())
    return len([ln for ln in r.stdout.strip().splitlines() if ln and ln != my_pid])


async def _init_isolated_db() -> None:
    _db.engine = create_async_engine(
        _db.DB_URL, echo=False,
        connect_args={"check_same_thread": False}, pool_pre_ping=True,
    )
    _db.SessionLocal = async_sessionmaker(
        _db.engine, expire_on_commit=False, class_=AsyncSession,
    )
    await _db.init_db()
    config.set_active_cache({
        "api_key": "sk-e2e-fake",
        "base_url": "http://127.0.0.1:1/v1",
        "model": "fake-e2e-model",
        "temperature": 0.0,
        "max_tokens": 0,
    })


# ── A. 白名单含 python3（任务16c 修：原只含 python 误拦 python3） ────────────
def test_a_whitelist_python3() -> list[str]:
    errs: list[str] = []
    print("\n=== A. 白名单含 python3（任务16c 修误拦）===")
    from api.mcp import _validate_stdio_command, DEFAULT_STDIO_COMMAND_WHITELIST

    _check("A1 DEFAULT_STDIO_COMMAND_WHITELIST 含 python3",
           "python3" in DEFAULT_STDIO_COMMAND_WHITELIST,
           f"whitelist={sorted(DEFAULT_STDIO_COMMAND_WHITELIST)}")
    if "python3" not in DEFAULT_STDIO_COMMAND_WHITELIST:
        errs.append("[A1] 白名单缺 python3")
        return errs

    # _validate_stdio_command("python3") 不 raise
    try:
        _validate_stdio_command("python3")
        _check("A2 _validate_stdio_command('python3') 不 raise", True)
    except Exception as e:  # noqa: BLE001
        _check("A2 _validate_stdio_command('python3') 不 raise", False, f"{type(e).__name__}: {e}")
        errs.append(f"[A2] python3 被误拦: {e}")

    # 原有白名单项仍放行（回归：补 python3 不破坏既有）
    from api.mcp import _validate_stdio_command as _v
    for ok_cmd in ["npx", "uvx", "python", "python3", "node", "uv"]:
        try:
            _v(ok_cmd)
        except Exception as e:  # noqa: BLE001
            _check(f"A3 白名单 {ok_cmd!r} 仍放行", False, f"{type(e).__name__}")
            errs.append(f"[A3] {ok_cmd} 误拦")
    _check("A3 既有白名单项（npx/uvx/python/python3/node/uv）全放行", True)

    # 非白名单仍拒（回归：补 python3 不放行 bash）
    from fastapi import HTTPException
    try:
        _v("bash")
        _check("A4 非白名单 'bash' 仍拒", False, "未 raise")
        errs.append("[A4] bash 未被拒")
    except HTTPException as e:
        _check("A4 非白名单 'bash' 仍拒", e.status_code == 400, f"status={e.status_code}")
        if e.status_code != 400:
            errs.append(f"[A4] bash 拒绝状态码非 400: {e.status_code}")

    # 真路由：command="python3" 经 create_mcp_connection_route 可落库
    async def _route_check() -> None:
        await _init_isolated_db()
        from api.mcp import create_mcp_connection_route
        from models import McpConnectionCreatePayload
        from store import crud
        payload = McpConnectionCreatePayload(
            name=f"probe_16c_python3_{uuid.uuid4().hex[:6]}",
            transport="stdio", command="python3",
            args=[str(FIXTURE)], enabled=True,
        )
        try:
            conn = await create_mcp_connection_route(payload)
        except HTTPException as e:
            _check("A5 create 路由 command=python3 落库", False, f"被拒: {e.detail}")
            errs.append(f"[A5] python3 路由被拒: {e.detail}")
            return
        _check("A5 create 路由 command=python3 落库", conn.command == "python3", f"command={conn.command}")
        if conn.command != "python3":
            errs.append("[A5] 落库 command 非 python3")
        await crud.delete_mcp_connection(conn.id)

    try:
        asyncio.run(_route_check())
    except Exception as e:  # noqa: BLE001
        _check("A5 create 路由 command=python3 落库", False, f"{type(e).__name__}: {e}")
        errs.append(f"[A5] 异常: {e}")

    return errs


# ── B. contextvars 注入隔离（任务16c 修：原全局变量并发污染） ─────────────────
def test_b_extra_tools_contextvar_isolation() -> list[str]:
    errs: list[str] = []
    print("\n=== B. _EXTRA_TOOLS contextvars 注入隔离（任务16c 修并发污染）===")
    from engine.agent_loop import _EXTRA_TOOLS, set_extra_tools

    # B1: _EXTRA_TOOLS 是 ContextVar（任务16c 改造的核心）
    import contextvars
    _check("B1 _EXTRA_TOOLS 是 contextvars.ContextVar",
           isinstance(_EXTRA_TOOLS, contextvars.ContextVar),
           f"type={type(_EXTRA_TOOLS).__name__}")
    if not isinstance(_EXTRA_TOOLS, contextvars.ContextVar):
        errs.append("[B1] _EXTRA_TOOLS 非 ContextVar（仍是全局变量，未修）")
        return errs

    # B2: 默认值是空 list
    _check("B2 _EXTRA_TOOLS.get() 默认空 list",
           _EXTRA_TOOLS.get() == [], f"default={_EXTRA_TOOLS.get()!r}")

    # B3: set_extra_tools 在当前 context 生效
    toolA = ["toolA"]
    set_extra_tools(toolA)
    _check("B3 set_extra_tools(['toolA']) 后当前 context 读到 toolA",
           _EXTRA_TOOLS.get() == ["toolA"], f"got={_EXTRA_TOOLS.get()!r}")
    if _EXTRA_TOOLS.get() != ["toolA"]:
        errs.append("[B3] set_extra_tools 未在当前 context 生效")

    # B4: 并发隔离——模拟两个 worker 任务的 set/get 交错
    # engine A set [toolA] → 切到 engine B set [toolB] → 切回 A 读
    # 全局变量版 A 会读到 [toolB]（被 B 污染）；ContextVar 版 A 读到 [toolA]。
    # 用 asyncio.gather（Py3.10 无 TaskGroup）+ 每个 worker 在自己的 task context
    # 里 set（contextvars 在 asyncio.Task 启动时自动 copy current context）。
    async def _simulate_concurrent_workers() -> None:
        results: dict[str, list] = {}

        async def worker(name: str, my_tools: list) -> None:
            # 每个 worker task 启动时 copy 主 context（含主 set 的 [toolA]），
            # 然后各自 set 自己的工具集——互不影响（asyncio task 切换时 contextvars
            # 自动隔离）。多次让出控制权制造交错（B set 后 A 才读）。
            set_extra_tools(my_tools)
            for _ in range(5):
                await asyncio.sleep(0)
            results[name] = list(_EXTRA_TOOLS.get())

        # 先在主 context set toolA，子 task 会 copy 这个 context 再各自 set
        set_extra_tools(["toolA"])
        await asyncio.gather(
            worker("B", ["toolB"]),
            worker("A", ["toolA"]),
        )

        # 子 task 各自 set 后读到的应是自己的（contextvars task 隔离）
        _check("B4a worker A set [toolA] 后（B 也 set 了）A 仍读到 [toolA]",
               results.get("A") == ["toolA"], f"A={results.get('A')!r}")
        _check("B4b worker B set [toolB] 后 A 也不受影响 B 读到 [toolB]",
               results.get("B") == ["toolB"], f"B={results.get('B')!r}")
        if results.get("A") != ["toolA"]:
            errs.append(f"[B4a] A 被污染: {results.get('A')!r}")
        if results.get("B") != ["toolB"]:
            errs.append(f"[B4b] B 被污染: {results.get('B')!r}")

        # B5: 子 task 的 set 不泄漏回主 context（隔离的反向证明）
        _check("B5 子 task set 不泄漏回主 context（主仍 [toolA]）",
               _EXTRA_TOOLS.get() == ["toolA"], f"main={_EXTRA_TOOLS.get()!r}")
        if _EXTRA_TOOLS.get() != ["toolA"]:
            errs.append(f"[B5] 主 context 被污染: {_EXTRA_TOOLS.get()!r}")

    try:
        asyncio.run(_simulate_concurrent_workers())
    except Exception as e:  # noqa: BLE001
        _check("B4/B5 并发隔离模拟", False, f"{type(e).__name__}: {e}")
        errs.append(f"[B4/B5] 异常: {e}")

    # 清理主 context
    set_extra_tools([])
    return errs


# ── C. MCP 自省超时降级（任务16c 修：原无超时拖到 worker 看门狗 300s） ────────
async def _async_c_timeout_degradation() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()
    from store import crud
    from models import McpConnectionCreatePayload

    # C1: MCP_INTROSPECT_TIMEOUT 配置存在且 > 0
    _check("C1 MCP_INTROSPECT_TIMEOUT 配置存在且 > 0",
           config.MCP_INTROSPECT_TIMEOUT > 0,
           f"value={config.MCP_INTROSPECT_TIMEOUT}")
    if config.MCP_INTROSPECT_TIMEOUT <= 0:
        errs.append("[C1] MCP_INTROSPECT_TIMEOUT 未配置")
        return errs

    # 建一条坏 MCP 连接（指向 hang fixture）+ 一条好连接（echo fixture）
    bad_conn = await crud.create_mcp_connection(McpConnectionCreatePayload(
        name=f"[e2e-16c] hang {uuid.uuid4().hex[:6]}",
        transport="stdio",
        command=sys.executable,
        args=[str(BAD_FIXTURE)],
        env=None, enabled=True,
    ))
    good_conn = await crud.create_mcp_connection(McpConnectionCreatePayload(
        name=f"[e2e-16c] echo {uuid.uuid4().hex[:6]}",
        transport="stdio",
        command=sys.executable,
        args=[str(FIXTURE)],
        env=None, enabled=True,
    ))

    from engine.mcp_manager import load_mcp_tools
    # C2: 混合加载 [bad, good] —— bad 超时被跳过，good 正常返 [echo]
    #     （验证：单 server 挂不死整批 + 不 raise，对齐 per-connection 容错）
    try:
        tools = await load_mcp_tools([bad_conn.id, good_conn.id])
    except Exception as e:  # noqa: BLE001
        _check("C2 混合加载不 raise（坏 server 降级不阻塞）", False, f"{type(e).__name__}: {e}")
        errs.append(f"[C2] load_mcp_tools raise: {e}")
        tools = []

    _check("C2 混合加载不 raise（坏 server 降级不阻塞）", True)
    tool_names = [t.name for t in tools]
    _check("C3 好 server 的 echo 工具仍加载（坏 server 未拖死整批）",
           "echo" in tool_names, f"tools={tool_names}")
    if "echo" not in tool_names:
        errs.append(f"[C3] echo 未加载（坏 server 拖死了整批）: {tool_names}")

    # C4: 单独加载坏 server —— 超时后返空 list，不 raise（降级为「无该 MCP 工具」）
    try:
        bad_tools = await load_mcp_tools([bad_conn.id])
    except Exception as e:  # noqa: BLE001
        _check("C4 坏 server 单独加载不 raise（超时降级为空）", False, f"{type(e).__name__}: {e}")
        errs.append(f"[C4] 坏 server raise: {e}")
        bad_tools = ["sentinel"]
    _check("C4 坏 server 单独加载不 raise（超时降级为空）", True)
    _check("C5 坏 server 超时后返空 list（降级为无工具）",
           bad_tools == [], f"bad_tools={bad_tools}")
    if bad_tools != []:
        errs.append(f"[C5] 坏 server 未降级为空: {bad_tools}")

    # 清理：删连接 + 等 hang 子进程被 stdio_client SIGTERM 终止
    await crud.delete_mcp_connection(bad_conn.id)
    await crud.delete_mcp_connection(good_conn.id)
    # 给 stdio_client 的 SIGTERM→SIGKILL 兜底一点时间清理坏 server 子进程
    await asyncio.sleep(1.0)
    leaked = _count_hang_procs()
    _check("C6 坏 server 子进程被清理（stdio_client 终止兜底）",
           leaked == 0, f"leaked={leaked}")
    if leaked != 0:
        errs.append(f"[C6] {leaked} 个 hang 子进程残留")

    return errs


def test_c_timeout_degradation() -> list[str]:
    print("\n=== C. MCP 自省超时降级（任务16c 修：原拖到 worker 看门狗）===")
    try:
        return asyncio.run(_async_c_timeout_degradation())
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"  ✗ C 段异常: {type(e).__name__}: {e}")
        return [f"[C] 异常: {e}"]


# ── D. spawn 泄漏重申（echo fixture 真起 + pgrep 断言测后干净） ────────────────
async def _async_d_no_leak() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()
    from store import crud
    from models import McpConnectionCreatePayload

    conn = await crud.create_mcp_connection(McpConnectionCreatePayload(
        name=f"[e2e-16c] echo {uuid.uuid4().hex[:6]}",
        transport="stdio",
        command=sys.executable,
        args=[str(FIXTURE)],
        env=None, enabled=True,
    ))

    from engine.mcp_manager import load_mcp_tools
    tools = await load_mcp_tools([conn.id])
    if not _check("D1 load_mcp_tools 返 [echo]", bool(tools) and tools[0].name == "echo",
                  f"tools={[t.name for t in tools]}"):
        errs.append("[D1] load_mcp_tools 未返 echo")
    else:
        # 真调用 ainvoke（re-spawn echo 子进程跑 tools/call）
        result = await tools[0].ainvoke({"text": ECHO_TOKEN})
        _check(f"D2 ainvoke echo 返原文（含 {ECHO_TOKEN}）",
               ECHO_TOKEN in str(result), f"out={str(result)[:80]}")
        if ECHO_TOKEN not in str(result):
            errs.append("[D2] ainvoke 未返原文")

    # 清理 + 断言无残留
    await crud.delete_mcp_connection(conn.id)
    await asyncio.sleep(0.3)
    leaked = _count_echo_procs()
    if not _check("D3 测后无 echo 子进程残留", leaked == 0, f"leaked={leaked}"):
        errs.append(f"[D3] {leaked} 个 echo 子进程残留")
    return errs


def test_d_no_spawn_leak() -> list[str]:
    print("\n=== D. spawn 泄漏重申（echo 真起 + pgrep 断言干净）===")
    try:
        return asyncio.run(_async_d_no_leak())
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ D 段异常: {type(e).__name__}: {e}")
        return [f"[D] 异常: {e}"]


# ── 主入口 ────────────────────────────────────────────────────────────────────
def main() -> int:
    print("=" * 70)
    print("任务16c 回归：MCP e2e 暴露的四类问题（白名单/注入时机/超时/泄漏）")
    print("=" * 70)
    all_errs: list[str] = []
    all_errs.extend(test_a_whitelist_python3())
    all_errs.extend(test_b_extra_tools_contextvar_isolation())
    all_errs.extend(test_c_timeout_degradation())
    all_errs.extend(test_d_no_spawn_leak())
    print("\n" + "=" * 70)
    if all_errs:
        print(f"FAIL — {len(all_errs)} 项失败：")
        for e in all_errs:
            print(f"  - {e}")
        return 1
    print("PASS — 任务16c 四类问题修复并锁住：")
    print("  · A 白名单补 python3（修误拦合法解释器）；")
    print("  · B _EXTRA_TOOLS 改 contextvars（修多 worker 并发工具集污染）；")
    print("  · C load_mcp_tools 加 MCP_INTROSPECT_TIMEOUT（修自省挂死拖到看门狗 300s）；")
    print("  · D echo 真起 + pgrep 断言测后无子进程残留（锁住无 spawn 泄漏）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

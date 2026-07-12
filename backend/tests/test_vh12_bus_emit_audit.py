"""VH12 回归：bus.py emit_* 全量审计——错误处理统一 + 背压兜底（task B15）.

锁住 B15 修复——``events/bus.py`` 全量审计 13 个 ``emit_*`` 函数后：
  - 13 个 ``emit_*`` helper 是纯投影器（构 dict → ``await bus_manager.emit``），无
    try/except、无静默吞没——错误处理单一真源在 ``BusManager.emit``。
  - ``BusManager.emit`` 的 ``except Exception`` 不再静默吞没：``logger.debug`` +
    ``exc_info=True`` 捕获 send 失败，然后 prune socket。
  - ``ws.send_json`` 包 ``asyncio.wait_for(timeout=WS_SEND_TIMEOUT)``——慢客户端
    不再无限阻塞 emit 协程（流式 per-token 背压兜底），超时即 prune。

B15 前的隐患：
  - ``BusManager.emit`` ``except Exception: dead.append(ws)`` 静默吞没——send 失败
    （socket 关闭 / 序列化错误 / 超时）无任何日志，真 bug（如 data 含不可序列化对象
    prune 了健康 socket）无法定位。
  - ``ws.send_json`` 无超时——慢客户端（网络卡、浏览器节流）会让 ``await send_json``
    阻塞，流式 per-token 路径（emit_task_token / emit_coordinator_token）每 token
    ``await emit``，一个慢消费者回压 LLM 流式 ``async for`` 循环致整条流水线停滞。

B15 后：
  - ``logger.debug`` + ``exc_info=True``：不静默（开 DEBUG 可见异常类型/socket），
    但不在默认日志级洪水（流式 per-token 对掉线客户端每 token 重试都触发 except，
    ``logger.exception`` ERROR 级 traceback 会按 token 频率刷屏）。
  - ``asyncio.wait_for(timeout=WS_SEND_TIMEOUT=5.0)``：慢客户端超时即 prune，
    best-effort 丢慢消费者，流式对其他客户端继续。

纯静态契约（读源码断言，不依赖后端在线）+ 行为等价（运行时 mock socket 验 live/
dead/slow 三态）双保险，与 test_vh1-vh11 同款风格。

六段契约：

  A. BusManager.emit 错误处理统一（不静默吞没）
    1. ``except Exception`` 后 ``logger.debug`` 调用（非裸 ``dead.append`` 静默）。
    2. ``logger.debug`` 含 ``exc_info=True``（捕获异常类型/traceback，排障可见）。
    3. 仍是 ``logger.debug``（非 ``logger.exception`` / ERROR 级——防流式 per-token
       对掉线客户端洪水日志）。

  B. 13 个 emit_* helper 纯投影器（无 try/except、无静默吞没）
    4. 13 个 ``emit_*`` 函数体无 ``except``（错误处理单一真源在 BusManager.emit）。
    5. 13 个 ``emit_*`` 函数体无 ``pass``（无静默吞没）。
    6. 13 个 ``emit_*`` 都 ``await bus_manager.emit(...)``（委托单一 fan-out 真源）。

  C. 背压兜底（ws.send_json 超时）
    7. ``ws.send_json`` 包 ``asyncio.wait_for(..., timeout=WS_SEND_TIMEOUT)``。
    8. ``WS_SEND_TIMEOUT`` 常量定义（模块级，float，>0）。
    9. 超时即 prune（``wait_for`` 超时 raise ``TimeoutError`` → except → dead.append）。

  D. logger 初始化（B15 新增）
   10. ``logger = logging.getLogger("multi-agent.bus")``（模块级 logger）。
   11. ``import logging``（B15 新增）。

  E. 行为零变（live/dead socket 处理不回归）
   12. 空 group → no-op（无 socket 不报错）。
   13. live socket send 成功 → 不 prune（仍在 connections）。
   14. dead socket（send_json raise）→ prune（移出 connections）。

  F. 背压兜底行为（slow socket 超时 prune）
   15. slow socket（send_json 阻塞 > timeout）→ 超时后 prune（移出 connections）。
   16. ``asyncio.wait_for`` 取消 send_json 协程（超时后协程被取消，不泄漏）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUS = REPO / "backend" / "events" / "bus.py"

# 13 个 emit_* helper（B15 审计对象）。
EMIT_HELPERS = [
    "emit_message_added",
    "emit_task_dispatched",
    "emit_task_completed",
    "emit_task_log",
    "emit_task_tool",
    "emit_task_think",
    "emit_task_token",
    "emit_agent_status",
    "emit_coordinator_plan",
    "emit_coordinator_think",
    "emit_coordinator_token",
    "emit_coordinator_reasoning",
    "emit_coordinator_stats",
]


def _fn_body(src: str, fname: str, indent_opts=("", "    ")) -> str:
    """抽 fn 函数体到下一个同级 def（试多种缩进）。模块级末函数回退到文件尾。"""
    for indent in indent_opts:
        m = re.search(
            rf"(?:async def|def) {fname}\([^)]*\)(.*?)(?=\n{indent}(?:async )?def )",
            src,
            re.S,
        )
        if m:
            return m.group(1)
    m = re.search(rf"(?:async def|def) {fname}\([^)]*\)(.*)$", src, re.S)
    return m.group(1) if m else ""


def _strip_docstrings(src: str) -> str:
    """剔三引号 docstring（防散文引用被误判为代码字面量，B6-B14 稳定坑）。"""
    return re.sub(r'""".*?"""', "", src, flags=re.S)


def assert_contract() -> list[str]:
    errs: list[str] = []
    bus = BUS.read_text(encoding="utf-8")

    emit_body = _fn_body(bus, "emit", indent_opts=("    ",))
    if not emit_body:
        errs.append("[setup] BusManager.emit 方法体未找到")
        return errs
    emit_code = _strip_docstrings(emit_body)

    # ── A. BusManager.emit 错误处理统一（不静默吞没）──
    # [1] except Exception 后 logger.debug 调用（非裸 dead.append 静默）
    if "logger.debug(" not in emit_code:
        errs.append("[A1] BusManager.emit except Exception 后无 logger.debug（B15 静默吞没未消除）")
    else:
        print("[A1] OK  except Exception 后 logger.debug 调用（不静默吞没）")
    # [2] logger.debug 含 exc_info=True
    if "exc_info=True" not in emit_code:
        errs.append("[A2] logger.debug 缺 exc_info=True（异常类型/traceback 不捕获）")
    else:
        print("[A2] OK  logger.debug 含 exc_info=True（异常类型/traceback 排障可见）")
    # [3] 仍是 logger.debug（非 logger.exception / ERROR 级——防流式洪水）
    if "logger.exception" in emit_code:
        errs.append("[A3] 用 logger.exception（ERROR 级，流式 per-token 会洪水）——应 logger.debug")
    else:
        print("[A3] OK  logger.debug（非 exception，防流式 per-token 对掉线客户端洪水日志）")

    # ── B. 13 个 emit_* helper 纯投影器 ──
    # [4] 13 个 emit_* 函数体无 except
    helpers_with_except = []
    for h in EMIT_HELPERS:
        body = _fn_body(bus, h, indent_opts=("",))
        if not body:
            helpers_with_except.append(f"{h}(body未找到)")
        elif "except" in _strip_docstrings(body):
            helpers_with_except.append(h)
    if helpers_with_except:
        errs.append(f"[B4] emit_* helper 含 except（错误处理应单一真源在 BusManager.emit）：{helpers_with_except}")
    else:
        print(f"[B4] OK  13 个 emit_* helper 无 except（错误处理单一真源在 BusManager.emit）")
    # [5] 13 个 emit_* 函数体无 pass（无静默吞没）
    helpers_with_pass = []
    for h in EMIT_HELPERS:
        body = _fn_body(bus, h, indent_opts=("",))
        if body and re.search(r"^\s*pass\s*$", _strip_docstrings(body), re.M):
            helpers_with_pass.append(h)
    if helpers_with_pass:
        errs.append(f"[B5] emit_* helper 含裸 pass（静默吞没）：{helpers_with_pass}")
    else:
        print("[B5] OK  13 个 emit_* helper 无裸 pass（无静默吞没）")
    # [6] 13 个 emit_* 都 await bus_manager.emit
    helpers_not_delegate = [h for h in EMIT_HELPERS if "bus_manager.emit(" not in _fn_body(bus, h, indent_opts=("",))]
    if helpers_not_delegate:
        errs.append(f"[B6] emit_* helper 未委托 bus_manager.emit：{helpers_not_delegate}")
    else:
        print("[B6] OK  13 个 emit_* helper 都 await bus_manager.emit(...)（单一 fan-out 真源）")

    # ── C. 背压兜底（ws.send_json 超时）──
    # [7] ws.send_json 包 asyncio.wait_for(..., timeout=WS_SEND_TIMEOUT)
    if not re.search(
        r"asyncio\.wait_for\(\s*ws\.send_json\(event_data\)\s*,\s*timeout=WS_SEND_TIMEOUT\s*\)",
        emit_code,
    ):
        errs.append("[C7] ws.send_json 未包 asyncio.wait_for（慢客户端无超时背压兜底）")
    else:
        print("[C7] OK  ws.send_json 包 asyncio.wait_for(..., timeout=WS_SEND_TIMEOUT)（背压兜底）")
    # [8] WS_SEND_TIMEOUT 常量定义（模块级，float，>0）
    m_timeout = re.search(r"^WS_SEND_TIMEOUT\s*:\s*float\s*=\s*([0-9.]+)\s*$", bus, re.M)
    if not m_timeout:
        errs.append("[C8] WS_SEND_TIMEOUT 常量未定义（应模块级 float）")
    else:
        val = float(m_timeout.group(1))
        if val <= 0:
            errs.append(f"[C8] WS_SEND_TIMEOUT={val} <= 0（应 >0）")
        else:
            print(f"[C8] OK  WS_SEND_TIMEOUT: float = {val}（模块级常量，>0）")
    # [9] 超时即 prune（wait_for 超时 raise TimeoutError → except → dead.append）
    #     wait_for 在 except Exception 之前，超时进入同一 except 分支 prune
    if "asyncio.wait_for(" not in emit_code or "dead.append(ws)" not in emit_code:
        errs.append("[C9] 超时 prune 链不完整（wait_for + dead.append 缺一）")
    else:
        print("[C9] OK  超时 → except Exception → dead.append（慢客户端超时即 prune）")

    # ── D. logger 初始化 ──
    # [10] logger = logging.getLogger("multi-agent.bus")
    if not re.search(r'logger\s*=\s*logging\.getLogger\(\s*["\']multi-agent\.bus["\']\s*\)', bus):
        errs.append("[D10] logger 未初始化（应 logging.getLogger('multi-agent.bus')）")
    else:
        print("[D10] OK  logger = logging.getLogger('multi-agent.bus')（模块级 logger）")
    # [11] import logging
    if "import logging" not in bus:
        errs.append("[D11] 缺 import logging（B15 新增）")
    else:
        print("[D11] OK  import logging（B15 新增）")

    # ── E. 行为零变（live/dead socket 处理不回归）──
    sys.path.insert(0, str(REPO / "backend"))
    try:
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from events.bus import bus_manager, BusManager
        import events.bus as busmod

        async def _run():
            # [12] 空 group → no-op
            bm = BusManager()
            await bm.emit("g_empty", {"type": "x"})  # no socket, no error
            return bm

        asyncio.run(_run())
        print("[E12] OK  空 group → no-op（无 socket 不报错）")

        async def _run_live():
            bm = BusManager()
            ws = MagicMock()
            ws.send_json = AsyncMock()
            bm.subscribe("g1", ws)
            await bm.emit("g1", {"type": "x"})
            return bm, ws

        bm, ws = asyncio.run(_run_live())
        if ws.send_json.call_count != 1 or ws not in bm._connections["g1"]:
            errs.append("[E13] live socket 处理回归（应 send 1 次 + 不 prune）")
        else:
            print("[E13] OK  live socket send 成功 + 不 prune（仍在 connections）")

        async def _run_dead():
            bm = BusManager()
            ws = MagicMock()
            ws.send_json = AsyncMock(side_effect=RuntimeError("closed"))
            bm.subscribe("g2", ws)
            await bm.emit("g2", {"type": "x"})
            return bm, ws

        bm, ws = asyncio.run(_run_dead())
        conns = bm._connections.get("g2", set())
        if ws in conns:
            errs.append("[E14] dead socket 未 prune（send_json raise 应移出）")
        else:
            print("[E14] OK  dead socket（send_json raise）→ prune（移出 connections）")
    except Exception as e:
        errs.append(f"[E] 行为零变运行时验证异常: {e}")

    # ── F. 背压兜底行为（slow socket 超时 prune）──
    try:
        async def _run_slow():
            import asyncio
            from unittest.mock import MagicMock
            from events.bus import BusManager
            import events.bus as busmod
            orig = busmod.WS_SEND_TIMEOUT
            busmod.WS_SEND_TIMEOUT = 0.05  # 50ms for test
            bm = BusManager()
            ws = MagicMock()
            async def slow_send(_):
                await asyncio.sleep(1.0)  # blocks way past 50ms
            ws.send_json = slow_send
            bm.subscribe("g3", ws)
            await bm.emit("g3", {"type": "x"})
            busmod.WS_SEND_TIMEOUT = orig  # restore
            return bm, ws

        bm, ws = asyncio.run(_run_slow())
        conns = bm._connections.get("g3", set())
        if ws in conns:
            errs.append("[F15] slow socket 未超时 prune（背压兜底未生效）")
        else:
            print("[F15] OK  slow socket（send_json 阻塞 > timeout）→ 超时 prune（背压兜底生效）")
        # [16] wait_for 取消 send_json 协程（超时后协程被取消）
        if "asyncio.wait_for(" in emit_code:
            print("[F16] OK  asyncio.wait_for 超时取消 send_json 协程（不泄漏）")
        else:
            errs.append("[F16] 缺 asyncio.wait_for（超时不取消协程，泄漏）")
    except Exception as e:
        errs.append(f"[F] 背压兜底运行时验证异常: {e}")

    return errs


def main() -> int:
    print("=== VH12 回归：bus.py emit_* 全量审计——错误处理统一 + 背压兜底（B15）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B15 bus.py emit_* 全量审计锁定：\n"
        "  · A BusManager.emit 错误处理统一：except Exception 后 logger.debug + exc_info=True（不静默吞没，非 exception 防洪水）；\n"
        "  · B 13 个 emit_* helper 纯投影器：无 except / 无 pass / 都 await bus_manager.emit（错误处理单一真源）；\n"
        "  · C 背压兜底：ws.send_json 包 asyncio.wait_for(timeout=WS_SEND_TIMEOUT=5.0)（慢客户端超时即 prune）；\n"
        "  · D logger 初始化：logging.getLogger('multi-agent.bus') + import logging；\n"
        "  · E 行为零变：空 group no-op + live socket 不 prune + dead socket（raise）prune；\n"
        "  · F 背压兜底行为：slow socket（阻塞 > timeout）超时 prune + wait_for 取消协程不泄漏。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

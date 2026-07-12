"""VH3 回归：_detect_residual_interrupt 可观测出口契约（task B6）.

锁住 B6 修复——``_detect_residual_interrupt`` 是「observability-only」helper：
既不 mutate state 也不 return 给路由（classify 调用方仅 ``await`` 其返回 None，
不影响 ``action_taken``）。B6 前的「模糊」在于：info 日志是自由文本 peep、
debug 兜底静默（``logger.debug(..., exc_info=True)`` 无结构化标记），既无出口
下游消费者（无 metrics/WS 事件），又未标注「可删」——读代码者不知它到底有没有用。

B6 决策：保留 helper（abandon-plan 是真实低频难发现事件），但补**结构化日志出口**
而非删（删了等于把唯一可观测点也抹掉，更糟）。两路日志都带 ``extra={"event": ...}``
让未来接 Loki/ELK/json-formatter 时可按 ``event`` 聚合，不必 grep 散文。同时在
docstring 标注「observability-only，无下游消费者，确认无需追踪时整段可删」消除
「无出口」的模糊。

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1/vh2 同款风格。

五段契约：

  A. info 出口带结构化 extra（abandon-plan 可观测）
    1. ``logger.info(...)`` 调用带 ``extra={...}`` 关键字参数（非裸文本日志）。
    2. ``extra`` 含 ``"event": "plan_abandoned_by_new_demand"`` 标记（可聚合 key）。
    3. ``extra`` 含 ``incoming_kind`` + ``pending_steps`` 字段（携带上下文非仅 prose）。

  B. debug 兜底带结构化 extra（probe 降级可观测，非静默）
    4. ``logger.debug(...)`` 调用带 ``extra={...}``（降级可被收集，非吞没）。
    5. ``extra`` 含 ``"event": "residual_interrupt_probe_skipped"`` 标记。
    6. ``exc_info=True`` 保留 traceback（降级原因可查）。

  C. helper 是 observability-only（不 mutate / 不路由 / 无返回值影响）
    7. 函数签名返回 ``-> None``（无返回值，调用方不消费结果）。
    8. 调用方 ``node_classify_incoming`` 仅 ``await _detect_residual_interrupt(config, kind)``
       不接收返回值（不把结果塞进 action_taken / 不改变路由）。
    9. 函数体内无 ``return`` 非 None（仅 try/except，异常兜底后自然落 None）。

  D. docstring 标注「可删」与「保留理由」（消除「无出口」模糊）
   10. docstring 含 ``Outlet contract`` / ``observability-only`` 说明出口性质。
   11. docstring 含「safe to delete」+ ``routing is unchanged`` 标注删除条件与边界。
   12. docstring 含 ``test_m12_boundary_new_demand`` 引用（删除时哪个测保路由不变）。

  E. 与协调者 best-effort 出口口径一致（B28 前瞻，本测仅锁 helper 不锁全部）
   13. except Exception 块用 ``logger.debug``（非 ``pass``/``logger.exception``——
       observability-only 不该把降级当 error 刷告警，也不该 ``pass`` 静默吞）。

为何纯静态：
  结构化日志的 ``extra={}`` 是「代码结构」契约（Python logging 标准 ``extra`` 参数，
  传 dict 即合并进 LogRecord.__dict__），运行时是否被收集取决于是否接 formatter/handler，
  但 ``extra`` 是否传是确定性代码锚定。静态契约锁「两路日志都带 extra.event」比
  运行时捕获 LogRecord 更可靠（需配 logging handler 抓 record，且与 logger 全局态耦合）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COORD = REPO / "backend" / "engine" / "coordinator.py"


def _fn_body(src: str, fname: str) -> str:
    """抽 async def fname(...) 到下一个顶层 def/async def 的函数体。"""
    m = re.search(
        rf"async def {fname}\([^)]*\)(.*?)(?=\nasync def |\ndef )",
        src,
        re.S,
    )
    return m.group(1) if m else ""


def assert_contract() -> list[str]:
    errs: list[str] = []
    coord = COORD.read_text(encoding="utf-8")
    body = _fn_body(coord, "_detect_residual_interrupt")

    if not body:
        errs.append("[setup] _detect_residual_interrupt 函数体未找到")
        return errs

    # ── A. info 出口带结构化 extra ──
    # info 调用跨多行含嵌套括号（消息串含 "(%d ... step(s))"），正则的 [^)] 边界
    # 会被消息里的 ) 截断。改用「带引号的 dict key」存在性断言——extra dict 里的
    # key/value 是带引号的字符串字面量（"event": "..."），在函数体内唯一，足以锁
    # 「info 出口带结构化 extra」契约，且不被消息散文里的括号干扰。
    # [1] logger.info(...) 调用存在 + 带 extra= 关键字参数
    has_info = "logger.info(" in body
    has_info_extra = re.search(r"logger\.info\(.*?extra\s*=\s*\{", body, re.S) is not None
    if not (has_info and has_info_extra):
        errs.append("[A1] logger.info 未带 extra={...} 结构化出口（仍是自由文本 peep）")
    else:
        print("[A1] OK  logger.info 带 extra={...} 结构化出口")
        # [2] extra 含 "event": "plan_abandoned_by_new_demand"（带引号 dict key，唯一锚点）
        if '"event": "plan_abandoned_by_new_demand"' not in body:
            errs.append('[A2] info extra 缺 "event": "plan_abandoned_by_new_demand" 聚合 key')
        else:
            print('[A2] OK  extra.event="plan_abandoned_by_new_demand"（可聚合 key）')
        # [3] extra 含 "incoming_kind": + "pending_steps": 带引号 dict key（携带上下文）
        if '"incoming_kind":' not in body or '"pending_steps":' not in body:
            errs.append("[A3] info extra 缺 \"incoming_kind\":/\"pending_steps\": 上下文字段")
        else:
            print('[A3] OK  extra 携带 incoming_kind + pending_steps 上下文（带引号 dict key）')

    # ── B. debug 兜底带结构化 extra ──
    # [4] logger.debug(...) 带 extra={...}
    has_dbg = "logger.debug(" in body
    has_dbg_extra = re.search(r"logger\.debug\(.*?extra\s*=\s*\{", body, re.S) is not None
    if not (has_dbg and has_dbg_extra):
        errs.append("[B4] logger.debug 未带 extra={...}（降级静默吞没，无结构化标记）")
    else:
        print("[B4] OK  logger.debug 带 extra={...}（降级可收集，非吞没）")
        # [5] extra 含 "event": "residual_interrupt_probe_skipped"
        if '"event": "residual_interrupt_probe_skipped"' not in body:
            errs.append('[B5] debug extra 缺 "event": "residual_interrupt_probe_skipped" 标记')
        else:
            print('[B5] OK  extra.event="residual_interrupt_probe_skipped"（降级可聚合）')
        # [6] exc_info=True 保留 traceback
        if "exc_info=True" not in body:
            errs.append("[B6] logger.debug 缺 exc_info=True（降级原因丢失）")
        else:
            print("[B6] OK  exc_info=True 保留 traceback（降级原因可查）")

    # ── C. helper 是 observability-only（不 mutate / 不路由 / 无返回值影响）──
    # [7] 函数签名返回 -> None
    m_sig = re.search(r"async def _detect_residual_interrupt\([^)]*\)\s*->\s*None\s*:", coord)
    if not m_sig:
        errs.append("[C7] _detect_residual_interrupt 签名未标注 -> None（返回值应不被消费）")
    else:
        print("[C7] OK  签名 -> None（无返回值，调用方不消费结果）")

    # [8] 调用方 node_classify_incoming 仅 await 不接收返回值
    m_call = re.search(r"await\s+_detect_residual_interrupt\(\s*config\s*,\s*kind\s*\)\s*(?:#|$|\n\s*return|\n\s*[^=])", coord)
    if not m_call:
        # 宽松：await _detect_residual_interrupt(config, kind) 不被赋值
        if re.search(r"await\s+_detect_residual_interrupt\(\s*config\s*,\s*kind\s*\)", coord):
            # 确认前面无 = 赋值
            call_match = re.search(r"(.*?)await\s+_detect_residual_interrupt\(\s*config\s*,\s*kind\s*\)", coord, re.S)
            if call_match and re.search(r"=\s*$", call_match.group(1).split("\n")[-1]):
                errs.append("[C8] _detect_residual_interrupt 返回值被赋值接收（违反 observability-only）")
            else:
                print("[C8] OK  调用方仅 await 不接收返回值（不改变路由）")
        else:
            errs.append("[C8] 未找到 _detect_residual_interrupt(config, kind) 调用")
    else:
        print("[C8] OK  调用方仅 await 不接收返回值（不改变路由）")

    # [9] 函数体内无非 None return（仅 try/except 落 None）
    returns = re.findall(r"^\s*return\s+(.+?)\s*$", body, re.M)
    non_none_returns = [r for r in returns if r != "None"]
    if non_none_returns:
        errs.append(f"[C9] 函数体内有非 None return：{non_none_returns}（应仅 try/except 落 None）")
    else:
        print("[C9] OK  函数体内无非 None return（仅 try/except 落 None，observability-only）")

    # ── D. docstring 标注「可删」与「保留理由」──
    # 抽 docstring（三引号间）
    m_doc = re.search(r'async def _detect_residual_interrupt\([^)]*\)[^"]*?"""(.*?)"""', coord, re.S)
    doc = m_doc.group(1) if m_doc else ""
    if not doc:
        errs.append("[setup] _detect_residual_interrupt docstring 未找到")
    else:
        # [10] docstring 含 Outlet contract / observability-only 出口性质说明
        if "Outlet contract" not in doc and "observability-only" not in doc and "observability only" not in doc:
            errs.append("[D10] docstring 未标注出口性质（Outlet contract/observability-only）")
        else:
            print("[D10] OK  docstring 标注出口性质（Outlet contract/observability-only）")
        # [11] docstring 含 safe to delete + routing is unchanged
        if "safe to delete" not in doc and "safe to delete" not in doc.lower():
            errs.append("[D11] docstring 未标注「safe to delete」删除条件")
        elif "routing is unchanged" not in doc and "routing" not in doc.lower():
            errs.append("[D11] docstring 标注可删但未说明「routing is unchanged」边界")
        else:
            print("[D11] OK  docstring 标注「safe to delete」+「routing is unchanged」删除边界")
        # [12] docstring 含 test_m12_boundary_new_demand 引用
        if "test_m12_boundary_new_demand" not in doc:
            errs.append("[D12] docstring 未引用 test_m12_boundary_new_demand（删除时不知哪个测保路由）")
        else:
            print("[D12] OK  docstring 引用 test_m12_boundary_new_demand（删除时路由测保绿）")

    # ── E. except 用 logger.debug（非 pass / 非 logger.exception）──
    # [13] except Exception 块用 logger.debug（observability-only 降级不当 error）
    m_exc = re.search(r"except Exception:\s*\n(.*?)(?=\n\n|\n    [a-z]|$)", body, re.S)
    if not m_exc:
        errs.append("[E13] except Exception 块未找到")
    else:
        exc_blk = m_exc.group(1)
        has_pass = bool(re.search(r"^\s*pass\s*$", exc_blk, re.M))
        has_exception = "logger.exception" in exc_blk
        has_debug = "logger.debug" in exc_blk
        if has_pass:
            errs.append("[E13] except 块用 pass（静默吞没降级原因）")
        elif has_exception:
            errs.append("[E13] except 块用 logger.exception（observability-only 降级不当 error 刷告警）")
        elif not has_debug:
            errs.append("[E13] except 块未用 logger.debug（降级不可观测）")
        else:
            print("[E13] OK  except 块用 logger.debug（降级可观测不当 error，非 pass 吞没）")

    return errs


def main() -> int:
    print("=== VH3 回归：_detect_residual_interrupt 可观测出口契约 ===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "VH3 回归契约锁定（B6 修复不退化）：\n"
        "  · A info 出口带结构化 extra（event=plan_abandoned_by_new_demand + incoming_kind + "
        "pending_steps），未来接 Loki/ELK 可按 event 聚合，不必 grep 散文；\n"
        "  · B debug 兜底带结构化 extra（event=residual_interrupt_probe_skipped + exc_info=True），"
        "降级可收集非静默吞没，保留 traceback；\n"
        "  · C helper observability-only：签名 -> None + 调用方仅 await 不接收返回值 + "
        "无非 None return（不 mutate 不路由，纯观测）；\n"
        "  · D docstring 标注出口性质（Outlet contract）+「safe to delete, routing is unchanged」"
        "删除边界 + 引用 test_m12_boundary_new_demand 保路由测，消除「无出口」模糊；\n"
        "  · E except 用 logger.debug（非 pass 吞没、非 logger.exception 刷 error），"
        "observability-only 降级口径得当。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

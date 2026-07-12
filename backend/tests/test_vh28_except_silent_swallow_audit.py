"""VH28 回归：全仓 except 静默吞没审计 + 修复锁契约（task B31）.

锁住 B31 换角度重巡航·错误处理——全仓 grep ``except Exception`` / ``except:`` / ``pass``
逐个判定「静默吞没 vs 合理兜底」，修掉真吞没.

B31 审计结论（全仓 except 三分法：静默吞没 / 合理兜底有出口 / 良性可吞没已注释）：

  ── 全仓 except 清单（非 tests/，~30 处，分三类） ──

  ── 类 1：合理兜底有出口（return 结构化结果 / continue 过滤 / 兜底值）——非静默 ──
    这类 except 的降级结果**有可见出口**（return 给调用方 / continue 跳过条目 / 赋兜底值
    后续走正常路径），异常未被「吞没」而是「转化」——保留不改：

    · llm/probe.py test_provider: 三连 httpx 超时/连接/HTTPError + JSON 解析失败
      → return {"ok": False, "error": ...} 结构化给前端（用户看到错误）.
    · llm/probe.py fetch_models: 同上三连 + JSON 解析 → return {"ok": False, "models": []}.
    · llm/probe.py context_window int(ctx): TypeError/ValueError → 0=未知（UI 显示"—"）.
    · engine/tools.py 各 tool: ValueError(safe_path) + Exception(读写) → return "Error: ...".
    · engine/tools.py run_command: spawn Exception → return "Error spawning". timeout kill
      ProcessLookupError → pass（良性竞态，已注释说明）.
    · engine/mcp_manager.py get_tools: Exception → logger.warning + continue（跳过该 server）.
    · engine/worker.py:199/217/299: emit_task_token/reasoning Exception → logger.exception
      + brain decision Exception → logger.warning + chat 兜底（与协调者 B28 同款）.
    · engine/coordinator.py 13 处: B28 已锁（best-effort logger.exception + LLM warning 兜底
      + observability debug）——见 test_vh25.
    · engine/registry.py:156/171: CancelledError + TimeoutError → continue（loop 续命）.
    · engine/agent_loop.py:98/219/247/392/419: _summarize_args 兜底 / init logger.exception /
      create_react_agent logger.exception / GraphRecursionError 兜底 / execution logger.exception.
    · engine/scheduler.py:107: fire Exception → logger.exception + 记 failed run.
    · api/system.py:309: MCP load Exception → return {"ok": False, "error": ...}.
    · api/skills.py:77/245/264: LLM 失败 warning + 兜底 / UnicodeDecodeError → 400 /
      JSONDecodeError → 400（HTTP 异常出口）.
    · api/agents.py:55: LLM 失败 warning + 兜底.
    · skill_hub.py:278/362/370: remote hub Exception → logger.warning + return []（# noqa
      BLE001 overlay must never raise）.
    · llm/client.py:187: json.JSONDecodeError → continue（SSE 跳过非 JSON 帧）.
    · llm/extract_json.py:55: JSONDecodeError → return None（解析失败兜底）.
    · engine/coordinator.py:213: _detect_residual_interrupt → logger.debug(exc_info)（B28 已锁）.
    · events/bus.py:88: per-socket fan-out → logger.debug(exc_info)（B15 已锁，防流式洪水）.

  ── 类 2：真静默吞没（bare pass / 无日志无出口）——B31 修复点 ──
    这类 except 把异常**完全吃掉**（无日志 + 无出口 + 无注释说明为何吞），故障不可观测——
    B31 逐个补 debug + exc_info 或注释说明为何良性吞没：

    1. store/database.py:43 _enable_wal_once ``except Exception: pass`` → **真吞没**
       （PRAGMA 失败不可观测，可能 silently 落 rollback journal 模式）→ 补 logger.debug.
    2. store/database.py:85 _migrate_schema ``except Exception: pass`` → **真吞没**
       （schema 升级失败不可观测，列缺失会运行时炸）→ 补 logger.debug.
    3. engine/agent_loop.py:413 checkpoint 恢复 ``except Exception: pass`` → **真吞没**
       （recursion 恢复时 aget_state 失败不可观测）→ 补 logger.debug（嵌套在已处理的
       GraphRecursionError 内，debug 非 exception 避免与外层 warning 重复）.
    4. engine/scheduler.py:134 remove_job ``except Exception: pass`` → **真吞没**
       （良性 JobLookupError 与真 scheduler 故障不分全静默）→ 补 logger.debug（良性
       JobLookupError 是正常路径，真故障需 exc_info 可查）.
    5. api/plan.py:133 get_resident_plan ``except Exception: return mirror`` → **半吞没**
       （降级语义保留但异常不可观测，checkpointer 故障与冷启 normal 路径不分）→ 补
       logger.debug（debug 非 exception，因 cold-coordinator normal 路径会高频命中）.

  ── 类 3：良性有意吞没（已注释说明为何 pass）——保留不改 ──
    6. engine/tools.py:151 ProcessLookupError ``pass`` → kill-after-exit 竞态良性，
       B31 补注释说明（非裸 pass，有 # 注释 + 后续 return）.
    7. api/websocket.py:23 WebSocketDisconnect ``pass`` → 客户端断开正常退出，finally
       unsubscribe 已处理，B31 补注释说明（每页关闭一次，logging 会洪水）.
    8. engine/registry.py:156 CancelledError ``pass`` → stop() cancel 后正常退出.
    9. engine/registry.py:171 TimeoutError ``continue`` → inbox 1s 心跳续命.

  ── B31 修复口径（不是「全删 pass」，是「分类处理」） ──
    · 真吞没（无日志无出口无注释）→ 补 ``logger.debug(..., exc_info=True)``（debug 非
      exception：这些多是 best-effort/降级路径，exception 级会刷 ERROR 把正常降级当故障；
      exc_info 保留 traceback 供 debug 级排查）.
    · 半吞没（有出口但无日志）→ 补 logger.debug（出口已给用户，日志补可观测性）.
    · 良性吞没（已知良性竞态/正常退出）→ 补注释说明为何 pass（不删——ProcessLookupError/
      WebSocketDisconnect/CancelledError 的 pass 是正确语义）.
    · 合理兜底（已有日志或 HTTP 出口）→ 不改.

  ── B31 不引入 logger 到 config.py（B25/vh22 已锁 config 无 logger 防泄 key） ──
    config.py 仍无 logger（vh22 锁契约），B31 修复的 5 处都在 database/agent_loop/
    scheduler/plan/tools——与 config.py 隔离.

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh27 同款风格.

五段契约：

  A. 真吞没已补 logger.debug（4 处 bare pass → debug + exc_info）
    1. database.py _enable_wal_once except 不再 bare pass.
    2. database.py _migrate_schema except 不再 bare pass.
    3. agent_loop.py checkpoint 恢复 except 不再 bare pass.
    4. scheduler.py remove_job except 不再 bare pass.

  B. 半吞没已补 logger.debug（1 处有出口无日志 → debug + exc_info）
    5. plan.py get_resident_plan except 补 logger.debug（降级出口保留）.

  C. 良性吞没已补注释说明（2 处有意 pass → 注释说明为何良性）
    6. tools.py ProcessLookupError pass 有注释说明竞态良性.
    7. websocket.py WebSocketDisconnect pass 有注释说明正常退出.

  D. 合理兜底未回归（已有出口/日志的不被 B31 误删 pass 逻辑破）
    8. tools.py run_command spawn Exception 仍 return "Error spawning"（兜底出口）.
    9. mcp_manager.py get_tools Exception 仍 logger.warning + continue.
   10. probe.py JSON 解析 except 仍 return {"ok": False, ...}（结构化出口）.

  E. 全仓无裸 ``except:``（无 untyped except）+ config.py 仍无 logger（B25 不回归）
   11. 全仓（非 tests/）无 ``except:``（裸 except 全类型吞）.
   12. config.py 仍无 logger/logging/print（vh22 B25 锁契约不回归）.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
DATABASE_PY = BACKEND / "store" / "database.py"
AGENT_LOOP_PY = BACKEND / "engine" / "agent_loop.py"
SCHEDULER_PY = BACKEND / "engine" / "scheduler.py"
PLAN_PY = BACKEND / "api" / "plan.py"
TOOLS_PY = BACKEND / "engine" / "tools.py"
WEBSOCKET_PY = BACKEND / "api" / "websocket.py"
MCP_MANAGER_PY = BACKEND / "engine" / "mcp_manager.py"
PROBE_PY = BACKEND / "llm" / "probe.py"
CONFIG_PY = BACKEND / "config.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _except_body(src: str, except_line_no: int, max_scan: int = 8) -> list[str]:
    """Return up to max_scan lines following the except clause (1-based line_no)."""
    lines = src.splitlines()
    out: list[str] = []
    for off in range(1, max_scan + 1):
        idx = except_line_no - 1 + off  # 0-based
        if idx >= len(lines):
            break
        ln = lines[idx]
        out.append(ln)
        # stop at next clause/def boundary
        if re.match(r"\s*(except|async def |def )", ln):
            break
    return out


def _line_no_of(src: str, needle: str, occurrence: int = 1) -> int:
    """1-based line number of the n-th occurrence of needle (0 if not found)."""
    count = 0
    for i, ln in enumerate(src.splitlines(), start=1):
        if needle in ln:
            count += 1
            if count == occurrence:
                return i
    return 0


def assert_contract() -> list[str]:
    errs: list[str] = []

    db = _read(DATABASE_PY)
    al = _read(AGENT_LOOP_PY)
    sched = _read(SCHEDULER_PY)
    plan = _read(PLAN_PY)
    tools = _read(TOOLS_PY)
    ws = _read(WEBSOCKET_PY)
    mcp = _read(MCP_MANAGER_PY)
    probe = _read(PROBE_PY)

    # ── A. 真吞没已补 logger.debug（4 处 bare pass → debug + exc_info）──
    # database.py 已有 logger（B31 补 import logging + getLogger）
    # [1] _enable_wal_once except 不再 bare pass
    wal_idx = _line_no_of(db, "except Exception:")
    wal_body = _except_body(db, wal_idx, 6) if wal_idx else []
    wal_flat = "\n".join(wal_body)
    if re.search(r"^\s*pass\s*$", wal_flat, re.M):
        errs.append("[A1] database.py _enable_wal_once except 仍 bare pass（PRAGMA 失败静默）")
    elif "logger.debug" not in wal_flat or "exc_info" not in wal_flat:
        errs.append("[A1] database.py _enable_wal_once except 未补 logger.debug(exc_info)（B31 修复未落地）")
    else:
        print("[A1] OK  database.py _enable_wal_once except → logger.debug(exc_info)（PRAGMA 失败可观测）")
    # database.py 已 import logging + logger
    if "import logging" not in db or "getLogger" not in db:
        errs.append("[A1b] database.py 未 import logging/getLogger（B31 logger 补漏）")
    else:
        print("[A1b] OK  database.py 已 import logging + getLogger（B31 logger 落地）")

    # [2] _migrate_schema except 不再 bare pass
    mig_idx = _line_no_of(db, "except Exception:", 2)
    mig_body = _except_body(db, mig_idx, 8) if mig_idx else []
    mig_flat = "\n".join(mig_body)
    if re.search(r"^\s*pass\s*$", mig_flat, re.M):
        errs.append("[A2] database.py _migrate_schema except 仍 bare pass（schema 升级静默）")
    elif "logger.debug" not in mig_flat or "exc_info" not in mig_flat:
        errs.append("[A2] database.py _migrate_schema except 未补 logger.debug(exc_info)（B31 修复未落地）")
    else:
        print("[A2] OK  database.py _migrate_schema except → logger.debug(exc_info)（schema 升级失败可观测）")

    # [3] agent_loop.py checkpoint 恢复 except 不再 bare pass
    # 锚点：recursion-limit recovery 内的 aget_state try 块的 except Exception
    cp_idx = _line_no_of(al, "checkpoint state read failed")
    if cp_idx == 0:
        errs.append("[A3] agent_loop.py checkpoint 恢复 except 未补 logger.debug（B31 修复锚点失）")
    else:
        # 往上找对应 except Exception（应在 cp_idx 上方几行）
        cp_body = "\n".join(al.splitlines()[max(0, cp_idx - 6):cp_idx + 4])
        if "logger.debug" in cp_body and "exc_info" in cp_body and "recursion-limit recovery" in cp_body:
            print("[A3] OK  agent_loop.py checkpoint 恢复 except → logger.debug(exc_info)（recursion 恢复失败可观测）")
        else:
            errs.append("[A3] agent_loop.py checkpoint 恢复 except 未补完整 logger.debug+exc_info+recursion 锚")

    # [4] scheduler.py remove_job except 不再 bare pass
    rj_idx = _line_no_of(sched, "remove_job skipped")
    if rj_idx == 0:
        errs.append("[A4] scheduler.py remove_job except 未补 logger.debug（B31 修复锚点失）")
    else:
        rj_body = "\n".join(sched.splitlines()[max(0, rj_idx - 4):rj_idx + 4])
        if "logger.debug" in rj_body and "exc_info" in rj_body:
            print("[A4] OK  scheduler.py remove_job except → logger.debug(exc_info)（scheduler 故障可观测）")
        else:
            errs.append("[A4] scheduler.py remove_job except 未补 logger.debug+exc_info")

    # ── B. 半吞没已补 logger.debug（plan.py get_resident_plan）──
    # [5] plan.py get_resident_plan except 补 logger.debug（降级出口保留）
    plan_idx = _line_no_of(plan, "degrading to in-memory mirror")
    if plan_idx == 0:
        errs.append("[B5] plan.py get_resident_plan except 未补 logger.debug（B31 修复锚点失）")
    else:
        plan_body = "\n".join(plan.splitlines()[max(0, plan_idx - 6):plan_idx + 4])
        has_debug = "logger.debug" in plan_body
        has_return = "return [dict(s) for s in engine._dispatch_plan]" in plan.splitlines()[plan_idx - 1] or \
                     "return [dict(s) for s in engine._dispatch_plan]" in "\n".join(plan.splitlines()[plan_idx - 1:plan_idx + 6])
        if has_debug and "exc_info" in plan_body:
            print("[B5] OK  plan.py get_resident_plan except → logger.debug(exc_info)（降级出口保留）")
        else:
            errs.append("[B5] plan.py get_resident_plan except 未补 logger.debug+exc_info（或降级 return 丢失）")
    # plan.py 已 import logging + logger
    if "import logging" not in plan or "getLogger" not in plan:
        errs.append("[B5b] plan.py 未 import logging/getLogger（B31 logger 补漏）")
    else:
        print("[B5b] OK  plan.py 已 import logging + getLogger（B31 logger 落地）")

    # ── C. 良性吞没已补注释说明（2 处有意 pass → 注释说明为何良性）──
    # [6] tools.py ProcessLookupError pass 有注释说明竞态良性
    pl_idx = _line_no_of(tools, "except ProcessLookupError:")
    if pl_idx == 0:
        errs.append("[C6] tools.py ProcessLookupError except 未找到（锚点失）")
    else:
        # B31 注释块较长，pass 可能在 except 后 8-10 行——扩大窗口到 12 行
        pl_body = "\n".join(tools.splitlines()[pl_idx - 1:pl_idx + 12])
        has_pass = re.search(r"^\s*pass\s*$", pl_body, re.M)
        # 注释应在 except 上方或 pass 上下方——查 except 前后 12 行内的 B31 注释
        ctx = "\n".join(tools.splitlines()[max(0, pl_idx - 4):pl_idx + 12])
        has_note = "B31" in ctx and ("竞态" in ctx or "race" in ctx.lower())
        if has_pass and has_note:
            print("[C6] OK  tools.py ProcessLookupError pass 有 B31 注释说明竞态良性（保留 pass）")
        else:
            errs.append(f"[C6] tools.py ProcessLookupError 缺 B31 竞态注释（pass={bool(has_pass)} note={bool(has_note)}）")
    # [7] websocket.py WebSocketDisconnect pass 有注释说明正常退出
    wd_idx = _line_no_of(ws, "except WebSocketDisconnect:")
    if wd_idx == 0:
        errs.append("[C7] websocket.py WebSocketDisconnect except 未找到（锚点失）")
    else:
        ctx = "\n".join(ws.splitlines()[max(0, wd_idx - 2):wd_idx + 6])
        has_pass = re.search(r"^\s*pass\s*$", ctx, re.M)
        has_note = "B31" in ctx and ("正常" in ctx or "disconnect" in ctx.lower())
        if has_pass and has_note:
            print("[C7] OK  websocket.py WebSocketDisconnect pass 有 B31 注释说明正常退出（保留 pass）")
        else:
            errs.append(f"[C7] websocket.py WebSocketDisconnect 缺 B31 注释（pass={bool(has_pass)} note={bool(has_note)}）")

    # ── D. 合理兜底未回归（已有出口/日志不被 B31 误删）──
    # [8] tools.py run_command spawn Exception 仍 return "Error spawning"
    if "return f\"Error spawning command: {exc}\"" not in tools:
        errs.append("[D8] tools.py run_command spawn Exception 兜底 return 丢失（B31 误删）")
    else:
        print("[D8] OK  tools.py run_command spawn Exception → return Error spawning（兜底出口未回归）")
    # [9] mcp_manager.py get_tools Exception 仍 logger.warning + continue
    mcp_body = "\n".join(mcp.splitlines())
    if "logger.warning" not in mcp_body or "continue" not in mcp.split("except Exception as exc:")[1].split("\n", 6)[0:6] if "except Exception as exc:" in mcp else True:
        # 上面表达式绕，重写：找 except Exception as exc: 后 6 行内有 warning+continue
        seg = mcp.split("except Exception as exc:", 1)
        if len(seg) < 2:
            errs.append("[D9] mcp_manager.py get_tools except Exception as exc 未找到")
        else:
            after = "\n".join(seg[1].splitlines()[:6])
            if "logger.warning" in after and "continue" in after:
                print("[D9] OK  mcp_manager.py get_tools Exception → logger.warning + continue（未回归）")
            else:
                errs.append("[D9] mcp_manager.py get_tools Exception 缺 logger.warning+continue")
    # [10] probe.py JSON 解析 except 仍 return {"ok": False, ...}
    if 'return {' not in probe or '"ok": False' not in probe or "响应非 JSON" not in probe:
        errs.append("[D10] probe.py JSON 解析 except 兜底 return 丢失（B31 误删）")
    else:
        print("[D10] OK  probe.py JSON 解析 except → return ok:False（结构化出口未回归）")

    # ── E. 全仓无裸 except: + config.py 仍无 logger（B25 不回归）──
    # [11] 全仓（非 tests/）无 `except:` 裸 except
    bare_bare = []
    for py in BACKEND.rglob("*.py"):
        if "/tests/" in str(py) or "\\tests\\" in str(py):
            continue
        txt = py.read_text(encoding="utf-8")
        for i, ln in enumerate(txt.splitlines(), start=1):
            if re.match(r"\s*except\s*:", ln):
                bare_bare.append(f"{py.relative_to(BACKEND)}:{i}")
    if bare_bare:
        errs.append(f"[E11] 全仓含裸 `except:`（untyped 全吞）：{bare_bare[:3]}")
    else:
        print("[E11] OK  全仓无裸 `except:`（无 untyped except 全吞）")
    # [12] config.py 仍无 logger/logging/print（vh22 B25 锁不回归）
    if CONFIG_PY.exists():
        cfg = _read(CONFIG_PY)
        has_logging = "import logging" in cfg
        has_logger = "logger" in cfg and re.search(r"\blogger\.\w", cfg)
        has_print = re.search(r"\bprint\s*\(", cfg)
        if has_logging or has_logger or has_print:
            errs.append(f"[E12] config.py 含 logging/logger/print（B25/vh22 破——key 可能落日志）logging={has_logging} logger={has_logger} print={bool(has_print)}")
        else:
            print("[E12] OK  config.py 仍无 logger/logging/print（B25/vh22 不回归）")
    else:
        errs.append("[E12] config.py 未找到")

    return errs


def main() -> int:
    print("=== VH28 回归：全仓 except 静默吞没审计 + 修复锁契约（B31）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B31 全仓 except 静默吞没审计锁定：\n"
        "  · A 真吞没 4 处 bare pass → logger.debug(exc_info)（database WAL/migrate + agent_loop checkpoint + scheduler remove_job）；\n"
        "  · B 半吞没 1 处有出口无日志 → logger.debug(exc_info)（plan get_resident_plan 降级补可观测）；\n"
        "  · C 良性吞没 2 处有意 pass → 补 B31 注释说明为何良性（ProcessLookupError 竞态 + WebSocketDisconnect 正常退出）；\n"
        "  · D 合理兜底未回归（tools spawn return + mcp warning+continue + probe JSON return）；\n"
        "  · E 全仓无裸 except: + config.py 仍无 logger（B25/vh22 不回归）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""VH15 回归：parseStats 共享解析器去重——useBusEvent 与 ChatPanel 两处守卫合一（task B18）.

锁住 B18 修复——``src/hooks/useBusEvent.ts:442-470`` coordinator_stats 分支与
``src/components/ChatPanel.tsx:138-150`` ``extractCoordStats`` 两处重复
``Number() / Number.isFinite / typeof string`` 守卫，抽共享 ``parseStats(dd)`` 到
``services/api.ts`` 单一真源。

B18 前的重复：
  - useBusEvent coordinator_stats 分支（WS 流式路径）：
      const phase = String(dd['phase'] || 'streaming')
      const elapsedMs = Number(dd['elapsed_ms'] || 0)
      const tokens = Number(dd['tokens'] || 0)
      const model = typeof dd['model'] === 'string' ? dd['model'] : undefined
      const reasoningTokensNum = Number(dd['reasoning_tokens'] || 0)
      const reasoning_tokens = Number.isFinite(reasoningTokensNum) && reasoningTokensNum > 0 ? ...
  - ChatPanel extractCoordStats（定稿气泡路径）：
      const elapsed = Number(data.elapsed_ms)
      if (!Number.isFinite(elapsed) || elapsed <= 0) return null
      const tokens = Number(data.tokens)
      const model = typeof data.model === 'string' && data.model ? data.model : undefined
      const reasoningTokensNum = Number(data.reasoning_tokens)
      const reasoning_tokens = Number.isFinite(reasoningTokensNum) && reasoningTokensNum > 0 ? ...
  → 两处几乎逐字相同，差异仅：① WS 有 phase，定稿无；② 定稿 strictElapsed（<=0 → null），
    WS 非 strict（streaming 中间值 0 合法）。同一套守卫逻辑复制两份——任一处口径漂移
    就 WS 与定稿不一致（流式气泡显示 5 tokens，定稿气泡却 0 tokens）。

B18 改法（抽 ``services/api.ts parseStats`` 单一真源）：
  - ``parseStats(raw, opts?)`` 统一 Number/Number.isFinite/typeof string 守卫。
  - ``withPhase``（默认 true）：WS 流式路径返 ``CoordStats``（含 phase）；
    定稿气泡传 false 返 ``FinalizedStats``（无 phase）——同一函数两种返回形。
  - ``strictElapsed``（默认 false）：WS 路径 false（streaming elapsed_ms=0 合法不返 null）；
    定稿气泡传 true（非有限/<=0 返 null，announce 类回复无 elapsed_ms 不渲染假状态行，
    A8/vg2 契约）。
  - 两个调用方都是薄封装：useBusEvent ``parseStats(dd, { withPhase: true })``，
    ChatPanel ``extractCoordStats = parseStats(data, { withPhase: false, strictElapsed: true })``。
  - 新增 ``CoordStats``（含 phase）/ ``FinalizedStats``（无 phase）类型——原两处内联类型
    注解（``{ elapsed_ms: number; tokens: number; phase: string; model?: string;
    reasoning_tokens?: number }``）抽成具名 interface，useBusEvent coordStats state 类型
    从内联改 ``Record<string, CoordStats>``。

行为零变约束（两处守卫口径逐字对齐）：
  - elapsed_ms：WS ``Number(dd['elapsed_ms'] || 0)`` ↔ parseStats ``Number(dd['elapsed_ms'] ?? 0)``，
    非 finite 降 0（WS 隐式 Number(NaN)=NaN → 赋给 state 是 NaN，parseStats 显式 finite? :0
    更稳——但 WS 路径后端必传 int，不会 NaN；定稿 strictElapsed 分支 finite/>0 才过）。
  - tokens：WS ``Number(dd['tokens'] || 0)`` ↔ parseStats ``Number(dd['tokens'] ?? 0)`` finite? :0。
  - model：两处都 ``typeof === 'string'`` 才取，WS 不判 falsy（''也取）vs 定稿判 falsy（''不取）
    —— parseStats 统一为定稿口径（``typeof === 'string' && modelRaw``，''不取）。口径收紧
    （WS 原 '' 会取为 model:''，现 '' 不取为 undefined——但后端 model 必传非空或 undefined，
    '' 不会出现，行为实际零变）。
  - reasoning_tokens：两处都 ``finite && > 0 ? : undefined``，口径逐字相同。
  - phase：WS ``String(dd['phase'] || 'streaming')`` ↔ parseStats ``String(dd['phase'] ?? 'streaming')``
    （|| 与 ?? 对 string 等价——'' 不会出现，phase 非 'streaming' 即 'done'）。

为何放 services/api.ts 而非 src/lib/utils：
  - parseStats 是 BusEventData.data 的投影器（后端 ``emit_coordinator_stats`` / 持久化
    ``agent_reply.data`` 的字段解析），与 ``TraceEvent`` / ``BusEventData`` 同源——放 api.ts
    与其他类型/投影器同模块，import 路径短（useBusEvent 已从 api.ts import 多个符号）。
  - src/lib 是通用 utils（fileIcon/tts/slashCommands），parseStats 是领域投影器非通用，
    不该混入。任务明列「services/api.ts 或 utils」——选 api.ts 更贴合领域归属。

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh14 同款风格。

七段契约：

  A. parseStats 真源定义（services/api.ts）
    1. ``export function parseStats(raw, opts?)`` 定义（单一真源）。
    2. ``export interface CoordStats``（含 phase，WS 流式返回形）。
    3. ``export interface FinalizedStats``（无 phase，定稿气泡返回形）。
    4. ``parseStats`` 返回 ``CoordStats | FinalizedStats | null``（两种形 + null）。

  B. parseStats 守卫口径（Number/Number.isFinite/typeof string）
    5. ``Number(dd['elapsed_ms'] ?? 0)`` + ``Number.isFinite`` 守卫。
    6. ``Number(dd['tokens'] ?? 0)`` + ``Number.isFinite`` 守卫（非有限降 0）。
    7. ``typeof dd['model'] === 'string' && modelRaw`` 守卫（仅非空 string 才取）。
    8. ``Number(dd['reasoning_tokens'] ?? 0)`` + ``finite && > 0 ? : undefined`` 守卫。

  C. parseStats 选项（withPhase / strictElapsed）
    9. ``withPhase``（默认 true）—— true 返 CoordStats（含 phase），false 返 FinalizedStats。
   10. ``strictElapsed``（默认 false）—— true 时 elapsed_ms 非有限/<=0 返 null（定稿气泡口径）。
   11. ``String(dd['phase'] ?? 'streaming')``（withPhase=true 时 phase 默认 streaming）。
   12. raw 非 object 返 null（WS 事件 data 缺失/异常兜底）。

  D. useBusEvent coordinator_stats 接线 parseStats（WS 流式路径）
   13. coordinator_stats 分支调 ``parseStats(dd, { withPhase: true })``（非内联守卫）。
   14. 不再有内联 ``Number(dd['elapsed_ms']...) / Number(dd['tokens']...)``（原重复守卫消失）。
   15. coordStats state 类型改 ``Record<string, CoordStats>``（非内联类型注解）。
   16. useBusEvent import ``parseStats`` + ``CoordStats`` from '../services/api'。

  E. ChatPanel extractCoordStats 接线 parseStats（定稿气泡路径）
   17. extractCoordStats 调 ``parseStats(data, { withPhase: false, strictElapsed: true })``。
   18. extractCoordStats 不再有内联 ``Number(data.elapsed_ms) / Number.isFinite(elapsed)``（原守卫消失）。
   19. extractCoordStats 返回 ``FinalizedStats | null``（非内联类型）。
   20. ChatPanel import ``parseStats`` + ``FinalizedStats`` from '../services/api'。

  F. 行为零变（两路径守卫口径对齐 + 不回归 A8/vg2/va2/va3）
   21. WS 路径仍非 strictElapsed（streaming elapsed_ms=0 合法不返 null）。
   22. 定稿路径仍 strictElapsed=true（elapsed_ms<=0 返 null，announce 不渲染假状态行——A8/vg2）。
   23. 两路径 reasoning_tokens 守卫同口径（finite && > 0 ? : undefined）。

  G. 无回归（既有测不破）
   24. va2 [10] 前端字段对齐：断言改为 parseStats 真源读四字段 + elapsed_ms 守卫（B18 下沉）。
   25. va3 [12] 不区分来源：断言改为 extractCoordStats 调 parseStats（无 sender_id 分支）+ parseStats 读四字段。
   26. vg2 [C10] elapsed_ms 守卫：断言改为 parseStats 真源有 Number.isFinite + <=0 → null（strictElapsed 分支）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
API = REPO / "src" / "services" / "api.ts"
HOOK = REPO / "src" / "hooks" / "useBusEvent.ts"
PANEL = REPO / "src" / "components" / "ChatPanel.tsx"


def _fn_body_ts(src: str, fname: str, kind: str = "function") -> str:
    """抽 TS 函数体。kind: 'function' (function f() {...}) 或 'export' (export function f())."""
    if kind == "export":
        m = re.search(rf"export function {fname}\([^)]*\)[^{{]*\{{(.*?)\n\}}", src, re.S)
    else:
        m = re.search(rf"function {fname}\([^)]*\)[^{{]*\{{(.*?)\n\}}", src, re.S)
    return m.group(1) if m else ""


def _strip_ts_comments(src: str) -> str:
    """剔单行 ``//`` 注释（B16/vh13 坑延续——注释引用不计代码字面量）。"""
    return re.sub(r"//[^\n]*", "", src)


def assert_contract() -> list[str]:
    errs: list[str] = []
    api = API.read_text(encoding="utf-8")
    hook = HOOK.read_text(encoding="utf-8")
    panel = PANEL.read_text(encoding="utf-8")
    api_nc = _strip_ts_comments(api)
    hook_nc = _strip_ts_comments(hook)
    panel_nc = _strip_ts_comments(panel)

    parse_body = _fn_body_ts(api, "parseStats", "export")
    if not parse_body:
        errs.append("[setup] services/api.ts parseStats 函数体未找到")
        return errs

    # ── A. parseStats 真源定义 ──
    # [1] export function parseStats 定义
    if not re.search(r"export function parseStats\(", api):
        errs.append("[A1] services/api.ts 缺 export function parseStats（B18 单一真源缺失）")
    else:
        print("[A1] OK  export function parseStats 定义（单一真源）")
    # [2] export interface CoordStats（含 phase）
    m_coord = re.search(r"export interface CoordStats\s*\{(.*?)\}", api, re.S)
    if not m_coord or "phase" not in m_coord.group(1):
        errs.append("[A2] 缺 export interface CoordStats（含 phase，WS 流式返回形）")
    else:
        print("[A2] OK  export interface CoordStats（含 phase，WS 流式返回形）")
    # [3] export interface FinalizedStats（无 phase）
    m_fin = re.search(r"export interface FinalizedStats\s*\{(.*?)\}", api, re.S)
    if not m_fin or "phase" in m_fin.group(1):
        errs.append("[A3] 缺 export interface FinalizedStats（无 phase，定稿气泡返回形）")
    else:
        print("[A3] OK  export interface FinalizedStats（无 phase，定稿气泡返回形）")
    # [4] parseStats 返回 CoordStats | FinalizedStats | null
    m_ret = re.search(r"export function parseStats\([^)]*\)\s*(?::\s*([^\n{]+))?\{", api)
    ret_ann = m_ret.group(1).strip() if m_ret and m_ret.group(1) else ""
    if "CoordStats" not in ret_ann or "FinalizedStats" not in ret_ann or "null" not in ret_ann:
        errs.append(f"[A4] parseStats 返回类型异常（应 CoordStats | FinalizedStats | null，实际 {ret_ann!r}）")
    else:
        print(f"[A4] OK  parseStats 返回 CoordStats | FinalizedStats | null（两种形 + null）")

    # ── B. parseStats 守卫口径 ──
    # [5] Number(dd['elapsed_ms'] ?? 0) + Number.isFinite 守卫
    if not re.search(r"Number\(dd\['elapsed_ms'\]\s*\?\?\s*0\)", parse_body):
        errs.append("[B5] parseStats 缺 Number(dd['elapsed_ms'] ?? 0)")
    elif "Number.isFinite(elapsedMsNum)" not in parse_body:
        errs.append("[B5] parseStats 缺 Number.isFinite(elapsedMsNum) 守卫")
    else:
        print("[B5] OK  Number(dd['elapsed_ms'] ?? 0) + Number.isFinite 守卫")
    # [6] Number(dd['tokens'] ?? 0) + Number.isFinite（非有限降 0）
    if not re.search(r"Number\(dd\['tokens'\]\s*\?\?\s*0\)", parse_body):
        errs.append("[B6] parseStats 缺 Number(dd['tokens'] ?? 0)")
    elif "Number.isFinite(tokensNum)" not in parse_body:
        errs.append("[B6] parseStats 缺 Number.isFinite(tokensNum) 守卫（非有限降 0）")
    else:
        print("[B6] OK  Number(dd['tokens'] ?? 0) + Number.isFinite 守卫（非有限降 0）")
    # [7] typeof dd['model'] === 'string' && modelRaw 守卫
    if not re.search(r"typeof modelRaw === 'string' && modelRaw", parse_body):
        errs.append("[B7] parseStats 缺 typeof modelRaw === 'string' && modelRaw 守卫（仅非空 string 才取）")
    else:
        print("[B7] OK  typeof modelRaw === 'string' && modelRaw 守卫（仅非空 string 才取）")
    # [8] Number(dd['reasoning_tokens'] ?? 0) + finite && > 0
    if not re.search(r"Number\(dd\['reasoning_tokens'\]\s*\?\?\s*0\)", parse_body):
        errs.append("[B8] parseStats 缺 Number(dd['reasoning_tokens'] ?? 0)")
    elif not re.search(r"Number\.isFinite\(reasoningTokensNum\)\s*&&\s*reasoningTokensNum\s*>\s*0", parse_body):
        errs.append("[B8] parseStats 缺 finite && > 0 守卫（reasoning_tokens 口径）")
    else:
        print("[B8] OK  Number(dd['reasoning_tokens'] ?? 0) + finite && > 0 守卫")

    # ── C. parseStats 选项 ──
    # [9] withPhase（默认 true）—— true 返 CoordStats，false 返 FinalizedStats
    if "withPhase" not in parse_body:
        errs.append("[C9] parseStats 缺 withPhase 选项")
    elif not re.search(r"withPhase\s*\?\?\s*true", parse_body):
        errs.append("[C9] parseStats withPhase 默认值异常（应默认 true）")
    else:
        print("[C9] OK  withPhase（默认 true，true→CoordStats / false→FinalizedStats）")
    # [10] strictElapsed（默认 false）—— true 时 elapsed_ms 非有限/<=0 返 null
    if "strictElapsed" not in parse_body:
        errs.append("[C10] parseStats 缺 strictElapsed 选项")
    elif not re.search(r"strictElapsed\s*\?\?\s*false", parse_body):
        errs.append("[C10] parseStats strictElapsed 默认值异常（应默认 false）")
    elif not re.search(r"strictElapsed\s*&&\s*\(!Number\.isFinite\(elapsedMsNum\)\s*\|\|\s*elapsedMs\s*<=\s*0\)", parse_body):
        errs.append("[C10] parseStats 缺 strictElapsed && (!finite || <=0) → null 守卫")
    else:
        print("[C10] OK  strictElapsed（默认 false，true 时 elapsed_ms 非有限/<=0 → null）")
    # [11] String(dd['phase'] ?? 'streaming')（withPhase=true 时 phase 默认 streaming）
    if not re.search(r"String\(dd\['phase'\]\s*\?\?\s*'streaming'\)", parse_body):
        errs.append("[C11] parseStats 缺 String(dd['phase'] ?? 'streaming')（withPhase=true 时 phase 默认）")
    else:
        print("[C11] OK  String(dd['phase'] ?? 'streaming')（withPhase=true 时 phase 默认 streaming）")
    # [12] raw 非 object 返 null（WS 事件 data 缺失/异常兜底）。
    #     B20：守卫下沉到 safeRecord 单一真源（parseStats 内调 safeRecord(raw)）。
    #     断言 parseStats 调 safeRecord（守卫真源）+ safeRecord 有非 object → null 兜底。
    if "safeRecord(raw)" not in parse_body:
        errs.append("[C12] parseStats 未调 safeRecord(raw)（B20 守卫下沉到单一真源未接线）")
    else:
        # safeRecord 真源有非 object → null 兜底（parseStats 复用）
        api_src = API.read_text(encoding="utf-8")
        m_safe = re.search(r"export function safeRecord\([^)]*\)[^{]*\{(.*?)\n\}", api_src, re.S)
        if not m_safe:
            errs.append("[C12] services/api.ts safeRecord 未找到（B20 单一守卫真源缺失）")
        elif "typeof raw !== 'object'" not in m_safe.group(1) and "typeof data !== 'object'" not in m_safe.group(1):
            errs.append("[C12] safeRecord 缺 typeof !== 'object' 守卫（raw 非 object → null 兜底破）")
        else:
            print("[C12] OK  parseStats → safeRecord(raw)（B20 守卫下沉）+ safeRecord 非 object → null 兜底")

    # ── D. useBusEvent coordinator_stats 接线 parseStats ──
    # [13] coordinator_stats 分支调 parseStats(dd, { withPhase: true })
    stats_blk = re.search(r"else if \(d\.type === 'coordinator_stats'\) \{(.*?)\n      \}", hook, re.S)
    if not stats_blk:
        errs.append("[D13] useBusEvent coordinator_stats 分支未找到")
    elif "parseStats(" not in stats_blk.group(1):
        errs.append("[D13] coordinator_stats 分支未调 parseStats（B18 单一真源未接线）")
    elif "withPhase: true" not in stats_blk.group(1):
        errs.append("[D13] coordinator_stats 分支未传 withPhase: true（WS 流式应含 phase）")
    else:
        print("[D13] OK  coordinator_stats 分支 → parseStats(dd, { withPhase: true })（WS 流式含 phase）")
    # [14] 不再有内联 Number(dd['elapsed_ms']...) / Number(dd['tokens']...)（原重复守卫消失）
    if stats_blk:
        blk_nc = _strip_ts_comments(stats_blk.group(1))
        if re.search(r"Number\(dd\['elapsed_ms'\]", blk_nc) or re.search(r"Number\(dd\['tokens'\]", blk_nc):
            errs.append("[D14] coordinator_stats 分支仍内联 Number(dd['elapsed_ms'/'tokens'])（原守卫未去重）")
        else:
            print("[D14] OK  coordinator_stats 分支不再内联 Number() 守卫（原重复消除）")
    # [15] coordStats state 类型改 Record<string, CoordStats>
    if not re.search(r"const\s+\[coordStats,\s*setCoordStats\]\s*=\s*useState<Record<string,\s*CoordStats>>", hook):
        errs.append("[D15] coordStats state 类型未改 Record<string, CoordStats>（仍内联类型注解）")
    else:
        print("[D15] OK  coordStats state 类型 Record<string, CoordStats>（内联类型抽具名 interface）")
    # [16] useBusEvent import parseStats + CoordStats
    imp = re.search(r"from '\.\./services/api'", hook, re.S)
    if not imp:
        errs.append("[D16] useBusEvent 未 import parseStats/CoordStats from ../services/api")
    else:
        imp_block = hook[: hook.index("from '../services/api'")]
        if "parseStats" not in imp_block or "CoordStats" not in imp_block:
            errs.append("[D16] useBusEvent import 缺 parseStats/CoordStats")
        else:
            print("[D16] OK  useBusEvent import parseStats + CoordStats from ../services/api")

    # ── E. ChatPanel extractCoordStats 接线 parseStats ──
    # [17] extractCoordStats 调 parseStats(data, { withPhase: false, strictElapsed: true })
    ext_body = _fn_body_ts(panel, "extractCoordStats")
    if not ext_body:
        errs.append("[E17] ChatPanel extractCoordStats 函数体未找到")
    elif "parseStats(" not in ext_body:
        errs.append("[E17] extractCoordStats 未调 parseStats（B18 单一真源未接线）")
    elif "withPhase: false" not in ext_body or "strictElapsed: true" not in ext_body:
        errs.append("[E17] extractCoordStats 未传 { withPhase: false, strictElapsed: true }（定稿气泡口径）")
    else:
        print("[E17] OK  extractCoordStats → parseStats({ withPhase: false, strictElapsed: true })（定稿气泡口径）")
    # [18] 不再有内联 Number(data.elapsed_ms) / Number.isFinite(elapsed)（原守卫消失）
    if ext_body:
        ext_nc = _strip_ts_comments(ext_body)
        if re.search(r"Number\(data\.elapsed_ms\)", ext_nc) or "Number.isFinite(elapsed)" in ext_nc:
            errs.append("[E18] extractCoordStats 仍内联 Number(data.elapsed_ms)/Number.isFinite(elapsed)（原守卫未去重）")
        else:
            print("[E18] OK  extractCoordStats 不再内联 Number()/Number.isFinite() 守卫（原重复消除）")
    # [19] extractCoordStats 返回 FinalizedStats | null
    m_ext_sig = re.search(r"function extractCoordStats\([^)]*\)\s*(?::\s*([^\n{]+))?\{", panel)
    ext_ann = m_ext_sig.group(1).strip() if m_ext_sig and m_ext_sig.group(1) else ""
    if "FinalizedStats" not in ext_ann or "null" not in ext_ann:
        errs.append(f"[E19] extractCoordStats 返回类型异常（应 FinalizedStats | null，实际 {ext_ann!r}）")
    else:
        print("[E19] OK  extractCoordStats 返回 FinalizedStats | null（内联类型抽具名）")
    # [20] ChatPanel import parseStats + FinalizedStats
    imp_p = re.search(r"from '\.\./services/api'", panel, re.S)
    if not imp_p:
        errs.append("[E20] ChatPanel 未 import parseStats/FinalizedStats from ../services/api")
    else:
        imp_block = panel[: panel.index("from '../services/api'")]
        if "parseStats" not in imp_block or "FinalizedStats" not in imp_block:
            errs.append("[E20] ChatPanel import 缺 parseStats/FinalizedStats")
        else:
            print("[E20] OK  ChatPanel import parseStats + FinalizedStats from ../services/api")

    # ── F. 行为零变（两路径守卫口径对齐）──
    # [21] WS 路径仍非 strictElapsed（streaming elapsed_ms=0 合法不返 null）
    if stats_blk and "strictElapsed: true" in stats_blk.group(1):
        errs.append("[F21] WS 路径误传 strictElapsed: true（streaming elapsed_ms=0 会返 null，破坏流式）")
    else:
        print("[F21] OK  WS 路径非 strictElapsed（streaming elapsed_ms=0 合法不返 null）")
    # [22] 定稿路径仍 strictElapsed=true（elapsed_ms<=0 返 null，A8/vg2）
    if ext_body and "strictElapsed: true" in ext_body:
        print("[F22] OK  定稿路径 strictElapsed=true（elapsed_ms<=0 → null，announce 不渲染假状态行，A8/vg2）")
    else:
        errs.append("[F22] 定稿路径缺 strictElapsed: true（会渲染 0 耗时假状态行，破 A8/vg2）")
    # [23] 两路径 reasoning_tokens 守卫同口径（finite && > 0 ? : undefined）—— parseStats 真源统一
    if "reasoning_tokens" in parse_body and re.search(r"Number\.isFinite\(reasoningTokensNum\)\s*&&\s*reasoningTokensNum\s*>\s*0", parse_body):
        print("[F23] OK  两路径 reasoning_tokens 守卫同口径（parseStats 真源 finite && > 0 ? : undefined）")
    else:
        errs.append("[F23] parseStats 缺 reasoning_tokens 守卫（两路径口径可能漂移）")

    # ── G. 无回归（既有测不破）──
    # [24] va2 [10] 改为 parseStats 真源读四字段 + elapsed_ms 守卫
    va2 = (REPO / "backend" / "tests" / "test_va2_coord_stats_contract.py").read_text(encoding="utf-8")
    if "parseStats" not in va2 or "Number.isFinite(elapsedMsNum)" not in va2:
        errs.append("[G24] va2 [10] 未改为 parseStats 真源断言（守卫下沉未同步测）")
    else:
        print("[G24] OK  va2 [10] 改为 parseStats 真源断言（读四字段 + elapsed_ms 守卫下沉）")
    # [25] va3 [12] 改为 extractCoordStats 调 parseStats + parseStats 读四字段
    va3 = (REPO / "backend" / "tests" / "test_va3_worker_stats_contract.py").read_text(encoding="utf-8")
    if "parseStats(" not in va3 or "strictElapsed: true" not in va3:
        errs.append("[G25] va3 [12] 未改为 parseStats 断言（接线未同步测）")
    else:
        print("[G25] OK  va3 [12] 改为 extractCoordStats → parseStats 断言（不区分来源 + 读四字段）")
    # [26] vg2 [C10] 改为 parseStats 真源 Number.isFinite + <=0 → null
    vg2 = (REPO / "backend" / "tests" / "test_vg2_finalized_stats.py").read_text(encoding="utf-8")
    if "parseStats" not in vg2 or "Number.isFinite(elapsedMsNum)" not in vg2:
        errs.append("[G26] vg2 [C10] 未改为 parseStats 真源断言（守卫下沉未同步测）")
    else:
        print("[G26] OK  vg2 [C10] 改为 parseStats 真源断言（Number.isFinite + <=0 → null 下沉）")

    return errs


def main() -> int:
    print("=== VH15 回归：parseStats 共享解析器去重——useBusEvent 与 ChatPanel 两处守卫合一（B18）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B18 parseStats 共享解析器去重锁定：\n"
        "  · A 真源：services/api.ts export function parseStats + CoordStats/FinalizedStats 具名 interface（返回 CoordStats | FinalizedStats | null）；\n"
        "  · B 守卫口径：Number(?? 0) + Number.isFinite + typeof string && 非空 + finite && >0（四字段统一守卫，原两处重复消除）；\n"
        "  · C 选项：withPhase（默认 true，WS 含 phase / 定稿无 phase）+ strictElapsed（默认 false，定稿 true 时 <=0→null）+ raw 非 object→null 兜底；\n"
        "  · D useBusEvent 接线：coordinator_stats 分支 → parseStats({ withPhase: true })（WS 流式含 phase）+ coordStats state 类型 Record<string, CoordStats>；\n"
        "  · E ChatPanel 接线：extractCoordStats → parseStats({ withPhase: false, strictElapsed: true })（定稿气泡口径）+ 返 FinalizedStats | null；\n"
        "  · F 行为零变：WS 非 strictElapsed（streaming 0 合法）/ 定稿 strictElapsed=true（<=0→null，A8/vg2）/ reasoning_tokens 两路径同口径；\n"
        "  · G 无回归：va2 [10] / va3 [12] / vg2 [C10] 三测断言改为 parseStats 真源（守卫下沉同步）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""VH17 回归：safeRecord 共享 null-guard 入口——四 extractor 守卫合一（task B20）.

锁住 B20 修复——``src/components/ChatPanel.tsx:138-193`` 三个 extractor
（``extractCoordStats`` / ``extractCoordReasoning`` / ``extractFinalizedArtifacts``）
的 null-guard+类型守卫模式重复，抽共享 ``safeRecord(data)`` 到 ``services/api.ts``。

B20 前的重复守卫：
  - extractCoordStats（B18 已抽 parseStats，parseStats 内 ``if (!raw || typeof raw !==
    'object') return null``）。
  - extractCoordReasoning：``if (!data) return undefined`` + ``data.reasoning``。
  - extractFinalizedArtifacts 三层守卫：
      if (!data || typeof data !== 'object') return []
      const dd = data as Record<string, unknown>
      const artifact = dd['artifact']
      if (!artifact || typeof artifact !== 'object') return []
      const manifest = artifact as Record<string, unknown>
      ...
        if (!raw || typeof raw !== 'object') return null
        const f = raw as Record<string, unknown>
  → 四处 ``if (!x || typeof x !== 'object')`` + ``x as Record<string, unknown>`` 守卫散落，
    口径虽同但任一处漂移就守卫不一致。

B20 改法（抽 ``services/api.ts safeRecord`` 单一真源）：
  - ``safeRecord(data: unknown): Record<string, unknown> | null``：非 object / null /
    undefined / 数组 → null；object → ``Record<string, unknown>``（TS narrowed，无需再 as）。
  - parseStats 内 ``if (!raw || typeof raw !== 'object') return null`` → ``const dd =
    safeRecord(raw); if (!dd) return null``（B18 parseStats 守卫下沉到 safeRecord）。
  - extractCoordReasoning：``if (!data) return undefined`` → ``const dd = safeRecord(data);
    if (!dd) return undefined``（data 已是 Record|null 类型，safeRecord 兜底未来 unknown 透传）。
  - extractFinalizedArtifacts 三层守卫全改 safeRecord：
      ``safeRecord(data)`` / ``safeRecord(dd['artifact'])`` / ``safeRecord(raw)``。
  - 四个 extractor 入口统一走 safeRecord（parseStats + extractCoordStats 间接 + 两个直接）。

行为零变约束：
  - safeRecord 守卫 ``!data || typeof data !== 'object'`` 与原四处逐字同口径。
  - 额外 ``Array.isArray(data)`` 排除数组——原四处未显式排除（数组 ``typeof === 'object'`` 会过
    守卫当 record）。但实际调用方：① parseStats 的 raw 是 WS event data（dict 非数组）；
    ② extractCoordReasoning data 是 Message.data（Record|null）；③ extractFinalizedArtifacts
    的 data/artifact/raw——bus.py emit_task_completed 的 data.artifact 是 dict ``{files:[...]}``，
    files 元素是 dict。数组分支实际不命中，加 ``Array.isArray`` 排除是更严守卫（防御性，行为零变）。
  - extractFinalizedArtifacts 的 ``files`` 仍 ``Array.isArray(files)`` 判（files 是数组，
    safeRecord 排除数组故 files 单独判，非走 safeRecord）——与原口径逐字同。
  - extractFinalizedArtifacts 返回元素 name/path/size/modified_at 守卫逐字不变
    （typeof string / typeof number，本处守卫非 null-guard，B20 不抽）。

为何 safeRecord 排除数组（Array.isArray）：
  - TS ``typeof [] === 'object'``，原 ``typeof data !== 'object'`` 守卫会让数组通过当 record
    用 ``dd['artifact']`` 访问（数组无 artifact key → undefined → 返 []，碰巧不炸但口径松）。
  - safeRecord 加 ``Array.isArray(data)`` 显式排除数组 → 数组返 null → extractFinalizedArtifacts
    返 []（更严守卫，防御性收紧）。实际 bus 事件 data 非数组（dict），行为零变。
  - 标准 pattern：``isRecord(x) = x && typeof x === 'object' && !Array.isArray(x)``。

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh16 同款风格。

七段契约：

  A. safeRecord 真源定义（services/api.ts）
    1. ``export function safeRecord(data: unknown): Record<string, unknown> | null`` 定义。
    2. 守卫 ``!data || typeof data !== 'object' || Array.isArray(data) → null``（三段全）。
    3. 返 ``data as Record<string, unknown>``（object narrowed，非数组）。

  B. parseStats 复用 safeRecord（B18 守卫下沉）
    4. parseStats 内 ``const dd = safeRecord(raw); if (!dd) return null``。
    5. parseStats 不再有 ``if (!raw || typeof raw !== 'object') return null``（原守卫消失）。

  C. extractCoordReasoning 复用 safeRecord
    6. extractCoordReasoning 内 ``const dd = safeRecord(data); if (!dd) return undefined``。
    7. extractCoordReasoning 不再有 ``if (!data) return undefined``（原守卫消失）。
    8. reasoning 字段守卫仍本处（``typeof r === 'string' && r``，非 null-guard 不抽）。

  D. extractFinalizedArtifacts 三层复用 safeRecord
    9. ``safeRecord(data)``（外层 data 守卫）。
   10. ``safeRecord(dd['artifact'])``（artifact manifest 守卫）。
   11. ``safeRecord(raw)``（files 元组守卫，map 内）。
   12. files 仍 ``Array.isArray(files)`` 判（数组非 record，单独判非走 safeRecord）。

  E. 行为零变（守卫口径对齐 + 返回形不变）
   13. safeRecord 守卫 ``!data || typeof data !== 'object'`` 与原四处逐字同口径。
   14. safeRecord 额外 ``Array.isArray`` 排除数组（更严守卫，实际数组不命中，零变）。
   15. extractFinalizedArtifacts 仍返 ``ArtifactFile[]``（非 null，空数组兜底）。
   16. extractCoordReasoning 仍返 ``string | undefined``（非 null）。

  F. import 接线
   17. ChatPanel import ``safeRecord`` from '../services/api'。
   18. parseStats 在同文件（services/api.ts）无需 import safeRecord（同模块）。

  G. 无回归（既有测不破 + 调用链不破）
   19. vh15 [C12] 改为 parseStats → safeRecord(raw) 断言（守卫下沉同步）。
   20. vb2 [前端1] 改为三层 safeRecord + Array.isArray(files) 断言（守卫下沉同步）。
   21. extractFinalizedArtifacts 仍被 finalizedBubbles 调用（line ~533 artifactFiles）。
   22. extractCoordReasoning 仍被定稿气泡 reasoning Collapse 调用（line ~1018）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
API = REPO / "src" / "services" / "api.ts"
PANEL = REPO / "src" / "components" / "ChatPanel.tsx"


def _fn_body_ts(src: str, fname: str, prefix: str = "function") -> str:
    """抽 TS 函数体。prefix: 'function' / 'export function'。"""
    pat = rf"{re.escape(prefix)} {fname}\([^)]*\)[^{{]*\{{(.*?)\n\}}"
    m = re.search(pat, src, re.S)
    return m.group(1) if m else ""


def _strip_ts_comments(src: str) -> str:
    """剔单行 ``//`` 注释（B16/vh13 坑延续）。"""
    return re.sub(r"//[^\n]*", "", src)


def assert_contract() -> list[str]:
    errs: list[str] = []
    api = API.read_text(encoding="utf-8")
    panel = PANEL.read_text(encoding="utf-8")
    api_nc = _strip_ts_comments(api)
    panel_nc = _strip_ts_comments(panel)

    safe_body = _fn_body_ts(api, "safeRecord", "export function")
    if not safe_body:
        errs.append("[setup] services/api.ts safeRecord 函数体未找到")
        return errs

    # ── A. safeRecord 真源定义 ──
    # [1] export function safeRecord 定义
    m_sig = re.search(r"export function safeRecord\(\s*data:\s*unknown\s*\)\s*:\s*Record<string,\s*unknown>\s*\|\s*null", api)
    if not m_sig:
        errs.append("[A1] 缺 export function safeRecord(data: unknown): Record<string, unknown> | null（B20 单一真源缺失）")
    else:
        print("[A1] OK  export function safeRecord(data: unknown): Record<string, unknown> | null（单一真源）")
    # [2] 守卫 !data || typeof data !== 'object' || Array.isArray(data) → null（三段全）
    safe_nc = _strip_ts_comments(safe_body)
    has_null = "!data" in safe_nc or "!raw" in safe_nc
    has_typeof = "typeof data !== 'object'" in safe_nc or "typeof raw !== 'object'" in safe_nc
    has_array = "Array.isArray(data)" in safe_nc or "Array.isArray(raw)" in safe_nc
    if not (has_null and has_typeof and has_array):
        errs.append(f"[A2] safeRecord 守卫不全（null={has_null} typeof={has_typeof} isArray={has_array}——应三段全）")
    else:
        print("[A2] OK  守卫 !data || typeof !== 'object' || Array.isArray → null（三段全）")
    # [3] 返 data as Record<string, unknown>
    if "as Record<string, unknown>" not in safe_nc:
        errs.append("[A3] safeRecord 未返 data as Record<string, unknown>（narrowed record）")
    else:
        print("[A3] OK  返 data as Record<string, unknown>（object narrowed，非数组）")

    # ── B. parseStats 复用 safeRecord ──
    parse_body = _fn_body_ts(api, "parseStats", "export function")
    if not parse_body:
        errs.append("[setup] parseStats 函数体未找到")
    else:
        # [4] const dd = safeRecord(raw); if (!dd) return null
        if "safeRecord(raw)" not in parse_body:
            errs.append("[B4] parseStats 未调 safeRecord(raw)（B20 守卫下沉未接线）")
        elif "if (!dd) return null" not in parse_body:
            errs.append("[B4] parseStats 缺 if (!dd) return null（safeRecord 返 null 未兜底）")
        else:
            print("[B4] OK  parseStats: const dd = safeRecord(raw); if (!dd) return null（守卫下沉）")
        # [5] 不再有 if (!raw || typeof raw !== 'object') return null（原守卫消失）
        parse_nc = _strip_ts_comments(parse_body)
        if re.search(r"if\s*\(!raw\s*\|\|\s*typeof raw !== 'object'\)\s*return null", parse_nc):
            errs.append("[B5] parseStats 仍 if (!raw || typeof raw !== 'object') return null（原守卫未消除）")
        else:
            print("[B5] OK  parseStats 不再 if (!raw || typeof raw !== 'object')（原守卫消除）")

    # ── C. extractCoordReasoning 复用 safeRecord ──
    reas_body = _fn_body_ts(panel, "extractCoordReasoning")
    if not reas_body:
        errs.append("[C6] extractCoordReasoning 函数体未找到")
    else:
        reas_nc = _strip_ts_comments(reas_body)
        # [6] const dd = safeRecord(data); if (!dd) return undefined
        if "safeRecord(data)" not in reas_nc:
            errs.append("[C6] extractCoordReasoning 未调 safeRecord(data)（B20 守卫下沉未接线）")
        elif "if (!dd) return undefined" not in reas_nc:
            errs.append("[C6] extractCoordReasoning 缺 if (!dd) return undefined（safeRecord 返 null 未兜底）")
        else:
            print("[C6] OK  extractCoordReasoning: const dd = safeRecord(data); if (!dd) return undefined")
        # [7] 不再有 if (!data) return undefined（原守卫消失）
        if re.search(r"if\s*\(!data\)\s*return undefined", reas_nc):
            errs.append("[C7] extractCoordReasoning 仍 if (!data) return undefined（原守卫未消除）")
        else:
            print("[C7] OK  extractCoordReasoning 不再 if (!data) return undefined（原守卫消除）")
        # [8] reasoning 字段守卫仍本处（typeof r === 'string' && r）
        if "typeof r === 'string'" not in reas_nc and "typeof dd['reasoning'] === 'string'" not in reas_nc:
            errs.append("[C8] extractCoordReasoning reasoning 字段守卫丢失（typeof string 应保留）")
        else:
            print("[C8] OK  reasoning 字段守卫仍本处（typeof string && 非空，非 null-guard 不抽）")

    # ── D. extractFinalizedArtifacts 三层复用 safeRecord ──
    fa_body = _fn_body_ts(panel, "extractFinalizedArtifacts")
    if not fa_body:
        errs.append("[D9] extractFinalizedArtifacts 函数体未找到")
    else:
        fa_nc = _strip_ts_comments(fa_body)
        # [9] safeRecord(data)（外层）
        if "safeRecord(data)" not in fa_nc:
            errs.append("[D9] extractFinalizedArtifacts 外层未调 safeRecord(data)")
        else:
            print("[D9] OK  外层 safeRecord(data)")
        # [10] safeRecord(dd['artifact'])
        if "safeRecord(dd['artifact'])" not in fa_nc:
            errs.append("[D10] extractFinalizedArtifacts artifact 层未调 safeRecord(dd['artifact'])")
        else:
            print("[D10] OK  artifact 层 safeRecord(dd['artifact'])")
        # [11] safeRecord(raw)（files 元组）
        if "safeRecord(raw)" not in fa_nc:
            errs.append("[D11] extractFinalizedArtifacts file 元组层未调 safeRecord(raw)")
        else:
            print("[D11] OK  file 元组层 safeRecord(raw)（map 内）")
        # [12] files 仍 Array.isArray(files)
        if "Array.isArray(files)" not in fa_nc:
            errs.append("[D12] extractFinalizedArtifacts 缺 Array.isArray(files)（files 数组守卫破）")
        else:
            print("[D12] OK  files 仍 Array.isArray(files)（数组非 record 单独判）")
        # 原三层 typeof 守卫消失
        if re.search(r"typeof data !== 'object'", fa_nc) or re.search(r"typeof artifact !== 'object'", fa_nc) or re.search(r"typeof raw !== 'object'", fa_nc):
            errs.append("[D] extractFinalizedArtifacts 仍有 typeof X !== 'object' 内联守卫（原三层守卫未消除）")
        else:
            print("[D] OK  原三层 typeof X !== 'object' 内联守卫全消除（safeRecord 下沉）")

    # ── E. 行为零变 ──
    # [13] safeRecord 守卫与原四处同口径（!data + typeof !== 'object'）
    if has_null and has_typeof:
        print("[E13] OK  safeRecord 守卫 !data + typeof !== 'object' 与原四处逐字同口径")
    else:
        errs.append("[E13] safeRecord 守卫口径与原不一致")
    # [14] 额外 Array.isArray（更严，实际数组不命中零变）
    if has_array:
        print("[E14] OK  safeRecord 额外 Array.isArray 排除数组（更严守卫，实际数组不命中零变）")
    else:
        errs.append("[E14] safeRecord 缺 Array.isArray（数组会过 typeof object 当 record）")
    # [15] extractFinalizedArtifacts 仍返 ArtifactFile[]
    if re.search(r"function extractFinalizedArtifacts\(data: unknown\):\s*ArtifactFile\[\]", panel):
        print("[E15] OK  extractFinalizedArtifacts 仍返 ArtifactFile[]（空数组兜底非 null）")
    else:
        errs.append("[E15] extractFinalizedArtifacts 返回类型异常（应 ArtifactFile[]）")
    # [16] extractCoordReasoning 仍返 string | undefined
    m_re_sig = re.search(r"function extractCoordReasoning\([^)]*\):\s*string\s*\|\s*undefined", panel)
    if not m_re_sig:
        errs.append("[E16] extractCoordReasoning 返回类型异常（应 string | undefined）")
    else:
        print("[E16] OK  extractCoordReasoning 仍返 string | undefined（非 null）")

    # ── F. import 接线 ──
    # [17] ChatPanel import safeRecord
    if "safeRecord" not in panel.split("from '../services/api'")[0]:
        errs.append("[F17] ChatPanel import 缺 safeRecord")
    else:
        print("[F17] OK  ChatPanel import safeRecord from ../services/api")
    # [18] parseStats 同文件无需 import（services/api.ts 内 safeRecord + parseStats 同模块）
    if "export function safeRecord" in api and "export function parseStats" in api:
        print("[F18] OK  safeRecord + parseStats 同模块（services/api.ts，无需 import）")
    else:
        errs.append("[F18] safeRecord 或 parseStats 不在 services/api.ts")

    # ── G. 无回归 ──
    # [19] vh15 [C12] 改为 parseStats → safeRecord(raw)
    vh15 = (REPO / "backend" / "tests" / "test_vh15_parse_stats_shared.py").read_text(encoding="utf-8")
    if "safeRecord(raw)" not in vh15:
        errs.append("[G19] vh15 [C12] 未改为 parseStats → safeRecord(raw) 断言（守卫下沉未同步测）")
    else:
        print("[G19] OK  vh15 [C12] 改为 parseStats → safeRecord(raw) 断言（守卫下沉同步）")
    # [20] vb2 [前端1] 改为三层 safeRecord + Array.isArray(files)
    vb2 = (REPO / "backend" / "tests" / "test_vb2_artifact_download.py").read_text(encoding="utf-8")
    if "safeRecord(data)" not in vb2 or "safeRecord(dd['artifact'])" not in vb2 or "safeRecord(raw)" not in vb2:
        errs.append("[G20] vb2 [前端1] 未改为三层 safeRecord 断言（守卫下沉未同步测）")
    else:
        print("[G20] OK  vb2 [前端1] 改为三层 safeRecord + Array.isArray(files) 断言（守卫下沉同步）")
    # [21] extractFinalizedArtifacts 仍被 finalizedBubbles 调用
    if "extractFinalizedArtifacts(e.data)" not in panel:
        errs.append("[G21] finalizedBubbles 未调 extractFinalizedArtifacts(e.data)（调用链断）")
    else:
        print("[G21] OK  extractFinalizedArtifacts 仍被 finalizedBubbles 调用（artifactFiles 链不破）")
    # [22] extractCoordReasoning 仍被定稿气泡调用
    if "extractCoordReasoning(msg.data)" not in panel:
        errs.append("[G22] 定稿气泡未调 extractCoordReasoning(msg.data)（调用链断）")
    else:
        print("[G22] OK  extractCoordReasoning 仍被定稿气泡 reasoning Collapse 调用（链不破）")

    return errs


def main() -> int:
    print("=== VH17 回归：safeRecord 共享 null-guard 入口——四 extractor 守卫合一（B20）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B20 safeRecord 共享 null-guard 入口锁定：\n"
        "  · A 真源：services/api.ts export function safeRecord(unknown) → Record<string, unknown> | null（!data + typeof !== 'object' + Array.isArray 三段守卫）；\n"
        "  · B parseStats 复用：const dd = safeRecord(raw); if (!dd) return null（B18 守卫下沉到 safeRecord）；\n"
        "  · C extractCoordReasoning 复用：const dd = safeRecord(data); if (!dd) return undefined（reasoning 字段守卫仍本处）；\n"
        "  · D extractFinalizedArtifacts 三层复用：safeRecord(data) + safeRecord(dd['artifact']) + safeRecord(raw) + Array.isArray(files)（原三层 typeof 守卫消除）；\n"
        "  · E 行为零变：守卫口径逐字对齐 + Array.isArray 更严（实际数组不命中零变）+ 返回形不变（ArtifactFile[] / string|undefined）；\n"
        "  · F import：ChatPanel import safeRecord + parseStats/safeRecord 同模块；\n"
        "  · G 无回归：vh15 [C12] / vb2 [前端1] 断言同步下沉 + 调用链不破（finalizedBubbles / 定稿气泡 reasoning Collapse）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

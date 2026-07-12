"""VH18 回归：dateKey 与 dateLabel 月份口径对齐——0-index 改 +1 显式同口径（task B21）.

锁住 B21 修复——``src/components/ChatPanel.tsx`` 日期分隔条链路两个函数的月份口径
隐式耦合：

  - ``dateKey(iso)``：返回 ``${y}-${getMonth()}-${getDate()}``——原用 ``getMonth()``
    0-indexed 不 +1。返回值仅用于 ``dateKey(prevIso) === dateKey(iso)`` 相等比较，
    从不展示给人看——故 0-index/1-index 对比较结果本无影响（两 key 都用 0-index 比较仍正确）。
  - ``dateLabel(iso)``：展示 ``${getMonth()+1}月${getDate()}日``——``getMonth()+1``
    1-indexed 月展示。

两函数都取「本地年月日」，但 dateKey 0-index + dateLabel 1-index——口径隐式耦合：
肉眼读 dateKey 的 ``${getMonth()}`` 会误以为是 1-index（与 dateLabel 看似不一致），
未来若有人改 dateKey 用 ``getMonth()+1``（对齐 dateLabel），或改 dateLabel 用 ``getMonth()``
（误删 +1），比较口径与展示口径就真脱钩。B21 改 dateKey 也 +1，与 dateLabel 显式同口径
（两处都 1-indexed 月），一处改另一处忘改则肉眼可见不一致。

为何不选 ``Date.toISOString().slice(0,10)``（任务给的另一选项）：
  ``toISOString`` 返 UTC 日期，会与 dateLabel 的本地「今天/昨天」判定在非 UTC 时区
  跨日边界处脱钩——本地晚 11 点发的消息 toISOString 已是次日 UTC，dateKey 用 UTC
  判同日、dateLabel 用本地判「今天」，两函数对同一条消息给出不同日期 → 分隔条漏渲染
  或误渲染。故 B21 选「显式 +1」（两函数同本地口径），非「toISOString UTC」。

  具体反例（北京 UTC+8）：
    iso = '2026-07-13T17:00:00+08:00'  # 本地 7-13 晚 5 点
    toISOString → '2026-07-13T09:00:00.000Z' → slice(0,10) = '2026-07-13'  # UTC 同日 ✓
    iso = '2026-07-13T23:30:00+08:00'  # 本地 7-13 晚 11:30
    toISOString → '2026-07-13T15:30:00.000Z' → slice(0,10) = '2026-07-13'  # UTC 仍 7-13 ✓
    iso = '2026-07-14T02:00:00+08:00'  # 本地 7-14 凌晨 2 点
    toISOString → '2026-07-13T18:00:00.000Z' → slice(0,10) = '2026-07-13'  # UTC 7-13 但本地 7-14！
    → dateKey(UTC) = '2026-07-13'，dateLabel(本地) 算 today=7-14 diff=1 → 「昨天」，
      但 dateKey 说与昨晚(7-13)同日不分隔 → 漏渲染分隔条。脱钩实锤。

午夜锚点 ``new Date(y, getMonth(), d)`` 在 dateLabel 内算日差——Date 构造器要求 0-indexed
月（``new Date(2026, 6, 13)`` = 7 月 13 日），故锚点用 ``getMonth()``（构造器口径非展示口径）。
B21 只改展示/比较口径（dateKey 的返回值 + dateLabel 的展示串）为 +1，午夜锚点构造器口径不动
（构造器就是要 0-indexed，加 +1 反而错——``new Date(2026, 7, 13)`` = 8 月 13 日）。

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh17 同款风格。

五段契约：

  A. dateKey 改 +1（与 dateLabel 显式同口径）
    1. dateKey 返回串含 ``getMonth() + 1``（1-indexed 月，与 dateLabel 对齐）。
    2. dateKey 不再有裸 ``getMonth()``（0-indexed 原口径消失）——区分锚点构造器口径。

  B. dateLabel 口径不变（+1 保留）
    3. dateLabel 展示串仍含 ``getMonth() + 1``（1-indexed 月展示，B21 未动）。
    4. dateLabel 午夜锚点 ``new Date(y, getMonth(), d)`` 用 0-indexed（构造器要求，非展示口径）。

  C. 两函数同口径（显式耦合消除）
    5. dateKey 与 dateLabel 都含 ``getMonth() + 1``（两处 1-indexed 月，口径显式一致）。
    6. dateKey 与 dateLabel 都从同一 ``new Date(iso)`` 取本地年月日（共用本地时区口径）。

  D. 行为零变（比较语义不变）
    7. dateKey 仍返回 ``string``（用于 === 比较，非展示）。
    8. dateKey 仍被 renderDateDivider 调用（``dateKey(prevIso) === dateKey(iso)`` 比较链不破）。
    9. dateLabel 仍被 renderDateDivider 调用（分隔条标签渲染不破）。

  E. 不用 toISOString().slice(0,10)（UTC 脱钩规避）
   10. dateKey 不含 ``toISOString``（UTC 日期会与本地 dateLabel 跨日脱钩）。
   11. dateKey 函数体注释说明为何不用 toISOString（UTC 与本地口径脱钩的决策记录）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
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
    panel = PANEL.read_text(encoding="utf-8")

    date_key_body = _fn_body_ts(panel, "dateKey")
    date_label_body = _fn_body_ts(panel, "dateLabel")
    if not date_key_body:
        errs.append("[setup] dateKey 函数体未找到"); return errs
    if not date_label_body:
        errs.append("[setup] dateLabel 函数体未找到"); return errs

    date_key_nc = _strip_ts_comments(date_key_body)
    date_label_nc = _strip_ts_comments(date_label_body)

    # ── A. dateKey 改 +1 ──
    # [1] dateKey 返回串含 getMonth() + 1
    if "getMonth() + 1" not in date_key_nc:
        errs.append("[A1] dateKey 未用 getMonth() + 1（仍 0-indexed，与 dateLabel 口径未对齐）")
    else:
        print("[A1] OK  dateKey 返回串含 getMonth() + 1（1-indexed 月，与 dateLabel 对齐）")
    # [2] dateKey 不再有裸 getMonth()（0-indexed 原口径消失）
    # 裸 getMonth() = getMonth() 后不跟 +1。用正则排除 ``getMonth() + 1``。
    bare_month = re.search(r"getMonth\(\)(?!\s*\+\s*1)", date_key_nc)
    if bare_month:
        errs.append("[A2] dateKey 仍有裸 getMonth()（0-indexed 原口径未消除——区分锚点构造器口径）")
    else:
        print("[A2] OK  dateKey 无裸 getMonth()（0-indexed 原口径消除，月一律 +1）")

    # ── B. dateLabel 口径不变 ──
    # [3] dateLabel 展示串仍含 getMonth() + 1
    if "getMonth() + 1" not in date_label_nc:
        errs.append("[B3] dateLabel 展示串丢 getMonth() + 1（1-indexed 月展示被误改）")
    else:
        print("[B3] OK  dateLabel 展示串仍含 getMonth() + 1（1-indexed 月展示，B21 未动）")
    # [4] dateLabel 午夜锚点 new Date(y, getMonth(), d) 用 0-indexed（构造器要求）
    # 锚点 = new Date(now.getFullYear(), now.getMonth(), now.getDate()) —— getMonth() 不 +1。
    # 注意 Date 构造器实参内嵌 getFullYear()/getDate() 各自带 ``)``，不能用 ``[^)]*`` 贪婪
    # （会在第一个内层 ``)`` 处截断漏掉 getMonth）。改用 ``[^;]*`` 跨实参匹配整行锚点。
    has_anchor = bool(re.search(r"new Date\([^;]*getMonth\(\)[^;]*\)", date_label_nc))
    if not has_anchor:
        errs.append("[B4] dateLabel 午夜锚点 new Date(..., getMonth(), ...) 缺失（日差计算锚点破）")
    else:
        # 锚点的 getMonth() 必须是裸的（构造器 0-indexed，非 +1）
        anchor_bare = bool(re.search(r"new Date\([^;]*getMonth\(\)(?!\s*\+\s*1)[^;]*\)", date_label_nc))
        if not anchor_bare:
            errs.append("[B4] dateLabel 午夜锚点 getMonth() 误 +1（构造器要 0-indexed，+1 会跳月）")
        else:
            print("[B4] OK  dateLabel 午夜锚点 new Date(..., getMonth(), ...) 用 0-indexed（构造器口径）")

    # ── C. 两函数同口径 ──
    # [5] dateKey 与 dateLabel 都含 getMonth() + 1
    if "getMonth() + 1" in date_key_nc and "getMonth() + 1" in date_label_nc:
        print("[C5] OK  dateKey + dateLabel 都含 getMonth() + 1（两处 1-indexed 月，口径显式一致）")
    else:
        errs.append("[C5] dateKey 或 dateLabel 缺 getMonth() + 1（两函数月口径未显式对齐）")
    # [6] dateKey 与 dateLabel 都从 new Date(iso) 取本地年月日
    if "new Date(iso)" in date_key_nc and "new Date(iso)" in date_label_nc:
        print("[C6] OK  dateKey + dateLabel 都从 new Date(iso) 取本地年月日（共用本地时区口径）")
    else:
        errs.append("[C6] dateKey 或 dateLabel 未从 new Date(iso) 取值（本地时区口径共用破）")

    # ── D. 行为零变 ──
    # [7] dateKey 仍返回 string
    m_sig = re.search(r"function dateKey\(iso:\s*string\)\s*:\s*string", panel)
    if not m_sig:
        errs.append("[D7] dateKey 签名异常（应 (iso: string): string）")
    else:
        print("[D7] OK  dateKey 仍返 string（用于 === 比较，非展示）")
    # [8] dateKey 仍被 renderDateDivider 调用（比较链不破）
    rdd_body = _fn_body_ts(panel, "renderDateDivider")
    if not rdd_body or "dateKey(prevIso)" not in rdd_body or "dateKey(iso)" not in rdd_body:
        errs.append("[D8] renderDateDivider 未调 dateKey(prevIso)/dateKey(iso)（比较链断）")
    else:
        print("[D8] OK  renderDateDivider 仍调 dateKey(prevIso) === dateKey(iso)（比较链不破）")
    # [9] dateLabel 仍被 renderDateDivider 调用（分隔条标签不破）
    if not rdd_body or "dateLabel(iso)" not in rdd_body:
        errs.append("[D9] renderDateDivider 未调 dateLabel(iso)（分隔条标签渲染断）")
    else:
        print("[D9] OK  renderDateDivider 仍调 dateLabel(iso)（分隔条标签渲染不破）")

    # ── E. 不用 toISOString().slice(0,10) ──
    # [10] dateKey 不含 toISOString
    if "toISOString" in date_key_nc:
        errs.append("[E10] dateKey 含 toISOString（UTC 日期会与本地 dateLabel 跨日脱钩）")
    else:
        print("[E10] OK  dateKey 不含 toISOString（规避 UTC 与本地口径脱钩）")
    # [11] dateKey 函数体注释说明为何不用 toISOString
    # 注释在函数体上方的 docstring（/** ... */）——查 dateKey 上方块注释含 toISOString。
    # 取 dateKey 定义前一行的块注释。
    m_doc = re.search(r"/\*\*(.*?)\*/\s*function dateKey", panel, re.S)
    doc_text = m_doc.group(1) if m_doc else ""
    if "toISOString" not in doc_text:
        errs.append("[E11] dateKey 块注释未说明为何不用 toISOString（决策记录缺失）")
    else:
        print("[E11] OK  dateKey 块注释说明不用 toISOString（UTC 脱钩决策记录）")

    return errs


def main() -> int:
    print("=== VH18 回归：dateKey 与 dateLabel 月份口径对齐——0-index 改 +1 显式同口径（B21）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B21 dateKey 与 dateLabel 月份口径对齐锁定：\n"
        "  · A dateKey 改 +1：返回串含 getMonth() + 1（1-indexed 月，与 dateLabel 对齐）+ 无裸 getMonth()；\n"
        "  · B dateLabel 不变：展示串仍 getMonth() + 1 + 午夜锚点 new Date(..., getMonth(), ...) 用 0-indexed（构造器口径）；\n"
        "  · C 同口径：dateKey + dateLabel 都 getMonth() + 1 + 都从 new Date(iso) 取本地年月日；\n"
        "  · D 行为零变：dateKey 仍返 string + renderDateDivider 仍调 dateKey 比较 + dateLabel 标签；\n"
        "  · E 不用 toISOString：规避 UTC 与本地 dateLabel 跨日脱钩 + 块注释记录决策。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

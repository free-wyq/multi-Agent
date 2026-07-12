"""VH5 回归：route_after_dispatch 死分支清理（task B8）.

锁住 B8 修复——``route_after_dispatch`` 原条件
``if action in ("dispatch_next", "confirm_dispatch", "direct_run"): return "dispatch_next"``
含两个死成员：``confirm_dispatch`` 只由 ``node_classify_incoming`` 产生、由
``route_after_classify`` 直接路由到 ``dispatch_next``，**永不到达 dispatch 节点**；
``direct_run`` 从未被任何节点产生（仅测试注释提及）。``wait_confirm`` 哨兵早在 7564caf
移除，但 node_dispatch docstring 仍自承「``action_taken`` 留为 ``wait_confirm``（inert）」，
是过时注释——B8 一并清理。

B8 后：``route_after_dispatch`` 只判 ``dispatch_next``，其余 fall-through END。node_dispatch
docstring 删除「wait_confirm sentinel」自承，改为「interrupt 暂停 mid-node，条件边不评估」。
state.py 的 action_taken 枚举注释删 ``wait_confirm``。

为何纯静态：
  ``route_after_dispatch`` 是纯路由函数（state → str），死分支是「逻辑可达性」契约——
  ``confirm_dispatch``/``direct_run`` 是否被 dispatch 节点产生是代码锚定的事实（grep 全仓
  ``action_taken="..."`` 赋值即证）。静态契约锁「route 条件只含 dispatch_next + 两个死成员
  无产生源」比运行时实测更可靠（实测需模拟一个永不发生的 action 才能触发死分支）。

六段契约：

  A. route_after_dispatch 条件收敛到 dispatch_next
    1. route 条件是 ``if action == "dispatch_next"``（非 ``in (...)`` 多元组）。
    2. route 不再含 ``confirm_dispatch``（死成员已删）。
    3. route 不再含 ``direct_run``（死成员已删）。
    4. fall-through 返回 END（防御性默认保留）。

  B. confirm_dispatch 不由 dispatch 节点产生（死成员证据）
    5. node_dispatch 函数体无 ``"confirm_dispatch"`` 赋值（只 node_classify_incoming 产生）。
    6. confirm_dispatch 由 route_after_classify 路由（不经 dispatch 节点）。

  C. direct_run 不由任何节点产生（死成员证据）
    7. engine/ 源码无 ``action_taken" = "direct_run"`` / ``"direct_run"`` 赋值（仅测试注释）。

  D. wait_confirm 哨兵已清理（node_dispatch docstring + 代码）
    8. node_dispatch 函数体无 ``"wait_confirm"`` 赋值（sentinel 早在 7564caf 移除）。
    9. node_dispatch docstring 不再自承「action_taken 留为 wait_confirm」（B8 清理过时注释）。

  E. state.py action_taken 枚举注释收敛
   10. state.py action_taken 注释不含 ``wait_confirm``（B8 一并清理枚举）。
   11. state.py action_taken 注释仍含 ``confirm_dispatch``（活的——node_classify_incoming 产生）。

  F. 路由行为零变（B8 纯删死分支）
   12. dispatch_next 仍路由到 "dispatch_next"（正常路径不变）。
   13. 非 dispatch_next 仍路由到 END（防御默认不变，覆盖原 confirm_dispatch/direct_run
       的 fall-through——它们本就 fall-through 到同一 END，删后行为等价）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COORD = REPO / "backend" / "engine" / "coordinator.py"
STATE = REPO / "backend" / "engine" / "state.py"


def _fn_body(src: str, fname: str) -> str:
    """抽 def fname(...) 到下一个顶层 def 的函数体（含 docstring）。"""
    m = re.search(
        rf"(?:async def|def) {fname}\([^)]*\)(.*?)(?=\n(?:async )?def )",
        src,
        re.S,
    )
    return m.group(1) if m else ""


def _body_no_doc(src: str, fname: str) -> str:
    """抽函数体并剔除 docstring（只留可执行代码，判死分支赋值用）。"""
    body = _fn_body(src, fname)
    return re.sub(r'""".*?"""', "", body, flags=re.S)


def assert_contract() -> list[str]:
    errs: list[str] = []
    coord = COORD.read_text(encoding="utf-8")
    state = STATE.read_text(encoding="utf-8")

    route_body = _fn_body(coord, "route_after_dispatch")
    if not route_body:
        errs.append("[setup] route_after_dispatch 函数体未找到")
        return errs

    # ── A. route 条件收敛到 dispatch_next ──
    # route 函数体含 docstring；先剔 docstring 再判可执行代码是否含死成员字面量，
    # 否则 docstring 里解释「为何删 confirm_dispatch/direct_run」的历史说明会被误判为残留。
    route_code = re.sub(r'""".*?"""', "", route_body, flags=re.S)
    # [1] route 条件是 if action == "dispatch_next"（非 in (...) 多元组）
    m_cond = re.search(r'if\s+action\s*==\s*"dispatch_next"\s*:', route_code)
    m_in_tuple = re.search(r'if\s+action\s+in\s*\(\s*"dispatch_next"', route_code)
    if not m_cond:
        errs.append("[A1] route 条件非 if action == 'dispatch_next'（未收敛到单一值）")
    elif m_in_tuple:
        errs.append("[A1] route 仍用 in (...) 多元组（应收敛到 == 'dispatch_next'）")
    else:
        print("[A1] OK  route 条件 if action == 'dispatch_next'（收敛单一值）")

    # [2] route 可执行代码不再含 confirm_dispatch
    if "confirm_dispatch" in route_code:
        errs.append("[A2] route 可执行代码仍含 confirm_dispatch（死成员未删）")
    else:
        print("[A2] OK  route 可执行代码不含 confirm_dispatch（死成员已删）")

    # [3] route 可执行代码不再含 direct_run
    if "direct_run" in route_code:
        errs.append("[A3] route 可执行代码仍含 direct_run（死成员未删）")
    else:
        print("[A3] OK  route 可执行代码不含 direct_run（死成员已删）")

    # [4] fall-through 返回 END
    if "return END" not in route_code:
        errs.append("[A4] route 无 return END（防御性默认丢失）")
    else:
        print("[A4] OK  fall-through return END（防御性默认保留）")

    # ── B. confirm_dispatch 不由 dispatch 节点产生 ──
    # [5] node_dispatch 函数体（剔 docstring）无 "confirm_dispatch" 赋值
    dispatch_code = _body_no_doc(coord, "node_dispatch")
    if '"confirm_dispatch"' in dispatch_code:
        errs.append("[B5] node_dispatch 可执行代码含 confirm_dispatch（不应产生此值）")
    else:
        print("[B5] OK  node_dispatch 不产生 confirm_dispatch（只 classify 产生）")

    # [6] confirm_dispatch 由 route_after_classify 路由（不经 dispatch 节点）
    classify_route = _fn_body(coord, "route_after_classify")
    if "confirm_dispatch" not in classify_route:
        errs.append("[B6] route_after_classify 未路由 confirm_dispatch（死成员证据链断）")
    else:
        print("[B6] OK  confirm_dispatch 由 route_after_classify 路由（不经 dispatch 节点→死成员）")

    # ── C. direct_run 不由任何节点产生 ──
    # [7] engine/ 源码无 "direct_run" 赋值（仅测试注释提及）
    # 全仓 engine/ grep（本测读 coordinator.py，但 direct_run 若被产生必在 coordinator 节点）
    coord_nodes_code = re.sub(r'""".*?"""', "", coord, flags=re.S)
    # 排除 route_after_dispatch 的历史 tuple（B8 已删，但双重确认无残留）
    # 找所有 action_taken 赋值
    direct_run_assign = re.search(r'action_taken["\']?\s*[,:=]\s*["\']direct_run["\']', coord_nodes_code)
    if direct_run_assign:
        errs.append("[C7] coordinator 源码含 action_taken='direct_run' 赋值（应无产生源）")
    else:
        print("[C7] OK  coordinator 源码无 action_taken='direct_run' 赋值（direct_run 无产生源→死成员）")

    # ── D. wait_confirm 哨兵已清理 ──
    # [8] node_dispatch 可执行代码无 "wait_confirm" 赋值
    if '"wait_confirm"' in dispatch_code:
        errs.append("[D8] node_dispatch 可执行代码含 wait_confirm 赋值（sentinel 应早在 7564caf 移除）")
    else:
        print("[D8] OK  node_dispatch 无 wait_confirm 赋值（sentinel 已移除）")

    # [9] node_dispatch docstring 不再自承「action_taken 留为 wait_confirm」
    dispatch_doc_m = re.search(r'async def node_dispatch\([^)]*\).*?""".*?"""', coord, re.S)
    dispatch_doc = dispatch_doc_m.group(0) if dispatch_doc_m else ""
    # B8 后 docstring 可提及 wait_confirm 作为「已移除」的历史说明，但不应再自承「留为 wait_confirm」
    if re.search(r'action_taken["\']?\s*["\']?留为?\s*["\']?wait_confirm|left as.*wait_confirm', dispatch_doc, re.I):
        errs.append("[D9] node_dispatch docstring 仍自承「action_taken 留为 wait_confirm」（B8 过时注释未清理）")
    else:
        print("[D9] OK  node_dispatch docstring 不自承「留为 wait_confirm」（B8 过时注释已清理）")

    # ── E. state.py action_taken 枚举注释收敛 ──
    # [10] state.py action_taken 注释不含 wait_confirm
    m_action = re.search(r"action_taken:\s*str\s*#(.*)", state)
    if not m_action:
        errs.append("[E10] state.py 未找到 action_taken: str 注释行")
    elif "wait_confirm" in m_action.group(1):
        errs.append("[E10] state.py action_taken 注释仍含 wait_confirm（枚举未收敛）")
    else:
        print("[E10] OK  state.py action_taken 注释不含 wait_confirm（枚举收敛）")

    # [11] state.py action_taken 注释仍含 confirm_dispatch（活的）
    if m_action and "confirm_dispatch" not in m_action.group(1):
        errs.append("[E11] state.py action_taken 注释丢失 confirm_dispatch（活值不应删）")
    else:
        print("[E11] OK  state.py action_taken 注释含 confirm_dispatch（活值保留——classify 产生）")

    # ── F. 路由行为零变 ──
    # [12] dispatch_next 仍路由到 "dispatch_next"
    if re.search(r'action\s*==\s*"dispatch_next"\s*:\s*\n\s*return\s+"dispatch_next"', route_body) is None:
        errs.append("[F12] dispatch_next 未路由到 'dispatch_next'（正常路径回归）")
    else:
        print("[F12] OK  dispatch_next → 'dispatch_next'（正常路径不变）")

    # [13] 非 dispatch_next 仍路由到 END（删 confirm_dispatch/direct_run 后行为等价）
    # 原 in (dispatch_next, confirm_dispatch, direct_run) 时三者都返 dispatch_next，
    # 其余 fall END。删后只有 dispatch_next 返 dispatch_next，其余（含原不在 tuple 的）返 END。
    # confirm_dispatch/direct_run 永不至此函数（前者 classify 路由、后者无产生源），故删后等价。
    if not re.search(r'return\s+END', route_body):
        errs.append("[F13] route 无 return END（防御默认丢失，行为回归）")
    else:
        print("[F13] OK  非 dispatch_next → END（删死成员后行为等价：confirm_dispatch 经 classify 路由、direct_run 无产生源）")

    return errs


def main() -> int:
    print("=== VH5 回归：route_after_dispatch 死分支清理 ===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "VH5 回归契约锁定（B8 清理不退化）：\n"
        "  · A route_after_dispatch 条件收敛到 if action == 'dispatch_next'，删 confirm_dispatch/"
        "direct_run 两个死成员，保留 fall-through END 防御默认；\n"
        "  · B confirm_dispatch 只由 node_classify_incoming 产生、route_after_classify 路由，"
        "不经 dispatch 节点（死成员证据）；\n"
        "  · C direct_run 无任何产生源（仅测试注释提及，死成员证据）；\n"
        "  · D node_dispatch 无 wait_confirm 赋值（sentinel 早在 7564caf 移除）+ docstring 不再自承"
        "「留为 wait_confirm」（B8 清理过时注释）；\n"
        "  · E state.py action_taken 枚举注释删 wait_confirm、保留 confirm_dispatch（活值）；\n"
        "  · F 路由行为零变：dispatch_next→'dispatch_next' 不变，非 dispatch_next→END 不变"
        "（删死成员后行为等价，confirm_dispatch 经 classify 路由、direct_run 无产生源）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

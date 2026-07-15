"""VH1 回归：A2A @mention 来回对话不退化（task B2）.

锁住 `plans/composed-doodling-creek.md` 已落地的 A2A 来回对话机制——防 route_mentions
退回 push_task（旧 bug：peer 被塞进 execute 重路径，接龙接不起来 + 产物幻觉）。
纯静态契约（读源码断言，不依赖后端在线），与 test_va2/va3/va6/vg1/vg2 同款风格。

四段契约（task B2 明列的四个锁定点）：

  A. route_mentions 用 push_notify（非 push_task）—— 来回走 brain→chat 轻路径
    1. route_mentions 函数体内调 ``await push_notify(...)``（非 push_task）。
    2. route_mentions 函数体内**不**调 ``await push_task``（防退回旧 bug：peer 被塞 execute
       重路径 create_react_agent，接龙断链 + 产物幻觉）。
    3. push_notify 的 kind 是 "agent_reply"（peer 的 brain 能看到 incoming_sender/message，
       _format_display_msg 加 [来自智能体 X] 前缀）。

  B. 反向清键允许 A→B→A→B 持续交替
    4. 成功 push 后 ``recent_routes.pop(f"{target_id}->{sender_id}", None)``——清反向 key，
       让 B→A 之后 A→B 不被 30s 内已存在拦死。
    5. 反向清键在 ``recent_routes[key] = now``（设正向 key）之后——顺序正确：先记正向再清反向。
    6. 防循环 dict 是群级共享（``recent_routes is None`` 时取 ``_get_recent_routes(group_id)``，
       非 per-engine）——per-engine 反向清键打不中对方 dict → 接龙 4 轮断。

  C. 同方向 30s 连发被拦（防死循环）
    7. ``if key in recent_routes: continue``——同方向 30s 内已路由则跳过（A 连发两次 @B 挡掉）。
    8. 30s 过期清理：``stale = [k for k,t in recent_routes.items() if now - t >= 30.0]`` + pop。
       ——反例防退化：不能只反向清键不查同方向（那样 A 连发两次 @B 会无限路由）；也不能只查
       同方向不反向清键（那样来回只能 2 轮就死）。两者必须同在。

  D. route_user_message 不再依赖 _a2a_turns（task-19 去中心化路径）
    9. route_user_message 切 GroupRuntime.invoke_turn 后，不再清 ``_a2a_turns[group_id]=0``
       （入站话筒交给群图 handoff 边 + GroupState.turn_count / recent_speakers 防连发，
       旧 _a2a_turns 预算重置只服务 route_mentions 的 legacy 防循环——双轨期保留）。
       断言锁定：route_user_message 函数体不含 ``_a2a_turns[group_id] = 0``。
   10. _A2A_CAP env 可调 + 默认 50（``os.environ.get("MULTI_AGENT_A2A_TURNS", "50")``）。
   11. route_mentions 达 cap 不再 push（``if _a2a_turns.get(group_id,0) >= _A2A_CAP: return``）。
   12. route_mentions push 后 ``_a2a_turns[group_id] += 1``（计数累加，cap 才有依据）。

  E. DAG 调度不受影响（dispatcher 直调 push_task 不经 route_mentions）—— plan「不动」保证
   13. dispatcher._dispatch_one 仍直调 ``await push_task(...)``（工程任务走 execute 重路径），
       不经 route_mentions —— 调度能力没退化（A2A 改 push_notify 只影响 @mention 路由层）。

为何纯静态：
  A2A 来回是「代码结构」契约（route_mentions 用哪个 pusher / 反向清键 / cap），运行时
  LLM 是否 @对人是随机的，但路由机制确定性靠代码锚定。静态契约锁住「push_notify + 反向
  清键 + 同方向拦截 + cap 重置」四个确定性条件，比运行时接龙实测更可靠（实测受 LLM
  随机性 + 后端在线 + 时序影响）。运行时覆盖由 plan 落地时的端到端验证承担。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MENTION = REPO / "backend" / "engine" / "mention.py"
DISPATCHER = REPO / "backend" / "engine" / "dispatcher.py"


def _fn_body(src: str, fname: str, indent_opts=("",)) -> str:
    """抽 fn 函数体到下一个同级 def（mention.py 是模块级 async def，0 缩进）。

    试多种缩进以兼容未来重构（类方法 4 空格 / 模块级 0 缩进）。
    """
    for indent in indent_opts:
        m = re.search(
            rf"(?:async def|def) {fname}\([^)]*\)(.*?)(?=\n{indent}(?:async )?def )",
            src,
            re.S,
        )
        if m:
            return m.group(1)
    return ""


def assert_contract() -> list[str]:
    errs: list[str] = []
    mention = MENTION.read_text(encoding="utf-8")
    dispatcher = DISPATCHER.read_text(encoding="utf-8")

    route_body = _fn_body(mention, "route_mentions", indent_opts=("", "    "))
    if not route_body:
        errs.append("[setup] route_mentions 函数体未找到（结构已变，无法核 A/B/C/D）")
        # 仍尝试继续，下面的检查各自兜底
        route_body = mention  # 退化为全文，避免 KeyError

    # ── A. route_mentions 用 push_notify（非 push_task）──
    # [1] route_mentions 体内调 await push_notify（非 push_task）
    m_notify = re.search(r"await\s+push_notify\s*\(", route_body)
    if not m_notify:
        errs.append("[A1] route_mentions 未调 await push_notify（peer 走不到 brain→chat 轻路径）")
    else:
        print("[A1] OK  route_mentions 用 await push_notify（peer 走 brain→chat 轻路径）")

    # [2] route_mentions 体内不调 await push_task（防退回旧 bug）
    m_task = re.search(r"await\s+push_task\s*\(", route_body)
    if m_task:
        errs.append(
            "[A2] route_mentions 仍调 await push_task（退回旧 bug：peer 被塞 execute 重路径，"
            "接龙断链 + 产物幻觉）"
        )
    else:
        print("[A2] OK  route_mentions 不调 push_task（不退回 execute 重路径旧 bug）")

    # [3] push_notify 的 kind 是 "agent_reply"
    if m_notify:
        # 抓 push_notify(...) 调用块，核第一个位置参数（kind）是 "agent_reply"
        m_kind = re.search(
            r'await\s+push_notify\s*\(\s*[^,]+,\s*"agent_reply"',
            route_body,
        )
        if not m_kind:
            errs.append('[A3] route_mentions 的 push_notify kind 非 "agent_reply"（peer brain 看不到 incoming）')
        else:
            print('[A3] OK  push_notify kind="agent_reply"（peer brain 看到 incoming_sender/message）')

    # ── B. 反向清键允许 A→B→A→B ──
    # [4] 成功 push 后 pop 反向 key
    m_rev = re.search(
        r'recent_routes\.pop\(\s*f"\{target_id\}->\{sender_id\}"\s*,\s*None\s*\)',
        route_body,
    )
    if not m_rev:
        errs.append("[B4] route_mentions 缺反向清键 recent_routes.pop(f\"{target_id}->{sender_id}\")（来回只能 2 轮就死）")
    else:
        print("[B4] OK  成功 push 后反向清键 recent_routes.pop(f\"{target_id}->{sender_id}\")（允许持续交替）")

    # [5] 反向清键在正向 key 赋值之后（顺序正确）
    if m_rev:
        idx_set = route_body.find("recent_routes[key] = now")
        idx_pop = m_rev.start()
        if idx_set < 0:
            errs.append("[B5] route_mentions 缺 recent_routes[key] = now（正向 key 未记，防循环失效）")
        elif idx_set >= idx_pop:
            errs.append("[B5] 反向清键在正向 key 赋值之前（顺序错：清早了，正向未记就清反向）")
        else:
            print("[B5] OK  反向清键在 recent_routes[key]=now 之后（顺序正确：先记正向再清反向）")

    # [6] 防循环 dict 群级共享（None → _get_recent_routes(group_id)）
    m_shared = re.search(
        r"if\s+recent_routes\s+is\s+None\s*:\s*recent_routes\s*=\s*_get_recent_routes\(\s*group_id\s*\)",
        route_body,
    )
    if not m_shared:
        errs.append("[B6] route_mentions 缺群级共享 fallback（recent_routes is None → _get_recent_routes）")
    else:
        print("[B6] OK  防循环 dict 群级共享（None → _get_recent_routes(group_id)，非 per-engine）")

    # ── C. 同方向 30s 连发被拦（防死循环）──
    # [7] if key in recent_routes: continue（同方向 30s 内已路由跳过）
    m_same = re.search(r'if\s+key\s+in\s+recent_routes\s*:\s*continue', route_body)
    if not m_same:
        errs.append("[C7] route_mentions 缺 if key in recent_routes: continue（同方向连发不拦=死循环）")
    else:
        print("[C7] OK  if key in recent_routes: continue（同方向 30s 连发被拦，防死循环）")

    # [8] 30s 过期清理（stale + pop）—— 反例防退化：不能只反向清键不查同方向
    m_stale = re.search(
        r"stale\s*=\s*\[\s*k\s+for\s+k,\s*t\s+in\s+recent_routes\.items\(\)\s+if\s+now\s*-\s*t\s*>=\s*30\.0\s*\]",
        route_body,
    )
    if not m_stale:
        errs.append("[C8] route_mentions 缺 30s 过期清理 stale 列表（防循环计数永不清=cap 后死锁）")
    else:
        # 核 stale 之后 pop
        idx_stale = m_stale.end()
        tail = route_body[idx_stale:idx_stale + 120]
        if "recent_routes.pop(k" not in tail and "recent_routes.pop(k" not in route_body[idx_stale - 40:idx_stale + 120]:
            errs.append("[C8] 30s 过期清理缺 recent_routes.pop(k)（stale 算了不清=无效）")
        else:
            print("[C8] OK  30s 过期清理 stale + recent_routes.pop(k)（防循环计数不堆积）")

    # ── D. route_user_message 不再依赖 _a2a_turns（task-19 去中心化路径）──
    # [9] route_user_message 切 GroupRuntime.invoke_turn 后不再清 _a2a_turns[group_id]=0
    # （入站话筒交给群图 handoff 边 + GroupState.turn_count / recent_speakers 防连发；
    # 旧 _a2a_turns 只服务 route_mentions 的 legacy 防循环，双轨期保留——不再由
    # route_user_message 入口重置）。
    rum_body = _fn_body(mention, "route_user_message", indent_opts=("", "    "))
    if not rum_body:
        errs.append("[D9] route_user_message 函数体未找到")
    elif "_a2a_turns[group_id] = 0" in rum_body:
        errs.append("[D9] route_user_message 仍含 _a2a_turns[group_id]=0（task-19 切群图后应移除）")
    else:
        print("[D9] OK  route_user_message 不再重置 _a2a_turns（话筒交群图 handoff，legacy cap 双轨保留）")

    # [10] _A2A_CAP env 可调 + 默认 50
    m_cap = re.search(
        r'_A2A_CAP\s*=\s*max\(\s*1,\s*int\(\s*os\.environ\.get\(\s*"MULTI_AGENT_A2A_TURNS",\s*"50"\s*\)\s*\)\s*\)',
        mention,
    )
    if not m_cap:
        errs.append("[D10] _A2A_CAP 未用 env MULTI_AGENT_A2A_TURNS 默认 50（cap 不可调/默认值变了）")
    else:
        print("[D10] OK  _A2A_CAP = max(1, int(env MULTI_AGENT_A2A_TURNS, '50'))（env 可调 + 默认 50）")

    # [11] route_mentions 达 cap 不再 push（if 条件 + 块内 return，中间可有 logger.debug）
    m_capcond = re.search(
        r"if\s+_a2a_turns\.get\(\s*group_id,\s*0\s*\)\s*>=\s*_A2A_CAP\s*:",
        route_body,
    )
    if not m_capcond:
        errs.append("[D11] route_mentions 缺 cap 兜底 if _a2a_turns.get(group_id,0)>=_A2A_CAP（无限刷屏烧 token）")
    else:
        # 块体在 if 行之后到下一个同级 def/空白行之前，必须含 return（防只记日志不退出）
        block = route_body[m_capcond.end(): m_capcond.end() + 200]
        if not re.search(r"^\s*return\b", block, re.M):
            errs.append("[D11] cap 兜底 if 块缺 return（达 cap 不退出=无限刷屏）")
        else:
            print("[D11] OK  达 _A2A_CAP 后 return 不再 push（兜底防 LLM 失灵无限刷屏）")

    # [12] route_mentions push 后 _a2a_turns[group_id] += 1（计数累加）
    m_inc = re.search(
        r'_a2a_turns\[group_id\]\s*=\s*_a2a_turns\.get\(\s*group_id,\s*0\s*\)\s*\+\s*1',
        route_body,
    )
    if not m_inc:
        errs.append("[D12] route_mentions 缺 push 后 _a2a_turns[group_id]+=1（计数不累加=cap 永不触顶）")
    else:
        print("[D12] OK  push 后 _a2a_turns[group_id]+=1（计数累加，cap 才有依据）")

    # ── E. DAG 调度不受影响（dispatcher 直调 push_task 不经 route_mentions）──
    # [13] dispatcher._dispatch_one 仍直调 await push_task
    dis_body = _fn_body(dispatcher, "_dispatch_one", indent_opts=("    ", ""))
    if not dis_body:
        errs.append("[E13] dispatcher._dispatch_one 函数体未找到")
    elif "await push_task(" not in dis_body:
        errs.append("[E13] dispatcher._dispatch_one 不再直调 push_task（DAG 调度退化：工程任务可能漏走 execute）")
    else:
        print("[E13] OK  dispatcher._dispatch_one 直调 push_task（DAG 调度不经 route_mentions，工程任务仍走 execute）")

    return errs


def main() -> int:
    print("=== VH1 回归：A2A @mention 来回对话不退化 ===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "VH1 回归契约锁定（composed-doodling-creek.md A2A 来回机制不退化）：\n"
        "  · A route_mentions 用 push_notify（非 push_task），kind=agent_reply——peer 走 brain→chat "
        "轻路径，不进 execute 重路径（接龙接得起来，无产物幻觉）；\n"
        "  · B 反向清键 recent_routes.pop(f\"{target_id}->{sender_id}\") 在正向 key 赋值后 + "
        "群级共享 dict（None→_get_recent_routes），允许 A→B→A→B 持续交替（非 per-engine 打不中对方）；\n"
        "  · C 同方向 30s 连发被拦（if key in recent_routes: continue）+ 30s 过期清理 stale+pop——"
        "防死循环（A 连发两次 @B 挡掉），与反向清键同在（缺任一=退化）；\n"
        "  · D route_user_message 不再重置 _a2a_turns（task-19 切群图 handoff 边）+ "
        "_A2A_CAP env 可调默认 50 + 达 cap 不 push + push 后计数累加——route_mentions 双轨防循环保留；\n"
        "  · E dispatcher._dispatch_one 仍直调 push_task（DAG 调度不经 route_mentions）——"
        "工程任务仍走 execute 重路径，A2A 改 push_notify 只影响 @mention 路由层。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

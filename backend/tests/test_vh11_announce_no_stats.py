"""VH11 回归：announce 类回复不带 stats 统一判定（task B14）.

锁住 B14 修复——``engine/dispatcher.py:82`` ``🚀 步骤 N 派发`` 与 coordinator
``node_dispatch`` ``📋 已制定协作计划`` 两类 announce 都 ``data=None`` 无 stats，
统一判定「announce 类回复不带 stats」并写注释说明（与 A8/vg2 的 stats 契约对齐）。

B14 改动（行为零变 + 注释统一 + dispatcher 走 persist_agent_reply 真源）：
  - dispatcher ``_dispatch_one``：原内联 ``crud.create_message({...data: None})`` +
    ``emit_message_added`` → 改调 ``persist_agent_reply(group_id, coordinator_id,
    dispatch_msg, None)``（B10 单一真源，与 registry._reply / coordinator._unified_reply
    / worker._unified_reply 同源）。message dict 不再在 dispatcher 重复（B10 已抽，B14
    接线）。``data`` 恒 None（模板 announce 无 stats）。
  - dispatcher ``_dispatch_one`` 注释：标注「B14 announce 类回复不带 stats」——模板
    文本（agent_name + instruction 拼接）非流式 LLM 输出，故不带 model/elapsed_ms/tokens
    stats，与 A8/vg2 的「dispatch announce 排除在 stats 契约外」对齐，前端
    extractCoordStats 对 data.elapsed_ms 缺失返 null 不渲染状态行（正确）。
  - coordinator ``node_dispatch`` 注释：在「📋 已制定协作计划」announce 两个分支
    （auto_confirm 直接干 / 确认后执行）前标注「B14 announce 类回复不带 stats」——
    与 dispatcher ``🚀 步骤 N 派发`` 同判定（模板文本 plan_summary 拼接非流式 LLM），
    _unified_reply 不传 data → persist_agent_reply 落 data=None。

为何统一判定重要：B14 前两处 announce 都 data=None 但无统一注释说明「为何 None」——
dispatcher ``data: None`` 裸值无解释，coordinator node_dispatch 的「📋」announce 也
无 stats 排除注释（只有 node_llm_decide 的 stats 盖注释提了「dispatch is excluded」）。
B14 在两处 announce 站点都写「B14 announce 类回复不带 stats」注释 + 互相引用对方，
形成统一判定真源——未来若有人给 announce 加 stats（误以为漏了），注释会拦住（说明
模板文本不匹配流式 stats，加了也是错的）。

为何 dispatcher 改调 persist_agent_reply（B10 真源）：B10 抽 persist_agent_reply 时
dispatcher ``_dispatch_one`` 是漏网之鱼（仍内联 crud.create_message + emit_message_added，
与三份 reply 实现之一的 registry._reply 重复）。B14 顺手接线——dispatcher 的 announce
本质就是 agent_reply（type="agent_reply" / receiver_id="broadcast" / task_id=None），
与 registry._reply 同形，应走同一真源。行为零变（message dict 6 key 一致 + data=None），
去重完成（dispatcher 不再内联 agent_reply dict）。

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh10 同款风格。

六段契约：

  A. dispatcher _dispatch_one announce 走 persist_agent_reply 真源（B14 接线 B10）
    1. ``_dispatch_one`` 调 ``persist_agent_reply(group_id, coordinator_id, dispatch_msg, None)``
       （非内联 crud.create_message）。
    2. ``_dispatch_one`` 不再内联 agent_reply dict（``"type": "agent_reply"`` 不在函数体）。
    3. dispatcher 顶部 ``from engine.reply import persist_agent_reply``（import 真源）。
    4. dispatcher 不再 import ``crud`` / ``emit_message_added``（B14 接线后去重）。

  B. dispatcher announce data=None（B14 announce 不带 stats）
    5. ``persist_agent_reply(..., None)`` 恒 data=None（announce 模板文本无 stats）。
    6. ``_dispatch_one`` 注释含「B14 announce」+「stats」+「data ... None」语义
       （统一判定文档化）。
    7. 注释引用 A8/vg2 或 node_dispatch（互相引用形成真源）。

  C. coordinator node_dispatch announce data=None（与 dispatcher 同判定）
    8. node_dispatch 两个分支（auto_confirm / 确认）都 ``_unified_reply(... content)``
       不传 data（默认 None）。
    9. node_dispatch announce 注释含「B14 announce」+「stats」+「data」语义
       （统一判定文档化）。
   10. 注释引用 dispatcher ``🚀 步骤 N 派发``（互相引用形成真源）。

  D. 行为零变（announce 落盘 shape 不变）
   11. dispatcher announce 仍 type="agent_reply" / receiver_id="broadcast" /
       task_id=None（persist_agent_reply 内部锁，B14 不改 shape）。
   12. coordinator node_dispatch announce 仍走 _unified_reply → persist_agent_reply
       （B10 已锁，B14 只加注释不改调用）。
   13. message dict 不再在 dispatcher 重复（grep ``"type": "agent_reply"`` in
       _dispatch_one body → 0，只在 reply.py）。

  E. 与 A8/vg2 stats 契约对齐（announce 排除在 stats 透传外）
   14. node_llm_decide 仍只在 chat/ask/continue 盖 _stream_stats（dispatch 不盖）
       —— A8/vg2 的「dispatch announce 排除」契约 B14 不破。
   15. registry._reply 仍 persist_agent_reply(..., None)（execute announce 无 stats）
       —— B10 已锁，B14 统一判定覆盖三类 announce（registry execute / dispatcher
       派发 / coordinator 计划）。

  F. 无回归（dispatch_ready_steps 调用链不破）
   16. ``_dispatch_one`` 仍 ``-> None``（B14 不改签名）+ 仍 push_task + emit_task_dispatched。
   17. ``dispatch_ready_steps`` 仍调 ``_dispatch_one``（B14 不改调用点）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DISPATCHER = REPO / "backend" / "engine" / "dispatcher.py"
COORD = REPO / "backend" / "engine" / "coordinator.py"
REPLY = REPO / "backend" / "engine" / "reply.py"


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


def assert_contract() -> list[str]:
    errs: list[str] = []
    disp = DISPATCHER.read_text(encoding="utf-8")
    coord = COORD.read_text(encoding="utf-8")
    reply_mod = REPLY.read_text(encoding="utf-8")

    do_body = _fn_body(disp, "_dispatch_one", indent_opts=("    ",))
    if not do_body:
        errs.append("[setup] _dispatch_one 函数体未找到")
        return errs

    # ── A. dispatcher _dispatch_one announce 走 persist_agent_reply 真源 ──
    # [1] 调 persist_agent_reply(group_id, coordinator_id, dispatch_msg, None)
    if not re.search(
        r"persist_agent_reply\(group_id,\s*coordinator_id,\s*dispatch_msg,\s*None\)",
        do_body,
    ):
        errs.append("[A1] _dispatch_one 未调 persist_agent_reply(..., None)（B14 未接线 B10 真源）")
    else:
        print("[A1] OK  _dispatch_one → persist_agent_reply(group_id, coordinator_id, dispatch_msg, None)")
    # [2] 不再内联 agent_reply dict
    if '"type": "agent_reply"' in do_body:
        errs.append("[A2] _dispatch_one 仍内联 agent_reply dict（B14 未去重）")
    else:
        print("[A2] OK  _dispatch_one 不再内联 agent_reply dict（去重完成）")
    # [3] 顶部 from engine.reply import persist_agent_reply
    if "from engine.reply import persist_agent_reply" not in disp:
        errs.append("[A3] dispatcher 未 from engine.reply import persist_agent_reply（import 真源缺失）")
    else:
        print("[A3] OK  dispatcher from engine.reply import persist_agent_reply（import 真源）")
    # [4] 不再 import crud / emit_message_added（去重后清理）
    if "from store import crud" in disp:
        errs.append("[A4] dispatcher 仍 import crud（B14 接线后应去 crud import）")
    elif "emit_message_added" in disp.split("async def _dispatch_one")[0]:
        # emit_message_added 可能在 _dispatch_one 之前 import 但已不用——检查 import 行
        if re.search(r"from events import.*emit_message_added", disp):
            errs.append("[A4] dispatcher 仍 import emit_message_added（B14 接线后应去）")
        else:
            print("[A4] OK  dispatcher 不再 import crud / emit_message_added（去重清理）")
    else:
        print("[A4] OK  dispatcher 不再 import crud / emit_message_added（去重清理）")

    # ── B. dispatcher announce data=None（B14 announce 不带 stats）──
    # [5] persist_agent_reply(..., None) 恒 data=None（[A1] 已锁调用，此处确认 None 字面量）
    if not re.search(r"persist_agent_reply\([^)]*,\s*None\s*\)", do_body, re.S):
        errs.append("[B5] _dispatch_one persist_agent_reply 调用未恒 data=None")
    else:
        print("[B5] OK  persist_agent_reply(..., None) 恒 data=None（announce 模板文本无 stats）")
    # [6] 注释含「B14 announce」+「stats」+「None」语义
    has_b14 = "B14" in do_body and "announce" in do_body
    has_stats = "stats" in do_body
    has_none = "None" in do_body
    if not (has_b14 and has_stats and has_none):
        errs.append(
            f"[B6] _dispatch_one 注释缺统一判定文档（B14+announce={has_b14} stats={has_stats} None={has_none}）"
        )
    else:
        print("[B6] OK  _dispatch_one 注释含「B14 announce」+「stats」+「None」（统一判定文档化）")
    # [7] 注释引用 A8/vg2 或 node_dispatch（互相引用形成真源）
    refs_other = ("A8" in do_body or "vg2" in do_body or "node_dispatch" in do_body
                  or "📋" in do_body or "已制定协作计划" in do_body)
    if not refs_other:
        errs.append("[B7] _dispatch_one 注释未引用 A8/vg2 或 node_dispatch（缺互相引用）")
    else:
        print("[B7] OK  _dispatch_one 注释引用 A8/vg2 / node_dispatch（互相引用形成真源）")

    # ── C. coordinator node_dispatch announce data=None（与 dispatcher 同判定）──
    nd_body = _fn_body(coord, "node_dispatch", indent_opts=("    ",))
    if not nd_body:
        errs.append("[C8] node_dispatch 函数体未找到")
    else:
        # [8] 两个分支都 _unified_reply(... content) 不传 data（默认 None）
        # auto_confirm 分支 + 确认分支，都 _unified_reply(group, agent, content) 无 data=
        m_auto = re.search(
            r'_unified_reply\(\s*state\["group_id"\],\s*state\["agent_id"\],\s*f"📋 已制定协作计划（直接干模式）[^"]*",\s*\)',
            nd_body,
            re.S,
        )
        m_confirm = re.search(
            r'_unified_reply\(\s*state\["group_id"\],\s*state\["agent_id"\],\s*f"📋 已制定协作计划，请确认后执行[^"]*",\s*\)',
            nd_body,
            re.S,
        )
        if not (m_auto and m_confirm):
            errs.append("[C8] node_dispatch 两个 announce 分支未都 _unified_reply(... 无 data)（B14 不破）")
        else:
            print("[C8] OK  node_dispatch 两分支都 _unified_reply(... content) 不传 data（默认 None）")
        # [9] 注释含「B14 announce」+「stats」+「data」语义
        has_b14_c = "B14" in nd_body and "announce" in nd_body
        has_stats_c = "stats" in nd_body
        has_data_c = "data" in nd_body
        if not (has_b14_c and has_stats_c and has_data_c):
            errs.append(
                f"[C9] node_dispatch 注释缺统一判定文档（B14+announce={has_b14_c} stats={has_stats_c} data={has_data_c}）"
            )
        else:
            print("[C9] OK  node_dispatch 注释含「B14 announce」+「stats」+「data」（统一判定文档化）")
        # [10] 注释引用 dispatcher 🚀 步骤 N 派发（互相引用形成真源）
        if "🚀" not in nd_body and "步骤 N 派发" not in nd_body and "_dispatch_one" not in nd_body:
            errs.append("[C10] node_dispatch 注释未引用 dispatcher 派发 announce（缺互相引用）")
        else:
            print("[C10] OK  node_dispatch 注释引用 dispatcher 派发 announce（互相引用形成真源）")

    # ── D. 行为零变（announce 落盘 shape 不变）──
    # [11] persist_agent_reply 内部 type="agent_reply" / receiver_id="broadcast" / task_id=task_id
    # B22：persist_agent_reply message dict 的 "task_id" 从恒 None 改为透传 task_id 参数
    # （registry _reply 透传真 task_id，graph _unified_reply 不传保持默认 None）。原「task_id=None」
    # 字面量断言不再成立——announce 路径（dispatcher _dispatch_one / coordinator node_dispatch）
    # 走 persist_agent_reply(..., None) 不传 task_id，task_id 默认 None 落盘（行为零变）。
    # 断言改为：message dict 含 "task_id": task_id（透传参数）+ dispatcher announce 仍调
    # persist_agent_reply(..., None)（data=None，不传 task_id → 默认 None）。
    if '"type": "agent_reply"' not in reply_mod or '"receiver_id": "broadcast"' not in reply_mod:
        errs.append("[D11] persist_agent_reply 内部 agent_reply shape 异常（D11 契约破）")
    elif '"task_id": task_id' not in reply_mod:
        errs.append("[D11] persist_agent_reply 内部缺 \"task_id\": task_id（B22 应透传 task_id 参数）")
    else:
        print("[D11] OK  persist_agent_reply 内部 agent_reply shape（type/broadcast/task_id=task_id，B22 透传）")
    # [12] node_dispatch 仍走 _unified_reply → persist_agent_reply（[C8] 已锁调用，此处确认 _unified_reply 仍模块级）
    if not re.search(r"^async def _unified_reply\(", coord, re.M):
        errs.append("[D12] coordinator 无模块级 _unified_reply（node_dispatch announce 链断）")
    else:
        print("[D12] OK  node_dispatch → _unified_reply → persist_agent_reply（B10 链路不破）")
    # [13] message dict 不再在 dispatcher 重复（grep "type": "agent_reply" in _dispatch_one → 0）
    if '"type": "agent_reply"' in do_body:
        errs.append("[D13] _dispatch_one 仍内联 agent_reply dict（去重未完成）")
    else:
        print("[D13] OK  agent_reply dict 仅在 engine/reply.py（dispatcher 去重完成）")

    # ── E. 与 A8/vg2 stats 契约对齐 ──
    # [14] node_llm_decide 仍只在 chat/ask/continue 盖 _stream_stats（dispatch 不盖）
    m_stats = re.search(
        r'if\s+decision\["action"\]\s+in\s+\("chat",\s*"ask",\s*"continue"\)\s*:\s*'
        r'decision\["_stream_stats"\]\s*=\s*\{(.*?)\}',
        coord,
        re.S,
    )
    if not m_stats:
        errs.append("[E14] node_llm_decide chat/ask/continue 盖 _stream_stats 契约破（A8/vg2 回归）")
    else:
        print("[E14] OK  node_llm_decide 仍只在 chat/ask/continue 盖 _stream_stats（dispatch 不盖，A8/vg2 不破）")
    # [15] registry._reply 仍 persist_agent_reply(..., None, task_id)（execute announce 无 stats）
    # B22：_reply 调用形如 persist_agent_reply(self.group_id, self.agent_id, content, None, task_id)
    # 第 4 参 None = data 恒 None（execute announce 无 stats 不变）；第 5 参 task_id = B22 透传。
    # 断言 None 在 persist_agent_reply 调用里（data=None，核心契约「announce 无 stats」不变）。
    reg = (REPO / "backend" / "engine" / "registry.py").read_text(encoding="utf-8")
    r_reply = _fn_body(reg, "_reply", indent_opts=("    ",))
    if not r_reply or not re.search(r"persist_agent_reply\([^)]*,\s*None\s*,\s*task_id\s*\)", r_reply, re.S):
        errs.append("[E15] registry._reply 未 persist_agent_reply(..., None, task_id)（execute announce 无 stats + B22 task_id 链断）")
    else:
        print("[E15] OK  registry._reply 仍 persist_agent_reply(..., None, task_id)（data 恒 None 无 stats + B22 透传 task_id）")

    # ── F. 无回归（dispatch_ready_steps 调用链不破）──
    # [16] _dispatch_one 仍 -> None + push_task + emit_task_dispatched
    if not re.search(r"async def _dispatch_one\([^)]*\)\s*->\s*None:", disp):
        errs.append("[F16] _dispatch_one 签名异常（应 -> None）")
    elif "push_task(" not in do_body:
        errs.append("[F16] _dispatch_one 丢失 push_task 调用（派发链断）")
    elif "emit_task_dispatched(" not in do_body:
        errs.append("[F16] _dispatch_one 丢失 emit_task_dispatched 调用（事件链断）")
    else:
        print("[F16] OK  _dispatch_one 仍 -> None + push_task + emit_task_dispatched（签名/派发链不变）")
    # [17] dispatch_ready_steps 仍调 _dispatch_one
    drs_body = _fn_body(disp, "dispatch_ready_steps", indent_opts=("",))
    if not drs_body or "_dispatch_one(" not in drs_body:
        errs.append("[F17] dispatch_ready_steps 未调 _dispatch_one（调用链断）")
    else:
        print("[F17] OK  dispatch_ready_steps 仍调 _dispatch_one（调用链不破）")

    return errs


def main() -> int:
    print("=== VH11 回归：announce 类回复不带 stats 统一判定（B14）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B14 announce 类回复不带 stats 统一判定锁定：\n"
        "  · A dispatcher _dispatch_one announce 走 persist_agent_reply 真源（B14 接线 B10，去重 agent_reply dict）；\n"
        "  · B dispatcher announce data=None（模板文本无 stats）+ 注释「B14 announce/stats/None」+ 引用 A8/vg2/node_dispatch；\n"
        "  · C coordinator node_dispatch 两分支 announce data=None（默认 None）+ 注释「B14 announce/stats」+ 引用 dispatcher；\n"
        "  · D 行为零变：persist_agent_reply agent_reply shape 不变 + node_dispatch → _unified_reply → persist_agent_reply 链不破 + dispatcher 去重；\n"
        "  · E 与 A8/vg2 对齐：node_llm_decide 仍只在 chat/ask/continue 盖 _stream_stats（dispatch 不盖）+ registry._reply 仍 None（三类 announce 统一无 stats）；\n"
        "  · F 无回归：_dispatch_one 签名/派发链 + dispatch_ready_steps 调用链不破。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

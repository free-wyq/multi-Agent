"""VH7 回归：persist_agent_reply 统一 reply 落盘真源（task B10）.

锁住 B10 修复——三份近乎相同的 reply 实现（registry._reply / coordinator._unified_reply
/ worker._unified_reply）抽共享 ``engine/reply.py`` ``persist_agent_reply``，统一
agent_reply 落盘（crud.create_message）+ emit（emit_message_added）真源。

B10 前的重复：三个函数都构建同一个 message dict（``{group_id, task_id=None,
sender_id, receiver_id="broadcast", type="agent_reply", content, data}``）+ 调
crud.create_message + emit_message_added。差异只在路由：registry 直接调 route_mentions
（引擎持有上下文），两个 graph 节点走引擎装的 reply callback（节点拿不到 engine 实例）。
B10 只抽「persist+emit」核心（单一真源），路由差异保留（真实架构缝：graph 节点 vs
engine 实例）——合并路由会迫使 graph 节点去够 engine，重引入 B9 刚消除的耦合。

为何不抽路由：registry 调 route_mentions 直接（它有 self.group_id/agent_id/name +
群级 recent_routes 上下文），graph 节点走 _REPLY_CB callback（set_reply_callback 装的，
contextvars 隔离并发引擎）。两者是不同的 mention 路由机制，强行合并要么 graph 节点
直接够 engine（耦合回归）要么 registry 也改走 callback（它本就有上下文，多此一举）。
B10 只统一「落盘+emit」——这是三份真正重复的部分，路由是各自合理的差异。

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh6 同款风格。

六段契约：

  A. persist_agent_reply 单一真源（engine/reply.py）
    1. ``engine/reply.py`` 文件存在 + 定义 ``async def persist_agent_reply``。
    2. persist_agent_reply 构建 agent_reply dict（type="agent_reply" / receiver_id="broadcast" / task_id=None）。
    3. persist_agent_reply 调 crud.create_message + emit_message_added(msg.model_dump())。
    4. persist_agent_reply 接 data 参数透传到 "data": data（不恒 None）。

  B. coordinator _unified_reply 委托 persist_agent_reply（不再内联落盘）
    5. coordinator _unified_reply 函数体调 persist_agent_reply（非内联 crud.create_message）。
    6. coordinator _unified_reply 不再内联 crud.create_message agent_reply dict（B10 去重）。
    7. coordinator _unified_reply 仍调 _REPLY_CB.get() callback（路由机制保留）。

  C. worker _unified_reply 委托 persist_agent_reply（与协调者同款）
    8. worker _unified_reply 函数体调 persist_agent_reply。
    9. worker _unified_reply 不再内联 crud.create_message agent_reply dict。
   10. worker _unified_reply 仍调 _REPLY_CB.get() callback（路由机制保留）。

  D. registry _reply 委托 persist_agent_reply（恒 data=None）
   11. registry _reply 调 persist_agent_reply(..., None)（恒 data=None，announce 无 stats）。
   12. registry _reply 不再内联 crud.create_message agent_reply dict（B10 去重）。
   13. registry _reply 仍直接调 route_mentions（路由机制保留——engine 持有上下文）。

  E. 行为零变（B10 纯抽公共，落盘+emit 口径不变）
   14. persist_agent_reply 的 message dict shape 与原三份一致（6 key: group_id/task_id/
       sender_id/receiver_id/type/content/data）。
   15. agent_reply dict 不再在三处重复（grep ``"type": "agent_reply"`` in coordinator/
       worker _unified_reply body + registry _reply body → 0，只在 reply.py）。

  F. 路由差异保留（B10 不合并路由，只统一落盘）
   16. coordinator/worker _unified_reply 走 _REPLY_CB callback（graph 节点机制）。
   17. registry _reply 走 route_mentions 直接调用（engine 实例机制）。
   18. 两种机制并存（非强行统一——是真实架构缝，合并会重引入耦合）。

  G. m12 测试 patch 仍可拦截（_unified_reply 仍是 coordinator 模块级名）
   19. coordinator 仍有模块级 _unified_reply（patch.object(coord_mod,"_unified_reply") 解析）。
   20. node_chat 调 _unified_reply（模块全局名，patch 拦截 node_chat 的 reply 落盘）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
REPLY = REPO / "backend" / "engine" / "reply.py"
COORD = REPO / "backend" / "engine" / "coordinator.py"
WORKER = REPO / "backend" / "engine" / "worker.py"
REGISTRY = REPO / "backend" / "engine" / "registry.py"


def _fn_body(src: str, fname: str, indent_opts=("", "    ")) -> str:
    """抽 fn 函数体到下一个同级 def（试多种缩进：模块级 0 / 类方法 4 空格）。

    模块级函数后若紧跟 ``__all__`` / 顶层赋值 / class（非 def），正则的
    ``(?=\n(?:async )?def )`` 锚点会失配（无下一个 def）。回退到「函数签名到文件尾」。
    """
    for indent in indent_opts:
        m = re.search(
            rf"(?:async def|def) {fname}\([^)]*\)(.*?)(?=\n{indent}(?:async )?def )",
            src,
            re.S,
        )
        if m:
            return m.group(1)
    # 回退：函数签名到文件尾（模块级函数后跟 __all__ / 赋值时用）
    m = re.search(rf"(?:async def|def) {fname}\([^)]*\)(.*)$", src, re.S)
    return m.group(1) if m else ""


def assert_contract() -> list[str]:
    errs: list[str] = []
    reply_mod = REPLY.read_text(encoding="utf-8") if REPLY.exists() else ""
    coord = COORD.read_text(encoding="utf-8")
    worker = WORKER.read_text(encoding="utf-8")
    registry = REGISTRY.read_text(encoding="utf-8")

    # ── A. persist_agent_reply 单一真源 ──
    # [1] engine/reply.py 存在 + 定义 async def persist_agent_reply
    if not reply_mod:
        errs.append("[A1] engine/reply.py 不存在（B10 未落地）")
        return errs
    if not re.search(r"async def persist_agent_reply\(", reply_mod):
        errs.append("[A1] engine/reply.py 未定义 async def persist_agent_reply")
    else:
        print("[A1] OK  engine/reply.py 定义 async def persist_agent_reply")

    pa_body = _fn_body(reply_mod, "persist_agent_reply", indent_opts=("",))
    if not pa_body:
        errs.append("[setup] persist_agent_reply 函数体未找到")
        return errs

    # [2] 构建 agent_reply dict（type="agent_reply" / receiver_id="broadcast" / task_id=task_id）
    # B22：persist_agent_reply 加 task_id 参数（registry _reply 透传收尾任务 id，
    # graph _unified_reply 不传保持默认 None）。message dict 的 "task_id" 改为
    # 透传 task_id 参数（非恒 None）——原 B10 「task_id=None」断言不再成立，改为
    # 断言透传 task_id 参数（B22 真源接线）。
    if '"type": "agent_reply"' not in pa_body:
        errs.append('[A2] persist_agent_reply 未构建 type="agent_reply" message dict')
    elif '"receiver_id": "broadcast"' not in pa_body:
        errs.append('[A2] persist_agent_reply message dict 缺 receiver_id="broadcast"')
    elif '"task_id": task_id' not in pa_body:
        errs.append('[A2] persist_agent_reply message dict 缺 "task_id": task_id（B22 应透传 task_id 参数非恒 None）')
    else:
        print('[A2] OK  persist_agent_reply 构建 agent_reply dict（type/broadcast/task_id=task_id，B22 透传）')

    # [3] 调 crud.create_message + emit_message_added
    if "crud.create_message" not in pa_body:
        errs.append("[A3] persist_agent_reply 未调 crud.create_message")
    elif "emit_message_added(msg.model_dump())" not in pa_body:
        errs.append("[A3] persist_agent_reply 未 emit_message_added(msg.model_dump())")
    else:
        print("[A3] OK  persist_agent_reply → crud.create_message + emit_message_added")

    # [4] 接 data 参数透传 "data": data（不恒 None）
    if '"data": data' not in pa_body:
        errs.append('[A4] persist_agent_reply 未透传 "data": data（应接 data 参数非恒 None）')
    else:
        print('[A4] OK  persist_agent_reply 透传 "data": data（接 data 参数，非恒 None）')
    # [4b] B22：接 task_id 参数（默认 None，registry _reply 透传真 task_id）
    if "task_id: str | None = None" not in pa_body and "task_id: Optional[str] = None" not in pa_body:
        # _fn_body 抽的是函数体（{ 后到 } 前），形参在签名行——查 reply_mod 全文（含签名）。
        if "task_id: str | None = None" in reply_mod or "task_id: Optional[str] = None" in reply_mod:
            print('[A4b] OK  persist_agent_reply 签名接 task_id: str | None = None（B22 默认 None 保全既有调用方）')
        else:
            errs.append('[A4b] persist_agent_reply 缺 task_id: str | None = None 参数（B22 未加 task_id 形参）')
    else:
        print('[A4b] OK  persist_agent_reply 接 task_id: str | None = None（B22 默认 None 保全既有调用方）')

    # ── B. coordinator _unified_reply 委托 ──
    c_reply = _fn_body(coord, "_unified_reply", indent_opts=("",))
    if not c_reply:
        errs.append("[B5] coordinator _unified_reply 函数体未找到")
    else:
        # [5] 调 persist_agent_reply（非内联 crud.create_message）
        if "persist_agent_reply" not in c_reply:
            errs.append("[B5] coordinator _unified_reply 未调 persist_agent_reply（B10 未接线）")
        else:
            print("[B5] OK  coordinator _unified_reply → persist_agent_reply")
        # [6] 不再内联 crud.create_message agent_reply dict
        if '"type": "agent_reply"' in c_reply:
            errs.append("[B6] coordinator _unified_reply 仍内联 agent_reply dict（B10 未去重）")
        else:
            print("[B6] OK  coordinator _unified_reply 不再内联 agent_reply dict（去重）")
        # [7] 仍调 _REPLY_CB callback
        if "_REPLY_CB.get()" not in c_reply:
            errs.append("[B7] coordinator _unified_reply 丢失 _REPLY_CB callback（路由机制回归）")
        else:
            print("[B7] OK  coordinator _unified_reply 保留 _REPLY_CB.get() callback（路由机制不变）")

    # ── C. worker _unified_reply 委托 ──
    w_reply = _fn_body(worker, "_unified_reply", indent_opts=("",))
    if not w_reply:
        errs.append("[C8] worker _unified_reply 函数体未找到")
    else:
        # [8] 调 persist_agent_reply
        if "persist_agent_reply" not in w_reply:
            errs.append("[C8] worker _unified_reply 未调 persist_agent_reply（B10 未接线）")
        else:
            print("[C8] OK  worker _unified_reply → persist_agent_reply")
        # [9] 不再内联 crud.create_message agent_reply dict
        if '"type": "agent_reply"' in w_reply:
            errs.append("[C9] worker _unified_reply 仍内联 agent_reply dict（B10 未去重）")
        else:
            print("[C9] OK  worker _unified_reply 不再内联 agent_reply dict（去重）")
        # [10] 仍调 _REPLY_CB callback
        if "_REPLY_CB.get()" not in w_reply:
            errs.append("[C10] worker _unified_reply 丢失 _REPLY_CB callback（路由机制回归）")
        else:
            print("[C10] OK  worker _unified_reply 保留 _REPLY_CB.get() callback（路由机制不变）")

    # ── D. registry _reply 委托 ──
    r_reply = _fn_body(registry, "_reply", indent_opts=("    ",))
    if not r_reply:
        errs.append("[D11] registry _reply 函数体未找到")
    else:
        # [11] 调 persist_agent_reply(..., None, task_id)（恒 data=None，B22 透传 task_id）
        # B22：_reply 加 task_id 参数透传到 persist_agent_reply 第 5 参。data 仍恒 None
        # （第 4 参，execute announce 不带 stats）。断言：persist_agent_reply 调用含 None
        # （data=None）+ task_id 参数名（B22 接线）。
        delegates_none = "persist_agent_reply" in r_reply and re.search(
            r"persist_agent_reply\([^)]*,\s*None\s*,\s*task_id\s*\)", r_reply, re.S
        ) is not None
        if not delegates_none:
            errs.append("[D11] registry _reply 未调 persist_agent_reply(..., None, task_id)（恒 data=None + B22 透传 task_id）")
        else:
            print("[D11] OK  registry _reply → persist_agent_reply(..., None, task_id)（恒 data=None + B22 透传 task_id）")
        # [12] 不再内联 crud.create_message agent_reply dict
        if '"type": "agent_reply"' in r_reply:
            errs.append("[D12] registry _reply 仍内联 agent_reply dict（B10 未去重）")
        else:
            print("[D12] OK  registry _reply 不再内联 agent_reply dict（去重）")
        # [13] 仍直接调 route_mentions（engine 实例机制）
        if "route_mentions" not in r_reply:
            errs.append("[D13] registry _reply 丢失 route_mentions 直接调用（路由机制回归）")
        else:
            print("[D13] OK  registry _reply 保留 route_mentions 直接调用（engine 持有上下文）")

    # ── E. 行为零变（Path C 严格改名 group_id→conversation_id 后，message dict key
    #     是 conversation_id，非 group_id；persist_agent_reply 入参 group_id 仍叫
    #     group_id 是历史命名，落盘时映射到 conversation_id 字段）──
    # [14] persist_agent_reply message dict shape 7 key（conversation_id 替 group_id）
    keys = set(re.findall(r'"(\w+)":', pa_body))
    expected = {"conversation_id", "task_id", "sender_id", "receiver_id", "type", "content", "data"}
    missing = expected - keys
    if missing:
        errs.append(f"[E14] persist_agent_reply message dict 缺 key {missing}（Path C 改名后应为 conversation_id 非 group_id）")
    else:
        print(f"[E14] OK  message dict 7 key 齐全（{sorted(expected)}，Path C 改名 group_id→conversation_id）")
    # [14b] message dict 不应再含 group_id key（严格改名）
    if '"group_id":' in pa_body:
        errs.append("[E14b] persist_agent_reply message dict 仍含 group_id key（Path C 严格改名后应为 conversation_id）")
    else:
        print("[E14b] OK  message dict 无 group_id key（Path C 严格改名 group_id→conversation_id）")

    # [15] agent_reply dict 不再在三处重复（只在 reply.py）
    dup_count = sum(
        ('"type": "agent_reply"' in body) for body in (c_reply, w_reply, r_reply)
    )
    if dup_count > 0:
        errs.append(f"[E15] agent_reply dict 仍在 {dup_count}/3 处重复（应只在 reply.py）")
    else:
        print("[E15] OK  agent_reply dict 仅在 engine/reply.py（三处去重完成）")

    # ── F. 路由差异保留 ──
    # [16] coordinator/worker 走 _REPLY_CB callback
    cb_count = sum(("_REPLY_CB.get()" in b) for b in (c_reply, w_reply))
    if cb_count != 2:
        errs.append(f"[F16] coordinator/worker _REPLY_CB callback 仅 {cb_count}/2 保留（路由机制应保留）")
    else:
        print("[F16] OK  coordinator/worker 都走 _REPLY_CB callback（graph 节点机制保留）")
    # [17] registry 走 route_mentions 直接
    if "route_mentions" not in r_reply:
        errs.append("[F17] registry _reply 未走 route_mentions 直接调用（应保留 engine 机制）")
    else:
        print("[F17] OK  registry _reply 走 route_mentions 直接调用（engine 实例机制保留）")
    # [18] 两种机制并存
    if cb_count == 2 and "route_mentions" in r_reply:
        print("[F18] OK  两种路由机制并存（callback + route_mentions，B10 不合并路由是正确取舍）")
    else:
        errs.append("[F18] 两种路由机制未并存（B10 应保留差异非合并）")

    # ── G. m12 patch 仍可拦截 ──
    # [19] coordinator 仍有模块级 _unified_reply
    if not re.search(r"^async def _unified_reply\(", coord, re.M):
        errs.append("[G19] coordinator 无模块级 _unified_reply（m12 patch.object(coord_mod,'_unified_reply') 会断）")
    else:
        print("[G19] OK  coordinator 仍有模块级 _unified_reply（m12 patch 解析）")
    # [20] node_chat 调 _unified_reply（模块全局名，patch 拦截）
    node_chat_body = _fn_body(coord, "node_chat", indent_opts=("",))
    if not node_chat_body or "_unified_reply(" not in node_chat_body:
        errs.append("[G20] coordinator node_chat 未调 _unified_reply（m12 patch 拦截链断）")
    else:
        print("[G20] OK  node_chat 调 _unified_reply（模块全局名，m12 patch 拦截落盘）")

    return errs


def main() -> int:
    print("=== VH7 回归：persist_agent_reply 统一 reply 落盘真源 ===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "VH7 回归契约锁定（B10 抽公共不退化）：\n"
        "  · A persist_agent_reply 单一真源（engine/reply.py）：构建 agent_reply dict + "
        "crud.create_message + emit_message_added + 接 data 透传；\n"
        "  · B coordinator _unified_reply 委托 persist_agent_reply（不再内联落盘）+ 保留 "
        "_REPLY_CB callback 路由；\n"
        "  · C worker _unified_reply 同款委托 + 保留 _REPLY_CB callback；\n"
        "  · D registry _reply 委托 persist_agent_reply(..., None)（恒 data=None）+ 保留 "
        "route_mentions 直接调用；\n"
        "  · E 行为零变：message dict 6 key 与原三份一致 + agent_reply dict 仅在 reply.py "
        "（三处去重完成）；\n"
        "  · F 路由差异保留：coordinator/worker 走 _REPLY_CB callback（graph 节点）+ registry "
        "走 route_mentions 直接（engine 实例），两种机制并存（B10 不合并路由是正确取舍——"
        "合并会重引入 graph 节点够 engine 的耦合，B9 刚消除）；\n"
        "  · G m12 patch 仍可拦截：coordinator 模块级 _unified_reply 保留 + node_chat 调模块全局名 "
        "（patch.object(coord_mod,'_unified_reply') 仍拦截落盘）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

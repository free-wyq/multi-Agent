"""VH57 回归：群组协作模式字段链路（model→state→group_runtime→worker）.

锁住「群组协作模式（centralized/decentralized）」改造的 config 链路——走
``auto_confirm`` 同款：model TypedDict → state schema → ``_resolve_group_config``
每回合现读 → ``_build_turn_input`` 注入。但比 ``auto_confirm`` 多两处消费：
①route_entry 按 mode 分流（vh58 锁）；②``_resolve_members`` 按 mode 条件化
coordinator 纳入/排除（做法 A 图级二选一）。

六段契约（纯静态 + 函数直调 stub，不依赖 live server / 真实 LLM）：

  A. Model 字段锁——GroupConfig 含 collaboration_mode + accessor
    1. ``GroupConfig`` TypedDict 含 ``collaboration_mode: str`` 字段.
    2. ``get_collaboration_mode(config)`` accessor 存在 + 可调用.
    3. 默认返回 "centralized"（None/空 dict/缺失 key/无效值均兜底）.
    4. "decentralized" 原样返回；"centralized" 原样返回.

  B. State 字段锁——GroupState 含 collaboration_mode
    5. ``GroupState`` TypedDict 含 ``collaboration_mode: str`` 字段（与 auto_confirm/
       leader_strategy 同层 config 注入区）.

  C. group_runtime 注入锁——_resolve_group_config 返 mode + _build_turn_input 注入
    6. ``_resolve_group_config`` 返回三元组（auto_confirm, leader_strategy, mode）.
    7. ``_build_turn_input`` 在 return dict 含 ``collaboration_mode`` key.

  D. _resolve_members mode 条件化锁——做法 A 图级二选一
    8. centralized 模式排除 coordinator（维持现状）.
    9. decentralized 模式不排除 coordinator（纳入 members 建其 agent 节点）.

  E. 老群组兜底锁——无 config / 缺 key 读 "centralized"
   10. config=None → "centralized"；config={} → "centralized"；
       config={"collaboration_mode": "junk"} → "centralized"（无效值兜底）.

  F. 向后兼容锁——resident 引擎路径 + single_chat 不破
   11. ``_resolve_group_config`` group-row miss 返默认三元组（False, "", "centralized"）.
   12. single_chat 路径不读 collaboration_mode（mention.py single_chat bypass 不进群图）.
"""
from __future__ import annotations

import asyncio
import inspect
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def assert_contract() -> list[str]:
    errs: list[str] = []

    try:
        from models.group import GroupConfig, get_collaboration_mode  # type: ignore
        from engine.state import GroupState  # type: ignore
        from engine.group_runtime import GroupRuntime  # type: ignore
    except Exception as e:  # noqa: BLE001
        return [f"[import] 导入失败：{type(e).__name__}: {e}"]

    # ── A. Model 字段 ─────────────────────────────────────────
    # A1 GroupConfig TypedDict 含 collaboration_mode 字段
    try:
        hints = GroupConfig.__annotations__
        if "collaboration_mode" not in hints:
            errs.append("[A1] GroupConfig 缺 collaboration_mode 字段")
        else:
            print(f"[A1] OK  GroupConfig 含 collaboration_mode: {hints['collaboration_mode']}")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[A1] GroupConfig 注解检查异常：{type(e).__name__}: {e}")

    # A2 accessor 存在 + 可调用
    if not callable(get_collaboration_mode):
        errs.append("[A2] get_collaboration_mode 不可调用")
    else:
        print("[A2] OK  get_collaboration_mode(config) accessor 就位")

    # A3 默认返回 "centralized"
    try:
        if get_collaboration_mode(None) != "centralized":
            errs.append(f"[A3] None config 应返 'centralized'，实际 {get_collaboration_mode(None)!r}")
        elif get_collaboration_mode({}) != "centralized":
            errs.append(f"[A3] 空 dict 应返 'centralized'，实际 {get_collaboration_mode({})!r}")
        elif get_collaboration_mode({"collaboration_mode": ""}) != "centralized":
            errs.append(f"[A3] 空串应兜底 'centralized'，实际 {get_collaboration_mode({'collaboration_mode': ''})!r}")
        else:
            print("[A3] OK  None / {} / 空串 → 'centralized'（默认兜底）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[A3] accessor 默认值检查异常：{type(e).__name__}: {e}")

    # A4 有效值原样返回
    try:
        if get_collaboration_mode({"collaboration_mode": "decentralized"}) != "decentralized":
            errs.append("[A4] 'decentralized' 应原样返回")
        elif get_collaboration_mode({"collaboration_mode": "centralized"}) != "centralized":
            errs.append("[A4] 'centralized' 应原样返回")
        else:
            print("[A4] OK  'decentralized' / 'centralized' 原样返回")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[A4] accessor 有效值检查异常：{type(e).__name__}: {e}")

    # A5 无效值兜底 "centralized"
    try:
        if get_collaboration_mode({"collaboration_mode": "junk"}) != "centralized":
            errs.append(f"[A5] 无效值 'junk' 应兜底 'centralized'，实际 {get_collaboration_mode({'collaboration_mode': 'junk'})!r}")
        else:
            print("[A5] OK  无效值 'junk' → 'centralized'（兜底）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[A5] accessor 无效值检查异常：{type(e).__name__}: {e}")

    # ── B. State 字段 ─────────────────────────────────────────
    try:
        hints = GroupState.__annotations__
        if "collaboration_mode" not in hints:
            errs.append("[B6] GroupState 缺 collaboration_mode 字段")
        else:
            print(f"[B6] OK  GroupState 含 collaboration_mode: {hints['collaboration_mode']}")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B6] GroupState 注解检查异常：{type(e).__name__}: {e}")

    # ── C. group_runtime 注入 ─────────────────────────────────
    # C7 _resolve_group_config 返回三元组
    try:
        sig = inspect.signature(GroupRuntime._resolve_group_config)
        rt = inspect.get_annotations(GroupRuntime._resolve_group_config, eval_str=True)
        # annotation should be tuple[bool, str, str] now
        ret = rt.get("return", "")
        if "str" not in str(ret) or "bool" not in str(ret):
            errs.append(f"[C7] _resolve_group_config 返回注解不含 bool+str+str：{ret!r}")
        else:
            print(f"[C7] OK  _resolve_group_config 返回注解 = {ret}")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C7] _resolve_group_config 签名检查异常：{type(e).__name__}: {e}")

    # C7-run 真 DB mock 返三元组
    try:
        class _Grp:
            def __init__(self, config): self.config = config
        rt = GroupRuntime.__new__(GroupRuntime)
        rt.group_id = "g1"
        rt.coordinator_id = "c1"
        # _resolve_group_config does `from store import crud` locally — patch store.crud
        with patch("store.crud.get_group", AsyncMock(return_value=_Grp({"collaboration_mode": "decentralized", "auto_confirm": True, "leader_strategy": "L1"}))):
            result = asyncio.run(GroupRuntime._resolve_group_config(rt))
        if result != (True, "L1", "decentralized"):
            errs.append(f"[C7-run] _resolve_group_config decentralized 应返 (True, 'L1', 'decentralized')，实际 {result!r}")
        else:
            print(f"[C7-run] OK  _resolve_group_config decentralized config → {result}")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C7-run] _resolve_group_config 直调异常：{type(e).__name__}: {e}")

    # C8 _build_turn_input 注入 collaboration_mode
    try:
        rt = GroupRuntime.__new__(GroupRuntime)
        rt.group_id = "g1"
        rt.coordinator_id = "c1"
        rt._memory = []
        rt._dispatch_plan = []
        leader = {"agent_id": "c1", "agent_name": "协调者", "system_prompt": "sp"}
        turn_input = GroupRuntime._build_turn_input(
            rt, "coordinator_reply", "hi", "user", None, leader, (False, "", "decentralized"),
        )
        if turn_input.get("collaboration_mode") != "decentralized":
            errs.append(f"[C8] _build_turn_input 应注入 collaboration_mode='decentralized'，实际 {turn_input.get('collaboration_mode')!r}")
        else:
            print(f"[C8] OK  _build_turn_input 注入 collaboration_mode={turn_input['collaboration_mode']!r}")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C8] _build_turn_input 直调异常：{type(e).__name__}: {e}")

    # ── D. _resolve_members mode 条件化 ──────────────────────
    # D9 centralized 模式排除 coordinator
    try:
        class _Grp2:
            def __init__(self, config): self.config = config
        class _M:
            def __init__(self, aid, name): self.agent_id = aid; self.agent_name = name; self.agent_role = "r"
        class _A:
            def __init__(self, aid, name): self.id = aid; self.name = name; self.role = "r"; self.system_prompt = "sp"; self.mounted_skills = []

        rt_c = GroupRuntime.__new__(GroupRuntime)
        rt_c.group_id = "g1"
        rt_c.coordinator_id = "c1"
        with patch("store.crud") as crud_mock:
            crud_mock.get_group = AsyncMock(return_value=_Grp2({"collaboration_mode": "centralized"}))
            crud_mock.list_group_members_with_agent = AsyncMock(return_value=[_M("w1", "前端"), _M("c1", "协调者")])
            crud_mock.list_agents = AsyncMock(return_value=[_A("w1", "前端"), _A("c1", "协调者")])
            members_c = asyncio.run(GroupRuntime._resolve_members(rt_c))
        coord_in_members = any(m["agent_id"] == "c1" for m in members_c)
        if coord_in_members:
            errs.append(f"[D9] centralized 模式应排除 coordinator，实际 members={[m['agent_id'] for m in members_c]}")
        else:
            print(f"[D9] OK  centralized 模式排除 coordinator（members={[m['agent_id'] for m in members_c]}）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D9] _resolve_members centralized 检查异常：{type(e).__name__}: {e}")

    # D10 decentralized 模式不排除 coordinator
    try:
        rt_d = GroupRuntime.__new__(GroupRuntime)
        rt_d.group_id = "g1"
        rt_d.coordinator_id = "c1"
        with patch("store.crud") as crud_mock:
            crud_mock.get_group = AsyncMock(return_value=_Grp2({"collaboration_mode": "decentralized"}))
            crud_mock.list_group_members_with_agent = AsyncMock(return_value=[_M("w1", "前端")])
            crud_mock.list_agents = AsyncMock(return_value=[_A("w1", "前端"), _A("c1", "协调者")])
            members_d = asyncio.run(GroupRuntime._resolve_members(rt_d))
        coord_in = any(m["agent_id"] == "c1" for m in members_d)
        if not coord_in:
            errs.append(f"[D10] decentralized 模式应纳入 coordinator，实际 members={[m['agent_id'] for m in members_d]}")
        else:
            print(f"[D10] OK  decentralized 模式纳入 coordinator（members={[m['agent_id'] for m in members_d]}）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D10] _resolve_members decentralized 检查异常：{type(e).__name__}: {e}")

    # ── E. 老群组兜底（A3 已覆盖 None / {} / 无效值）─────────
    if not any(e.startswith("[A3]") or e.startswith("[A5]") for e in errs):
        print("[E11] OK  老群组兜底（None / {} / 缺 key / 无效值 → 'centralized'）已在 A3/A5 覆盖")
    else:
        errs.append("[E11] 老群组兜底失败（见 A3/A5）")

    # ── F. 向后兼容 ──────────────────────────────────────────
    # F12 group-row miss → 默认三元组
    try:
        rt_miss = GroupRuntime.__new__(GroupRuntime)
        rt_miss.group_id = "g1"
        rt_miss.coordinator_id = "c1"
        with patch("store.crud.get_group", AsyncMock(return_value=None)):
            result = asyncio.run(GroupRuntime._resolve_group_config(rt_miss))
        if result != (False, "", "centralized"):
            errs.append(f"[F12] group-row miss 应返 (False, '', 'centralized')，实际 {result!r}")
        else:
            print(f"[F12] OK  group-row miss → {result}（默认三元组兜底）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F12] group-row miss 检查异常：{type(e).__name__}: {e}")

    # F13 single_chat 不读 collaboration_mode（mention.py single_chat bypass）
    try:
        mention_py = (BACKEND / "engine" / "mention.py").read_text(encoding="utf-8")
        # route_user_message 的 single_chat bypass 分支在 route_entry 之前 return，
        # 不进群图——collaboration_mode 是群图 route_entry 才读的字段，single_chat
        # 路径 push_notify 到驻留 worker engine，不读 collaboration_mode。
        single_chat_block = re.search(
            r"if group and \(group\.config or \{\}\)\.get\(\"single_chat\"\).*?\n        return",
            mention_py, re.S,
        )
        if not single_chat_block:
            errs.append("[F13] mention.py single_chat bypass 块未找到（route_user_message 早期 return）")
        elif "collaboration_mode" in single_chat_block.group(0):
            errs.append("[F13] single_chat bypass 不应读 collaboration_mode")
        else:
            print("[F13] OK  single_chat bypass 不读 collaboration_mode（不进群图）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F13] single_chat 检查异常：{type(e).__name__}: {e}")

    return errs


def main() -> int:
    print("=== VH57 回归：群组协作模式字段链路（model→state→group_runtime→worker）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "群组协作模式 config 链路锁定：\n"
        "  · A GroupConfig 含 collaboration_mode + accessor（默认 'centralized'，无效值兜底）；\n"
        "  · B GroupState 含 collaboration_mode 字段（与 auto_confirm/leader_strategy 同层）；\n"
        "  · C _resolve_group_config 返三元组 + _build_turn_input 注入 collaboration_mode；\n"
        "  · D _resolve_members mode 条件化（centralized 排除 / decentralized 纳入 coordinator）；\n"
        "  · E 老群组兜底（None/{} / 缺 key / 无效值 → 'centralized'）；\n"
        "  · F 向后兼容（group-row miss 默认三元组 + single_chat 不读 mode）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

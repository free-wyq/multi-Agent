"""VH8 回归：coordinator_id 身份层启动缓存·配置层 per-notify 现读（task B11）.

锁住 B11 决策——``engine/registry.py`` 两类字段时效口径有意分层（非不一致 bug）：

  身份层（startup-baked，``__init__`` 落定，引擎生命周期内不再变）：
    ``coordinator_id`` / ``is_coordinator`` / ``graph_kind`` / ``single_chat`` /
    ``system_prompt``。``is_coordinator`` 派生 ``graph_kind`` → 决定编译哪张 LangGraph
    图（coordinator 图 vs worker 图），是引擎*身份*非消息级配置。每 notify 刷新
    ``coordinator_id`` 会要求重建图 + 作废 MemorySaver checkpointer 线程（coordinator
    线程的 dispatch_plan/interrupt 状态 worker 图无法解释），代价高且状态腐蚀风险大，
    故启动缓存「不再二次查库」（``_run_worker_task`` 的 report-back notify 用
    ``self.coordinator_id``）。

  配置层（per-invoke，``_handle_notify`` 每次 ``crud.get_group`` 现读）：
    ``auto_confirm`` / ``leader_strategy``。消息级行为旋钮（群设置 Modal / plan-direct
    API 可随时改），现读代价一次 DB 读（vs 下游 LLM 调用可忽略），缓存会冻结「等待确认
    vs 直接干」模式与指挥策略直到重启。

  后果（pending-restart 文档化）：``PUT /api/groups/{id}`` 改 ``coordinator_id`` 只落 DB
  行，不重建驻留引擎——换群主仅进程重启或解散重建后生效。有意分层（图身份 ≠ 消息级
  配置），非 bug。

B11 是纯文档化（不加 per-notify 刷新也不加引擎重建），故本测全静态——锁「两类字段
分属不同时效层 + 互不越界 + 文档化」的代码契约。静态比运行时实测更可靠：实测换群主
需起引擎 + 触发 notify，且要观察「不刷新」的*不存在*行为（负断言）——静态锁赋值点
即证。

六段契约：

  A. coordinator_id 身份层（startup-baked，仅 __init__ 赋值）
    1. ``__init__`` 赋 ``self.coordinator_id = coordinator_id``（启动烘焙）。
    2. 全文无 ``self.coordinator_id =`` 再赋值（__init__ 之外不刷新——身份不变）。
    3. ``self.is_coordinator`` 仅 __init__ 赋值（派生身份，不随配置变）。
    4. ``self.graph_kind`` 仅 __init__ 选图分支赋值（图编译一次性，不随 notify 切）。

  B. coordinator_id 运行期读用缓存（不二次查库）
    5. ``_run_worker_task`` report-back notify 用 ``self.coordinator_id``（缓存），
       函数体无 ``crud.get_group``（不每任务查库）。
    6. ``_on_task_timed_out`` report-back 用 ``self.coordinator_id``（缓存），无
       ``crud.get_group``。
    7. worker ``sys_for_invoke`` 传 ``"coordinator_id": self.coordinator_id``（缓存，
       供 ``_build_context_from_db`` 把协调者消息标成「协调者」）。

  C. 配置层 per-invoke 现读（auto_confirm / leader_strategy 不缓存到实例）
    8. ``_handle_notify`` coordinator 分支 ``crud.get_group(self.group_id)`` 现读 +
       ``grp.config.get("auto_confirm")``（非实例缓存）。
    9. ``leader_strategy`` 经 ``get_leader_strategy(grp.config)`` 取自同一现读 grp。
   10. 全文无 ``self.auto_confirm =`` / ``self.leader_strategy =`` 赋值（配置层不落实例）。

  D. 入站路由现读 coordinator（不读引擎缓存）
   11. ``route_user_message`` ``crud.get_group`` 现读 + 用 ``group.coordinator_id``
       落 no-mention fallback（入站消息总推给 DB 真源的当前群主）。
   12. ``route_plan_resume`` ``crud.get_group`` 现读 + ``group.coordinator_id``
       （计划确认 notify 推给 DB 当前群主，不读引擎缓存）。

  E. 文档化（B11 时效口径契约落地）
   13. ``AgentEngine`` 类 docstring 含「时效口径契约」+「身份层」+「配置层」
       （B11 文档化锚点，防后续误改加 per-notify 刷新）。
   14. ``_run_worker_task`` report-back 注释引用 B11 / 启动缓存（身份层非查库说明）。
   15. ``update_group`` 路由 docstring 含「pending-restart」/「不重建驻留引擎」
       （换群主后果文档化）。

  F. 行为零变（B11 纯文档化，MT-06 409 守卫不退化）
   16. ``update_group`` 路由换群主到非成员仍 raise 409「新群主必须是该群组的现有成员」
       （MT-06 守卫保留，B11 只加文档不动逻辑）。
   17. ``update_group`` 路由仍 ``return await crud.update_group(...)``（委托 crud，
       B11 不改写库路径）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
REGISTRY = REPO / "backend" / "engine" / "registry.py"
MENTION = REPO / "backend" / "engine" / "mention.py"
GROUPS = REPO / "backend" / "api" / "groups.py"


def _fn_body(src: str, fname: str, indent_opts=("", "    ")) -> str:
    """抽 fn 函数体到下一个同级 def（试多种缩进：模块级 0 / 类方法 4 空格）。

    含 docstring。模块级/类方法最后函数后跟 ``__all__`` / 顶层赋值 / class 时，
    ``(?=\n(?:async )?def )`` 锚点失配——回退到「函数签名到文件尾」。
    """
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


def _class_body(src: str, cls: str) -> str:
    """抽 class 到文件尾（含类 docstring + 全部方法体，供类级 docstring 断言）。"""
    m = re.search(rf"class {cls}\b.*:", src, re.S)
    if not m:
        return ""
    # 到下一个顶层 class 或文件尾
    start = m.start()
    rest = src[start:]
    nxt = re.search(r"\nclass \w", rest[1:])  # 跳过 class 自身行
    if nxt:
        return rest[: nxt.start() + 1]
    return rest


def assert_contract() -> list[str]:
    errs: list[str] = []
    registry = REGISTRY.read_text(encoding="utf-8")
    mention = MENTION.read_text(encoding="utf-8")
    groups = GROUPS.read_text(encoding="utf-8")

    # ── A. coordinator_id 身份层（startup-baked）──
    init_body = _fn_body(registry, "__init__", indent_opts=("    ",))
    if not init_body:
        errs.append("[A1] AgentEngine.__init__ 函数体未找到")
    else:
        # [1] __init__ 赋 self.coordinator_id（启动烘焙，可带 : str 类型注解）
        if not re.search(r"self\.coordinator_id\s*(:\s*str\s*)?=\s*coordinator_id\b", init_body):
            errs.append("[A1] __init__ 未赋 self.coordinator_id = coordinator_id（启动烘焙缺失）")
        else:
            print("[A1] OK  __init__ 烘焙 self.coordinator_id = coordinator_id（身份层落定）")
        # [3] __init__ 赋 self.is_coordinator（派生身份，可带 : bool 类型注解）
        if not re.search(
            r"self\.is_coordinator\s*(:\s*bool\s*)?=\s*self\.agent_id\s*==\s*coordinator_id",
            init_body,
        ):
            errs.append("[A3] __init__ 未赋 self.is_coordinator（派生身份缺失）")
        else:
            print("[A3] OK  __init__ 烘焙 self.is_coordinator（派生身份，is_coordinator == coordinator_id）")
        # [4] graph_kind 仅 __init__ 选图分支赋值（= 赋值，排除 == 比较）
        gk_assigns = re.findall(r"self\.graph_kind\s*(:\s*str\s*)?=(?!=)", registry)
        if len(gk_assigns) != 2:  # coordinator 分支 + worker 分支（两处，皆在 __init__）
            errs.append(f"[A4] self.graph_kind 赋值点 {len(gk_assigns)} 处（应仅在 __init__ 选图分支 2 处）")
        else:
            print("[A4] OK  self.graph_kind 仅 __init__ 选图分支赋值（图编译一次性）")

    # [2] 全文 self.coordinator_id = 仅 __init__ 一处（无 per-notify 刷新；排除 != 比较）
    coord_reassigns = re.findall(r"self\.coordinator_id\s*(:\s*str\s*)?=(?!=)", registry)
    if len(coord_reassigns) != 1:
        errs.append(f"[A2] self.coordinator_id 赋值 {len(coord_reassigns)} 处（应仅 __init__ 1 处，身份层不变）")
    else:
        print("[A2] OK  self.coordinator_id 仅 __init__ 赋值（无 per-notify 刷新——身份层不变）")

    # ── B. coordinator_id 运行期读用缓存（不二次查库）──
    # [5] _run_worker_task report-back 用 self.coordinator_id + 无 crud.get_group
    rwt = _fn_body(registry, "_run_worker_task", indent_opts=("    ",))
    if not rwt:
        errs.append("[B5] _run_worker_task 函数体未找到")
    else:
        # report-back notify 块用 self.coordinator_id（缓存）
        # 抽「self.coordinator_id and self.coordinator_id != agent_id」报告块
        if not re.search(r"self\.coordinator_id\s+and\s+self\.coordinator_id\s*!=\s*agent_id", rwt):
            errs.append("[B5] _run_worker_task report-back 未用 self.coordinator_id（缓存身份层）")
        elif "crud.get_group" in rwt:
            errs.append("[B5] _run_worker_task 含 crud.get_group（应读缓存不每任务查库）")
        else:
            print("[B5] OK  _run_worker_task report-back 用 self.coordinator_id（缓存，不二次查库）")

    # [6] _on_task_timed_out report-back 用 self.coordinator_id + 无 crud.get_group
    oto = _fn_body(registry, "_on_task_timed_out", indent_opts=("    ",))
    if not oto:
        errs.append("[B6] _on_task_timed_out 函数体未找到")
    else:
        if not re.search(r"self\.coordinator_id\s+and\s+self\.coordinator_id\s*!=\s*self\.agent_id", oto):
            errs.append("[B6] _on_task_timed_out report-back 未用 self.coordinator_id")
        elif "crud.get_group" in oto:
            errs.append("[B6] _on_task_timed_out 含 crud.get_group（应读缓存）")
        else:
            print("[B6] OK  _on_task_timed_out report-back 用 self.coordinator_id（缓存，不查库）")

    # [7] worker sys_for_invoke 传 coordinator_id=self.coordinator_id（缓存）
    notify_body = _fn_body(registry, "_handle_notify", indent_opts=("    ",))
    if not notify_body:
        errs.append("[B7] _handle_notify 函数体未找到")
    else:
        if '"coordinator_id": self.coordinator_id' not in notify_body:
            errs.append('[B7] _handle_notify 未传 "coordinator_id": self.coordinator_id（worker 上下文标签缺缓存）')
        else:
            print('[B7] OK  worker sys_for_invoke 传 "coordinator_id": self.coordinator_id（缓存供 _build_context_from_db 标签）')

    # ── C. 配置层 per-invoke 现读（auto_confirm / leader_strategy 不落实例）──
    if not notify_body:
        errs.append("[C8] _handle_notify 函数体未找到（[C8]/[C9] 依赖）")
    else:
        # [8] crud.get_group(self.group_id) 现读 + grp.config auto_confirm
        if "crud.get_group(self.group_id)" not in notify_body:
            errs.append("[C8] _handle_notify coordinator 分支未 crud.get_group(self.group_id) 现读配置")
        elif not re.search(r'auto_confirm\s*=\s*bool\(\s*grp\.config\.get\(\s*"auto_confirm"', notify_body):
            errs.append("[C8] auto_confirm 未从现读 grp.config 取（应 per-invoke 现读非实例缓存）")
        else:
            print("[C8] OK  auto_confirm = bool(grp.config.get('auto_confirm')) per-invoke 现读（配置层）")
        # [9] leader_strategy 经 get_leader_strategy(grp.config) 取自同一现读 grp
        if "get_leader_strategy(grp.config)" not in notify_body:
            errs.append("[C9] leader_strategy 未经 get_leader_strategy(grp.config) 取自现读 grp")
        else:
            print("[C9] OK  leader_strategy = get_leader_strategy(grp.config) per-invoke 现读（配置层）")

    # [10] 全文无 self.auto_confirm = / self.leader_strategy =（配置层不落实例）
    has_ac_cache = bool(re.search(r"self\.auto_confirm\s*=", registry))
    has_ls_cache = bool(re.search(r"self\.leader_strategy\s*=", registry))
    if has_ac_cache or has_ls_cache:
        errs.append(f"[C10] 配置层落实例缓存（auto_confirm={has_ac_cache} leader_strategy={has_ls_cache}，应 per-invoke 现读）")
    else:
        print("[C10] OK  无 self.auto_confirm/self.leader_strategy 实例赋值（配置层 per-invoke 现读不缓存）")

    # ── D. 入站路由现读 coordinator（不读引擎缓存）──
    rum = _fn_body(mention, "route_user_message", indent_opts=("",))
    if not rum:
        errs.append("[D11] route_user_message 函数体未找到")
    else:
        # [11] crud.get_group(group_id) 现读 + group.coordinator_id 落 no-mention fallback
        if "crud.get_group(group_id)" not in rum:
            errs.append("[D11] route_user_message 未 crud.get_group(group_id) 现读 coordinator")
        elif "group.coordinator_id" not in rum:
            errs.append("[D11] route_user_message 未用 group.coordinator_id（入站应推给 DB 当前群主）")
        else:
            print("[D11] OK  route_user_message crud.get_group 现读 group.coordinator_id（入站推 DB 当前群主）")

    rpr = _fn_body(mention, "route_plan_resume", indent_opts=("",))
    if not rpr:
        errs.append("[D12] route_plan_resume 函数体未找到")
    else:
        if "crud.get_group(group_id)" not in rpr:
            errs.append("[D12] route_plan_resume 未 crud.get_group(group_id) 现读 coordinator")
        elif "group.coordinator_id" not in rpr:
            errs.append("[D12] route_plan_resume 未用 group.coordinator_id")
        else:
            print("[D12] OK  route_plan_resume crud.get_group 现读 group.coordinator_id（计划确认推 DB 当前群主）")

    # ── E. 文档化（B11 时效口径契约落地）──
    cls_body = _class_body(registry, "AgentEngine")
    if not cls_body:
        errs.append("[E13] AgentEngine 类体未找到")
    else:
        # [13] 类 docstring 含时效口径契约 + 身份层 + 配置层
        has_contract = "时效口径契约" in cls_body
        has_layers = "身份层" in cls_body and "配置层" in cls_body
        if not (has_contract and has_layers):
            errs.append(f"[E13] AgentEngine 类 docstring 缺时效口径契约文档（contract={has_contract} 身份/配置层={has_layers}）")
        else:
            print("[E13] OK  AgentEngine 类 docstring 含「时效口径契约」+「身份层/配置层」（B11 文档化锚点）")

    # [14] _run_worker_task report-back 注释引用 B11 / 启动缓存
    if not rwt:
        errs.append("[E14] _run_worker_task 函数体未找到（[E14] 依赖）")
    else:
        has_b11 = "B11" in rwt or "启动缓存" in rwt or "startup-baked" in rwt
        if not has_b11:
            errs.append("[E14] _run_worker_task report-back 注释未引用 B11/启动缓存（身份层说明缺失）")
        else:
            print("[E14] OK  _run_worker_task report-back 注释引用 B11/启动缓存（身份层非查库说明）")

    # [15] update_group 路由 docstring 含 pending-restart / 不重建驻留引擎
    upd = _fn_body(groups, "update_group", indent_opts=("",))
    if not upd:
        errs.append("[E15] update_group 路由函数体未找到")
    else:
        has_restart = "pending-restart" in upd or "不重建" in upd
        if not has_restart:
            errs.append("[E15] update_group docstring 缺 pending-restart/不重建驻留引擎 文档（换群主后果未文档化）")
        else:
            print("[E15] OK  update_group docstring 含 pending-restart/不重建驻留引擎（换群主后果文档化）")

    # ── F. 行为零变（MT-06 409 守卫不退化）──
    if not upd:
        errs.append("[F16] update_group 路由函数体未找到（[F16]/[F17] 依赖）")
    else:
        # [16] 换群主到非成员仍 raise 409「新群主必须是该群组的现有成员」
        # 剔 docstring 再判可执行代码（docstring 里的 detail 文本会干扰判 raise 语句）
        upd_code = re.sub(r'""".*?"""', "", upd, flags=re.S)
        if 'status_code=409' not in upd_code or "新群主必须是该群组的现有成员" not in upd:
            errs.append("[F16] update_group 换群主到非成员 409 守卫退化（MT-06 应保留）")
        else:
            print("[F16] OK  update_group 换群主到非成员仍 raise 409（MT-06 守卫保留，B11 不动逻辑）")
        # [17] 仍 return await crud.update_group(...)
        if "return await crud.update_group(" not in upd_code:
            errs.append("[F17] update_group 未委托 crud.update_group（B11 不应改写库路径）")
        else:
            print("[F17] OK  update_group 仍 return await crud.update_group(...)（委托 crud，写库路径不变）")

    return errs


def main() -> int:
    print("=== VH8 回归：coordinator_id 身份层启动缓存 · 配置层 per-notify 现读（B11）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B11 时效口径契约文档化锁定（两类字段有意分层，非不一致 bug）：\n"
        "  · A 身份层（coordinator_id/is_coordinator/graph_kind）startup-baked，仅 __init__ 赋值，\n"
        "    引擎生命周期内不再变（启动缓存「不再二次查库」是有意为之）；\n"
        "  · B 运行期读用缓存：_run_worker_task/_on_task_timed_out report-back + worker sys_for_invoke\n"
        "    都用 self.coordinator_id（不每任务查库）；\n"
        "  · C 配置层（auto_confirm/leader_strategy）per-invoke 现读 crud.get_group，不落实例缓存；\n"
        "  · D 入站路由（route_user_message/route_plan_resume）crud.get_group 现读 group.coordinator_id\n"
        "    —— 入站总推给 DB 当前群主，引擎缓存只管 report-back；\n"
        "  · E 文档化：AgentEngine 类 docstring 时效口径契约 + report-back 注释 + update_group\n"
        "    pending-restart（换群主只落 DB 不重建引擎，重启/解散重建才生效）；\n"
        "  · F 行为零变：MT-06 非成员 409 守卫保留 + 委托 crud.update_group 写库不变（B11 纯文档化）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

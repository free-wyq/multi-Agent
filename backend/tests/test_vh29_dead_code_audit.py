"""VH29 回归：死代码重巡航——未引用函数核实 + 保留理由文档化（task B32）.

锁住 B32 换角度重巡航·死代码——grep ``# TODO`` / ``# 旧路径`` / ``# 兼容`` / 未引用函数，
逐个核实删除或文档化保留理由.

B32 审计结论（grep 标记 + AST 未引用检测双法，分三类）：

  ── 标记 grep（# TODO / # 旧路径 / # 兼容 / # 废弃）──
    · ``# TODO`` / ``# FIXME`` / ``# XXX``：全仓零命中（非 tests/）——无遗留待办标记.
    · ``# 旧路径`` / ``# 旧逻辑`` / ``废弃`` / ``deprecated`` / ``DEPRECATED``：非 tests/
      零源码标记命中（仅 vh10 测 docstring 提「新旧逻辑对照」是测试描述非源码标记）.
    · ``# 兼容`` / ``legacy`` / ``backward.compat`` / ``向后兼容``：大量命中但全是**活路径**
      的 legacy-DB-row fallback 注释（provider catalog 的 ``models=[]`` → fallback ``model``
      列、``_migrate_legacy_models`` seed、``_select_model`` 5 级 fallback）——是 B24/vh21/ve
      已锁的多模型服务商目录的向后兼容代码，非死代码（有消费方 + 有测试锁）.

  ── AST 未引用检测（@router 装饰器豁免 + introspection helper 核实）──
    AST 扫全仓 ~290 个 def/class，按「非 def 行的引用计数」筛零 src 引用候选.
    装饰器豁免：``@router.get/post/...`` 装饰的 endpoint 函数（FastAPI 经路由表反射调用，
    非显式 call）——``ws_bus`` / ``list_providers_route`` / ``create_provider_route`` 等
    ~25 个 ``*_route`` endpoint 全豁免（main.py include_router 注册其 router 模块）.

    真正零引用（src + tests 都不调）的非 endpoint 函数：
    1. ``engine/registry.py:954 AgentRegistry.remove_engine`` → **真死代码**（B32 删）.
       全仓 grep ``remove_engine`` 零调用方（src + tests）. ``stop_group``（删团队）自己
       inline 了 stop+pop 循环（963-985），未走 remove_engine. 单 engine 移除路径无消费方
       （删 agent 走 ``crud.delete_agent`` + ``stop_group``，不停单 engine）→ 删.
    2. ``agent_templates.py:237 list_categories`` → **零引用但保留**（B32 文档化保留理由）.
       introspection helper（catalog 分类元数据），前端 templates 页当前客户端硬编码分类
       未走此 endpoint，但 docstring 点明「Useful for the UI to render category filter tabs」
       是预留 UI 消费出口 → 保留 + 补注释说明保留意图.
    3. ``skill_hub.py:375 list_hubs`` → **零引用但保留**（B32 文档化保留理由）.
       与 list_categories 同型：introspection helper（技能市场 provider badge），前端当前
       客户端硬编码 badge 未走，但 docstring 点明「Useful for the UI to badge」是预留出口
       → 保留 + 补注释说明.

  ── 别名核实（_ContentExtractor 向后兼容别名）──
    4. ``engine/coordinator.py:1299 _ContentExtractor(ContentExtractor)`` → **零源码消费但
       保留**（B32 文档化保留理由）. 全仓 grep ``_ContentExtractor`` 非 tests/ 引用为零
       （coordinator.py:1356 + worker.py:172 都用公共名 ``ContentExtractor()``）. 但
       test_vh6 [B6] 显式锁定此别名存在（锁「向后兼容别名保留」契约）→ 保留 + 补注释说明
       删它需同步删 vh6 [B6] 断言（非本轮范围）.

  ── B32 修复口径（不是「全删未引用」，是「分类核实」） ──
    · 真死代码（零调用 + 无契约锁 + 无未来出口预期）→ 删（remove_engine 1 处）.
    · 零引用但有契约锁（_ContentExtractor 被 vh6 [B6] 锁）→ 保留 + 补注释说明删需同步删测.
    · 零引用但预留 UI 出口（list_categories / list_hubs 是 introspection helper）→ 保留 +
      补注释说明保留意图（前端未来消费的预期场景）.
    · ``# 兼容`` / ``legacy`` 大量命中全是活路径的 legacy-DB fallback（B24/vh21/ve 已锁）
      → 不动（有消费方 + 有测试锁，非死代码）.

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh28 同款风格.

四段契约：

  A. 真死代码已删（remove_engine 零调用 + 无契约锁）
    1. registry.py 不再有 ``async def remove_engine``.
    2. stop_group 仍 inline stop+pop（删 remove_engine 后解散路径不破）.

  B. 零引用但保留的 introspection helper 已文档化（list_categories / list_hubs）
    3. list_categories 仍在 + 注释说明保留理由（预留 UI 分类 tab 出口）.
    4. list_hubs 仍在 + 注释说明保留理由（预留 UI badge 出口）.

  C. 别名 _ContentExtractor 保留 + 文档化（vh6 [B6] 契约锁）
    5. _ContentExtractor 别名仍在（vh6 [B6] 不回归）.
    6. 别名注释说明「删需同步删 vh6 [B6] 断言」（B32 文档化）.

  D. ``# 兼容`` / legacy 命中确认是活路径非死代码（B24/vh21/ve 已锁不回归）
    7. config.py 仍有 select_active_model 5 级 legacy fallback（vh21 锁，单一真源在 config）.
    8. crud.py 仍有 _migrate_legacy_models seed（ve 锁）.
    9. 无源码 ``# TODO`` / ``# 旧路径`` / ``废弃`` 标记（全仓零遗留待办）.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
REGISTRY_PY = BACKEND / "engine" / "registry.py"
COORD_PY = BACKEND / "engine" / "coordinator.py"
TEMPLATES_PY = BACKEND / "agent_templates.py"
SKILL_HUB_PY = BACKEND / "skill_hub.py"
CONFIG_PY = BACKEND / "config.py"
CRUD_PY = BACKEND / "store" / "crud.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _grep_count(text: str, needle: str) -> int:
    """Count non-comment whole-word occurrences of needle in text (rough)."""
    count = 0
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            continue
        count += ln.count(needle)
    return count


def assert_contract() -> list[str]:
    errs: list[str] = []

    reg = _read(REGISTRY_PY)
    coord = _read(COORD_PY)
    tmpl = _read(TEMPLATES_PY)
    hub = _read(SKILL_HUB_PY)
    cfg = _read(CONFIG_PY)
    crud = _read(CRUD_PY)

    # ── A. 真死代码已删（remove_engine）──
    # [1] registry.py 不再有 async def remove_engine
    if re.search(r"^\s*async def remove_engine\b", reg, re.M):
        errs.append("[A1] registry.py 仍有 async def remove_engine（B32 删未落地——真死代码残留）")
    else:
        print("[A1] OK  registry.py 已删 async def remove_engine（零调用真死代码）")
    # [2] stop_group 仍 inline stop+pop（解散路径不破）
    sg_match = re.search(r"async def stop_group\(self, group_id", reg)
    if not sg_match:
        errs.append("[A2] stop_group 函数未找到（锚点失）")
    else:
        sg_body = reg[sg_match.start():sg_match.start() + 1800]
        has_stop = "await group[aid].stop()" in sg_body
        has_pop = "_engines.pop(group_id" in sg_body
        if has_stop and has_pop:
            print("[A2] OK  stop_group 仍 inline stop+pop（删 remove_engine 后解散路径不破）")
        else:
            errs.append(f"[A2] stop_group 解散路径破（stop={has_stop} pop={has_pop}）")

    # ── B. 零引用但保留的 introspection helper 已文档化 ──
    # [3] list_categories 仍在 + 注释说明保留理由
    lc_match = re.search(r"^def list_categories\(", tmpl, re.M)
    if not lc_match:
        errs.append("[B3] list_categories 未找到（误删——应保留）")
    else:
        lc_full = tmpl[lc_match.start():lc_match.start() + 1200]
        has_b32 = "B32" in lc_full
        has_reason = "保留" in lc_full or "预留" in lc_full or "UI" in lc_full
        if has_b32 and has_reason:
            print("[B3] OK  list_categories 保留 + B32 注释说明预留 UI 出口")
        else:
            errs.append(f"[B3] list_categories 缺 B32 保留理由注释（b32={has_b32} reason={has_reason}）")
    # [4] list_hubs 仍在 + 注释说明保留理由
    lh_match = re.search(r"^def list_hubs\(", hub, re.M)
    if not lh_match:
        errs.append("[B4] list_hubs 未找到（误删——应保留）")
    else:
        lh_full = hub[lh_match.start():lh_match.start() + 1200]
        has_b32 = "B32" in lh_full
        has_reason = "保留" in lh_full or "预留" in lh_full or "UI" in lh_full
        if has_b32 and has_reason:
            print("[B4] OK  list_hubs 保留 + B32 注释说明预留 UI 出口")
        else:
            errs.append(f"[B4] list_hubs 缺 B32 保留理由注释（b32={has_b32} reason={has_reason}）")

    # ── C. 别名 _ContentExtractor 保留 + 文档化（vh6 [B6] 契约锁）──
    # [5] _ContentExtractor 别名仍在（vh6 [B6] 不回归）
    if not re.search(r"class _ContentExtractor\(ContentExtractor\):", coord):
        errs.append("[C5] coordinator _ContentExtractor 别名丢失（vh6 [B6] 契约破）")
    else:
        print("[C5] OK  coordinator _ContentExtractor(ContentExtractor) 别名保留（vh6 不回归）")
    # [6] 别名注释说明「删需同步删 vh6 [B6] 断言」
    alias_match = re.search(r"class _ContentExtractor\(ContentExtractor\):", coord)
    if not alias_match:
        errs.append("[C6] _ContentExtractor 别名块未找到（锚点失）")
    else:
        # 取别名定义后 1200 字符（docstring + B32 注释）
        ab = coord[alias_match.start():alias_match.start() + 1200]
        has_b32 = "B32" in ab
        has_vh6_note = "vh6" in ab and ("[B6]" in ab or "B6" in ab)
        if has_b32 and has_vh6_note:
            print("[C6] OK  _ContentExtractor 别名 B32 注释说明删需同步删 vh6 [B6]（文档化保留理由）")
        else:
            errs.append(f"[C6] _ContentExtractor 别名缺 B32+vh6 注释（b32={has_b32} vh6={has_vh6_note}）")

    # ── D. ``# 兼容`` / legacy 命中确认是活路径非死代码 ──
    # [7] config.py 仍有 select_active_model 5 级 legacy fallback（vh21 锁，_select_model 在
    # store/crud.py 委托 config.select_active_model——单一真源在 config）
    if "def select_active_model" not in cfg or "legacy" not in cfg:
        errs.append("[D7] config.py select_active_model 缺失或无 legacy fallback（vh21 锁破）")
    else:
        sm_match = re.search(r"def select_active_model\(", cfg)
        if sm_match and ("is_default" in cfg[sm_match.start():sm_match.start() + 1800]):
            print("[D7] OK  config.py select_active_model 5 级 legacy fallback 仍在（vh21 锁不回归）")
        else:
            errs.append("[D7] config.py select_active_model 函数体未找到 legacy fallback（vh21 锁破）")
    # [8] crud.py 仍有 _migrate_legacy_models seed（ve 锁）
    if "def _migrate_legacy_models" not in crud:
        errs.append("[D8] crud.py _migrate_legacy_models 缺失（ve 锁破）")
    else:
        print("[D8] OK  crud.py _migrate_legacy_models seed 仍在（ve 锁不回归）")
    # [9] 无源码 # TODO / # 旧路径 / 废弃 标记（全仓零遗留待办）
    markers = []
    for py in BACKEND.rglob("*.py"):
        sp = str(py)
        if "/tests/" in sp or "\\tests\\" in sp:
            continue
        txt = py.read_text(encoding="utf-8")
        for i, ln in enumerate(txt.splitlines(), start=1):
            s = ln.strip()
            # 只查行首注释标记（# TODO / # 旧路径 / 废弃），不查字符串内
            if re.match(r"#\s*(TODO|FIXME|XXX|旧路径|旧逻辑)\b", s, re.I):
                markers.append(f"{py.relative_to(BACKEND)}:{i}: {s}")
            elif re.match(r"#\s*废弃\b", s) or re.match(r"#\s*deprecated\b", s, re.I):
                markers.append(f"{py.relative_to(BACKEND)}:{i}: {s}")
    if markers:
        errs.append(f"[D9] 全仓含遗留待办标记：{markers[:3]}")
    else:
        print("[D9] OK  全仓无源码 # TODO / # 旧路径 / 废弃 标记（零遗留待办）")

    return errs


def main() -> int:
    print("=== VH29 回归：死代码重巡航——未引用函数核实 + 保留理由文档化（B32）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B32 死代码重巡航锁定：\n"
        "  · A 真死代码已删 1 处（registry remove_engine 零调用，stop_group 自 inline stop+pop）；\n"
        "  · B 零引用 introspection helper 保留 2 处 + B32 注释（list_categories / list_hubs 预留 UI 出口）；\n"
        "  · C _ContentExtractor 别名保留 + B32 注释（vh6 [B6] 契约锁，删需同步删测）；\n"
        "  · D ``# 兼容``/legacy 命中确认是活路径（config _select_model + crud _migrate_legacy_models，vh21/ve 锁不回归）+ 全仓无源码 TODO/旧路径/废弃 标记。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

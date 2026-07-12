"""VH16 回归：getAgentColor role snake_case 主键 + 中文旧名兼容（task B19）.

锁住 B19 修复——``src/components/ChatPanel.tsx:70-79`` ``getAgentColor`` 的
``ROLE_COLORS`` 用中文角色名（``后端开发工程师`` / ``DevOps 工程师`` / ``产品经理``）
硬编码，与 codebase 其他处 snake_case role（``backend_engineer`` /
``frontend_engineer`` / ``qa_engineer`` / ``devops_engineer`` / ``product_manager``）
命名不一致：

  - 后端 ``backend/agent_templates.py`` _CATALOG 模板 role 一律 snake_case：
    ``backend_engineer`` / ``frontend_engineer`` / ``fullstack_engineer`` /
    ``qa_engineer`` / ``devops_engineer`` / ``product_manager``（line 71/83/95/107/119/131）。
  - 后端 ``backend/store/seed.py`` 落盘 role=snake_case（line 103/120：
    ``frontend_engineer`` / ``backend_engineer``；coordinator line 87）。
  - 后端 ``backend/llm/prompts.py:103/113`` 文档 role 规范：snake_case 英文
    （``frontend_engineer`` / ``backend_engineer`` / ``coordinator`` / ``data_analyst``）。
  - 前端 ``src/pages/AgentPage.tsx:42-49`` ROLES / ``src/components/Sidebar.tsx:261-265``
    表单选项用中文（``后端开发工程师`` / ... / ``产品经理`` / ``自定义``）。
  - ``src/components/AgentDetailPanel.tsx:550`` placeholder ``如：后端开发工程师``。

  后果：模板雇佣（AG-12 hire）的 agent role=snake_case，原 ROLE_COLORS 按中文键查不到 →
  落 ``?? '#8b5cf6'`` 默认紫；表单手建 agent role=中文 → 命中显色。同群两类 agent
  显色口径不一致（模板 agent 全紫，手建 agent 有主题色）。

B19 改法（role snake_case 主键 + LEGACY_ROLE_ALIASES 中文旧名兼容）：
  - ``ROLE_COLORS`` 主键改 snake_case（``backend_engineer`` / ``frontend_engineer`` /
    ``qa_engineer`` / ``devops_engineer`` / ``product_manager``），5 个显色 hex 逐字保留。
  - ``LEGACY_ROLE_ALIASES`` 中文旧名 → snake_case 归一映射（中文 role 经别名归一
    到 snake_case 主键再查色——单色源，不复制色值）。
  - 查色逻辑：``key = LEGACY_ROLE_ALIASES[agent.role] ?? agent.role`` →
    ``ROLE_COLORS[key] ?? '#8b5cf6'``。snake_case role 直接命中主键；中文 role 经别名
    归一命中主键；未知 role 落默认紫（含 fullstack_engineer / 自定义 / coordinator——
    coordinator 由 line 207 预过滤不进 getAgentColor）。

行为零变约束：
  - 5 个有显式色的角色 hex 逐字保留（backend #6366f1 / frontend #06b6d4 / qa #f59e0b /
    devops #10b981 / product #f43f5e）。
  - fullstack_engineer / 自定义 / 未知 role 原未在 ROLE_COLORS 显式键（落 ?? '#8b5cf6'
    默认），现仍不显式键 → 落同默认（原中文「自定义」键移除，因别名表无自定义→snake
    映射，且自定义本就落默认紫，行为零变）。
  - coordinator 由 line 207 ``id === 'coordinator'`` 预过滤（直接 #722ed1），
    不进 getAgentColor——B19 不动此分支。
  - 未找到 agent（id 不在 agents 列表）仍返 #722ed1（line 81 早返）。

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh15 同款风格。

七段契约：

  A. ROLE_COLORS 主键 snake_case（非中文）
    1. ``ROLE_COLORS`` 键含 ``backend_engineer``（非中文「后端开发工程师」）。
    2. ``ROLE_COLORS`` 键含 ``frontend_engineer`` / ``qa_engineer`` / ``devops_engineer``
       / ``product_manager``（全 snake_case 5 个主键）。
    3. ``ROLE_COLORS`` 无中文键（「后端开发工程师」/「前端开发工程师」/「测试工程师」/
       「DevOps 工程师」/「产品经理」均不在主键）。
    4. 5 个 hex 色值逐字保留（backend #6366f1 / frontend #06b6d4 / qa #f59e0b /
       devops #10b981 / product #f43f5e）。

  B. LEGACY_ROLE_ALIASES 中文旧名兼容
    5. ``LEGACY_ROLE_ALIASES`` 定义（中文 → snake_case 归一映射）。
    6. ``LEGACY_ROLE_ALIASES`` 含 5 个中文→snake 映射（后端开发工程师→backend_engineer 等）。
    7. LEGACY_ROLE_ALIASES 不复制色值（只映射键，色值仍在 ROLE_COLORS 单色源）。

  C. 查色逻辑（别名归一 + 主键查色 + 默认兜底）
    8. ``key = LEGACY_ROLE_ALIASES[agent.role] ?? agent.role``（别名归一，snake 直接用）。
    9. ``ROLE_COLORS[key] ?? '#8b5cf6'``（主键查色，未知落默认紫）。
   10. 未找到 agent 仍返 ``'#722ed1'``（早返，B19 不动）。
   11. coordinator 由 line ~207 ``id === 'coordinator'`` 预过滤（不进 getAgentColor）。

  D. 行为零变（5 角色显色 + 默认兜底不回归）
   12. snake_case role（backend_engineer）→ 命中 #6366f1（模板雇佣 agent 显色正确）。
   13. 中文 role（后端开发工程师）→ 经别名归一 → 命中 #6366f1（表单 agent 显色不变）。
   14. fullstack_engineer / 自定义 / 未知 role → 落 #8b5cf6 默认（原未显式键，零变）。
   15. coordinator → #722ed1（line 207 预过滤，不进 getAgentColor）。

  E. 与后端 role 规范对齐
   16. 后端 agent_templates.py _CATALOG role 一律 snake_case（6 模板：backend/frontend/
       fullstack/qa/devops/product_manager）。
   17. 后端 store/seed.py 落盘 role=snake_case（frontend_engineer / backend_engineer）。
   18. 前端 ROLE_COLORS 主键与后端 snake_case role 一一对应（5 显色 + fullstack 落默认）。

  F. 无回归（coordinator 预过滤 + getAgentColor 调用链不破）
   19. getAgentColor 仍是 ``(id: string, agents: AgentDefinition[]) => string`` 签名。
   20. getAgentColor 仍被 ChatAvatar（line ~207）调用（B19 不改调用点）。
   21. coordinator/broadcast/system 仍 line ~207 预过滤 #722ed1（不进 getAgentColor）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PANEL = REPO / "src" / "components" / "ChatPanel.tsx"
TEMPLATES = REPO / "backend" / "agent_templates.py"
SEED = REPO / "backend" / "store" / "seed.py"


def _fn_body_ts(src: str, fname: str) -> str:
    """抽 TS function 函数体到下一个 ``function``/``const``/``/**``（含注释块前缀）。"""
    m = re.search(rf"function {fname}\([^)]*\)[^{{]*\{{(.*?)\n\}}", src, re.S)
    return m.group(1) if m else ""


def _strip_ts_comments(src: str) -> str:
    """剔单行 ``//`` 注释（B16/vh13 坑延续——注释引用不计代码字面量）。"""
    return re.sub(r"//[^\n]*", "", src)


def assert_contract() -> list[str]:
    errs: list[str] = []
    panel = PANEL.read_text(encoding="utf-8")
    panel_nc = _strip_ts_comments(panel)

    body = _fn_body_ts(panel, "getAgentColor")
    if not body:
        errs.append("[setup] getAgentColor 函数体未找到")
        return errs
    body_nc = _strip_ts_comments(body)

    # 抽 ROLE_COLORS 块（含注释后的 Record 定义）
    m_colors = re.search(r"const ROLE_COLORS:\s*Record<string,\s*string>\s*=\s*\{(.*?)\}", body_nc, re.S)
    if not m_colors:
        errs.append("[setup] ROLE_COLORS 块未找到")
        colors_blk = ""
    else:
        colors_blk = m_colors.group(1)

    # 抽 LEGACY_ROLE_ALIASES 块
    m_aliases = re.search(r"const LEGACY_ROLE_ALIASES:\s*Record<string,\s*string>\s*=\s*\{(.*?)\}", body_nc, re.S)
    aliases_blk = m_aliases.group(1) if m_aliases else ""

    # ── A. ROLE_COLORS 主键 snake_case ──
    # [1] backend_engineer 键（非中文）
    if "backend_engineer:" not in colors_blk and "'backend_engineer'" not in colors_blk and '"backend_engineer"' not in colors_blk:
        errs.append("[A1] ROLE_COLORS 缺 backend_engineer 主键（仍用中文「后端开发工程师」）")
    else:
        print("[A1] OK  ROLE_COLORS 主键 backend_engineer（snake_case，非中文）")
    # [2] 5 个 snake_case 主键齐全
    snake_keys = ["backend_engineer", "frontend_engineer", "qa_engineer", "devops_engineer", "product_manager"]
    missing = [k for k in snake_keys if f"'{k}'" not in colors_blk and f'"{k}"' not in colors_blk and f"{k}:" not in colors_blk]
    if missing:
        errs.append(f"[A2] ROLE_COLORS 缺 snake_case 主键 {missing}（应 5 个全）")
    else:
        print(f"[A2] OK  ROLE_COLORS 5 个 snake_case 主键齐全：{snake_keys}")
    # [3] 无中文键
    cn_keys = ["后端开发工程师", "前端开发工程师", "测试工程师", "DevOps 工程师", "产品经理"]
    leaked_cn = [k for k in cn_keys if k in colors_blk]
    if leaked_cn:
        errs.append(f"[A3] ROLE_COLORS 仍含中文键 {leaked_cn}（应改 snake_case）")
    else:
        print(f"[A3] OK  ROLE_COLORS 无中文键（5 主键全 snake_case）")
    # [4] 5 个 hex 色值逐字保留
    expected_hex = {
        "backend_engineer": "#6366f1",
        "frontend_engineer": "#06b6d4",
        "qa_engineer": "#f59e0b",
        "devops_engineer": "#10b981",
        "product_manager": "#f43f5e",
    }
    hex_missing = [k for k, h in expected_hex.items() if h.lower() not in colors_blk.lower()]
    if hex_missing:
        errs.append(f"[A4] ROLE_COLORS 缺色值 {[f'{k}={expected_hex[k]}' for k in hex_missing]}（5 hex 应逐字保留）")
    else:
        print("[A4] OK  5 个 hex 色值逐字保留（backend #6366f1 / frontend #06b6d4 / qa #f59e0b / devops #10b981 / product #f43f5e）")

    # ── B. LEGACY_ROLE_ALIASES 中文旧名兼容 ──
    # [5] LEGACY_ROLE_ALIASES 定义
    if not m_aliases:
        errs.append("[B5] 缺 LEGACY_ROLE_ALIASES（中文旧名兼容缺失——表单 agent role=中文会落默认紫）")
    else:
        print("[B5] OK  LEGACY_ROLE_ALIASES 定义（中文旧名 → snake_case 归一映射）")
    # [6] 5 个中文→snake 映射
    if m_aliases:
        expected_aliases = {
            "后端开发工程师": "backend_engineer",
            "前端开发工程师": "frontend_engineer",
            "测试工程师": "qa_engineer",
            "DevOps 工程师": "devops_engineer",
            "产品经理": "product_manager",
        }
        alias_missing = [cn for cn, sn in expected_aliases.items() if cn not in aliases_blk or sn not in aliases_blk]
        if alias_missing:
            errs.append(f"[B6] LEGACY_ROLE_ALIASES 缺映射 {alias_missing}（中文→snake 归一不全）")
        else:
            print(f"[B6] OK  5 个中文→snake 映射齐全（{list(expected_aliases.keys())} → {list(expected_aliases.values())}）")
    # [7] LEGACY_ROLE_ALIASES 不复制色值（只映射键）
    if m_aliases:
        # 别名表里不应出现 hex 色值（色值只在 ROLE_COLORS 单色源）
        if re.search(r"#[0-9a-fA-F]{6}", aliases_blk):
            errs.append("[B7] LEGACY_ROLE_ALIASES 含 hex 色值（应只映射键，色值在 ROLE_COLORS 单色源）")
        else:
            print("[B7] OK  LEGACY_ROLE_ALIASES 不复制色值（只映射键，单色源在 ROLE_COLORS）")

    # ── C. 查色逻辑 ──
    # [8] key = LEGACY_ROLE_ALIASES[agent.role] ?? agent.role
    if not re.search(r"LEGACY_ROLE_ALIASES\[agent\.role\]\s*\?\?\s*agent\.role", body_nc):
        errs.append("[C8] 查色逻辑缺 key = LEGACY_ROLE_ALIASES[agent.role] ?? agent.role（别名归一）")
    else:
        print("[C8] OK  key = LEGACY_ROLE_ALIASES[agent.role] ?? agent.role（别名归一，snake 直接用）")
    # [9] ROLE_COLORS[key] ?? '#8b5cf6'
    if not re.search(r"ROLE_COLORS\[key\]\s*\?\?\s*'#8b5cf6'", body_nc):
        errs.append("[C9] 查色逻辑缺 ROLE_COLORS[key] ?? '#8b5cf6'（主键查色 + 默认兜底）")
    else:
        print("[C9] OK  ROLE_COLORS[key] ?? '#8b5cf6'（主键查色，未知落默认紫）")
    # [10] 未找到 agent 仍返 #722ed1（早返）
    if not re.search(r"if\s*\(!agent\)\s*return\s*'#722ed1'", body_nc):
        errs.append("[C10] getAgentColor 缺 !agent → '#722ed1' 早返（未找到 agent 兜底破）")
    else:
        print("[C10] OK  !agent → '#722ed1' 早返（未找到 agent 兜底保留）")
    # [11] coordinator 预过滤（line ~207，不进 getAgentColor）
    if not re.search(r"id\s*===\s*'coordinator'", panel_nc):
        errs.append("[C11] ChatPanel 缺 id === 'coordinator' 预过滤（coordinator 应直接 #722ed1 不进 getAgentColor）")
    else:
        print("[C11] OK  coordinator 由 id==='coordinator' 预过滤（#722ed1，不进 getAgentColor）")

    # ── D. 行为零变（5 角色显色 + 默认兜底）──
    # [12] snake_case role 命中主键（backend_engineer → #6366f1）
    if "backend_engineer" in colors_blk and "#6366f1" in colors_blk:
        print("[D12] OK  snake_case role（backend_engineer）→ #6366f1（模板雇佣 agent 显色正确）")
    else:
        errs.append("[D12] snake_case role 未命中主键色（模板雇佣 agent 会落默认紫）")
    # [13] 中文 role 经别名归一命中（后端开发工程师 → backend_engineer → #6366f1）
    if m_aliases and "后端开发工程师" in aliases_blk and "backend_engineer" in aliases_blk:
        print("[D13] OK  中文 role（后端开发工程师）→ 别名归一 → backend_engineer → #6366f1（表单 agent 显色不变）")
    else:
        errs.append("[D13] 中文 role 未经别名归一（表单 agent 显色会回归默认紫）")
    # [14] fullstack_engineer / 自定义 / 未知 role → 落 #8b5cf6（原未显式键，零变）
    #     原 ROLE_COLORS 有「自定义」键 = '#8b5cf6'；现移除该键，但「自定义」role 无别名 →
    #     落 ROLE_COLORS[未命中] ?? '#8b5cf6' = 同色。fullstack_engineer 原也无键 → 同。
    if "fullstack_engineer" not in colors_blk:
        print("[D14] OK  fullstack_engineer 未显式键 → 落 #8b5cf6 默认（原无键，零变）")
    else:
        errs.append("[D14] fullstack_engineer 显式键（原无键，行为变）")
    # [15] coordinator → #722ed1（line ~207 预过滤，[C11] 已锁）

    # ── E. 与后端 role 规范对齐 ──
    # [16] 后端 agent_templates.py _CATALOG role 一律 snake_case
    tpl = TEMPLATES.read_text(encoding="utf-8")
    # 模板 role 字段在 name 后第 3 个位置，抽所有 snake_case role 字面量
    tpl_roles = re.findall(r'"(backend_engineer|frontend_engineer|fullstack_engineer|qa_engineer|devops_engineer|product_manager)"', tpl)
    tpl_unique = sorted(set(tpl_roles))
    if len(tpl_unique) < 5:
        errs.append(f"[E16] agent_templates.py role snake_case 不全（{tpl_unique}，应 ≥5）")
    else:
        print(f"[E16] OK  agent_templates.py _CATALOG role 一律 snake_case：{tpl_unique}")
    # [17] 后端 store/seed.py 落盘 role=snake_case
    seed = SEED.read_text(encoding="utf-8")
    seed_roles = re.findall(r'role="(backend_engineer|frontend_engineer|fullstack_engineer|qa_engineer|devops_engineer|product_manager|coordinator)"', seed)
    if not seed_roles:
        errs.append("[E17] store/seed.py 落盘 role 无 snake_case（与前端 ROLE_COLORS 主键不对齐）")
    else:
        print(f"[E17] OK  store/seed.py 落盘 role=snake_case：{sorted(set(seed_roles))}")
    # [18] 前端 ROLE_COLORS 主键与后端 snake_case 一一对应（5 显色 + fullstack 落默认）
    if all(k in tpl for k in snake_keys):
        print(f"[E18] OK  前端 ROLE_COLORS 主键 {snake_keys} 与后端模板 snake_case role 一一对应")
    else:
        errs.append("[E18] 前端 ROLE_COLORS 主键与后端模板 role 未一一对应")

    # ── F. 无回归（签名 + 调用链）──
    # [19] getAgentColor 签名不变
    if not re.search(r"function getAgentColor\(\s*id:\s*string,\s*agents:\s*AgentDefinition\[\]\s*\):\s*string", panel):
        errs.append("[F19] getAgentColor 签名异常（应 (id: string, agents: AgentDefinition[]) => string）")
    else:
        print("[F19] OK  getAgentColor 签名 (id: string, agents: AgentDefinition[]) => string（不变）")
    # [20] getAgentColor 仍被调用（ChatAvatar line ~207）
    if "getAgentColor(id, agents)" not in panel and "getAgentColor(" not in panel.split("function getAgentColor", 1)[-1]:
        # 宽松：函数定义后出现 getAgentColor( 调用
        after_def = panel.split("function getAgentColor", 1)[-1]
        if "getAgentColor(" not in after_def:
            errs.append("[F20] getAgentColor 无调用方（调用链断）")
        else:
            print("[F20] OK  getAgentColor 仍被调用（ChatAvatar，B19 不改调用点）")
    else:
        print("[F20] OK  getAgentColor 仍被调用（ChatAvatar，B19 不改调用点）")
    # [21] coordinator/broadcast/system 预过滤 #722ed1（不进 getAgentColor）
    if re.search(r"id\s*===\s*'coordinator'\s*\|\|\s*id\s*===\s*'broadcast'\s*\|\|\s*id\s*===\s*'system'\s*\?\s*'#722ed1'", panel_nc) or re.search(r"id\s*===\s*'coordinator'.*?'#722ed1'.*?getAgentColor", panel_nc, re.S):
        print("[F21] OK  coordinator/broadcast/system 预过滤 #722ed1（不进 getAgentColor）")
    else:
        errs.append("[F21] coordinator/broadcast/system 预过滤缺失（会进 getAgentColor 落默认紫）")

    return errs


def main() -> int:
    print("=== VH16 回归：getAgentColor role snake_case 主键 + 中文旧名兼容（B19）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B19 getAgentColor role snake_case 主键 + 中文旧名兼容锁定：\n"
        "  · A ROLE_COLORS 主键 snake_case（backend_engineer/frontend_engineer/qa_engineer/devops_engineer/product_manager）——无中文键，5 hex 逐字保留；\n"
        "  · B LEGACY_ROLE_ALIASES 中文旧名 → snake_case 归一映射（5 映射齐全，不复制色值，单色源在 ROLE_COLORS）；\n"
        "  · C 查色逻辑：key = LEGACY_ROLE_ALIASES[role] ?? role → ROLE_COLORS[key] ?? '#8b5cf6'（别名归一 + 主键查色 + 默认兜底）+ !agent 早返 #722ed1 + coordinator 预过滤；\n"
        "  · D 行为零变：snake role 命中主键（模板 agent 显色）+ 中文 role 经别名归一命中（表单 agent 不变）+ fullstack/自定义/未知落默认紫（原无键零变）+ coordinator #722ed1；\n"
        "  · E 与后端对齐：agent_templates.py _CATALOG role snake_case + store/seed.py 落盘 snake_case + 前端主键一一对应；\n"
        "  · F 无回归：getAgentColor 签名不变 + 仍被 ChatAvatar 调用 + coordinator/broadcast/system 预过滤不破。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

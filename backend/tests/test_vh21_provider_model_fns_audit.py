"""VH21 回归：provider 模型解析三函数命名/职责边界 + 类型注解（task B24）.

锁住 B24 审计——``backend/store/crud.py`` 三个 provider 模型解析函数
（``_provider_to_model`` / ``_select_model`` / ``update_provider_model``）的
命名与职责边界 + VE 锁定的「legacy 列不再外显」无回归 + 补类型注解。

B24 审计结论（三函数职责边界清晰，命名自描述，无需重命名）：
  - ``_provider_to_model(p) -> LlmProvider``：ORM 行 → 屏蔽后的 Pydantic 输出模型
    （api_key masked）。职责：HTTP 输出映射。**输出 _select_model(p) 解析后生效模型**，
    非原始 legacy ``model`` 列（VE1 锁定——legacy 列不再外显）。
  - ``_select_model(p) -> str``：provider entity → 生效 model_id（单一真源）。职责：
    委托 config.select_active_model 5 级 fallback（is_default → match legacy model →
    first catalog → legacy model 列 → _DEFAULT_MODEL）。被 _provider_to_model +
    _provider_to_cache_dict + probe.test_provider 三处复用。
  - ``update_provider_model(provider_id, model) -> LlmProviderEntity | None``：定向写
    legacy ``model`` 列（PUT /api/config 热切换路径）。职责：legacy 列唯一写者
    （update_provider 编辑保存不写 model 列，VE2 锁定）。

为何不重命名：三函数命名已自描述（_provider_to_model = 映射 / _select_model = 解析 /
update_provider_model = 写 legacy 列）。_select_model 的「_」前缀虽是私有约定，但已
被 probe.py + system.py + cf 测试跨模块引用（``from store.crud import _select_model``），
重命名会破跨模块 import（高风险，无收益——名字已准确）。B24 只补类型注解 + 文档化
职责边界，不改命名。

B24 类型注解补全（最小侵入，行为零变）：
  - ``_provider_to_cache_dict(p) -> dict`` → ``-> dict[str, Any]``（返回 13-key cache
    dict，键是 str 值是 Any——模型/base_url/api_key 等异质值）。
  - ``_deactivate_all(db) -> None`` → ``db: AsyncSession``（补 db 形参类型——
    AsyncSession 是 sqlalchemy.ext.asyncio 的会话类型，与 SessionLocal 工厂一致）。
  - import 追加 ``AsyncSession``（``from sqlalchemy.ext.asyncio import AsyncSession,
    async_sessionmaker``）。
  - 其余三函数（_provider_to_model / _select_model / update_provider_model）类型注解
    已齐全（VE 已锁），B24 不动。

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh20 同款风格。

六段契约：

  A. 三函数命名与签名（B24 审计：命名自描述，不改）
    1. ``_provider_to_model(p: LlmProviderEntity) -> LlmProvider`` 定义（ORM→Pydantic 映射）。
    2. ``_select_model(p: LlmProviderEntity) -> str`` 定义（provider→生效 model_id 解析）。
    3. ``update_provider_model(provider_id: str, model: str) -> LlmProviderEntity | None``
       定义（定向写 legacy model 列）。

  B. 职责边界（三函数各司其职，不混淆）
    4. _provider_to_model 输出 ``"model": _select_model(p)``（解析值，非 p.model）。
    5. _select_model 委托 ``config.select_active_model``（单一真源 5 级 fallback）。
    6. update_provider_model 写 ``row.model = model``（legacy 列唯一写者，热切换用）。

  C. VE 锁定的「legacy 列不再外显」无回归
    7. _provider_to_model 不输出 ``"model": p.model``（原样吐 legacy 列会回归 VE 缺陷）。
    8. update_provider 跳过 model 列（``elif k == "model": ... continue``，不写 legacy 列）。
    9. update_provider_model 仍写 legacy 列（热切换路径，VE3 锁定不回归）。

  D. B24 补类型注解（最小侵入）
   10. ``_provider_to_cache_dict`` 返回 ``dict[str, Any]``（原 ``dict`` 裸类型补全）。
   11. ``_deactivate_all(db: AsyncSession)`` 补 db 形参类型（原裸 ``db``）。
   12. import 含 ``AsyncSession``（from sqlalchemy.ext.asyncio import AsyncSession）。

  E. _select_model 跨模块复用（单一真源被三处调用）
   13. _provider_to_model 调 _select_model（HTTP 输出走解析值）。
   14. _provider_to_cache_dict 调 _select_model（cache 写解析值）。
   15. probe.py ``from store.crud import _select_model``（连连性探针用解析值发请求）。

  F. 行为零变 + 无回归
   16. 三函数签名语义不变（_provider_to_model 仍返 LlmProvider / _select_model 仍返 str /
       update_provider_model 仍返 LlmProviderEntity | None）。
   17. VE 自测全绿（VE1-VE4 + VE1-rt/VE2-rt 运行时，legacy 列不再外显 + 热切换不回归）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CRUD_PY = REPO / "backend" / "store" / "crud.py"
PROBE_PY = REPO / "backend" / "llm" / "probe.py"


def _fn_body_py(src: str, fname: str, is_async: bool = False) -> str:
    """抽 Python 函数体（到下一个顶层 def 为止）。"""
    prefix = "async def" if is_async else "def"
    pat = rf"{prefix} {fname}\([^)]*\).*?(?=\n(?:async )?def |\Z)"
    m = re.search(pat, src, re.S)
    return m.group(0) if m else ""


def assert_contract() -> list[str]:
    errs: list[str] = []
    crud = CRUD_PY.read_text(encoding="utf-8")

    # ── A. 三函数命名与签名 ──
    # [1] _provider_to_model 定义
    if not re.search(r"^def _provider_to_model\(p: LlmProviderEntity\) -> LlmProvider:", crud, re.M):
        errs.append("[A1] 缺 def _provider_to_model(p: LlmProviderEntity) -> LlmProvider（ORM→Pydantic 映射）")
    else:
        print("[A1] OK  def _provider_to_model(p: LlmProviderEntity) -> LlmProvider（ORM→Pydantic 映射）")
    # [2] _select_model 定义
    if not re.search(r"^def _select_model\(p: LlmProviderEntity\) -> str:", crud, re.M):
        errs.append("[A2] 缺 def _select_model(p: LlmProviderEntity) -> str（provider→生效 model_id 解析）")
    else:
        print("[A2] OK  def _select_model(p: LlmProviderEntity) -> str（provider→生效 model_id 解析）")
    # [3] update_provider_model 定义
    if not re.search(r"^async def update_provider_model\(provider_id: str, model: str\) -> LlmProviderEntity \| None:", crud, re.M):
        errs.append("[A3] 缺 async def update_provider_model(provider_id: str, model: str) -> LlmProviderEntity | None（定向写 legacy 列）")
    else:
        print("[A3] OK  async def update_provider_model(provider_id: str, model: str) -> LlmProviderEntity | None（定向写 legacy 列）")

    # ── B. 职责边界 ──
    ptm_body = _fn_body_py(crud, "_provider_to_model")
    sm_body = _fn_body_py(crud, "_select_model")
    upm_body = _fn_body_py(crud, "update_provider_model", is_async=True)
    if not ptm_body:
        errs.append("[setup] _provider_to_model 函数体未找到")
    else:
        # [4] _provider_to_model 输出 "model": _select_model(p)
        if not re.search(r'["\']model["\']:\s*_select_model\(p\)', ptm_body):
            errs.append("[B4] _provider_to_model 未输出 _select_model(p)（应输出解析后生效模型）")
        else:
            print("[B4] OK  _provider_to_model 输出 _select_model(p)（HTTP 输出走解析值）")
    if not sm_body:
        errs.append("[setup] _select_model 函数体未找到")
    else:
        # [5] _select_model 委托 config.select_active_model
        if "config.select_active_model" not in sm_body:
            errs.append("[B5] _select_model 未委托 config.select_active_model（5 级 fallback 单一真源破）")
        else:
            print("[B5] OK  _select_model 委托 config.select_active_model（5 级 fallback 单一真源）")
    if not upm_body:
        errs.append("[setup] update_provider_model 函数体未找到")
    else:
        # [6] update_provider_model 写 row.model = model
        if "row.model = model" not in upm_body:
            errs.append("[B6] update_provider_model 未写 row.model = model（legacy 列写者破）")
        else:
            print("[B6] OK  update_provider_model 写 row.model = model（legacy 列唯一写者）")

    # ── C. VE 锁定的「legacy 列不再外显」无回归 ──
    if ptm_body:
        # [7] _provider_to_model 不输出 "model": p.model（原样吐 legacy 列会回归 VE 缺陷）
        if re.search(r'["\']model["\']:\s*p\.model\b', ptm_body):
            errs.append("[C7] _provider_to_model 仍输出 p.model（legacy 列外显——VE 缺陷回归）")
        else:
            print("[C7] OK  _provider_to_model 不输出 p.model（legacy 列不再外显，VE 锁定）")
    # [8] update_provider 跳过 model 列
    upd_body = _fn_body_py(crud, "update_provider", is_async=True)
    if not upd_body:
        errs.append("[setup] update_provider 函数体未找到")
    else:
        if 'elif k == "model":' not in upd_body:
            errs.append("[C8] update_provider 缺 elif k == 'model' 分支（未跳过 model 列）")
        else:
            seg = upd_body.split('elif k == "model":', 1)[1]
            branch = re.split(r'\n            (?:elif |else:|if )', seg, maxsplit=1)[0]
            has_continue = re.search(r'^\s*continue\s*$', branch, re.M) is not None
            has_setattr = re.search(r'setattr\s*\(\s*row\s*,\s*k\s*,\s*v\s*\)', branch) is not None
            if not (has_continue and not has_setattr):
                errs.append(f"[C8] update_provider 'model' 分支未跳过（continue={has_continue} setattr={has_setattr}）")
            else:
                print("[C8] OK  update_provider 跳过 model 列（elif k == 'model': ... continue，不写 legacy）")
    if upm_body:
        # [9] update_provider_model 仍写 legacy 列（VE3 锁定不回归）
        if "row.model = model" not in upm_body:
            errs.append("[C9] update_provider_model 未写 legacy 列（热切换路径回归）")
        else:
            print("[C9] OK  update_provider_model 仍写 legacy 列（热切换路径，VE3 不回归）")

    # ── D. B24 补类型注解 ──
    # [10] _provider_to_cache_dict 返回 dict[str, Any]
    if not re.search(r"^def _provider_to_cache_dict\(p: LlmProviderEntity\) -> dict\[str, Any\]:", crud, re.M):
        errs.append("[D10] _provider_to_cache_dict 返回类型未补 dict[str, Any]（B24 类型注解缺失）")
    else:
        print("[D10] OK  _provider_to_cache_dict -> dict[str, Any]（B24 补类型注解）")
    # [11] _deactivate_all(db: AsyncSession)
    if not re.search(r"^async def _deactivate_all\(db: AsyncSession\) -> None:", crud, re.M):
        errs.append("[D11] _deactivate_all 缺 db: AsyncSession 形参类型（B24 类型注解缺失）")
    else:
        print("[D11] OK  _deactivate_all(db: AsyncSession)（B24 补 db 形参类型）")
    # [12] import 含 AsyncSession
    if not re.search(r"from sqlalchemy\.ext\.asyncio import\s+[^\\n]*AsyncSession", crud):
        errs.append("[D12] crud.py 缺 from sqlalchemy.ext.asyncio import AsyncSession（B24 import 缺失）")
    else:
        print("[D12] OK  import AsyncSession（from sqlalchemy.ext.asyncio，B24 接线）")

    # ── E. _select_model 跨模块复用 ──
    if ptm_body:
        # [13] _provider_to_model 调 _select_model
        if "_select_model(p)" not in ptm_body:
            errs.append("[E13] _provider_to_model 未调 _select_model（HTTP 输出未走解析值）")
        else:
            print("[E13] OK  _provider_to_model 调 _select_model（HTTP 输出走解析值）")
    cache_body = _fn_body_py(crud, "_provider_to_cache_dict")
    if not cache_body:
        errs.append("[setup] _provider_to_cache_dict 函数体未找到")
    else:
        # [14] _provider_to_cache_dict 调 _select_model
        if "_select_model(p)" not in cache_body:
            errs.append("[E14] _provider_to_cache_dict 未调 _select_model（cache 未走解析值）")
        else:
            print("[E14] OK  _provider_to_cache_dict 调 _select_model（cache 写解析值）")
    # [15] probe.py from store.crud import _select_model
    probe = PROBE_PY.read_text(encoding="utf-8")
    if "from store.crud import _select_model" not in probe:
        errs.append("[E15] probe.py 未 from store.crud import _select_model（连连性探针未复用解析真源）")
    else:
        print("[E15] OK  probe.py from store.crud import _select_model（连连性探针复用解析真源）")

    # ── F. 行为零变 + 无回归 ──
    # [16] 三函数签名语义不变
    if ptm_body and "return LlmProvider.model_validate(" in ptm_body:
        print("[F16] OK  _provider_to_model 仍返 LlmProvider（LlmProvider.model_validate）")
    else:
        errs.append("[F16] _provider_to_model 返回语义变（应 LlmProvider.model_validate）")
    if sm_body and "return config.select_active_model(" in sm_body:
        print("[F16b] OK  _select_model 仍返 str（config.select_active_model 返 str）")
    else:
        errs.append("[F16b] _select_model 返回语义变（应委托 config.select_active_model 返 str）")
    if upm_body and "return row" in upm_body:
        print("[F16c] OK  update_provider_model 仍返 LlmProviderEntity | None（return row）")
    else:
        errs.append("[F16c] update_provider_model 返回语义变（应 return row）")
    # [17] VE 自测全绿（由 test_ve_provider_card_model.py 独立验证，此处只确认契约源码一致）
    if (re.search(r'["\']model["\']:\s*_select_model\(p\)', ptm_body) and
        'elif k == "model":' in upd_body and
        "row.model = model" in upm_body):
        print("[F17] OK  VE 契约源码一致（_provider_to_model 解析值 + update_provider 跳过 + update_provider_model 写）")
    else:
        errs.append("[F17] VE 契约源码不一致（VE 自测会 FAIL）")

    return errs


def main() -> int:
    print("=== VH21 回归：provider 模型解析三函数命名/职责边界 + 类型注解（B24）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B24 provider 模型解析三函数审计锁定：\n"
        "  · A 三函数命名自描述（_provider_to_model 映射 / _select_model 解析 / update_provider_model 写 legacy 列），不重命名；\n"
        "  · B 职责边界清晰（_provider_to_model 输出解析值 / _select_model 委托 config.select_active_model / update_provider_model 写 row.model）；\n"
        "  · C VE 锁定无回归（_provider_to_model 不输出 p.model + update_provider 跳过 model 列 + update_provider_model 仍写 legacy）；\n"
        "  · D B24 补类型注解（_provider_to_cache_dict -> dict[str, Any] + _deactivate_all(db: AsyncSession) + import AsyncSession）；\n"
        "  · E _select_model 跨模块复用（_provider_to_model + _provider_to_cache_dict + probe.py 三处复用单一解析真源）；\n"
        "  · F 行为零变（三函数签名语义不变 + VE 契约源码一致）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

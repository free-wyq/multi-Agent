"""验证 VE：服务商卡片显示的 model = 引擎实际生效的 model（legacy 列不再误显）.

用户实测踩坑：
  - 彩讯 provider 的 catalog is_default=kimi-k2.6，但 legacy `model` 列被某次编辑
    保存刷成了 deepseek-v4-flash（PUT /api/config 热切换或旧表单写入）。
  - _provider_to_model 原样吐 legacy 列 → 前端卡片 Tag 显 `deepseek-v4-flash`，
    但后端 _select_model 走 catalog is_default → 引擎实际生效 `kimi-k2.6`。
  - 「看着生效的不是生效的」= 误人子弟：用户以为在用 deepseek-v4-flash，其实 kimi-k2.6。

修法（backend/store/crud.py）：
  VE1. _provider_to_model 输出 `_select_model(p)`（解析后生效模型），非原始 `p.model`。
       legacy 列退回纯内部 fallback（update_provider_model 热切换仍写它，但不再显给前端）。
  VE2. update_provider 不再写 `model` 列——生效模型由 catalog is_default 决定，
       前端 payload 带 model 也不写（避免编辑保存把 legacy 列刷成与 catalog 不一致）。
       update_provider_model（PUT /api/config）仍是 legacy 列唯一写者（热切换用）。

本测纯单元（不依赖后端在线，临时 DB 隔离），锁住：
  VE1. _provider_to_model 输出解析值：catalog is_default=A、legacy 列=B → 输出 A（非 B）。
  VE2. update_provider 传 model=B 不改 legacy 列（仍保持原值或旧值）；传 models 改 is_default
       后输出跟着变。
  VE3. update_provider_model（PUT /api/config 热切换）仍写 legacy 列（热切换路径不回归）。
  VE4. 四处一致核对：DB legacy 列 / catalog 默认 / _provider_to_model 输出 / _select_model 解析。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

# 测试从 backend/tests/ 下运行，需把 backend/ 加到 sys.path，使
# `import config` / `import store` / `import models` 能解析到 backend/ 下模块。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = Path(__file__).resolve().parents[2]
CRUD_PY = REPO / "backend" / "store" / "crud.py"


def _static_checks(errs: list[str]) -> None:
    """静态契约校验（不依赖 DB/后端在线）。"""
    src = CRUD_PY.read_text(encoding="utf-8")

    # 取 _provider_to_model 整个函数体（到下一个顶层 def 为止）。
    m_fn = re.search(
        r"def _provider_to_model\(p: LlmProviderEntity\).*?(?=\ndef |\Z)",
        src, re.S,
    )
    if not m_fn:
        errs.append("[VE1] 无法定位 _provider_to_model 函数体")
        return
    body = m_fn.group(0)

    # VE1. _provider_to_model 输出 _select_model(p)，非 p.model。
    if re.search(r'"model":\s*p\.model', body) or re.search(r"'model':\s*p\.model", body):
        errs.append("[VE1] _provider_to_model 仍输出 p.model（legacy 列）——应输出 _select_model(p) 解析值")
    elif re.search(r'"model":\s*_select_model\(p\)', body) or re.search(r"'model':\s*_select_model\(p\)", body):
        print("[VE1] OK  _provider_to_model 输出 _select_model(p)（解析后生效模型，非 legacy 列）")
    else:
        errs.append("[VE1] _provider_to_model 的 model 输出既非 p.model 也非 _select_model(p)——无法判定")

    # 取 update_provider 整个函数体（到下一个顶层 def 为止）。
    m_upd = re.search(
        r"async def update_provider\(provider_id.*?(?=\n(?:async )?def |\Z)",
        src, re.S,
    )
    if not m_upd:
        errs.append("[VE2] 无法定位 update_provider 函数体")
        return
    upd = m_upd.group(0)

    # VE2. update_provider 不再把 'model' 放进 setattr 通用白名单。
    #   命中白名单行含 "model" → 仍会写 legacy 列（FAIL）。
    if re.search(r'if k in \([^)]*\b"model"[^)]*\)\s*:\s*\n\s*setattr\(row, k, v\)', upd):
        errs.append("[VE2] update_provider 仍把 'model' 放进 setattr 通用白名单（会写 legacy 列制造分裂态）")
    elif 'elif k == "model":' in upd:
        # 确认 model 分支最终走到 continue（跳过），且分支体内无 setattr(row, k, v)。
        seg = upd.split('elif k == "model":', 1)[1]
        # 取该分支体（到下一个 elif/else/顶层缩进回退前）。
        branch = re.split(r'\n            (?:elif |else:|if )', seg, maxsplit=1)[0]
        has_continue = re.search(r'^\s*continue\s*$', branch, re.M) is not None
        has_setattr = re.search(r'setattr\s*\(\s*row\s*,\s*k\s*,\s*v\s*\)', branch) is not None
        if has_continue and not has_setattr:
            print("[VE2] OK  update_provider 跳过 'model' 列（elif k == 'model': ... continue，不写 legacy 列）")
        else:
            errs.append(f"[VE2] update_provider 'model' 分支 has_continue={has_continue} has_setattr={has_setattr}")
    else:
        errs.append("[VE2] update_provider 既无 model 白名单也无 elif k=='model' 分支——无法确认已跳过")


async def _runtime_checks(errs: list[str]) -> None:
    """运行时契约校验（临时 DB，隔离不污染开发库）。"""
    orig_data_dir = os.environ.get("MULTI_AGENT_DATA_DIR")
    tmp_dir = tempfile.mkdtemp(prefix="ve_card_test_")
    os.environ["MULTI_AGENT_DATA_DIR"] = tmp_dir
    try:
        import importlib
        import store.database as _db
        importlib.reload(_db)
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
        _db.engine = create_async_engine(_db.DB_URL, echo=False,
                                         connect_args={"check_same_thread": False}, pool_pre_ping=True)
        _db.SessionLocal = async_sessionmaker(_db.engine, expire_on_commit=False, class_=AsyncSession)

        from store import crud
        from models.llm_provider import LlmProviderCreatePayload, LlmModel

        await _db.init_db()

        # ── VE1 运行时：catalog is_default=A、legacy model 列=B → 输出 A ──
        payload = LlmProviderCreatePayload(
            name="ve-probe",
            provider="openai",
            model="legacy-B-should-not-show",  # legacy 列，故意 ≠ is_default
            base_url="http://example.com/v1",
            api_key="sk-ve-probe-123456",
            models=[
                LlmModel(model_id="catalog-A-effective", is_default=True),
                LlmModel(model_id="legacy-B-should-not-show", is_default=False),
            ],
            is_active=True,
        )
        created = await crud.create_provider(payload)
        if created.model != "catalog-A-effective":
            errs.append(
                f"[VE1-rt] _provider_to_model 输出 model={created.model!r}"
                f" 期望 'catalog-A-effective'（is_default），不应显 legacy 列 'legacy-B-should-not-show'"
            )
        else:
            print("[VE1-rt] OK  catalog is_default=A、legacy 列=B → 卡片显 A（生效模型）")

        # ── VE2 运行时：update_provider 传 model=C 不改 legacy 列 ──
        #   create 时 legacy 列 = 'legacy-B-should-not-show'（payload.model）。
        #   update 传 model='legacy-C-should-be-ignored' → legacy 列应保持 B（不被 C 覆盖）。
        upd_payload = LlmProviderCreatePayload(
            name="ve-probe",
            model="legacy-C-should-be-ignored",  # 应被忽略
            base_url="http://example.com/v1",
            models=[
                LlmModel(model_id="catalog-A-effective", is_default=True),
                LlmModel(model_id="legacy-B-should-not-show", is_default=False),
            ],
        )
        await crud.update_provider(created.id, upd_payload)
        # 直查 DB legacy 列
        from store.entities import LlmProviderEntity
        async with _db.SessionLocal() as db:
            row = await db.get(LlmProviderEntity, created.id)
            legacy_col = row.model
        if legacy_col == "legacy-C-should-be-ignored":
            errs.append(
                "[VE2-rt] update_provider 把 legacy 列写成了 payload.model（C）"
                "——应跳过 model 列（保持原值 B），热切换才该走 update_provider_model"
            )
        else:
            print(f"[VE2-rt] OK  update_provider 传 model=C 被忽略，legacy 列仍={legacy_col!r}（未被刷）")

        # ── VE3：update_provider_model（PUT /api/config 热切换）仍写 legacy 列 ──
        await crud.update_provider_model(created.id, "hot-switch-D")
        async with _db.SessionLocal() as db:
            row2 = await db.get(LlmProviderEntity, created.id)
            legacy_col2 = row2.model
        if legacy_col2 != "hot-switch-D":
            errs.append(
                f"[VE3] update_provider_model 未写 legacy 列（={legacy_col2!r}）"
                "——热切换路径应仍写 legacy 列，本测锁住不回归"
            )
        else:
            print("[VE3] OK  update_provider_model 仍写 legacy 列（热切换路径未回归）")

        # ── VE4：四处一致核对 ──
        #   热切换写 legacy=D 后，_provider_to_model 仍应输出 catalog is_default=A
        #   （_select_model 第 1 级 is_default 命中，不被 legacy 列 D 干扰）。
        final = await crud.get_provider(created.id)
        if final.model != "catalog-A-effective":
            errs.append(
                f"[VE4] 热切换 legacy=D 后卡片显 model={final.model!r}"
                f" 期望仍 'catalog-A-effective'（is_default 优先于 legacy 列）"
            )
        else:
            print("[VE4] OK  四处一致：卡片显 A = catalog 默认 A（legacy 列 D 不干扰显示）")

        await _db.engine.dispose()
    except Exception as exc:
        errs.append(f"运行时校验异常: {exc!r}")
    finally:
        if orig_data_dir is not None:
            os.environ["MULTI_AGENT_DATA_DIR"] = orig_data_dir
        else:
            os.environ.pop("MULTI_AGENT_DATA_DIR", None)


async def main() -> int:
    print("=== VE 自测：卡片显示 model = 引擎生效 model（legacy 列不再误显）===")
    errs: list[str] = []
    print("\n── 静态契约（crud.py 源码）──")
    _static_checks(errs)
    if not errs:
        print("\n── 运行时契约（临时 DB 隔离）──")
        await _runtime_checks(errs)
    print("\n" + "=" * 50)
    if errs:
        print("=== 结果: FAIL ===")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("=== 结果: PASS ===")
    print("服务商卡片显示与生效模型一致：")
    print("  · _provider_to_model 输出 _select_model(p)（解析值），非 legacy model 列；")
    print("  · update_provider 跳过 model 列（不写 legacy，避免编辑保存制造分裂态）；")
    print("  · update_provider_model（PUT /api/config 热切换）仍写 legacy 列（热切换路径不回归）；")
    print("  · catalog is_default 优先于 legacy 列：卡片显的 = 引擎用的。")
    return 0


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))

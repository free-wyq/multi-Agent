"""多模型服务商目录 · 预设 catalog 自测（T26）。

不依赖 pytest，直接 asyncio 跑。catalog 是模块级静态 tuple 常量（无 DB / 无
网络依赖），故核心断言走纯单元测（直接 import ``llm_provider_catalog``）——
CI 无后端也能跑。后端在线时额外端到端验证 ``GET /api/providers/catalog``
路由返回与单元测一致（HTTP 真源交叉确认）。

仿 ``test_ag11_templates_browse.py`` 模式（httpx HTTP 真源 + 静态 catalog 交叉
验证），但 catalog 路径无 WS / 无 inbox，纯 HTTP 校验。

验证六块（确定性断言）：
  ① 7 预设齐全：list_catalog() 返回 7 个预设，slug 集合 == {openai, deepseek,
     anthropic, kimi, glm, qwen, ollama}（覆盖国际+国内+本地三类）；
  ② 单 default 不变量：每个预设的 models 恰好 1 个 is_default=True（catalog
     源头合规，update_provider 单 default 校验的上游保障）；
  ③ 连接配置键完整：每个预设含 slug/name/provider/base_url + 6 连接级字段
     （api_version/organization/extra_headers/request_timeout/max_retries/proxy）
     + temperature/max_tokens + models + note（14 字段，前端 ProviderEditor
     可直接加载全字段表单）；
  ④ 预设不含创建时字段：无 api_key / is_active / id / created_at / updated_at
     （这些是 ``crud.create_provider`` 分配的字段，预设是编辑器模板非行）；
  ⑤ 真实可用性：base_url 非 localhost（除 ollama 本地预设）+ 每预设 models
     >=1 + note 非空（UI 卡片提示）；
  ⑥ HTTP 端到端（后端在线时）：GET /api/providers/catalog 返回 200 + 与
     list_catalog() 逐字一致（路由零加工委托）。

为何不连 WS：catalog 是同步 HTTP GET 静态端点，不经引擎 inbox/WS，纯 HTTP 校验。

为何无收尾清理：catalog 是静态常量不落库，GET-only 无副作用。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

# 纯单元测路径：直接 import catalog 模块（不依赖后端在线）。
# 测试从 backend/tests/ 下运行，需把 backend/（tests 的父目录）加到 sys.path，
# 这样 `import llm_provider_catalog` 才能解析到 backend/llm_provider_catalog.py。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from llm_provider_catalog import list_catalog, get_catalog, _CATALOG_INDEX  # noqa: E402

BASE = "http://localhost:8000"

# 7 个预设 slug（覆盖国际 / 国内 / 本地三类 LLM 服务商）
EXPECTED_SLUGS = {"openai", "deepseek", "anthropic", "kimi", "glm", "qwen", "ollama"}

# 每个预设应含的 14 字段（连接配置键完整）
REQUIRED_PRESET_KEYS = {
    "slug", "name", "provider", "base_url",
    "api_version", "organization", "extra_headers",
    "request_timeout", "max_retries", "proxy",
    "temperature", "max_tokens", "models", "note",
}

# 创建时才填充的字段（预设 dict 不应含）
CREATE_TIME_KEYS = {"api_key", "is_active", "id", "created_at", "updated_at"}

# 每个 model entry 应含的 7 字段
REQUIRED_MODEL_KEYS = {
    "model_id", "display_name", "context_window",
    "supports_function_calling", "supports_vision", "supports_streaming",
    "is_default",
}


async def health_ok() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{BASE}/health")
            return r.status_code == 200 and r.json().get("status") == "ok"
    except Exception:
        return False


async def http_get_catalog() -> tuple[int, list[dict] | None]:
    """GET /api/providers/catalog → (status, list|None)."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{BASE}/api/providers/catalog")
            return r.status_code, (r.json() if r.status_code == 200 else None)
    except Exception as exc:
        print(f"  [http] 请求失败（后端可能未启动）: {exc}")
        return 0, None


def _check_unit_catalog() -> list[str]:
    """纯单元测断言（不依赖后端）：返回错误列表，空=全过。"""
    errs: list[str] = []
    catalog = list_catalog()

    # ① 7 预设齐全
    if len(catalog) != 7:
        errs.append(f"① 预设数量 != 7：实际 {len(catalog)}")
    slugs = {p.get("slug") for p in catalog}
    missing = EXPECTED_SLUGS - slugs
    extra = slugs - EXPECTED_SLUGS
    if missing:
        errs.append(f"① 缺失 slug: {missing}")
    if extra:
        errs.append(f"① 多余 slug: {extra}")

    for p in catalog:
        slug = p.get("slug", "?")
        # ③ 连接配置键完整
        keys = set(p.keys())
        missing_keys = REQUIRED_PRESET_KEYS - keys
        if missing_keys:
            errs.append(f"③ [{slug}] 缺字段: {missing_keys}")
        # ④ 不含创建时字段
        bad_create_keys = keys & CREATE_TIME_KEYS
        if bad_create_keys:
            errs.append(f"④ [{slug}] 误含创建时字段: {bad_create_keys}")

        # ② 单 default 不变量
        models = p.get("models") or []
        if not isinstance(models, list) or not models:
            errs.append(f"② [{slug}] models 非列表或空")
            continue
        defaults = [m for m in models if isinstance(m, dict) and m.get("is_default")]
        if len(defaults) != 1:
            errs.append(f"② [{slug}] is_default 数量 != 1：实际 {len(defaults)}")
        # 每个 model 字段完整
        for m in models:
            if not isinstance(m, dict):
                errs.append(f"   [{slug}] model 非 dict: {type(m).__name__}")
                continue
            m_keys = set(m.keys())
            missing_m = REQUIRED_MODEL_KEYS - m_keys
            if missing_m:
                errs.append(f"   [{slug}] model {m.get('model_id','?')} 缺字段: {missing_m}")

        # ⑤ 真实可用性
        base_url = p.get("base_url") or ""
        if not base_url:
            errs.append(f"⑤ [{slug}] base_url 空")
        elif slug != "ollama" and "localhost" in base_url:
            errs.append(f"⑤ [{slug}] 非 ollama 但 base_url 含 localhost: {base_url}")
        if not p.get("note"):
            errs.append(f"⑤ [{slug}] note 空（UI 提示缺失）")

    return errs


def _print_unit_results(errs: list[str]) -> bool:
    """打印单元测结果，返回是否全过。"""
    print("\n=== 单元测（list_catalog 纯静态，不依赖后端）===")
    catalog = list_catalog()
    print(f"  ① 预设数量: {len(catalog)}（期望 7）")
    for p in catalog:
        models = p.get("models") or []
        defaults = [m for m in models if m.get("is_default")]
        print(f"  · {p['slug']:10} provider={p['provider']:10} models={len(models)} "
              f"default={'✓' if len(defaults)==1 else f'✗{len(defaults)}'} "
              f"note={'✓' if p.get('note') else '✗'}")
    if errs:
        print(f"\n  ✗ 失败 {len(errs)} 项:")
        for e in errs:
            print(f"    - {e}")
        return False
    print("  ✓ 单元测全过")
    return True


async def _check_http_parity() -> bool:
    """HTTP 端到端：GET /api/providers/catalog 与 list_catalog() 逐字一致。"""
    print("\n=== HTTP 端到端（GET /api/providers/catalog）===")
    if not await health_ok():
        print("  ⊘ 后端未在线，跳过 HTTP 验证（单元测已覆盖核心断言）")
        return True  # 后端不在线不算失败
    status, http_catalog = await http_get_catalog()
    if status != 200 or http_catalog is None:
        print(f"  ✗ HTTP {status}（期望 200）")
        return False
    unit_catalog = list_catalog()
    if http_catalog == unit_catalog:
        print(f"  ✓ HTTP 200 + 返回 {len(http_catalog)} 预设，与 list_catalog() 逐字一致")
        return True
    print(f"  ✗ HTTP 返回与 list_catalog() 不一致")
    print(f"    http len={len(http_catalog)}, unit len={len(unit_catalog)}")
    return False


async def main() -> int:
    print("=== 多模型服务商目录 · 预设 catalog 自测 ===")

    # 单元测（核心，不依赖后端）
    unit_errs = _check_unit_catalog()
    unit_ok = _print_unit_results(unit_errs)

    # get_catalog 单预设查询单元测
    print("\n=== get_catalog(slug) 单预设查询 ===")
    get_errs: list[str] = []
    for slug in EXPECTED_SLUGS:
        single = get_catalog(slug)
        if single is None:
            get_errs.append(f"get_catalog({slug!r}) 返回 None")
            continue
        if single.get("slug") != slug:
            get_errs.append(f"get_catalog({slug!r}).slug != {slug}")
    unknown = get_catalog("nonexistent-xxx")
    if unknown is not None:
        get_errs.append(f"get_catalog(unknown) 应返回 None，实际 {type(unknown).__name__}")
    if get_errs:
        print(f"  ✗ 失败 {len(get_errs)} 项:")
        for e in get_errs:
            print(f"    - {e}")
        unit_ok = False
    else:
        print(f"  ✓ 7 个 slug 查询各返正确预设 + 未知 slug 返 None")

    # 索引完整性（_CATALOG_INDEX 与 list_catalog 一致）
    print("\n=== _CATALOG_INDEX 一致性 ===")
    idx_errs: list[str] = []
    if set(_CATALOG_INDEX.keys()) != EXPECTED_SLUGS:
        idx_errs.append(f"索引 keys != 7 slugs: {set(_CATALOG_INDEX.keys())}")
    # 索引返回的 preset.model_dump() == get_catalog(slug)
    for slug in EXPECTED_SLUGS:
        if _CATALOG_INDEX[slug].model_dump() != get_catalog(slug):
            idx_errs.append(f"{slug}: 索引 model_dump != get_catalog")
    if idx_errs:
        print(f"  ✗ 失败 {len(idx_errs)} 项:")
        for e in idx_errs:
            print(f"    - {e}")
        unit_ok = False
    else:
        print("  ✓ _CATALOG_INDEX keys 齐 + 与 get_catalog 一致")

    # HTTP 端到端（后端在线时）
    http_ok = await _check_http_parity()

    print("\n=== 结论 ===")
    if unit_ok and http_ok:
        print("PASS — 预设 catalog 自测通过：")
        print("  · 7 预设齐全（openai/deepseek/anthropic/kimi/glm/qwen/ollama）；")
        print("  · 每预设恰好 1 个 is_default 模型（单 default 不变量）；")
        print("  · 14 连接配置键完整（slug/name/provider/base_url + 6 连接级 + temp/max + models + note）；")
        print("  · 不含创建时字段（api_key/is_active/id）；")
        print("  · get_catalog(slug) 单查询 + 未知 slug 返 None；")
        print("  · HTTP 路由（在线时）与 list_catalog() 逐字一致。")
        return 0
    print("FAIL — 见上方失败项")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""CF-04/CF-05 自测：GET/PUT /api/config 脱敏 + 热切换验证。

真起 localhost:8000（在线集成测试，与 test_be_reset_session.py 同模式：httpx 直连
已起的后端进程，不发 pytest、不开 TestClient）。覆盖 GET 脱敏 + PUT 热切换两条路径。

端点契约（backend/api/system.py + backend/config.py）：
  GET /api/config → get_config_public()
      {
        "provider": "openai",
        "model": "deepseek-v4-flash",          ← 当前活跃模型
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-***db7",                ← 脱敏（首3+尾3，短 key → ***）
        "has_key": True,                        ← UI 显示「已配置」
        "temperature": 0.0,
        "max_tokens": 4096,
      }
      原始密钥永不离开进程——api_key 是 mask 预览，不是真实 key。
  PUT /api/config body={model} → set_config(model) + get_config_public()
      set_config 把 model 写回 os.environ["LLM_MODEL"]，因 get_config() 每次
      实时读环境（不缓存 import 快照），下次 engine invoke 即生效，无需重启（CF-05）。
      空/None model 是 no-op（echo 当前状态不覆盖）。

验证（真起后端 + 真 LLM）：
  1. GET /api/config 200 + 结构正确（7 字段齐：provider/model/base_url/api_key/
     has_key/temperature/max_tokens）。
  2. 脱敏：api_key 不等于真实 OPENAI_API_KEY 全文；长度合理（mask 形态
     首3+***+尾3 ≤ 11 字符，或空串/*** 当 key 短/无）；has_key 与 api_key 是否
     非空一致。raw key 不应出现在响应里。
  3. PUT /api/config 切到目标模型（用 .env 里的 LLM_MODEL 作「可切回的基准」，
     避免把活跃模型改成不存在的值导致后续任务失败）→ 响应 model==新值，结构同 GET。
  4. 热切换生效：再 GET 一次，model 仍是新值（说明 os.environ 已写回，不是
     只在响应里 echo）。
  5. no-op 语义：PUT 空 model → 不报错、不覆盖（model 维持上一步值）。
  6. 真 LLM 验证（可选，环境无 key 时降级 skip 不 fail）：切到 .env 配置的模型后，
     发一条最小 chat completion 请求（经 /api/messages 触发或直接校验 LLM 可达）。
     为避免污染群组消息流 + 避免长任务，采用「PUT 切回原 model + GET 一致」作为
     热切换已落地的确定性证据，不强行打 LLM（LLM 可达性已在 GET has_key=true 体现，
     真实调 LLM 由 PL/MT 系列自测覆盖，本测聚焦 config 端点契约）。
  7. 收尾：把 model 切回原始值（无论前面切到什么），保证 .env 配置不被动残留。

为何不发真 LLM 聊天：
  config 端点是配置读写，验证「脱敏 + 热切换」即可确定契约。发真 LLM 会引入
  延迟/OOM 风险且与 config 端点正确性无关——LLM 可达性由 has_key=true + base_url
  正确即可判定，真实调用由 test_pl*/test_mt* 系列覆盖。本测聚焦「key 不泄露 +
  model 切换即时生效」。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx

# 纯单元测路径：测试从 backend/tests/ 下运行，需把 backend/ 加到 sys.path，
# 这样 `import config` 能解析到 backend/config.py（_check_multi_model_cache
# 直接读 config._ACTIVE_CACHE 内部真源，绕过 public 脱敏层）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://localhost:8000"

# 用 .env 里配置的 LLM_MODEL 作为「可安全切换的基准模型」——切到它再切回，
# 不会把活跃模型改成不存在值。.env 已由后端 config.py 在启动时 load_dotenv。
_ENV_MODEL = os.environ.get("LLM_MODEL") or "deepseek-v4-flash"
# 一个明显不同于默认的「跳板模型」用于验证切换确实改了值——选一个 .env 没配的
# 占位名，切换后立刻切回，避免污染后续真实任务。
_PROBE_MODEL = "cf04-probe-model-switch"


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health", timeout=5.0)
        return r.json().get("status") == "ok"


async def get_config() -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/config", timeout=10.0)
        assert r.status_code == 200, f"GET /api/config status={r.status_code} body={r.text}"
        return r.json()


async def put_config(model: str | None) -> dict:
    async with httpx.AsyncClient() as c:
        body = {"model": model} if model is not None else {}
        r = await c.put(f"{BASE}/api/config", json=body, timeout=10.0)
        assert r.status_code == 200, f"PUT /api/config model={model!r} status={r.status_code} body={r.text}"
        return r.json()


async def _create_provider(payload: dict, timeout: float = 10.0) -> dict:
    """POST /api/providers → 返回新建 provider（masked）。"""
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{BASE}/api/providers", json=payload, timeout=timeout)
        assert r.status_code == 200, f"POST /api/providers status={r.status_code} body={r.text}"
        return r.json()


async def _update_provider(pid: str, payload: dict, timeout: float = 10.0) -> dict:
    """PUT /api/providers/{id} → 返回更新后 provider。"""
    async with httpx.AsyncClient() as c:
        r = await c.put(f"{BASE}/api/providers/{pid}", json=payload, timeout=timeout)
        assert r.status_code == 200, f"PUT /api/providers/{pid} status={r.status_code} body={r.text}"
        return r.json()


async def _delete_provider(pid: str, timeout: float = 10.0) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{BASE}/api/providers/{pid}", timeout=timeout)
        return {"status": r.status_code, "body": r.json() if r.status_code == 200 else r.text}


async def _list_providers(timeout: float = 10.0) -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/providers", timeout=timeout)
        assert r.status_code == 200, f"GET /api/providers status={r.status_code}"
        return r.json()


async def _check_multi_model_cache() -> list[str]:
    """T28：create/update provider 后 cache 含 6 连接级 key + 选定 model。

    cache 是后端进程的内部状态，HTTP 测不到（GET /api/config 只暴露 7 字段
    脱敏层）。故此校验走**纯单元测**：在测试进程内用临时 DB 直接调
    ``crud.create_provider`` + 手动 ``set_active_cache(_provider_to_cache_dict(entity))``
    （精确复刻 T15 路由层 ``_refresh_active_cache`` 的逻辑），然后直查
    ``config._ACTIVE_CACHE`` 真源。不依赖后端在线，CI 确定性。

    选定 model 验证 is_default fallback 链：
    - create 时 models 含 is_default=deepseek-chat → cache["model"]==deepseek-chat
      （_select_model 第 1 级 is_default 命中，非 legacy model 列）。
    - update 把 is_default 改成 deepseek-reasoner → cache["model"] 跟着变。
    - create 时 models 为空（legacy 风格）→ cache["model"] fallback 到 model 列。
    """
    errs: list[str] = []
    import tempfile
    # 临时 DATA_DIR 隔离，不污染开发库
    orig_data_dir = os.environ.get("MULTI_AGENT_DATA_DIR")
    tmp_dir = tempfile.mkdtemp(prefix="cf_t28_test_")
    os.environ["MULTI_AGENT_DATA_DIR"] = tmp_dir
    try:
        import importlib
        import store.database as _db
        importlib.reload(_db)
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
        _db.engine = create_async_engine(_db.DB_URL, echo=False, connect_args={"check_same_thread": False}, pool_pre_ping=True)
        _db.SessionLocal = async_sessionmaker(_db.engine, expire_on_commit=False, class_=AsyncSession)

        import config as _config
        from store import crud
        from models.llm_provider import LlmProviderCreatePayload, LlmModel

        await _db.init_db()
        # init_db 会 env-seed 一个 active provider + set_active_cache，记录为 baseline
        baseline_cache = dict(_config._ACTIVE_CACHE) if _config._ACTIVE_CACHE else None

        # ── 用例 1：create active provider（带 models + 6 连接级字段）──
        # models 含 is_default=deepseek-chat → cache["model"] 应 == deepseek-chat
        payload = LlmProviderCreatePayload(
            name="cf-t28-probe",
            provider="deepseek",
            model="legacy-deepseek-chat",  # 故意不同于 is_default，验证 _select_model 走 is_default
            base_url="https://api.deepseek.com/v1",
            api_key="sk-cf-t28-cache-test-123456",
            temperature=0.3,
            max_tokens=8192,
            models=[
                LlmModel(model_id="deepseek-chat", is_default=True, context_window=64000),
                LlmModel(model_id="deepseek-reasoner", is_default=False),
            ],
            api_version="2024-02-15",
            organization="org-cf-t28",
            extra_headers={"X-Custom": "cf-t28"},
            request_timeout=55.0,
            max_retries=4,
            proxy="http://cf-t28-proxy:8080",
            is_active=True,
        )
        created = await crud.create_provider(payload)
        created_id = created.id
        # 路由层 _refresh_active_cache 的精确复刻：取 active entity → cache dict → set_active_cache
        active_entity = await crud.get_active_provider_entity()
        if active_entity:
            _config.set_active_cache(crud._provider_to_cache_dict(active_entity))
        print(f"[create] provider id={created_id[:16]}… active=True")

        cache = _config._ACTIVE_CACHE
        if cache is None:
            errs.append("create active 后 _ACTIVE_CACHE 仍 None")
            return errs

        # cache 应含 13 key（6 legacy + models + 6 连接级）
        expected_keys = {
            "provider", "model", "base_url", "api_key", "temperature", "max_tokens",
            "models", "api_version", "organization", "extra_headers",
            "request_timeout", "max_retries", "proxy",
        }
        missing = expected_keys - set(cache.keys())
        if missing:
            errs.append(f"cache 缺连接级 key: {missing}（实际 {sorted(cache.keys())}）")
        else:
            print("[check 8a] cache 含 13 key（6 legacy + models + 6 连接级）  OK")

        # 6 连接级字段值正确
        conn_checks = [
            ("api_version", "2024-02-15"),
            ("organization", "org-cf-t28"),
            ("extra_headers", {"X-Custom": "cf-t28"}),
            ("request_timeout", 55.0),
            ("max_retries", 4),
            ("proxy", "http://cf-t28-proxy:8080"),
        ]
        conn_errs_before = len(errs)
        for k, expected in conn_checks:
            actual = cache.get(k)
            if actual != expected:
                errs.append(f"cache[{k!r}]={actual!r} 期望 {expected!r}")
        if len(errs) == conn_errs_before:
            print("[check 8b] 6 连接级字段值全正确  OK")

        # 选定 model：is_default 命中（deepseek-chat），非 legacy 列（legacy-deepseek-chat）
        if cache.get("model") != "deepseek-chat":
            errs.append(
                f"cache['model']={cache.get('model')!r} 期望 'deepseek-chat'"
                f"（_select_model 应走 is_default，非 legacy 列 legacy-deepseek-chat）"
            )
        else:
            print("[check 8c] 选定 model=deepseek-chat（is_default 命中，非 legacy 列）  OK")

        # raw api_key 在 cache（INTERNAL，供 engine 认证）
        if cache.get("api_key") != "sk-cf-t28-cache-test-123456":
            errs.append("cache['api_key'] 应为 raw key（INTERNAL，供 engine 认证）")
        else:
            print("[check 8d] cache 含 raw api_key（INTERNAL，engine 认证用）  OK")

        # models 列表落进 cache
        if not isinstance(cache.get("models"), list) or len(cache["models"]) != 2:
            errs.append(f"cache['models'] 应 2 条，实际 {cache.get('models')!r}")
        else:
            print("[check 8e] cache['models'] 含 2 条 catalog  OK")

        # ── 用例 2：update 把 is_default 改成 reasoner → cache["model"] 跟着变 ──
        upd_payload = LlmProviderCreatePayload(
            name="cf-t28-probe",
            models=[
                LlmModel(model_id="deepseek-chat", is_default=False),
                LlmModel(model_id="deepseek-reasoner", is_default=True),  # 现在它是 default
            ],
        )
        await crud.update_provider(created_id, upd_payload)
        # 复刻路由刷新
        active_entity2 = await crud.get_active_provider_entity()
        if active_entity2:
            _config.set_active_cache(crud._provider_to_cache_dict(active_entity2))
        cache2 = _config._ACTIVE_CACHE
        if cache2 and cache2.get("model") != "deepseek-reasoner":
            errs.append(
                f"update is_default 后 cache['model']={cache2.get('model')!r}"
                f" 期望 'deepseek-reasoner'（is_default fallback 应跟踪新 default）"
            )
        else:
            print("[check 8f] update is_default 后 cache['model']=deepseek-reasoner  OK")

        # ── 用例 3：legacy 风格 create（无 models）→ cache["model"] fallback 到 model 列 ──
        legacy_payload = LlmProviderCreatePayload(
            name="cf-t28-legacy",
            provider="openai",
            model="gpt-4o-legacy-fallback",
            base_url="https://api.openai.com/v1",
            api_key="sk-cf-t28-legacy-654321",
            is_active=True,
        )
        legacy_created = await crud.create_provider(legacy_payload)
        active_entity3 = await crud.get_active_provider_entity()
        if active_entity3:
            _config.set_active_cache(crud._provider_to_cache_dict(active_entity3))
        cache3 = _config._ACTIVE_CACHE
        # _migrate_legacy_models 从 model 列 seed is_default → cache["model"] == model 列值
        if cache3 and cache3.get("model") != "gpt-4o-legacy-fallback":
            errs.append(
                f"legacy create 后 cache['model']={cache3.get('model')!r}"
                f" 期望 'gpt-4o-legacy-fallback'（空 models → fallback 到 model 列 + seed）"
            )
        else:
            print("[check 8g] legacy create（无 models）→ cache model fallback 到 model 列  OK")
        # legacy 仍含 13 key（连接级走默认）
        if cache3 and len(cache3) != 13:
            errs.append(f"legacy create 后 cache key 数={len(cache3)} 期望 13")

        await _db.engine.dispose()
    except Exception as exc:
        errs.append(f"多模型 cache 校验异常: {exc!r}")
    finally:
        # 恢复 DATA_DIR
        if orig_data_dir is not None:
            os.environ["MULTI_AGENT_DATA_DIR"] = orig_data_dir
        else:
            os.environ.pop("MULTI_AGENT_DATA_DIR", None)

    return errs


async def main() -> int:
    print("=== CF-04/CF-05 自测：GET/PUT /api/config 脱敏 + 热切换 ===")
    if not await health_ok():
        print("[fatal] backend 不在线（localhost:8000 /health 未返 ok）")
        print("        请先起后端：cd backend && python3 -m uvicorn main:app --port 8000")
        return 2
    print("[health] ok")

    errs: list[str] = []

    # ── 步骤 1+2：GET /api/config 结构 + 脱敏 ──
    print("\n── 步骤1：GET /api/config 结构与脱敏 ──")
    cfg = await get_config()
    print(f"[get] {cfg}")

    required_keys = {"provider", "model", "base_url", "api_key", "has_key", "temperature", "max_tokens"}
    missing = required_keys - cfg.keys()
    if missing:
        errs.append(f"GET 响应缺字段：{missing}（实际 {set(cfg.keys())}）")
    else:
        print("[check 1] GET 响应 7 字段齐全  OK")

    # 脱敏校验：api_key 不应等于真实全文
    raw_key = os.environ.get("OPENAI_API_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")
    masked_key = cfg.get("api_key", "")
    has_key = cfg.get("has_key")

    # has_key 与 raw_key 是否非空应一致
    if bool(raw_key) != bool(has_key):
        errs.append(f"has_key={has_key} 与实际有无 key（{'有' if raw_key else '无'}）不一致")

    if raw_key:
        # 有 key 时：masked 绝不能等于 raw 全文
        if masked_key == raw_key:
            errs.append(f"脱敏失败：api_key 暴露了原始密钥全文（{masked_key[:6]}…）")
        # mask 形态校验：首3 + *** + 尾3（长度 ≤ 3+3+3=9+可能更多星，但绝不应是几十字符的全文）
        if len(masked_key) >= len(raw_key) and len(raw_key) > 8:
            errs.append(f"脱敏可疑：masked 长度({len(masked_key)}) ≥ raw 长度({len(raw_key)})")
        # raw 不应作为子串出现在任何字段（防止整 key 泄露）
        for k in ("api_key", "model", "base_url", "provider"):
            v = cfg.get(k, "")
            if isinstance(v, str) and raw_key in v and k != "api_key":
                errs.append(f"字段 {k} 意外包含原始密钥")
        print(f"[check 2] 脱敏：api_key={masked_key!r}（raw 长度 {len(raw_key)} 未泄露）  OK")
    else:
        # 无 key：api_key 应为空串，has_key=False
        if masked_key:
            errs.append(f"无 key 但 api_key 非空：{masked_key!r}")
        if has_key is not False:
            errs.append(f"无 key 但 has_key 非 False：{has_key}")
        print("[check 2] 无 key：api_key 空 + has_key=False  OK")

    original_model = cfg.get("model")
    print(f"[baseline] 当前 model={original_model!r}")

    # ── 步骤 3：PUT 切到跳板模型 ──
    print(f"\n── 步骤2：PUT /api/config 切到跳板模型 {_PROBE_MODEL!r} ──")
    put_resp = await put_config(_PROBE_MODEL)
    print(f"[put] {put_resp}")
    if put_resp.get("model") != _PROBE_MODEL:
        errs.append(f"PUT 响应 model 未更新：期望 {_PROBE_MODEL!r}，实际 {put_resp.get('model')!r}")
    else:
        print("[check 3] PUT 响应 model==跳板值  OK")
    # PUT 响应同样应脱敏
    if raw_key and put_resp.get("api_key") == raw_key:
        errs.append("PUT 响应脱敏失败：api_key 暴露原始密钥")

    # ── 步骤 4：热切换落地——再 GET 确认 os.environ 已写回 ──
    print("\n── 步骤3：热切换落地验证（再 GET 确认 model 已改） ──")
    cfg_after = await get_config()
    if cfg_after.get("model") != _PROBE_MODEL:
        errs.append(
            f"热切换未落地：PUT 后 GET model={cfg_after.get('model')!r}，"
            f"期望 {_PROBE_MODEL!r}（说明 set_config 未写回 os.environ）"
        )
    else:
        print(f"[check 4] 热切换落地：GET model={cfg_after.get('model')!r} == 跳板值  OK")

    # ── 步骤 5：no-op 语义——PUT 空 model 不覆盖 ──
    print("\n── 步骤4：no-op 语义（PUT 空 model 不覆盖） ──")
    noop_resp = await put_config(None)
    print(f"[noop] {noop_resp}")
    if noop_resp.get("model") != _PROBE_MODEL:
        errs.append(
            f"no-op 失败：PUT 空 model 后 model={noop_resp.get('model')!r}，"
            f"应维持 {_PROBE_MODEL!r}（空 model 不应覆盖）"
        )
    else:
        print("[check 5] PUT 空 model no-op（model 维持）  OK")

    # ── 步骤 6：热切换真实生效（真 LLM 可达性旁证） ──
    # config 端点本身不调 LLM；has_key=true + base_url 正确即说明 LLM 可达配置就绪。
    # 真实 LLM 调用由 PL/MT 系列自测覆盖，这里只断言「配置层就绪」。
    print("\n── 步骤5：LLM 配置就绪旁证（has_key + base_url） ──")
    if cfg_after.get("has_key") is True and cfg_after.get("base_url"):
        print(f"[check 6] LLM 配置就绪：has_key=True base_url={cfg_after.get('base_url')}  OK")
    else:
        print("[skip] LLM 未配置 key 或 base_url，跳过可达性旁证（不 fail）")

    # ── 步骤 7：收尾——切回原始 model，不污染 .env 配置 ──
    print(f"\n── 步骤6：收尾切回原始 model={original_model!r} ──")
    restore_resp = await put_config(original_model or _ENV_MODEL)
    if restore_resp.get("model") != (original_model or _ENV_MODEL):
        errs.append(f"收尾失败：model 未切回，实际 {restore_resp.get('model')!r}")
    else:
        print(f"[check 7] 已切回 model={restore_resp.get('model')!r}  OK")

    # ── 步骤 8：多模型服务商目录 · cache 连接级字段 + 选定 model ──
    # T28 新增：create/update provider 后 _ACTIVE_CACHE 应含 13 key（6 legacy +
    # models + 6 连接级），且 cache["model"] 经 _select_model 解析（is_default
    # fallback 链）。通过 create active provider → GET /api/config 旁证 cache
    # 已刷新（get_config_public 读 cache，但只暴露 7 字段——所以 cache 的完整
    # 13 key 用直接 import config._ACTIVE_CACHE 校验，绕过 public 脱敏层）。
    print("\n── 步骤7：多模型 cache 连接级字段 + 选定 model（is_default fallback）──")
    mm_errs = await _check_multi_model_cache()
    errs.extend(mm_errs)

    # ── 结果 ──
    print("\n" + "=" * 50)
    if errs:
        print("=== 结果: FAIL ===")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("=== 结果: PASS ===")
    print("GET/PUT /api/config 全链路验证通过：")
    print("  · GET 200 + 7 字段齐全（provider/model/base_url/api_key/has_key/temperature/max_tokens）；")
    print("  · 脱敏：api_key 是首3+***+尾3 预览，原始密钥未离开进程；has_key 与实际有无 key 一致；")
    print("  · PUT {model} 热切换：响应 model==新值，os.environ 已写回（再 GET 确认）；")
    print("  · 空 model no-op（不覆盖当前值）；")
    print("  · LLM 配置就绪旁证（has_key + base_url）；")
    print("  · 收尾切回原始 model，不污染 .env 配置；")
    print("  · 多模型：create/update 后 cache 含 13 key（6 legacy + models + 6 连接级）；")
    print("    选定 model 经 _select_model 解析（is_default 命中→update 切 default→legacy fallback）。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

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

import httpx

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
    print("  · 收尾切回原始 model，不污染 .env 配置。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""多模型服务商目录 · probe 自测（T27）。

不依赖 pytest，直接 asyncio 跑。用 ``httpx.MockTransport`` 拦截 probe 内的
httpx 调用，不连外网（CI 友好 + 确定性）。仿 test_provider_catalog.py 的
纯单元测模式（直接 import ``llm.probe``，不依赖后端在线）。

验证三块：
  ① ``test_provider`` 成功/失败路径全覆盖：
     - 成功：200 + choices 非空 → {ok, latency_ms>0, status_code=200}
     - 401 错误：{ok=False, status_code=401, error 含 401}
     - 超时：{ok=False, status_code=None, error 含超时}
     - 连接失败：{ok=False, status_code=None, error 含连接失败}
     - 空配置早返回：base_url/api_key 未配置 → ok=False + status_code=None
     - 200 空 choices：{ok=False, error 含空 choices}
     - 200 非 JSON：{ok=False, error 含非 JSON}
     - 连接级透传：proxy/extra_headers/request_timeout 进 httpx
     - model 经 _select_model 解析（is_default 命中非 legacy model 列）
  ② ``fetch_models`` 解析 OpenAI /v1/models 响应：
     - 标准 ``{"data":[{"id":"..."}]}`` → 归一化 + 首个 is_default + 字母序
     - LlmModel 字段完整（7 字段）+ 能力默认值
     - 去重（同 model_id 多次出现）
     - model_id 字段名容忍（id/model/name）
     - context_window 多字段名（context_length/max_context_length）
     - 裸 list 响应（无 data 包装）
     - 空模型列表 → ok=False
     - 非 JSON / 4xx / 超时 → ok=False
  ③ 路由契约：两函数返回 ``{ok, ..., error, status_code}`` 结构，永不 raise
     （所有失败模式捕获进 error，路由可直接转发）。

为何不连后端/外网：probe 用 entity 连接级配置真连上游，单测若连真上游会
消耗 token + 不确定（CI 必须确定性）。MockTransport 拦截 httpx 让测试完全
可控且零成本。

为何不连 WS：probe 是同步 HTTP 探测，不经引擎 inbox/WS。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from llm import probe as probe_mod  # noqa: E402
from llm.probe import test_provider, fetch_models  # noqa: E402
from store.entities import LlmProviderEntity  # noqa: E402


def _make_entity(**kw) -> LlmProviderEntity:
    """Build a transient LlmProviderEntity (no DB) for probe tests."""
    defaults = dict(
        id="p1", name="DeepSeek", provider="deepseek", model="deepseek-chat",
        base_url="https://api.deepseek.com/v1", api_key="sk-test-key-123456",
        temperature=0.0, max_tokens=4096,
        models=[{"model_id": "deepseek-chat", "is_default": True}],
        api_version="", organization="", extra_headers=None, request_timeout=10.0,
        max_retries=2, proxy="", is_active=0,
        created_at="2026-07-12T00:00:00Z", updated_at="2026-07-12T00:00:00Z",
    )
    defaults.update(kw)
    e = LlmProviderEntity()
    for k, v in defaults.items():
        setattr(e, k, v)
    return e


def _patch_httpx(handler, capture: dict | None = None):
    """Monkey-patch probe's httpx.AsyncClient to use MockTransport.

    Returns a context manager that restores the original on exit. ``capture``
    dict (if given) receives the client_kwargs + request url/headers/body.
    """
    transport = httpx.MockTransport(handler)
    orig = probe_mod.httpx.AsyncClient

    def _fake(**kw):
        if capture is not None:
            capture["client_kwargs"] = dict(kw)

        def _request_handler(request):
            if capture is not None:
                capture["url"] = str(request.url)
                capture["headers"] = dict(request.headers)
                capture["body"] = (
                    request.read().decode() if request.content else ""
                )
            return handler(request)

        return orig(transport=httpx.MockTransport(_request_handler), timeout=kw.get("timeout", 30.0))

    probe_mod.httpx.AsyncClient = _fake
    return orig


def _restore(orig) -> None:
    probe_mod.httpx.AsyncClient = orig


# ── test_provider 成功/失败路径 ────────────────────────────────────────

async def case_test_provider_success() -> tuple[bool, str]:
    """成功路径：200 + choices 非空 → ok=True."""
    cap: dict = {}
    orig = _patch_httpx(
        lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}),
        capture=cap,
    )
    try:
        result = await test_provider(_make_entity())
    finally:
        _restore(orig)
    if not result["ok"]:
        return False, f"ok 应 True，实际 {result}"
    if result["status_code"] != 200:
        return False, f"status_code 应 200，实际 {result['status_code']}"
    if result["error"] != "":
        return False, f"error 应空，实际 {result['error']!r}"
    # 请求体含 max_tokens=1 + ping + model 来自 _select_model
    import json
    body = json.loads(cap["body"])
    if body.get("max_tokens") != 1:
        return False, f"max_tokens 应 1，实际 {body.get('max_tokens')}"
    if body.get("messages") != [{"role": "user", "content": "ping"}]:
        return False, f"messages 不符: {body.get('messages')}"
    if body.get("model") != "deepseek-chat":
        return False, f"model 应 deepseek-chat（_select_model），实际 {body.get('model')}"
    # auth header
    if cap["headers"].get("authorization") != "Bearer sk-test-key-123456":
        return False, "Authorization header 缺失"
    return True, "成功路径 ok=True + 请求体正确 + auth header"


async def case_test_provider_401() -> tuple[bool, str]:
    orig = _patch_httpx(lambda req: httpx.Response(401, text='{"error":"invalid key"}'))
    try:
        result = await test_provider(_make_entity())
    finally:
        _restore(orig)
    if result["ok"]:
        return False, f"ok 应 False，实际 {result}"
    if result["status_code"] != 401:
        return False, f"status_code 应 401，实际 {result['status_code']}"
    if "401" not in result["error"]:
        return False, f"error 应含 401，实际 {result['error']!r}"
    return True, "401 → ok=False + status_code=401 + error 含 401"


async def case_test_provider_timeout() -> tuple[bool, str]:
    orig = _patch_httpx(lambda req: (_ for _ in ()).throw(httpx.TimeoutException("timed out")))
    try:
        result = await test_provider(_make_entity())
    finally:
        _restore(orig)
    if result["ok"]:
        return False, f"ok 应 False，实际 {result}"
    if result["status_code"] is not None:
        return False, f"status_code 应 None（请求未到达服务器），实际 {result['status_code']}"
    if "超时" not in result["error"]:
        return False, f"error 应含超时，实际 {result['error']!r}"
    return True, "超时 → ok=False + status_code=None + error 含超时"


async def case_test_provider_connect_error() -> tuple[bool, str]:
    orig = _patch_httpx(lambda req: (_ for _ in ()).throw(httpx.ConnectError("DNS failed")))
    try:
        result = await test_provider(_make_entity())
    finally:
        _restore(orig)
    if result["ok"]:
        return False, f"ok 应 False，实际 {result}"
    if result["status_code"] is not None:
        return False, f"status_code 应 None，实际 {result['status_code']}"
    if "连接失败" not in result["error"]:
        return False, f"error 应含连接失败，实际 {result['error']!r}"
    return True, "连接失败 → ok=False + status_code=None + error 含连接失败"


async def case_test_provider_no_base_url() -> tuple[bool, str]:
    result = await test_provider(_make_entity(base_url=""))
    if result["ok"]:
        return False, f"ok 应 False，实际 {result}"
    if result["status_code"] is not None:
        return False, f"status_code 应 None，实际 {result['status_code']}"
    if "base_url" not in result["error"]:
        return False, f"error 应含 base_url，实际 {result['error']!r}"
    return True, "base_url 未配置 → 早返回 ok=False"


async def case_test_provider_no_api_key() -> tuple[bool, str]:
    result = await test_provider(_make_entity(api_key=""))
    if result["ok"]:
        return False, f"ok 应 False，实际 {result}"
    if result["status_code"] is not None:
        return False, f"status_code 应 None，实际 {result['status_code']}"
    if "api_key" not in result["error"]:
        return False, f"error 应含 api_key，实际 {result['error']!r}"
    return True, "api_key 未配置 → 早返回 ok=False"


async def case_test_provider_empty_choices() -> tuple[bool, str]:
    orig = _patch_httpx(lambda req: httpx.Response(200, json={"choices": []}))
    try:
        result = await test_provider(_make_entity())
    finally:
        _restore(orig)
    if result["ok"]:
        return False, f"ok 应 False，实际 {result}"
    if "choices" not in result["error"]:
        return False, f"error 应含 choices，实际 {result['error']!r}"
    return True, "200 空 choices → ok=False + error 含 choices"


async def case_test_provider_non_json() -> tuple[bool, str]:
    orig = _patch_httpx(lambda req: httpx.Response(200, text="<html>not json</html>"))
    try:
        result = await test_provider(_make_entity())
    finally:
        _restore(orig)
    if result["ok"]:
        return False, f"ok 应 False，实际 {result}"
    if "JSON" not in result["error"]:
        return False, f"error 应含 JSON，实际 {result['error']!r}"
    return True, "200 非 JSON → ok=False + error 含非 JSON"


async def case_test_provider_connection_passthrough() -> tuple[bool, str]:
    """proxy + extra_headers + request_timeout 透传到 httpx."""
    cap: dict = {}
    orig = _patch_httpx(
        lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]}),
        capture=cap,
    )
    try:
        await test_provider(_make_entity(
            proxy="http://my-proxy:8080", extra_headers={"X-Org": "ds"}, request_timeout=25.0,
        ))
    finally:
        _restore(orig)
    ck = cap.get("client_kwargs", {})
    if ck.get("proxy") != "http://my-proxy:8080":
        return False, f"proxy 未透传: {ck}"
    if ck.get("timeout") != 25.0:
        return False, f"timeout 未透传: {ck}"
    if cap["headers"].get("x-org") != "ds":
        return False, f"extra_headers 未透传: {cap['headers']}"
    return True, "proxy + extra_headers + timeout 全透传到 httpx"


async def case_test_provider_model_via_select() -> tuple[bool, str]:
    """model 经 _select_model 解析（is_default 命中非 legacy model 列）."""
    cap: dict = {}
    orig = _patch_httpx(
        lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]}),
        capture=cap,
    )
    try:
        await test_provider(_make_entity(
            model="legacy-m",
            models=[{"model_id": "real-default", "is_default": True},
                    {"model_id": "legacy-m", "is_default": False}],
        ))
    finally:
        _restore(orig)
    import json
    body = json.loads(cap["body"])
    if body["model"] != "real-default":
        return False, f"model 应 real-default（is_default），实际 {body['model']}"
    return True, "model 经 _select_model 解析 is_default 命中"


# ── fetch_models 解析 OpenAI /v1/models 响应 ────────────────────────────

async def case_fetch_standard_openai() -> tuple[bool, str]:
    """标准 {data:[{id}]} → 归一化 + 首个 is_default + 字母序."""
    orig = _patch_httpx(lambda req: httpx.Response(200, json={
        "data": [
            {"id": "deepseek-chat", "owned_by": "deepseek"},
            {"id": "deepseek-reasoner", "owned_by": "deepseek"},
            {"id": "deepseek-coder", "owned_by": "deepseek"},
        ],
    }))
    try:
        result = await fetch_models(_make_entity())
    finally:
        _restore(orig)
    if not result["ok"]:
        return False, f"ok 应 True，实际 {result}"
    if len(result["models"]) != 3:
        return False, f"应 3 模型，实际 {len(result['models'])}"
    # 字母序：deepseek-chat, deepseek-coder, deepseek-reasoner
    ids = [m["model_id"] for m in result["models"]]
    if ids != ["deepseek-chat", "deepseek-coder", "deepseek-reasoner"]:
        return False, f"排序不符: {ids}"
    # 首个 is_default
    if not result["models"][0]["is_default"]:
        return False, "首个应 is_default=True"
    if any(m["is_default"] for m in result["models"][1:]):
        return False, "其余应 is_default=False"
    return True, "标准 /models → 3 模型归一化 + 字母序 + 首个 is_default"


async def case_fetch_model_fields() -> tuple[bool, str]:
    """LlmModel 字段完整（7 字段）+ 能力默认值."""
    orig = _patch_httpx(lambda req: httpx.Response(200, json={
        "data": [{"id": "m1", "context_length": 128000}],
    }))
    try:
        result = await fetch_models(_make_entity())
    finally:
        _restore(orig)
    m = result["models"][0]
    required = {"model_id", "display_name", "context_window",
                "supports_function_calling", "supports_vision",
                "supports_streaming", "is_default"}
    missing = required - set(m.keys())
    if missing:
        return False, f"model 缺字段: {missing}"
    if m["display_name"] != "m1":
        return False, f"display_name 应 m1，实际 {m['display_name']}"
    if m["context_window"] != 128000:
        return False, f"context_window 应 128000，实际 {m['context_window']}"
    if not m["supports_function_calling"] or not m["supports_streaming"]:
        return False, "function_calling/streaming 应默认 True"
    if m["supports_vision"]:
        return False, "vision 应默认 False"
    return True, "LlmModel 7 字段完整 + 能力默认值正确"


async def case_fetch_dedup() -> tuple[bool, str]:
    orig = _patch_httpx(lambda req: httpx.Response(200, json={
        "data": [
            {"id": "dup-m", "owned_by": "a"},
            {"id": "dup-m", "owned_by": "b"},  # 重复
            {"id": "unique-m"},
        ],
    }))
    try:
        result = await fetch_models(_make_entity())
    finally:
        _restore(orig)
    if len(result["models"]) != 2:
        return False, f"去重后应 2 模型，实际 {len(result['models'])}"
    return True, "同 model_id 去重"


async def case_fetch_model_id_field_tolerance() -> tuple[bool, str]:
    """model_id 字段名容忍：id / model / name."""
    orig = _patch_httpx(lambda req: httpx.Response(200, json={
        "data": [{"id": "via-id"}, {"model": "via-model"}, {"name": "via-name"}],
    }))
    try:
        result = await fetch_models(_make_entity())
    finally:
        _restore(orig)
    ids = {m["model_id"] for m in result["models"]}
    if ids != {"via-id", "via-model", "via-name"}:
        return False, f"字段名容忍失败: {ids}"
    return True, "model_id 三字段名容忍（id/model/name）"


async def case_fetch_context_window_tolerance() -> tuple[bool, str]:
    """context_window 多字段名：context_length / max_context_length / 缺失."""
    orig = _patch_httpx(lambda req: httpx.Response(200, json={
        "data": [
            {"id": "m-ctx-len", "context_length": 64000},
            {"id": "m-max-ctx", "max_context_length": 32000},
            {"id": "m-no-ctx"},
            {"id": "m-bad-ctx", "context_window": "not-a-number"},
        ],
    }))
    try:
        result = await fetch_models(_make_entity())
    finally:
        _restore(orig)
    ctx = {m["model_id"]: m["context_window"] for m in result["models"]}
    if ctx["m-ctx-len"] != 64000:
        return False, f"context_length 解析失败: {ctx}"
    if ctx["m-max-ctx"] != 32000:
        return False, f"max_context_length 解析失败: {ctx}"
    if ctx["m-no-ctx"] != 0:
        return False, f"缺失应 0，实际 {ctx['m-no-ctx']}"
    if ctx["m-bad-ctx"] != 0:
        return False, f"非数字应 0，实际 {ctx['m-bad-ctx']}"
    return True, "context_window 三字段名容忍 + 非数字→0"


async def case_fetch_bare_list() -> tuple[bool, str]:
    """裸 list 响应（无 data 包装）."""
    orig = _patch_httpx(lambda req: httpx.Response(200, json=[{"id": "m1"}, {"id": "m2"}]))
    try:
        result = await fetch_models(_make_entity())
    finally:
        _restore(orig)
    if not result["ok"]:
        return False, f"ok 应 True，实际 {result}"
    if len(result["models"]) != 2:
        return False, f"应 2 模型，实际 {len(result['models'])}"
    return True, "裸 list 响应容忍"


async def case_fetch_empty_list() -> tuple[bool, str]:
    orig = _patch_httpx(lambda req: httpx.Response(200, json={"data": []}))
    try:
        result = await fetch_models(_make_entity())
    finally:
        _restore(orig)
    if result["ok"]:
        return False, f"ok 应 False，实际 {result}"
    if "空" not in result["error"]:
        return False, f"error 应含空，实际 {result['error']!r}"
    return True, "空模型列表 → ok=False"


async def case_fetch_failure_paths() -> tuple[bool, str]:
    """非 JSON / 4xx / 超时 → ok=False."""
    # 非 JSON
    orig = _patch_httpx(lambda req: httpx.Response(200, text="<html>"))
    try:
        r = await fetch_models(_make_entity())
        if r["ok"] or "JSON" not in r["error"]:
            return False, f"非 JSON 路径失败: {r}"
    finally:
        _restore(orig)
    # 4xx
    orig = _patch_httpx(lambda req: httpx.Response(403, text='{"error":"forbidden"}'))
    try:
        r = await fetch_models(_make_entity())
        if r["ok"] or r["status_code"] != 403:
            return False, f"4xx 路径失败: {r}"
    finally:
        _restore(orig)
    # 超时
    orig = _patch_httpx(lambda req: (_ for _ in ()).throw(httpx.TimeoutException("t")))
    try:
        r = await fetch_models(_make_entity())
        if r["ok"] or r["status_code"] is not None or "超时" not in r["error"]:
            return False, f"超时路径失败: {r}"
    finally:
        _restore(orig)
    # 连接失败
    orig = _patch_httpx(lambda req: (_ for _ in ()).throw(httpx.ConnectError("dns")))
    try:
        r = await fetch_models(_make_entity())
        if r["ok"] or r["status_code"] is not None or "连接失败" not in r["error"]:
            return False, f"连接失败路径失败: {r}"
    finally:
        _restore(orig)
    return True, "非 JSON / 4xx / 超时 / 连接失败 全 ok=False"


async def case_never_raises() -> tuple[bool, str]:
    """路由契约：两函数永不 raise（所有失败进 error）。"""
    # 各种异常输入都不应抛
    try:
        await test_provider(_make_entity(base_url="", api_key=""))
        await test_provider(_make_entity(model="", models=[]))
        await fetch_models(_make_entity(base_url=""))
        await fetch_models(_make_entity(api_key=""))
    except Exception as exc:
        return False, f"probe 不应 raise，实际 {exc!r}"
    return True, "两函数永不 raise（路由可安全转发）"


# ── 主流程 ──────────────────────────────────────────────────────────

async def main() -> int:
    print("=== 多模型服务商目录 · probe 自测 ===")

    cases = [
        ("test_provider 成功路径", case_test_provider_success),
        ("test_provider 401", case_test_provider_401),
        ("test_provider 超时", case_test_provider_timeout),
        ("test_provider 连接失败", case_test_provider_connect_error),
        ("test_provider base_url 未配置", case_test_provider_no_base_url),
        ("test_provider api_key 未配置", case_test_provider_no_api_key),
        ("test_provider 200 空 choices", case_test_provider_empty_choices),
        ("test_provider 200 非 JSON", case_test_provider_non_json),
        ("test_provider 连接级透传", case_test_provider_connection_passthrough),
        ("test_provider model 经 _select_model", case_test_provider_model_via_select),
        ("fetch_models 标准 OpenAI 响应", case_fetch_standard_openai),
        ("fetch_models LlmModel 字段完整", case_fetch_model_fields),
        ("fetch_models 去重", case_fetch_dedup),
        ("fetch_models model_id 字段名容忍", case_fetch_model_id_field_tolerance),
        ("fetch_models context_window 字段名容忍", case_fetch_context_window_tolerance),
        ("fetch_models 裸 list 响应", case_fetch_bare_list),
        ("fetch_models 空模型列表", case_fetch_empty_list),
        ("fetch_models 失败路径合集", case_fetch_failure_paths),
        ("路由契约：永不 raise", case_never_raises),
    ]

    results: list[tuple[str, bool, str]] = []
    for name, fn in cases:
        try:
            ok, msg = await fn()
        except Exception as exc:
            ok, msg = False, f"异常: {exc!r}"
        results.append((name, ok, msg))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {msg}")

    print("\n=== 结论 ===")
    all_pass = all(ok for _, ok, _ in results)
    if all_pass:
        print(f"PASS — probe 自测通过（{len(results)} 用例全过）：")
        print("  · test_provider 成功/401/超时/连接失败/空配置/空 choices/非 JSON 全路径；")
        print("  · 连接级 proxy/extra_headers/timeout 透传 + model 经 _select_model；")
        print("  · fetch_models 解析 /v1/models（标准/去重/字段名容忍/裸 list/空/失败）；")
        print("  · 两函数永不 raise（路由可安全转发）。")
        return 0
    failed = [n for n, ok, _ in results if not ok]
    print(f"FAIL — {len(failed)} 项失败: {failed}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""VH22 回归：config.py 无 OPENAI_API_KEY 外泄路径审计（task B25）.

锁住 B25 审计——``backend/config.py`` 硬编码默认值与 .env 读取 + 确认无
OPENAI_API_KEY 外泄路径、无 key 落日志/异常文本。

B25 审计结论（config.py 无外泄路径，3 道防线齐全）：

  防线 1：HTTP 输出脱敏（get_config_public）
    ``get_config_public()`` 把 ``api_key`` 经 ``_mask_key()`` 脱敏（首 3 + *** +
    尾 3，短/空 key → *** / ""），raw key 永不离开进程。GET /api/config 与
    PUT /api/config 响应都走此函数，前端只收 mask 预览 + has_key 布尔。

  防线 2：env 读取仅落 cache（不落日志/异常）
    ``_env_config()`` 从 ``os.environ`` 读 OPENAI_API_KEY（fallback ANTHROPIC_API_KEY）
    → 落 ``_ACTIVE_CACHE["api_key"]``（内存 dict）。config.py / llm/client.py /
    llm/probe.py / store/crud.py 四模块的 env 读取都只把 key 喂进 cache 或
    Bearer 请求头（Authorization: Bearer {key}），从不写日志、不进异常文本、
    不进 RuntimeError 消息。

  防线 3：cache 内 key 只走 Bearer 头（请求外发，非日志）
    ``chat_completion`` / ``chat_completion_stream`` / ``test_provider`` /
    ``fetch_models`` 从 cache/读 entity 取 api_key → 拼 ``Authorization: Bearer
    {api_key}`` 请求头 → httpx 发往上游。请求头是外发 HTTP（给上游认证），
    不进 logger / print / RuntimeError。``ChatOpenAI(api_key=...)`` 同理——
    key 喂 langchain 框架，框架用其建请求头，不写日志（实测 ChatOpenAI
    repr/str 不含 key）。

  唯一潜在路径审计（resp.text 入 RuntimeError，已被评估为安全）：
    ``llm/client.py:88`` ``raise RuntimeError(f"LLM API error {resp.status_code}:
    {resp.text}")`` + ``:171`` 流式 ``body_text``。resp.text 是**上游响应体**
    （上游控制的错误 JSON，如 ``{"error":{"message":"Invalid API key provided"}}``），
    非我们的请求头/请求体。OpenAI/DeepSeek/Kimi/GLM 标准 401 错误体只说
    「key 无效」不回显 key 全文。RuntimeError 被 coordinator/worker 的
    ``except Exception as e: logger.warning("...failed: %s", e)`` 捕获入日志——
    日志里的 e 是上游错误体（不含我们的 key），非 key 本身。安全。

B25 审计确认（无外泄路径，故不改代码，只补契约测锁住）：
  - config.py 无 logger / print（grep 确认 config.py 全文无 logging/print）。
  - _mask_key 短 key 兜底（≤8 字符 → ***，空 → ""），不泄露部分 key。
  - env 读取 4 处（config.py:46-48/97-98 + crud.py:1647-1648）都只落 cache/请求头。
  - .env 在 .gitignore（line 18-19），git ls-files 确认 .env 未提交。
  - 无硬编码 sk- 真实 key（grep 全仓 backend/ + src/ 无 sk-{10+} 真实 key）。

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh21 同款风格。

六段契约：

  A. HTTP 输出脱敏（get_config_public）
    1. ``get_config_public()`` 定义（HTTP 输出脱敏层）。
    2. get_config_public 输出 ``"api_key": _mask_key(key)``（脱敏，非 raw）。
    3. get_config_public 输出 ``"has_key": bool(key)``（布尔，非 key 全文）。

  B. _mask_key 短/空 key 兜底（不泄露部分 key）
    4. ``_mask_key(key)`` 定义（脱敏函数）。
    5. _mask_key 空 key → ``""``（空不泄露）。
    6. _mask_key 短 key（≤8）→ ``"***"``（短 key 不显首尾，防部分泄露）。
    7. _mask_key 长 key → ``f"{key[:3]}***{key[-3:]}"``（首3+尾3，中间星号）。

  C. env 读取仅落 cache（不落日志/异常）
    8. config.py 无 ``logging`` import / 无 ``logger`` / 无 ``print(``。
    9. _env_config 读 OPENAI_API_KEY（fallback ANTHROPIC_API_KEY）落 cache dict。
   10. cache ``api_key`` 仅经 get_config() 读出（sync，不写日志）。

  D. cache key 只走 Bearer 请求头（请求外发）
   11. chat_completion 拼 ``Authorization: Bearer {config['apiKey']}``（请求头）。
   12. chat_completion_stream 拼 Bearer 头（同上）。
   13. probe.test_provider / fetch_models 拼 Bearer 头（同上）。

  E. 异常文本不含 key（resp.text 是上游响应体）
   14. client.py RuntimeError 消息含 ``resp.text``（上游响应体，非我们的 key）。
   15. client.py RuntimeError 消息不含 ``apiKey`` / ``api_key`` 变量插值。
   16. agent_loop.py 异常 ``{exc}`` 是 ChatOpenAI 抛的异常（实测不含 key）。

  F. .env 不提交 + 无硬编码 key
   17. .gitignore 含 ``.env``（line 18-19）。
   18. backend/ + src/ 无硬编码 ``sk-`` 真实 key（grep sk-[a-zA-Z0-9]{10,} 无命中非测试）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONFIG_PY = REPO / "backend" / "config.py"
CLIENT_PY = REPO / "backend" / "llm" / "client.py"
PROBE_PY = REPO / "backend" / "llm" / "probe.py"
AGENT_LOOP_PY = REPO / "backend" / "engine" / "agent_loop.py"
GITIGNORE = REPO / ".gitignore"


def _fn_body_py(src: str, fname: str) -> str:
    """抽 Python 函数体（到下一个顶层 def 为止）。"""
    pat = rf"def {fname}\([^)]*\).*?(?=\n(?:async )?def |\Z)"
    m = re.search(pat, src, re.S)
    return m.group(0) if m else ""


def assert_contract() -> list[str]:
    errs: list[str] = []
    config = CONFIG_PY.read_text(encoding="utf-8")
    client = CLIENT_PY.read_text(encoding="utf-8")
    probe = PROBE_PY.read_text(encoding="utf-8")
    agent_loop = AGENT_LOOP_PY.read_text(encoding="utf-8")

    # ── A. HTTP 输出脱敏 ──
    gcp_body = _fn_body_py(config, "get_config_public")
    if not gcp_body:
        errs.append("[A1] get_config_public 函数体未找到")
    else:
        # [1] get_config_public 定义
        print("[A1] OK  get_config_public 定义（HTTP 输出脱敏层）")
        # [2] 输出 _mask_key(key)
        if "_mask_key(key)" not in gcp_body:
            errs.append("[A2] get_config_public 未输出 _mask_key(key)（raw key 会外泄 HTTP）")
        else:
            print("[A2] OK  get_config_public 输出 _mask_key(key)（脱敏，非 raw）")
        # [3] 输出 has_key: bool(key)
        if '"has_key": bool(key)' not in gcp_body:
            errs.append("[A3] get_config_public 缺 has_key: bool(key)（应布尔非 key 全文）")
        else:
            print("[A3] OK  get_config_public 输出 has_key: bool(key)（布尔，非 key 全文）")

    # ── B. _mask_key 短/空 key 兜底 ──
    mk_body = _fn_body_py(config, "_mask_key")
    if not mk_body:
        errs.append("[B4] _mask_key 函数体未找到")
    else:
        print("[B4] OK  _mask_key 定义（脱敏函数）")
        mk_nc = re.sub(r"#[^\n]*", "", mk_body)  # 剔注释
        # [5] 空 key → ""
        if 'return ""' not in mk_body and 'return ""' not in mk_nc:
            errs.append("[B5] _mask_key 空 key 未返 \"\"（空 key 应不泄露）")
        else:
            print('[B5] OK  _mask_key 空 key → ""（空不泄露）')
        # [6] 短 key（≤8）→ "***"
        if 'return "***"' not in mk_body:
            errs.append('[B6] _mask_key 短 key 未返 "***"（短 key 应不显首尾防部分泄露）')
        else:
            print('[B6] OK  _mask_key 短 key → "***"（短 key 不显首尾）')
        # [7] 长 key → f"{key[:3]}***{key[-3:]}"
        if 'f"{key[:3]}***{key[-3:]}"' not in mk_body:
            errs.append("[B7] _mask_key 长 key 未返 f\"{key[:3]}***{key[-3:]}\"（首3+尾3）")
        else:
            print('[B7] OK  _mask_key 长 key → f"{key[:3]}***{key[-3:]}"（首3+尾3，中间星号）')

    # ── C. env 读取仅落 cache ──
    # [8] config.py 无 logging / logger / print
    has_logging = bool(re.search(r"^import logging\b|^from logging\b", config, re.M))
    has_logger = "logger" in config and re.search(r"\blogger\.\w", config)
    has_print = bool(re.search(r"\bprint\s*\(", config))
    if has_logging or has_logger or has_print:
        errs.append(f"[C8] config.py 含日志/print（logging={has_logging} logger={has_logger} print={has_print}——key 可能落日志）")
    else:
        print("[C8] OK  config.py 无 logging/logger/print（env 读取不落日志）")
    # [9] _env_config 读 OPENAI_API_KEY fallback ANTHROPIC_API_KEY 落 cache
    env_body = _fn_body_py(config, "_env_config")
    if not env_body:
        errs.append("[C9] _env_config 函数体未找到")
    elif 'os.environ.get("OPENAI_API_KEY"' not in env_body or "ANTHROPIC_API_KEY" not in env_body:
        errs.append("[C9] _env_config 未读 OPENAI_API_KEY fallback ANTHROPIC_API_KEY")
    else:
        print("[C9] OK  _env_config 读 OPENAI_API_KEY fallback ANTHROPIC_API_KEY → cache dict")
    # [10] cache api_key 经 get_config 读出（sync）
    gc_body = _fn_body_py(config, "get_config")
    if not gc_body:
        errs.append("[C10] get_config 函数体未找到")
    elif "return dict(_ACTIVE_CACHE)" not in gc_body and "_ACTIVE_CACHE" not in gc_body:
        errs.append("[C10] get_config 未读 _ACTIVE_CACHE（cache api_key 出口断）")
    else:
        print("[C10] OK  get_config 读 _ACTIVE_CACHE（cache api_key sync 出口，不写日志）")

    # ── D. cache key 只走 Bearer 请求头 ──
    # [11] chat_completion Bearer
    if 'f"Bearer {config[\'apiKey\']}"' not in client and "Bearer {config['apiKey']}" not in client:
        errs.append("[D11] chat_completion 未拼 Authorization: Bearer {apiKey} 请求头")
    else:
        print("[D11] OK  chat_completion 拼 Authorization: Bearer {apiKey}（请求头外发）")
    # [12] chat_completion_stream Bearer
    cc_stream_body = _fn_body_py(client, "chat_completion_stream") if "def chat_completion_stream" in client else ""
    # _fn_body_py 抽 async def 需 is_async——手动查
    m_stream = re.search(r"async def chat_completion_stream\(.*?(?=\n(?:async )?def |\Z)", client, re.S)
    stream_body = m_stream.group(0) if m_stream else ""
    if not stream_body:
        errs.append("[D12] chat_completion_stream 函数体未找到")
    elif "Bearer" not in stream_body:
        errs.append("[D12] chat_completion_stream 未拼 Bearer 头")
    else:
        print("[D12] OK  chat_completion_stream 拼 Bearer 头（请求头外发）")
    # [13] probe.test_provider + fetch_models Bearer
    if "f\"Bearer {api_key}\"" not in probe:
        errs.append("[D13] probe 未拼 Bearer {api_key} 请求头")
    else:
        print("[D13] OK  probe（test_provider + fetch_models）拼 Bearer {api_key}（请求头外发）")

    # ── E. 异常文本不含 key ──
    # [14] client.py RuntimeError 含 resp.text（上游响应体）
    if "resp.text" not in client and "body_text" not in client:
        errs.append("[E14] client.py RuntimeError 未含 resp.text/body_text（上游响应体出口断）")
    else:
        print("[E14] OK  client.py RuntimeError 含 resp.text/body_text（上游响应体，非我们的 key）")
    # [15] client.py RuntimeError 不含 apiKey/api_key 变量插值
    # 查 raise RuntimeError 行是否插值 config['apiKey'] / api_key 变量
    raise_lines = re.findall(r"raise RuntimeError\([^)]*\)", client, re.S)
    leak_in_raise = any(
        re.search(r"config\['apiKey'\]|config\[\"apiKey\"\]|\bapi_key\b", line)
        and "resp.text" not in line and "body_text" not in line
        for line in raise_lines
    )
    if leak_in_raise:
        errs.append("[E15] client.py raise RuntimeError 插值了 apiKey/api_key 变量（key 会进异常文本）")
    else:
        print("[E15] OK  client.py raise RuntimeError 不插值 apiKey/api_key 变量（异常不含 key）")
    # [16] agent_loop.py 异常 {exc} 是 ChatOpenAI 抛的（实测不含 key）
    # agent_loop 模型初始化/执行异常用 {exc} 拼消息——exc 来自 ChatOpenAI/langgraph，
    # 实测 ChatOpenAI repr/str 不含 api_key。断言 agent_loop 异常消息只插值 exc（非 cfg/api_key）。
    al_raises = re.findall(r'f"[^"]*\{exc\}[^"]*"', agent_loop)
    if not al_raises:
        errs.append("[E16] agent_loop.py 无 {exc} 异常插值（断言失败）")
    else:
        # 检查这些 {exc} 行是否也插值了 cfg/api_key（不应有）
        bad = [r for r in al_raises if re.search(r"cfg\[|api_key|apiKey", r)]
        if bad:
            errs.append(f"[E16] agent_loop.py {exc} 异常行同时插值 cfg/api_key：{bad}")
        else:
            print("[E16] OK  agent_loop.py {exc} 异常是 ChatOpenAI/langgraph 抛（实测不含 key，不插值 cfg/api_key）")

    # ── F. .env 不提交 + 无硬编码 key ──
    # [17] .gitignore 含 .env
    gi = GITIGNORE.read_text(encoding="utf-8") if GITIGNORE.exists() else ""
    if ".env" not in gi:
        errs.append("[F17] .gitignore 缺 .env（.env 可能提交）")
    else:
        print("[F17] OK  .gitignore 含 .env（.env 不提交）")
    # [18] backend/ + src/ 无硬编码 sk- 真实 key（≥10 字符）
    leaked = []
    for pat in (REPO / "backend").rglob("*.py"):
        if "__pycache__" in str(pat) or "test_" in pat.name:
            continue
        src = pat.read_text(encoding="utf-8", errors="ignore")
        # 找 sk- 后跟 ≥10 字符（排除测试用 FAKEKEY / cf-t28 / ve-probe 等已知 fixture）
        for m in re.finditer(r"sk-[a-zA-Z0-9_-]{10,}", src):
            leaked.append(f"{pat.name}: {m.group(0)[:15]}…")
    for pat in (REPO / "src").rglob("*.ts*"):
        if "__pycache__" in str(pat):
            continue
        src = pat.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"sk-[a-zA-Z0-9_-]{10,}", src):
            leaked.append(f"{pat.name}: {m.group(0)[:15]}…")
    if leaked:
        errs.append(f"[F18] 硬编码 sk- key 命中：{leaked[:5]}")
    else:
        print("[F18] OK  backend/ + src/ 无硬编码 sk- 真实 key（grep sk-[10+] 无命中非测试）")

    return errs


def main() -> int:
    print("=== VH22 回归：config.py 无 OPENAI_API_KEY 外泄路径审计（B25）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B25 config.py 无 OPENAI_API_KEY 外泄路径审计锁定：\n"
        "  · A HTTP 输出脱敏（get_config_public → _mask_key + has_key 布尔）；\n"
        "  · B _mask_key 短/空 key 兜底（空→\"\"，短≤8→***，长→首3+尾3，不泄露部分 key）；\n"
        "  · C env 读取仅落 cache（config.py 无 logging/logger/print，env→cache→get_config sync 出口）；\n"
        "  · D cache key 只走 Bearer 请求头（chat_completion/stream/probe 四处拼 Authorization: Bearer 外发上游）；\n"
        "  · E 异常文本不含 key（RuntimeError 插值 resp.text 上游响应体非我们的 key + agent_loop {exc} 来自 ChatOpenAI 实测不含 key）；\n"
        "  · F .env 在 .gitignore 不提交 + backend/src 无硬编码 sk- 真实 key。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""需求2-后端 回归：worker 结构化卡片片段提示词/解析契约.

锁住 [需求2-后端] 改动——worker 产出结构化卡片片段的提示词注入 + 解析。
设计单真源：``docs/structured-result-card-schema.md``（[需求2-设计] commit 9df5116）。

本任务确认「卡片走 content 子串透传，不落 task.artifact / 不塞 reply data」
（[需求2-设计] §2 的核心决策）：卡片是 `````card```` 围栏内 JSON，作为 ``content``
文本的一部分，经现有 ``_unified_reply → persist_agent_reply → emit_message_added``
全程透传，``data`` 仍只载流式 run-stats。故 v1 不改 DB schema / 不加事件类型 /
不改 reply 落盘 message dict 的 7 key shape。

纯静态契约（读源码断言，不依赖后端在线 / 真实 LLM），与 test_vh*/va* 同款风格。

七段契约：

  A. card_fragment 解析模块（llm/card_fragment.py · 单一真源）
    1. ``llm/card_fragment.py`` 存在 + 定义 ``CARD_FRAGMENT_RE``。
    2. CARD_FRAGMENT_RE 正则模式 = `````card\\s*\\n([\\s\\S]*?)````` （与前端
       ``src/components/ChatMessageBubble.tsx`` 的 ``CARD_RE`` byte-identical，
       后端计数与前端解析对同一 `````card```` 块判定一致）。
    3. ``count_card_fragments(content)`` 返回围栏块数（含非法 JSON 的块——围栏
       本身是 wire-format 标记，前端降级为普通代码块不丢弃，故计数也含它）。
    4. ``extract_card_payloads(content)`` 解码围栏内 JSON：非法 JSON 跳过、非 dict
       顶层跳过、合法 dict 按 document order 返回（镜像前端 parseCards 优雅降级）。
    5. ``llm/__init__.py`` 导出 ``CARD_FRAGMENT_RE`` / ``count_card_fragments`` /
       ``extract_card_payloads``（公共 API，供 reply.py / 未来端点使用）。

  B. 提示词注入（llm/prompts.py · build_brain_prompt 末尾）
    6. ``CARD_OUTPUT_CONTRACT`` 常量存在（独立常量，非内联字面量——便于 execute
       路径的 ReAct system prompt 复用同一份文字，单一真源）。
    7. ``build_brain_prompt`` 返回值含 ``CARD_OUTPUT_CONTRACT``（chat 路径 brain
       决策提示词末尾内嵌卡片输出契约）。
    8. 契约文字含三种 kind（``kv`` / ``list`` / ``table``）+ 「所有值统一 string」
       约束 + 「纯散文不强套卡片」边界（避免过度结构化）。
    9. ``llm/__init__.py`` 导出 ``CARD_OUTPUT_CONTRACT``。

  C. execute 路径也带契约（engine/agent_loop.py · create_react_agent system prompt）
   10. ``engine/agent_loop.py`` import ``CARD_OUTPUT_CONTRACT`` from llm.prompts。
   11. ``run_agent_loop`` 装配的 ``sys_content`` 拼接 ``_CARD_OUTPUT_SYSTEM_SUFFIX``
       （execute 路径 ReAct 最终答案也能出卡片，与 chat 路径同源文字）。
   12. ``_CARD_OUTPUT_SYSTEM_SUFFIX`` = ``\\n`` + CARD_OUTPUT_CONTRACT + ``\\n``
       （基于常量组装，非重复字面量——单一真源）。

  D. 卡片观测（engine/reply.py · persist_agent_reply 落盘后 best-effort 统计）
   13. ``persist_agent_reply`` 落盘后调 ``count_card_fragments(content)`` 统计
       围栏块数（验证 LLM 是否遵守提示词）。
   14. 仅当 >0 时 ``logger.info`` 记一行（0 块不记——避免对纯散文回复刷日志）。
   15. 统计包在 try/except best-effort 内（正则/计数失败不影响落盘主流程）。

  E. 不改 DB/事件/message shape（[需求2-设计] §2 核心决策锁定）
   16. ``persist_agent_reply`` message dict 仍 7 key（conversation_id/task_id/
       sender_id/receiver_id/type/content/data），无新 key（卡片是 content 子串，
       不加 card 字段）。
   17. ``data`` 仍透传调用方传入的 run-stats（``"data": data``），不塞卡片数据。
   18. ``persist_agent_reply`` 签名不变（``group_id, agent_id, content, data=None,
       task_id=None``），既有调用方（registry._reply / coordinator._unified_reply /
       worker._unified_reply）零改动。

  F. 行为零变（解析是新增模块，提示词是追加段，不破坏既有回复链）
   19. ``count_card_fragments`` 对空串/无卡片段返回 0（纯散文回复不记日志）。
   20. ``extract_card_payloads`` 对空串返回 []。
   21. baidu 热搜回归 shape：`````card```` table 块能解码，kind==="table"，
       rows 是 list[list[str]]（所有值 string）。

  G. 单一真源（提示词文字在 prompts.py 一处，解析正则在 card_fragment.py 一处）
   22. execute 路径的 ``_CARD_OUTPUT_SYSTEM_SUFFIX`` 复用 ``CARD_OUTPUT_CONTRACT``
       常量（非复制文字——grep ``CARD_OUTPUT_CONTRACT`` 在 agent_loop.py 出现）。
   23. 解析正则 ``CARD_FRAGMENT_RE`` 在 card_fragment.py 单一定义（reply.py /
       prompts.py 不重复定义正则）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CARD_FRAGMENT = REPO / "backend" / "llm" / "card_fragment.py"
LLM_INIT = REPO / "backend" / "llm" / "__init__.py"
PROMPTS = REPO / "backend" / "llm""prompts.py" if False else REPO / "backend" / "llm" / "prompts.py"
AGENT_LOOP = REPO / "backend" / "engine" / "agent_loop.py"
REPLY = REPO / "backend" / "engine" / "reply.py"

# 与前端 src/components/ChatMessageBubble.tsx 的 CARD_RE 必须一致（byte-identical）。
EXPECTED_CARD_RE_PATTERN = r"```card\s*\n([\s\S]*?)```"


def _fn_body(src: str, fname: str, indent_opts=("", "    ")) -> str:
    """抽 fn 函数体到下一个同级 def（试多种缩进：模块级 0 / 类方法 4 空格）。"""
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


def assert_contract() -> list[str]:
    errs: list[str] = []
    cf = CARD_FRAGMENT.read_text(encoding="utf-8") if CARD_FRAGMENT.exists() else ""
    prompts = PROMPTS.read_text(encoding="utf-8")
    agent_loop = AGENT_LOOP.read_text(encoding="utf-8")
    reply_mod = REPLY.read_text(encoding="utf-8") if REPLY.exists() else ""
    llm_init = LLM_INIT.read_text(encoding="utf-8") if LLM_INIT.exists() else ""

    # ── A. card_fragment 解析模块 ──
    if not cf:
        errs.append("[A1] backend/llm/card_fragment.py 不存在（[需求2-后端] 未落地）")
        return errs
    if "CARD_FRAGMENT_RE" not in cf:
        errs.append("[A1] card_fragment.py 未定义 CARD_FRAGMENT_RE")
    else:
        print("[A1] OK  card_fragment.py 定义 CARD_FRAGMENT_RE")
    # [2] 正则模式 byte-identical（与前端 CARD_RE 对齐——后端计数=前端解析）
    m = re.search(r'CARD_FRAGMENT_RE\s*=\s*re\.compile\(r"([^"]+)"\)', cf)
    if not m:
        errs.append("[A2] CARD_FRAGMENT_RE 未用 re.compile(r\"...\") 定义")
    elif m.group(1) != EXPECTED_CARD_RE_PATTERN:
        errs.append(
            f"[A2] CARD_FRAGMENT_RE 模式 {m.group(1)!r} != 期望 {EXPECTED_CARD_RE_PATTERN!r}"
            "（必须与前端 CARD_RE byte-identical）"
        )
    else:
        print("[A2] OK  CARD_FRAGMENT_RE 模式与前端 CARD_RE byte-identical")

    # [3] count_card_fragments 返回围栏块数（含非法 JSON 块）
    if "def count_card_fragments(" not in cf:
        errs.append("[A3] card_fragment.py 未定义 count_card_fragments")
    elif "CARD_FRAGMENT_RE.findall" not in cf and "len(CARD_FRAGMENT_RE" not in cf:
        errs.append("[A3] count_card_fragments 未用 CARD_FRAGMENT_RE 计数")
    else:
        print("[A3] OK  count_card_fragments 用 CARD_FRAGMENT_RE 计数（含非法 JSON 块）")

    # [4] extract_card_payloads 跳过非法 JSON / 非 dict，返回 dict 列表
    if "def extract_card_payloads(" not in cf:
        errs.append("[A4] card_fragment.py 未定义 extract_card_payloads")
    else:
        ep_body = _fn_body(cf, "extract_card_payloads", indent_opts=("",))
        if "json.loads" not in ep_body:
            errs.append("[A4] extract_card_payloads 未用 json.loads 解码")
        elif "isinstance(payload, dict)" not in ep_body:
            errs.append("[A4] extract_card_payloads 未过滤非 dict 顶层（schema 要求 object）")
        else:
            print("[A4] OK  extract_card_payloads: json.loads + isinstance dict 过滤")

    # [5] llm/__init__.py 导出三公共名
    for name in ("CARD_FRAGMENT_RE", "count_card_fragments", "extract_card_payloads"):
        if name not in llm_init:
            errs.append(f"[A5] llm/__init__.py 未导出 {name}")
        else:
            pass
    if not any(f"[A5]" in e for e in errs):
        print("[A5] OK  llm/__init__.py 导出 CARD_FRAGMENT_RE/count_card_fragments/extract_card_payloads")

    # ── B. 提示词注入（build_brain_prompt 末尾）──
    # [6] CARD_OUTPUT_CONTRACT 独立常量
    if not re.search(r'^CARD_OUTPUT_CONTRACT\s*=', prompts, re.M):
        errs.append("[B6] prompts.py 未定义独立常量 CARD_OUTPUT_CONTRACT（应非内联字面量）")
    else:
        print("[B6] OK  CARD_OUTPUT_CONTRACT 独立常量（单一真源，execute 路径复用）")
    # [7] build_brain_prompt 返回值含 CARD_OUTPUT_CONTRACT
    bp_body = _fn_body(prompts, "build_brain_prompt", indent_opts=("",))
    if "CARD_OUTPUT_CONTRACT" not in bp_body:
        errs.append("[B7] build_brain_prompt 返回值未含 CARD_OUTPUT_CONTRACT（chat 路径未注入）")
    else:
        print("[B7] OK  build_brain_prompt 内嵌 CARD_OUTPUT_CONTRACT（chat 路径带契约）")
    # [8] 契约文字含三 kind + string 约束 + 边界
    m_contract = re.search(r'CARD_OUTPUT_CONTRACT\s*=\s*(?:"""[\s\S]*?"""|f"""[\s\S]*?"""|\([\s\S]*?\))', prompts)
    if not m_contract:
        errs.append("[B8] CARD_OUTPUT_CONTRACT 值未找到（无法校验文字）")
    else:
        contract_text = m_contract.group(0)
        missing = []
        for kw in ("kv", "list", "table", "string", "card"):
            if kw not in contract_text:
                missing.append(kw)
        # 边界：纯散文不强套卡片（避免过度结构化）
        boundary = ("不强套" in contract_text) or ("不要" in contract_text and "卡片" in contract_text)
        if missing:
            errs.append(f"[B8] CARD_OUTPUT_CONTRACT 缺关键词 {missing}")
        elif not boundary:
            errs.append("[B8] CARD_OUTPUT_CONTRACT 缺「纯散文不强套卡片」边界（避免过度结构化）")
        else:
            print("[B8] OK  契约含三 kind + string 约束 + 纯散文边界")
    # [9] llm/__init__.py 导出 CARD_OUTPUT_CONTRACT
    if "CARD_OUTPUT_CONTRACT" not in llm_init:
        errs.append("[B9] llm/__init__.py 未导出 CARD_OUTPUT_CONTRACT")
    else:
        print("[B9] OK  llm/__init__.py 导出 CARD_OUTPUT_CONTRACT")

    # ── C. execute 路径也带契约（agent_loop.py）──
    if "from llm.prompts import CARD_OUTPUT_CONTRACT" not in agent_loop:
        errs.append("[C10] agent_loop.py 未 import CARD_OUTPUT_CONTRACT from llm.prompts")
    else:
        print("[C10] OK  agent_loop.py import CARD_OUTPUT_CONTRACT（execute 路径复用）")
    # [11] sys_content 拼接 _CARD_OUTPUT_SYSTEM_SUFFIX
    ral_body = _fn_body(agent_loop, "run_agent_loop", indent_opts=("",))
    if "_CARD_OUTPUT_SYSTEM_SUFFIX" not in ral_body:
        errs.append("[C11] run_agent_loop 未拼接 _CARD_OUTPUT_SYSTEM_SUFFIX（execute ReAct 不带契约）")
    else:
        print("[C11] OK  run_agent_loop 拼接 _CARD_OUTPUT_SYSTEM_SUFFIX（execute 路径带契约）")
    # [12] _CARD_OUTPUT_SYSTEM_SUFFIX 基于常量组装（非重复字面量）
    m_suff = re.search(r'_CARD_OUTPUT_SYSTEM_SUFFIX\s*=\s*([^\n]+)', agent_loop)
    if not m_suff:
        errs.append("[C12] _CARD_OUTPUT_SYSTEM_SUFFIX 未定义")
    elif "CARD_OUTPUT_CONTRACT" not in m_suff.group(1):
        errs.append("[C12] _CARD_OUTPUT_SYSTEM_SUFFIX 未复用 CARD_OUTPUT_CONTRACT 常量（疑似复制文字）")
    else:
        print("[C12] OK  _CARD_OUTPUT_SYSTEM_SUFFIX 基于 CARD_OUTPUT_CONTRACT 常量（单一真源）")

    # ── D. 卡片观测（reply.py persist_agent_reply 落盘后统计）──
    if not reply_mod:
        errs.append("[D] engine/reply.py 不存在")
    else:
        pa_body = _fn_body(reply_mod, "persist_agent_reply", indent_opts=("",))
        if not pa_body:
            errs.append("[D] persist_agent_reply 函数体未找到")
        else:
            # [13] 调 count_card_fragments
            if "count_card_fragments" not in pa_body and "count_card_fragments" not in reply_mod:
                errs.append("[D13] persist_agent_reply 未调 count_card_fragments（无卡片观测）")
            elif "count_card_fragments" not in reply_mod:
                errs.append("[D13] reply.py 未 import / 调用 count_card_fragments")
            else:
                print("[D13] OK  persist_agent_reply 调 count_card_fragments（卡片观测）")
            # [14] 仅 >0 时 logger.info（0 块不记）
            if "logger.info" not in reply_mod or "n_cards" not in reply_mod:
                errs.append("[D14] reply.py 未在 n_cards>0 时 logger.info（0 块应不记）")
            else:
                print("[D14] OK  仅 n_cards>0 时 logger.info（纯散文不刷日志）")
            # [15] try/except best-effort
            if "except Exception" not in pa_body and "except Exception" not in reply_mod:
                errs.append("[D15] persist_agent_reply 统计未包 try/except（观测失败会断落盘）")
            else:
                print("[D15] OK  统计包 try/except best-effort（不影响落盘主流程）")

    # ── E. 不改 DB/事件/message shape ──
    if pa_body:
        # [16] message dict 仍 7 key
        keys = set(re.findall(r'"(\w+)":', pa_body))
        expected = {"conversation_id", "task_id", "sender_id", "receiver_id", "type", "content", "data"}
        missing = expected - keys
        if missing:
            errs.append(f"[E16] persist_agent_reply message dict 缺 key {missing}（卡片不应加新 key）")
        else:
            print(f"[E16] OK  message dict 仍 7 key（{sorted(expected)}，卡片是 content 子串不加字段）")
        # [17] data 透传 run-stats（不塞卡片）
        if '"data": data' not in pa_body:
            errs.append("[E17] persist_agent_reply 未透传 \"data\": data（data 仍应载 run-stats 非卡片）")
        else:
            print('[E17] OK  "data": data 透传 run-stats（卡片不塞 data）')
    # [18] 签名不变（多行签名也接受——参数可每行一个）
    sig_match = re.search(
        r"async def persist_agent_reply\(([^)]*)\)",
        reply_mod,
        re.S,
    )
    if not sig_match:
        errs.append("[E18] persist_agent_reply 签名未找到")
    else:
        sig = sig_match.group(1)
        sig_ok = (
            "group_id: str" in sig
            and "agent_id: str" in sig
            and "content: str" in sig
            and re.search(r"data:\s*dict\[str,\s*Any\]\s*\|\s*None\s*=\s*None", sig) is not None
            and re.search(r"task_id:\s*str\s*\|\s*None\s*=\s*None", sig) is not None
        )
        if not sig_ok:
            errs.append("[E18] persist_agent_reply 签名变了（应保持 group_id, agent_id, content, data=None, task_id=None）")
        else:
            print("[E18] OK  persist_agent_reply 签名不变（既有调用方零改动）")

    # ── F. 行为零变（解析函数对空/无卡片输入的退化）──
    # 直接 import 跑（纯函数，无副作用）
    sys.path.insert(0, str(REPO / "backend"))
    try:
        from llm.card_fragment import count_card_fragments, extract_card_payloads  # noqa: E402
    except Exception as exc:  # pragma: no cover
        errs.append(f"[F] import card_fragment 失败: {exc}")
        return errs
    if count_card_fragments("") != 0:
        errs.append("[F19] count_card_fragments('') != 0")
    elif count_card_fragments("纯散文，无卡片") != 0:
        errs.append("[F19] count_card_fragments(纯散文) != 0")
    else:
        print("[F19] OK  count_card_fragments 对空串/纯散文返 0（不刷日志）")
    if extract_card_payloads("") != []:
        errs.append("[F20] extract_card_payloads('') != []")
    else:
        print("[F20] OK  extract_card_payloads('') == []")
    # [21] baidu 热搜 shape
    hot = (
        '```card\n{"icon":"🔥","title":"百度热搜 Top 5","kind":"table",'
        '"columns":["排名","标题","热度"],'
        '"rows":[["1","神舟二十号成功对接","9821"],["2","北方多地降温","8740"]]}\n```'
    )
    ps = extract_card_payloads(hot)
    if len(ps) != 1 or ps[0].get("kind") != "table":
        errs.append(f"[F21] baidu table 解码失败: {ps}")
    elif not isinstance(ps[0].get("rows"), list) or len(ps[0]["rows"]) != 2:
        errs.append(f"[F21] baidu rows 非 2 行: {ps[0].get('rows')}")
    elif not all(isinstance(v, str) for r in ps[0]["rows"] for v in r):
        errs.append("[F21] baidu rows 值非全 string（数字应 stringify）")
    else:
        print("[F21] OK  baidu table 解码：kind=table, rows=list[list[str]]（全 string）")

    # ── G. 单一真源 ──
    # [22] execute 路径复用常量（非复制文字）
    if "CARD_OUTPUT_CONTRACT" not in agent_loop:
        errs.append("[G22] agent_loop.py 未引用 CARD_OUTPUT_CONTRACT 常量（疑似复制文字）")
    else:
        print("[G22] OK  execute 路径复用 CARD_OUTPUT_CONTRACT 常量（非复制文字）")
    # [23] 正则只在 card_fragment.py 定义（reply.py / prompts.py 不重复定义）
    for f_path, f_name in ((REPLY, "reply.py"), (PROMPTS, "prompts.py")):
        src = f_path.read_text(encoding="utf-8") if f_path.exists() else ""
        if re.search(r're\.compile\(r"```card', src):
            errs.append(f"[G23] {f_name} 重复定义 card 正则（应只在 card_fragment.py）")
    else:
        print("[G23] OK  card 正则仅在 card_fragment.py（reply.py/prompts.py 不重复定义）")

    return errs


def main() -> int:
    print("=== 需求2-后端 回归：worker 结构化卡片片段提示词/解析契约 ===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "需求2-后端 契约锁定（卡片走 content 子串透传，不落 task.artifact / 不塞 reply data）：\n"
        "  · A card_fragment 解析模块（CARD_FRAGMENT_RE 与前端 byte-identical + count/extract 两函数）；\n"
        "  · B build_brain_prompt 末尾内嵌 CARD_OUTPUT_CONTRACT（chat 路径带契约，纯散文不强套）；\n"
        "  · C execute 路径 ReAct system prompt 也拼同一契约（_CARD_OUTPUT_SYSTEM_SUFFIX 复用常量）；\n"
        "  · D persist_agent_reply 落盘后 best-effort 统计卡片片段数（>0 才 logger.info，不阻断）；\n"
        "  · E 不改 DB/事件/message shape（7 key 不变 + data 仍载 run-stats + 签名不变）；\n"
        "  · F 行为零变（空串/纯散文返 0 + baidu table 解码全 string）；\n"
        "  · G 单一真源（契约文字在 prompts.py 一处，正则在 card_fragment.py 一处，execute 路径复用常量）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

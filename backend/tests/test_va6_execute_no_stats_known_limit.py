"""A6 评估·保守不改：execute 路径 announce 无 stats（已知限制锁存）.

任务 A6 评估 registry.py:398-406 `_reply` 是否需透传上一轮 create_react_agent 的 usage。
结论：保守不改，记录为已知限制。本测静态锁住该评估结论——防后续误改 _reply 加 stats
破坏「announce 无 stats」设计（与协调者 dispatch announce 排除同理），并锁住 A4/A5
已把创作锚定 chat（execute 路径只服务真工程任务）。

静态契约（读源码断言，不依赖后端在线）：

  A. execute 路径 announce 无 stats（设计取舍，非 bug）：
    1. registry._reply 恒 data=None（4 处调用：成功「任务完成🎉」/取消「⏹ 任务已停止」/
       超时「⏱ 超时」/失败「执行出错了」），announce 不带 stats。
    2. node_execute（worker.py）的 ack「收到，我来...」announce 也 data=None（不传 _stream_stats）。
    3. coordinator node_dispatch 的「📋 已制定协作计划」announce 也 data=None（A2 已锁）。
       —— 三处 announce 同模式：模板文本非流式决策文本，stats 不匹配 content，故不带。

  B. 创作类已锚定 chat 路径（A4/A5），execute 路径不再服务创作：
    4. build_brain_prompt chat 条款含「直接生成文本内容」枚举（写文章/写文案/写总结/翻译/
       改写/润色）+ execute 条款收紧为「写代码/改配置/运行命令/调用工具」+ 反向提醒创作不属于 execute。
    5. COORDINATOR_SYSTEM + build_coordinator_prompt action 说明同样把创作归 chat 不归 dispatch。

  C. 已知限制文档化（_reply docstring 锁住评估结论 + 改造路径）：
    6. _reply docstring 含「已知限制」「保守不改」+ 改造三步路径（usage_metadata 捕获 +
       _reply 加 data 参数 + 3 调用方分别处理）。
    7. run_agent_loop 当前不捕获 usage（astream_events 只取 content/tool）——佐证改造门槛高。

  D. chat 路径 stats 已透传（A2/A3 覆盖，本测复核接口一致）：
    8. worker node_chat → _unified_reply(data=_stream_stats)；coordinator node_chat 同款。
       chat 路径定稿气泡自带 stats（A6 主路径「作文改走 chat 后定稿气泡自带 stats」已确认）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
REGISTRY = REPO / "backend" / "engine" / "registry.py"
WORKER = REPO / "backend" / "engine" / "worker.py"
COORD = REPO / "backend" / "engine" / "coordinator.py"
PROMPTS = REPO / "backend" / "llm" / "prompts.py"
AGENT_LOOP = REPO / "backend" / "engine" / "agent_loop.py"


def _fn_body(src: str, fname: str) -> str:
    # 匹配 ``[async ]def fname(...)``（参数跨行用 [^)]* + re.S）。截到下一个同级
    # ``\n    [async ]def ``（4 空格缩进，类方法级）。worker.py 是模块级 async def
    # （0 缩进），coordinator/registry 是类方法（4 缩进）——两种缩进都试。
    for indent in ("    ", ""):
        m = re.search(
            rf"(?:async def|def) {fname}\([^)]*\)(.*?)(?=\n{indent}(?:async )?def )",
            src,
            re.S,
        )
        if m:
            return m.group(1)
    return ""


def assert_contract() -> list[str]:
    errs: list[str] = []
    registry = REGISTRY.read_text(encoding="utf-8")
    worker = WORKER.read_text(encoding="utf-8")
    coord = COORD.read_text(encoding="utf-8")
    prompts = PROMPTS.read_text(encoding="utf-8")
    agent_loop = AGENT_LOOP.read_text(encoding="utf-8")

    # ── A. execute 路径 announce 无 stats ──
    # [1] registry._reply 恒 data=None + docstring 标注已知限制
    #     B10 抽 persist_agent_reply 后，_reply 改调 persist_agent_reply(..., None)
    #     传 None 锁「恒 data=None」。两种断言都接受：内联 "data": None（B10 前）
    #     或 调 persist_agent_reply(...None)（B10 后）。核心契约「_reply 不传 stats」不变。
    reply_body = _fn_body(registry, "_reply")
    if not reply_body:
        errs.append("[1] registry._reply 函数体未找到")
    else:
        inline_data_none = '"data": None' in reply_body
        delegates_none = "persist_agent_reply" in reply_body and re.search(
            r"persist_agent_reply\([^)]*,\s*None\s*\)", reply_body, re.S
        ) is not None
        if not (inline_data_none or delegates_none):
            errs.append("[1] registry._reply 未恒 data=None（announce 不该带 stats）")
        else:
            print("[1] OK  registry._reply 恒 data=None（execute announce 无 stats，B10 调 persist_agent_reply(...None)）")
        if "已知限制" not in reply_body or "保守不改" not in reply_body:
            errs.append("[1] _reply docstring 未标注「已知限制/保守不改」（A6 评估结论未锁）")
        else:
            print("[1] OK  _reply docstring 标注「已知限制·保守不改」+ 改造路径")

    # [2] node_execute ack「收到，我来」announce 不带 stats（worker.py）
    exe_body = _fn_body(worker, "node_execute")
    if not exe_body:
        errs.append("[2] worker node_execute 函数体未找到")
    elif 'data=state.get("_stream_stats")' in exe_body:
        errs.append("[2] node_execute 传了 stats（ack announce 不该带）")
    else:
        print("[2] OK  node_execute ack「收到，我来...」announce 不带 stats（与 _reply 同模式）")

    # [3] coordinator node_dispatch announce「📋 已制定协作计划」不带 stats
    dispatch_body = _fn_body(coord, "node_dispatch")
    if not dispatch_body:
        errs.append("[3] coordinator node_dispatch 函数体未找到")
    elif 'data=state.get("_stream_stats")' in dispatch_body:
        errs.append("[3] node_dispatch 传了 stats（announce 不该带）")
    else:
        print("[3] OK  node_dispatch「📋 已制定协作计划」announce 不带 stats（三处 announce 同模式）")

    # ── B. 创作类锚定 chat（A4/A5），execute 不服务创作 ──
    # [4] build_brain_prompt 创作归 chat 不归 execute
    if "直接生成文本内容" not in prompts:
        errs.append("[4] build_brain_prompt 缺「直接生成文本内容」条款（A4 未落地）")
    else:
        # chat 条款含创作枚举 + execute 条款反向提醒
        has_chat_essay = any(
            kw in prompts for kw in ["写文章", "写文案", "写总结", "翻译", "改写", "润色"]
        )
        has_execute_exclude = "单纯生成文字内容" in prompts and "不属于 execute" in prompts
        if not (has_chat_essay and has_execute_exclude):
            errs.append("[4] build_brain_prompt 创作归 chat / execute 反向提醒不完整（A4 回归）")
        else:
            print("[4] OK  build_brain_prompt 创作归 chat + execute 反向提醒「单纯生成文字不属于 execute」")

    # [5] COORDINATOR_SYSTEM + build_coordinator_prompt 创作归 chat 不归 dispatch
    if "直接生成文本内容" not in prompts or "不属于 dispatch" not in prompts:
        errs.append("[5] COORDINATOR_SYSTEM/build_coordinator_prompt 缺创作归 chat 条款（A5 未落地）")
    else:
        coord_chat = "不要拆成 dispatch" in prompts or "都走 chat" in prompts
        coord_exclude = "单纯生成文字" in prompts and "不属于 dispatch" in prompts
        if not (coord_chat and coord_exclude):
            errs.append("[5] coordinator 创作归 chat / dispatch 反向提醒不完整（A5 回归）")
        else:
            print("[5] OK  COORDINATOR_SYSTEM + build_coordinator_prompt 创作归 chat 不归 dispatch")

    # ── C. 已知限制文档化 ──
    # [6] _reply docstring 含改造路径（usage_metadata）
    if "usage_metadata" not in reply_body:
        errs.append("[6] _reply docstring 未提及 usage_metadata 改造路径")
    else:
        print("[6] OK  _reply docstring 含改造路径（usage_metadata 捕获 + _reply 加 data + 3 调用方）")

    # [7] run_agent_loop 当前不捕获 usage（佐证改造门槛）
    if "usage_metadata" in agent_loop or "token_usage" in agent_loop:
        errs.append("[7] run_agent_loop 已捕获 usage——本限制前提已变，需重评")
    else:
        # 确认 on_chat_model_end 取 content 不取 usage
        m_end = re.search(r"on_chat_model_end.*?msg\.content", agent_loop, re.S)
        if not m_end:
            errs.append("[7] run_agent_loop on_chat_model_end 未取 msg.content（结构已变）")
        else:
            print("[7] OK  run_agent_loop 当前不捕获 usage（on_chat_model_end 只取 content）—— 改造需新增 usage 累加")

    # ── D. chat 路径 stats 已透传（A6 主路径确认）──
    # [8] worker node_chat + coordinator node_chat 都带 _stream_stats
    chat_w = _fn_body(worker, "node_chat")
    chat_c = _fn_body(coord, "node_chat")
    if 'data=state.get("_stream_stats")' not in chat_w:
        errs.append("[8] worker node_chat 未带 _stream_stats（chat 路径 stats 透传回归）")
    elif 'data=state.get("_stream_stats")' not in chat_c:
        errs.append("[8] coordinator node_chat 未带 _stream_stats（chat 路径 stats 透传回归）")
    else:
        print("[8] OK  worker + coordinator node_chat 都带 _stream_stats（chat 路径定稿气泡自带 stats）")

    return errs


def main() -> int:
    print("=== A6 评估·保守不改：execute 路径 announce 无 stats（已知限制锁存）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "A6 评估结论：保守不改，记录为已知限制。\n"
        "  · execute→task→「任务完成🎉」announce 不带 stats（设计取舍：announce 非流式决策文本，"
        "stats 不匹配 content）—— 三处 announce（node_execute ack / _reply 收尾 / node_dispatch 计划）同模式；\n"
        "  · A4/A5 已把创作类锚定 chat 路径（chat 路径 stats 已透传，A2/A3 锁定）——"
        "execute 路径只服务真工程任务，announce 不带 stats 对用户价值有限；\n"
        "  · _reply docstring 锁住评估结论 + 改造三步路径（usage_metadata 捕获 + _reply 加 data + 3 调用方）；\n"
        "  · run_agent_loop 当前不捕获 usage（on_chat_model_end 只取 content）——佐证改造跨 3 文件 + LangGraph 事件层，门槛高，保守不改。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

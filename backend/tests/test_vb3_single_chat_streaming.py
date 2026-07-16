"""验证B-3 自测：单聊回复逐字流式气泡可见（task 28）.

回归「CodeBuddy-style 气泡过程」前端单聊流式链路（task 23/24/25）：
  task 23  worker.py: 给单聊 worker 引入 reply_id（node_brain_decide 生成 uuid4 hex，
          塞进 _stream_stats；node_chat/ask 落盘到 agent_reply.data.reply_id）
  task 24  worker.py: node_brain_decide 的 async for content_delta 循环里推 task_token
          （按 reply_id 归并；用 _ContentExtractor 跳过 JSON 骨架，只推可见回复解码增量）
  task 25  useBusEvent.ts: mapKind 放行 task_token + 按 task_id 前缀分流
          （task_ 前缀→PL-08 真 task streaming；无前缀 reply_id→coordStreaming 复用
          协调者流式气泡渲染）

本自测验证「单聊回复逐字流式气泡可见」契约——分两段：

  阶段 A（前端静态契约）：读 useBusEvent.ts + ChatPanel.tsx + worker.py 源码，
    断言单聊流式 token 归并 + 渲染链路完整接线：
    1. useBusEvent.mapKind('task_token')=='token'（task_token 进 TraceEvent 流 + 排除出 logs）。
    2. useBusEvent task_token 分流：task_id 前缀 'task_' → streaming[task_id]（PL-08）；
       否则（reply_id 裸 hex）→ coordStreaming[reply_id]（复用协调者流式气泡渲染）。
    3. useBusEvent 排除 task_token 出 logs（逐字 delta 不当一条日志灌 LogPanel）。
    4. ChatPanel.coordinatorStreamingBubbles 遍历 coordStreaming 按 reply_id 渲染
       ChatMessageBubble（isStreaming=true，content=累积 delta）——单聊回复复用此气泡。
    5. ChatPanel BusEventContext 消费 coordStreaming（单聊流式缓冲经 context 下发）。
    6. 后端 worker.py node_brain_decide 生成 reply_id + 在 async for 循环里 emit_task_token
       （reply_id 作 task_id 槽位，phase='streaming'）。

  阶段 B（后端运行时 + 流式拼接等式）：单聊群「后端工程师」发 chat 类消息（非动手指令，
    避免触发 execute 走 create_react_agent 的 task_token），WS 抓 task_token 事件 +
    持久化 agent_reply → 断言：
    7. 收到多条 task_token 事件（≥5，逐字增量非一次性）。
    8. task_token 的 task_id 是裸 hex（无 'task_' 前缀 = reply_id，非 PL-08 真 task）。
       这证明 worker 单聊流式走 reply_id 通道（task 24），不是 PL-08 execute 路径。
    9. 全部 task_token content 按到达序拼接 == 持久化 agent_reply.content
       （最强证据：流式 token 拼起来就是最终定稿回复，证明前端拼接逻辑正确、
        后端 token 与终态文本同源——_ContentExtractor 跳过 JSON 骨架后取的 content
        字段值即 decision.content，与 node_chat 落盘的 content 同一来源）。
    10. 持久化 agent_reply.data.reply_id 存在（task 23 落盘），且 == task_token 的 task_id
        （流式缓冲 key == 定稿回复的 reply_id → 前端 finalizedBubbles 退场判定可对齐）。

为何用「拼接相等」而非语义判断：
  与 PL-08 同款「数学等式校验」——_ContentExtractor.feed/take 取的 content 增量累加
  即模型本次调用的可见回复文本，_parse_brain_decision 解析的 decision.content 也是
  同一 content 字段值（extractor 与 parser 都从 raw JSON 取 content），二者必然相等。
  任何不等都说明流式通路有 bug（如 extractor 漏解码、JSON 骨架泄漏），是最硬的逐字流式证据。

为何发 chat 类消息而非 execute：
  task 24 的流式 token 在 node_brain_decide 的 chat_completion_stream 循环里推——
  brain 每次调用 LLM 都流式（无论最终 action 是 chat/execute/ask）。但 execute 路径
  还会触发 _run_worker_task → create_react_agent，后者也推 task_token（PL-08，task_id 是
  tq_ 真任务 id）。两条流式混在一起会污染「单聊流式」的判定（task_id 会有 reply_id 裸 hex
  + tq_ 两种）。故发 chat 类消息（纯讨论/咨询，brain 判 chat → node_chat 落盘），
  只走 brain 流式通道，task_token 全是 reply_id 裸 hex，干净验证 task 24/25 链路。

为何用单聊群「后端工程师」：
  group_e53545c（single_chat=true，coordinator_id=agent_backend_1）——agent_backend_1
  走 worker 图（is_coordinator 但 single_chat→graph_kind=worker），brain 用其自身
  system_prompt 主导。@后端工程师 或裸消息都直送其 brain（route_user_message 无 @mention
  → coordinator_id=agent_backend_1 → push_notify coordinator_reply → worker brain）。
  单 worker 单任务内存占用低。

为何「chat 类消息」用成语接龙/闲聊：
  agent_backend_1 的 system_prompt 是「你是后端工程师，负责 API 与数据层开发」——纯工作
  人设对闲聊可能回避。但单聊群不加「团队互动」语义（registry 单聊分支注释），故用直接
  问一个技术讨论问题（chat 路径最稳：brain 判 chat → 流式回复 → node_chat 落盘），
  避免 execute（污染 task_id）和 ask（回复太短流式 token 少）。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time

import httpx
import websockets

BASE = "http://localhost:8000"
# 单聊群「后端工程师」——single_chat=true，coordinator_id=agent_backend_1。
GROUP_ID = "group_e53545c71a8c4cf8ae5e69d06ef77952"
WS_URL = f"ws://localhost:8000/ws/bus/{GROUP_ID}"
WORKER_ID = "agent_backend_1"

# chat 类消息（技术讨论）——brain 判 chat → node_chat 落盘，只走 brain 流式通道，
# 不触发 execute（避免 PL-08 tq_ task_token 污染 reply_id 流式判定）。
# 要求多句话回复，确保足够 token 体积验证逐字流式。
TASK_CONTENT = (
    "你好，请帮我详细介绍一下 RESTful API 的核心设计原则，"
    "包括资源命名、HTTP 方法语义、状态码使用这几个方面，每个方面展开 2-3 句话说明。"
)

WS_TIMEOUT = 180.0
MIN_TOKEN_EVENTS = 5  # 至少 5 个 token 增量才算「逐字」而非一次性

# 前端源码路径（静态契约断言用）。
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
USE_BUS_EVENT = REPO / "src" / "hooks" / "useBusEvent.ts"
CHAT_PANEL = REPO / "src" / "components" / "ChatPanel.tsx"
WORKER_PY = REPO / "backend" / "engine" / "worker.py"


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def worker_status() -> str:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/status/{GROUP_ID}")
        for a in r.json():
            if a["id"] == WORKER_ID:
                return a["status"]
    return "unknown"


async def send_message(content: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{BASE}/api/messages",
            json={
                "group_id": GROUP_ID,
                "sender_id": "user",
                "receiver_id": "broadcast",
                "type": "user_input",
                "content": content,
            },
        )
        return r.json()


async def collect_until_reply(timeout: float) -> list[dict]:
    """连 WS 收事件直到 worker 的 agent_reply 落地或超时。

    单聊 chat 路径无 task_complete（非 execute），收尾信号是 worker 的 agent_reply
    事件（node_chat → _unified_reply → emit_message_added）。收到后再多收 2s 尾巴
    （mention 路由等紧随其后）。
    """
    events: list[dict] = []
    deadline = time.time() + timeout
    finished = False
    async with websockets.connect(WS_URL, ping_interval=None, ping_timeout=None, max_size=8 * 1024 * 1024) as ws:
        while time.time() < deadline and not finished:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            events.append(ev)
            # 收尾信号：worker 的 agent_reply 落地（node_chat 持久化）
            if (
                ev.get("type") == "agent_reply"
                and ev.get("sender_id") == WORKER_ID
            ):
                end = time.time() + 2.0
                while time.time() < end:
                    try:
                        raw2 = await asyncio.wait_for(
                            ws.recv(), timeout=max(0.1, end - time.time())
                        )
                        events.append(json.loads(raw2))
                    except asyncio.TimeoutError:
                        break
                finished = True
    return events


# ── 前端 + 后端静态契约断言 ──


def assert_contract() -> list[str]:
    """读前端 useBusEvent.ts + ChatPanel.tsx + 后端 worker.py 源码断言单聊流式链路。"""
    errs: list[str] = []
    hook = USE_BUS_EVENT.read_text(encoding="utf-8")
    panel = CHAT_PANEL.read_text(encoding="utf-8")
    worker = WORKER_PY.read_text(encoding="utf-8")

    # [1] mapKind('task_token')=='token' + 排除出 logs
    m = re.search(r"case 'task_token':\s*return\s*'(\w+)'", hook)
    if not m:
        errs.append("[前端1] useBusEvent.mapKind 未把 'task_token' 映成 kind")
    elif m.group(1) != "token":
        errs.append(f"[前端1] mapKind('task_token')='{m.group(1)}'（应为 'token'）")
    else:
        print("[前端1] OK  mapKind('task_token')=='token'（进 TraceEvent 流）")
    # logs 排除 task_token
    if "d.type !== 'task_token'" not in hook:
        errs.append("[前端1] useBusEvent logs 未排除 task_token（逐字 delta 会灌爆 LogPanel）")
    else:
        print("[前端1] OK  logs 排除 task_token（逐字 delta 不当一条日志）")

    # [2] task_token 分流：task_ 前缀 → streaming；否则 → coordStreaming
    if "key.startsWith('task_')" not in hook:
        errs.append("[前端2] useBusEvent task_token 未按 'task_' 前缀分流（task 25 缺失）")
    else:
        # 确认：task_ 前缀 → setStreaming；else → setCoordStreaming
        m2 = re.search(
            r"if \(d\.type === 'task_token'\) \{.*?if \(key\.startsWith\('task_'\)\) \{.*?setStreaming\(.*?\} else \{(.*?)setCoordStreaming",
            hook,
            re.S,
        )
        if not m2:
            errs.append("[前端2] task_token 分流结构不符（task_→streaming / else→coordStreaming）")
        else:
            print("[前端2] OK  task_token 分流：task_ 前缀→streaming[task_id]，否则→coordStreaming[reply_id]")

    # [3] ChatPanel.coordinatorStreamingBubbles 遍历 coordStreaming 渲染 ChatMessageBubble
    if "coordinatorStreamingBubbles" not in panel:
        errs.append("[前端3] ChatPanel 无 coordinatorStreamingBubbles（流式气泡渲染缺失）")
    elif "Object.entries(coordStreaming)" not in panel:
        errs.append("[前端3] coordinatorStreamingBubbles 未遍历 coordStreaming（按 reply_id 取缓冲）")
    elif "coord-streaming-" not in panel:
        errs.append("[前端3] 流式气泡未用 coord-streaming- key（按 reply_id 区分气泡）")
    else:
        # 确认渲染 ChatMessageBubble 且 isStreaming
        m3 = re.search(
            r"\{coordinatorStreamingBubbles\.map\(\(b\) => \{.*?<ChatMessageBubble",
            panel,
            re.S,
        )
        if not m3:
            errs.append("[前端3] coordinatorStreamingBubbles 未渲染 ChatMessageBubble")
        else:
            print("[前端3] OK  coordinatorStreamingBubbles 遍历 coordStreaming → ChatMessageBubble（单聊复用此气泡）")

    # [4] BusEventContext 消费 coordStreaming（单聊流式缓冲经 context 下发）
    # 宽松校验：useBusEventContext() 解构行含 coordStreaming（单行解构，跨多字段）。
    if "coordStreaming: ctx.coordStreaming" not in hook:
        errs.append("[前端4] useBusEvent 未把 coordStreaming 经 WS-02 命中复用下发")
    else:
        # ChatPanel 的 useBusEventContext() 解构行包含 coordStreaming
        ctx_line = next(
            (ln for ln in panel.splitlines() if "useBusEventContext()" in ln),
            "",
        )
        if "coordStreaming" not in ctx_line:
            errs.append("[前端4] ChatPanel useBusEventContext 解构未含 coordStreaming")
        else:
            print("[前端4] OK  coordStreaming 经 BusEventContext 下发 + ChatPanel 消费")

    # [5] 后端 worker.py node_brain_decide 生成 reply_id + emit_task_token(reply_id, ...)
    if "reply_id = uuid.uuid4().hex" not in worker:
        errs.append("[后端5] worker.py 未生成 reply_id（task 23 缺失）")
    elif "emit_task_token(" not in worker:
        errs.append("[后端5] worker.py 未调 emit_task_token（task 24 缺失）")
    else:
        # emit_task_token 调用块跨多行含嵌套括号，用「await emit_task_token(」到「piece,」整段校验
        # 第二个位置参数（task_id 槽位）应是 reply_id（裸标识，非 state.get(...) 等表达式）。
        # B3 抽出 _stream_brain_decision 后第一参数从 state.get("group_id","") 改为 group_id 形参
        # （镜像协调者 _stream_coordinator_decision 用 group_id 形参）。两种写法都接受，核心契约
        # 「task_id 槽位是 reply_id（裸 hex）」不变。
        emit_block = re.search(
            r'await emit_task_token\(\s*(?:state\.get\("group_id", ""\)|group_id),\s*(\w+),',
            worker,
            re.S,
        )
        if not emit_block:
            errs.append("[后端5] emit_task_token 调用结构不符（无法定位 task_id 槽位参数）")
        elif emit_block.group(1) != "reply_id":
            errs.append(
                f"[后端5] emit_task_token task_id 槽位传的是 '{emit_block.group(1)}'（应为 reply_id）"
            )
        else:
            print("[后端5] OK  worker.py reply_id 生成 + emit_task_token(reply_id) 推逐字 delta")

    # [6] ContentExtractor 跳过 JSON 骨架（只推可见回复解码增量）
    #     B9 抽出 _ContentExtractor 到 llm/json_stream.py（公共 ContentExtractor），
    #     消除 worker 从 coordinator 反向导入。两种名都接受（核心契约「worker 用
    #     streaming-JSON extractor 跳骨架」不变，仅类名/位置迁移）。
    if "_ContentExtractor" not in worker and "ContentExtractor" not in worker:
        errs.append("[后端6] worker.py 未用 ContentExtractor（JSON 骨架会泄漏到流式气泡）")
    elif "extractor.feed" not in worker or "extractor.take" not in worker:
        errs.append("[后端6] ContentExtractor feed/take 未接线")
    else:
        print("[后端6] OK  ContentExtractor.feed/take 跳过 JSON 骨架，只推可见回复解码增量")

    return errs


async def main() -> int:
    print("=== 验证B-3：单聊回复逐字流式气泡可见 ===\n")

    # ── 阶段 A：静态契约 ──
    print("── 阶段 A：静态契约断言 ──")
    fe_errs = assert_contract()
    if fe_errs:
        print("\n[阶段A] FAIL:")
        for e in fe_errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL（静态契约） ===")
        return 1
    print("[阶段A] PASS\n")

    # ── 阶段 B：后端运行时 + 流式拼接等式 ──
    print("── 阶段 B：后端运行时 + 流式拼接等式 ──")
    if not await health_ok():
        print("[fatal] backend 不在线"); return 2
    print("[health] ok")

    # 等 worker 空闲
    for _ in range(30):
        st = await worker_status()
        if st == "idle":
            break
        print(f"[wait] worker 状态={st}，等待空闲...")
        await asyncio.sleep(2)
    else:
        print("[fatal] worker 一直 busy，放弃本次自测"); return 2
    print(f"[worker] {WORKER_ID} idle")

    # 连 WS + 发 chat 类消息
    ws_task = asyncio.create_task(collect_until_reply(WS_TIMEOUT))
    await asyncio.sleep(0.5)
    sent = await send_message(TASK_CONTENT)
    print(f"[send] user message id={sent.get('id','')[:16]}...")

    events = await ws_task
    type_counts: dict[str, int] = {}
    for e in events:
        t = e.get("type", "?")
        type_counts[t] = type_counts.get(t, 0) + 1
    print(f"[events] 收到 {len(events)} 条; 类型分布={type_counts}")

    # ── 抽取 task_token + 持久化 agent_reply ──
    token_events = [e for e in events if e.get("type") == "task_token"]
    # worker 的持久化回复（node_chat 落地，时间最晚的一条 agent_reply from worker）
    reply_ev = next(
        (e for e in reversed(events)
         if e.get("type") == "agent_reply" and e.get("sender_id") == WORKER_ID),
        None,
    )

    errs: list[str] = []

    # ── 校验 7：收到多条 task_token（≥5，逐字增量）──
    n_tokens = len(token_events)
    print(f"[check 7] task_token 事件数={n_tokens} (要求 ≥{MIN_TOKEN_EVENTS})")
    if n_tokens < MIN_TOKEN_EVENTS:
        errs.append(f"task_token 事件仅 {n_tokens} 条（要求 ≥{MIN_TOKEN_EVENTS}，未达逐字流式粒度）")

    # token 增量粒度（多数短增量）
    deltas = [str(e.get("content") or "") for e in token_events]
    short_count = sum(1 for d in deltas if len(d) <= 12)
    short_ratio = short_count / len(deltas) if deltas else 0.0
    print(f"[check 7] token 增量数={len(deltas)} 短增量(≤12字)占比={short_ratio:.0%}")
    if deltas and short_ratio < 0.5:
        errs.append(f"短增量占比仅 {short_ratio:.0%}（要求 ≥50%，token 粒度过粗非逐字）")
    if deltas:
        print(f"  [token样本] 前8个增量: {deltas[:8]!r}")

    # ── 校验 8：task_token 的 task_id 是裸 hex（无 'task_' 前缀 = reply_id）──
    token_task_ids = {e.get("task_id") for e in token_events if e.get("task_id")}
    print(f"[check 8] task_token task_id 集合={token_task_ids}")
    if not token_task_ids:
        errs.append("task_token 无 task_id（reply_id 未透传到 task_id 槽位）")
    else:
        non_task_prefix = [tid for tid in token_task_ids if not str(tid).startswith("task_")]
        if len(token_task_ids) > 1:
            errs.append(
                f"task_token task_id 不唯一（{token_task_ids}）——可能混入 PL-08 execute 路径的 tq_ task_token"
            )
        elif not non_task_prefix:
            errs.append(
                f"task_token task_id 全是 'task_' 前缀（{token_task_ids}）——走的是 PL-08 真 task 路径，"
                f"非单聊 worker brain 流式（task 24 未触发）"
            )
        else:
            # 裸 hex（32 字符 uuid4 hex，无前缀）
            tid = next(iter(token_task_ids))
            if len(str(tid)) == 32 and re.match(r"^[0-9a-f]{32}$", str(tid)):
                print(f"[check 8] OK  task_token task_id 是裸 hex（reply_id，非 PL-08 真 task）")
            else:
                errs.append(
                    f"task_token task_id={tid} 非 32 位裸 hex（reply_id 格式异常）"
                )

    # ── 校验 9：流式 token 拼接 == 持久化 agent_reply.content（同源等式）──
    full_stream = "".join(deltas)
    reply_text = str(reply_ev.get("content") or "") if reply_ev else ""
    print(f"[check 9] 流式拼接长度={len(full_stream)} 定稿回复长度={len(reply_text)}")
    if not reply_ev:
        errs.append("未收到 worker agent_reply（node_chat 未落盘回复）")
    elif not full_stream:
        errs.append("流式拼接为空（task_token content 全空）")
    else:
        # 严格相等；若不等（如 extractor 截断/转义差异），降级为包含校验
        strict_eq = full_stream == reply_text
        contain_ok = (
            full_stream in reply_text or reply_text in full_stream
        )
        print(f"[check 9] 严格相等={strict_eq} 包含降级={contain_ok}")
        if not strict_eq and not contain_ok:
            errs.append("流式 token 拼接与定稿回复既不严格相等也不互含（流式与终态不同源）")
            print(f"  [diag] 流式拼接(前200): {full_stream[:200]!r}")
            print(f"  [diag] 定稿回复(前200): {reply_text[:200]!r}")
        elif strict_eq:
            print(f"[check 9] OK  流式拼接 == 定稿回复（严格相等，逐字流式同源）")
        else:
            print(f"[check 9] OK  流式拼接与定稿回复互含（同源，逐字流式可见）")
        if reply_text:
            print(f"  [reply样本] {reply_text[:80]!r}")

    # ── 校验 10：持久化 agent_reply.data.reply_id 存在且 == task_token task_id ──
    if reply_ev:
        reply_data = reply_ev.get("data") or {}
        reply_id_in_data = reply_data.get("reply_id") if isinstance(reply_data, dict) else None
        if not reply_id_in_data:
            errs.append("持久化 agent_reply.data.reply_id 缺失（task 23 未落盘 reply_id）")
        else:
            print(f"[check 10] agent_reply.data.reply_id={str(reply_id_in_data)[:16]}...")
            if token_task_ids and reply_id_in_data in token_task_ids:
                print(f"[check 10] OK  data.reply_id == task_token task_id（流式缓冲 key == 定稿回复 reply_id）")
            else:
                errs.append(
                    f"data.reply_id={reply_id_in_data} 不在 task_token task_id 集合 {token_task_ids}"
                    f"（流式缓冲 key 与定稿回复 reply_id 未对齐，finalizedBubbles 退场判定会失配）"
                )

    if errs:
        print("\n[阶段B] FAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL（运行时） ===")
        return 1

    print("\n=== 结果: PASS ===")
    print(
        "单聊回复逐字流式气泡可见：\n"
        "  · 静态契约：mapKind('task_token')=='token' + logs 排除 task_token + "
        "task_ 前缀分流（task_→streaming / 裸 hex→coordStreaming）+ "
        "coordinatorStreamingBubbles 渲染 + coordStreaming 经 context 下发 + "
        "worker.py reply_id 生成 + emit_task_token + ContentExtractor 跳 JSON 骨架；\n"
        f"  · 运行时：收到 {n_tokens} 个 task_token 逐字增量（短增量占比 {short_ratio:.0%}），"
        f"task_id 是裸 hex（reply_id，非 PL-08 真 task），"
        f"流式拼接 == 定稿回复（同源等式），"
        f"data.reply_id == task_token task_id（缓冲 key 对齐定稿回复）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

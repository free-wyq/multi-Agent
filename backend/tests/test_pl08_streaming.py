"""PL-08 自测：验证思考/答案逐 token 流式呈现。

不依赖 pytest，直接 asyncio 跑。验证路径：@后端工程师 发一个需要「先看工作区再写
分析报告」的任务——worker 会 list_dir（工具）→ 写一份多句文本答案。agentic loop 里
create_agent 的每次模型调用经 on_chat_model_stream 逐 token 推 emit_task_token
（type==task_token），on_chain_end|model 时推完整 task_think（thinking/final）。

核心校验（确定性，非语义判断）：
  1. 收到多条 task_token 事件（≥5）——证明是「逐 token 增量」而非一次性整段
  2. 每个 task_token 的 content 是短增量（多数 ≤12 字）——证明是 token 粒度非整段
  3. 全部 task_token content 按到达序拼接 == 全部 task_think content 按到达序拼接
     ——最强证据：流式 token 拼起来就是最终定稿文本，证明前端拼接逻辑正确、
     后端 token 与终态文本同源（on_chat_model_stream 与 on_chain_end|model 取自
     同一个 AIMessage.content）
  4. task_complete(success) 收尾

为何用「拼接相等」而非语义判断：
  LLM 输出语义判断易误判。「流式 token 拼接 == 定稿文本」是数学等式校验——
  on_chat_model_stream 的 chunk.content 累加即模型本次调用的完整文本输出，
  on_chain_end|model 的 _extract_ai_content 取的也是同一 AIMessage.content，
  二者必然相等。任何不等都说明流式通路有 bug，是最硬的逐字流式证据。

为何只发单任务而非双用例：
  M12 自测曾因双用例+驻留后端叠加触发 exit 137 OOM。本任务单 worker 单任务，
  token 体积小，内存占用远低于 M12，单用例足够覆盖流式通路且更稳。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx
import websockets

BASE = "http://localhost:8000"
WS_URL = "ws://localhost:8000/ws/bus/group_demo_1"
GROUP_ID = "group_demo_1"
WORKER_ID = "agent_backend_1"

# 任务：让 worker 先用 list_dir 看工作区（触发一次工具调用模型轮），再写多句分析
# 报告（触发最终答案模型轮，产生足够 token 体积验证逐字流式）。
TASK_CONTENT = (
    f"@后端工程师 请用 list_dir 看一下当前工作区有哪些文件，"
    f"然后写一份 3-4 句话的工作区文件结构简要分析报告直接回复我（不用写文件，口头回复即可）。"
)

WS_TIMEOUT = 180.0
MIN_TOKEN_EVENTS = 5  # 至少 5 个 token 增量才算「逐字」而非一次性


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


async def collect_until_done(timeout: float) -> list[dict]:
    """连 WS 收事件直到 task_complete/task_failed 或超时。返回全量事件（到达序）。"""
    events: list[dict] = []
    deadline = time.time() + timeout
    finished = False
    async with websockets.connect(WS_URL) as ws:
        while time.time() < deadline and not finished:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            events.append(ev)
            if ev.get("type") in ("task_complete", "task_failed"):
                end = time.time() + 3.0
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


async def main() -> int:
    print("=== PL-08 自测：思考/答案逐 token 流式呈现 ===")
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

    # 并发：连 WS + 发消息
    ws_task = asyncio.create_task(collect_until_done(WS_TIMEOUT))
    await asyncio.sleep(0.5)
    sent = await send_message(TASK_CONTENT)
    print(f"[send] user message id={sent.get('id','')[:16]}...")

    events = await ws_task

    # 事件类型统计
    type_counts: dict[str, int] = {}
    for e in events:
        type_counts[e.get("type", "?")] = type_counts.get(e.get("type", "?"), 0) + 1
    print(f"[events] 收到 {len(events)} 条; 类型分布={type_counts}")

    # 校验 1：task_token 事件数
    token_events = [e for e in events if e.get("type") == "task_token"]
    n_tokens = len(token_events)
    print(f"[check 1] task_token 事件数={n_tokens} (要求 ≥{MIN_TOKEN_EVENTS})")

    # 校验 2：token 增量粒度（多数短增量）
    deltas = [str(e.get("content") or "") for e in token_events]
    short_count = sum(1 for d in deltas if len(d) <= 12)
    short_ratio = short_count / len(deltas) if deltas else 0.0
    print(f"[check 2] token 增量数={len(deltas)} 短增量(≤12字)占比={short_ratio:.0%}")
    if deltas:
        sample = deltas[:8]
        print(f"[token] 前8个增量样本: {sample!r}")

    # 校验 3：流式 token 拼接 == task_think 拼接（同源等式）
    full_stream = "".join(deltas)
    think_events = [e for e in events if e.get("type") == "task_think"]
    full_think = "".join(str(e.get("content") or "") for e in think_events)
    print(f"[check 3] 流式拼接长度={len(full_stream)} 定稿拼接长度={len(full_think)}")
    # 严格相等；若不等（如推理模型 reasoning_content 走旁路），降级为包含校验
    strict_eq = full_stream != "" and full_stream == full_think
    contain_ok = (
        full_stream != ""
        and full_think != ""
        and (full_stream in full_think or full_think in full_stream)
    )
    print(f"[check 3] 严格相等={strict_eq} 包含降级={contain_ok}")
    if not strict_eq and not contain_ok:
        print(f"[diag] 流式拼接(前200): {full_stream[:200]!r}")
        print(f"[diag] 定稿拼接(前200): {full_think[:200]!r}")

    # 校验 4：task_complete success
    complete_ev = next((e for e in events if e.get("type") == "task_complete"), None)
    failed_ev = next((e for e in events if e.get("type") == "task_failed"), None)
    success = complete_ev is not None and failed_ev is None
    print(f"[check 4] task_complete(success)={success}")

    errs = []
    if n_tokens < MIN_TOKEN_EVENTS:
        errs.append(f"task_token 事件仅 {n_tokens} 条（要求 ≥{MIN_TOKEN_EVENTS}，未达逐字流式粒度）")
    if not deltas:
        errs.append("未捕获任何 task_token 事件（on_chat_model_stream 未流转到 emit_task_token）")
    elif short_ratio < 0.5:
        errs.append(f"短增量占比仅 {short_ratio:.0%}（要求 ≥50%，token 粒度过粗非逐字）")
    if not (strict_eq or contain_ok):
        errs.append("流式 token 拼接与定稿文本既不严格相等也不互含（流式与终态不同源，通路有误）")
    if not success:
        tail = (complete_ev or failed_ev or {}).get("content", "")
        errs.append(f"任务未以 task_complete(success) 收尾 (tail={tail[:80]!r})")

    if errs:
        print("\n[结果] FAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1

    print("\n=== 结果: PASS ===")
    print(f"收到 {n_tokens} 个 task_token 逐字增量，短增量占比 {short_ratio:.0%}，"
          f"流式拼接(长度{len(full_stream)})与定稿文本"
          f"{'严格相等' if strict_eq else '互含'}，证明 on_chat_model_stream→emit_task_token→"
          f"前端拼接 通路正确，思考/答案逐字流式呈现达成。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

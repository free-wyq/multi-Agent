"""自测：协调者流式回复状态行应携带 model + reasoning_tokens + 推理事件。

验证本轮改动：backend 把 LLM model + reasoning 透传到 coordinator_stats /
coordinator_reasoning WS 事件 + 持久化 agent_reply.data，前端据此渲染
「model · Ns · ↓ N tokens（含 N 推理）」状态行 + 折叠推理区。

校验点（确定性，不判语义）：
  1. coordinator_stats 事件 data 含 model / elapsed_ms / tokens / phase（既有契约不破）
  2. coordinator_stats 的 model == 当前活跃 config 的 model
  3. coordinator_stats 事件 data 含 reasoning_tokens 字段（int，≥0）
     —— DeepSeek 推理模型 reasoning_tokens > 0；非推理模型 = 0（字段仍在，不缺）
  4. coordinator_reasoning 事件存在且逐字 delta 非空（推理模型流 reasoning_content）
     —— 非推理模型不流，此条跳过（不视为 fail）
  5. 持久化 agent_reply.data 含 model + reasoning_tokens（定稿气泡保留可见）

直接 asyncio + websockets，不依赖 pytest。
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
COORD_ID = "agent_coord_1"

# 一句无需派工的纯闲聊——协调者应走 action=chat，触发 _stream_coordinator_decision
# → emit coordinator_token + coordinator_reasoning(推理模型) + coordinator_stats(含 model/reasoning_tokens)
# + node_chat 落盘 data。
CHAT_CONTENT = "你好，用一句话介绍下你自己"


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def active_model() -> str:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/config")
        return str(r.json().get("model") or "")


async def coord_status() -> str:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/status/{GROUP_ID}")
        for a in r.json():
            if a["id"] == COORD_ID:
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


async def collect_coord_reply(timeout: float) -> tuple[list[dict], dict | None]:
    """收 WS 事件直到协调者 chat 回复落地（agent_reply from coordinator）或超时。

    返回 (全量事件, 协调者持久化回复事件 或 None)。
    """
    events: list[dict] = []
    reply_ev: dict | None = None
    deadline = time.time() + timeout
    async with websockets.connect(WS_URL) as ws:
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            events.append(ev)
            # 协调者定稿回复：type=agent_reply 且 sender=coordinator
            if (
                ev.get("type") == "agent_reply"
                and ev.get("sender_id") == COORD_ID
                and reply_ev is None
            ):
                reply_ev = ev
                # 多收 1s 让迟到的 coordinator_stats(done) 落进来
                end = time.time() + 1.0
                while time.time() < end:
                    try:
                        raw2 = await asyncio.wait_for(
                            ws.recv(), timeout=max(0.1, end - time.time())
                        )
                        events.append(json.loads(raw2))
                    except asyncio.TimeoutError:
                        break
                break
    return events, reply_ev


async def fetch_persisted_reply(reply_id: str) -> dict | None:
    """从 /api/messages 拉群消息，找含 reply_id 的 agent_reply（落盘的 data）。"""
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/messages", params={"group_id": GROUP_ID})
        msgs = r.json()
        items = msgs if isinstance(msgs, list) else msgs.get("items", msgs.get("messages", []))
        for m in items:
            data = m.get("data") or {}
            if isinstance(data, dict) and data.get("reply_id") == reply_id:
                return m
    return None


async def main() -> int:
    print("=== 自测：协调者状态行 model + reasoning ===")
    if not await health_ok():
        print("[fatal] backend 不在线"); return 2
    print("[health] ok")

    expected_model = await active_model()
    print(f"[config] 当前活跃 model={expected_model!r}")

    # 等协调者空闲
    for _ in range(20):
        st = await coord_status()
        if st == "idle":
            break
        print(f"[wait] coordinator 状态={st}...")
        await asyncio.sleep(2)
    else:
        print("[fatal] coordinator 一直 busy"); return 2
    print("[coord] idle")

    ws_task = asyncio.create_task(collect_coord_reply(60.0))
    await asyncio.sleep(0.5)
    sent = await send_message(CHAT_CONTENT)
    print(f"[send] user message id={sent.get('id','')[:16]}...")

    events, reply_ev = await ws_task

    type_counts: dict[str, int] = {}
    for e in events:
        t = e.get("type", "?")
        type_counts[t] = type_counts.get(t, 0) + 1
    print(f"[events] 收到 {len(events)} 条; 类型分布={type_counts}")

    # ---- 校验 1+2：coordinator_stats 事件携带 model + 既有字段 ----
    stats_events = [e for e in events if e.get("type") == "coordinator_stats"]
    print(f"[stats] coordinator_stats 事件数={len(stats_events)}")
    errs: list[str] = []
    stats_models: set[str] = set()
    for s in stats_events:
        data = s.get("data") or {}
        m = data.get("model")
        stats_models.add(str(m) if m is not None else "<missing>")
        for k in ("reply_id", "elapsed_ms", "tokens", "phase"):
            if k not in data:
                errs.append(f"coordinator_stats 缺字段 {k}（既有契约被破坏）")
                break
    print(f"[stats] model 取值集合={stats_models}")
    if not stats_events:
        errs.append("未收到任何 coordinator_stats 事件（_stream_coordinator_decision 未流转）")
    else:
        for s in stats_events:
            data = s.get("data") or {}
            m = data.get("model")
            if not m:
                errs.append(f"coordinator_stats 的 model 为空/缺失（phase={data.get('phase')})")
                break
            if str(m) != expected_model:
                errs.append(
                    f"coordinator_stats.model={m!r} ≠ 活跃 config model={expected_model!r}"
                )
                break

    # ---- 校验 3：coordinator_stats 事件携带 reasoning_tokens（int ≥0）----
    reasoning_tokens_values: list[int] = []
    if stats_events:
        for s in stats_events:
            data = s.get("data") or {}
            rt = data.get("reasoning_tokens")
            if not isinstance(rt, int) or rt < 0:
                errs.append(
                    f"coordinator_stats 缺/非法 reasoning_tokens（phase={data.get('phase')} val={rt!r}）"
                )
                break
            reasoning_tokens_values.append(rt)
        if reasoning_tokens_values:
            final_rt = reasoning_tokens_values[-1]
            print(f"[stats] reasoning_tokens 序列末值={final_rt}（推理模型应 >0）")

    # ---- 校验 4：coordinator_reasoning 事件（推理模型逐字流 reasoning_content）----
    reasoning_events = [e for e in events if e.get("type") == "coordinator_reasoning"]
    reasoning_full = "".join(str(e.get("content") or "") for e in reasoning_events)
    print(f"[reasoning] coordinator_reasoning 事件数={len(reasoning_events)} 拼接长度={len(reasoning_full)}")
    if reasoning_tokens_values and reasoning_tokens_values[-1] > 0:
        # 推理模型：reasoning 事件该有内容
        if not reasoning_events or not reasoning_full:
            errs.append("推理模型 reasoning_tokens>0 却无 coordinator_reasoning 事件（reasoning_content 未流转）")
        else:
            print(f"[reasoning] 样本(前80字): {reasoning_full[:80]!r}")
    else:
        print("[reasoning] 非推理模型（reasoning_tokens=0）→ coordinator_reasoning 事件可有可无，跳过")

    # ---- 校验 5：持久化 agent_reply.data.model + reasoning_tokens 落盘 ----
    reply_id = None
    if reply_ev:
        rd = reply_ev.get("data") or {}
        reply_id = rd.get("reply_id")
        print(f"[reply] agent_reply 事件 reply_id={reply_id} data.keys={list(rd.keys())}")
        if "model" not in rd:
            errs.append("agent_reply 事件 data 缺 model（未透传到 emit）")
        elif str(rd["model"]) != expected_model:
            errs.append(f"agent_reply.data.model={rd['model']!r} ≠ 活跃 config {expected_model!r}")
        if "reasoning_tokens" not in rd:
            errs.append("agent_reply 事件 data 缺 reasoning_tokens（定稿气泡无法显示推理 token）")
        # 校验 reasoning 文本落盘（推理模型：reasoning 非空字符串；非推理模型：无此 key 或空）
        if reasoning_tokens_values and reasoning_tokens_values[-1] > 0:
            if "reasoning" not in rd or not isinstance(rd.get("reasoning"), str) or not rd["reasoning"]:
                errs.append(
                    f"推理模型 reasoning_tokens>0 但 agent_reply.data 缺/空 reasoning 文本（val={rd.get('reasoning')!r}）"
                )
    else:
        errs.append("未收到协调者 agent_reply 事件（chat 回复未落地）")

    if reply_id:
        await asyncio.sleep(0.3)  # 让持久化写完
        persisted = await fetch_persisted_reply(reply_id)
        if persisted:
            pd = persisted.get("data") or {}
            print(f"[persist] 落盘 agent_reply.data.keys={list(pd.keys())} model={pd.get('model')!r} reasoning_tokens={pd.get('reasoning_tokens')!r}")
            if "model" not in pd:
                errs.append("落盘 agent_reply.data 缺 model（定稿气泡无法显示模型）")
            elif str(pd["model"]) != expected_model:
                errs.append(f"落盘 data.model={pd['model']!r} ≠ 活跃 config {expected_model!r}")
            if "reasoning_tokens" not in pd:
                errs.append("落盘 agent_reply.data 缺 reasoning_tokens（定稿气泡无法显示推理 token）")
            # 校验落盘 reasoning 文本（推理模型非空字符串——定稿气泡折叠区据此展开）
            if reasoning_tokens_values and reasoning_tokens_values[-1] > 0:
                rt = pd.get("reasoning")
                if not isinstance(rt, str) or not rt:
                    errs.append(f"推理模型落盘 data.reasoning 缺/空（val={rt!r}，定稿气泡无法展开推理）")
                else:
                    print(f"[persist] 落盘 reasoning 文本长度={len(rt)} 字（定稿折叠区可展开）")
        else:
            errs.append(f"未在 /api/messages 找到 reply_id={reply_id} 的持久化回复")

    # ---- 结果 ----
    if errs:
        print("\n[结果] FAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1

    rt_final = reasoning_tokens_values[-1] if reasoning_tokens_values else 0
    print("\n=== 结果: PASS ===")
    print(f"coordinator_stats({len(stats_events)} 条) + 持久化 agent_reply.data 均携带 "
          f"model={expected_model!r} + reasoning_tokens={rt_final}；"
          f"coordinator_reasoning 事件 {len(reasoning_events)} 条（拼接 {len(reasoning_full)} 字）。"
          f"状态行 model + 推理透传链路（后端 stream→stats/reasoning→WS→落盘→前端）正确。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

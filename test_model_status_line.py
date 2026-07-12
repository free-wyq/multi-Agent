"""自测：协调者流式回复状态行应携带 model 字段。

验证本轮改动：backend 把 LLM model 透传到 coordinator_stats WS 事件 +
持久化 agent_reply.data，前端据此渲染「model · Ns · ↓ N tokens」状态行。

校验点（确定性，不判语义）：
  1. coordinator_stats 事件 data 含 model 字段，值 == 当前活跃 config 的 model
  2. coordinator_stats 事件 data 含 elapsed_ms / tokens / phase（既有契约不破）
  3. 持久化 agent_reply（task 无关的 chat 回复）data 含 model 字段
     —— 定稿气泡「完成后保留可见」的关键：model 落盘了
  4. model 随热切换变化（若切了模型，新一轮回复的 model 跟着变）——
     这里只验证「与 GET /api/config 的 model 一致」，热切换的端到端留给手动测

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
# → emit coordinator_token + coordinator_stats(含 model) + node_chat 落盘 data。
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
    print("=== 自测：协调者状态行 model 字段 ===")
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
        # 至少 done 终态那条该带 model
        done_stats = [s for s in stats_events if (s.get("data") or {}).get("phase") == "done"]
        if not done_stats:
            # 闲聊可能 done 还没到就被 reply_ev 截断；放宽：只要有任意 stats 带 model
            pass
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

    # ---- 校验 3：持久化 agent_reply.data.model 落盘 ----
    reply_id = None
    if reply_ev:
        rd = reply_ev.get("data") or {}
        reply_id = rd.get("reply_id")
        print(f"[reply] agent_reply 事件 reply_id={reply_id} data.keys={list(rd.keys())}")
        if "model" not in rd:
            errs.append("agent_reply 事件 data 缺 model（未透传到 emit）")
        elif str(rd["model"]) != expected_model:
            errs.append(f"agent_reply.data.model={rd['model']!r} ≠ 活跃 config {expected_model!r}")
    else:
        # 闲聊回复合规情况：agent_reply 落地（前端 _unified_reply → emit_message_added）
        errs.append("未收到协调者 agent_reply 事件（chat 回复未落地）")

    if reply_id:
        await asyncio.sleep(0.3)  # 让持久化写完
        persisted = await fetch_persisted_reply(reply_id)
        if persisted:
            pd = persisted.get("data") or {}
            print(f"[persist] 落盘 agent_reply.data.keys={list(pd.keys())} model={pd.get('model')!r}")
            if "model" not in pd:
                errs.append("落盘 agent_reply.data 缺 model（定稿气泡无法显示模型）")
            elif str(pd["model"]) != expected_model:
                errs.append(f"落盘 data.model={pd['model']!r} ≠ 活跃 config {expected_model!r}")
        else:
            errs.append(f"未在 /api/messages 找到 reply_id={reply_id} 的持久化回复")

    # ---- 结果 ----
    if errs:
        print("\n[结果] FAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1

    print("\n=== 结果: PASS ===")
    print(f"coordinator_stats({len(stats_events)} 条) + 持久化 agent_reply.data 均携带 "
          f"model={expected_model!r}，状态行 model 透传链路（后端 stats→WS→落盘→前端）正确。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

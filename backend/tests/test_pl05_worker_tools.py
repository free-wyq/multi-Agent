"""PL-05 自测：验证 worker 自主调用 read_file/write_file/run_command 工具。

不依赖 pytest，直接 asyncio 跑。验证路径：POST 一条 @后端工程师 的群消息，
消息内容明确要求 worker 动手用文件工具干活。route_user_message 把消息路由到
worker 引擎 inbox（kind=agent_reply），worker brain 判定 execute → 推任务给
自己 → _run_worker_task → run_agent_loop（create_agent + bind_tools）→ LLM 自主
决定调用 write_file/run_command/read_file。抓 WS 事件流校验。

为何 @mention 直送 worker 而非走 coordinator 全链路：
  全链路不确定性高（coordinator 可能 chat 不 dispatch），且牵涉已驻留的 demo
  协调者状态。PL-05 范围是「worker 一旦干活，会不会自主调用文件/命令工具」，
  @mention 直送 worker 是最聚焦的验证方式。worker 仍需自主完成 brain→execute
  决策 + agentic loop 里的工具选择，体现「自主」。

校验项：
  1. WS 事件流出现 type==task_tool 且 name==write_file（worker 自主创建文件）
  2. WS 事件流出现 type==task_tool 且 name==run_command（worker 自主跑命令）
  3. WS 事件流出现 type==task_tool 且 name==read_file（worker 自主读文件）
  4. 磁盘交叉验证：workspace 下出现测试产物文件，且内容非空
  5. 任务以 task_complete(success=True) 收尾（worker 真正完成而非报错）
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import websockets

BASE = "http://localhost:8000"
WS_URL = "ws://localhost:8000/ws/bus/group_demo_1"
GROUP_ID = "group_demo_1"

# DATA_DIR 默认 ~/.local/share/multi-agent（与 backend/config.py 一致）
DATA_DIR = str(Path.home() / ".local" / "share" / "multi-agent")

# 产物文件名（worker 应通过 write_file 创建）。自测专属前缀避免与既有
# login.html / server.js 等历史产物冲突。
OUT_FILE = "pl05_selftest_note.md"

# @mention 指向后端工程师（agent_backend_1）。任务内容明确要求动手用工具，
# 促使 worker brain 判定 execute，并引导 agentic loop 调用三件套。
TASK_CONTENT = (
    f"@后端工程师 请直接动手执行以下自测任务，必须使用你的工具完成，不要只是口头描述：\n"
    f"1. 用 write_file 工具创建文件 {OUT_FILE}，内容写两行：第一行 'PL-05 tool selftest OK'，"
    f"第二行 'time=<TBD>'；\n"
    f"2. 用 run_command 工具执行 `ls -1` 列出工作区文件，确认 {OUT_FILE} 已创建；\n"
    f"3. 用 read_file 工具读回 {OUT_FILE} 确认内容正确；\n"
    f"4. 完成后用一句话回复结论。"
)

WS_TIMEOUT = 180.0  # LLM 多轮工具调用，给足时间


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def worker_status() -> str:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/status/{GROUP_ID}")
        for a in r.json():
            if a["id"] == "agent_backend_1":
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
    """连 WS 收事件直到抓到 task_complete/task_failed 或超时。返回全量事件。"""
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


def tool_calls(events: list[dict]) -> list[tuple[str, str]]:
    """从 task_tool 事件提取 (phase, name) 序列。"""
    calls: list[tuple[str, str]] = []
    for e in events:
        if e.get("type") == "task_tool":
            data = e.get("data") or {}
            phase = data.get("phase", "?")
            name = data.get("name", "?")
            calls.append((phase, name))
    return calls


def workspace_file(group_id: str, rel: str) -> Path:
    return Path(DATA_DIR) / "workspaces" / group_id / rel


async def main() -> int:
    print("=== PL-05 自测：worker 自主调用 read_file/write_file/run_command ===")
    if not await health_ok():
        print("[fatal] backend 不在线"); return 2
    print("[health] ok")

    # 等 worker 空闲（避免上一轮遗留 executing 导致任务进 backlog 延迟）
    for _ in range(30):
        st = await worker_status()
        if st == "idle":
            break
        print(f"[wait] worker 状态={st}，等待空闲...")
        await asyncio.sleep(2)
    else:
        print("[fatal] worker 一直 busy，放弃本次自测"); return 2
    print("[worker] agent_backend_1 idle")

    # 清理上一次可能残留的产物文件，确保本次 write_file 是真实新建
    out_path = workspace_file(GROUP_ID, OUT_FILE)
    if out_path.exists():
        out_path.unlink()
        print(f"[cleanup] 删除残留产物 {out_path.name}")

    # 并发：连 WS + 发消息（先连 WS 确保不漏首批事件）
    ws_task = asyncio.create_task(collect_until_done(WS_TIMEOUT))
    # 给 WS 一点时间连上
    await asyncio.sleep(0.5)
    sent = await send_message(TASK_CONTENT)
    print(f"[send] user message id={sent.get('id','')[:16]}...")

    events = await ws_task

    # 事件类型统计
    type_counts: dict[str, int] = {}
    for e in events:
        type_counts[e.get("type", "?")] = type_counts.get(e.get("type", "?"), 0) + 1
    print(f"[events] 收到 {len(events)} 条; 类型分布={type_counts}")

    calls = tool_calls(events)
    tools_used = {name for _, name in calls}
    print(f"[tools] 被调用工具={sorted(tools_used)}")
    print(f"[tools] 调用序列(phase,name)={calls}")

    # 校验 1-3：三件套
    have_write = "write_file" in tools_used
    have_run = "run_command" in tools_used
    have_read = "read_file" in tools_used
    print(f"[check] write_file={have_write} run_command={have_run} read_file={have_read}")

    # 校验 4：磁盘交叉验证
    disk_ok = out_path.exists() and out_path.stat().st_size > 0
    disk_content = ""
    if disk_ok:
        disk_content = out_path.read_text(encoding="utf-8", errors="replace")[:200]
    print(f"[check] 磁盘产物存在={disk_ok} (size={out_path.stat().st_size if disk_ok else 0})")
    if disk_ok:
        print(f"[disk] 内容预览: {disk_content!r}")

    # 校验 5：task_complete success
    complete_ev = next((e for e in events if e.get("type") == "task_complete"), None)
    failed_ev = next((e for e in events if e.get("type") == "task_failed"), None)
    success = complete_ev is not None and failed_ev is None
    print(f"[check] task_complete(success)={success}")

    errs = []
    if not have_write:
        errs.append("未捕获 write_file 工具调用")
    if not have_run:
        errs.append("未捕获 run_command 工具调用")
    if not have_read:
        errs.append("未捕获 read_file 工具调用")
    if not disk_ok:
        errs.append(f"磁盘产物 {OUT_FILE} 未生成或为空")
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
    print("worker 自主调用 read_file/write_file/run_command 三件套，"
          "磁盘产物交叉验证通过，任务成功收尾。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

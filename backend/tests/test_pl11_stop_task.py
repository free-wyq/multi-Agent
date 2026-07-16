"""PL-11 自测：执行中点停止 worker 中断回 idle。

不依赖 pytest，直接 asyncio 跑。沿用 PL-05/08/09 自测模式（httpx + websockets）。

PL-11 取消机制（前后端闭环）：
  前端 StopTaskButton → taskApi.stop → POST /api/tasks/{id}/stop
  后端 stop_task_by_id 扫引擎找 status==executing && current_task_id==task_id，
  request_cancel → cancel child _worker_task；下一个 await 抛 CancelledError。
  _handle_task 捕获（_cancel_requested=True 时吞掉，不杀引擎循环）→ _on_task_cancelled：
    complete_task(failed, "任务已停止") + emit_task_completed(success=False) → WS task_failed
    _publish_log("⏹ 任务已被用户停止") → WS task_log
    _reply("⏹ 任务已停止") → WS agent_reply
  → _reset_idle → emit agent_status(idle, current_task_id=None)

本自测验证全链路：
  1. 发一个多工具长任务给 @后端工程师 → worker 进入 executing（有 current_task_id）。
  2. 立即 POST /api/tasks/{task_id}/stop → 响应 executing=True（cancel 已发）。
  3. 轮询 HTTP /api/status → worker 回 idle（不靠 WS，真源交叉验证）。
  4. WS 事件流出现 task_failed 且 content 含「已停止」（证明走了 _on_task_cancelled 收尾，
     而非自然完成 task_complete success）。
  5. WS 出现 agent_status(idle, current_task_id=None)（引擎回 idle）。
  6. 任务确被中断而非自然完成：task_failed（非 task_complete success）。

为何用多工具长任务：
  停止窗口 = executing 持续时间。单轮 chat 任务 ~3-5s 太短，poll 到 executing 再 POST stop
  可能任务已自然完成（race lost，stop 返回 executing=False）。7 步多工具任务 ~20-35s 窗口，
  poll 0.5s 一次，POST stop 近即时，cancel 必落在下一个 await 点。若首跑 race lost（极少），
  重试一次（max 2 attempts）。

为何单用例：
  M12/PL-10 自测双用例叠驻留后端触发 exit 137 OOM。本测试 LLM 调用被 cancel 中断
  （不完整执行），内存占用低于 PL-09（180 事件全跑完）。单用例足够覆盖停止闭环。
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
WORKER_ID = "agent_backend_1"

# DATA_DIR 默认 ~/.local/share/multi-agent（与 backend/config.py 一致）
DATA_DIR = Path.home() / ".local" / "share" / "multi-agent"
WORKSPACE = DATA_DIR / "workspaces" / GROUP_ID

# 多工具长任务：7 步，每步强制调工具，~20-35s 执行窗口给停止留余量。
# 产物文件用自测专属前缀 pl11_stop_probe，避免与历史产物冲突。
TASK_CONTENT = (
    f"@后端工程师 请依次执行以下自测任务，每一步都必须真实调用对应工具（不要跳步、不要只口头描述）：\n"
    f"1. 用 write_file 工具创建文件 pl11_stop_probe.md，写入3行：标题 'PL-11 Stop Probe'、"
    f"分隔线、一行说明；\n"
    f"2. 用 run_command 工具执行 `ls -la` 列出工作区所有文件；\n"
    f"3. 用 read_file 工具读回 pl11_stop_probe.md 确认内容；\n"
    f"4. 用 write_file 工具创建文件 pl11_stop_probe2.md，写入不少于5行的详细执行日志；\n"
    f"5. 用 run_command 工具执行 `pwd` 和 `whoami` 两条命令；\n"
    f"6. 用 read_file 工具读回 pl11_stop_probe2.md；\n"
    f"7. 最后写一段不少于200字的执行总结。\n"
    f"注意：必须严格按顺序完成全部7步，每步都调用工具。"
)

EXEC_WAIT = 60.0       # 等 worker 进入 executing 的超时
STOP_TIMEOUT = 40.0    # POST stop 后等 worker 回 idle 的超时
MAX_ATTEMPTS = 2       # race lost 重试上限
WS_COLLECT_TIMEOUT = 50.0  # WS 事件收集总超时


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def worker_status() -> tuple[str, str | None]:
    """返回 (worker status, current_task_id or None)。"""
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/status/{GROUP_ID}")
        for a in r.json():
            if a["id"] == WORKER_ID:
                return a["status"], a.get("current_task_id")
    return "unknown", None


async def all_workers_idle() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/status/{GROUP_ID}")
        return all(a["status"] == "idle" for a in r.json())


async def wait_idle(timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await all_workers_idle():
            return True
        await asyncio.sleep(0.5)
    return False


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


async def stop_task(task_id: str, group_id: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{BASE}/api/tasks/{task_id}/stop",
            params={"groupId": group_id},
        )
        return r.json()


async def wait_executing(timeout: float) -> str | None:
    """轮询直到 worker executing，返回 current_task_id；超时返回 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st, tid = await worker_status()
        if st == "executing" and tid:
            return tid
        await asyncio.sleep(0.4)
    return None


async def collect_events_until_idle(timeout: float) -> list[dict]:
    """WS 收事件直到收到 worker 的 agent_status(idle) 或超时。返回事件列表。

  注意：不能用「HTTP idle 就 break」——发消息后到 worker 真正 claim 任务开始执行
  之间有启动延迟，此时 HTTP 仍 idle，若此时 recv 超时查 HTTP 见 idle 就 break 会
  提前退出，漏收全部执行期事件（首版 bug）。故只以「收到 agent_status(idle) 事件」
  为收尾信号（那是取消/完成后引擎 _reset_idle 真实推送的），辅以硬超时兜底。
  """
    events: list[dict] = []
    deadline = time.time() + timeout
    try:
        async with websockets.connect(WS_URL, ping_interval=None, ping_timeout=None, max_size=8 * 1024 * 1024) as ws:
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue  # 继续等，不查 HTTP（避免启动期误判 idle 提前退出）
                ev = json.loads(raw)
                events.append(ev)
                # 收到 worker 的 agent_status(idle) 即收尾
                if ev.get("type") == "agent_status":
                    dd = ev.get("data") or {}
                    if dd.get("status") == "idle" and ev.get("sender_id") == WORKER_ID:
                        # 再收 1s 尾巴（task_failed 通常紧随其后）
                        end = time.time() + 1.5
                        while time.time() < end:
                            try:
                                events.append(json.loads(await asyncio.wait_for(
                                    ws.recv(), timeout=max(0.1, end - time.time()))))
                            except asyncio.TimeoutError:
                                break
                        break
    except Exception as e:
        print(f"[ws] collect error: {e}")
    return events


async def cleanup_probe_files() -> None:
    """清理自测可能创建的探针文件（停止时可能已/未创建）。"""
    for name in ("pl11_stop_probe.md", "pl11_stop_probe2.md"):
        p = WORKSPACE / name
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass


async def attempt(n: int) -> tuple[bool, list[str]]:
    """单次停止尝试。返回 (success, errs)。"""
    errs: list[str] = []
    print(f"\n── attempt {n} ──")

    # 先确保 worker idle（避免上一轮残留）
    if not await wait_idle(30.0):
        errs.append("前置：worker 未在 30s 内回到 idle，无法开始停止测试")
        return False, errs

    # 开 WS 收事件 + 发任务（并行）
    ws_task = asyncio.create_task(collect_events_until_idle(WS_COLLECT_TIMEOUT))
    await asyncio.sleep(0.5)  # 让 WS 先连上
    print(f"[attempt {n}] 发送多工具长任务（7 步）...")
    await send_message(TASK_CONTENT)

    # 等 worker 进入 executing，抓 current_task_id
    print(f"[attempt {n}] 轮询等待 worker executing...")
    exec_tid = await wait_executing(EXEC_WAIT)
    if not exec_tid:
        errs.append("worker 未在超时内进入 executing（任务未触发执行）")
        # 取回 WS 事件供诊断
        events = await ws_task
        print(f"[attempt {n}] WS 事件类型分布: "
              f"{sorted({e.get('type') for e in events})}")
        return False, errs
    print(f"[attempt {n}] worker executing, current_task_id={exec_tid[:12]}...")

    # 立即 POST stop
    print(f"[attempt {n}] POST /api/tasks/{exec_tid[:12]}.../stop")
    resp = await stop_task(exec_tid, GROUP_ID)
    executing_flag = resp.get("executing")
    print(f"[attempt {n}] stop 响应: executing={executing_flag} "
          f"queued={resp.get('queued')} msg={resp.get('message')}")

    if not executing_flag:
        # race lost：任务在 poll→stop 之间自然完成了。等 WS 收完，重试。
        print(f"[attempt {n}] race lost（stop 时任务已不在 executing），等收尾后重试...")
        await ws_task
        return None, errs  # type: ignore[return-value]  # signal retry

    # cancel 已发，等 worker 回 idle（HTTP 真源）
    idle_ok = await wait_idle(STOP_TIMEOUT)
    print(f"[attempt {n}] worker 回 idle={idle_ok}")
    if not idle_ok:
        errs.append("POST stop 后 worker 未在超时内回 idle（取消未生效或卡住）")
        await ws_task
        return False, errs

    # 等 WS 事件收齐（collect_events_until_idle 已在 idle 时收尾，但确保 task）
    events = await ws_task
    types = sorted(e.get("type") for e in events)
    print(f"[attempt {n}] WS 收到 {len(events)} 条事件，类型: {types}")

    # 校验 4：出现 task_failed 且 content 含「已停止」
    task_failed_ev = next(
        (e for e in events
         if e.get("type") == "task_failed"
         and e.get("task_id") == exec_tid),
        None,
    )
    print(f"[attempt {n}] task_failed 事件={'找到' if task_failed_ev else '未找到'}"
          f" content={str(task_failed_ev.get('content') if task_failed_ev else '')[:40]}")
    if not task_failed_ev:
        errs.append("未收到 task_failed 事件（取消收尾 _on_task_cancelled 未执行）")
    elif "已停止" not in str(task_failed_ev.get("content") or ""):
        errs.append(f"task_failed content 不含「已停止」: "
                    f"{task_failed_ev.get('content')!r}")

    # 校验 5：agent_status(idle) 且 current_task_id 清空
    idle_ev = next(
        (e for e in events
         if e.get("type") == "agent_status"
         and e.get("sender_id") == WORKER_ID
         and (e.get("data") or {}).get("status") == "idle"),
        None,
    )
    print(f"[attempt {n}] agent_status(idle) 事件={'找到' if idle_ev else '未找到'}")
    if not idle_ev:
        errs.append("未收到 worker 的 agent_status(idle) 事件")

    # 校验 6：任务确被中断而非自然完成——同一 task_id 不能既有 task_complete(success)
    # 又有 task_failed。断言只有 task_failed（无 task_complete success）。
    complete_ev = next(
        (e for e in events
         if e.get("type") == "task_complete"
         and e.get("task_id") == exec_tid),
        None,
    )
    if complete_ev:
        errs.append("收到 task_complete(success)——任务自然完成了，未被停止中断（race lost）")
        print(f"[attempt {n}] 警告: 收到 task_complete（任务自然完成），stop 未生效")

    # 校验 3：HTTP 真源确认 idle（已在 wait_idle 确认，再显式断言）
    st, tid = await worker_status()
    print(f"[attempt {n}] HTTP /api/status 最终: worker status={st} current_task_id={tid}")
    if st != "idle":
        errs.append(f"HTTP 真源 worker 状态={st} 非 idle")
    if tid is not None:
        errs.append(f"HTTP 真源 worker current_task_id={tid} 未清空")

    return (len(errs) == 0), errs


async def main() -> int:
    print("=== PL-11 自测：执行中点停止 worker 中断回 idle ===")
    if not await health_ok():
        print("[fatal] backend 不在线"); return 2
    print("[health] ok")

    try:
        overall_ok = False
        last_errs: list[str] = []
        for n in range(1, MAX_ATTEMPTS + 1):
            result, errs = await attempt(n)
            if result is None:
                # race lost → 重试
                last_errs = errs
                print(f"[attempt {n}] race lost，将重试...")
                continue
            last_errs = errs
            if result:
                overall_ok = True
                break
            else:
                print(f"[attempt {n}] FAIL，重试...")
                continue

        # 清理探针文件
        await cleanup_probe_files()
        print("\n[cleanup] 探针文件已清理")

        if overall_ok:
            print("\n=== 结果: PASS ===")
            print("执行中点停止 worker 中断回 idle 全链路验证通过：")
            print("  · POST /api/tasks/{id}/stop 响应 executing=True（cancel 已发）；")
            print("  · worker 经 CancelledError 中断 → _on_task_cancelled 收尾 → _reset_idle；")
            print("  · WS task_failed(content 含「已停止」) 证明走了取消收尾路径；")
            print("  · WS agent_status(idle) + HTTP /api/status 双重确认 worker 回 idle、")
            print("    current_task_id 清空；")
            print("  · 无 task_complete(success)，证明任务确被中断而非自然完成。")
            return 0
        else:
            print("\n=== 结果: FAIL ===")
            for e in last_errs:
                print(f"  - {e}")
            print(f"\n（共尝试 {MAX_ATTEMPTS} 次，均未通过）")
            return 1
    except Exception:
        print("\n=== 结果: ERROR（异常）===")
        import traceback
        traceback.print_exc()
        await cleanup_probe_files()
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

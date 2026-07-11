"""PL-06 自测：验证挂载技能后 worker 自主按技能内容执行。

不依赖 pytest，直接 asyncio 跑。验证路径：
  1. POST /api/skills 创建一个带「签名规范」的技能——技能内容规定 worker 干活时
     必须把产物写成特定格式（带固定哨兵标记），这是技能专属约束，不在 worker
     默认 system_prompt 里。
  2. POST /api/skills/{id}/mount 挂载到后端工程师 agent_backend_1。
  3. @后端工程师 发任务，任务本身不规定产物格式（只说「做个健康检查报告」），
     产物格式只能来自技能。
  4. 抓 WS 事件流校验：
     a. task_log 出现「[技能] 已加载 N 个挂载技能」——证明 agent_executor
        resolve_skill_contents 成功并把技能注入 system_prompt；
     b. 磁盘产物文件出现，且内容含技能规定的哨兵标记——证明 worker 真的
        按技能内容执行，而非凭自己默认风格干活；
     c. task_complete(success=True) 收尾。

为何用「哨兵标记」而非语义判断：
  LLM 输出语义判断容易误判。「产物里必须含技能规定的固定字符串」是确定性校验——
  worker 不读技能内容就不可能写出这个标记，最强证据证明「按技能执行」。

收尾：卸载技能 + 删除技能 + 清理产物，避免污染其他自测。
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

DATA_DIR = str(Path.home() / ".local" / "share" / "multi-agent")

# 技能规定的哨兵标记——worker 不读技能就不可能写出这个字符串
SENTINEL = "PL06_SKILL_SIGNATURE_OK"
# 技能规定的产物文件名（技能专属，不与 PL-05 产物冲突）
OUT_FILE = "health_check_report.md"

# 技能内容：规定 worker 做健康检查报告时必须用特定格式 + 哨兵
SKILL_CONTENT = f"""# 健康检查报告技能

## 用途
当被要求做系统/服务健康检查报告时，按本技能规定的标准格式输出。

## 必须遵守的输出规范
1. 报告必须写入文件 {OUT_FILE}（用 write_file 工具）
2. 文件第一行必须是哨兵标记：{SENTINEL}
3. 报告分三个小节：## 服务状态 / ## 资源占用 / ## 结论
4. 每个小节写一行占位内容即可（本技能是自测用，不要求真实数据）
5. 写完后用 read_file 读回确认，再用一句话回复结论

## 注意
- 不要用别的文件名，必须叫 {OUT_FILE}
- 第一行哨兵标记必须原样写出，不可改写
"""

WS_TIMEOUT = 180.0


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


async def create_skill() -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{BASE}/api/skills",
            json={
                "name": "PL06健康检查报告规范",
                "description": "规定健康检查报告的输出格式与哨兵标记（PL-06自测用）",
                "content": SKILL_CONTENT,
                "source": "custom",
                "tags": ["pl06", "selftest"],
            },
        )
        return r.json()


async def mount_skill(skill_id: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{BASE}/api/skills/{skill_id}/mount", json={"agentId": WORKER_ID})
        return r.json()


async def unmount_skill(skill_id: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{BASE}/api/skills/{skill_id}/unmount", json={"agentId": WORKER_ID})
        return r.json()


async def delete_skill(skill_id: str) -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{BASE}/api/skills/{skill_id}")
        return r.json()


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


def workspace_file(group_id: str, rel: str) -> Path:
    return Path(DATA_DIR) / "workspaces" / group_id / rel


async def main() -> int:
    print("=== PL-06 自测：挂载技能后 worker 自主按技能内容执行 ===")
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

    skill_id = None
    out_path = workspace_file(GROUP_ID, OUT_FILE)
    # 清理上一次残留产物
    if out_path.exists():
        out_path.unlink()
        print(f"[cleanup] 删除残留产物 {out_path.name}")

    try:
        # 1. 创建技能
        skill = await create_skill()
        skill_id = skill.get("id")
        if not skill_id:
            print(f"[fatal] 创建技能失败: {skill}"); return 2
        print(f"[skill] 创建 id={skill_id} name={skill.get('name')}")

        # 2. 挂载到 worker
        mounted = await mount_skill(skill_id)
        ms = mounted.get("mounted_skills") if mounted else None
        if not ms or skill_id not in ms:
            print(f"[fatal] 挂载失败，mounted_skills={ms}"); return 2
        print(f"[mount] {WORKER_ID}.mounted_skills={ms}")

        # 3. 发任务——任务本身不规定产物格式，只说「做健康检查报告」
        #    产物格式只能从技能里来
        task_msg = (
            f"@后端工程师 请对当前服务做一次健康检查报告，直接动手用工具完成。"
            f"按你掌握的相关规范输出报告即可。"
        )

        ws_task = asyncio.create_task(collect_until_done(WS_TIMEOUT))
        await asyncio.sleep(0.5)
        sent = await send_message(task_msg)
        print(f"[send] user message id={sent.get('id','')[:16]}...")

        events = await ws_task

        # 事件统计
        type_counts: dict[str, int] = {}
        for e in events:
            type_counts[e.get("type", "?")] = type_counts.get(e.get("type", "?"), 0) + 1
        print(f"[events] 收到 {len(events)} 条; 类型分布={type_counts}")

        # 校验 a：task_log 出现技能加载日志
        skill_loaded = any(
            e.get("type") == "task_log"
            and "技能" in (e.get("content") or "")
            and "挂载技能" in (e.get("content") or "")
            for e in events
        )
        # 兜底：agent_executor 的日志文案是「[技能] 已加载 N 个挂载技能到上下文」
        if not skill_loaded:
            skill_loaded = any(
                e.get("type") == "task_log"
                and "已加载" in (e.get("content") or "")
                and "挂载技能" in (e.get("content") or "")
                for e in events
            )
        print(f"[check a] 技能加载日志出现={skill_loaded}")
        if not skill_loaded:
            # 打印所有 task_log 内容便于排查
            for e in events:
                if e.get("type") == "task_log":
                    print(f"   task_log: {e.get('content')}")

        # 校验 b：磁盘产物含哨兵标记
        disk_ok = out_path.exists() and out_path.stat().st_size > 0
        disk_content = ""
        has_sentinel = False
        if disk_ok:
            disk_content = out_path.read_text(encoding="utf-8", errors="replace")
            has_sentinel = SENTINEL in disk_content
        print(f"[check b] 磁盘产物存在={disk_ok} 含哨兵={has_sentinel} (size={out_path.stat().st_size if disk_ok else 0})")
        if disk_ok:
            print(f"[disk] 内容预览(前200字): {disk_content[:200]!r}")

        # 校验 c：task_complete success
        complete_ev = next((e for e in events if e.get("type") == "task_complete"), None)
        failed_ev = next((e for e in events if e.get("type") == "task_failed"), None)
        success = complete_ev is not None and failed_ev is None
        print(f"[check c] task_complete(success)={success}")

        errs = []
        if not skill_loaded:
            errs.append("未捕获技能加载日志（agent_executor 未注入技能到 system_prompt）")
        if not disk_ok:
            errs.append(f"磁盘产物 {OUT_FILE} 未生成或为空（worker 未按技能创建文件）")
        if disk_ok and not has_sentinel:
            errs.append(f"产物不含技能规定的哨兵标记 {SENTINEL!r}（worker 未按技能内容执行）")
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
        print(f"技能已挂载并被 worker 加载（task_log 技能加载日志出现），"
              f"产物 {OUT_FILE} 含技能规定的哨兵标记 {SENTINEL!r}，"
              f"证明 worker 自主按技能内容执行。")
        return 0

    finally:
        # 收尾：卸载 + 删技能 + 清产物，避免污染其他自测
        if skill_id:
            try:
                await unmount_skill(skill_id)
                print(f"[cleanup] 卸载技能 {skill_id}")
            except Exception as e:
                print(f"[cleanup] 卸载失败（非致命）: {e!r}")
            try:
                await delete_skill(skill_id)
                print(f"[cleanup] 删除技能 {skill_id}")
            except Exception as e:
                print(f"[cleanup] 删除技能失败（非致命）: {e!r}")
        if out_path.exists():
            out_path.unlink()
            print(f"[cleanup] 删除产物 {out_path.name}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

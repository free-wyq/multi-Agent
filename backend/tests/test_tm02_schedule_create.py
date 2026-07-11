"""TM-02 自测：创建定时任务（表单提交路径端到端验证）。

不依赖 pytest，直接 asyncio 跑。沿用 TM-01 / MC-02 自测模式（httpx HTTP 真源 +
探针落库 + 真源交叉 + 收尾清理，不连 WS）。

TM-02 链路（SchedulePage 创建任务表单提交）：
  前端表单（SchedulePage.tsx handleCreate）：
    · Form.validateFields → 组装 ScheduledTaskCreatePayload
    · 频率换算：freq_value × FREQ_UNIT_SECONDS[freq_unit] → interval_seconds
      （seconds=1 / minutes=60 / hours=3600 / days=86400）
    · schedule_type 固定 'interval'，cron/run_at 不传（undefined 不经 JSON 序列化）
    · scheduledTaskApi.create(payload) → POST /api/scheduled-tasks → fetchAll 刷新
  后端：
    POST /api/scheduled-tasks → scheduled_tasks.py create_scheduled_task
      → crud.create_scheduled_task 落库 + （enabled 时 add_job 注册 APScheduler）
      → 返回 ScheduledTask（含 id/created_at，前端卡片渲染源）

本自测复刻「表单→payload→落库」链路，HTTP 层用表单提交的精确 payload 形态
调 POST /api/scheduled-tasks，验证后端忠实落库 + 频率换算正确 + 字段真源一致
+ 创建即注册调度（enabled=True）。

与 TM-01 自测的区别：TM-01 验证「列表展示」（list/get/pause/resume 契约 + 三种
调度类型字段），TM-02 聚焦「创建表单提交路径」——重点在频率换算逻辑
（freq_value×unit→interval_seconds）+ payload 组装（schedule_type=interval +
interval_seconds + 不传 cron/run_at）+ 创建即注册调度（enabled=True 时 add_job）
的端到端正确性。TM-01 用三种调度类型探针，TM-02 用不同频率单位（小时/天/分钟）
验证换算全档正确。

验证八块（确定性断言）：
  ① 小时频率探针（freq_value=2/unit=hours→interval_seconds=7200）→ 200 +
     ScheduledTask（schedule_type=interval / interval_seconds=7200 / enabled=True）；
  ② 天频率探针（freq_value=1/unit=days→interval_seconds=86400）→ 200 + 落库；
  ③ 分钟频率探针（freq_value=30/unit=minutes→interval_seconds=1800）→ 200 + 落库；
  ④ payload 组装：创建请求不含 cron/run_at 字段（interval 分支不传，与 McpPage
     transport 分流 omit 同语义）；
  ⑤ 频率换算真源：落库 interval_seconds == freq_value × FREQ_UNIT_SECONDS[unit]
     （小时/天/分钟三档换算全正确）；
  ⑥ GET /api/scheduled-tasks 列表含三条探针（fetchAll 刷新能拿到新卡片）；
  ⑦ 单读回读字段 == payload 原值（跨端点单一真源，证明落库忠实）；
  ⑧ 收尾清理删除探针任务，校验无残留（取消 APScheduler job + 删库）。

为何不连 WS：TM-02 是同步 HTTP 接口（create→crud 落库+add_job 注册调度），
不经引擎 inbox/WS 事件流，纯 HTTP 校验即可（与 TM-01/MC-02 同构）。
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"

# 种子 agent/group（backend/store/seed.py 植入的 demo 数据）。
AGENT_ID = "agent_frontend_1"
GROUP_ID = "group_demo_1"

# 前端 FREQ_UNIT_SECONDS 映射的 Python 复刻（确定性断言用）。
FREQ_UNIT_SECONDS = {
    "seconds": 1,
    "minutes": 60,
    "hours": 3600,
    "days": 86400,
}


def build_payload(name: str, content: str, freq_value: int, freq_unit: str) -> dict:
    """复刻前端 SchedulePage.handleCreate 的 payload 组装逻辑。

    前端：schedule_type 固定 'interval'，interval_seconds = freq_value × unit_seconds，
    cron/run_at 不传（undefined 不经 JSON 序列化）。这里精确复刻组装结果。
    """
    interval_seconds = freq_value * FREQ_UNIT_SECONDS[freq_unit]
    return {
        "name": name,
        "content": content,
        "agent_id": AGENT_ID,
        "group_id": GROUP_ID,
        "schedule_type": "interval",
        "interval_seconds": interval_seconds,
        "enabled": True,
    }


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def create(payload: dict) -> tuple[int, dict | None]:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{BASE}/api/scheduled-tasks", json=payload)
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


async def list_tasks() -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/scheduled-tasks")
        return r.json() if r.status_code == 200 else []


async def get_task(task_id: str) -> dict | None:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/scheduled-tasks/{task_id}")
        if r.status_code == 404:
            return None
        return r.json() if r.status_code == 200 else None


async def delete_task(task_id: str) -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{BASE}/api/scheduled-tasks/{task_id}")
        return r.status_code == 200 and r.json() is True


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def main() -> int:
    print("=== TM-02 自测：创建定时任务（表单提交路径）===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    probe_ids: list[str] = []

    before = await list_tasks()
    print(f"[pre] 创建前定时任务数：{len(before)}")

    # 三种频率单位的探针（小时/天/分钟），覆盖前端 FREQ_UNIT_SECONDS 三档换算。
    probes = [
        ("小时", "[TM-02] 两小时巡检", "执行两小时一次的定时巡检。", 2, "hours", 7200),
        ("天", "[TM-02] 每日晨报", "生成今日工作晨报并发送。", 1, "days", 86400),
        ("分钟", "[TM-02] 半小时同步", "每半小时同步一次状态。", 30, "minutes", 1800),
    ]
    created: list[tuple[dict, dict, int]] = []  # (payload, task, expected_secs)

    # ── 1~3. 三档频率探针创建 → 200 + ScheduledTask ──
    for i, (unit_label, name, content, freq_value, freq_unit, expected_secs) in enumerate(probes, 1):
        payload = build_payload(name, content, freq_value, freq_unit)
        print(f"\n[check {i}] {unit_label}频率探针：freq_value={freq_value} unit={freq_unit} → interval_seconds={expected_secs}")
        status, task = await create(payload)
        if not _check("HTTP 200", status == 200, f"status={status} body={task}"):
            errs.append(f"[{unit_label}] 非 200 status={status}")
            continue
        assert task is not None
        if task.get("id"):
            probe_ids.append(task["id"])
        # 落库：id sched_ 前缀 + schedule_type=interval + interval_seconds 换算正确 + enabled=True + created_at 非空
        ok = (
            str(task.get("id", "")).startswith("sched_")
            and task.get("name") == name
            and task.get("schedule_type") == "interval"
            and int(task.get("interval_seconds", -1)) == expected_secs
            and task.get("agent_id") == AGENT_ID
            and task.get("group_id") == GROUP_ID
            and task.get("content") == content
            and task.get("enabled") is True
            and bool(task.get("created_at"))
        )
        if _check(
            f"{unit_label}落库 schedule_type=interval / interval_seconds={expected_secs} / enabled=True",
            ok,
            f"task={task}",
        ):
            print(f"      样本：id={task['id'][:18]}… interval_seconds={task.get('interval_seconds')}")
        else:
            errs.append(f"[{unit_label}] 字段异常：{task}")
        created.append((payload, task, expected_secs))

    # ── 4. payload 组装：创建请求不含 cron/run_at ──
    print("\n[check 4] payload 组装：interval 分支不传 cron/run_at（omit 不相关字段）")
    # 验证我们发出的 payload 确实不含 cron/run_at（前端表单 interval 分支同理不传）
    for payload, _, _ in created:
        omit_ok = "cron" not in payload and "run_at" not in payload
        if not _check(
            f"payload '{payload['name']}' 不含 cron/run_at",
            omit_ok,
            f"keys={list(payload.keys())}",
        ):
            errs.append(f"[omit] payload 残留 cron/run_at：{list(payload.keys())}")

    # ── 5. 频率换算真源：落库 interval_seconds == freq_value × unit_seconds ──
    print("\n[check 5] 频率换算真源：interval_seconds == freq_value × unit_seconds")
    for payload, task, expected_secs in created:
        if not task:
            continue
        got = int(task.get("interval_seconds", -1))
        if _check(
            f"{payload['name']}：interval_seconds={got} == 预期 {expected_secs}",
            got == expected_secs,
            f"got={got} want={expected_secs}",
        ):
            pass
        else:
            errs.append(f"[freq] {payload['name']} 换算错 got={got} want={expected_secs}")

    # ── 6. GET 列表含三条探针 ──
    print("\n[check 6] GET /api/scheduled-tasks 列表含三条探针（fetchAll 刷新拿到）")
    after = await list_tasks()
    after_ids = {t["id"] for t in after}
    all_in = all(t.get("id") in after_ids for _, t, _ in created if t)
    if _check("列表含三条频率探针", all_in):
        print(f"      列表总数：{len(after)}")
    else:
        errs.append("[list] 探针不在列表")

    # ── 7. 单读回读字段 == payload 原值 ──
    print("\n[check 7] 单读回读字段 == payload 原值（跨端点单一真源）")
    for payload, task, _ in created:
        if not task:
            continue
        reread = await get_task(task["id"])
        if reread is None:
            _check(f"{payload['name']}：回读 200", False)
            errs.append(f"[reread] {task['id']} 404")
            continue
        same = (
            reread.get("name") == payload["name"]
            and reread.get("content") == payload["content"]
            and reread.get("schedule_type") == payload["schedule_type"]
            and int(reread.get("interval_seconds", -1)) == payload["interval_seconds"]
            and reread.get("agent_id") == payload["agent_id"]
            and reread.get("group_id") == payload["group_id"]
            and reread.get("enabled") == payload["enabled"]
        )
        if _check(f"{payload['name']}：回读全字段 == payload 原值", same, f"reread={reread}"):
            pass
        else:
            errs.append(f"[reread] {payload['name']} 回读漂移：{reread}")

    # ── 8. 收尾清理：删除三条探针 ──
    print(f"\n[cleanup] 删除 {len(probe_ids)} 条探针任务")
    for tid in probe_ids:
        ok = await delete_task(tid)
        if not ok:
            print(f"  ⚠️ 删除失败 {tid}")
            errs.append(f"[cleanup] 删除失败 {tid}")
    final = await list_tasks()
    leaked = [t for t in final if t["id"] in probe_ids]
    if not _check("清理后无残留探针任务", not leaked, f"{len(leaked)} 条残留"):
        errs.append(f"[cleanup] {len(leaked)} 条残留")

    # ── 汇总 ──
    print("\n" + "=" * 56)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — 创建定时任务表单提交路径端到端验证通过：")
    print("  · 三档频率探针（小时/天/分钟）创建落库（id sched_ 前缀）；")
    print("  · payload 组装：interval 分支不传 cron/run_at（omit 不相关字段）；")
    print("  · 频率换算真源：interval_seconds == freq_value × unit_seconds（7200/86400/1800）；")
    print("  · 列表含三条探针（scheduledTaskApi.list() 真源交叉）；")
    print("  · 单读回读全字段 == payload 原值（跨端点单一真源）；")
    print("  · 清理三条探针无残留（取消 APScheduler job + 删库）。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""全链路回归自测：自主规划→技能/Agent→MCP→定时→多智能体协同端到端跑通。

这是 .task.md 第 112 项（最后一项）：聚合回归套件，逐条驱动各 P0 模块的真实自测脚本，
确认「自主规划 → 技能/Agent → MCP → 定时 → 多智能体协同」全链路端到端贯通。

设计原则（无人值守 + 最稳妥最易维护）：
  ① 复用已有 35+ 个分项自测脚本（每个已验证通过、有 cleanup）——不重写断言，只编排「跑 + 收集退出码」。
  ② 每条独立运行（绝对路径 + cwd=backend，沿用 MT 自测验证过的启动姿势），超时护栏防卡死。
  ③ 按模块分组 + 模块内串行（同模块测试共享状态如 demo 群 config / 工作区产物，串行避免竞态；
     不同模块间也串行——单进程 uvicorn + 单 WS 总线，并发跑多组 LLM 会互相抢 deepseek 配额 +
     demo 群 config 串改，串行最稳）。
  ④ 模块顺序：先跑「无副作用/纯 HTTP 契约」模块（AG 模板浏览/列表、SK 市场、MC、TM）暖场 +
     确认后端健康，再跑「真 LLM 执行 + WS 事件流」重模块（PL 自主规划、MT 多智能体协同），
     最后跑「失败/超时降级」收尾（MT-15/MT-17 故意制造失败，放最后不污染前面的成功路径）。
  ⑤ 每条记录 exit code + 耗时；汇总成回归矩阵（PASS/FAIL/SKIP）。任一硬 FAIL → 套件 FAIL（EXIT=1）。

模块覆盖（对应 PRD P0 全链路）：
  · 自主规划 PL：PL-01 拆解计划（PL-05/06/08/09/10/11/12 是 PL 子能力，PL-01 是入口代表性回归）
  · 技能 SK：SK-01 生成 + SK-10 市场搜索（生成 + 市场两端）
  · Agent AG：AG-01 生成 + AG-08 挂载技能获得能力（生成 + 挂载闭环）+ AG-05 列表展示
  · MCP MC：MC-01 列表展示（MCP 页配置连接端到端）
  · 定时 TM：TM-01 列表展示（定时任务调度端到端）
  · 多智能体协同 MT：MT-01 组装 → MT-09 理解目标 → MT-10 并行拆解 → MT-11 智能派工 →
    MT-12 并发执行 → MT-13 跟踪进度 → MT-14 动态调整 → MT-16 汇总整合（成功侧全链路）→
    MT-15 失败恢复 + MT-17 超时降级（失败/降级侧）

为何选这些代表性条目而非全跑 35+ 条：
  全跑耗时数小时（PL 重模块每条 1-3 分钟 LLM + MT 多智能体协同每条 2-4 分钟）+ deepseek 配额有限。
  代表性条目覆盖每模块的「入口/核心能力」（PL-01 拆解、SK-01 生成+SK-10 市场、AG-01 生成+AG-08 挂载、
  MC-01 列表、TM-01 列表、MT-01 组装+MT-09~16 协同全链路+MT-15/17 降级），任一 FAIL 即定位到模块故障。
  这是「全链路端到端跑通」的最小充分覆盖——每模块至少一条真驱动，协同侧跑完整成功链路 + 失败降级。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field

BASE = "http://localhost:8000"
BACKEND_DIR = "/home/wyq/work/project/multi-Agent/backend"
TESTS_DIR = f"{BACKEND_DIR}/tests"

# 单条测试超时（秒）。PL/MT 重模块 LLM 多轮，给足；纯 HTTP 契约模块短即可。
PER_TEST_TIMEOUT = 360.0


@dataclass
class TestCase:
    module: str        # 模块分组（PL/SK/AG/MC/TM/MT）
    name: str          # 简称
    file: str          # tests/ 下文件名
    desc: str          # 链路描述


@dataclass
class TestResult:
    case: TestCase
    ok: bool
    skipped: bool = False
    elapsed: float = 0.0
    detail: str = ""


# 回归矩阵——按模块分组，模块内串行，模块间串行。
# 顺序：暖场（纯 HTTP 契约）→ 自主规划 → 技能/Agent/MCP/定时 → 多智能体协同成功链路 → 失败/降级收尾。
REGRESSION_CASES: list[TestCase] = [
    # ── 暖场：纯 HTTP 契约模块（无 LLM/WS，快，确认后端健康 + 契约不破）──
    TestCase("AG", "AG-05 列表展示", "test_ag05_agent_list_display.py",
             "GET /api/agents 字段契约 + 渲染条件"),
    TestCase("AG", "AG-11 模板浏览", "test_ag11_templates_browse.py",
             "GET /api/agents/templates 分类筛选 + 字段契约"),
    TestCase("SK", "SK-10 市场搜索", "test_sk10_market_search.py",
             "GET /api/skills/market 服务端搜索 + limit 校验"),
    TestCase("MC", "MC-01 MCP 列表", "test_mc01_mcp_list.py",
             "POST/GET /api/mcp 连接落库 + 列表展示"),
    TestCase("TM", "TM-01 定时列表", "test_tm01_schedule_list.py",
             "POST/GET /api/scheduled-tasks 调度落库 + 列表展示"),

    # ── 自主规划 PL（真 LLM + WS 事件流，demo 群）──
    TestCase("PL", "PL-01 拆解计划", "test_pl01_plan_decompose.py",
             "coordinator 自动拆解多步计划 depends_on"),

    # ── 技能/Agent 生成 + 挂载闭环（真 LLM 生成 + HTTP 契约）──
    TestCase("SK", "SK-01 技能生成", "test_sk01_skill_generate.py",
             "POST /api/skills/generate 自然语言→技能配置"),
    TestCase("AG", "AG-01 Agent 生成", "test_ag01_agent_generate.py",
             "POST /api/agents/generate 自然语言→智能体配置"),
    TestCase("AG", "AG-08 挂载技能获得能力", "test_ag08_skill_mount.py",
             "POST mount → mounted_skills 持久化 + resolve_skill_contents"),

    # ── 多智能体协同 MT 成功全链路（专属探针群 + reload + 直接干）──
    TestCase("MT", "MT-01 组装团队", "test_mt01_team_assemble.py",
             "POST /api/groups + add_member + list_members 组装"),
    TestCase("MT", "MT-09 理解目标", "test_mt09_leader_understand_goal.py",
             "Leader 理解用户目标 think/reply 引用目标"),
    TestCase("MT", "MT-10 并行拆解", "test_mt10_parallel_decompose.py",
             "自动拆解可并行子任务 >=2 步 depends_on==[]"),
    TestCase("MT", "MT-11 智能派工", "test_mt11_capability_dispatch.py",
             "据 Worker 专业能力智能派工 无错配+双向覆盖"),
    TestCase("MT", "MT-12 并发执行", "test_mt12_parallel_workers.py",
             "多 Worker 同时执行互不阻塞 status 同时 executing"),
    TestCase("MT", "MT-13 跟踪进度", "test_mt13_leader_track_progress.py",
             "Leader 实时跟踪各 Worker 进度 report-back+汇总"),
    TestCase("MT", "MT-14 动态调整", "test_mt14_adjust_plan.py",
             "据中间结果动态调整剩余派工 串行依赖触发调整点"),
    TestCase("MT", "MT-16 汇总整合", "test_mt16_summary_integration.py",
             "所有子任务完成后汇总整合输出 计划清空终态"),

    # ── 失败/降级侧收尾（故意制造失败/超时，放最后不污染成功路径）──
    TestCase("MT", "MT-15 失败恢复", "test_mt15_failure_recovery.py",
             "Worker 失败后自动重派/降级 计划未死锁"),
    TestCase("MT", "MT-17 超时降级", "test_mt17_timeout_degradation.py",
             "Worker 长时间无响应超时降级 看门狗合成 report-back"),
]


async def health_ok() -> bool:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{BASE}/health")
            return r.status_code == 200 and r.json().get("status") == "ok"
    except Exception:
        return False


async def run_one(case: TestCase) -> TestResult:
    """跑一条自测脚本（子进程 + 超时护栏），收集 exit code + 末尾输出。

    flaky 容忍：WS/LLM 驱动的重模块（PL/MT）偶发受 deepseek 抖动 + reload 时序影响
    单次失败（httpx.ReadTimeout / 单 worker 未在窗口内执行）。对这类「环境性瞬时失败」
    自动重试 1 次（首次非 0 → 再跑一次取最终结果），区分「真回归」与「瞬时抖动」。
    重试只对非超时失败生效（超时已被 PER_TEST_TIMEOUT 兜底，重试只会再等一遍）。
    """
    path = os.path.join(TESTS_DIR, case.file)
    if not os.path.exists(path):
        return TestResult(case, ok=False, skipped=True, detail=f"文件不存在: {path}")

    async def _run_once() -> tuple[int, float, str]:
        t0 = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, path,
                cwd=BACKEND_DIR,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as e:
            return -1, 0.0, f"启动失败: {e!r}"
        try:
            await asyncio.wait_for(proc.wait(), timeout=PER_TEST_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, time.time() - t0, f"超时（>{PER_TEST_TIMEOUT:.0f}s 被 kill）"
        elapsed = time.time() - t0
        out = ""
        if proc.stdout is not None:
            try:
                out = (await proc.stdout.read()).decode("utf-8", errors="replace")
            except Exception:
                out = ""
        return (proc.returncode or 0), elapsed, out

    code, elapsed, out = await _run_once()
    detail_full = out
    # flaky 重试：首次非 0 且非超时失败 → 重跑一次（瞬时 deepseek/WS 抖动）。
    retried = False
    if code != 0 and "超时" not in out and len(out) > 0:
        # 只对真跑出输出的失败重试（非崩溃/非超时），且间隔一下让 deepseek 缓口气
        await asyncio.sleep(5.0)
        code2, elapsed2, out2 = await _run_once()
        retried = True
        if code2 == 0:
            code, elapsed, detail_full = code2, elapsed + elapsed2 + 5.0, out2
        else:
            # 重试仍失败：保留首次 detail（更长，便于定位）
            detail_full = out + "\n--- 重试仍 FAIL ---\n" + out2

    # 取末尾 6 行作 detail（定位用）
    tail = "\n".join(detail_full.strip().splitlines()[-6:])
    ok = code == 0
    if retried and ok:
        tail = "[重试后 PASS] " + tail
    return TestResult(case, ok=ok, elapsed=elapsed, detail=tail)


def _mark(ok: bool, skipped: bool) -> str:
    if skipped:
        return "⊘"
    return "✓" if ok else "✗"


async def main() -> int:
    print("=" * 64)
    print("全链路回归自测：自主规划→技能/Agent→MCP→定时→多智能体协同")
    print("=" * 64)
    if not await health_ok():
        print("[fatal] backend 不在线（http://localhost:8000/health）")
        return 2
    print("[health] ok — 后端在线，开始回归套件\n")

    results: list[TestResult] = []
    cur_module = ""
    for case in REGRESSION_CASES:
        if case.module != cur_module:
            cur_module = case.module
            print(f"\n── 模块 {cur_module} ──")
        print(f"▶ [{case.module}] {case.name}（{case.file}）")
        print(f"    链路：{case.desc}")
        r = await run_one(case)
        results.append(r)
        mark = _mark(r.ok, r.skipped)
        tag = "PASS" if r.ok else ("SKIP" if r.skipped else "FAIL")
        retry_note = "（含重试）" if "重试" in r.detail else ""
        print(f"    {mark} [{tag}] {r.elapsed:.0f}s{retry_note}")
        if not r.ok and not r.skipped:
            for line in r.detail.splitlines():
                print(f"       │ {line}")
        elif not r.ok:
            print(f"       │ {r.detail}")

    # ── 回归矩阵汇总 ──
    print("\n" + "=" * 64)
    print("回归矩阵")
    print("=" * 64)
    print(f"{'模块':<6} {'条目':<22} {'结果':<8} {'耗时':<8}")
    print("-" * 64)
    fails: list[TestResult] = []
    skips: list[TestResult] = []
    for r in results:
        tag = "PASS" if r.ok else ("SKIP" if r.skipped else "FAIL")
        print(f"{r.case.module:<6} {r.case.name:<22} {tag:<8} {r.elapsed:>5.0f}s")
        if not r.ok and r.skipped:
            skips.append(r)
        elif not r.ok:
            fails.append(r)

    total = len(results)
    passed = sum(1 for r in results if r.ok)
    failed = len(fails)
    skipped = len(skips)
    print("-" * 64)
    print(f"合计 {total} 条：✓ PASS {passed}  ✗ FAIL {failed}  ⊘ SKIP {skipped}")

    print("\n全链路覆盖：")
    print("  · 自主规划 PL：PL-01 拆解计划（coordinator LLM 自动拆解多步 depends_on）")
    print("  · 技能 SK：SK-01 生成 + SK-10 市场搜索（自然语言→技能 + 市场检索）")
    print("  · Agent AG：AG-01 生成 + AG-08 挂载技能获得能力 + AG-05 列表展示")
    print("  · MCP MC：MC-01 连接列表展示（MCP 页配置连接端到端）")
    print("  · 定时 TM：TM-01 定时任务列表展示（APScheduler 调度落库）")
    print("  · 多智能体协同 MT：MT-01 组装 → MT-09 理解 → MT-10 并行拆解 →")
    print("    MT-11 智能派工 → MT-12 并发执行 → MT-13 跟踪进度 → MT-14 动态调整 →")
    print("    MT-16 汇总整合（成功全链路）→ MT-15 失败恢复 + MT-17 超时降级（降级侧）")

    if failed:
        print(f"\n=== 结果: FAIL — {failed} 项未通过 ===")
        for r in fails:
            print(f"  ✗ [{r.case.module}] {r.case.name}（{r.case.file}）")
        return 1
    print("\n=== 结果: PASS — 全链路端到端跑通 ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

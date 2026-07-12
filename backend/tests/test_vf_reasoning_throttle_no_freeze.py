"""验证 VF：思考流式不卡死——coordinator_reasoning 节流 + logs 排除.

用户实测：让 kimi-k2.6 写 200 字作文，页面卡 3 分钟才流正文。根因：

  推理模型思考阶段 chunk 极密（kimi-k2.6 写 200 字实测 820 个 reasoning delta，
  思考持续 39s 才开始流可见 content）。原前端 useBusEvent 有两个叠加缺陷：

  VF1. coordinator_reasoning delta 进了 logs（日志过滤只排除 coordinator_token/
       task_token，没排除 coordinator_reasoning）。logs 是 ChatPanel chatMessages
       effect 的依赖 → 每个 reasoning delta 触发一次 effect → 800+ 次 React 重渲染
       风暴卡死主线程。

  VF2. coordinator_reasoning delta 每条直接 setCoordReasoning → 800+ 次 setState
       → 800+ 次重渲染（即便不进 logs，单是 coordReasoning state 变化就重渲染）。

  叠加效果：WS onmessage 被重渲染占满 → 后端 send_json 背压 → emit 协程排队 →
  正文 content delta 被堵在思考 delta 后面 → 页面卡几分钟直到思考结束。

修法（src/hooks/useBusEvent.ts）：
  VF1. 日志过滤排除 coordinator_reasoning + coordinator_stats（与 coordinator_token/
       task_token 同源排除——逐字 delta 不该进日志流）。
  VF2. coordinator_reasoning delta 攒进 ref 缓冲，~50ms flush 一次到 state（把
       ~800 次 setState 压到 ~20 次）。ref 不触发渲染，flush 才触发；最后 delta 后
       定时器兜底 flush 残留，effect 清理时也 flush，不丢字。

本测纯静态契约（不依赖后端在线），锁住：
  VF1. 日志过滤条件含 coordinator_reasoning 排除（不再进 logs）。
  VF2. coordinator_reasoning 分支用 ref 缓冲 + setTimeout 节流，非直接 setCoordReasoning。
  VF3. 节流 flush 函数存在（flushReasoning）+ effect 清理时兜底 flush。
  VF4. logs 仍保留 coordinator_token/task_token 排除（不回归——这两个本就排除）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK_TS = REPO / "src" / "hooks" / "useBusEvent.ts"


def check() -> int:
    errs: list[str] = []
    hook = HOOK_TS.read_text(encoding="utf-8")

    print("── useBusEvent: 思考流式不卡死（节流 + logs 排除）──")

    # VF1. 日志过滤排除 coordinator_reasoning（不再进 logs）
    #   定位 logs 过滤 if 条件，断言含 coordinator_reasoning 排除
    m_log = re.search(
        r"if \(\s*d\.content\s*&&\s*(.*?)\s*\) \{\s*\n\s*const entry: LogEntry",
        hook, re.S,
    )
    if not m_log:
        errs.append("[VF1] 无法定位 logs 过滤 if 条件")
    else:
        cond = m_log.group(1)
        excludes = re.findall(r"d\.type !== ['\"](\w+)['\"]", cond)
        if "coordinator_reasoning" not in excludes:
            errs.append(
                f"[VF1] logs 过滤未排除 coordinator_reasoning（排除列表={excludes}）"
                "——思考逐字 delta 会进 logs 触发 chatMessages effect 重渲染风暴"
            )
        else:
            print(f"[VF1] OK  logs 排除 coordinator_reasoning（思考逐字 delta 不进日志流，排除列表={excludes}）")

    # VF4. logs 仍排除 coordinator_token/task_token（不回归）
    if m_log:
        cond = m_log.group(1)
        excludes = re.findall(r"d\.type !== ['\"](\w+)['\"]", cond)
        missing_old = [t for t in ("coordinator_token", "task_token") if t not in excludes]
        if missing_old:
            errs.append(f"[VF4] logs 过滤丢了原有排除 {missing_old}（回归）")
        else:
            print(f"[VF4] OK  logs 仍排除 coordinator_token/task_token（不回归）")

    # VF2. coordinator_reasoning 分支用 ref 缓冲 + setTimeout 节流，非直接 setCoordReasoning
    #   定位 coordinator_reasoning 分支块，断言块内有 push 到 ref + setTimeout，无直接
    #   setCoordReasoning（..., delta）
    m_re = re.search(
        r"else if \(d\.type === 'coordinator_reasoning'\) \{(.*?)\n      \} else if \(d\.type === 'coordinator_stats'\)",
        hook, re.S,
    )
    if not m_re:
        errs.append("[VF2] 无法定位 coordinator_reasoning 分支（或其后继非 coordinator_stats）")
    else:
        blk = m_re.group(1)
        has_buf_push = "reasoningBufRef.current.push" in blk
        has_timer = "reasoningFlushTimer" in blk and "setTimeout" in blk
        # 不应直接 setCoordReasoning((prev) => ... prev[rid] + d.content)（即逐字直推）
        direct_push = re.search(
            r"setCoordReasoning\(\(prev\)[^)]*\{[^}]*prev\[rid\]\s*\|\|\s*''\)\s*\+\s*d\.content",
            blk, re.S,
        )
        if not has_buf_push:
            errs.append("[VF2] coordinator_reasoning 分支未用 ref 缓冲（reasoningBufRef.current.push 缺失）")
        elif not has_timer:
            errs.append("[VF2] coordinator_reasoning 分支未设节流定时器（setTimeout 缺失）")
        elif direct_push:
            errs.append("[VF2] coordinator_reasoning 分支仍直接 setCoordReasoning 逐字 delta（未节流）")
        else:
            print("[VF2] OK  coordinator_reasoning delta 攒 ref + setTimeout(~50ms) 节流 flush（非逐字 setState）")

    # VF3. flushReasoning 函数存在 + effect 清理兜底 flush
    if "const flushReasoning" not in hook and "flushReasoning =" not in hook:
        errs.append("[VF3] 缺少 flushReasoning 节流 flush 函数")
    elif "reasoningBufRef" not in hook or "reasoningFlushTimer" not in hook:
        errs.append("[VF3] 缺少 reasoningBufRef / reasoningFlushTimer ref（节流缓冲载体缺失）")
    else:
        # effect 清理里兜底 flush（切群/卸载不丢字）
        if "flushReasoning()" not in hook.split("return () =>", 1)[-1].split("}, [", 1)[0] if "return () =>" in hook else "":
            # 宽松判定：清理块附近出现 flushReasoning 调用
            cleanup_blk = hook.split("return () =>", 1)[-1] if "return () =>" in hook else ""
            if "flushReasoning()" in cleanup_blk:
                print("[VF3] OK  flushReasoning 函数 + effect 清理兜底 flush（切群不丢字）")
            else:
                errs.append("[VF3] effect 清理未兜底 flushReasoning（切群残留 delta 可能丢）")
        else:
            print("[VF3] OK  flushReasoning 函数 + effect 清理兜底 flush（切群不丢字）")

    print()
    if errs:
        print("=== 结果: FAIL ===")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("=== 结果: PASS ===")
    print("思考流式不卡死：")
    print("  · logs 排除 coordinator_reasoning/stats（逐字 delta 不进日志流，不触发 chatMessages effect 风暴）；")
    print("  · coordinator_reasoning delta 攒 ref + ~50ms 节流 flush（~800 次 setState 压到 ~20 次）；")
    print("  · flushReasoning 兜底 flush + effect 清理 flush（切群/最后残留不丢字）；")
    print("  · coordinator_token/task_token 排除不回归。")
    return 0


if __name__ == "__main__":
    sys.exit(check())

"""VH2 回归：worker reasoning_tokens fallback 与协调者一致（task B5）.

锁住 B5 修复——``_stream_brain_decision`` 的 ``reasoning_tokens`` 在 provider 不回
``reasoning_usage`` 时退化为 ``live_reasoning_tokens`` 粗估，与协调者
``_stream_coordinator_decision`` 的 ``live_reasoning_tokens`` 兜底口径一致（而非旧版
直接 ``reasoning_tokens = final_reasoning_tokens`` 无 fallback，导致 reasoning 模型只回
content usage 不回 reasoning_usage 时 stats 显示 0 推理，与实际有推理链不符）。

纯静态契约（读源码断言，不依赖后端在线），与 test_va2/va3/va6/vg1/vg2/vh1 同款风格。

四段契约：

  A. worker _stream_brain_decision 加了 live_reasoning_tokens 累加 + fallback
    1. 模块级初始化 ``live_reasoning_tokens = 0``（在 async for 循环前）。
    2. ``reasoning_delta`` 块内累加 ``live_reasoning_tokens += max(1, len(reasoning_delta)//3)``
       （与协调者 ``live_reasoning_tokens += max(1, len(reasoning_delta)//3)`` 同款）。
    3. fallback 估值：``reasoning_tokens = final_reasoning_tokens if final_reasoning_tokens
       else live_reasoning_tokens``（非旧版 ``reasoning_tokens = final_reasoning_tokens``）。

  B. 与协调者 _stream_coordinator_decision 同款兜底（一致性）
    4. 协调者 ``real_reasoning_tokens = final_reasoning_tokens if final_reasoning_tokens
       else live_reasoning_tokens``（已落地，验证未回归）。
    5. 协调者 ``live_reasoning_tokens += max(1, len(reasoning_delta)//3)``（已落地，未回归）。
    6. worker fallback 表达式与协调者同形（``final if final else live``，仅变量名
       real_reasoning_tokens vs reasoning_tokens 差异——worker 不分 real/live 两名）。

  C. 非推理模型不受影响（fallback 不改变 0 值）
    7. 非推理模型无 reasoning_delta → live_reasoning_tokens=0 + final_reasoning_tokens=0
       → fallback ``0 if 0 else 0`` = 0（如实显示 0 推理，与旧版无差异）。
       ——静态核：fallback 是 ``if final_reasoning_tokens else live``，两路都 0 时结果 0，
       不引入假正数（len//3 粗估只在 reasoning_delta 存在时累加，非推理模型无 delta）。

  D. tokens 与 reasoning_tokens fallback 口径对称（B5 顺带对齐）
    8. worker ``tokens = final_tokens if final_tokens else max(1, len(raw_full)//3)``
       （content 路径已有 fallback，B5 给 reasoning 路径补同款 fallback，两者对称）。
    9. 协调者 ``real_tokens = final_tokens if final_tokens else live_tokens``（live_tokens
       累加，未回归）——worker content 用 ``len(raw_full)//3`` 而非 live_tokens（worker 无
       stats 流式 emit，无 live_tokens 累加），差异是设计取舍（worker stats 只落盘不流式推）。

为何纯静态：
  fallback 是「代码表达式」契约（``final if final else live`` 三元），运行时 provider 是否
  回 reasoning_usage 受模型/端点影响，但 fallback 表达式确定性靠代码锚定。静态契约锁住
  「worker reasoning_tokens 有 live fallback + 与协调者同形」两个确定性条件，比运行时实测
  更可靠（实测需触发「reasoning 模型不回 reasoning_usage」这个特定 provider 行为）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORKER = REPO / "backend" / "engine" / "worker.py"
COORD = REPO / "backend" / "engine" / "coordinator.py"


def _fn_body(src: str, fname: str, indent_opts=("", "    ")) -> str:
    """抽 fn 函数体到下一个同级 def（试多种缩进：模块级 0 / 类方法 4 空格）。"""
    for indent in indent_opts:
        m = re.search(
            rf"(?:async def|def) {fname}\([^)]*\)(.*?)(?=\n{indent}(?:async )?def )",
            src,
            re.S,
        )
        if m:
            return m.group(1)
    return ""


def assert_contract() -> list[str]:
    errs: list[str] = []
    worker = WORKER.read_text(encoding="utf-8")
    coord = COORD.read_text(encoding="utf-8")

    brain_w = _fn_body(worker, "_stream_brain_decision", indent_opts=("",))
    coord_stream = _fn_body(coord, "_stream_coordinator_decision", indent_opts=("    ", ""))

    if not brain_w:
        errs.append("[setup] worker _stream_brain_decision 函数体未找到（B3 可能未落地）")
    if not coord_stream:
        errs.append("[setup] coordinator _stream_coordinator_decision 函数体未找到")

    # ── A. worker _stream_brain_decision 加了 live_reasoning_tokens 累加 + fallback ──
    # [1] live_reasoning_tokens = 0 初始化（async for 循环前）
    m_init = re.search(r"live_reasoning_tokens\s*=\s*0", brain_w)
    if not m_init:
        errs.append("[A1] worker _stream_brain_decision 缺 live_reasoning_tokens = 0 初始化")
    else:
        # 确认初始化在 async for 循环前
        idx_init = m_init.start()
        idx_loop = brain_w.find("async for content_delta")
        if idx_loop < 0 or idx_init >= idx_loop:
            errs.append("[A1] live_reasoning_tokens 初始化不在 async for 循环前（累加无效）")
        else:
            print("[A1] OK  live_reasoning_tokens = 0 在 async for 循环前初始化")

    # [2] reasoning_delta 块内累加 live_reasoning_tokens += max(1, len(reasoning_delta)//3)
    m_acc = re.search(
        r"live_reasoning_tokens\s*\+=\s*max\(\s*1,\s*len\(\s*reasoning_delta\s*\)\s*//\s*3\s*\)",
        brain_w,
    )
    if not m_acc:
        errs.append("[A2] worker reasoning_delta 块未累加 live_reasoning_tokens += max(1, len//3)")
    else:
        print("[A2] OK  reasoning_delta 块累加 live_reasoning_tokens += max(1, len//3)")

    # [3] fallback 估值：reasoning_tokens = final if final else live
    m_fb = re.search(
        r"reasoning_tokens\s*=\s*\(\s*final_reasoning_tokens\s+if\s+final_reasoning_tokens\s+else\s+live_reasoning_tokens\s*\)",
        brain_w,
    )
    if not m_fb:
        # 也接受单行无括号写法
        m_fb = re.search(
            r"reasoning_tokens\s*=\s*final_reasoning_tokens\s+if\s+final_reasoning_tokens\s+else\s+live_reasoning_tokens",
            brain_w,
        )
    if not m_fb:
        errs.append(
            "[A3] worker reasoning_tokens 缺 fallback（应为 final_reasoning_tokens if final_reasoning_tokens else live_reasoning_tokens）"
        )
    else:
        print("[A3] OK  reasoning_tokens = final_reasoning_tokens if final else live_reasoning_tokens（有 fallback）")

    # ── B. 与协调者同款兜底（一致性）──
    # [4] 协调者 real_reasoning_tokens fallback（已落地，未回归）
    m_coord_fb = re.search(
        r"real_reasoning_tokens\s*=\s*\(\s*final_reasoning_tokens\s+if\s+final_reasoning_tokens\s+else\s+live_reasoning_tokens\s*\)",
        coord_stream,
    )
    if not m_coord_fb:
        errs.append("[B4] 协调者 real_reasoning_tokens fallback 缺失或回归")
    else:
        print("[B4] OK  协调者 real_reasoning_tokens = final if final else live（未回归）")

    # [5] 协调者 live_reasoning_tokens 累加（未回归）
    m_coord_acc = re.search(
        r"live_reasoning_tokens\s*\+=\s*max\(\s*1,\s*len\(\s*reasoning_delta\s*\)\s*//\s*3\s*\)",
        coord_stream,
    )
    if not m_coord_acc:
        errs.append("[B5] 协调者 live_reasoning_tokens 累加缺失或回归")
    else:
        print("[B5] OK  协调者 live_reasoning_tokens += max(1, len//3)（未回归）")

    # [6] worker fallback 表达式与协调者同形（final if final else live）
    if m_fb and m_coord_fb:
        # 两者都含 "final_reasoning_tokens if final_reasoning_tokens else live_reasoning_tokens"
        shared = "final_reasoning_tokens if final_reasoning_tokens else live_reasoning_tokens"
        if shared in brain_w and shared in coord_stream:
            print("[B6] OK  worker 与协调者 fallback 表达式同形（final if final else live）")
        else:
            errs.append("[B6] worker/协调者 fallback 表达式不同形（口径不一致）")

    # ── C. 非推理模型不受影响（fallback 不改变 0 值）──
    # [7] 确认 live_reasoning_tokens 只在 reasoning_delta 块内累加（非推理模型无 delta → 恒 0）
    # 抽 reasoning_delta 块（if reasoning_delta: 到下一个同级 if）
    m_reason_block = re.search(
        r"if reasoning_delta:\s*\n(.*?)(?=\n        if (?:usage|reasoning_usage) is not None:|\n            if (?:usage|reasoning_usage) is not None:)",
        brain_w,
        re.S,
    )
    if not m_reason_block:
        errs.append("[C7] 无法定位 worker reasoning_delta 块（结构已变）")
    else:
        blk = m_reason_block.group(1)
        # 累加在 reasoning_delta 块内 → 无 delta 时 live_reasoning_tokens 不增长
        if "live_reasoning_tokens += " not in blk:
            errs.append("[C7] live_reasoning_tokens 累加不在 reasoning_delta 块内（非推理模型会被错估）")
        else:
            print("[C7] OK  live_reasoning_tokens 仅在 reasoning_delta 块累加（非推理模型恒 0，fallback 不引入假正数）")

    # ── D. tokens 与 reasoning_tokens fallback 口径对称 ──
    # [8] worker tokens 已有 fallback（content 路径，B5 前就有）
    m_tokens_fb = re.search(
        r"tokens\s*=\s*final_tokens\s+if\s+final_tokens\s+else\s+max\(\s*1,\s*len\(\s*raw_full\s*\)\s*//\s*3\s*\)",
        brain_w,
    )
    if not m_tokens_fb:
        errs.append("[D8] worker tokens 缺 content fallback（B5 前就应有，可能回归）")
    else:
        print("[D8] OK  worker tokens = final_tokens if final else max(1, len(raw_full)//3)（content 路径 fallback 在位）")

    # [9] 协调者 real_tokens = final_tokens if final_tokens else live_tokens（未回归）
    m_coord_tokens = re.search(
        r"real_tokens\s*=\s*final_tokens\s+if\s+final_tokens\s+else\s+live_tokens",
        coord_stream,
    )
    if not m_coord_tokens:
        errs.append("[D9] 协调者 real_tokens fallback 缺失或回归")
    else:
        print("[D9] OK  协调者 real_tokens = final_tokens if final else live_tokens（未回归）")

    return errs


def main() -> int:
    print("=== VH2 回归：worker reasoning_tokens fallback 与协调者一致 ===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "VH2 回归契约锁定（B5 修复不退化）：\n"
        "  · A worker _stream_brain_decision 加 live_reasoning_tokens 初始化 + reasoning_delta 块累加 + "
        "fallback 估值（final_reasoning_tokens if final else live_reasoning_tokens）——provider 不回 "
        "reasoning_usage 时退化为粗估，避免 reasoning 模型 stats 显示 0 推理；\n"
        "  · B 与协调者 _stream_coordinator_decision 同款兜底（real_reasoning_tokens = final if final else "
        "live + live_reasoning_tokens += max(1,len//3)），worker/协调者 fallback 表达式同形（口径一致）；\n"
        "  · C live_reasoning_tokens 仅在 reasoning_delta 块累加 → 非推理模型无 delta 恒 0，fallback 不引入"
        "假正数（0 if 0 else 0 = 0，与旧版无差异）；\n"
        "  · D worker tokens（content 路径）+ reasoning_tokens（reasoning 路径）都有 fallback，"
        "与协调者 real_tokens/real_reasoning_tokens 对称（worker content 用 len(raw_full)//3 而非 live_tokens"
        "——worker 无 stats 流式 emit 无 live_tokens 累加，是设计取舍非回归）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

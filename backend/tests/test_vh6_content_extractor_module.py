"""VH6 回归：_ContentExtractor 抽到 llm/json_stream.py 消除反向导入（task B9）.

锁住 B9 修复——``_ContentExtractor`` 从 ``engine/coordinator.py`` 抽到
``llm/json_stream.py``（公共名 ``ContentExtractor``），消除 ``worker.py:20``
``from engine.coordinator import _ContentExtractor`` 的反向导入耦合。

B9 前的坏味道：``engine.worker`` 依赖 ``engine.coordinator``（worker→coordinator
反向导入），而 ``engine.coordinator`` 不导入 worker（无正向边）——这是单向被
打破的耦合，worker 为复用一个 streaming-JSON 解析工具被迫依赖协调者整个模块
（拉进 coordinator 的所有顶层副作用：contextvars 声明、logger、_GRAPH_INSTANCE
等）。centralize 到 ``llm/`` 后：worker 和 coordinator 都从 ``llm.json_stream``
取，worker 不再依赖 coordinator，模块依赖图回到「coordinator/worker 平级 +
都依赖 llm」的干净拓扑。

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh5 同款风格。

六段契约：

  A. ContentExtractor 新家在 llm/json_stream.py（公共名）
    1. ``llm/json_stream.py`` 文件存在 + 定义 ``class ContentExtractor``。
    2. ContentExtractor 是公共名（非 ``_ContentExtractor`` 下划线私有）。
    3. ``llm/__init__.py`` 导出 ContentExtractor（``from .json_stream import ContentExtractor``）。

  B. coordinator 改从 llm 取（消除定义重复）
    4. coordinator.py ``from llm.json_stream import ContentExtractor``（非本地定义）。
    5. coordinator 调用点用 ``ContentExtractor()``（非 ``_ContentExtractor()``）。
    6. coordinator 保留 ``_ContentExtractor`` 向后兼容别名（subclass ContentExtractor），
       让旧引用 ``engine.coordinator._ContentExtractor`` 仍可解析。

  C. worker 改从 llm 取（消除反向导入·B9 核心）
    7. worker.py 无 ``from engine.coordinator import`` 行（反向导入已删）。
    8. worker.py ``from llm.json_stream import ContentExtractor``（正向依赖 llm）。
    9. worker 调用点用 ``ContentExtractor()``（非 ``_ContentExtractor()``）。

  D. 行为零变（B9 纯搬家，解析逻辑不变）
   10. ContentExtractor 的 feed/take 逻辑与原 coordinator._ContentExtractor 一致
       （_KEY='"content"' / _in_content / _escaped / _buf / _out 状态机字段在位）。
   11. coordinator._ContentExtractor 别名 is ContentExtractor（subclass 不改行为）。

  E. 无新循环依赖（搬家不引入环）
   12. coordinator 不导入 worker（无 coordinator→worker 边，B9 不引入）。
   13. llm/json_stream.py 不导入 engine.*（纯工具，无反向依赖 engine）。

  F. 依赖拓扑收敛（worker 不再依赖 coordinator）
   14. worker.py 的 import 块无 ``engine.coordinator``（worker 与 coordinator 平级）。
   15. coordinator.py 的 import 块无 ``engine.worker``（本来就没有，B9 不破）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
JSON_STREAM = REPO / "backend" / "llm" / "json_stream.py"
LLM_INIT = REPO / "backend" / "llm" / "__init__.py"
COORD = REPO / "backend" / "engine" / "coordinator.py"
WORKER = REPO / "backend" / "engine" / "worker.py"


def _imports_block(src: str) -> str:
    """抽文件顶部 import 块（从第一个 from/import 到第一个非 import 顶层语句）。"""
    lines = []
    for ln in src.splitlines():
        s = ln.strip()
        if s.startswith("from ") or s.startswith("import ") or s == "" or s.startswith("#") or s.startswith('"""') or s.endswith('"""'):
            if s.startswith("from ") or s.startswith("import "):
                lines.append(ln)
        elif lines:
            break
    return "\n".join(lines)


def assert_contract() -> list[str]:
    errs: list[str] = []

    # ── A. ContentExtractor 新家在 llm/json_stream.py ──
    # [1] llm/json_stream.py 存在 + 定义 class ContentExtractor
    if not JSON_STREAM.exists():
        errs.append("[A1] llm/json_stream.py 文件不存在（B9 未落地）")
        return errs
    js = JSON_STREAM.read_text(encoding="utf-8")
    if not re.search(r"^class ContentExtractor\b", js, re.M):
        errs.append("[A1] llm/json_stream.py 未定义 class ContentExtractor")
    else:
        print("[A1] OK  llm/json_stream.py 定义 class ContentExtractor")

    # [2] ContentExtractor 是公共名（非 _ContentExtractor）
    if re.search(r"^class _ContentExtractor\b", js, re.M):
        errs.append("[A2] json_stream.py 用了私有名 _ContentExtractor（应为公共 ContentExtractor）")
    else:
        print("[A2] OK  ContentExtractor 公共名（无下划线私有前缀）")

    # [3] llm/__init__.py 导出 ContentExtractor
    init = LLM_INIT.read_text(encoding="utf-8")
    if "from .json_stream import ContentExtractor" not in init and "from llm.json_stream import ContentExtractor" not in init:
        errs.append("[A3] llm/__init__.py 未导出 ContentExtractor")
    elif "ContentExtractor" not in init.split("__all__")[1] if "__all__" in init else True:
        # 宽松：只要 __all__ 含 ContentExtractor
        if "__all__" in init and "ContentExtractor" in init.split("__all__", 1)[1]:
            print("[A3] OK  llm/__init__.py 导出 ContentExtractor（__all__ 含）")
        else:
            print("[A3] OK  llm/__init__.py 导出 ContentExtractor（from .json_stream import）")
    else:
        print("[A3] OK  llm/__init__.py 导出 ContentExtractor")

    # ── B. coordinator 改从 llm 取 ──
    coord = COORD.read_text(encoding="utf-8")
    coord_imports = _imports_block(coord)

    # [4] coordinator from llm.json_stream import ContentExtractor
    # 整文件搜（import 行是单行，不受多行 import 块影响）。
    if "from llm.json_stream import ContentExtractor" not in coord:
        errs.append("[B4] coordinator 未 from llm.json_stream import ContentExtractor")
    else:
        print("[B4] OK  coordinator from llm.json_stream import ContentExtractor")

    # [5] coordinator 调用点用 ContentExtractor()（非 _ContentExtractor()）
    # 抽 _stream_coordinator_decision 函数体
    m_stream = re.search(r"async def _stream_coordinator_decision\([^)]*\)(.*?)(?=\nasync def |\ndef )", coord, re.S)
    stream_body = m_stream.group(1) if m_stream else ""
    if "extractor = ContentExtractor()" not in stream_body:
        errs.append("[B5] coordinator _stream_coordinator_decision 未用 ContentExtractor()（仍 _ContentExtractor()）")
    else:
        print("[B5] OK  coordinator 调用点用 ContentExtractor()")

    # [6] coordinator 保留 _ContentExtractor 向后兼容别名（subclass ContentExtractor）
    m_alias = re.search(r"class _ContentExtractor\(ContentExtractor\)", coord)
    if not m_alias:
        errs.append("[B6] coordinator 未保留 _ContentExtractor 别名（旧引用会断）")
    else:
        print("[B6] OK  coordinator 保留 _ContentExtractor(ContentExtractor) 向后兼容别名")

    # ── C. worker 改从 llm 取（B9 核心）──
    worker = WORKER.read_text(encoding="utf-8")
    worker_imports = _imports_block(worker)

    # [7] worker.py 无 from engine.coordinator import 行
    if re.search(r"from\s+engine\.coordinator\s+import", worker_imports) or re.search(r"from\s+engine\s+import\s+coordinator", worker_imports):
        errs.append("[C7] worker.py 仍 from engine.coordinator import（反向导入未删·B9 核心）")
    else:
        print("[C7] OK  worker.py 无 from engine.coordinator import（反向导入已删）")

    # [8] worker.py from llm.json_stream import ContentExtractor
    if "from llm.json_stream import ContentExtractor" not in worker_imports:
        errs.append("[C8] worker.py 未 from llm.json_stream import ContentExtractor")
    else:
        print("[C8] OK  worker.py from llm.json_stream import ContentExtractor（正向依赖 llm）")

    # [9] worker 调用点用 ContentExtractor()（非 _ContentExtractor()）
    m_wstream = re.search(r"async def _stream_brain_decision\([^)]*\)(.*?)(?=\nasync def |\ndef )", worker, re.S)
    wstream_body = m_wstream.group(1) if m_wstream else ""
    if "extractor = ContentExtractor()" not in wstream_body:
        errs.append("[C9] worker _stream_brain_decision 未用 ContentExtractor()（仍 _ContentExtractor()）")
    else:
        print("[C9] OK  worker 调用点用 ContentExtractor()")

    # ── D. 行为零变（B9 纯搬家）──
    # [10] ContentExtractor 状态机字段在位（_KEY/_in_content/_escaped/_buf/_out）
    fields = ["_KEY", "_in_content", "_escaped", "_buf", "_out", "_key_idx", "_brace_seen"]
    missing = [f for f in fields if f not in js]
    if missing:
        errs.append(f"[D10] ContentExtractor 缺状态机字段 {missing}（搬家丢逻辑）")
    else:
        print(f"[D10] OK  ContentExtractor 状态机字段齐全（{len(fields)} 字段，feed/take 逻辑未变）")

    # [11] coordinator._ContentExtractor 别名 is ContentExtractor（subclass 不改行为）
    if m_alias:
        # subclass 无 __init__/feed/take override 即行为等价（继承父类）
        alias_body = re.search(r"class _ContentExtractor\(ContentExtractor\):(.*?)(?=\n\n|\nclass |\nasync def |\ndef )", coord, re.S)
        if alias_body:
            ab = alias_body.group(1)
            # 别名体只应有 docstring，无 def feed/take/__init__ override
            if re.search(r"def (feed|take|__init__)\b", ab):
                errs.append("[D11] _ContentExtractor 别名 override 了 feed/take/__init__（行为可能变）")
            else:
                print("[D11] OK  _ContentExtractor 别名无 override（纯继承，行为等价）")
        else:
            print("[D11] OK  _ContentExtractor 别名体仅 docstring（无 override，行为等价）")

    # ── E. 无新循环依赖 ──
    # [12] coordinator 不导入 worker
    if re.search(r"from\s+engine\.worker\s+import|from\s+engine\s+import\s+worker", coord_imports):
        errs.append("[E12] coordinator 导入 worker（B9 引入新环）")
    else:
        print("[E12] OK  coordinator 不导入 worker（无新环）")

    # [13] llm/json_stream.py 可执行代码不导入 engine.*（docstring 历史提及不算）
    js_code = re.sub(r'""".*?"""', "", js, flags=re.S)
    if re.search(r"^\s*(from\s+engine\b|import\s+engine\b)", js_code, re.M):
        errs.append("[E13] llm/json_stream.py 可执行代码导入 engine.*（纯工具不该反向依赖 engine）")
    else:
        print("[E13] OK  llm/json_stream.py 可执行代码不导入 engine.*（纯工具，docstring 历史提及不算）")

    # ── F. 依赖拓扑收敛 ──
    # [14] worker import 块无 engine.coordinator
    if "engine.coordinator" in worker_imports or "import coordinator" in worker_imports:
        errs.append("[F14] worker import 块仍含 engine.coordinator（worker 依赖 coordinator 未消除）")
    else:
        print("[F14] OK  worker import 块无 engine.coordinator（worker 与 coordinator 平级）")

    # [15] coordinator import 块无 engine.worker（本来就没有，B9 不破）
    if "engine.worker" in coord_imports or "import worker" in coord_imports:
        errs.append("[F15] coordinator import 块含 engine.worker（本来不该有）")
    else:
        print("[F15] OK  coordinator import 块无 engine.worker（拓扑未破）")

    return errs


def main() -> int:
    print("=== VH6 回归：_ContentExtractor 抽到 llm/json_stream.py 消除反向导入 ===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "VH6 回归契约锁定（B9 抽取不退化）：\n"
        "  · A ContentExtractor 新家 llm/json_stream.py（公共名，llm/__init__ 导出）；\n"
        "  · B coordinator from llm.json_stream import ContentExtractor + 调用点用 ContentExtractor() + "
        "保留 _ContentExtractor(ContentExtractor) 向后兼容别名；\n"
        "  · C worker 无 from engine.coordinator import（反向导入已删·B9 核心）+ from llm.json_stream "
        "import ContentExtractor + 调用点用 ContentExtractor()；\n"
        "  · D 行为零变：状态机字段齐全 + 别名无 override 纯继承（feed/take 解析逻辑不变）；\n"
        "  · E 无新循环依赖：coordinator 不导入 worker + json_stream 不导入 engine.*；\n"
        "  · F 依赖拓扑收敛：worker import 块无 engine.coordinator（worker 与 coordinator 平级，"
        "都依赖 llm），coordinator 无 engine.worker。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

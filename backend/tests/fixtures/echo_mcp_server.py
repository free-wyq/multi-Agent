#!/usr/bin/env python3
"""MCP echo server fixture — 暴露单个 ``echo(text)`` 工具，stdio transport.

用途
----
任务16b（MCP 全链路 e2e）的确定性 MCP 服务端。e2e 测试建一条指向本脚本的
stdio MCP 连接 → langchain-mcp-adapters ``MultiServerMCPClient`` spawn 本进程
作为子进程 → 自省出 ``echo`` 工具 → 挂载到 agent → 真 发 ``/api/messages`` 触发
execute → 断言 trace 含 ``tool_start/end name=echo`` + reply 含回显原文。

为何是 FastMCP 而非手写 JSON-RPC：硬约束 [[engines-use-frameworks-not-handrolled]]
+ [[use-open-source-not-handrolled]] —— 引擎/协议用开源框架预置能力，不自研。
``mcp.server.fastmcp.FastMCP``（mcp SDK 1.28.1）封装 stdio JSON-RPC 握手 /
``tools/list`` / ``tools/call`` 全套，本 fixture 只声明工具语义。

工具语义
--------
``echo(text: str) -> str``
  原样返回入参 ``text``（确定性、无副作用、无外部依赖、无网络）。
  选 ``echo`` 而非 ``add``/``fetch`` 之类：回显是 e2e 断言最稳的契约——输入即
  期望输出，不引入算术/网络/时序歧义，且 reply 文本里能直接 grep 到原文。

command / args 约定（e2e 落库字段）
-----------------------------------
- ``command``：``"python"``（对齐 ``api/mcp.DEFAULT_STDIO_COMMAND_WHITELIST``）。
- ``args``：``["<repo>/backend/tests/fixtures/echo_mcp_server.py"]``（脚本绝对路径）。
- ``env``：可空。

⚠️ ``python`` vs ``python3`` PATH 坑（任务16c 已修：白名单补 ``python3``）
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
本机 PATH 上只有 ``python3``（``/usr/bin/python3``，无 ``python`` 软链）。
任务16c 已把 ``api/mcp.DEFAULT_STDIO_COMMAND_WHITELIST`` 补上 ``python3``，
故 ``POST /api/mcp`` 用 ``command="python3"`` + ``args=["<fixture 绝对路径>"]``
可过白名单直接落库（不再被误拦）。``command="python"`` 仍在白名单内（PATH
上有软链的机器用）；``sys.executable``（含 ``/`` 的全路径）仍被白名单拦——
可信测试代码需走全路径时直调 ``crud.create_mcp_connection`` 绕过 HTTP 校验
（任务16b e2e 即用此法，见下）。

两条 e2e 落库法（任选其一，都验过）：

  1. **走 HTTP 白名单 + ``command="python3"``**（推荐，验生产路径）：
     ``POST /api/mcp`` 传 ``command="python3", args=[<fixture>]`` ——
     白名单放行 → spawn 用 PATH 上的 ``python3``。
  2. **绕 HTTP 白名单 + ``command=sys.executable``**（验 raw 链路，可信测试代码）：
     ``crud.create_mcp_connection(command=sys.executable, args=[<fixture>])``
     （``sys.executable`` 如 ``/usr/bin/python3`` 含 ``/``，HTTP 入口会拦，
     但 ``crud`` 直调不校验——e2e 是可信测试代码，非外部输入）。
     ``StdioServerParameters`` 接受全路径，spawn 用真解释器，不被 PATH 卡。

本 fixture 自身对解释器无要求——谁拉起它（``python`` / ``python3`` / 全路径）
都能跑，因为入口只是 ``mcp.run(transport="stdio")``。

手动验证
--------
::

    python3 backend/tests/fixtures/echo_mcp_server.py        # 阻塞读 stdin（stdio server）
    # 或经 langchain-mcp-adapters 真起一遍（任务16b 自测脚本同款）：
    #   client = MultiServerMCPClient({"echo": {
    #       "transport":"stdio","command": sys.executable,
    #       "args":["<repo>/backend/tests/fixtures/echo_mcp_server.py"]}})
    #   tools = await client.get_tools(server_name="echo")   # → [<Tool echo>]
    #   await tools[0].ainvoke({"text":"hi"})                # → "hi"

设计取舍
--------
- **同步 ``def echo``（非 ``async def``）**：FastMCP 同步工具在线程池跑，回显
  无 I/O，同步更简；e2e 调 ``ainvoke`` 仍走异步通道（SDK 适配）。
- **无 ``__main__`` 之外的副作用**：模块顶层只建 ``FastMCP`` 实例 + 注册 tool，
  ``run()`` 严格在 ``if __name__ == "__main__"`` 内，避免被 import 侧起 server。
- **不接 LLM / 不连 DB / 不读写文件**：纯函数工具，e2e 失败时排除 fixture 自身
  作为噪声源。
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

# server name "echo" —— langchain-mcp-adapters 用 connection dict 的 key 作
# server_name（见 mcp_manager._build_client），此处 FastMCP 实例名仅用于
# MCP 协议握手时的 server identity，与 connection key 解耦。
mcp = FastMCP("echo")


@mcp.tool()
def echo(text: str) -> str:
    """Echo back the input text verbatim.

    Args:
        text: 任意字符串，原样返回。

    Returns:
        与 ``text`` 完全相同的字符串。
    """
    return text


if __name__ == "__main__":
    # stdio transport：从 stdin 读 JSON-RPC（initialize / tools/list / tools/call），
    # 向 stdout 写响应。FastMCP.run 阻塞至 stdin 关闭 / transport 结束。
    mcp.run(transport="stdio")

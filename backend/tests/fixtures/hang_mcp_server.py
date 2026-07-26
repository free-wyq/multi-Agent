#!/usr/bin/env python3
"""MCP hang server fixture — 启动后永不响应 tools/list，强制自省超时.

用途
----
任务16c 回归测试 ``test_mc_e2e_16c_regressions.py`` 的 C 段（MCP 自省超时降级）。
``load_mcp_tools`` 用 ``asyncio.wait_for(timeout=MCP_INTROSPECT_TIMEOUT)`` 包住
``get_tools``；本 fixture 启动后**不注册任何工具、不进 MCP 协议握手**，只在
``mcp.run(transport="stdio")`` 之前死循环 sleep，让 ``stdio_client`` spawn 出来
后 ``ClientSession.initialize()`` / ``tools/list`` 永远等不到响应 → 自省必超时。

为何不写一个「响应慢」的真 FastMCP server：任务16c 要锁的是「server 完全不响应」
的最坏情况（stdio server 启动失败 / 自省挂死），不是「响应慢」。一个 ``while True:
time.sleep(1)`` 的进程最贴近「spawn 出来了但永不响应」的真实故障形态，且不引入
FastMCP 握手的复杂性（不需要它真跑 MCP 协议——超时路径在协议握手前就触发）。

command / args 约定
-------------------
与 ``echo_mcp_server.py`` 同型：``command=sys.executable``（e2e 可信测试代码直调
``crud`` 绕 HTTP 白名单）+ ``args=[<本脚本绝对路径>]``。本 fixture 对解释器无要求。

清理
----
``stdio_client`` 的 ``async with`` 退出时走 SIGTERM→（PROCESS_TERMINATION_TIMEOUT
后）SIGKILL 兜底终止本进程；回归测试 C 段在 ``await asyncio.sleep(1.0)`` 后用
``pgrep -f hang_mcp_server`` 断言无残留（锁住超时降级路径不泄漏子进程）。
"""
from __future__ import annotations

import time

# 不 import FastMCP、不注册 tool、不握手——只 spawn 出来占着进程，永不响应。
# stdio_client 的 stdout_reader 会一直等本进程的 stdout 输出（永远不来）→
# ClientSession.initialize() 永不完成 → load_mcp_tools 的 wait_for 必超时。
while True:
    time.sleep(1)

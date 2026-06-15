"""
群主智能体（Coordinator）

基于 LangGraph + LangChain 实现：
意图分析 → 任务拆解 → DAG 构建 → 调度派发 → 状态监控 → 结果汇总

群主是后端进程内的 LangGraph 状态图，不是 Claude Code 实例。
"""

"""
群主智能体（Coordinator）单元测试

测试范围：
- LangGraph 状态图构建和编译
- 各节点函数的纯逻辑（mock LLM 和 DB）
- 条件边 should_continue 逻辑
- 辅助函数 _build_roles_context / _build_dag_structure
- 调度服务层 get_coordinator_graph_structure
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.coordinator.state import CoordinatorState
from app.coordinator.graph import (
    analyze,
    decompose,
    dispatch,
    monitor,
    summarize,
    should_continue,
    build_coordinator_graph,
    _build_roles_context,
    _build_dag_structure,
)
from app.coordinator.llm import IntentAnalysis, SubTaskDef, TaskDecomposition
from app.coordinator.service import get_coordinator_graph_structure


# ── 辅助函数测试 ──────────────────────────────────────────────────────


class TestBuildRolesContext:
    """角色上下文构建测试"""

    def test_known_roles(self):
        result = _build_roles_context(["frontend-engineer", "backend-engineer"])
        assert "前端工程师" in result
        assert "后端工程师" in result

    def test_unknown_role(self):
        result = _build_roles_context(["custom-role"])
        assert "custom-role" in result
        assert "自定义角色" in result

    def test_empty_roles(self):
        result = _build_roles_context([])
        assert result == ""

    def test_mixed_roles(self):
        result = _build_roles_context(["tester", "custom-role"])
        assert "测试工程师" in result
        assert "自定义角色" in result


class TestBuildDagStructure:
    """DAG 结构构建测试"""

    def test_simple_tasks(self):
        subtasks = [
            {"title": "任务A", "description": "A", "assigned_agent_id": "fe", "dependencies": []},
            {"title": "任务B", "description": "B", "assigned_agent_id": "be", "dependencies": [0]},
        ]
        nodes, edges = _build_dag_structure(subtasks)
        assert len(nodes) == 2
        assert nodes[0]["id"] == "task-0"
        assert nodes[0]["label"] == "任务A"
        assert nodes[1]["id"] == "task-1"
        assert len(edges) == 1
        assert edges[0]["source"] == "task-0"
        assert edges[0]["target"] == "task-1"

    def test_parallel_tasks(self):
        subtasks = [
            {"title": "A", "description": "A", "assigned_agent_id": "fe", "dependencies": []},
            {"title": "B", "description": "B", "assigned_agent_id": "be", "dependencies": []},
        ]
        nodes, edges = _build_dag_structure(subtasks)
        assert len(nodes) == 2
        assert len(edges) == 0  # 无依赖，无边

    def test_empty_subtasks(self):
        nodes, edges = _build_dag_structure([])
        assert nodes == []
        assert edges == []

    def test_diamond_dependency(self):
        """A → C, B → C（汇聚依赖）"""
        subtasks = [
            {"title": "A", "description": "A", "assigned_agent_id": "fe", "dependencies": []},
            {"title": "B", "description": "B", "assigned_agent_id": "be", "dependencies": []},
            {"title": "C", "description": "C", "assigned_agent_id": "test", "dependencies": [0, 1]},
        ]
        nodes, edges = _build_dag_structure(subtasks)
        assert len(nodes) == 3
        assert len(edges) == 2  # A→C, B→C


# ── 条件边测试 ──────────────────────────────────────────────────────


class TestShouldContinue:
    """should_continue 条件边逻辑测试"""

    def test_no_subtasks(self):
        state = {"subtasks": [], "running_task_ids": [], "completed_task_ids": [], "failed_task_ids": []}
        assert should_continue(state) == "end"

    def test_has_running_tasks(self):
        state = {
            "subtasks": [{"title": "A"}],
            "running_task_ids": ["t1"],
            "completed_task_ids": [],
            "failed_task_ids": [],
        }
        assert should_continue(state) == "monitor"

    def test_all_completed(self):
        state = {
            "subtasks": [{"title": "A"}, {"title": "B"}],
            "running_task_ids": [],
            "completed_task_ids": ["t1", "t2"],
            "failed_task_ids": [],
        }
        assert should_continue(state) == "summarize"

    def test_all_failed(self):
        state = {
            "subtasks": [{"title": "A"}],
            "running_task_ids": [],
            "completed_task_ids": [],
            "failed_task_ids": ["t1"],
        }
        assert should_continue(state) == "summarize"

    def test_mixed_completed_and_failed(self):
        state = {
            "subtasks": [{"title": "A"}, {"title": "B"}],
            "running_task_ids": [],
            "completed_task_ids": ["t1"],
            "failed_task_ids": ["t2"],
        }
        assert should_continue(state) == "summarize"

    def test_still_has_pending(self):
        """有 submitted 的任务但 running 为空，应该继续 monitor"""
        state = {
            "subtasks": [{"title": "A"}, {"title": "B"}],
            "running_task_ids": [],
            "completed_task_ids": ["t1"],
            "failed_task_ids": [],
        }
        assert should_continue(state) == "monitor"


# ── 图构建测试 ──────────────────────────────────────────────────────


class TestBuildCoordinatorGraph:
    """LangGraph 状态图构建和编译测试"""

    def test_build_and_compile(self):
        graph = build_coordinator_graph()
        compiled = graph.compile()
        assert compiled is not None

    def test_graph_nodes(self):
        graph = build_coordinator_graph()
        compiled = graph.compile()
        node_names = set(compiled.nodes.keys())
        # 应包含所有业务节点
        assert "analyze" in node_names
        assert "decompose" in node_names
        assert "dispatch" in node_names
        assert "monitor" in node_names
        assert "summarize" in node_names

    def test_graph_mermaid_output(self):
        """验证图能生成 mermaid 格式输出"""
        graph = build_coordinator_graph()
        compiled = graph.compile()
        mermaid = compiled.get_graph().draw_mermaid()
        assert "analyze" in mermaid
        assert "decompose" in mermaid
        assert "dispatch" in mermaid
        assert "monitor" in mermaid
        assert "summarize" in mermaid


# ── 节点函数测试（mock LLM/DB） ────────────────────────────────────


class TestAnalyzeNode:
    """意图分析节点测试"""

    @pytest.mark.asyncio
    async def test_analyze_returns_expected_fields(self):
        mock_result = IntentAnalysis(
            analysis="需要前端和后端协作",
            involved_roles=["frontend-engineer", "backend-engineer"],
        )
        with patch("app.coordinator.graph.get_intent_analyzer") as mock_get:
            mock_analyzer = AsyncMock()
            mock_analyzer.ainvoke = AsyncMock(return_value=mock_result)
            mock_get.return_value = mock_analyzer

            state: CoordinatorState = {
                "group_id": "g1",
                "requirement": "开发一个登录功能",
                "intent_analysis": "",
                "involved_roles": [],
                "subtasks": [],
                "dag_nodes": [],
                "dag_edges": [],
                "pending_task_ids": [],
                "running_task_ids": [],
                "completed_task_ids": [],
                "failed_task_ids": [],
                "summary": "",
                "artifacts": [],
                "messages": [],
            }
            result = await analyze(state)

            assert result["intent_analysis"] == "需要前端和后端协作"
            assert result["involved_roles"] == ["frontend-engineer", "backend-engineer"]


class TestDecomposeNode:
    """任务拆解节点测试"""

    @pytest.mark.asyncio
    async def test_decompose_returns_subtasks_and_dag(self):
        mock_result = TaskDecomposition(
            subtasks=[
                SubTaskDef(title="前端页面", description="实现登录UI", assigned_role="frontend-engineer", depends_on=[]),
                SubTaskDef(title="后端接口", description="实现登录API", assigned_role="backend-engineer", depends_on=[]),
                SubTaskDef(title="集成测试", description="测试登录流程", assigned_role="tester", depends_on=[0, 1]),
            ],
            reasoning="前端和后端可并行，测试依赖两者",
        )
        with patch("app.coordinator.graph.get_task_decomposer") as mock_get, \
             patch("app.coordinator.graph._get_group_roles", new_callable=AsyncMock) as mock_roles:
            mock_decomposer = AsyncMock()
            mock_decomposer.ainvoke = AsyncMock(return_value=mock_result)
            mock_get.return_value = mock_decomposer
            mock_roles.return_value = []  # 无群组角色，走 _build_roles_context 分支

            state: CoordinatorState = {
                "group_id": "g1",
                "requirement": "开发一个登录功能",
                "intent_analysis": "需要前端和后端协作",
                "involved_roles": ["frontend-engineer", "backend-engineer", "tester"],
                "subtasks": [],
                "dag_nodes": [],
                "dag_edges": [],
                "pending_task_ids": [],
                "running_task_ids": [],
                "completed_task_ids": [],
                "failed_task_ids": [],
                "summary": "",
                "artifacts": [],
                "messages": [],
            }
            result = await decompose(state)

            assert len(result["subtasks"]) == 3
            assert result["subtasks"][0]["title"] == "前端页面"
            assert result["subtasks"][2]["dependencies"] == [0, 1]
            assert len(result["dag_nodes"]) == 3
            assert len(result["dag_edges"]) == 2  # 前端→测试, 后端→测试


class TestSummarizeNode:
    """结果汇总节点测试"""

    @pytest.mark.asyncio
    async def test_summarize_with_artifacts(self):
        # Mock DB 查询
        mock_tasks = []
        for title, artifact, summary in [
            ("前端页面", "/shared/login.py", "登录页面完成"),
            ("后端接口", "/shared/api.py", "API接口完成"),
        ]:
            t = MagicMock()
            t.title = title
            t.artifact_path = artifact
            t.result_summary = summary
            mock_tasks.append(t)

        mock_content = MagicMock()
        mock_content.content = "登录功能已全部完成"
        mock_summarizer = AsyncMock()
        mock_summarizer.ainvoke = AsyncMock(return_value=mock_content)

        with patch("app.coordinator.graph.get_summarizer", return_value=mock_summarizer), \
             patch("app.coordinator.graph.async_session") as mock_session_ctx:
            mock_db = AsyncMock()
            mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch("app.coordinator.graph.task_service") as mock_task_svc:
                mock_task_svc.list_tasks_by_group = AsyncMock(return_value=mock_tasks)

                state: CoordinatorState = {
                    "group_id": "g1",
                    "requirement": "开发登录功能",
                    "intent_analysis": "",
                    "involved_roles": [],
                    "subtasks": [],
                    "dag_nodes": [],
                    "dag_edges": [],
                    "pending_task_ids": [],
                    "running_task_ids": [],
                    "completed_task_ids": [],
                    "failed_task_ids": [],
                    "summary": "",
                    "artifacts": [],
                    "messages": [],
                }
                result = await summarize(state)

                assert result["summary"] == "登录功能已全部完成"
                assert len(result["artifacts"]) == 2
                assert result["artifacts"][0]["path"] == "/shared/login.py"


# ── 服务层测试 ──────────────────────────────────────────────────────


class TestCoordinatorService:
    """调度服务层测试"""

    @pytest.mark.asyncio
    async def test_get_coordinator_graph_structure(self):
        result = await get_coordinator_graph_structure()
        assert "nodes" in result
        assert "edges" in result
        node_ids = [n["id"] for n in result["nodes"]]
        assert "analyze" in node_ids
        assert "decompose" in node_ids
        assert "dispatch" in node_ids
        assert "monitor" in node_ids
        assert "summarize" in node_ids

    @pytest.mark.asyncio
    async def test_graph_structure_edges(self):
        result = await get_coordinator_graph_structure()
        # 至少要有 analyze→decompose, decompose→dispatch 等边
        edge_pairs = [(e["source"], e["target"]) for e in result["edges"]]
        assert ("analyze", "decompose") in edge_pairs
        assert ("decompose", "dispatch") in edge_pairs


# ── 结构化输出 Schema 测试 ──────────────────────────────────────────


class TestSchemas:
    """Pydantic 结构化输出 Schema 测试"""

    def test_intent_analysis_schema(self):
        obj = IntentAnalysis(analysis="test", involved_roles=["fe"])
        assert obj.analysis == "test"
        assert obj.involved_roles == ["fe"]

    def test_task_decomposition_schema(self):
        obj = TaskDecomposition(
            subtasks=[
                SubTaskDef(title="A", description="do A", assigned_role="fe", depends_on=[]),
            ],
            reasoning="test",
        )
        assert len(obj.subtasks) == 1
        assert obj.subtasks[0].title == "A"

    def test_subtask_def_with_dependencies(self):
        obj = SubTaskDef(title="B", description="do B", assigned_role="be", depends_on=[0, 1])
        assert obj.depends_on == [0, 1]

    def test_subtask_def_default_dependencies(self):
        obj = SubTaskDef(title="A", description="do A", assigned_role="fe")
        assert obj.depends_on == []

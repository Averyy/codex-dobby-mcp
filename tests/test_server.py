from pathlib import Path

import pytest
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from codex_dobby_mcp import __version__
from codex_dobby_mcp import server as server_module
from codex_dobby_mcp.models import DEFAULT_TOOL_TIMEOUT_SECONDS, ReviewAgent, ToolName
from codex_dobby_mcp.server import (
    _effective_start_run_timeout,
    create_server,
)


def test_server_exposes_exactly_eleven_tools() -> None:
    server = create_server()
    tools = sorted(server._tool_manager.list_tools(), key=lambda tool: tool.name)
    tool_names = [tool.name for tool in tools]

    assert tool_names == [
        "brainstorm",
        "build",
        "get_run",
        "list_runs",
        "plan",
        "research",
        "reverse_engineer",
        "review",
        "start_run",
        "validate",
        "wait_run",
    ]
    assert all(tool.description for tool in tools)
    assert all(tool.output_schema for tool in tools)


def test_server_advertises_dobby_distribution_version() -> None:
    server = create_server()

    assert __version__ != "0.0.0+unknown"
    assert server.version == __version__


def test_review_and_start_run_document_supported_review_agents() -> None:
    server = create_server()
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}
    expected_agents = [agent.value for agent in ReviewAgent]

    for tool_name in ("review", "start_run"):
        schema = tools[tool_name].parameters
        agents_schema = schema["properties"]["agents"]

        assert schema["$defs"]["ReviewAgent"]["enum"] == expected_agents
        assert agents_schema["description"].startswith("Review lenses to run.")
        assert "Supported values: generalist, security, performance, architecture, correctness, ux, regression." in agents_schema["description"]
        assert agents_schema["examples"] == [
            ["correctness"],
            ["correctness", "regression", "architecture"],
        ]

    review_output = tools["review"].output_schema
    assert review_output is not None
    review_details = review_output["$defs"]["ReviewDetails"]["properties"]
    assert "Integrity signal only" in review_details["manifest_validated"]["description"]
    assert "every selected lens completed" in review_details["coverage_complete"]["description"]
    assert "Verified completed lens count" in review_details["completed_agents"]["description"]


def test_every_tool_requires_repo_root() -> None:
    server = create_server()

    for tool in server._tool_manager.list_tools():
        assert "repo_root" in tool.parameters["required"], tool.name
        repo_root_schema = tool.parameters["properties"]["repo_root"]
        assert repo_root_schema["type"] == "string"
        assert "Always pass the caller's active repository root" in repo_root_schema["description"]


@pytest.mark.asyncio
async def test_tool_boundary_rejects_missing_repo_root_before_creating_artifacts(
    tmp_path: Path,
) -> None:
    server = create_server(tmp_path)
    plan_tool = {tool.name: tool for tool in server._tool_manager.list_tools()}["plan"]

    with pytest.raises(ToolError, match=r"repo_root\s+Field required"):
        await plan_tool.run({"prompt": "plan it"}, Context())
    assert not (tmp_path / ".codex-dobby").exists()


@pytest.mark.asyncio
async def test_tool_boundary_rejects_relative_repo_root() -> None:
    server = create_server()
    plan_tool = {tool.name: tool for tool in server._tool_manager.list_tools()}["plan"]

    with pytest.raises(ToolError, match="repo_root must be an absolute path"):
        await plan_tool.run({"prompt": "plan it", "repo_root": "../wrong-repo"}, Context())


def test_create_runner_prefers_explicit_codex_binary_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_BINARY", "/tmp/custom-codex")

    runner = server_module.create_runner(tmp_path)

    assert runner.codex_binary == "/tmp/custom-codex"
    assert runner.prompts.prompts_root.exists()
    assert runner.worker_schema_path.exists()
    assert runner.review_agents_root.exists()


def test_create_runner_uses_packaged_assets() -> None:
    runner = server_module.create_runner(Path.cwd())
    # Resolve against the imported package rather than the source tree so this
    # also holds when the tests run against an installed wheel.
    assets_root = Path(server_module.__file__).resolve().parent / "assets"

    assert runner.prompts.prompts_root == assets_root / "prompts"
    assert runner.worker_schema_path == assets_root / "schemas" / "worker-output.schema.json"
    assert runner.review_agents_root == assets_root / "codex_agents"


def test_review_tool_defaults_multi_agent_to_the_same_budget_as_start_run() -> None:
    """review(agents=[a, b]) must not get half the budget start_run gives it.

    The two entry points drive the same orchestrator over the same work, so a
    hardcoded 600s default on the direct tool silently halved multi-agent
    reviews launched that way.
    """
    assert _effective_start_run_timeout(
        ToolName.REVIEW,
        None,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    ) == 1200

    server = create_server()
    review_tool = {tool.name: tool for tool in server._tool_manager.list_tools()}["review"]
    timeout_schema = review_tool.parameters["properties"]["timeout_seconds"]
    # No pinned 600: the tool resolves the default through the shared helper.
    assert timeout_schema.get("default") is None


def test_start_run_defaults_multi_agent_review_to_twenty_minutes() -> None:
    assert _effective_start_run_timeout(
        ToolName.REVIEW,
        None,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    ) == 1200
    assert _effective_start_run_timeout(
        ToolName.REVIEW,
        None,
        [ReviewAgent.CORRECTNESS],
    ) == DEFAULT_TOOL_TIMEOUT_SECONDS[ToolName.REVIEW]
    assert _effective_start_run_timeout(
        ToolName.REVIEW,
        777,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    ) == 777


@pytest.mark.asyncio
async def test_review_tool_boundary_rejects_invalid_agents(tmp_path: Path) -> None:
    server = create_server()
    review_tool = {tool.name: tool for tool in server._tool_manager.list_tools()}["review"]

    with pytest.raises(ToolError, match="Unsupported review agents: bogus"):
        await review_tool.run(
            {"prompt": "review it", "repo_root": str(tmp_path), "agents": ["bogus"]},
            Context(),
        )


@pytest.mark.asyncio
async def test_start_run_tool_boundary_rejects_agents_for_non_review_tools(tmp_path: Path) -> None:
    server = create_server()
    start_run_tool = {tool.name: tool for tool in server._tool_manager.list_tools()}["start_run"]

    with pytest.raises(ToolError, match="agents is only supported when tool=review"):
        await start_run_tool.run(
            {
                "tool": "plan",
                "prompt": "plan it",
                "repo_root": str(tmp_path),
                "agents": ["security"],
            },
            Context(),
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"ticket": "ABC-1"}', '{"ticket": "ABC-1"}'),
        ("[1, 2, 3]", "[1, 2, 3]"),
        ("plain prose", "plain prose"),
        ("42", "42"),
    ],
)
def test_important_context_survives_the_sdk_json_pre_parse(raw: str, expected: str) -> None:
    """JSON-looking context text must reach the worker, not be rejected.

    The SDK JSON-decodes any string argument whose annotation is not exactly
    ``str`` (python-sdk #3055), so an optional ``str | None`` parameter given
    JSON-looking text arrived as a dict/list and was then rejected with
    "Input should be a valid string".
    """
    server = create_server()
    plan_tool = {tool.name: tool for tool in server._tool_manager.list_tools()}["plan"]
    metadata = plan_tool.fn_metadata

    pre_parsed = metadata.pre_parse_json(
        {"prompt": "do it", "repo_root": "/tmp/repo", "important_context": raw}
    )
    arguments = metadata.arg_model.model_validate(pre_parsed)

    assert arguments.important_context == expected


def test_important_context_schema_is_still_a_nullable_string() -> None:
    server = create_server()
    plan_tool = {tool.name: tool for tool in server._tool_manager.list_tools()}["plan"]

    schema = plan_tool.parameters["properties"]["important_context"]

    assert schema["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert schema["default"] is None

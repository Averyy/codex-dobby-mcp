from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from codex_dobby_mcp import server as server_module
from codex_dobby_mcp.models import ReviewAgent
from codex_dobby_mcp.server import _caller_repo_root, create_server


def test_server_exposes_exactly_ten_tools() -> None:
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
    ]
    assert all(tool.description for tool in tools)
    assert all(getattr(tool, "outputSchema", None) or getattr(tool, "output_schema", None) for tool in tools)


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
    assets_root = Path(__file__).resolve().parents[1] / "src" / "codex_dobby_mcp" / "assets"

    assert runner.prompts.prompts_root == assets_root / "prompts"
    assert runner.worker_schema_path == assets_root / "schemas" / "worker-output.schema.json"
    assert runner.review_agents_root == assets_root / "codex_agents"


def test_caller_repo_root_prefers_repo_root_keys() -> None:
    ctx = SimpleNamespace(
        _request_context=object(),
        request_context=SimpleNamespace(
            meta=SimpleNamespace(
                model_dump=lambda: {
                    "cwd": "/tmp/cwd",
                    "repoRoot": "/tmp/repo-root",
                    "repo_root": "/tmp/repo-root-explicit",
                }
            )
        ),
    )

    assert _caller_repo_root(ctx) == "/tmp/repo-root-explicit"


def test_caller_repo_root_falls_back_to_cwd() -> None:
    ctx = SimpleNamespace(
        _request_context=object(),
        request_context=SimpleNamespace(
            meta=SimpleNamespace(
                model_dump=lambda: {
                    "cwd": "/tmp/cwd",
                }
            )
        ),
    )

    assert _caller_repo_root(ctx) == "/tmp/cwd"


def test_caller_repo_root_reads_nested_meta_keys() -> None:
    ctx = SimpleNamespace(
        _request_context=object(),
        request_context=SimpleNamespace(
            meta=SimpleNamespace(
                model_dump=lambda: {
                    "_meta": {
                        "repo_root": "/tmp/nested-repo-root",
                    }
                }
            )
        ),
    )

    assert _caller_repo_root(ctx) == "/tmp/nested-repo-root"


def test_caller_repo_root_returns_none_without_metadata() -> None:
    assert _caller_repo_root(None) is None
    ctx = SimpleNamespace(_request_context=object(), request_context=SimpleNamespace(meta=None))
    assert _caller_repo_root(ctx) is None


@pytest.mark.asyncio
async def test_review_tool_boundary_rejects_invalid_agents() -> None:
    server = create_server()
    review_tool = {tool.name: tool for tool in server._tool_manager.list_tools()}["review"]

    with pytest.raises(ToolError, match="Unsupported review agents: bogus"):
        await review_tool.run({"prompt": "review it", "agents": ["bogus"]})


@pytest.mark.asyncio
async def test_start_run_tool_boundary_rejects_agents_for_non_review_tools() -> None:
    server = create_server()
    start_run_tool = {tool.name: tool for tool in server._tool_manager.list_tools()}["start_run"]

    with pytest.raises(ToolError, match="agents is only supported when tool=review"):
        await start_run_tool.run(
            {
                "tool": "plan",
                "prompt": "plan it",
                "agents": ["security"],
            }
        )

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from codex_dobby_mcp import __version__
from codex_dobby_mcp import server as server_module
from codex_dobby_mcp.models import DEFAULT_TOOL_TIMEOUT_SECONDS, ReviewAgent, ToolName
from codex_dobby_mcp.server import (
    _effective_start_run_timeout,
    _request_from_params,
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
    "raw",
    [
        # Round-trips through json.dumps unchanged, so it passes even when the
        # SDK does re-parse. Kept as the control for the cases below.
        '{"ticket": "ABC-1"}',
        "[1, 2, 3]",
        "plain prose",
        "42",
        # Each of these is destroyed by a decode/re-encode round trip.
        '{\n  "pretty": true\n}',  # layout collapsed onto one line
        '{"n": 1e3}',  # 1e3 reformatted to 1000.0
        '{"d": 1, "d": 2}',  # duplicate key silently dropped
        '{"accent": "caf\\u00e9"}',  # \uXXXX unescaped to a literal é
        '{"deep": {"tab": "a\\tb"}}',
        "null",  # decodes to None, i.e. the argument vanishes entirely
        "true",
    ],
)
def test_important_context_reaches_the_worker_byte_for_byte(raw: str) -> None:
    """Context text must arrive exactly as the caller wrote it.

    ``FuncMetadata.pre_parse_json`` JSON-decodes any string argument whose
    annotation is not exactly ``str`` (python-sdk #3055). Under an optional
    ``str | None`` annotation that silently rewrote the caller's text —
    collapsing layout, reformatting numbers, dropping duplicate keys,
    unescaping ``\\uXXXX`` — and turned the literal text ``null`` into no
    argument at all. ``ImportantContextParam`` annotates plain ``str`` so the
    pre-parse never fires.
    """
    server = create_server()
    plan_tool = {tool.name: tool for tool in server._tool_manager.list_tools()}["plan"]
    metadata = plan_tool.fn_metadata

    pre_parsed = metadata.pre_parse_json(
        {"prompt": "do it", "repo_root": "/tmp/repo", "important_context": raw}
    )
    arguments = metadata.arg_model.model_validate(pre_parsed)

    assert arguments.important_context == raw


def test_absent_important_context_is_none_not_empty_string() -> None:
    """The empty-string default must not reach the worker as context.

    ``ImportantContextParam`` defaults to "" so its annotation can stay exactly
    ``str``; downstream still expects ``None`` when the caller passed nothing,
    including when a client sends an explicit ``null``.
    """
    assert _request_from_params(prompt="p", repo_root="/tmp/repo").important_context is None
    assert (
        _request_from_params(
            prompt="p", repo_root="/tmp/repo", important_context=""
        ).important_context
        is None
    )
    assert (
        _request_from_params(
            prompt="p", repo_root="/tmp/repo", important_context="ticket ABC-1"
        ).important_context
        == "ticket ABC-1"
    )


def test_explicit_null_important_context_is_accepted_over_the_wire() -> None:
    """The published schema is nullable, so an explicit null must validate."""
    server = create_server()
    plan_tool = {tool.name: tool for tool in server._tool_manager.list_tools()}["plan"]
    metadata = plan_tool.fn_metadata

    pre_parsed = metadata.pre_parse_json(
        {"prompt": "do it", "repo_root": "/tmp/repo", "important_context": None}
    )
    arguments = metadata.arg_model.model_validate(pre_parsed)

    assert arguments.important_context == ""


def test_important_context_schema_is_still_a_nullable_string() -> None:
    server = create_server()
    plan_tool = {tool.name: tool for tool in server._tool_manager.list_tools()}["plan"]

    schema = plan_tool.parameters["properties"]["important_context"]

    assert schema["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert schema["default"] is None


def test_misspelled_tool_argument_is_rejected_not_silently_dropped() -> None:
    """A typo must not silently change the scope of a run.

    The SDK builds argument models with Pydantic's default extra="ignore"
    (python-sdk #3067), so `file=[...]` instead of `files=[...]` was dropped and
    the caller unknowingly got a whole-repo run instead of a scoped one.
    """
    server = create_server()
    plan_tool = {tool.name: tool for tool in server._tool_manager.list_tools()}["plan"]
    arg_model = plan_tool.fn_metadata.arg_model

    with pytest.raises(ValidationError) as excinfo:
        arg_model.model_validate(
            {"prompt": "review it", "repo_root": "/tmp/repo", "file": ["a.py"]}
        )
    assert "file" in str(excinfo.value)

    # The correctly spelled argument still works.
    parsed = arg_model.model_validate(
        {"prompt": "review it", "repo_root": "/tmp/repo", "files": ["a.py"]}
    )
    assert parsed.files == ["a.py"]


def test_every_tool_publishes_that_it_rejects_unknown_arguments() -> None:
    server = create_server()

    for tool in server._tool_manager.list_tools():
        assert tool.fn_metadata.arg_model.model_config["extra"] == "forbid", tool.name
        # Clients see the constraint rather than only hitting it server-side.
        assert tool.parameters.get("additionalProperties") is False, tool.name
        # Hardening must not disturb the published argument surface.
        assert "repo_root" in tool.parameters["properties"], tool.name
        assert "ctx" not in tool.parameters["properties"], tool.name


def test_get_logger_does_not_reset_an_explicitly_configured_level() -> None:
    """Fetching a logger must not silently turn debug logging back off.

    get_logger() runs at runner construction, so a configure_logging(DEBUG) that
    got reset to INFO on the next lookup made debug output impossible to keep on.
    """
    import logging

    from codex_dobby_mcp.logging_utils import configure_logging, get_logger

    root = logging.getLogger("codex_dobby_mcp")
    original_level = root.level
    try:
        configure_logging(logging.DEBUG)
        assert root.level == logging.DEBUG

        get_logger("runner")
        assert root.level == logging.DEBUG, "get_logger reset the configured level"

        # An explicit call still changes it.
        configure_logging(logging.WARNING)
        assert root.level == logging.WARNING
    finally:
        root.setLevel(original_level)


def test_configure_logging_keeps_a_level_set_before_any_handler_exists() -> None:
    """Installing the handler must not overwrite an already chosen level.

    Keying the INFO default on "no handler yet" clobbered a level set directly
    on the logger before anything fetched one.
    """
    import logging

    from codex_dobby_mcp.logging_utils import get_logger

    root = logging.getLogger("codex_dobby_mcp")
    original_level = root.level
    original_handlers = list(root.handlers)
    try:
        for handler in original_handlers:
            root.removeHandler(handler)
        root.setLevel(logging.DEBUG)

        get_logger("runner")

        assert root.level == logging.DEBUG
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in original_handlers:
            root.addHandler(handler)
        root.setLevel(original_level)


def test_progress_token_is_read_with_the_sdk_2x_snake_case_key() -> None:
    """SDK 2.x request _meta is a TypedDict keyed progress_token, not a model.

    Reading progressToken instead returns None for every client and silently
    disables streaming progress. Nothing else in the suite executes this path,
    so without this the rename is invisible.
    """
    from types import SimpleNamespace

    from codex_dobby_mcp.server import _progress_token_from_ctx

    ctx = SimpleNamespace(
        _request_context=object(),
        request_context=SimpleNamespace(meta={"progress_token": "tok-1"}),
    )
    assert _progress_token_from_ctx(ctx) == "tok-1"

    # A camelCase-only meta must not be mistaken for a token.
    camel = SimpleNamespace(
        _request_context=object(),
        request_context=SimpleNamespace(meta={"progressToken": "tok-1"}),
    )
    assert _progress_token_from_ctx(camel) is None

    assert _progress_token_from_ctx(None) is None
    absent = SimpleNamespace(
        _request_context=object(), request_context=SimpleNamespace(meta=None)
    )
    assert _progress_token_from_ctx(absent) is None


def test_event_sink_is_built_only_when_the_caller_asked_for_progress() -> None:
    from types import SimpleNamespace

    from codex_dobby_mcp.server import _build_event_sink

    assert _build_event_sink(None) is None
    no_token = SimpleNamespace(
        _request_context=object(), request_context=SimpleNamespace(meta=None)
    )
    assert _build_event_sink(no_token) is None

    sent: list = []

    class _Session:
        async def send_notification(self, notification):  # type: ignore[no-untyped-def]
            sent.append(notification)

    with_token = SimpleNamespace(
        _request_context=object(),
        request_context=SimpleNamespace(
            meta={"progress_token": "tok-2"}, session=_Session()
        ),
    )
    sink = _build_event_sink(with_token)
    assert sink is not None

    asyncio.run(sink({"type": "tool_call", "title": "rg TODO"}, 1))

    assert len(sent) == 1
    params = sent[0].params
    assert params.progress_token == "tok-2"
    assert params.progress == 1.0
    assert params.message == "rg TODO"
    # _meta.acpEvent is a client-visible contract.
    assert params.meta["acpEvent"] == {"type": "tool_call", "title": "rg TODO"}

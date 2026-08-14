from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Annotated

from mcp import types as mcp_types
from mcp.server.mcpserver import Context, MCPServer
from pydantic import AfterValidator, BeforeValidator, Field

from codex_dobby_mcp._version import __version__
from codex_dobby_mcp.background_runs import BackgroundRunManager
from codex_dobby_mcp.models import (
    AsyncRunHandle,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    InvocationRequest,
    SUPPORTED_REVIEW_AGENTS_TEXT,
    ReviewAgent,
    RunListResponse,
    RunLookupResponse,
    ToolName,
    ToolResponse,
    parse_review_agents_input,
)
from codex_dobby_mcp.paths import resolve_repo_root
from codex_dobby_mcp.review_agents import review_agent_assets_root, review_uses_orchestrator
from codex_dobby_mcp.runner import CodexRunner


def _restore_json_pre_parsed_text(value: object) -> object:
    """Undo the SDK's JSON pre-parse for optional free-text arguments.

    ``FuncMetadata.pre_parse_json`` JSON-decodes any string argument whose
    annotation is not exactly ``str`` (python-sdk #3055, present in 1.x and
    2.x). An optional ``str | None`` parameter carrying JSON-looking text —
    pasting a JSON snippet into important_context is the obvious case — is
    therefore decoded into a dict or list and then rejected by validation with
    a confusing "Input should be a valid string". Re-serialise it so the
    caller's argument survives instead of failing the call.
    """
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


ImportantContextParam = Annotated[
    str | None,
    BeforeValidator(_restore_json_pre_parsed_text),
]


ReviewAgentsParam = Annotated[
    list[ReviewAgent] | None,
    BeforeValidator(parse_review_agents_input),
    Field(
        description=(
            "Review lenses to run. Only used for `review`, or for `start_run` when `tool` is `review`. "
            f"Supported values: {SUPPORTED_REVIEW_AGENTS_TEXT}."
        ),
        examples=[
            ["correctness"],
            ["correctness", "regression", "architecture"],
        ],
    ),
]

def _validate_repo_root_param(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("repo_root must not be empty")
    if not Path(normalized).expanduser().is_absolute():
        raise ValueError("repo_root must be an absolute path")
    return normalized


RepoRootParam = Annotated[
    str,
    AfterValidator(_validate_repo_root_param),
    Field(
        description=(
            "Absolute path to the target git worktree. Always pass the caller's active "
            "repository root; Dobby deliberately has no implicit repo fallback."
        )
    ),
]


def _assets_root() -> Path:
    return Path(__file__).resolve().parent / "assets"


def _default_codex_binary() -> str:
    if override := os.environ.get("CODEX_BINARY"):
        return override
    if discovered := shutil.which("codex"):
        return discovered
    return "/opt/homebrew/bin/codex"


def _effective_start_run_timeout(
    tool: ToolName,
    timeout_seconds: int | None,
    agents: list[ReviewAgent] | None,
) -> int:
    if timeout_seconds is not None:
        return timeout_seconds
    if tool == ToolName.REVIEW and review_uses_orchestrator(agents):
        return 1200
    return DEFAULT_TOOL_TIMEOUT_SECONDS[tool]


def _reject_unknown_tool_arguments(server: MCPServer) -> None:
    """Make a misspelled argument an error instead of a silent default.

    The SDK builds each tool's argument model with Pydantic's default
    ``extra="ignore"`` (python-sdk #3067), so a caller who writes ``file=[...]``
    instead of ``files=[...]`` has it dropped and unknowingly gets a whole-repo
    run. Dobby's arguments materially change scope, cost, and permissions, so
    an unknown name should fail loudly. Also republishes each tool's input
    schema so ``additionalProperties: false`` is visible to clients rather than
    only enforced server-side.
    """
    for tool in server._tool_manager.list_tools():
        arg_model = tool.fn_metadata.arg_model
        if arg_model.model_config.get("extra") == "forbid":
            continue
        arg_model.model_config["extra"] = "forbid"
        arg_model.model_rebuild(force=True)
        tool.parameters = arg_model.model_json_schema(by_alias=True)


def create_runner(spawn_root: Path | None = None) -> CodexRunner:
    assets_root = _assets_root()
    return CodexRunner(
        spawn_root=(spawn_root or Path.cwd()).resolve(),
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agent_assets_root(assets_root),
        codex_binary=_default_codex_binary(),
    )


def _request_from_params(
    *,
    prompt: str,
    repo_root: str | None = None,
    files: list[str] | None = None,
    important_context: ImportantContextParam = None,
    timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS[ToolName.PLAN],
    extra_roots: list[str] | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    agents: list[ReviewAgent] | None = None,
    danger: bool = False,
) -> InvocationRequest:
    return InvocationRequest(
        prompt=prompt,
        repo_root=repo_root,
        files=files or [],
        important_context=important_context,
        timeout_seconds=timeout_seconds,
        extra_roots=extra_roots or [],
        model=model,
        reasoning_effort=reasoning_effort,
        agents=agents or [],
        danger=danger,
    )


def _progress_token_from_ctx(ctx: Context | None) -> str | int | None:
    if ctx is None or ctx._request_context is None:
        return None
    # MCP SDK 2.x: request `_meta` is a `RequestParamsMeta` TypedDict keyed by
    # the snake_case protocol field, not a model with a camelCase attribute.
    meta = ctx.request_context.meta
    if meta is None:
        return None
    token = meta.get("progress_token")
    if token is None:
        return None
    return token


def _build_event_sink(ctx: Context | None):
    """Return an async sink that forwards ACP events as MCP progress notifications.

    Returns ``None`` when there is no caller-side progressToken, which keeps
    runs silent for clients that never asked for streaming. The full
    structured event lives in ``_meta.acpEvent`` on each notification; the
    short ``message`` field carries a human-readable summary so even clients
    that ignore ``_meta`` see something useful.
    """
    progress_token = _progress_token_from_ctx(ctx)
    if progress_token is None or ctx is None:
        return None

    session = ctx.request_context.session

    async def sink(event: dict, count: int) -> None:
        message = event.get("title") or event.get("type")
        params = mcp_types.ProgressNotificationParams(
            progress_token=progress_token,
            progress=float(count),
            message=message if isinstance(message, str) else None,
            _meta={"acpEvent": event},
        )
        await session.send_notification(mcp_types.ProgressNotification(params=params))

    return sink


def _build_wait_event_sink(ctx: Context | None):
    """Sink for ``wait_run`` — same shape as the worker sink, but tagged per task.

    MCP requires ``progress`` to be strictly monotonic per ``progressToken``.
    A single ``wait_run`` may forward events from multiple background runs,
    each with its own per-run counter starting at 1, so we maintain a
    wait-run-level counter that increments across every event regardless of
    which task it came from.
    """
    progress_token = _progress_token_from_ctx(ctx)
    if progress_token is None or ctx is None:
        return None

    session = ctx.request_context.session
    counter = [0]

    async def sink(event: dict, count: int, task_id: str) -> None:
        counter[0] += 1
        message = event.get("title") or event.get("type")
        params = mcp_types.ProgressNotificationParams(
            progress_token=progress_token,
            progress=float(counter[0]),
            message=message if isinstance(message, str) else None,
            _meta={
                "acpEvent": event,
                "taskId": task_id,
                "perTaskCount": count,
            },
        )
        await session.send_notification(mcp_types.ProgressNotification(params=params))

    return sink


def create_server(spawn_root: Path | None = None) -> MCPServer:
    runner = create_runner(spawn_root)
    background_runs = BackgroundRunManager(runner)
    server = MCPServer(
        "codex-dobby-mcp",
        version=__version__,
        instructions="Delegate scoped work to codex exec and return concise structured results. Always pass the absolute path of the caller's active git worktree as repo_root; Dobby deliberately does not guess or default it. Important: short timeouts cause failures. Use the selected tool's default timeout or longer; do not override it lower unless you have a specific reason. Review is direct with zero or one effective agent; pass two or more canonical agents for Codex multi-agent v2. Multi-agent start_run defaults to 1200 seconds when timeout_seconds is omitted. For long review, research, build, validate, or reverse_engineer work, prefer start_run plus wait_run. Always wait for each start_run to return its task_id before looking up that run; independent start_run calls may run in parallel. wait_run blocks until done or until its own timeout_seconds elapses, then re-call it with pending_task_ids. If the caller is Claude Code and you want the parent to keep working instead of blocking in wait_run, schedule a poll of get_run via /loop or ScheduleWakeup. Use get_run/list_runs for a non-blocking peek or to recover a task id after a caller-side timeout. For multi-agent results, manifest_validated proves structural integrity while coverage_complete proves every lens completed; neither proves a reviewer received the exact assignment. Runs accept no further input once started.",
    )

    @server.tool(name="plan", structured_output=True)
    async def plan(
        prompt: str,
        repo_root: RepoRootParam,
        files: list[str] | None = None,
        important_context: ImportantContextParam = None,
        timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS[ToolName.PLAN],
        extra_roots: list[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        ctx: Context | None = None,
    ) -> ToolResponse:
        """Break down a task and propose a scoped plan without editing files. Recommended timeout: 10 minutes (600s)."""
        request = _request_from_params(
            prompt=prompt,
            repo_root=repo_root,
            files=files,
            important_context=important_context,
            timeout_seconds=timeout_seconds,
            extra_roots=extra_roots,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        return await runner.run(ToolName.PLAN, request, event_sink=_build_event_sink(ctx))

    @server.tool(name="research", structured_output=True)
    async def research(
        prompt: str,
        repo_root: RepoRootParam,
        files: list[str] | None = None,
        important_context: ImportantContextParam = None,
        timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS[ToolName.RESEARCH],
        extra_roots: list[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        ctx: Context | None = None,
    ) -> ToolResponse:
        """Investigate code, docs, and context in read-only mode and report findings. Recommended timeout: 20 minutes (1200s)."""
        request = _request_from_params(
            prompt=prompt,
            repo_root=repo_root,
            files=files,
            important_context=important_context,
            timeout_seconds=timeout_seconds,
            extra_roots=extra_roots,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        return await runner.run(ToolName.RESEARCH, request, event_sink=_build_event_sink(ctx))

    @server.tool(name="brainstorm", structured_output=True)
    async def brainstorm(
        prompt: str,
        repo_root: RepoRootParam,
        files: list[str] | None = None,
        important_context: ImportantContextParam = None,
        timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS[ToolName.BRAINSTORM],
        extra_roots: list[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        ctx: Context | None = None,
    ) -> ToolResponse:
        """Evaluate an idea, scope an MVP, and recommend whether it is worth building. Recommended timeout: 10 minutes (600s)."""
        request = _request_from_params(
            prompt=prompt,
            repo_root=repo_root,
            files=files,
            important_context=important_context,
            timeout_seconds=timeout_seconds,
            extra_roots=extra_roots,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        return await runner.run(ToolName.BRAINSTORM, request, event_sink=_build_event_sink(ctx))

    @server.tool(name="build", structured_output=True)
    async def build(
        prompt: str,
        repo_root: RepoRootParam,
        files: list[str] | None = None,
        important_context: ImportantContextParam = None,
        timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS[ToolName.BUILD],
        extra_roots: list[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        danger: bool = False,
        ctx: Context | None = None,
    ) -> ToolResponse:
        """Implement a change, run focused verification, and report results. Recommended timeout: 20 minutes (1200s)."""
        request = _request_from_params(
            prompt=prompt,
            repo_root=repo_root,
            files=files,
            important_context=important_context,
            timeout_seconds=timeout_seconds,
            extra_roots=extra_roots,
            model=model,
            reasoning_effort=reasoning_effort,
            danger=danger,
        )
        return await runner.run(ToolName.BUILD, request, event_sink=_build_event_sink(ctx))

    @server.tool(name="validate", structured_output=True)
    async def validate(
        prompt: str,
        repo_root: RepoRootParam,
        files: list[str] | None = None,
        important_context: ImportantContextParam = None,
        timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS[ToolName.VALIDATE],
        extra_roots: list[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        ctx: Context | None = None,
    ) -> ToolResponse:
        """Run existing repo validation commands (build, test, lint) and report the results. Recommended timeout: 10 minutes (600s)."""
        request = _request_from_params(
            prompt=prompt,
            repo_root=repo_root,
            files=files,
            important_context=important_context,
            timeout_seconds=timeout_seconds,
            extra_roots=extra_roots,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        return await runner.run(ToolName.VALIDATE, request, event_sink=_build_event_sink(ctx))

    @server.tool(name="review", structured_output=True)
    async def review(
        prompt: str,
        repo_root: RepoRootParam,
        files: list[str] | None = None,
        important_context: ImportantContextParam = None,
        timeout_seconds: int | None = None,
        extra_roots: list[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        agents: ReviewAgentsParam = None,
        ctx: Context | None = None,
    ) -> ToolResponse:
        """Review code directly with zero or one effective agent, or pass two or more canonical agents to fan out through Codex multi-agent v2. Dobby verifies v2 support and independently correlates each manifest entry with Codex session rollouts. In review_details, manifest_validated proves the fan-out was structurally honest and coverage_complete proves every selected lens completed; neither proves a reviewer received the exact assignment, because the orchestrating parent also holds the digest each child attests. Defaults to 600 seconds, or 1200 seconds for multi-agent review, when timeout_seconds is omitted."""
        # Same default as start_run: a multi-agent fan-out through the same
        # orchestrator needs the same budget whichever entry point launched it.
        request = _request_from_params(
            prompt=prompt,
            repo_root=repo_root,
            files=files,
            important_context=important_context,
            timeout_seconds=_effective_start_run_timeout(
                ToolName.REVIEW, timeout_seconds, agents
            ),
            extra_roots=extra_roots,
            model=model,
            reasoning_effort=reasoning_effort,
            agents=agents,
        )
        return await runner.run(ToolName.REVIEW, request, event_sink=_build_event_sink(ctx))

    @server.tool(name="reverse_engineer", structured_output=True)
    async def reverse_engineer(
        prompt: str,
        repo_root: RepoRootParam,
        files: list[str] | None = None,
        important_context: ImportantContextParam = None,
        timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS[ToolName.REVERSE_ENGINEER],
        extra_roots: list[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        danger: bool = False,
        ctx: Context | None = None,
    ) -> ToolResponse:
        """Use reverse-engineering tooling and broader roots to investigate binaries. Recommended timeout: 30 minutes (1800s)."""
        request = _request_from_params(
            prompt=prompt,
            repo_root=repo_root,
            files=files,
            important_context=important_context,
            timeout_seconds=timeout_seconds,
            extra_roots=extra_roots,
            model=model,
            reasoning_effort=reasoning_effort,
            danger=danger,
        )
        return await runner.run(ToolName.REVERSE_ENGINEER, request, event_sink=_build_event_sink(ctx))

    @server.tool(name="start_run", structured_output=True)
    async def start_run(
        tool: ToolName,
        prompt: str,
        repo_root: RepoRootParam,
        files: list[str] | None = None,
        important_context: ImportantContextParam = None,
        timeout_seconds: int | None = None,
        extra_roots: list[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        agents: ReviewAgentsParam = None,
        danger: bool = False,
        ctx: Context | None = None,
    ) -> AsyncRunHandle:
        """Start a Dobby tool in the background and return immediately with a task id. Wait for this specific call to return before looking up its run; independent start_run calls may execute in parallel. Then use wait_run(task_id=...) for one run or wait_run(task_ids=[...]) to be woken by whichever finishes first. Multi-agent review defaults to 1200 seconds when timeout_seconds is omitted. get_run/list_runs remain available for a non-blocking peek."""
        effective_timeout_seconds = _effective_start_run_timeout(
            tool,
            timeout_seconds,
            agents,
        )
        request = _request_from_params(
            prompt=prompt,
            repo_root=repo_root,
            files=files,
            important_context=important_context,
            timeout_seconds=effective_timeout_seconds,
            extra_roots=extra_roots,
            model=model,
            reasoning_effort=reasoning_effort,
            agents=agents,
            danger=danger,
        )
        spec = runner.prepare(tool, request)
        return background_runs.start(spec)

    @server.tool(name="get_run", structured_output=True)
    async def get_run(
        task_id: str,
        repo_root: RepoRootParam,
        ctx: Context | None = None,
    ) -> RunLookupResponse:
        """Get the status or final ToolResponse for a Dobby run by task id. This can recover results from .codex-dobby/runs even after a blocking tools/call timed out."""
        resolved_repo_root = resolve_repo_root(runner.spawn_root, repo_root)
        return background_runs.get(resolved_repo_root, task_id)

    @server.tool(name="wait_run", structured_output=True)
    async def wait_run(
        repo_root: RepoRootParam,
        task_id: str | None = None,
        task_ids: list[str] | None = None,
        timeout_seconds: int = 540,
        ctx: Context | None = None,
    ) -> RunLookupResponse:
        """Call after start_run to block until a background run finishes. Pass task_id for one run, task_ids=[...] to wait for whichever of several finishes FIRST, or omit both to wait on every currently-live run for the repo. On completion returns the winning run's final ToolResponse (pending_task_ids lists any still-running siblings); on timeout returns a RUNNING lookup whose summary says to keep calling wait_run with pending_task_ids until one finishes. Background tasks are shielded from waiter cancellation, so re-calling never loses work. Default 540s (9 min); clamped to [1, 100_000s / ~27.8h], matching Claude Code's MCP_TOOL_TIMEOUT default. Pick timeout_seconds below your own MCP client's tools/call ceiling — Claude Code defaults to ~28h (the full clamp is usable); Codex CLI defaults to 60s per [mcp_servers.<id>].tool_timeout_sec (raise that first); Claude Desktop / Cursor / Cline / Continue vary."""
        resolved_repo_root = resolve_repo_root(runner.spawn_root, repo_root)
        clamped = max(1, min(int(timeout_seconds), 100_000))
        return await background_runs.wait(
            resolved_repo_root,
            task_id=task_id,
            task_ids=task_ids,
            timeout_seconds=clamped,
            event_sink=_build_wait_event_sink(ctx),
        )

    @server.tool(name="list_runs", structured_output=True)
    async def list_runs(
        repo_root: RepoRootParam,
        limit: int = 10,
        ctx: Context | None = None,
    ) -> RunListResponse:
        """List recent Dobby runs for a repo. Useful for recovering task ids and results after a caller-side timeout."""
        resolved_repo_root = resolve_repo_root(runner.spawn_root, repo_root)
        return background_runs.list(resolved_repo_root, limit=limit)

    _reject_unknown_tool_arguments(server)
    return server


app = create_server()


def main() -> None:
    app.run(transport="stdio")


if __name__ == "__main__":
    main()

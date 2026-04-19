from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import Context, FastMCP
from pydantic import BeforeValidator, Field

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
from codex_dobby_mcp.review_agents import review_agent_assets_root
from codex_dobby_mcp.runner import CodexRunner


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


def _assets_root() -> Path:
    return Path(__file__).resolve().parent / "assets"


def _default_codex_binary() -> str:
    if override := os.environ.get("CODEX_BINARY"):
        return override
    if discovered := shutil.which("codex"):
        return discovered
    return "/opt/homebrew/bin/codex"


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
    important_context: str | None = None,
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


def _caller_repo_root(ctx: Context | None) -> str | None:
    if ctx is None or ctx._request_context is None or ctx.request_context.meta is None:
        return None

    meta = ctx.request_context.meta.model_dump()
    candidate_maps = [meta]
    nested_meta = meta.get("_meta")
    if isinstance(nested_meta, dict):
        candidate_maps.append(nested_meta)

    for mapping in candidate_maps:
        for key in ("repo_root", "repoRoot", "working_directory", "workingDirectory", "cwd"):
            value = mapping.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _resolved_repo_root(runner: CodexRunner, repo_root: str | None, ctx: Context | None) -> Path:
    return resolve_repo_root(runner.spawn_root, repo_root or _caller_repo_root(ctx))


def create_server(spawn_root: Path | None = None) -> FastMCP:
    runner = create_runner(spawn_root)
    background_runs = BackgroundRunManager(runner)
    server = FastMCP(
        "codex-dobby-mcp",
        instructions="Delegate scoped work to codex exec and return concise structured results. Important: short timeouts cause failures. Always use a longer timeout_seconds than you think is needed — the default (900s) is a safe choice. Do not override it lower unless you have a specific reason. For long review, research, build, validate, or reverse_engineer work, prefer start_run plus wait_run (wait_run blocks until done or until its own timeout_seconds elapses, then re-call). If the caller is Claude Code and you want the parent to keep working on other things instead of blocking in wait_run, schedule a poll of get_run via /loop or ScheduleWakeup — the parent wakes itself periodically to check without holding a tool call open. Use get_run/list_runs for a non-blocking peek or to recover a task id after a caller-side timeout.",
    )

    @server.tool(name="plan", structured_output=True)
    async def plan(
        prompt: str,
        repo_root: str | None = None,
        files: list[str] | None = None,
        important_context: str | None = None,
        timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS[ToolName.PLAN],
        extra_roots: list[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        ctx: Context | None = None,
    ) -> ToolResponse:
        """Break down a task and propose a scoped plan without editing files. Recommended timeout: 10 minutes (600s)."""
        request = _request_from_params(
            prompt=prompt,
            repo_root=repo_root or _caller_repo_root(ctx),
            files=files,
            important_context=important_context,
            timeout_seconds=timeout_seconds,
            extra_roots=extra_roots,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        return await runner.run(ToolName.PLAN, request)

    @server.tool(name="research", structured_output=True)
    async def research(
        prompt: str,
        repo_root: str | None = None,
        files: list[str] | None = None,
        important_context: str | None = None,
        timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS[ToolName.RESEARCH],
        extra_roots: list[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        ctx: Context | None = None,
    ) -> ToolResponse:
        """Investigate code, docs, and context in read-only mode and report findings. Recommended timeout: 20 minutes (1200s)."""
        request = _request_from_params(
            prompt=prompt,
            repo_root=repo_root or _caller_repo_root(ctx),
            files=files,
            important_context=important_context,
            timeout_seconds=timeout_seconds,
            extra_roots=extra_roots,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        return await runner.run(ToolName.RESEARCH, request)

    @server.tool(name="brainstorm", structured_output=True)
    async def brainstorm(
        prompt: str,
        repo_root: str | None = None,
        files: list[str] | None = None,
        important_context: str | None = None,
        timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS[ToolName.BRAINSTORM],
        extra_roots: list[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        ctx: Context | None = None,
    ) -> ToolResponse:
        """Evaluate an idea, scope an MVP, and recommend whether it is worth building. Recommended timeout: 10 minutes (600s)."""
        request = _request_from_params(
            prompt=prompt,
            repo_root=repo_root or _caller_repo_root(ctx),
            files=files,
            important_context=important_context,
            timeout_seconds=timeout_seconds,
            extra_roots=extra_roots,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        return await runner.run(ToolName.BRAINSTORM, request)

    @server.tool(name="build", structured_output=True)
    async def build(
        prompt: str,
        repo_root: str | None = None,
        files: list[str] | None = None,
        important_context: str | None = None,
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
            repo_root=repo_root or _caller_repo_root(ctx),
            files=files,
            important_context=important_context,
            timeout_seconds=timeout_seconds,
            extra_roots=extra_roots,
            model=model,
            reasoning_effort=reasoning_effort,
            danger=danger,
        )
        return await runner.run(ToolName.BUILD, request)

    @server.tool(name="validate", structured_output=True)
    async def validate(
        prompt: str,
        repo_root: str | None = None,
        files: list[str] | None = None,
        important_context: str | None = None,
        timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS[ToolName.VALIDATE],
        extra_roots: list[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        ctx: Context | None = None,
    ) -> ToolResponse:
        """Run existing repo validation commands (build, test, lint) and report the results. Recommended timeout: 10 minutes (600s)."""
        request = _request_from_params(
            prompt=prompt,
            repo_root=repo_root or _caller_repo_root(ctx),
            files=files,
            important_context=important_context,
            timeout_seconds=timeout_seconds,
            extra_roots=extra_roots,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        return await runner.run(ToolName.VALIDATE, request)

    @server.tool(name="review", structured_output=True)
    async def review(
        prompt: str,
        repo_root: str | None = None,
        files: list[str] | None = None,
        important_context: str | None = None,
        timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS[ToolName.REVIEW],
        extra_roots: list[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        agents: ReviewAgentsParam = None,
        ctx: Context | None = None,
    ) -> ToolResponse:
        """Review code with one agent (default) or fan out to multiple specialist agents. Recommended timeout: 10 minutes single-agent, 20 minutes multi-agent (pass timeout_seconds=1200 when using multiple agents)."""
        request = _request_from_params(
            prompt=prompt,
            repo_root=repo_root or _caller_repo_root(ctx),
            files=files,
            important_context=important_context,
            timeout_seconds=timeout_seconds,
            extra_roots=extra_roots,
            model=model,
            reasoning_effort=reasoning_effort,
            agents=agents,
        )
        return await runner.run(ToolName.REVIEW, request)

    @server.tool(name="reverse_engineer", structured_output=True)
    async def reverse_engineer(
        prompt: str,
        repo_root: str | None = None,
        files: list[str] | None = None,
        important_context: str | None = None,
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
            repo_root=repo_root or _caller_repo_root(ctx),
            files=files,
            important_context=important_context,
            timeout_seconds=timeout_seconds,
            extra_roots=extra_roots,
            model=model,
            reasoning_effort=reasoning_effort,
            danger=danger,
        )
        return await runner.run(ToolName.REVERSE_ENGINEER, request)

    @server.tool(name="start_run", structured_output=True)
    async def start_run(
        tool: ToolName,
        prompt: str,
        repo_root: str | None = None,
        files: list[str] | None = None,
        important_context: str | None = None,
        timeout_seconds: int | None = None,
        extra_roots: list[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        agents: ReviewAgentsParam = None,
        danger: bool = False,
        ctx: Context | None = None,
    ) -> AsyncRunHandle:
        """Start a Dobby tool in the background and return immediately with a task id. Follow up with wait_run(task_id=...) to block on one run, or call start_run several times and then wait_run(task_ids=[...]) to be woken by whichever finishes first. get_run/list_runs remain available for a non-blocking peek. Recommended when your MCP client enforces short tools/call timeouts."""
        effective_timeout_seconds = timeout_seconds or DEFAULT_TOOL_TIMEOUT_SECONDS[tool]
        request = _request_from_params(
            prompt=prompt,
            repo_root=repo_root or _caller_repo_root(ctx),
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
        repo_root: str | None = None,
        ctx: Context | None = None,
    ) -> RunLookupResponse:
        """Get the status or final ToolResponse for a Dobby run by task id. This can recover results from .codex-dobby/runs even after a blocking tools/call timed out."""
        resolved_repo_root = _resolved_repo_root(runner, repo_root, ctx)
        return background_runs.get(resolved_repo_root, task_id)

    @server.tool(name="wait_run", structured_output=True)
    async def wait_run(
        task_id: str | None = None,
        task_ids: list[str] | None = None,
        repo_root: str | None = None,
        timeout_seconds: int = 540,
        ctx: Context | None = None,
    ) -> RunLookupResponse:
        """Call after start_run to block until a background run finishes. Pass task_id for one run, task_ids=[...] to wait for whichever of several finishes FIRST, or omit both to wait on every currently-live run for the repo. On completion returns the winning run's final ToolResponse (pending_task_ids lists any still-running siblings); on timeout returns a RUNNING lookup whose summary says to keep calling wait_run with pending_task_ids until one finishes. Background tasks are shielded from waiter cancellation, so re-calling never loses work. Default 540s (9 min); clamped to [1, 100_000s / ~27.8h], matching Claude Code's MCP_TOOL_TIMEOUT default. Pick timeout_seconds below your own MCP client's tools/call ceiling — Claude Code defaults to ~28h (the full clamp is usable); Codex CLI defaults to 60s per [mcp_servers.<id>].tool_timeout_sec (raise that first); Claude Desktop / Cursor / Cline / Continue vary."""
        resolved_repo_root = _resolved_repo_root(runner, repo_root, ctx)
        clamped = max(1, min(int(timeout_seconds), 100_000))
        return await background_runs.wait(
            resolved_repo_root,
            task_id=task_id,
            task_ids=task_ids,
            timeout_seconds=clamped,
        )

    @server.tool(name="list_runs", structured_output=True)
    async def list_runs(
        repo_root: str | None = None,
        limit: int = 10,
        ctx: Context | None = None,
    ) -> RunListResponse:
        """List recent Dobby runs for a repo. Useful for recovering task ids and results after a caller-side timeout."""
        resolved_repo_root = _resolved_repo_root(runner, repo_root, ctx)
        return background_runs.list(resolved_repo_root, limit=limit)

    return server


app = create_server()


def main() -> None:
    app.run(transport="stdio")


if __name__ == "__main__":
    main()

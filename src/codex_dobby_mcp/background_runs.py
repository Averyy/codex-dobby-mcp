from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from codex_dobby_mcp.models import (
    AsyncRunHandle,
    AsyncRunState,
    Completeness,
    ResultArtifactState,
    ResolvedInvocation,
    ReviewDetails,
    RunListResponse,
    RunLookupResponse,
    RunStatus,
    RunSummary,
    ToolName,
    ToolResponse,
)
from codex_dobby_mcp.paths import PathResolutionError, run_artifacts_for_task, runs_root_for_repo, write_json
from codex_dobby_mcp.review_agents import selected_review_agents
from codex_dobby_mcp.runner import CodexRunner


@dataclass
class BackgroundRunEntry:
    spec: ResolvedInvocation
    task: asyncio.Task[ToolResponse]


class BackgroundRunManager:
    def __init__(self, runner: CodexRunner):
        self._runner = runner
        self._entries: dict[tuple[str, str], BackgroundRunEntry] = {}

    def start(self, spec: ResolvedInvocation) -> AsyncRunHandle:
        task_id = spec.artifacts.run_dir.name
        key = self._key(spec.repo_root, task_id)
        task = asyncio.create_task(
            self._run_in_background(spec),
            name=f"codex-dobby:{spec.tool.value}:{task_id}",
        )
        self._entries[key] = BackgroundRunEntry(spec=spec, task=task)
        return AsyncRunHandle(
            task_id=task_id,
            tool=spec.tool,
            state=AsyncRunState.RUNNING,
            summary=f"Started background {spec.tool.value} run. Use get_run(task_id=...) to fetch the result.",
            repo_root=str(spec.repo_root),
            artifact_paths=spec.artifacts.as_public_dict(),
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
        )

    def get(self, repo_root: Path, task_id: str) -> RunLookupResponse:
        key = self._key(repo_root, task_id)
        entry = self._entries.get(key)
        if entry is not None:
            if not entry.task.done():
                return RunLookupResponse(
                    task_id=task_id,
                    state=AsyncRunState.RUNNING,
                    summary=f"{entry.spec.tool.value} run is still active.",
                    repo_root=str(repo_root),
                    tool=entry.spec.tool,
                    artifact_paths=entry.spec.artifacts.as_public_dict(),
                )
            with suppress(asyncio.CancelledError):
                return self._lookup_from_result(repo_root, entry.spec.tool, entry.task.result())

        return self._lookup_from_artifacts(repo_root, task_id)

    def list(self, repo_root: Path, limit: int = 10) -> RunListResponse:
        try:
            runs_root = runs_root_for_repo(repo_root)
        except PathResolutionError:
            return RunListResponse(repo_root=str(repo_root), runs=[])
        if not runs_root.exists():
            return RunListResponse(repo_root=str(repo_root), runs=[])

        run_dirs = sorted(
            (path for path in runs_root.iterdir() if path.is_dir() and not path.is_symlink()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        runs: list[RunSummary] = []
        for run_dir in run_dirs[:limit]:
            lookup = self.get(repo_root, run_dir.name)
            runs.append(
                RunSummary(
                    task_id=lookup.task_id,
                    state=lookup.state,
                    summary=lookup.summary,
                    repo_root=lookup.repo_root,
                    tool=lookup.tool,
                    status=lookup.status,
                    result_state=lookup.result_state,
                )
            )
        return RunListResponse(repo_root=str(repo_root), runs=runs)

    async def _run_in_background(self, spec: ResolvedInvocation) -> ToolResponse:
        try:
            return await self._runner.run_resolved(spec)
        except asyncio.CancelledError:
            response = self._background_failure_response(spec, "Background run was cancelled before completion.")
            write_json(spec.artifacts.result_json, response.model_dump(mode="json"))
            return response
        except Exception as exc:
            response = self._background_failure_response(spec, f"Background run failed: {exc}")
            write_json(spec.artifacts.result_json, response.model_dump(mode="json"))
            return response

    def _lookup_from_artifacts(self, repo_root: Path, task_id: str) -> RunLookupResponse:
        try:
            artifacts = run_artifacts_for_task(repo_root, task_id)
        except PathResolutionError:
            return RunLookupResponse(
                task_id=task_id,
                state=AsyncRunState.NOT_FOUND,
                summary="Run not found.",
                repo_root=str(repo_root),
            )
        if not artifacts.run_dir.exists():
            return RunLookupResponse(
                task_id=task_id,
                state=AsyncRunState.NOT_FOUND,
                summary="Run not found.",
                repo_root=str(repo_root),
            )

        result = self._load_result(artifacts.result_json)
        if result is not None:
            if result.result_state == ResultArtifactState.PLACEHOLDER:
                return RunLookupResponse(
                    task_id=task_id,
                    state=AsyncRunState.UNKNOWN,
                    summary="Run directory exists, but only a placeholder result artifact is available; no final result was written.",
                    repo_root=str(repo_root),
                    tool=result.tool,
                    result_state=result.result_state,
                    artifact_paths=result.artifact_paths,
                    warnings=list(result.warnings)
                    + ["Result artifact is still a placeholder; Dobby did not persist a final ToolResponse."],
                )
            return self._lookup_from_result(repo_root, result.tool, result)

        return RunLookupResponse(
            task_id=task_id,
            state=AsyncRunState.UNKNOWN,
            summary="Run directory exists, but no readable result is available yet.",
            repo_root=str(repo_root),
            tool=self._load_tool_name(artifacts.request_json),
            artifact_paths=artifacts.as_public_dict(),
            warnings=["Result artifact is missing or unreadable."],
        )

    def _lookup_from_result(self, repo_root: Path, tool: ToolName, result: ToolResponse) -> RunLookupResponse:
        return RunLookupResponse(
            task_id=result.task_id,
            state=AsyncRunState.FINISHED,
            summary=result.summary,
            repo_root=str(repo_root),
            tool=tool,
            status=result.status,
            result_state=result.result_state,
            artifact_paths=result.artifact_paths,
            result=result,
            warnings=list(result.warnings),
        )

    def _background_failure_response(self, spec: ResolvedInvocation, summary: str) -> ToolResponse:
        return ToolResponse(
            task_id=spec.artifacts.run_dir.name,
            tool=spec.tool,
            status=RunStatus.ERROR,
            summary=summary,
            completeness=Completeness.BLOCKED,
            important_facts=[],
            next_steps=[],
            files_changed=[],
            artifact_paths=spec.artifacts.as_public_dict(),
            sandbox_violations=[],
            repo_root=str(spec.repo_root),
            warnings=[summary],
            raw_output_available=False,
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
            review_details=_review_details_for_spec(spec),
        )

    @staticmethod
    def _key(repo_root: Path, task_id: str) -> tuple[str, str]:
        return (str(repo_root.resolve()), task_id)

    @staticmethod
    def _load_result(path: Path) -> ToolResponse | None:
        if not path.exists():
            return None
        try:
            return ToolResponse.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, json.JSONDecodeError):
            return None

    @staticmethod
    def _load_tool_name(path: Path) -> ToolName | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        raw_tool = payload.get("tool")
        if not isinstance(raw_tool, str):
            return None
        with suppress(ValueError):
            return ToolName(raw_tool)
        return None


def _review_details_for_spec(spec: ResolvedInvocation) -> ReviewDetails | None:
    if spec.tool != ToolName.REVIEW:
        return None
    effective_agents = selected_review_agents(spec.request.agents)
    return ReviewDetails(
        requested_review_agents=list(spec.requested_review_agents),
        effective_review_agents=effective_agents,
    )

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
    GhidraDetails,
    GhidraUsageMode,
    ResultArtifactState,
    ResolvedInvocation,
    ReverseEngineerDetails,
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
        self._entries: dict[tuple[object, str], BackgroundRunEntry] = {}

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

    async def wait(
        self,
        repo_root: Path,
        task_id: str | None,
        task_ids: list[str] | None,
        timeout_seconds: float,
    ) -> RunLookupResponse:
        if task_id is not None and task_ids is not None:
            raise ValueError("Pass either task_id or task_ids, not both.")
        if task_ids is not None and len(task_ids) == 0:
            raise ValueError("task_ids must be non-empty; omit the param to wait on all live runs.")

        if task_id is not None:
            ids: list[str] = [task_id]
        elif task_ids is not None:
            seen: set[str] = set()
            ids = []
            for tid in task_ids:
                if tid not in seen:
                    seen.add(tid)
                    ids.append(tid)
        else:
            ids = self._live_task_ids_for_repo(repo_root)
            if not ids:
                return RunLookupResponse(
                    task_id="",
                    state=AsyncRunState.NOT_FOUND,
                    summary="No live background runs found for this repo.",
                    repo_root=str(repo_root),
                )

        if len(ids) == 1:
            return await self._wait_single(repo_root, ids[0], timeout_seconds)
        return await self._wait_multi(repo_root, ids, timeout_seconds)

    async def _wait_single(
        self, repo_root: Path, task_id: str, timeout_seconds: float
    ) -> RunLookupResponse:
        entry = self._entries.get(self._key(repo_root, task_id))
        if entry is None:
            return self._lookup_from_artifacts(repo_root, task_id)

        if not entry.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(entry.task), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                return RunLookupResponse(
                    task_id=task_id,
                    state=AsyncRunState.RUNNING,
                    summary=(
                        f"{entry.spec.tool.value} run is still active after waiting "
                        f"{timeout_seconds:.0f}s. Keep calling wait_run with the same task_id "
                        "until state is finished (the background task is shielded, so nothing "
                        "is lost by re-calling)."
                    ),
                    repo_root=str(repo_root),
                    tool=entry.spec.tool,
                    artifact_paths=entry.spec.artifacts.as_public_dict(),
                    pending_task_ids=[task_id],
                )

        with suppress(asyncio.CancelledError):
            return self._lookup_from_result(repo_root, entry.spec.tool, entry.task.result())
        return self._lookup_from_artifacts(repo_root, task_id)

    async def _wait_multi(
        self, repo_root: Path, task_ids: list[str], timeout_seconds: float
    ) -> RunLookupResponse:
        still_running: dict[str, BackgroundRunEntry] = {}
        for tid in task_ids:
            entry = self._entries.get(self._key(repo_root, tid))
            if entry is None:
                continue
            if entry.task.done():
                with suppress(asyncio.CancelledError):
                    lookup = self._lookup_from_result(repo_root, entry.spec.tool, entry.task.result())
                    return lookup.model_copy(
                        update={"pending_task_ids": [t for t in task_ids if t != tid]}
                    )
            else:
                still_running[tid] = entry

        for tid in task_ids:
            if tid in still_running:
                continue
            artifact_lookup = self._lookup_from_artifacts(repo_root, tid)
            if artifact_lookup.state == AsyncRunState.FINISHED:
                return artifact_lookup.model_copy(
                    update={"pending_task_ids": [t for t in task_ids if t != tid]}
                )

        if not still_running:
            tid = task_ids[0]
            return self._lookup_from_artifacts(repo_root, tid).model_copy(
                update={"pending_task_ids": [t for t in task_ids if t != tid]}
            )

        shielded = {asyncio.shield(entry.task): tid for tid, entry in still_running.items()}
        try:
            done, _ = await asyncio.wait(
                shielded.keys(),
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for fut in shielded:
                if not fut.done():
                    fut.cancel()

        if done:
            winning_future = next(iter(done))
            winning_id = shielded[winning_future]
            entry = still_running[winning_id]
            with suppress(asyncio.CancelledError):
                lookup = self._lookup_from_result(repo_root, entry.spec.tool, entry.task.result())
                return lookup.model_copy(
                    update={"pending_task_ids": [t for t in task_ids if t != winning_id]}
                )
            return self._lookup_from_artifacts(repo_root, winning_id).model_copy(
                update={"pending_task_ids": [t for t in task_ids if t != winning_id]}
            )

        primary = next(iter(still_running))
        primary_entry = still_running[primary]
        return RunLookupResponse(
            task_id=primary,
            state=AsyncRunState.RUNNING,
            summary=(
                f"{len(still_running)} of {len(task_ids)} runs still active after waiting "
                f"{timeout_seconds:.0f}s. Keep calling wait_run with "
                "task_ids=pending_task_ids until one finishes (background tasks are shielded, "
                "so nothing is lost by re-calling). Or use get_run per id for a non-blocking peek."
            ),
            repo_root=str(repo_root),
            tool=primary_entry.spec.tool,
            artifact_paths=primary_entry.spec.artifacts.as_public_dict(),
            pending_task_ids=list(still_running.keys()),
        )

    def _live_task_ids_for_repo(self, repo_root: Path) -> list[str]:
        target = self._repo_key(repo_root)
        return [
            entry.spec.artifacts.run_dir.name
            for (root, _task_id), entry in self._entries.items()
            if root == target and not entry.task.done()
        ]

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
            reverse_engineer_details=_reverse_engineer_details_for_background_failure(spec),
        )

    @staticmethod
    def _repo_key(repo_root: Path) -> object:
        resolved = repo_root.resolve()
        try:
            stat = resolved.stat()
        except OSError:
            return str(resolved)
        return (stat.st_dev, stat.st_ino)

    @staticmethod
    def _key(repo_root: Path, task_id: str) -> tuple[object, str]:
        return (BackgroundRunManager._repo_key(repo_root), task_id)

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


def _reverse_engineer_details_for_background_failure(spec: ResolvedInvocation) -> ReverseEngineerDetails | None:
    if spec.tool != ToolName.REVERSE_ENGINEER:
        return None
    if not spec.ghidra_available:
        return ReverseEngineerDetails(
            ghidra=GhidraDetails(
                configured=False,
                mode=GhidraUsageMode.NOT_CONFIGURED,
                summary="Ghidra integration was not configured for this run.",
            )
        )
    return ReverseEngineerDetails(
        ghidra=GhidraDetails(
            configured=True,
            mode=GhidraUsageMode.PRELAUNCH_FAILURE,
            summary="Ghidra was configured, but Dobby failed before any child Ghidra activity could run.",
        )
    )

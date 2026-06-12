from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
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
    StopReason,
    ToolName,
    ToolResponse,
)
from codex_dobby_mcp.paths import PathResolutionError, run_artifacts_for_task, runs_root_for_repo, write_json
from codex_dobby_mcp.review_agents import selected_review_agents
from codex_dobby_mcp.runner import CodexRunner


SubscriberQueue = "asyncio.Queue[tuple[dict, int] | None]"
WaitRunSink = Callable[[dict, int, str], Awaitable[None]]


@dataclass
class BackgroundRunEntry:
    spec: ResolvedInvocation
    task: asyncio.Task[ToolResponse]
    subscribers: list["asyncio.Queue"] = field(default_factory=list)


class BackgroundRunManager:
    def __init__(self, runner: CodexRunner):
        self._runner = runner
        self._entries: dict[tuple[object, str], BackgroundRunEntry] = {}

    def start(self, spec: ResolvedInvocation) -> AsyncRunHandle:
        task_id = spec.artifacts.run_dir.name
        key = self._key(spec.repo_root, task_id)

        # Sink resolves the entry from the manager registry at call time,
        # avoiding the chicken-and-egg between entry, task, and sink. The
        # task can't run before the synchronous code below registers the
        # entry under ``key``, so the lookup is always populated when the
        # sink fires.
        async def broadcast_sink(event: dict, count: int) -> None:
            entry = self._entries.get(key)
            if entry is None:
                return
            for queue in list(entry.subscribers):
                try:
                    queue.put_nowait((event, count))
                except asyncio.QueueFull:
                    # Drop on full queue rather than blocking the run; the
                    # subscriber can fall back to events.jsonl for the missed
                    # events.
                    pass

        task = asyncio.create_task(
            self._run_in_background(spec, event_sink=broadcast_sink),
            name=f"codex-dobby:{spec.tool.value}:{task_id}",
        )
        entry = BackgroundRunEntry(spec=spec, task=task)
        self._entries[key] = entry
        task.add_done_callback(lambda _t, _e=entry: self._notify_subscribers_done(_e))
        return AsyncRunHandle(
            task_id=task_id,
            tool=spec.tool,
            state=AsyncRunState.RUNNING,
            summary=(
                f"Started background {spec.tool.value} run {task_id}. "
                f"Call wait_run(task_id='{task_id}') to block until it finishes, or "
                f"get_run(task_id='{task_id}') for a non-blocking peek. This run is "
                "managed only by the codex-dobby wait_run/get_run/list_runs tools."
            ),
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
                    summary=(
                        f"{entry.spec.tool.value} run {task_id} is still running. "
                        f"Call wait_run(task_id='{task_id}') to block until it finishes, or "
                        "get_run again later for another non-blocking check."
                    ),
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
        *,
        event_sink: WaitRunSink | None = None,
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
                    summary=(
                        "No live background runs found for this repo. After a server "
                        "restart, runs survive only as artifacts, not live tasks — call "
                        "list_runs to see recent runs and get_run(task_id=...) to fetch "
                        "one by id."
                    ),
                    repo_root=str(repo_root),
                )

        if len(ids) == 1:
            return await self._wait_single(repo_root, ids[0], timeout_seconds, event_sink=event_sink)
        return await self._wait_multi(repo_root, ids, timeout_seconds, event_sink=event_sink)

    async def _wait_single(
        self,
        repo_root: Path,
        task_id: str,
        timeout_seconds: float,
        *,
        event_sink: WaitRunSink | None = None,
    ) -> RunLookupResponse:
        entry = self._entries.get(self._key(repo_root, task_id))
        if entry is None:
            return self._lookup_from_artifacts(repo_root, task_id)

        forward_task: asyncio.Task | None = None
        subscriber_queue: asyncio.Queue | None = None
        if event_sink is not None and not entry.task.done():
            subscriber_queue = asyncio.Queue(maxsize=1024)
            entry.subscribers.append(subscriber_queue)
            forward_task = asyncio.create_task(
                _forward_events_until_done(subscriber_queue, event_sink, task_id),
                name=f"codex-dobby-wait-forward:{task_id}",
            )

        try:
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
        finally:
            if subscriber_queue is not None and entry is not None and subscriber_queue in entry.subscribers:
                entry.subscribers.remove(subscriber_queue)
                with suppress(asyncio.QueueFull):
                    subscriber_queue.put_nowait(None)
            if forward_task is not None and not forward_task.done():
                forward_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await forward_task

    async def _wait_multi(
        self,
        repo_root: Path,
        task_ids: list[str],
        timeout_seconds: float,
        *,
        event_sink: WaitRunSink | None = None,
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

        forward_tasks: list[asyncio.Task] = []
        subscriber_queues: list[tuple[BackgroundRunEntry, asyncio.Queue]] = []
        if event_sink is not None:
            for tid, run_entry in still_running.items():
                queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
                run_entry.subscribers.append(queue)
                subscriber_queues.append((run_entry, queue))
                forward_tasks.append(
                    asyncio.create_task(
                        _forward_events_until_done(queue, event_sink, tid),
                        name=f"codex-dobby-wait-forward:{tid}",
                    )
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
            for run_entry, queue in subscriber_queues:
                if queue in run_entry.subscribers:
                    run_entry.subscribers.remove(queue)
                with suppress(asyncio.QueueFull):
                    queue.put_nowait(None)
            for forward_task in forward_tasks:
                if not forward_task.done():
                    forward_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await forward_task

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
                    stop_reason=lookup.stop_reason,
                )
            )
        return RunListResponse(repo_root=str(repo_root), runs=runs)

    async def _run_in_background(
        self,
        spec: ResolvedInvocation,
        *,
        event_sink=None,
    ) -> ToolResponse:
        try:
            return await self._runner.run_resolved(spec, event_sink=event_sink)
        except asyncio.CancelledError:
            response = self._background_failure_response(
                spec,
                "Background run was cancelled before completion.",
                stop_reason=StopReason.CANCELLED,
            )
            write_json(spec.artifacts.result_json, response.model_dump(mode="json", by_alias=True))
            return response
        except Exception as exc:
            response = self._background_failure_response(
                spec,
                f"Background run failed: {exc}",
                stop_reason=StopReason.ERROR,
            )
            write_json(spec.artifacts.result_json, response.model_dump(mode="json", by_alias=True))
            return response

    @staticmethod
    def _notify_subscribers_done(entry: BackgroundRunEntry) -> None:
        for queue in list(entry.subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        entry.subscribers.clear()

    def _lookup_from_artifacts(self, repo_root: Path, task_id: str) -> RunLookupResponse:
        try:
            artifacts = run_artifacts_for_task(repo_root, task_id)
        except PathResolutionError:
            return RunLookupResponse(
                task_id=task_id,
                state=AsyncRunState.NOT_FOUND,
                summary=(
                    f"Run not found: could not resolve a runs directory for repo_root "
                    f"{repo_root}. Lookups are keyed by repo_root + task_id; check the "
                    "repo_root matches the one the run was started with."
                ),
                repo_root=str(repo_root),
            )
        if not artifacts.run_dir.exists():
            return RunLookupResponse(
                task_id=task_id,
                state=AsyncRunState.NOT_FOUND,
                summary=(
                    f"Run {task_id} not found under this repo. Lookups are keyed by "
                    "repo_root + task_id — call list_runs to see recent runs for this "
                    "repo, or check the repo_root matches the one the run was started with."
                ),
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
            stop_reason=result.stop_reason,
            artifact_paths=result.artifact_paths,
            result=result,
            warnings=list(result.warnings),
        )

    def _background_failure_response(
        self,
        spec: ResolvedInvocation,
        summary: str,
        *,
        stop_reason: StopReason = StopReason.ERROR,
    ) -> ToolResponse:
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
            stop_reason=stop_reason,
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


async def _forward_events_until_done(
    queue: "asyncio.Queue",
    sink: WaitRunSink,
    task_id: str,
) -> None:
    """Pull (event, count) tuples off ``queue`` and forward them to ``sink``.

    Stops when a ``None`` sentinel arrives (run finished or wait_run unsubscribed)
    or the surrounding task is cancelled. Sink errors are swallowed so a
    flaky client cannot kill the forwarder mid-run.
    """
    try:
        while True:
            payload = await queue.get()
            if payload is None:
                return
            event, count = payload
            try:
                await sink(event, count, task_id)
            except Exception:
                pass
    except asyncio.CancelledError:
        return


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

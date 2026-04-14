import asyncio
from pathlib import Path

import pytest

from codex_dobby_mcp.background_runs import BackgroundRunManager
from codex_dobby_mcp.models import (
    AsyncRunState,
    Completeness,
    InvocationRequest,
    ReasoningEffort,
    ResultArtifactState,
    ResolvedInvocation,
    RunArtifacts,
    RunStatus,
    ToolName,
    ToolResponse,
)
from codex_dobby_mcp.paths import create_run_artifacts, write_json


def _artifacts(repo_root: Path, task_id: str) -> RunArtifacts:
    return create_run_artifacts(repo_root, task_id=task_id)


def _spec(repo_root: Path, task_id: str) -> ResolvedInvocation:
    artifacts = _artifacts(repo_root, task_id)
    return ResolvedInvocation(
        tool=ToolName.REVIEW,
        request=InvocationRequest(prompt="review it"),
        requested_timeout_seconds=600,
        repo_root=repo_root,
        model="gpt-5.4-mini",
        reasoning_effort=ReasoningEffort.HIGH,
        sandbox_roots=[repo_root],
        writable_roots=[repo_root],
        advisory_read_only_roots=[],
        artifacts=artifacts,
        gitignore_updated=False,
    )


def _result(spec: ResolvedInvocation, summary: str = "finished") -> ToolResponse:
    return ToolResponse(
        task_id=spec.artifacts.run_dir.name,
        tool=spec.tool,
        status=RunStatus.SUCCESS,
        summary=summary,
        completeness=Completeness.FULL,
        important_facts=[],
        next_steps=[],
        files_changed=[],
        artifact_paths=spec.artifacts.as_public_dict(),
        sandbox_violations=[],
        repo_root=str(spec.repo_root),
        warnings=[],
        raw_output_available=True,
        model=spec.model,
        reasoning_effort=spec.reasoning_effort,
    )


@pytest.mark.asyncio
async def test_background_run_manager_returns_finished_result_for_live_entry(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    spec = _spec(repo_root, "live-task")

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:
            return _result(resolved, summary="live result")

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    handle = manager.start(spec)

    assert handle.state == AsyncRunState.RUNNING
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    lookup = manager.get(repo_root, spec.artifacts.run_dir.name)

    assert lookup.state == AsyncRunState.FINISHED
    assert lookup.result is not None
    assert lookup.result.summary == "live result"
    assert lookup.status == RunStatus.SUCCESS
    assert lookup.result_state == ResultArtifactState.FINAL


def test_background_run_manager_can_recover_result_from_artifacts(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    spec = _spec(repo_root, "artifact-task")
    result = _result(spec, summary="artifact result")
    write_json(spec.artifacts.result_json, result.model_dump(mode="json"))

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:  # pragma: no cover
            return _result(resolved)

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    lookup = manager.get(repo_root, spec.artifacts.run_dir.name)

    assert lookup.state == AsyncRunState.FINISHED
    assert lookup.result is not None
    assert lookup.result.summary == "artifact result"
    assert lookup.result_state == ResultArtifactState.FINAL


def test_background_run_manager_treats_placeholder_result_as_unknown(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    spec = _spec(repo_root, "placeholder-task")
    placeholder = _result(spec, summary="placeholder only").model_copy(
        update={"result_state": ResultArtifactState.PLACEHOLDER}
    )
    write_json(spec.artifacts.result_json, placeholder.model_dump(mode="json"))

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:  # pragma: no cover
            return _result(resolved)

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    lookup = manager.get(repo_root, spec.artifacts.run_dir.name)

    assert lookup.state == AsyncRunState.UNKNOWN
    assert lookup.result is None
    assert lookup.status is None
    assert lookup.result_state == ResultArtifactState.PLACEHOLDER
    assert "placeholder result artifact" in lookup.summary


def test_background_run_manager_treats_traversal_task_id_as_not_found(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:  # pragma: no cover
            return _result(resolved)

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    lookup = manager.get(repo_root, "../../outside")

    assert lookup.state == AsyncRunState.NOT_FOUND
    assert lookup.summary == "Run not found."
    assert lookup.result is None


def test_background_run_manager_ignores_symlinked_runs_root_when_listing(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    artifacts_root = repo_root / ".codex-dobby"
    artifacts_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escaped-task").mkdir()
    (artifacts_root / "runs").symlink_to(outside, target_is_directory=True)

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:  # pragma: no cover
            return _result(resolved)

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    listing = manager.list(repo_root, limit=10)

    assert listing.repo_root == str(repo_root)
    assert listing.runs == []


def test_background_run_manager_lists_recent_runs(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    first = _spec(repo_root, "task-one")
    second = _spec(repo_root, "task-two")
    write_json(first.artifacts.result_json, _result(first, summary="first").model_dump(mode="json"))
    write_json(second.artifacts.result_json, _result(second, summary="second").model_dump(mode="json"))

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:  # pragma: no cover
            return _result(resolved)

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    listing = manager.list(repo_root, limit=10)

    assert listing.repo_root == str(repo_root)
    assert {run.task_id for run in listing.runs} == {"task-one", "task-two"}

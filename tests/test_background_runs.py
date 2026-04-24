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
        model="gpt-5.5",
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


@pytest.mark.asyncio
async def test_background_run_manager_wait_returns_finished_result(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    spec = _spec(repo_root, "wait-finish")

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:
            await asyncio.sleep(0.05)
            return _result(resolved, summary="waited result")

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    manager.start(spec)

    lookup = await manager.wait(
        repo_root, task_id=spec.artifacts.run_dir.name, task_ids=None, timeout_seconds=5
    )

    assert lookup.state == AsyncRunState.FINISHED
    assert lookup.result is not None
    assert lookup.result.summary == "waited result"


@pytest.mark.asyncio
async def test_background_run_manager_wait_returns_running_on_timeout(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    spec = _spec(repo_root, "wait-timeout")

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:
            await asyncio.sleep(5)
            return _result(resolved)

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    manager.start(spec)

    lookup = await manager.wait(
        repo_root, task_id=spec.artifacts.run_dir.name, task_ids=None, timeout_seconds=0.05
    )

    assert lookup.state == AsyncRunState.RUNNING
    assert lookup.result is None
    assert "still active" in lookup.summary
    assert "Keep calling wait_run" in lookup.summary
    assert lookup.pending_task_ids == [spec.artifacts.run_dir.name]


@pytest.mark.asyncio
async def test_background_run_manager_wait_finds_entry_on_case_insensitive_fs(tmp_path: Path) -> None:
    repo_root = tmp_path / "Repo"
    repo_root.mkdir()
    case_variant = Path(str(repo_root).replace("Repo", "repo", 1))
    if not case_variant.exists() or case_variant.stat().st_ino != repo_root.stat().st_ino:
        pytest.skip("Requires a case-insensitive filesystem (e.g., default macOS APFS)")

    spec = _spec(repo_root, "case-mismatch")

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:
            await asyncio.sleep(0.05)
            return _result(resolved, summary="case-insensitive waited")

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    manager.start(spec)

    lookup = await manager.wait(
        case_variant, task_id=spec.artifacts.run_dir.name, task_ids=None, timeout_seconds=5
    )

    assert lookup.state == AsyncRunState.FINISHED
    assert lookup.result is not None
    assert lookup.result.summary == "case-insensitive waited"


@pytest.mark.asyncio
async def test_background_run_manager_wait_falls_back_to_artifacts(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    spec = _spec(repo_root, "wait-artifact")
    write_json(spec.artifacts.result_json, _result(spec, summary="on disk").model_dump(mode="json"))

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:  # pragma: no cover
            return _result(resolved)

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    lookup = await manager.wait(
        repo_root, task_id=spec.artifacts.run_dir.name, task_ids=None, timeout_seconds=5
    )

    assert lookup.state == AsyncRunState.FINISHED
    assert lookup.result is not None
    assert lookup.result.summary == "on disk"


@pytest.mark.asyncio
async def test_background_run_manager_wait_multi_returns_first_to_finish(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    fast = _spec(repo_root, "fast")
    slow = _spec(repo_root, "slow")

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:
            delay = 0.02 if resolved.artifacts.run_dir.name == "fast" else 5.0
            await asyncio.sleep(delay)
            return _result(resolved, summary=resolved.artifacts.run_dir.name)

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    manager.start(fast)
    manager.start(slow)

    lookup = await manager.wait(
        repo_root, task_id=None, task_ids=["slow", "fast"], timeout_seconds=5
    )

    assert lookup.state == AsyncRunState.FINISHED
    assert lookup.result is not None
    assert lookup.result.summary == "fast"
    assert lookup.pending_task_ids == ["slow"]


@pytest.mark.asyncio
async def test_background_run_manager_wait_multi_timeout_reports_all_pending(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    a = _spec(repo_root, "alpha")
    b = _spec(repo_root, "beta")

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:
            await asyncio.sleep(5)
            return _result(resolved)

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    manager.start(a)
    manager.start(b)

    lookup = await manager.wait(
        repo_root, task_id=None, task_ids=["alpha", "beta"], timeout_seconds=0.05
    )

    assert lookup.state == AsyncRunState.RUNNING
    assert lookup.result is None
    assert set(lookup.pending_task_ids) == {"alpha", "beta"}
    assert "Keep calling wait_run" in lookup.summary
    assert "pending_task_ids" in lookup.summary


@pytest.mark.asyncio
async def test_background_run_manager_wait_all_live_defaults_to_running_runs(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    fast = _spec(repo_root, "only-fast")
    slow = _spec(repo_root, "only-slow")

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:
            delay = 0.02 if "fast" in resolved.artifacts.run_dir.name else 5.0
            await asyncio.sleep(delay)
            return _result(resolved, summary=resolved.artifacts.run_dir.name)

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    manager.start(fast)
    manager.start(slow)

    lookup = await manager.wait(repo_root, task_id=None, task_ids=None, timeout_seconds=5)

    assert lookup.state == AsyncRunState.FINISHED
    assert lookup.result is not None
    assert lookup.result.summary == "only-fast"
    assert lookup.pending_task_ids == ["only-slow"]


@pytest.mark.asyncio
async def test_background_run_manager_wait_all_live_returns_not_found_when_empty(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:  # pragma: no cover
            return _result(resolved)

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    lookup = await manager.wait(repo_root, task_id=None, task_ids=None, timeout_seconds=5)

    assert lookup.state == AsyncRunState.NOT_FOUND
    assert "No live background runs" in lookup.summary


@pytest.mark.asyncio
async def test_background_run_manager_wait_multi_skips_stale_ids_for_live_finished(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    # "stale" is never started, so it has no registry entry and no artifact directory.
    live = _spec(repo_root, "live-fast")

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:
            await asyncio.sleep(0.02)
            return _result(resolved, summary="finished live")

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    manager.start(live)

    lookup = await manager.wait(
        repo_root, task_id=None, task_ids=["stale", "live-fast"], timeout_seconds=5
    )

    assert lookup.state == AsyncRunState.FINISHED
    assert lookup.result is not None
    assert lookup.result.summary == "finished live"
    assert lookup.pending_task_ids == ["stale"]


@pytest.mark.asyncio
async def test_background_run_manager_wait_multi_stale_with_artifact_wins_over_live_running(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    stale = _spec(repo_root, "stale-finished")
    write_json(
        stale.artifacts.result_json, _result(stale, summary="on disk winner").model_dump(mode="json")
    )
    live = _spec(repo_root, "live-slow")

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:
            await asyncio.sleep(5)
            return _result(resolved)

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    manager.start(live)

    lookup = await manager.wait(
        repo_root,
        task_id=None,
        task_ids=["live-slow", "stale-finished"],
        timeout_seconds=5,
    )

    assert lookup.state == AsyncRunState.FINISHED
    assert lookup.result is not None
    assert lookup.result.summary == "on disk winner"
    assert lookup.pending_task_ids == ["live-slow"]


@pytest.mark.asyncio
async def test_background_run_manager_wait_multi_timeout_excludes_stale_from_pending(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    # "stale" has no entry and no artifacts. live-a is running.
    live_a = _spec(repo_root, "live-a")

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:
            await asyncio.sleep(5)
            return _result(resolved)

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    manager.start(live_a)

    lookup = await manager.wait(
        repo_root, task_id=None, task_ids=["stale", "live-a"], timeout_seconds=0.05
    )

    assert lookup.state == AsyncRunState.RUNNING
    assert lookup.pending_task_ids == ["live-a"]
    assert "stale" not in lookup.pending_task_ids


@pytest.mark.asyncio
async def test_background_run_manager_wait_multi_all_stale_returns_first_artifact_lookup(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:  # pragma: no cover
            return _result(resolved)

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    lookup = await manager.wait(
        repo_root, task_id=None, task_ids=["ghost-a", "ghost-b"], timeout_seconds=5
    )

    assert lookup.state == AsyncRunState.NOT_FOUND
    assert lookup.task_id == "ghost-a"
    assert lookup.pending_task_ids == ["ghost-b"]


@pytest.mark.asyncio
async def test_background_run_manager_wait_dedupes_repeated_task_ids(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    spec = _spec(repo_root, "dup-task")

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:
            await asyncio.sleep(0.02)
            return _result(resolved, summary="dup-result")

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    manager.start(spec)

    lookup = await manager.wait(
        repo_root, task_id=None, task_ids=["dup-task", "dup-task"], timeout_seconds=5
    )

    assert lookup.state == AsyncRunState.FINISHED
    assert lookup.result is not None
    assert lookup.result.summary == "dup-result"
    assert lookup.pending_task_ids == []


@pytest.mark.asyncio
async def test_background_run_manager_wait_survives_waiter_cancellation(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    spec = _spec(repo_root, "shielded")

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:
            await asyncio.sleep(0.2)
            return _result(resolved, summary="survived")

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    manager.start(spec)

    waiter = asyncio.create_task(
        manager.wait(repo_root, task_id="shielded", task_ids=None, timeout_seconds=5)
    )
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    follow_up = await manager.wait(
        repo_root, task_id="shielded", task_ids=None, timeout_seconds=5
    )
    assert follow_up.state == AsyncRunState.FINISHED
    assert follow_up.result is not None
    assert follow_up.result.summary == "survived"


@pytest.mark.asyncio
async def test_background_run_manager_wait_multi_survives_waiter_cancellation(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    a = _spec(repo_root, "multi-a")
    b = _spec(repo_root, "multi-b")

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:
            await asyncio.sleep(0.2)
            return _result(resolved, summary=resolved.artifacts.run_dir.name)

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    manager.start(a)
    manager.start(b)

    waiter = asyncio.create_task(
        manager.wait(
            repo_root, task_id=None, task_ids=["multi-a", "multi-b"], timeout_seconds=5
        )
    )
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    follow_up = await manager.wait(
        repo_root, task_id=None, task_ids=["multi-a", "multi-b"], timeout_seconds=5
    )
    assert follow_up.state == AsyncRunState.FINISHED
    assert follow_up.result is not None
    assert follow_up.result.summary in {"multi-a", "multi-b"}


@pytest.mark.asyncio
async def test_background_run_manager_wait_rejects_conflicting_inputs(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    class FakeRunner:
        async def run_resolved(self, resolved: ResolvedInvocation) -> ToolResponse:  # pragma: no cover
            return _result(resolved)

    manager = BackgroundRunManager(FakeRunner())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await manager.wait(repo_root, task_id="x", task_ids=["y"], timeout_seconds=1)
    with pytest.raises(ValueError):
        await manager.wait(repo_root, task_id=None, task_ids=[], timeout_seconds=1)


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

"""Tests for ACP-borrowed fields on ToolResponse: stop_reason and file_diffs."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from codex_dobby_mcp.models import (
    AsyncRunState,
    InvocationRequest,
    ResultArtifactState,
    RunStatus,
    StopReason,
    ToolName,
)
from codex_dobby_mcp.review_agents import review_agents_root
from codex_dobby_mcp.runner import CodexRunner, _build_repo_snapshot


@pytest.fixture(autouse=True)
def stub_snapshot_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_capture(repo_root: Path, deadline: float, *, include_head: bool, use_metadata_fingerprints: bool):
        _ = deadline
        return _build_repo_snapshot(
            repo_root,
            include_head=include_head,
            use_metadata_fingerprints=use_metadata_fingerprints,
        )

    monkeypatch.setattr("codex_dobby_mcp.runner._capture_repo_snapshot_with_deadline", fake_capture)


@pytest.fixture(autouse=True)
def writable_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home = tmp_path / "codex-home"
    (codex_home / "sessions").mkdir(parents=True)
    (codex_home / "auth.json").write_text('{"auth_mode":"chatgpt"}\n', encoding="utf-8")
    (codex_home / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))


def _make_repo(tmp_path: Path, *, with_initial_commit: bool = True) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    subprocess.run(["git", "-C", str(repo_root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_root), "config", "user.name", "test"], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")
    if with_initial_commit:
        subprocess.run(["git", "-C", str(repo_root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(repo_root), "commit", "-q", "-m", "initial"], check=True
        )
    return repo_root


def _make_runner(repo_root: Path) -> CodexRunner:
    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    return CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )


class _FakeProcess:
    """Minimal subprocess stand-in used by these tests.

    The simple stdin/stdout/stderr-less surface routes the runner into its
    non-streaming `communicate()` branch, which is enough to drive the
    response builder.
    """

    def __init__(self, *, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
        return (b"", self._stderr)

    def kill(self) -> None:
        self.returncode = -9


def _worker_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "summary": "done",
        "completeness": "full",
        "important_facts": [],
        "next_steps": [],
        "files_changed": [],
        "warnings": [],
    }
    payload.update(overrides)
    return payload


def _install_fake_codex(
    monkeypatch: pytest.MonkeyPatch,
    *,
    worker_payload: dict[str, object] | None = None,
    side_effect=None,
    returncode: int = 0,
    stderr: bytes = b"",
) -> None:
    payload = _worker_payload() if worker_payload is None else worker_payload

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        if side_effect is not None:
            side_effect(args, kwargs)
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return _FakeProcess(returncode=returncode, stderr=stderr)

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)


# --- stop_reason ----------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_reason_end_turn_on_clean_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = _make_repo(tmp_path)
    runner = _make_runner(repo_root)
    _install_fake_codex(monkeypatch)

    result = await runner.run(ToolName.PLAN, InvocationRequest(prompt="inspect"))

    assert result.status == RunStatus.SUCCESS
    assert result.stop_reason == StopReason.END_TURN


@pytest.mark.asyncio
async def test_stop_reason_refusal_when_worker_signals_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo(tmp_path)
    runner = _make_runner(repo_root)
    _install_fake_codex(
        monkeypatch,
        worker_payload=_worker_payload(refused=True, summary="cannot do that"),
    )

    result = await runner.run(ToolName.PLAN, InvocationRequest(prompt="something"))

    assert result.status == RunStatus.SUCCESS
    assert result.stop_reason == StopReason.REFUSAL


@pytest.mark.asyncio
async def test_stop_reason_error_on_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo(tmp_path)
    runner = _make_runner(repo_root)

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        # do not write last_message — codex failed before producing one
        return _FakeProcess(returncode=2, stderr=b"boom\n")

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)

    result = await runner.run(ToolName.PLAN, InvocationRequest(prompt="inspect"))

    assert result.status == RunStatus.ERROR
    assert result.stop_reason == StopReason.ERROR


@pytest.mark.asyncio
async def test_stop_reason_cancelled_in_placeholder_stub(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    runner = _make_runner(repo_root)

    async def cancel_immediately(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise asyncio.CancelledError()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("codex_dobby_mcp.runner._create_process_with_deadline", cancel_immediately)
    try:
        with pytest.raises(asyncio.CancelledError):
            await runner.run(ToolName.PLAN, InvocationRequest(prompt="inspect"))
    finally:
        monkeypatch.undo()

    runs_root = repo_root / ".codex-dobby" / "runs"
    run_dirs = list(runs_root.iterdir())
    assert len(run_dirs) == 1
    stub = json.loads((run_dirs[0] / "result.json").read_text(encoding="utf-8"))
    assert stub["result_state"] == ResultArtifactState.PLACEHOLDER.value
    assert stub["stop_reason"] == StopReason.CANCELLED.value


@pytest.mark.asyncio
async def test_readonly_external_worktree_changes_are_warning_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """External edits during a read-only run are NOT a sandbox violation.

    Codex's OS-level read-only sandbox blocks codex writes; if the wrapper
    still sees worktree changes it almost always means the user (or another
    process) edited files in the repo while the long-running tool was in
    flight. That is informational, not a failure.
    """
    repo_root = _make_repo(tmp_path)
    runner = _make_runner(repo_root)

    def touch_file_during_run(_args, _kwargs):
        # Simulates the user editing repo files in another terminal mid-run.
        (repo_root / "snuck-in.txt").write_text("oops\n", encoding="utf-8")

    # Worker output reports no files_changed (it's a read-only research run)
    # so any wrapper-detected changes must have come from outside codex.
    _install_fake_codex(monkeypatch, side_effect=touch_file_during_run)
    result = await runner.run(ToolName.PLAN, InvocationRequest(prompt="inspect"))

    assert result.status == RunStatus.SUCCESS
    assert result.stop_reason == StopReason.END_TURN
    assert any(
        "External worktree changes observed during the run" in w
        for w in result.warnings
    ), f"expected external-changes warning, got: {result.warnings}"


@pytest.mark.asyncio
async def test_readonly_with_worker_claimed_changes_is_sandbox_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the worker itself reports it modified files in a read-only run,
    that IS a sandbox violation — wrapper-detected change overlapping
    worker-reported change escalates to ERROR."""
    repo_root = _make_repo(tmp_path)
    runner = _make_runner(repo_root)

    def write_and_claim(_args, _kwargs):
        (repo_root / "claimed.txt").write_text("worker did this\n", encoding="utf-8")

    payload = _worker_payload(files_changed=["claimed.txt"])
    _install_fake_codex(monkeypatch, side_effect=write_and_claim, worker_payload=payload)
    result = await runner.run(ToolName.PLAN, InvocationRequest(prompt="inspect"))

    assert result.status == RunStatus.ERROR
    assert result.stop_reason == StopReason.SANDBOX_VIOLATION


@pytest.mark.asyncio
async def test_background_failure_response_sets_stop_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BackgroundRunManager fallback for a runner exception must populate
    stop_reason — otherwise wait_run callers see status=error/stop_reason=None."""
    from codex_dobby_mcp.background_runs import BackgroundRunManager

    repo_root = _make_repo(tmp_path)
    runner = _make_runner(repo_root)
    manager = BackgroundRunManager(runner)

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated codex spawn failure")

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)

    spec = runner.prepare(ToolName.PLAN, InvocationRequest(prompt="inspect"))
    handle = manager.start(spec)
    lookup = await manager.wait(
        spec.repo_root,
        task_id=handle.task_id,
        task_ids=None,
        timeout_seconds=10.0,
    )

    assert lookup.state == AsyncRunState.FINISHED
    assert lookup.status == RunStatus.ERROR
    assert lookup.stop_reason == StopReason.ERROR
    assert lookup.result is not None
    assert lookup.result.stop_reason == StopReason.ERROR


@pytest.mark.asyncio
async def test_stop_reason_field_is_optional_on_legacy_responses() -> None:
    """RunLookupResponse must validate when stop_reason is absent (older artifacts)."""
    from codex_dobby_mcp.models import AsyncRunState, RunLookupResponse

    payload = {
        "task_id": "abc",
        "state": AsyncRunState.UNKNOWN.value,
        "summary": "no result",
        "repo_root": "/tmp",
    }
    parsed = RunLookupResponse.model_validate(payload)
    assert parsed.stop_reason is None


# --- file_diffs -----------------------------------------------------------


@pytest.mark.asyncio
async def test_file_diffs_capture_modified_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo(tmp_path)
    target = repo_root / "hello.py"
    target.write_text("print('old')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_root), "commit", "-q", "-m", "add hello"], check=True)

    runner = _make_runner(repo_root)

    def mutate_file(_args, _kwargs):
        target.write_text("print('new')\n", encoding="utf-8")

    _install_fake_codex(monkeypatch, side_effect=mutate_file)

    result = await runner.run(ToolName.BUILD, InvocationRequest(prompt="rewrite hello"))

    assert result.status == RunStatus.SUCCESS
    assert any(diff.path.endswith("hello.py") for diff in result.file_diffs)
    diff = next(d for d in result.file_diffs if d.path.endswith("hello.py"))
    assert diff.old_text == "print('old')\n"
    assert diff.new_text == "print('new')\n"
    assert diff.truncated is False


@pytest.mark.asyncio
async def test_file_diffs_capture_new_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo(tmp_path)
    runner = _make_runner(repo_root)
    new_file = repo_root / "fresh.txt"

    def create_file(_args, _kwargs):
        new_file.write_text("brand new\n", encoding="utf-8")

    _install_fake_codex(monkeypatch, side_effect=create_file)

    result = await runner.run(ToolName.BUILD, InvocationRequest(prompt="add a file"))

    assert result.status == RunStatus.SUCCESS
    diff = next((d for d in result.file_diffs if d.path.endswith("fresh.txt")), None)
    assert diff is not None
    assert diff.old_text is None
    assert diff.new_text == "brand new\n"
    assert diff.truncated is False


@pytest.mark.asyncio
async def test_file_diffs_capture_deleted_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo(tmp_path)
    doomed = repo_root / "old.txt"
    doomed.write_text("goodbye\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_root), "commit", "-q", "-m", "add doomed"], check=True)

    runner = _make_runner(repo_root)

    def delete_file(_args, _kwargs):
        doomed.unlink()

    _install_fake_codex(monkeypatch, side_effect=delete_file)

    result = await runner.run(ToolName.BUILD, InvocationRequest(prompt="rm old"))

    assert result.status == RunStatus.SUCCESS
    diff = next((d for d in result.file_diffs if d.path.endswith("old.txt")), None)
    assert diff is not None
    assert diff.old_text == "goodbye\n"
    assert diff.new_text is None


@pytest.mark.asyncio
async def test_file_diffs_truncate_binary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo(tmp_path)
    binary = repo_root / "blob.bin"
    binary.write_bytes(b"\x00\x01\x02prefix-with-null")
    subprocess.run(["git", "-C", str(repo_root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_root), "commit", "-q", "-m", "add blob"], check=True)

    runner = _make_runner(repo_root)

    def mutate_blob(_args, _kwargs):
        binary.write_bytes(b"\x00\x01\x02different-bytes-still-binary")

    _install_fake_codex(monkeypatch, side_effect=mutate_blob)

    result = await runner.run(ToolName.BUILD, InvocationRequest(prompt="rewrite blob"))

    diff = next((d for d in result.file_diffs if d.path.endswith("blob.bin")), None)
    assert diff is not None
    assert diff.truncated is True
    assert diff.old_text is None
    assert diff.new_text is None


@pytest.mark.asyncio
async def test_file_diffs_truncate_oversize_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_dobby_mcp import runner as runner_module

    monkeypatch.setattr(runner_module, "_FILE_DIFF_MAX_BYTES", 64)

    repo_root = _make_repo(tmp_path)
    big = repo_root / "big.txt"
    big.write_text("X" * 4096, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_root), "commit", "-q", "-m", "add big"], check=True)

    runner = _make_runner(repo_root)

    def mutate_big(_args, _kwargs):
        big.write_text("Y" * 4096, encoding="utf-8")

    _install_fake_codex(monkeypatch, side_effect=mutate_big)

    result = await runner.run(ToolName.BUILD, InvocationRequest(prompt="rewrite big"))

    diff = next((d for d in result.file_diffs if d.path.endswith("big.txt")), None)
    assert diff is not None
    assert diff.truncated is True
    assert diff.old_text is None
    assert diff.new_text is None


@pytest.mark.asyncio
async def test_file_diffs_empty_for_read_only_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo(tmp_path)
    runner = _make_runner(repo_root)
    _install_fake_codex(monkeypatch)

    result = await runner.run(ToolName.PLAN, InvocationRequest(prompt="inspect"))

    assert result.file_diffs == []


@pytest.mark.asyncio
async def test_file_diffs_serialize_with_camelcase_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo(tmp_path)
    runner = _make_runner(repo_root)
    new_file = repo_root / "alias_check.txt"

    def create_file(_args, _kwargs):
        new_file.write_text("hi\n", encoding="utf-8")

    _install_fake_codex(monkeypatch, side_effect=create_file)

    result = await runner.run(ToolName.BUILD, InvocationRequest(prompt="add alias"))

    diff = next((d for d in result.file_diffs if d.path.endswith("alias_check.txt")), None)
    assert diff is not None
    payload = diff.model_dump(mode="json", by_alias=True)
    assert "oldText" in payload
    assert "newText" in payload
    assert payload["oldText"] is None
    assert payload["newText"] == "hi\n"


@pytest.mark.asyncio
async def test_result_json_artifact_uses_camelcase_for_file_diffs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The persisted result.json must use ACP camelCase for FileDiff fields.

    Regression: previously the file was serialized without ``by_alias=True``,
    so on-disk artifact had ``old_text``/``new_text`` even though the docs
    claimed camelCase. Anything reading result.json off disk would see one
    shape while MCP wire consumers saw another.
    """
    repo_root = _make_repo(tmp_path)
    runner = _make_runner(repo_root)
    target = repo_root / "shape_check.py"
    target.write_text("first\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo_root), "commit", "-q", "-m", "add shape_check"], check=True
    )

    def mutate(_args, _kwargs):
        target.write_text("second\n", encoding="utf-8")

    _install_fake_codex(monkeypatch, side_effect=mutate)
    result = await runner.run(ToolName.BUILD, InvocationRequest(prompt="rewrite"))

    result_json_path = Path(result.artifact_paths["result_json"])
    on_disk = json.loads(result_json_path.read_text(encoding="utf-8"))
    diffs = on_disk["file_diffs"]
    assert diffs, "expected at least one FileDiff persisted"
    sample = diffs[0]
    assert "oldText" in sample, f"expected oldText alias in result.json, got keys: {list(sample.keys())}"
    assert "newText" in sample
    assert "old_text" not in sample
    assert "new_text" not in sample


@pytest.mark.asyncio
async def test_file_diffs_merges_worker_supplied_diff_when_wrapper_truncates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker-reported old/new text fills in for files the wrapper had to truncate."""
    from codex_dobby_mcp import runner as runner_module

    monkeypatch.setattr(runner_module, "_FILE_DIFF_MAX_BYTES", 64)

    repo_root = _make_repo(tmp_path)
    huge = repo_root / "huge.txt"
    huge.write_text("X" * 4096, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_root), "commit", "-q", "-m", "add huge"], check=True)

    runner = _make_runner(repo_root)
    worker_payload = _worker_payload(
        files_changed=["huge.txt"],
        file_diffs=[{"path": "huge.txt", "oldText": "synthetic-old", "newText": None}],
    )

    def mutate(_args, _kwargs):
        huge.write_text("Y" * 4096, encoding="utf-8")

    _install_fake_codex(monkeypatch, side_effect=mutate, worker_payload=worker_payload)

    result = await runner.run(ToolName.BUILD, InvocationRequest(prompt="rewrite huge"))

    diff = next((d for d in result.file_diffs if d.path.endswith("huge.txt")), None)
    assert diff is not None
    # Wrapper-detected truncation is preserved, but worker oldText fills in.
    assert diff.truncated is True
    assert diff.old_text == "synthetic-old"

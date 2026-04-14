import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from codex_dobby_mcp.models import (
    Completeness,
    DEFAULT_REASONING_EFFORTS,
    DEFAULT_TIMEOUT_SECONDS,
    InvocationRequest,
    RECURSION_GUARD_ENV,
    ResultArtifactState,
    ReasoningEffort,
    ReviewAgent,
    ToolName,
    RunStatus,
)


def _request(**overrides) -> InvocationRequest:
    """Build an InvocationRequest bypassing the minimum timeout for unit tests."""
    defaults = dict(
        prompt="test prompt",
        repo_root=None,
        files=[],
        important_context=None,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        extra_roots=[],
        model=None,
        reasoning_effort=None,
        agents=[],
        danger=False,
    )
    defaults.update(overrides)
    return InvocationRequest.model_construct(**defaults)
from codex_dobby_mcp.paths import PathResolutionError
from codex_dobby_mcp.review_agents import (
    REVIEW_SUBAGENT_DEFAULT_MODEL,
    REVIEW_SUBAGENT_DEFAULT_REASONING_EFFORT,
    review_agents_root,
)
from codex_dobby_mcp.runner import (
    CodexRunner,
    RunnerError,
    _build_repo_snapshot,
    _codex_home_permission_issue,
    _collect_sandbox_violations,
    _changed_status_files,
    _create_process_with_deadline,
    _execute_process_with_streaming_logs,
    _count_completed_spawn_agent_calls,
    _first_meaningful_output_line,
    _git_status,
    _match_review_spawn_prompt,
    _path_fingerprint,
    _review_orchestration_warnings,
    _salvaged_review_worker_result,
)


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


def test_worker_schema_file_is_valid_json_schema() -> None:
    schema_path = (
        Path(__file__).resolve().parents[1] / "src" / "codex_dobby_mcp" / "assets" / "schemas" / "worker-output.schema.json"
    )
    payload = json.loads(schema_path.read_text(encoding="utf-8"))

    assert payload["type"] == "object"
    assert payload["additionalProperties"] is False
    assert payload["required"] == ["summary", "completeness", "important_facts", "next_steps", "files_changed", "warnings"]


def test_default_reasoning_efforts_include_high_build_and_medium_validate() -> None:
    assert DEFAULT_REASONING_EFFORTS[ToolName.BUILD] == ReasoningEffort.HIGH
    assert DEFAULT_REASONING_EFFORTS[ToolName.VALIDATE] == ReasoningEffort.MEDIUM


def test_runner_requires_valid_structured_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    prompts_root = assets_root / "prompts"
    schema_path = assets_root / "schemas" / "worker-output.schema.json"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=prompts_root,
        worker_schema_path=schema_path,
        review_agents_root=review_agents_root(assets_root),
    )

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        class Result:
            returncode = 0
            stdout = "true\n"
            stderr = ""

        return Result()

    monkeypatch.setattr("codex_dobby_mcp.paths.subprocess.run", fake_run)
    artifacts = runner._resolve(ToolName.PLAN, InvocationRequest(prompt="inspect")).artifacts
    artifacts.last_message_txt.write_text('{"summary":"ok"}', encoding="utf-8")

    with pytest.raises(RunnerError):
        runner._load_worker_result(artifacts, allow_missing=False)


def test_single_agent_review_uses_direct_review_defaults(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    spec = runner._resolve(
        ToolName.REVIEW,
        InvocationRequest(prompt="review it", agents=[ReviewAgent.CORRECTNESS]),
    )

    assert spec.model == REVIEW_SUBAGENT_DEFAULT_MODEL
    assert spec.reasoning_effort == REVIEW_SUBAGENT_DEFAULT_REASONING_EFFORT


def test_multi_agent_review_uses_medium_parent_reasoning_by_default(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    spec = runner._resolve(
        ToolName.REVIEW,
        InvocationRequest(prompt="review it", agents=[ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION]),
    )

    assert spec.model == "gpt-5.4"
    assert spec.reasoning_effort == REVIEW_SUBAGENT_DEFAULT_REASONING_EFFORT


def test_multi_agent_review_passes_timeout_and_agents_through_uncapped(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    spec = runner._resolve(
        ToolName.REVIEW,
        InvocationRequest(
            prompt="review it",
            timeout_seconds=1200,
        ),
    )

    assert spec.requested_timeout_seconds == 1200
    assert spec.request.timeout_seconds == 1200
    assert spec.request.agents == []
    assert spec.reasoning_effort == ReasoningEffort.MEDIUM


def test_runner_rejects_agents_for_non_review_tools(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    with pytest.raises(ValueError, match="agents is only supported when tool=review"):
        runner._resolve(
            ToolName.PLAN,
            InvocationRequest(prompt="plan it", agents=[ReviewAgent.SECURITY]),
        )


def test_short_timeout_plan_uses_medium_reasoning_by_default(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    spec = runner._resolve(
        ToolName.PLAN,
        InvocationRequest(prompt="plan it", timeout_seconds=300),
    )

    assert spec.model == "gpt-5.4"
    assert spec.reasoning_effort == ReasoningEffort.HIGH


def test_persist_request_keeps_requested_timeout_and_records_effective_timeout(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    spec = runner._resolve(
        ToolName.REVIEW,
        InvocationRequest(
            prompt="review it",
            timeout_seconds=1200,
        ),
    )
    runner._persist_request(spec)

    payload = json.loads(spec.artifacts.request_json.read_text(encoding="utf-8"))

    assert payload["request"]["timeout_seconds"] == 1200
    assert payload["resolved"]["effective_timeout_seconds"] == 1200
    assert payload["resolved"]["requested_review_agents"] == []
    assert payload["resolved"]["effective_review_agents"] == ["generalist"]


def test_persist_request_preserves_requested_review_agents_before_normalization(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    spec = runner._resolve(
        ToolName.REVIEW,
        InvocationRequest(
            prompt="review it",
            agents=[ReviewAgent.CORRECTNESS, ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        ),
    )
    runner._persist_request(spec)

    payload = json.loads(spec.artifacts.request_json.read_text(encoding="utf-8"))

    assert payload["resolved"]["requested_review_agents"] == ["correctness", "correctness", "regression"]
    assert payload["resolved"]["effective_review_agents"] == ["correctness", "regression"]


@pytest.mark.asyncio
async def test_multi_agent_review_uses_effective_timeout_for_process_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            self.returncode = -9

    captured_timeout: dict[str, float] = {}

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "summary": "review complete",
                    "completeness": "full",
                    "important_facts": [],
                    "next_steps": [],
                    "files_changed": [],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    async def fake_execute(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured_timeout["value"] = args[4]
        stdout_log = args[2]
        stderr_log = args[3]
        stdout_log.write_text("", encoding="utf-8")
        stderr_log.write_text("", encoding="utf-8")
        return (0, False, False)

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("codex_dobby_mcp.runner._execute_process_with_streaming_logs", fake_execute)
    monkeypatch.setattr(
        "codex_dobby_mcp.runner._review_orchestration_diagnostics",
        lambda *args, **kwargs: SimpleNamespace(warnings=[]),
    )

    result = await runner.run(
        ToolName.REVIEW,
        InvocationRequest(
            prompt="review it",
            files=["runner.py"],
            agents=[ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
            timeout_seconds=1200,
        ),
    )

    assert result.status == RunStatus.SUCCESS
    assert 1170 <= captured_timeout["value"] <= 1200


@pytest.mark.asyncio
async def test_multi_agent_review_passes_all_requested_agents_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "summary": "review complete",
                    "completeness": "full",
                    "important_facts": [],
                    "next_steps": [],
                    "files_changed": [],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    async def fake_execute(*args, **kwargs):  # type: ignore[no-untyped-def]
        stdout_log = args[2]
        stderr_log = args[3]
        stdout_log.write_text("", encoding="utf-8")
        stderr_log.write_text("", encoding="utf-8")
        return (0, False, False)

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("codex_dobby_mcp.runner._execute_process_with_streaming_logs", fake_execute)
    monkeypatch.setattr(
        "codex_dobby_mcp.runner._review_orchestration_diagnostics",
        lambda *args, **kwargs: SimpleNamespace(warnings=[]),
    )

    result = await runner.run(
        ToolName.REVIEW,
        InvocationRequest(
            prompt="review it",
            agents=[
                ReviewAgent.SECURITY,
                ReviewAgent.ARCHITECTURE,
                ReviewAgent.CORRECTNESS,
                ReviewAgent.REGRESSION,
                ReviewAgent.PERFORMANCE,
            ],
            timeout_seconds=1200,
        ),
    )

    assert result.status == RunStatus.SUCCESS
    assert not any("capped" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_multi_agent_review_run_renders_prompt_with_effective_timeout_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "summary": "review complete",
                    "completeness": "full",
                    "important_facts": [],
                    "next_steps": [],
                    "files_changed": [],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    async def fake_execute(*args, **kwargs):  # type: ignore[no-untyped-def]
        stdout_log = args[2]
        stderr_log = args[3]
        stdout_log.write_text("", encoding="utf-8")
        stderr_log.write_text("", encoding="utf-8")
        return (0, False, False)

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("codex_dobby_mcp.runner._execute_process_with_streaming_logs", fake_execute)
    monkeypatch.setattr(
        "codex_dobby_mcp.runner._review_orchestration_diagnostics",
        lambda *args, **kwargs: SimpleNamespace(warnings=[]),
    )

    result = await runner.run(
        ToolName.REVIEW,
        InvocationRequest(
            prompt="review it",
            files=["runner.py"],
            agents=[ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
            timeout_seconds=1200,
        ),
    )

    prompt_text = Path(result.artifact_paths["prompt_txt"]).read_text(encoding="utf-8")

    assert "Timeout plan for this run: total budget `1200` seconds." in prompt_text


@pytest.mark.asyncio
async def test_runner_cancellation_terminates_spawned_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.kill_calls = 0
            self.wait_calls = 0

        async def wait(self) -> int:
            self.wait_calls += 1
            self.returncode = -9
            return -9

        def kill(self) -> None:
            self.kill_calls += 1
            self.returncode = -9

    process = FakeProcess()

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        return process

    async def fake_execute(*args, **kwargs):  # type: ignore[no-untyped-def]
        await asyncio.sleep(3600)
        return (0, False, False)

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("codex_dobby_mcp.runner._execute_process_with_streaming_logs", fake_execute)

    task = asyncio.create_task(
        runner.run(ToolName.PLAN, _request(prompt="inspect", timeout_seconds=60))
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.kill_calls == 1
    assert process.wait_calls == 1


def test_count_completed_spawn_agent_calls_ignores_non_matching_events() -> None:
    stdout = "\n".join(
        [
            '{"type":"thread.started","thread_id":"t1"}',
            '{"type":"item.started","item":{"id":"item_1","type":"collab_tool_call","tool":"spawn_agent","sender_thread_id":"t1","prompt":"p"}}',
            '{"type":"item.completed","item":{"id":"item_1","type":"collab_tool_call","tool":"spawn_agent","sender_thread_id":"t1","prompt":"p"}}',
            '{"type":"item.completed","item":{"id":"item_2","type":"collab_tool_call","tool":"wait","sender_thread_id":"t1"}}',
            '{"type":"item.completed","item":{"id":"item_3","type":"command_execution","tool":"spawn_agent","sender_thread_id":"t1"}}',
        ]
    )

    assert _count_completed_spawn_agent_calls(stdout) == 1


def test_count_completed_spawn_agent_calls_ignores_non_top_level_spawn_events() -> None:
    stdout = "\n".join(
        [
            '{"type":"thread.started","thread_id":"parent"}',
            '{"type":"item.started","item":{"id":"item_1","type":"collab_tool_call","tool":"spawn_agent","sender_thread_id":"child","prompt":"p"}}',
            '{"type":"item.completed","item":{"id":"item_1","type":"collab_tool_call","tool":"spawn_agent","sender_thread_id":"child","prompt":"p"}}',
            '{"type":"item.started","item":{"id":"item_2","type":"collab_tool_call","tool":"spawn_agent","sender_thread_id":"parent","prompt":"p"}}',
            '{"type":"item.completed","item":{"id":"item_2","type":"collab_tool_call","tool":"spawn_agent","sender_thread_id":"parent","prompt":"p"}}',
        ]
    )

    assert _count_completed_spawn_agent_calls(stdout) == 1


def test_first_meaningful_output_line_skips_json_event_stream() -> None:
    stdout = "\n".join(
        [
            '{"type":"thread.started","thread_id":"t1"}',
            '{"type":"turn.started"}',
            "real summary line",
        ]
    )

    assert _first_meaningful_output_line(stdout) == "real summary line"
    assert _first_meaningful_output_line('{"type":"thread.started"}\n') is None


def test_collect_sandbox_violations_extracts_unique_messages() -> None:
    violations = _collect_sandbox_violations(
        '\n'.join(
            [
                '{"type":"error","message":"Sandbox blocked write access to /repo/tmp.txt"}',
                "permission denied while writing file in sandbox",
                "permission denied while writing file in sandbox",
            ]
        ),
        "",
    )

    assert violations == [
        "Sandbox blocked write access to /repo/tmp.txt",
        "permission denied while writing file in sandbox",
    ]


def test_collect_sandbox_violations_ignores_non_error_json_content() -> None:
    violations = _collect_sandbox_violations(
        json.dumps(
            {
                "summary": "Research complete",
                "important_facts": [
                    "Each tool returns structured fields including `sandbox_violations`.",
                    "def _collect_sandbox_violations(*streams: str) -> list[str]:",
                    '{"type":"error","message":"Sandbox blocked write access to /repo/tmp.txt"}',
                ],
            }
        ),
        "",
    )

    assert violations == []


def test_collect_sandbox_violations_ignores_plain_code_identifiers() -> None:
    violations = _collect_sandbox_violations(
        "\n".join(
            [
                "sandbox_violations = _collect_sandbox_violations(stdout, stderr)",
                "def _collect_sandbox_violations(*streams: str) -> list[str]:",
            ]
        ),
        "",
    )

    assert violations == []


def test_collect_sandbox_violations_ignores_pretty_printed_json_list_items() -> None:
    violations = _collect_sandbox_violations(
        '\n'.join(
            [
                '[',
                '  "or \\"sandbox violation\\" in lower",',
                '  "if any(token in lower for token in (\\"permission denied\\", \\"operation not permitted\\")):",',
                ']',
            ]
        ),
        "",
    )

    assert violations == []


def test_collect_sandbox_violations_ignores_code_like_false_positives_from_stderr() -> None:
    violations = _collect_sandbox_violations(
        "\n".join(
            [
                '\'{"type":"error","message":"Sandbox blocked write access to /repo/tmp.txt"}\',',
                'return (b"", b"Sandbox blocked write access to /repo/tmp.txt\\n")',
                '855 return (b"", b"Sandbox blocked write access to /repo/tmp.txt\\n")',
                'src/codex_dobby_mcp/runner.py:806: if any(token in lower for token in ("permission denied", "operation not permitted", "read-only file system")):',
                'assert result.sandbox_violations == ["Sandbox blocked write access to /repo/tmp.txt"]',
                'or "sandbox violation" in lower',
                'or "sandbox not permitted" in lower',
            ]
        ),
        "",
    )

    assert violations == []


def test_codex_home_permission_issue_explains_required_access() -> None:
    violation = (
        "Error: thread/start failed: Codex cannot access session files at "
        "/Users/avery/.codex/sessions (permission denied)."
    )

    assert _codex_home_permission_issue([violation]) == (
        "Codex CLI could not access its session files at /Users/avery/.codex/sessions. "
        "Dobby seeds a private per-run Codex home for child runs, so the server process needs read access "
        "to the parent Codex auth/config files and read/write access to the private runtime home it creates "
        "under the system temp directory."
    )


def test_codex_home_permission_issue_extracts_temp_runtime_session_paths() -> None:
    violation = (
        "Fatal error: Codex cannot access session files at "
        "/var/folders/test/codex-dobby/task-1/codex-home/sessions (permission denied)."
    )

    assert _codex_home_permission_issue([violation]) == (
        "Codex CLI could not access its session files at "
        "/var/folders/test/codex-dobby/task-1/codex-home/sessions. "
        "Dobby seeds a private per-run Codex home for child runs, so the server process needs read access "
        "to the parent Codex auth/config files and read/write access to the private runtime home it creates "
        "under the system temp directory."
    )


@pytest.mark.asyncio
async def test_runner_preflights_unreadable_parent_codex_auth_before_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    blocked_home = tmp_path / "blocked-codex-home"
    (blocked_home / "sessions").mkdir(parents=True)
    auth_path = blocked_home / "auth.json"
    auth_path.write_text('{"auth_mode":"chatgpt"}\n', encoding="utf-8")
    (blocked_home / "config.toml").write_text("", encoding="utf-8")
    auth_path.chmod(0o000)
    monkeypatch.setenv("CODEX_HOME", str(blocked_home))

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    async def should_not_spawn(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("preflight should have returned before spawning codex")

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", should_not_spawn)
    try:
        result = await runner.run(ToolName.PLAN, InvocationRequest(prompt="inspect"))
    finally:
        auth_path.chmod(0o644)

    assert result.status == RunStatus.ERROR
    assert result.raw_output_available is False
    assert result.summary == (
        f"Codex CLI cannot read the parent Codex auth file at {auth_path}. "
        "Dobby seeds a private per-run Codex home for child runs, so the server process needs read access "
        "to the parent Codex auth/config files and read/write access to the private runtime home it creates "
        "under the system temp directory."
    )
    assert result.warnings == [result.summary]
    assert result.sandbox_violations == [result.summary]


def test_resolve_summary_prefers_error_reason_over_worker_summary() -> None:
    from codex_dobby_mcp.models import WorkerResult

    worker_result = WorkerResult(
        summary="Looks good",
        completeness=Completeness.FULL,
        important_facts=[],
        next_steps=[],
        files_changed=[],
        warnings=[],
    )

    assert (
        CodexRunner._resolve_summary(
            worker_result,
            stdout="",
            stderr="",
            exit_code=0,
            timeout_hit=False,
            worker_result_error=None,
            error_reasons=["Review did not record completed wait results for every spawned subagent"],
        )
        == "Review did not record completed wait results for every spawned subagent"
    )


def test_git_status_uses_nul_delimited_porcelain_for_quoted_and_renamed_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        class Result:
            returncode = 0
            stdout = (
                b" M normal.py\0"
                b'?? weird name "quote".py\0'
                b"R  renamed.py\0original.py\0"
                b"!! ignored.bin\0"
            )
            stderr = b""

        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _git_status(tmp_path) == ["normal.py", 'weird name "quote".py', "renamed.py", "ignored.bin"]


def test_path_fingerprint_handles_directories(tmp_path: Path) -> None:
    directory = tmp_path / "ignored-dir"
    directory.mkdir()
    nested = directory / "nested.txt"
    nested.write_text("one", encoding="utf-8")

    first = _path_fingerprint(directory)
    nested.write_text("two", encoding="utf-8")

    assert _path_fingerprint(directory) != first


def test_metadata_snapshot_detects_changes_to_existing_dirty_file(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    dirty_file = repo_root / "dirty.txt"
    dirty_file.write_text("one", encoding="utf-8")

    before = _build_repo_snapshot(
        repo_root,
        include_head=False,
        use_metadata_fingerprints=True,
    )
    time.sleep(0.001)
    dirty_file.write_text("two", encoding="utf-8")
    after = _build_repo_snapshot(
        repo_root,
        include_head=False,
        use_metadata_fingerprints=True,
    )

    assert _changed_status_files(before, after) == ["dirty.txt"]


def test_review_orchestration_warnings_require_enough_completed_spawns() -> None:
    good_stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "parent",
                }
            ),
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "id": "item_0",
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "sender_thread_id": "parent",
                        "receiver_thread_ids": [],
                        "prompt": "review this code",
                        "agents_states": {},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_0",
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "sender_thread_id": "parent",
                        "receiver_thread_ids": ["child-1"],
                        "prompt": "security review this code",
                        "agents_states": {"child-1": {"status": "pending_init"}},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "id": "item_1",
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "sender_thread_id": "parent",
                        "receiver_thread_ids": [],
                        "prompt": "regression review this code",
                        "agents_states": {},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_1",
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "sender_thread_id": "parent",
                        "receiver_thread_ids": ["child-2"],
                        "prompt": "regression review this code",
                        "agents_states": {"child-2": {"status": "pending_init"}},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "id": "item_wait",
                        "type": "collab_tool_call",
                        "tool": "wait",
                        "sender_thread_id": "parent",
                        "receiver_thread_ids": ["child-1", "child-2"],
                        "prompt": None,
                        "agents_states": {},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_wait",
                        "type": "collab_tool_call",
                        "tool": "wait",
                        "sender_thread_id": "parent",
                        "receiver_thread_ids": [],
                        "prompt": None,
                        "agents_states": {
                            "child-1": {"status": "completed"},
                            "child-2": {"status": "completed"},
                        },
                    },
                }
            ),
        ]
    )

    early_wait_stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "parent",
                }
            ),
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "id": "item_0",
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "sender_thread_id": "parent",
                        "receiver_thread_ids": [],
                        "prompt": "security review this code",
                        "agents_states": {},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_0",
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "sender_thread_id": "parent",
                        "receiver_thread_ids": ["child-1"],
                        "prompt": "security review this code",
                        "agents_states": {"child-1": {"status": "pending_init"}},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "id": "item_wait",
                        "type": "collab_tool_call",
                        "tool": "wait",
                        "sender_thread_id": "parent",
                        "receiver_thread_ids": ["child-1"],
                        "prompt": None,
                        "agents_states": {},
                    },
                }
            ),
        ]
    )

    incomplete_wait_stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "parent",
                }
            ),
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "id": "item_0",
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "sender_thread_id": "parent",
                        "receiver_thread_ids": [],
                        "prompt": "security review this code",
                        "agents_states": {},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_0",
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "sender_thread_id": "parent",
                        "receiver_thread_ids": ["child-1"],
                        "prompt": "security review this code",
                        "agents_states": {"child-1": {"status": "pending_init"}},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "id": "item_1",
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "sender_thread_id": "parent",
                        "receiver_thread_ids": [],
                        "prompt": "regression review this code",
                        "agents_states": {},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_1",
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "sender_thread_id": "parent",
                        "receiver_thread_ids": ["child-2"],
                        "prompt": "regression review this code",
                        "agents_states": {"child-2": {"status": "pending_init"}},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "id": "item_wait",
                        "type": "collab_tool_call",
                        "tool": "wait",
                        "sender_thread_id": "parent",
                        "receiver_thread_ids": ["child-1", "child-2"],
                        "prompt": None,
                        "agents_states": {},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_wait",
                        "type": "collab_tool_call",
                        "tool": "wait",
                        "sender_thread_id": "parent",
                        "receiver_thread_ids": ["child-1"],
                        "prompt": None,
                        "agents_states": {
                            "child-2": {"status": "completed"},
                        },
                    },
                }
            ),
        ]
    )

    bad_stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "parent",
                }
            ),
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "id": "item_0",
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "sender_thread_id": "parent",
                        "receiver_thread_ids": [],
                        "prompt": "review this code",
                        "agents_states": {},
                    },
                }
            ),
        ]
    )

    assert _review_orchestration_warnings(good_stdout, [ReviewAgent.SECURITY, ReviewAgent.REGRESSION]) == []
    assert _review_orchestration_warnings(
        bad_stdout,
        [ReviewAgent.SECURITY],
    ) == ["Review emitted 0 completed spawn_agent calls; expected exactly 1"]
    assert _review_orchestration_warnings(
        early_wait_stdout,
        [ReviewAgent.SECURITY, ReviewAgent.REGRESSION],
    ) == [
        "Review emitted 1 completed spawn_agent calls; expected exactly 2",
        "Review started waiting before spawning all selected review agents",
        "Review spawn prompts did not cover selected agents: regression",
        "Review did not record completed wait results for every spawned subagent",
    ]
    assert _review_orchestration_warnings(
        incomplete_wait_stdout,
        [ReviewAgent.SECURITY, ReviewAgent.REGRESSION],
    ) == ["Review did not record completed wait results for every spawned subagent"]


def test_match_review_spawn_prompt_prefers_exact_header_markers() -> None:
    from codex_dobby_mcp.review_agents import selected_review_agent_definitions

    definitions = selected_review_agent_definitions([ReviewAgent.SECURITY, ReviewAgent.REGRESSION])
    prompt = "\n".join(
        [
            "Required custom agent: dobby_review_security",
            "Assigned lens: security",
            "",
            "Injected custom review agents available in this run:",
            "- dobby_review_security (security)",
            "- dobby_review_regression (regression and patterns)",
        ]
    )

    assert _match_review_spawn_prompt(prompt, definitions) == ReviewAgent.SECURITY.value


def test_review_orchestration_warnings_accept_assignment_header_prompts() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "parent"}),
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "id": "item_0",
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "sender_thread_id": "parent",
                        "receiver_thread_ids": [],
                        "prompt": "Required custom agent: dobby_review_correctness\nAssigned lens: correctness",
                        "agents_states": {},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_0",
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "sender_thread_id": "parent",
                        "receiver_thread_ids": ["child-1"],
                        "prompt": "Required custom agent: dobby_review_correctness\nAssigned lens: correctness",
                        "agents_states": {"child-1": {"status": "pending_init"}},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "id": "item_wait",
                        "type": "collab_tool_call",
                        "tool": "wait",
                        "sender_thread_id": "parent",
                        "receiver_thread_ids": ["child-1"],
                        "prompt": None,
                        "agents_states": {},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_wait",
                        "type": "collab_tool_call",
                        "tool": "wait",
                        "sender_thread_id": "parent",
                        "receiver_thread_ids": ["child-1"],
                        "prompt": None,
                        "agents_states": {"child-1": {"status": "completed", "message": "{}"}},
                    },
                }
            ),
        ]
    )

    assert _review_orchestration_warnings(stdout, [ReviewAgent.CORRECTNESS]) == []


def test_salvaged_review_worker_result_uses_completed_wait_messages() -> None:
    child_message = json.dumps(
        {
            "summary": "Found a timeout bug",
            "completeness": "full",
            "important_facts": ["runner.py can raise bare asyncio.TimeoutError after preflight"],
            "next_steps": ["Wrap subprocess startup in structured timeout handling."],
            "files_changed": [],
            "warnings": [],
        }
    )
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "parent"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_wait",
                        "type": "collab_tool_call",
                        "tool": "wait",
                        "sender_thread_id": "parent",
                        "agents_states": {
                            "child-1": {
                                "status": "completed",
                                "message": child_message,
                            }
                        },
                    },
                }
            ),
        ]
    )

    salvaged = _salvaged_review_worker_result(stdout, [ReviewAgent.CORRECTNESS])

    assert salvaged is not None
    assert salvaged.summary == "Found a timeout bug"
    assert salvaged.important_facts == ["runner.py can raise bare asyncio.TimeoutError after preflight"]


@pytest.mark.asyncio
async def test_execute_process_with_streaming_logs_streams_output_to_files(tmp_path: Path) -> None:
    class FakeStream:
        def __init__(self, chunks: list[bytes]):
            self._chunks = chunks

        async def read(self, _size: int) -> bytes:
            if self._chunks:
                return self._chunks.pop(0)
            return b""

    class FakeStdin:
        def __init__(self) -> None:
            self.writes: list[bytes] = []
            self.closed = False

        def write(self, payload: bytes) -> None:
            self.writes.append(payload)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = FakeStdin()
            self.stdout = FakeStream([b'{"type":"thread.started"}\n', b"summary line\n"])
            self.stderr = FakeStream([b"warning line\n"])
            self.returncode = 0

        async def wait(self) -> int:
            return 0

        def kill(self) -> None:
            self.returncode = -9

    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    process = FakeProcess()

    exit_code, timeout_hit, stall_hit = await _execute_process_with_streaming_logs(
        process,
        b"prompt text",
        stdout_log,
        stderr_log,
        5,
    )

    assert exit_code == 0
    assert timeout_hit is False
    assert process.stdin.writes == [b"prompt text"]
    assert process.stdin.closed is True
    assert stdout_log.read_text(encoding="utf-8") == '{"type":"thread.started"}\nsummary line\n'
    assert stderr_log.read_text(encoding="utf-8") == "warning line\n"


@pytest.mark.asyncio
async def test_execute_process_with_streaming_logs_ignores_kill_race_in_non_streaming_mode(tmp_path: Path) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.communicate_calls = 0
            self.kill_calls = 0

        async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                await asyncio.sleep(1)
            self.returncode = 0
            return (b"stdout\n", b"stderr\n")

        def kill(self) -> None:
            self.kill_calls += 1
            raise ProcessLookupError

    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"

    exit_code, timeout_hit, stall_hit = await _execute_process_with_streaming_logs(
        FakeProcess(),
        b"prompt text",
        stdout_log,
        stderr_log,
        0.01,
    )

    assert exit_code == 0
    assert timeout_hit is True
    assert stdout_log.read_text(encoding="utf-8") == "stdout\n"
    assert stderr_log.read_text(encoding="utf-8") == "stderr\n"


@pytest.mark.asyncio
async def test_execute_process_with_streaming_logs_ignores_kill_race_in_streaming_mode(tmp_path: Path) -> None:
    class FakeStream:
        async def read(self, _size: int) -> bytes:
            return b""

    class FakeStdin:
        def write(self, _payload: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = FakeStdin()
            self.stdout = FakeStream()
            self.stderr = FakeStream()
            self.returncode: int | None = None
            self.wait_calls = 0
            self.kill_calls = 0

        async def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                await asyncio.sleep(1)
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.kill_calls += 1
            raise ProcessLookupError

    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"

    exit_code, timeout_hit, stall_hit = await _execute_process_with_streaming_logs(
        FakeProcess(),
        b"prompt text",
        stdout_log,
        stderr_log,
        0.01,
    )

    assert exit_code == 0
    assert timeout_hit is True
    assert stdout_log.read_text(encoding="utf-8") == ""
    assert stderr_log.read_text(encoding="utf-8") == ""


@pytest.mark.asyncio
async def test_execute_process_with_streaming_logs_drains_late_output_after_graceful_timeout(
    tmp_path: Path,
) -> None:
    terminated = asyncio.Event()

    class FakeStream:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = chunks
            self._drained = False

        async def read(self, _size: int) -> bytes:
            if self._drained:
                return b""
            await terminated.wait()
            await asyncio.sleep(0.5)
            self._drained = True
            return b"".join(self._chunks)

    class FakeStdin:
        def write(self, _payload: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = FakeStdin()
            self.stdout = FakeStream([b'{"type":"item.completed"}\n'])
            self.stderr = FakeStream([])
            self.returncode: int | None = None
            self.terminate_calls = 0
            self.kill_calls = 0

        async def wait(self) -> int:
            if not terminated.is_set():
                await asyncio.sleep(1)
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.terminate_calls += 1
            terminated.set()

        def kill(self) -> None:
            self.kill_calls += 1
            self.returncode = -9

    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"

    exit_code, timeout_hit, stall_hit = await _execute_process_with_streaming_logs(
        FakeProcess(),
        b"prompt text",
        stdout_log,
        stderr_log,
        0.01,
    )

    assert exit_code == 0
    assert timeout_hit is True
    assert stdout_log.read_text(encoding="utf-8") == '{"type":"item.completed"}\n'
    assert stderr_log.read_text(encoding="utf-8") == ""


@pytest.mark.asyncio
async def test_execute_process_with_streaming_logs_bounds_cleanup_after_timeout(tmp_path: Path) -> None:
    class HangingStream:
        async def read(self, _size: int) -> bytes:
            await asyncio.sleep(1)
            return b""

    class FakeStdin:
        def write(self, _payload: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = FakeStdin()
            self.stdout = HangingStream()
            self.stderr = HangingStream()
            self.returncode: int | None = None

        async def wait(self) -> int:
            await asyncio.sleep(1)
            self.returncode = -9 if self.returncode is None else self.returncode
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    started = time.monotonic()

    exit_code, timeout_hit, stall_hit = await _execute_process_with_streaming_logs(
        FakeProcess(),
        b"prompt text",
        stdout_log,
        stderr_log,
        0.01,
    )

    elapsed = time.monotonic() - started

    assert exit_code == -9
    assert timeout_hit is True
    assert elapsed < 2.5


@pytest.mark.asyncio
async def test_execute_process_with_streaming_logs_detects_silent_hang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SilentStream:
        async def read(self, _size: int) -> bytes:
            await asyncio.sleep(10)
            return b""

    class FakeStdin:
        def write(self, _payload: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = FakeStdin()
            self.stdout = SilentStream()
            self.stderr = SilentStream()
            self.returncode: int | None = None
            self.terminate_called = False

        async def wait(self) -> int:
            while self.returncode is None:
                await asyncio.sleep(0.02)
            return self.returncode

        def terminate(self) -> None:
            self.terminate_called = True
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr("codex_dobby_mcp.runner._CODEX_STALL_THRESHOLD_SECONDS", 0.2)
    monkeypatch.setattr("codex_dobby_mcp.runner._CODEX_STALL_CHECK_INTERVAL_SECONDS", 0.05)

    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    process = FakeProcess()
    started = time.monotonic()

    exit_code, timeout_hit, stall_hit = await _execute_process_with_streaming_logs(
        process,
        b"prompt text",
        stdout_log,
        stderr_log,
        5.0,
    )

    elapsed = time.monotonic() - started

    assert stall_hit is True
    assert timeout_hit is False
    assert exit_code == -15
    assert process.terminate_called is True
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_execute_process_with_non_streaming_logs_bounds_cleanup_after_timeout(tmp_path: Path) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.communicate_calls = 0

        async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
            self.communicate_calls += 1
            await asyncio.sleep(1)
            self.returncode = -9 if self.returncode is None else self.returncode
            return (b"late stdout\n", b"late stderr\n")

        def kill(self) -> None:
            self.returncode = -9

    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    started = time.monotonic()

    exit_code, timeout_hit, stall_hit = await _execute_process_with_streaming_logs(
        FakeProcess(),
        b"prompt text",
        stdout_log,
        stderr_log,
        0.01,
    )

    elapsed = time.monotonic() - started

    assert exit_code == -9
    assert timeout_hit is True
    assert elapsed < 2.5
    assert stdout_log.read_text(encoding="utf-8") == ""
    assert stderr_log.read_text(encoding="utf-8") == ""


@pytest.mark.asyncio
async def test_runner_writes_result_json_stub_on_cancellation(tmp_path: Path) -> None:
    """If Dobby is cancelled mid-run, an aborted-stub result.json must exist on disk."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

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
    assert runs_root.exists()
    run_dirs = list(runs_root.iterdir())
    assert len(run_dirs) == 1
    result_path = run_dirs[0] / "result.json"
    assert result_path.exists(), "result.json stub must exist even after cancellation"
    stub = json.loads(result_path.read_text(encoding="utf-8"))
    assert stub["status"] == "error"
    assert stub["completeness"] == "blocked"
    assert "Run did not complete" in stub["summary"]
    assert stub["raw_output_available"] is False
    assert stub["tool"] == "plan"
    assert stub["result_state"] == ResultArtifactState.PLACEHOLDER.value


@pytest.mark.asyncio
async def test_runner_returns_structured_error_when_worker_output_is_invalid(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text('{"summary":"missing required fields"}', encoding="utf-8")
        return FakeProcess()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    try:
        result = await runner.run(ToolName.PLAN, InvocationRequest(prompt="inspect"))
    finally:
        monkeypatch.undo()

    assert result.status == RunStatus.ERROR
    assert result.summary == "Codex completed with invalid structured output"
    assert result.completeness == Completeness.BLOCKED
    assert result.next_steps == []
    assert "Codex completed with invalid structured output" in result.warnings


@pytest.mark.asyncio
async def test_review_response_includes_review_details(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "summary": "Found a correctness issue in the review target.",
                    "completeness": "full",
                    "important_facts": ["A correctness-focused review completed successfully."],
                    "next_steps": [],
                    "files_changed": [],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)

    result = await runner.run(
        ToolName.REVIEW,
        _request(prompt="review it", agents=[ReviewAgent.CORRECTNESS], timeout_seconds=10),
    )

    assert result.status == RunStatus.SUCCESS
    assert result.review_details is not None
    assert result.review_details.requested_review_agents == [ReviewAgent.CORRECTNESS]
    assert result.review_details.effective_review_agents == [ReviewAgent.CORRECTNESS]


@pytest.mark.asyncio
async def test_read_only_run_ignores_model_reported_file_changes_without_observed_writes(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "summary": "no real file changes",
                    "completeness": "full",
                    "important_facts": [],
                    "next_steps": ["Inspect stdout.log if you need the raw worker transcript."],
                    "files_changed": ["fake.py"],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    try:
        result = await runner.run(ToolName.PLAN, InvocationRequest(prompt="inspect"))
    finally:
        monkeypatch.undo()

    assert result.status == RunStatus.SUCCESS
    assert result.completeness == Completeness.FULL
    assert result.next_steps == ["Inspect stdout.log if you need the raw worker transcript."]
    assert result.files_changed == []
    assert "Worker reported file changes that wrapper did not observe: fake.py" in result.warnings


@pytest.mark.asyncio
async def test_read_only_run_flags_new_dirty_paths(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "summary": "inspect complete",
                    "completeness": "full",
                    "important_facts": [],
                    "next_steps": [],
                    "files_changed": [],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        (repo_root / "notes.txt").write_text("unexpected write\n", encoding="utf-8")
        return FakeProcess()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    try:
        result = await runner.run(ToolName.PLAN, InvocationRequest(prompt="inspect"))
    finally:
        monkeypatch.undo()

    assert result.status == RunStatus.ERROR
    assert "notes.txt" in result.files_changed
    assert "Read-only tool changed files outside wrapper-managed artifacts" in result.warnings


@pytest.mark.asyncio
async def test_runner_inherits_parent_environment_for_child_codex_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    captured_env: dict[str, str] = {}
    seeded_auth = ""
    seeded_config = ""

    class FakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal captured_env, seeded_auth, seeded_config
        captured_env = dict(kwargs["env"])
        seeded_home = Path(captured_env["CODEX_HOME"])
        seeded_auth = (seeded_home / "auth.json").read_text(encoding="utf-8")
        seeded_config = (seeded_home / "config.toml").read_text(encoding="utf-8")
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "summary": "research complete",
                    "completeness": "full",
                    "important_facts": [],
                    "next_steps": [],
                    "files_changed": [],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    parent_codex_home = tmp_path / "real-codex-home"
    sessions_dir = parent_codex_home / "sessions"
    sessions_dir.mkdir(parents=True)
    parent_auth = '{"auth_mode":"chatgpt","provider":"test"}\n'
    parent_config = '[mcp_servers.fetchaller]\ncommand = "python"\nargs = ["-m", "fetchaller.main"]\n'
    (parent_codex_home / "auth.json").write_text(parent_auth, encoding="utf-8")
    (parent_codex_home / "config.toml").write_text(parent_config, encoding="utf-8")
    sessions_dir.chmod(0o555)
    monkeypatch.setenv("CODEX_HOME", str(parent_codex_home))
    monkeypatch.setenv("FETCHALLER_TOKEN", "allowed")
    monkeypatch.setenv("TOP_SECRET", "allowed-too")
    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)

    try:
        result = await runner.run(ToolName.RESEARCH, InvocationRequest(prompt="inspect"))
    finally:
        sessions_dir.chmod(0o755)

    assert result.status == RunStatus.SUCCESS
    expected_codex_home = Path(tempfile.gettempdir()).resolve() / "codex-dobby" / result.task_id / "codex-home"
    assert Path(captured_env["CODEX_HOME"]) == expected_codex_home
    assert captured_env["FETCHALLER_TOKEN"] == "allowed"
    assert captured_env["TOP_SECRET"] == "allowed-too"
    assert seeded_auth == parent_auth
    assert seeded_config == parent_config
    assert captured_env["TMPDIR"].endswith("/.codex-dobby/runs/" + result.task_id + "/runtime/tmp")
    assert captured_env["TMP"] == captured_env["TMPDIR"]
    assert captured_env["TEMP"] == captured_env["TMPDIR"]
    assert captured_env["UV_CACHE_DIR"].endswith("/.codex-dobby/runs/" + result.task_id + "/runtime/cache/uv")
    assert captured_env["XDG_CACHE_HOME"].endswith("/.codex-dobby/runs/" + result.task_id + "/runtime/cache/xdg")
    assert captured_env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert captured_env[RECURSION_GUARD_ENV] == "1"
    assert not expected_codex_home.exists()


@pytest.mark.asyncio
async def test_runner_surfaces_sandbox_violations_from_process_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (b"", b"Sandbox blocked write access to /repo/tmp.txt\n")

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "summary": "validation complete",
                    "completeness": "full",
                    "important_facts": [],
                    "next_steps": [],
                    "files_changed": [],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)

    result = await runner.run(ToolName.VALIDATE, InvocationRequest(prompt="run the tests"))

    assert result.status == RunStatus.SUCCESS
    assert result.sandbox_violations == ["Sandbox blocked write access to /repo/tmp.txt"]


@pytest.mark.asyncio
async def test_runner_missing_parent_codex_auth_still_spawns_when_env_based_auth_is_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    parent_codex_home = tmp_path / "codex-home"
    (parent_codex_home / "sessions").mkdir(parents=True, exist_ok=True)
    (parent_codex_home / "auth.json").unlink()
    (parent_codex_home / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(parent_codex_home))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "summary": "plan complete",
                    "completeness": "full",
                    "important_facts": [],
                    "next_steps": [],
                    "files_changed": [],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    result = await runner.run(ToolName.PLAN, InvocationRequest(prompt="inspect"))

    assert result.status == RunStatus.SUCCESS


@pytest.mark.asyncio
async def test_runner_promotes_codex_home_permission_failures_to_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        returncode = 1

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (
                b"",
                (
                    b"WARNING: proceeding, even though we could not update PATH: Operation not permitted (os error 1)\n"
                    b"Error: thread/start: thread/start failed: error creating thread: Fatal error: "
                    b"Codex cannot access session files at /Users/avery/.codex/sessions (permission denied). "
                    b"If sessions were created using sudo, fix ownership: sudo chown -R $(whoami) "
                    b"/Users/avery/.codex (underlying error: Operation not permitted (os error 1))\n"
                ),
            )

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        return FakeProcess()

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)

    result = await runner.run(ToolName.PLAN, InvocationRequest(prompt="inspect"))

    assert result.status == RunStatus.ERROR
    assert result.summary == (
        "Codex CLI could not access its session files at /Users/avery/.codex/sessions. "
        "Dobby seeds a private per-run Codex home for child runs, so the server process needs read access "
        "to the parent Codex auth/config files and read/write access to the private runtime home it creates "
        "under the system temp directory."
    )
    assert result.warnings == [result.summary]
    assert result.sandbox_violations == [
        "Error: thread/start: thread/start failed: error creating thread: Fatal error: Codex cannot access session files at /Users/avery/.codex/sessions (permission denied). If sessions were created using sudo, fix ownership: sudo chown -R $(whoami) /Users/avery/.codex (underlying error: Operation not permitted (os error 1))"
    ]


@pytest.mark.asyncio
async def test_mutating_run_returns_error_if_git_head_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "summary": "build complete",
                    "completeness": "full",
                    "important_facts": [],
                    "next_steps": [],
                    "files_changed": [],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    heads = iter([None, "new-head"])

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("codex_dobby_mcp.runner._git_head", lambda repo: next(heads))

    result = await runner.run(ToolName.BUILD, InvocationRequest(prompt="build it"))

    assert result.status == RunStatus.ERROR
    assert result.summary == "Mutating tool changed git history or references, which Dobby does not allow"
    assert "Mutating tool changed git history or references, which Dobby does not allow" in result.warnings


@pytest.mark.asyncio
async def test_mutating_run_reports_only_files_changed_by_this_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    subprocess.run(["git", "-C", str(repo_root), "config", "user.email", "smoke@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_root), "config", "user.name", "Smoke"], check=True)
    (repo_root / "already_dirty.txt").write_text("base\n", encoding="utf-8")
    (repo_root / "untouched_dirty.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_root), "commit", "-q", "-m", "init"], check=True)

    (repo_root / "already_dirty.txt").write_text("dirty before run\n", encoding="utf-8")
    (repo_root / "untouched_dirty.txt").write_text("leave me alone\n", encoding="utf-8")
    (repo_root / "preexisting_untracked.txt").write_text("already here\n", encoding="utf-8")

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "summary": "build complete",
                    "completeness": "full",
                    "important_facts": [],
                    "next_steps": [],
                    "files_changed": [],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        (repo_root / "already_dirty.txt").write_text("dirty during run\n", encoding="utf-8")
        (repo_root / "new_file.txt").write_text("created during run\n", encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)

    result = await runner.run(ToolName.BUILD, InvocationRequest(prompt="build it"))

    assert result.status == RunStatus.SUCCESS
    assert set(result.files_changed) == {"already_dirty.txt", "new_file.txt"}
    assert "untouched_dirty.txt" not in result.files_changed
    assert "preexisting_untracked.txt" not in result.files_changed


@pytest.mark.asyncio
async def test_mutating_run_preserves_reported_changes_outside_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    extra_root = tmp_path / "shared-data"
    extra_root.mkdir()

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            self.returncode = -9

    outside_file = extra_root / "note.txt"

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        outside_file.write_text("created during run\n", encoding="utf-8")
        output_path.write_text(
            json.dumps(
                {
                    "summary": "build complete",
                    "completeness": "full",
                    "important_facts": [],
                    "next_steps": [],
                    "files_changed": [str(outside_file)],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)

    result = await runner.run(
        ToolName.BUILD,
        InvocationRequest(prompt="build it", extra_roots=[str(extra_root)]),
    )

    assert result.status == RunStatus.SUCCESS
    assert result.files_changed == [str(outside_file)]


@pytest.mark.asyncio
async def test_review_without_dirty_files_or_named_targets_defaults_to_full_repo_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    subprocess.run(["git", "-C", str(repo_root), "config", "user.email", "smoke@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_root), "config", "user.name", "Smoke"], check=True)
    (repo_root / "tracked.txt").write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_root), "commit", "-q", "-m", "init"], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "summary": "full repo review complete",
                    "completeness": "full",
                    "important_facts": [],
                    "next_steps": [],
                    "files_changed": [],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    async def fake_execute(*args, **kwargs):  # type: ignore[no-untyped-def]
        stdout_log = args[2]
        stderr_log = args[3]
        stdout_log.write_text("", encoding="utf-8")
        stderr_log.write_text("", encoding="utf-8")
        return (0, False, False)

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("codex_dobby_mcp.runner._execute_process_with_streaming_logs", fake_execute)
    monkeypatch.setattr(
        "codex_dobby_mcp.runner._review_orchestration_diagnostics",
        lambda *args, **kwargs: SimpleNamespace(warnings=[]),
    )

    result = await runner.run(
        ToolName.REVIEW,
        InvocationRequest(prompt="review it", agents=[ReviewAgent.CORRECTNESS]),
    )

    prompt_text = Path(result.artifact_paths["prompt_txt"]).read_text(encoding="utf-8")

    assert result.status == RunStatus.SUCCESS
    assert result.summary == "full repo review complete"
    assert result.raw_output_available is True
    assert "Relevant files from Claude:\n- (entire repo)" in prompt_text


@pytest.mark.asyncio
async def test_runner_returns_structured_timeout_when_baseline_snapshot_exceeds_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    async def fake_run_blocking(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise asyncio.TimeoutError

    monkeypatch.setattr("codex_dobby_mcp.runner._capture_repo_snapshot_with_deadline", fake_run_blocking)

    result = await runner.run(ToolName.PLAN, _request(prompt="inspect", timeout_seconds=1))

    assert result.status == RunStatus.ERROR
    assert result.summary == "Codex run timed out after 1 seconds"
    assert result.raw_output_available is False


@pytest.mark.asyncio
async def test_review_run_salvages_completed_subagent_result_after_parent_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        returncode = None

        async def wait(self) -> int:
            self.returncode = -9
            return -9

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        return FakeProcess()

    async def fake_execute(*args, **kwargs):  # type: ignore[no-untyped-def]
        stdout_log = args[2]
        stderr_log = args[3]
        correctness_message = json.dumps(
            {
                "summary": "Found a timeout bug",
                "completeness": "full",
                "important_facts": ["runner.py can raise bare asyncio.TimeoutError after preflight"],
                "next_steps": ["Wrap subprocess startup in structured timeout handling."],
                "files_changed": [],
                "warnings": [],
            }
        )
        regression_message = json.dumps(
            {
                "summary": "Found a missing regression test",
                "completeness": "full",
                "important_facts": ["tests do not cover the post-timeout salvage path"],
                "next_steps": ["Add a regression test for timed-out parent review salvage."],
                "files_changed": [],
                "warnings": [],
            }
        )
        stdout_log.write_text(
            "\n".join(
                [
                    json.dumps({"type": "thread.started", "thread_id": "parent"}),
                    json.dumps(
                        {
                            "type": "item.started",
                            "item": {
                                "id": "item_spawn",
                                "type": "collab_tool_call",
                                "tool": "spawn_agent",
                                "sender_thread_id": "parent",
                                "receiver_thread_ids": [],
                                "prompt": "Required custom agent: dobby_review_correctness\nAssigned lens: correctness",
                                "agents_states": {},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "item_spawn",
                                "type": "collab_tool_call",
                                "tool": "spawn_agent",
                                "sender_thread_id": "parent",
                                "receiver_thread_ids": ["child-1"],
                                "prompt": "Required custom agent: dobby_review_correctness\nAssigned lens: correctness",
                                "agents_states": {
                                    "child-1": {
                                        "status": "pending_init",
                                    }
                                },
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.started",
                            "item": {
                                "id": "item_spawn_2",
                                "type": "collab_tool_call",
                                "tool": "spawn_agent",
                                "sender_thread_id": "parent",
                                "receiver_thread_ids": [],
                                "prompt": "Required custom agent: dobby_review_regression\nAssigned lens: regression and patterns",
                                "agents_states": {},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "item_spawn_2",
                                "type": "collab_tool_call",
                                "tool": "spawn_agent",
                                "sender_thread_id": "parent",
                                "receiver_thread_ids": ["child-2"],
                                "prompt": "Required custom agent: dobby_review_regression\nAssigned lens: regression and patterns",
                                "agents_states": {
                                    "child-2": {
                                        "status": "pending_init",
                                    }
                                },
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.started",
                            "item": {
                                "id": "item_wait",
                                "type": "collab_tool_call",
                                "tool": "wait",
                                "sender_thread_id": "parent",
                                "receiver_thread_ids": ["child-1", "child-2"],
                                "agents_states": {},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "item_wait",
                                "type": "collab_tool_call",
                                "tool": "wait",
                                "sender_thread_id": "parent",
                                "agents_states": {
                                    "child-1": {
                                        "status": "completed",
                                        "message": correctness_message,
                                    },
                                    "child-2": {
                                        "status": "completed",
                                        "message": regression_message,
                                    }
                                },
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        stderr_log.write_text("", encoding="utf-8")
        return (-9, True, False)

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("codex_dobby_mcp.runner._execute_process_with_streaming_logs", fake_execute)

    result = await runner.run(
        ToolName.REVIEW,
        _request(
            prompt="review it",
            files=["runner.py"],
            agents=[ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
            timeout_seconds=10,
        ),
    )

    assert result.status == RunStatus.SUCCESS
    assert result.summary == "Review orchestrator did not return final JSON; merged findings from 2/2 completed subagents."
    assert result.completeness == Completeness.PARTIAL
    assert result.important_facts == [
        "runner.py can raise bare asyncio.TimeoutError after preflight",
        "tests do not cover the post-timeout salvage path",
    ]
    assert "Codex run timed out after 10 seconds" in result.warnings
    assert "Review orchestrator did not return final JSON; merged findings from 2/2 completed subagents." in result.warnings


@pytest.mark.asyncio
async def test_review_run_returns_partial_success_when_timeout_salvage_is_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        returncode = None

        async def wait(self) -> int:
            self.returncode = -9
            return -9

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        return FakeProcess()

    async def fake_execute(*args, **kwargs):  # type: ignore[no-untyped-def]
        stdout_log = args[2]
        stderr_log = args[3]
        correctness_message = json.dumps(
            {
                "summary": "Found a timeout cleanup bug",
                "completeness": "full",
                "important_facts": ["runner.py leaves timeout cleanup unbounded in the streaming path"],
                "next_steps": ["Bound timeout cleanup and stream draining to the remaining deadline."],
                "files_changed": [],
                "warnings": [],
            }
        )
        stdout_log.write_text(
            "\n".join(
                [
                    json.dumps({"type": "thread.started", "thread_id": "parent"}),
                    json.dumps(
                        {
                            "type": "item.started",
                            "item": {
                                "id": "item_spawn_1",
                                "type": "collab_tool_call",
                                "tool": "spawn_agent",
                                "sender_thread_id": "parent",
                                "receiver_thread_ids": [],
                                "prompt": "Required custom agent: dobby_review_correctness\nAssigned lens: correctness",
                                "agents_states": {},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "item_spawn_1",
                                "type": "collab_tool_call",
                                "tool": "spawn_agent",
                                "sender_thread_id": "parent",
                                "receiver_thread_ids": ["child-1"],
                                "prompt": "Required custom agent: dobby_review_correctness\nAssigned lens: correctness",
                                "agents_states": {"child-1": {"status": "pending_init"}},
                                },
                            }
                        ),
                    json.dumps(
                        {
                            "type": "item.started",
                            "item": {
                                "id": "item_spawn_2",
                                "type": "collab_tool_call",
                                "tool": "spawn_agent",
                                "sender_thread_id": "parent",
                                "receiver_thread_ids": [],
                                "prompt": "Required custom agent: dobby_review_regression\nAssigned lens: regression and patterns",
                                "agents_states": {},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "item_spawn_2",
                                "type": "collab_tool_call",
                                "tool": "spawn_agent",
                                "sender_thread_id": "parent",
                                "receiver_thread_ids": ["child-2"],
                                "prompt": "Required custom agent: dobby_review_regression\nAssigned lens: regression and patterns",
                                "agents_states": {"child-2": {"status": "pending_init"}},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.started",
                            "item": {
                                "id": "item_wait",
                                "type": "collab_tool_call",
                                "tool": "wait",
                                "sender_thread_id": "parent",
                                "receiver_thread_ids": ["child-1", "child-2"],
                                "agents_states": {},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "item_wait",
                                "type": "collab_tool_call",
                                "tool": "wait",
                                "sender_thread_id": "parent",
                                "agents_states": {
                                    "child-1": {
                                        "status": "completed",
                                        "message": correctness_message,
                                    }
                                },
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        stderr_log.write_text("", encoding="utf-8")
        return (-9, True, False)

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("codex_dobby_mcp.runner._execute_process_with_streaming_logs", fake_execute)

    result = await runner.run(
        ToolName.REVIEW,
        _request(
            prompt="review it",
            files=["runner.py"],
            agents=[ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
            timeout_seconds=10,
        ),
    )

    assert result.status == RunStatus.SUCCESS
    assert result.summary == "Review orchestrator did not return final JSON; surfaced findings from 1/2 completed subagents."
    assert result.completeness == Completeness.PARTIAL
    assert result.important_facts == ["runner.py leaves timeout cleanup unbounded in the streaming path"]
    assert "Review orchestrator did not return final JSON; surfaced findings from 1/2 completed subagents." in result.warnings
    assert "Review returned partial findings from 1/2 completed subagents because orchestration did not finish before timeout" in result.warnings
    assert "Codex run timed out after 10 seconds" in result.warnings
    assert "Review did not record completed wait results for every spawned subagent" in result.warnings


@pytest.mark.asyncio
async def test_review_run_keeps_partial_parent_result_when_only_wait_coverage_is_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "summary": "One review lens completed and surfaced useful timeout findings.",
                    "completeness": "partial",
                    "important_facts": ["The regression lens completed; the correctness lens did not."],
                    "next_steps": [],
                    "files_changed": [],
                    "warnings": ["Only the regression and patterns subagent completed within the allotted review window."],
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    async def fake_execute(*args, **kwargs):  # type: ignore[no-untyped-def]
        stdout_log = args[2]
        stderr_log = args[3]
        stdout_log.write_text(
            "\n".join(
                [
                    json.dumps({"type": "thread.started", "thread_id": "parent"}),
                    json.dumps(
                        {
                            "type": "item.started",
                            "item": {
                                "id": "item_spawn_1",
                                "type": "collab_tool_call",
                                "tool": "spawn_agent",
                                "sender_thread_id": "parent",
                                "receiver_thread_ids": [],
                                "prompt": "Required custom agent: dobby_review_correctness\nAssigned lens: correctness",
                                "agents_states": {},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "item_spawn_1",
                                "type": "collab_tool_call",
                                "tool": "spawn_agent",
                                "sender_thread_id": "parent",
                                "receiver_thread_ids": ["child-1"],
                                "prompt": "Required custom agent: dobby_review_correctness\nAssigned lens: correctness",
                                "agents_states": {"child-1": {"status": "pending_init"}},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.started",
                            "item": {
                                "id": "item_spawn_2",
                                "type": "collab_tool_call",
                                "tool": "spawn_agent",
                                "sender_thread_id": "parent",
                                "receiver_thread_ids": [],
                                "prompt": "Required custom agent: dobby_review_regression\nAssigned lens: regression and patterns",
                                "agents_states": {},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "item_spawn_2",
                                "type": "collab_tool_call",
                                "tool": "spawn_agent",
                                "sender_thread_id": "parent",
                                "receiver_thread_ids": ["child-2"],
                                "prompt": "Required custom agent: dobby_review_regression\nAssigned lens: regression and patterns",
                                "agents_states": {"child-2": {"status": "pending_init"}},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.started",
                            "item": {
                                "id": "item_wait",
                                "type": "collab_tool_call",
                                "tool": "wait",
                                "sender_thread_id": "parent",
                                "receiver_thread_ids": ["child-1", "child-2"],
                                "agents_states": {},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "item_wait",
                                "type": "collab_tool_call",
                                "tool": "wait",
                                "sender_thread_id": "parent",
                                "receiver_thread_ids": ["child-2"],
                                "agents_states": {"child-2": {"status": "completed", "message": "{}"}},
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        stderr_log.write_text("", encoding="utf-8")
        return (0, False, False)

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("codex_dobby_mcp.runner._execute_process_with_streaming_logs", fake_execute)

    result = await runner.run(
        ToolName.REVIEW,
        _request(
            prompt="review it",
            files=["runner.py"],
            agents=[ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
            timeout_seconds=10,
        ),
    )

    assert result.status == RunStatus.SUCCESS
    assert result.summary == "One review lens completed and surfaced useful timeout findings."
    assert result.completeness == Completeness.PARTIAL
    assert result.important_facts == ["The regression lens completed; the correctness lens did not."]
    assert "Review did not record completed wait results for every spawned subagent" in result.warnings


@pytest.mark.asyncio
async def test_single_agent_review_returns_partial_success_when_timeout_has_usable_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        returncode = None

        async def wait(self) -> int:
            self.returncode = -9
            return -9

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "summary": "Found one timeout-handling issue before the deadline expired.",
                    "completeness": "full",
                    "important_facts": ["The direct review path produced useful output before the process timed out."],
                    "next_steps": [],
                    "files_changed": [],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    async def fake_execute(*args, **kwargs):  # type: ignore[no-untyped-def]
        stdout_log = args[2]
        stderr_log = args[3]
        stdout_log.write_text("", encoding="utf-8")
        stderr_log.write_text("", encoding="utf-8")
        return (-9, True, False)

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("codex_dobby_mcp.runner._execute_process_with_streaming_logs", fake_execute)

    result = await runner.run(
        ToolName.REVIEW,
        _request(
            prompt="review it",
            files=["runner.py"],
            agents=[ReviewAgent.CORRECTNESS],
            timeout_seconds=10,
        ),
    )

    assert result.status == RunStatus.SUCCESS
    assert result.summary == "Found one timeout-handling issue before the deadline expired."
    assert result.completeness == Completeness.PARTIAL
    assert "Codex run timed out after 10 seconds" in result.warnings


@pytest.mark.asyncio
async def test_runner_returns_structured_timeout_when_deadline_expires_before_streaming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.kill_calls = 0
            self.wait_calls = 0

        async def wait(self) -> int:
            self.wait_calls += 1
            self.returncode = -9 if self.returncode is None else self.returncode
            return self.returncode

        def kill(self) -> None:
            self.kill_calls += 1
            self.returncode = -9

    process = FakeProcess()

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        return process

    baseline = runner._capture_repo_snapshot(
        repo_root,
        runner._resolve(ToolName.PLAN, InvocationRequest(prompt="inspect")).artifacts,
        gitignore_updated=False,
        include_head=False,
    )

    async def fake_run_blocking(*args, **kwargs):  # type: ignore[no-untyped-def]
        return baseline

    remaining_calls = iter([1.0, asyncio.TimeoutError()])

    def fake_seconds_remaining(_deadline: float) -> float:
        value = next(remaining_calls)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("codex_dobby_mcp.runner._capture_repo_snapshot_with_deadline", fake_run_blocking)
    monkeypatch.setattr("codex_dobby_mcp.runner._seconds_remaining", fake_seconds_remaining)

    result = await runner.run(ToolName.PLAN, _request(prompt="inspect", timeout_seconds=10))

    assert result.status == RunStatus.ERROR
    assert result.summary == "Codex run timed out after 10 seconds"
    assert "Codex run timed out after 10 seconds" in result.warnings
    assert process.kill_calls == 1
    assert process.wait_calls == 1


@pytest.mark.asyncio
async def test_runner_warns_when_post_run_snapshot_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "summary": "done",
                    "completeness": "full",
                    "important_facts": [],
                    "next_steps": [],
                    "files_changed": [],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    baseline = runner._capture_repo_snapshot(
        repo_root,
        runner._resolve(ToolName.PLAN, InvocationRequest(prompt="inspect")).artifacts,
        gitignore_updated=False,
        include_head=False,
    )
    call_count = 0

    async def fake_run_blocking(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return baseline
        raise asyncio.TimeoutError

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("codex_dobby_mcp.runner._capture_repo_snapshot_with_deadline", fake_run_blocking)

    result = await runner.run(ToolName.PLAN, _request(prompt="inspect", timeout_seconds=10))

    assert result.status == RunStatus.SUCCESS
    assert result.summary == "done"
    assert result.completeness == Completeness.PARTIAL
    assert "Post-run repo snapshot timed out; repo change verification may be incomplete" in result.warnings
    assert "Codex run timed out after 10 seconds" not in result.warnings


@pytest.mark.asyncio
async def test_mutating_run_errors_when_post_run_snapshot_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "summary": "build complete",
                    "completeness": "full",
                    "important_facts": [],
                    "next_steps": [],
                    "files_changed": [],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    baseline = runner._capture_repo_snapshot(
        repo_root,
        runner._resolve(ToolName.BUILD, InvocationRequest(prompt="build it")).artifacts,
        gitignore_updated=False,
        include_head=True,
    )
    call_count = 0

    async def fake_run_blocking(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return baseline
        raise asyncio.TimeoutError

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("codex_dobby_mcp.runner._capture_repo_snapshot_with_deadline", fake_run_blocking)

    result = await runner.run(ToolName.BUILD, _request(prompt="build it", timeout_seconds=10))

    assert result.status == RunStatus.ERROR
    assert result.summary == "Post-run repo snapshot timed out; mutating tool results could not be fully verified"
    assert result.completeness == Completeness.BLOCKED
    assert "Post-run repo snapshot timed out; repo change verification may be incomplete" in result.warnings
    assert "Post-run repo snapshot timed out; mutating tool results could not be fully verified" in result.warnings


@pytest.mark.asyncio
async def test_create_process_with_deadline_cleans_up_process_returned_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.kill_calls = 0
            self.wait_calls = 0

        async def wait(self) -> int:
            self.wait_calls += 1
            self.returncode = -9
            return -9

        def kill(self) -> None:
            self.kill_calls += 1
            self.returncode = -9

    process = FakeProcess()

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            return process

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)

    with pytest.raises(asyncio.TimeoutError):
        await _create_process_with_deadline(["codex"], Path("/tmp"), {}, time.monotonic() + 0.01)

    assert process.kill_calls == 1
    assert process.wait_calls == 1


@pytest.mark.asyncio
async def test_create_process_with_deadline_bounds_cleanup_wait_for_late_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.kill_calls = 0

        async def wait(self) -> int:
            await asyncio.sleep(1)
            self.returncode = -9
            return -9

        def kill(self) -> None:
            self.kill_calls += 1
            self.returncode = -9

    process = FakeProcess()

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            return process

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)

    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await _create_process_with_deadline(["codex"], Path("/tmp"), {}, time.monotonic() + 0.01)

    elapsed = time.monotonic() - started

    assert process.kill_calls == 1
    assert elapsed < 1.5


def test_invalid_extra_roots_do_not_create_artifacts_or_gitignore(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    with pytest.raises(PathResolutionError):
        runner._resolve(ToolName.PLAN, InvocationRequest(prompt="inspect", extra_roots=["missing"]))

    assert not (repo_root / ".codex-dobby").exists()
    assert not (repo_root / ".gitignore").exists()


def test_gitignore_guard_failure_does_not_create_artifacts(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo_root / ".gitignore").symlink_to(outside / "gitignore")

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    with pytest.raises(PathResolutionError):
        runner._resolve(ToolName.BUILD, InvocationRequest(prompt="inspect"))

    assert not (repo_root / ".codex-dobby").exists()


def test_read_only_tools_do_not_touch_gitignore(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo_root / ".gitignore").symlink_to(outside / "gitignore")

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    resolved = runner._resolve(ToolName.PLAN, InvocationRequest(prompt="inspect"))

    assert resolved.gitignore_updated is False
    assert resolved.repo_root == repo_root.resolve()
    assert (repo_root / ".gitignore").is_symlink()


def test_whitespace_only_prompt_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InvocationRequest(prompt="   ")


def test_invalid_review_agents_get_targeted_error_message() -> None:
    with pytest.raises(ValidationError) as exc_info:
        InvocationRequest(prompt="review it", agents=["backend", "systems"])

    message = str(exc_info.value)

    assert "Unsupported review agents: backend, systems." in message
    assert "Supported agents: generalist, security, performance, architecture, correctness, ux, regression." in message


def test_review_agents_accept_valid_string_names() -> None:
    request = InvocationRequest(prompt="review it", agents=["correctness", "regression"])

    assert request.agents == [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION]


def test_read_only_extra_roots_outside_repo_become_advisory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    extra_root = tmp_path / "shared-data"
    extra_root.mkdir()
    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        class Result:
            returncode = 0
            stdout = "true\n"
            stderr = ""

        return Result()

    monkeypatch.setattr("codex_dobby_mcp.paths.subprocess.run", fake_run)

    resolved = runner._resolve(ToolName.RESEARCH, InvocationRequest(prompt="inspect", extra_roots=[str(extra_root)]))

    assert resolved.sandbox_roots == [repo_root.resolve()]
    assert resolved.writable_roots == [repo_root.resolve()]
    assert resolved.advisory_read_only_roots == [extra_root.resolve()]
    assert resolved.gitignore_updated is False


def test_mutating_extra_roots_outside_repo_remain_writable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    extra_root = tmp_path / "shared-data"
    extra_root.mkdir()
    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        class Result:
            returncode = 0
            stdout = "true\n"
            stderr = ""

        return Result()

    monkeypatch.setattr("codex_dobby_mcp.paths.subprocess.run", fake_run)

    resolved = runner._resolve(ToolName.BUILD, InvocationRequest(prompt="implement", extra_roots=[str(extra_root)]))

    assert resolved.sandbox_roots == [repo_root.resolve(), extra_root.resolve()]
    assert resolved.writable_roots == [repo_root.resolve(), extra_root.resolve()]
    assert resolved.advisory_read_only_roots == []


def test_reverse_engineer_adds_builtin_mcp_servers_root_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    mcp_servers_root = tmp_path / "mcp-servers"
    mcp_servers_root.mkdir()
    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        class Result:
            returncode = 0
            stdout = "true\n"
            stderr = ""

        return Result()

    monkeypatch.setattr("codex_dobby_mcp.paths.subprocess.run", fake_run)
    monkeypatch.setattr(
        "codex_dobby_mcp.runner.reverse_engineer_default_writable_roots",
        lambda: [mcp_servers_root.resolve()],
    )
    monkeypatch.setattr(
        "codex_dobby_mcp.runner.reverse_engineer_default_readonly_roots",
        lambda: [],
    )

    resolved = runner._resolve(ToolName.REVERSE_ENGINEER, InvocationRequest(prompt="inspect firmware"))

    assert resolved.sandbox_roots == [repo_root.resolve(), mcp_servers_root.resolve()]
    assert resolved.writable_roots == [repo_root.resolve(), mcp_servers_root.resolve()]
    assert resolved.advisory_read_only_roots == []

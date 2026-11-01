import asyncio
import codex_dobby_mcp.runner as runner_module
from dataclasses import replace
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import pytest
from pydantic import ValidationError

from codex_dobby_mcp.models import (
    Completeness,
    DEFAULT_REASONING_EFFORTS,
    DEFAULT_TIMEOUT_SECONDS,
    GhidraUsageMode,
    InvocationRequest,
    RECURSION_GUARD_ENV,
    ResultArtifactState,
    ReasoningEffort,
    ReviewAgent,
    ReviewAgentExecutionStatus,
    ReviewDetails,
    ReviewExecutionEvidence,
    ReviewOrchestrationProtocol,
    StopReason,
    ToolName,
    ToolResponse,
    RunStatus,
    WorkerResult,
)
from codex_dobby_mcp.paths import PathResolutionError, reverse_engineer_default_writable_roots
from codex_dobby_mcp.review_agents import (
    REVIEW_AGENT_DEFINITIONS,
    REVIEW_SUBAGENT_DEFAULT_MODEL,
    REVIEW_SUBAGENT_DEFAULT_REASONING_EFFORT,
    review_agents_root,
)
from codex_dobby_mcp.runner import (
    CodexRunner,
    RunnerError,
    _augment_codex_error,
    _build_repo_snapshot,
    _codex_home_permission_issue,
    _collect_sandbox_violations,
    _mirror_runtime_config_references,
    _cleanup_private_child_runtime,
    _changed_status_files,
    _create_process_with_deadline,
    _execute_process_with_streaming_logs,
    _ensure_multi_agent_v2_available,
    _first_meaningful_output_line,
    _ghidra_usage_mode,
    _git_status,
    _path_fingerprint,
    _reverse_engineer_details_for_run,
    _reverse_engineer_failure_details,
    _review_execution_diagnostics,
    _quarantined_review_worker_result,
    _review_rollout_evidence as _raw_review_rollout_evidence,
    _rollout_validation_issues,
    _response_with_private_runtime_cleanup_failure,
    ReviewRolloutAgentEvidence,
    ReviewRolloutEvidence,
    _rewrite_path_prefix,
    _salvaged_review_worker_result_from_rollouts,
    _salvage_exec_trace,
    _salvage_worker_result_from_trace,
    _shielded_terminate_process,
    _stall_diagnostics,
    _stall_threshold_for_effort,
    _strict_child_worker_result,
    _strict_worker_result,
    _verified_review_worker_result_from_rollouts,
    _write_private_text,
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


def _review_execution_manifest(
    agents: list[ReviewAgent],
    *,
    statuses: dict[ReviewAgent, str] | None = None,
) -> dict[str, object]:
    statuses = statuses or {}
    return {
        "protocol": "multi_agent_v2",
        "agents": [
            {
                "review_agent": agent.value,
                "task_name": f"dobby_review_{agent.value}",
                "status": statuses.get(agent, "completed"),
                "summary": f"{agent.value} review {statuses.get(agent, 'completed')}",
            }
            for agent in agents
        ],
    }


def _rollout_evidence(
    agents: list[ReviewAgent],
    *,
    statuses: dict[ReviewAgent, str] | None = None,
):
    statuses = statuses or {}
    observations = []
    for agent in agents:
        status = ReviewAgentExecutionStatus(statuses.get(agent, "completed"))
        if status == ReviewAgentExecutionStatus.COMPLETED:
            completeness = Completeness.FULL
        elif status == ReviewAgentExecutionStatus.PARTIAL:
            completeness = Completeness.PARTIAL
        elif status == ReviewAgentExecutionStatus.BLOCKED:
            completeness = Completeness.BLOCKED
        else:
            completeness = None
        task_name = f"dobby_review_{agent.value}"
        child_result = (
            WorkerResult(
                summary=f"{agent.value} review {status.value}",
                completeness=completeness,
                important_facts=[f"{agent.value} independently verified finding"],
                next_steps=[f"{agent.value} independently verified next step"],
                files_changed=[],
                warnings=[],
                refused=False,
                file_diffs=[],
                review_execution=None,
            )
            if completeness is not None
            else None
        )
        observations.append(
            ReviewRolloutAgentEvidence(
                review_agent=agent.value,
                task_name=task_name,
                agent_role=task_name,
                spawn_attempt_count=1,
                started_count=1 if child_result is not None else 0,
                child_thread_ids=(f"thread-{agent.value}",) if child_result is not None else (),
                child_roles=(task_name,) if child_result is not None else (),
                delivered_count=1 if child_result is not None else 0,
                child_result=child_result,
                # A lens that never delivered a result never reached a
                # terminal event either. Claiming otherwise here is what let
                # the partial-coverage tests pass against a broken runner.
                terminal_event_count=1 if child_result is not None else 0,
                child_multi_agent_versions=("disabled",)
                if child_result is not None
                else (),
                started_thread_ids=(f"thread-{agent.value}",)
                if child_result is not None
                else (),
            )
        )
    return ReviewRolloutEvidence(
        collected=True,
        agents=tuple(observations),
        validation_issues=(),
    )


def _write_review_rollouts(
    codex_home: Path,
    statuses: dict[ReviewAgent, Completeness | None],
    *,
    modern_v2: bool = False,
    nested_collaboration: bool = False,
) -> None:
    """Write a production-shaped v2 rollout tree.

    A ``None`` completeness models a lens the parent spawned but whose child
    never started: the ``spawn_agent`` call is present, and nothing else is —
    no started event, no child session file, no terminal event, no delivery.
    """
    sessions_root = codex_home / "sessions" / "2026" / "08" / "12"
    sessions_root.mkdir(parents=True)
    root_thread_id = "root-thread"
    root_items: list[dict[str, object]] = [
        {
            "type": "session_meta",
            "payload": {
                "id": root_thread_id,
                "session_id": root_thread_id,
                "originator": "codex_exec",
                "thread_source": "user",
                "source": "exec",
            },
        },
        {
            "type": "turn_context",
            "payload": {
                "multi_agent_version": "v2",
            },
        },
    ]
    listed_agents: list[dict[str, object]] = []
    for agent, completeness in statuses.items():
        task_name = f"dobby_review_{agent.value}"
        agent_path = f"/root/{task_name}"
        child_thread_id = f"child-{agent.value}"
        spawn_payload = {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "arguments": json.dumps(
                    {
                        "task_name": task_name,
                        "agent_type": task_name,
                        "fork_turns": "none",
                        "message": "gAAAA" + ("A" * 256),
                    }
                ),
            },
        }
        if modern_v2:
            spawn_payload["payload"]["call_id"] = f"spawn-{agent.value}"
        root_items.append(spawn_payload)
        if completeness is None:
            # Spawn attempted, child never started. No further rollout
            # evidence exists for this lens.
            continue
        if modern_v2:
            root_items.append(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": f"spawn-{agent.value}",
                        "output": json.dumps({"task_name": agent_path}),
                    },
                }
            )
        else:
            root_items.append(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "sub_agent_activity",
                        "agent_path": agent_path,
                        "agent_thread_id": child_thread_id,
                        "kind": "started",
                    },
                }
            )
        assignment_digest = _fixture_assignment_digest(agent)
        child_result = WorkerResult(
            summary=(
                f"Dobby assignment {assignment_digest}: "
                f"{agent.value} child result"
            ),
            completeness=completeness,
            important_facts=[f"{agent.value} finding"],
            next_steps=[f"{agent.value} next step"],
            files_changed=[],
            warnings=[],
            refused=False,
            file_diffs=[],
            review_execution=None,
        )
        root_items.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "agent_message",
                    "author": agent_path,
                    "recipient": "/root",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                f"Message Type: FINAL_ANSWER\nTask name: /root/{task_name}\n"
                                f"Sender: {agent_path}\nPayload:\n{child_result.model_dump_json()}"
                            ),
                        }
                    ],
                },
            }
        )
        listed_agents.append(
            {
                "agent_name": agent_path,
                "agent_status": {"completed": child_result.model_dump_json()},
            }
        )
        child_items = [
            {
                "type": "session_meta",
                "payload": {
                    "id": child_thread_id,
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": root_thread_id,
                                "agent_path": agent_path,
                                "agent_role": task_name,
                            }
                        }
                    },
                    "thread_source": "subagent",
                    "agent_path": agent_path,
                    "multi_agent_version": "v2" if modern_v2 else "disabled",
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": child_result.model_dump_json(),
                },
            },
        ]
        if modern_v2 and nested_collaboration and agent == ReviewAgent.CORRECTNESS:
            child_items.insert(1, {
                "type": "response_item",
                "payload": {"type": "function_call", "name": "spawn_agent", "arguments": "{}"},
            })
        (sessions_root / f"{child_thread_id}.jsonl").write_text(
            "\n".join(json.dumps(item) for item in child_items) + "\n",
            encoding="utf-8",
        )
    root_items.extend(
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "list_agents",
                    "call_id": "list-agents-call",
                    "arguments": "{}",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "list-agents-call",
                    "output": json.dumps({"agents": listed_agents}),
                },
            },
        ]
    )
    (sessions_root / "root.jsonl").write_text(
        "\n".join(json.dumps(item) for item in root_items) + "\n",
        encoding="utf-8",
    )


def _fixture_canonical_assignment(agent: ReviewAgent) -> str:
    return json.dumps(
        {
            "files": ["runner.py"],
            "repo_root": "/repo",
            "task_name": f"dobby_review_{agent.value}",
            "task_prompt": "Inspect the requested code",
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _fixture_assignment_digest(agent: ReviewAgent) -> str:
    return hashlib.sha256(
        _fixture_canonical_assignment(agent).encode("utf-8")
    ).hexdigest()


def _fixture_spawn_messages(agents: list[ReviewAgent]) -> dict[str, str]:
    messages: dict[str, str] = {}
    for agent in agents:
        task_name = f"dobby_review_{agent.value}"
        canonical_assignment = _fixture_canonical_assignment(agent)
        assignment_digest = _fixture_assignment_digest(agent)
        messages[task_name] = (
            f"Required custom agent: {task_name}\n"
            f"Assigned lens: {REVIEW_AGENT_DEFINITIONS[agent].label}\n"
            f"Canonical assignment JSON: {canonical_assignment}\n"
            f"Canonical assignment SHA-256: {assignment_digest}\n"
            "Requirements:\n- return strict JSON"
        )
    return messages


def _review_rollout_evidence(
    codex_home: Path,
    agents: list[ReviewAgent],
    *,
    allow_truncated_tail: bool = False,
):
    return _raw_review_rollout_evidence(
        codex_home,
        agents,
        _fixture_spawn_messages(agents),
        allow_truncated_tail=allow_truncated_tail,
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

    async def fake_v2_capability(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        "codex_dobby_mcp.runner._ensure_multi_agent_v2_available",
        fake_v2_capability,
    )


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
    assert payload["required"] == [
        "summary",
        "completeness",
        "important_facts",
        "next_steps",
        "files_changed",
        "warnings",
        "refused",
        "file_diffs",
        "review_execution",
    ]
    agent_properties = payload["properties"]["review_execution"]["anyOf"][0]["properties"]["agents"]["items"]["properties"]
    assert agent_properties["task_name"]["minLength"] == 1
    assert agent_properties["summary"]["minLength"] == 1


def test_private_runtime_cleanup_failure_is_returned_and_escalates_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root = tmp_path / "private-runtime"
    private_root.mkdir()

    def fail_rmtree(_path: Path) -> None:
        raise OSError("resource busy")

    monkeypatch.setattr("codex_dobby_mcp.runner.shutil.rmtree", fail_rmtree)
    issue = _cleanup_private_child_runtime(private_root)

    assert issue is not None
    assert str(private_root) in issue
    assert "copied credentials and session data" in issue

    response = ToolResponse(
        task_id="task-1",
        tool=ToolName.REVIEW,
        status=RunStatus.SUCCESS,
        summary="review complete",
        completeness=Completeness.FULL,
        important_facts=["verified finding"],
        next_steps=[],
        files_changed=[],
        file_diffs=[],
        artifact_paths={},
        sandbox_violations=[],
        repo_root=str(tmp_path),
        warnings=["existing warning"],
        raw_output_available=True,
        model="gpt-5.5",
        reasoning_effort=ReasoningEffort.MEDIUM,
    )
    escalated = _response_with_private_runtime_cleanup_failure(response, issue)

    assert escalated.status == RunStatus.ERROR
    assert escalated.completeness == Completeness.BLOCKED
    assert escalated.stop_reason == StopReason.ERROR
    assert escalated.summary == issue
    assert escalated.warnings == [issue, "existing warning"]
    assert escalated.important_facts == ["verified finding"]


def test_default_reasoning_efforts_include_high_build_and_medium_validate() -> None:
    assert DEFAULT_REASONING_EFFORTS[ToolName.BUILD] == ReasoningEffort.HIGH
    assert DEFAULT_REASONING_EFFORTS[ToolName.VALIDATE] == ReasoningEffort.MEDIUM


def test_review_details_remain_compatible_with_pre_v2_result_artifacts() -> None:
    details = ReviewDetails.model_validate(
        {
            "requested_review_agents": ["correctness", "regression"],
            "effective_review_agents": ["correctness", "regression"],
        }
    )

    assert details.orchestration_protocol is None
    assert details.execution_evidence is None
    assert details.execution_manifest is None
    assert details.manifest_validated is False
    assert details.coverage_complete is None
    assert details.completed_agents is None


def test_ghidra_usage_mode_distinguishes_direct_helper_and_startup_only() -> None:
    assert _ghidra_usage_mode(["list_instances", "connect_instance", "list_open_programs"], []) == GhidraUsageMode.DIRECT_MCP
    assert _ghidra_usage_mode(["list_instances", "connect_instance"], ["list_open_programs", "search_strings"]) == GhidraUsageMode.HELPER_FALLBACK
    assert _ghidra_usage_mode(["list_instances", "connect_instance", "check_tools"], []) == GhidraUsageMode.STARTUP_ONLY
    assert _ghidra_usage_mode([], []) == GhidraUsageMode.NO_ACTIVITY


def test_reverse_engineer_details_report_helper_fallback_calls(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )
    spec = runner._resolve(ToolName.REVERSE_ENGINEER, InvocationRequest(prompt="inspect binary"))
    spec = spec.model_copy(update={"ghidra_available": True})

    details = _reverse_engineer_details_for_run(
        spec,
        stdout="",
        stderr="\n".join(
            [
                "mcp: ghidra/list_instances started",
                "mcp: ghidra/connect_instance started",
                "mcp: ghidra/check_tools started",
                "    open_programs = gh.dispatch_get('/list_open_programs')",
                "        strings = gh.dispatch_get('/search_strings', params={'search_term': 'Dirac'})",
            ]
        ),
    )

    assert details is not None
    assert details.ghidra is not None
    assert details.ghidra.mode == GhidraUsageMode.HELPER_FALLBACK
    assert details.ghidra.mcp_calls == ["list_instances", "connect_instance", "check_tools"]
    assert details.ghidra.helper_calls == ["list_open_programs", "search_strings"]


def test_reverse_engineer_failure_details_report_prelaunch_failure(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )
    spec = runner._resolve(ToolName.REVERSE_ENGINEER, InvocationRequest(prompt="inspect binary"))
    spec = spec.model_copy(update={"ghidra_available": True})

    details = _reverse_engineer_failure_details(spec, prelaunch_failure=True)

    assert details is not None
    assert details.ghidra is not None
    assert details.ghidra.mode == GhidraUsageMode.PRELAUNCH_FAILURE
    assert details.ghidra.summary == "Ghidra was configured, but Dobby failed before any child Ghidra activity could run."


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
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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


def test_multi_agent_review_uses_low_parent_reasoning_by_default(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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

    assert spec.model == "gpt-6.1-sol"
    assert spec.reasoning_effort == ReasoningEffort.LOW


def test_multi_agent_review_passes_timeout_and_agents_through_uncapped(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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


def test_runner_rejects_prompt_referencing_external_repo_without_repo_root(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")
    external_repo = tmp_path / "external"
    external_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(external_repo)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    with pytest.raises(PathResolutionError, match="repo_root was not provided"):
        runner._resolve(
            ToolName.REVERSE_ENGINEER,
            InvocationRequest(prompt=f"Investigate {external_repo}/src/main.rs"),
        )


def test_runner_rejects_relative_files_missing_from_spawn_root(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    with pytest.raises(PathResolutionError, match="do not exist under server cwd"):
        runner._resolve(
            ToolName.REVIEW,
            _request(
                prompt=(
                    "Review `native/mic-capture/src/win_capture.cpp` and "
                    "`src/main/mic-capture/service.ts`."
                ),
            ),
        )


def test_runner_rejects_relative_files_param_missing_from_spawn_root(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    with pytest.raises(PathResolutionError, match="do not exist under server cwd"):
        runner._resolve(
            ToolName.REVIEW,
            _request(
                prompt="Review the windows capture work.",
                files=["native/mic-capture/src/win_capture.cpp"],
            ),
        )


def test_runner_accepts_relative_files_present_in_spawn_root(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")
    target = repo_root / "src" / "app.ts"
    target.parent.mkdir(parents=True)
    target.write_text("// present\n", encoding="utf-8")

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
        _request(prompt="Review `src/app.ts`."),
    )

    assert spec.repo_root == repo_root.resolve()


def test_runner_allows_external_repo_when_repo_root_is_explicit(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")
    external_repo = tmp_path / "external"
    external_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(external_repo)], check=True)

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    spec = runner._resolve(
        ToolName.REVERSE_ENGINEER,
        InvocationRequest(
            prompt=f"Investigate {external_repo}/src/main.rs",
            repo_root=str(external_repo),
        ),
    )

    assert spec.repo_root == external_repo.resolve()


def test_short_timeout_plan_uses_medium_reasoning_by_default(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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

    assert spec.model == "gpt-6.1-sol"
    assert spec.reasoning_effort == ReasoningEffort.MEDIUM


def test_persist_request_keeps_requested_timeout_and_records_effective_timeout(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": _review_execution_manifest(
                        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
                        statuses={ReviewAgent.REGRESSION: "failed"},
                    ),
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
        "codex_dobby_mcp.runner._review_rollout_evidence",
        lambda *_args, **_kwargs: _rollout_evidence(
            [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION]
        ),
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
    assert result.important_facts == [
        "correctness independently verified finding",
        "regression independently verified finding",
    ]
    assert any(
        "Dobby rebuilt the response from independently verified child rollout results"
        in warning
        for warning in result.warnings
    )
    assert result.review_details is not None
    assert (
        result.review_details.orchestration_protocol
        == ReviewOrchestrationProtocol.MULTI_AGENT_V2
    )
    assert (
        result.review_details.execution_evidence
        == ReviewExecutionEvidence.CODEX_SESSION_ROLLOUTS
    )
    assert result.review_details.manifest_validated is True
    assert result.review_details.execution_manifest is not None
    assert result.review_details.coverage_complete is True
    assert result.review_details.completed_agents == 2
    assert result.review_details.partial_agents == 0
    assert result.review_details.blocked_agents == 0
    assert result.review_details.failed_agents == 0


@pytest.mark.asyncio
async def test_multi_agent_review_passes_all_requested_agents_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": _review_execution_manifest(
                        [
                            ReviewAgent.SECURITY,
                            ReviewAgent.ARCHITECTURE,
                            ReviewAgent.CORRECTNESS,
                            ReviewAgent.REGRESSION,
                            ReviewAgent.PERFORMANCE,
                        ]
                    ),
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
        "codex_dobby_mcp.runner._review_rollout_evidence",
        lambda *_args, **_kwargs: _rollout_evidence(
            [
                ReviewAgent.SECURITY,
                ReviewAgent.ARCHITECTURE,
                ReviewAgent.CORRECTNESS,
                ReviewAgent.REGRESSION,
                ReviewAgent.PERFORMANCE,
            ]
        ),
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
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": None,
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
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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


_AUTH_ERROR_STDOUT = (
    '{"type":"error","message":"Your access token could not be refreshed '
    "because your refresh token was already used. Please log out and sign in "
    'again."}\n'
    '{"type":"turn.failed","error":{"message":"Your access token could not be '
    'refreshed because your refresh token was already used."}}\n'
)


def test_resolve_summary_surfaces_codex_auth_error_over_orchestration_summary() -> None:
    # The exact production bug: a codex auth failure kills the review parent
    # before any subagent spawns, so the orchestrator reports "completed 0/N
    # subagents" in error_reasons. The real, actionable codex error (sitting in
    # stdout) must win and carry the re-login hint.
    summary = CodexRunner._resolve_summary(
        None,
        stdout=_AUTH_ERROR_STDOUT,
        stderr="",
        exit_code=1,
        timeout_hit=False,
        worker_result_error=None,
        error_reasons=[
            "Review orchestration incomplete: completed 0/2 subagents; spawned 0/2 subagents."
        ],
    )

    assert summary.startswith("Your access token could not be refreshed")
    assert "completed 0/2 subagents" not in summary
    assert "codex logout && codex login" in summary


def test_resolve_summary_adds_hint_for_token_expired_variant() -> None:
    stdout = (
        '{"type":"error","message":"Provided authentication token is expired. '
        'Please try signing in again."}\n'
    )
    summary = CodexRunner._resolve_summary(
        None,
        stdout=stdout,
        stderr="",
        exit_code=1,
        timeout_hit=False,
        worker_result_error=None,
        error_reasons=[],
    )

    assert "authentication token is expired" in summary
    assert "codex logout && codex login" in summary


def test_resolve_summary_stall_reason_not_clobbered_by_codex_error_scan() -> None:
    # A stall never emits a turn-level error, so the stall diagnostic (in
    # error_reasons) must remain the summary even though stall_hit forces a
    # non-zero exit.
    stall_reason = "Codex produced no stdout or stderr activity for 300 seconds and was killed as stalled."
    summary = CodexRunner._resolve_summary(
        None,
        stdout="",
        stderr="",
        exit_code=-15,
        timeout_hit=False,
        worker_result_error=None,
        error_reasons=[stall_reason],
        stall_hit=True,
    )

    assert summary == stall_reason


def test_augment_codex_error_leaves_non_auth_errors_unchanged() -> None:
    message = "Model returned invalid response_format: expected object, got array"
    assert _augment_codex_error(message) == message


def test_augment_codex_error_flags_refresh_token_reuse() -> None:
    message = (
        "Your access token could not be refreshed because your refresh token "
        "was already used. Please log out and sign in again."
    )
    augmented = _augment_codex_error(message)
    assert augmented.startswith(message)
    assert "codex logout && codex login" in augmented


def test_git_status_parses_quoted_renamed_and_filters_ignored_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_argv: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):  # type: ignore[no-untyped-def]
        captured_argv.append(list(argv))

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

    # Ignored (``!!``) entries are filtered so build artifacts / caches never
    # enter the snapshot; the rename keeps only its new path.
    assert _git_status(tmp_path) == ["normal.py", 'weird name "quote".py', "renamed.py"]
    # And we no longer ask git to enumerate ignored files at all.
    assert not any("--ignored=matching" in argv for argv in captured_argv)


def test_build_repo_snapshot_excludes_gitignored_paths(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / ".gitignore").write_text("build_out/\n", encoding="utf-8")
    tracked = repo_root / "keep.py"
    tracked.write_text("x = 1\n", encoding="utf-8")
    ignored_dir = repo_root / "build_out"
    ignored_dir.mkdir()
    (ignored_dir / "artifact.bin").write_text("huge", encoding="utf-8")

    # Mutating (content-fingerprint) snapshot is where the double-hash of
    # ignored trees used to happen.
    snapshot = _build_repo_snapshot(
        repo_root,
        include_head=False,
        use_metadata_fingerprints=False,
    )

    assert "keep.py" in snapshot.dirty_files
    assert not any(path.startswith("build_out") for path in snapshot.dirty_files)


def test_rewrite_path_prefix_only_replaces_at_path_boundary() -> None:
    text = 'x = "~/.codex/config.toml"\ny = "~/.codex-backup/keep"\nz = "~/.codex"\n'
    out = _rewrite_path_prefix(text, "~/.codex", "/private/child-home")

    assert '"/private/child-home/config.toml"' in out  # real reference rewritten
    assert '"~/.codex-backup/keep"' in out  # sibling name left intact
    assert '"/private/child-home"' in out  # bare reference (quote boundary) rewritten


def test_write_private_text_creates_owner_only_file(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    _write_private_text(target, "[mcp_servers.x]\nkey = 'secret'\n")

    assert target.read_text(encoding="utf-8") == "[mcp_servers.x]\nkey = 'secret'\n"
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_shielded_terminate_reaps_process_despite_outer_cancel() -> None:
    class _StubbornProcess:
        """A codex that ignores SIGTERM and only dies on SIGKILL."""

        def __init__(self) -> None:
            self.returncode: int | None = None
            self.terminated = False
            self.killed = False

        def terminate(self) -> None:
            self.terminated = True  # ignored: does not set returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            while self.returncode is None:
                await asyncio.sleep(0.005)
            return self.returncode

    process = _StubbornProcess()

    async def caller() -> None:
        await _shielded_terminate_process(process, timeout=0.05)

    task = asyncio.ensure_future(caller())
    await asyncio.sleep(0)  # let the terminate escalation start
    task.cancel()  # cancel the outer await mid-reap
    with suppress(asyncio.CancelledError):
        await task

    # The escalation must still have completed and reaped the process.
    assert process.terminated is True
    assert process.killed is True
    assert process.returncode is not None


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
    (repo_root / "runner.py").write_text("", encoding="utf-8")
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


def test_v2_review_execution_manifest_validates_exact_agent_task_mapping() -> None:
    worker_result = WorkerResult.model_validate(
        {
            "summary": "review complete",
            "completeness": "full",
            "important_facts": [],
            "next_steps": [],
            "files_changed": [],
            "warnings": [],
            "refused": False,
            "file_diffs": [],
            "review_execution": _review_execution_manifest(
                [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION]
            ),
        }
    )

    diagnostics = _review_execution_diagnostics(
        worker_result,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        _rollout_evidence([ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION]),
    )

    assert diagnostics.manifest_validated is True
    assert diagnostics.all_completed is True
    assert diagnostics.usable_count == 2
    assert diagnostics.warnings == []


def test_v2_review_execution_manifest_requires_independent_rollout_evidence() -> None:
    worker_result = WorkerResult.model_validate(
        {
            "summary": "parent claims both reviewers completed",
            "completeness": "full",
            "important_facts": [],
            "next_steps": [],
            "files_changed": [],
            "warnings": [],
            "refused": False,
            "file_diffs": [],
            "review_execution": _review_execution_manifest(
                [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION]
            ),
        }
    )

    diagnostics = _review_execution_diagnostics(
        worker_result,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    assert diagnostics.manifest_validated is False
    assert diagnostics.execution_evidence is None
    assert diagnostics.validation_issues == (
        "Codex session rollout evidence was not collected",
    )


def test_same_result_delivered_through_both_channels_is_one_delivery(
    tmp_path: Path,
) -> None:
    """review.md asks the parent to observe two channels for the same result.

    The child emits a FINAL_ANSWER message and list_agents reports a completed
    payload. Counting distinct payload *bytes* made a one-byte formatting
    difference between those copies look like a second delivery — an integrity
    failure that quarantined a review in which every lens had succeeded.
    """
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    # Re-emit the correctness lens's list_agents payload with trailing
    # whitespace: same result, different bytes.
    root_rollout = next((codex_home / "sessions").rglob("root.jsonl"))
    items = [json.loads(line) for line in root_rollout.read_text(encoding="utf-8").splitlines()]
    for item in items:
        payload = item.get("payload", {})
        if payload.get("type") != "function_call_output":
            continue
        listed = json.loads(payload["output"])
        for entry in listed["agents"]:
            if entry["agent_name"].endswith("correctness"):
                entry["agent_status"]["completed"] += "\n"
        payload["output"] = json.dumps(listed)
    root_rollout.write_text(
        "\n".join(json.dumps(item) for item in items) + "\n", encoding="utf-8"
    )

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    correctness = evidence.by_task_name["dobby_review_correctness"]
    assert correctness.delivered_count == 1
    assert correctness.observed_status == ReviewAgentExecutionStatus.COMPLETED

    worker_result = _verified_review_worker_result_from_rollouts(evidence)
    diagnostics = _review_execution_diagnostics(
        worker_result,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        evidence,
    )
    assert diagnostics.validation_issues == ()
    assert diagnostics.manifest_validated is True
    assert diagnostics.usable_count == 2


def test_two_genuinely_different_delivered_results_still_fail_integrity(
    tmp_path: Path,
) -> None:
    """Semantic counting must not excuse a second, different result."""
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    root_rollout = next((codex_home / "sessions").rglob("root.jsonl"))
    items = [json.loads(line) for line in root_rollout.read_text(encoding="utf-8").splitlines()]
    for item in items:
        payload = item.get("payload", {})
        if payload.get("type") != "function_call_output":
            continue
        listed = json.loads(payload["output"])
        for entry in listed["agents"]:
            if entry["agent_name"].endswith("correctness"):
                swapped = json.loads(entry["agent_status"]["completed"])
                swapped["important_facts"] = ["a different finding entirely"]
                entry["agent_status"]["completed"] = json.dumps(swapped)
        payload["output"] = json.dumps(listed)
    root_rollout.write_text(
        "\n".join(json.dumps(item) for item in items) + "\n", encoding="utf-8"
    )

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    correctness = evidence.by_task_name["dobby_review_correctness"]
    assert correctness.observed_status == ReviewAgentExecutionStatus.FAILED
    assert correctness.child_result is None


def test_v2_review_partial_coverage_survives_a_lens_that_never_delivered(
    tmp_path: Path,
) -> None:
    """A lens that never starts must not discard the lens that did deliver.

    Drives ``_review_rollout_evidence`` over a real rollout tree rather than a
    synthetic evidence helper, because the helper is what previously let this
    path pass while production returned ``status: error``.
    """
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: None,
        },
    )
    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    assert evidence.collected is True
    assert evidence.validation_issues == ()
    regression = evidence.by_task_name["dobby_review_regression"]
    assert regression.terminal_event_count == 0
    assert regression.child_result is None
    assert [agent.observed_status for agent in evidence.agents] == [
        ReviewAgentExecutionStatus.COMPLETED,
        ReviewAgentExecutionStatus.FAILED,
    ]

    worker_result = _verified_review_worker_result_from_rollouts(evidence)
    diagnostics = _review_execution_diagnostics(
        worker_result,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        evidence,
    )

    # The manifest is honest about the failure, so its integrity holds.
    assert diagnostics.validation_issues == ()
    assert diagnostics.manifest_validated is True
    assert diagnostics.usable_count == 1
    assert diagnostics.failed_count == 1
    assert diagnostics.all_completed is False

    # The failure is reported as lost coverage, not as tampering.
    assert any(
        "dobby_review_regression delivered no usable review result" in issue
        for issue in diagnostics.coverage_issues
    )
    # Previously unreachable: a validated manifest reporting a failed lens.
    assert any(
        "0 blocked, and 1 failed of 2 selected review agents" in warning
        for warning in diagnostics.warnings
    )

    # The surviving lens's independently verified findings are still returned.
    assert worker_result.completeness == Completeness.PARTIAL
    assert "correctness finding" in worker_result.important_facts
    assert worker_result.review_execution.agents[1].status == (
        ReviewAgentExecutionStatus.FAILED
    )


def test_failed_lens_prose_never_reaches_the_caller_through_review_details(
    tmp_path: Path,
) -> None:
    """A failed lens contributes no words, not just no findings.

    ``child_result`` can be populated for a lens whose evidence still failed,
    so keying the per-agent manifest summary on the payload merely existing let
    an excluded child's own prose out through ``review_details``.
    """
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )
    leaked = "regression child result"
    assert any(
        agent.child_result is not None and leaked in agent.child_result.summary
        for agent in evidence.agents
    )

    # Force the regression lens to fail while keeping its delivered payload,
    # which is exactly the shape a spawn-contract violation produces.
    agents = tuple(
        agent
        if agent.task_name != "dobby_review_regression"
        else replace(
            agent,
            spawn_contract_issues=(
                'dobby_review_regression spawn did not use fork_turns="none"',
            ),
        )
        for agent in evidence.agents
    )
    tainted = replace(evidence, agents=agents)
    regression = tainted.by_task_name["dobby_review_regression"]
    assert regression.observed_status == ReviewAgentExecutionStatus.FAILED
    assert regression.child_result is not None

    verified = _verified_review_worker_result_from_rollouts(tainted)
    rendered = json.dumps(verified.model_dump(mode="json"))

    assert leaked not in rendered
    assert "regression finding" not in rendered
    entry = next(
        agent
        for agent in verified.review_execution.agents
        if agent.task_name == "dobby_review_regression"
    )
    assert entry.status == ReviewAgentExecutionStatus.FAILED
    assert "No usable delivered child result" in entry.summary
    # The lens that did pass is still reported in full.
    assert "correctness finding" in verified.important_facts


def test_v2_review_survives_a_lens_the_parent_never_spawned(
    tmp_path: Path,
) -> None:
    """A missing spawn removes one lens; it does not discard the others.

    A shortfall in the global spawn count used to be a run-wide integrity
    failure, which quarantined every lens that had delivered verified findings.
    An excess still is one, because it means something ran that was not asked
    for.
    """
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: None,
        },
    )
    # Drop the regression spawn call entirely, so the parent never even tried.
    root_rollout = next((codex_home / "sessions").glob("**/root.jsonl"))
    kept = []
    for line in root_rollout.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        payload = item.get("payload", {})
        if payload.get("name") == "spawn_agent" and "regression" in payload.get("arguments", ""):
            continue
        kept.append(line)
    root_rollout.write_text("\n".join(kept) + "\n", encoding="utf-8")

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    assert evidence.validation_issues == ()
    regression = evidence.by_task_name["dobby_review_regression"]
    assert regression.spawn_attempt_count == 0
    assert regression.observed_status == ReviewAgentExecutionStatus.FAILED

    worker_result = _verified_review_worker_result_from_rollouts(evidence)
    diagnostics = _review_execution_diagnostics(
        worker_result,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        evidence,
    )

    assert diagnostics.manifest_validated is True
    assert diagnostics.usable_count == 1
    assert any("spawn_attempts=0" in issue for issue in diagnostics.coverage_issues)
    assert "correctness finding" in worker_result.important_facts


def test_v2_review_manifest_still_fails_when_it_claims_a_missing_lens_completed(
    tmp_path: Path,
) -> None:
    """Honest failure is coverage; a manifest that lies is still quarantined."""
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: None,
        },
    )
    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )
    lying_result = WorkerResult.model_validate(
        {
            "summary": "parent claims both lenses completed",
            "completeness": "full",
            "important_facts": ["correctness finding"],
            "next_steps": [],
            "files_changed": [],
            "warnings": [],
            "refused": False,
            "file_diffs": [],
            "review_execution": _review_execution_manifest(
                [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
            ),
        }
    )

    diagnostics = _review_execution_diagnostics(
        lying_result,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        evidence,
    )

    assert diagnostics.manifest_validated is False
    assert any(
        "dobby_review_regression reported completed but Codex rollout evidence shows failed"
        in issue
        for issue in diagnostics.validation_issues
    )


def test_v2_review_rollouts_verify_child_role_delivery_and_blocked_status(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.BLOCKED,
        },
    )
    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )
    worker_result = WorkerResult.model_validate(
        {
            "summary": "one child completed and one was blocked",
            "completeness": "partial",
            "important_facts": ["correctness finding"],
            "next_steps": [],
            "files_changed": [],
            "warnings": [],
            "refused": False,
            "file_diffs": [],
            "review_execution": _review_execution_manifest(
                [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
                statuses={ReviewAgent.REGRESSION: "blocked"},
            ),
        }
    )

    diagnostics = _review_execution_diagnostics(
        worker_result,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        evidence,
    )

    assert evidence.collected is True
    assert evidence.validation_issues == ()
    assert [agent.observed_status for agent in evidence.agents] == [
        ReviewAgentExecutionStatus.COMPLETED,
        ReviewAgentExecutionStatus.BLOCKED,
    ]
    assert diagnostics.manifest_validated is True
    assert diagnostics.execution_evidence == ReviewExecutionEvidence.CODEX_SESSION_ROLLOUTS
    assert diagnostics.usable_count == 1
    assert diagnostics.blocked_count == 1


def test_v2_review_rollouts_treat_all_blocked_children_as_unusable(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.BLOCKED,
            ReviewAgent.REGRESSION: Completeness.BLOCKED,
        },
    )
    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )
    worker_result = WorkerResult.model_validate(
        {
            "summary": "both reviewers were blocked",
            "completeness": "blocked",
            "important_facts": [],
            "next_steps": [],
            "files_changed": [],
            "warnings": [],
            "refused": False,
            "file_diffs": [],
            "review_execution": _review_execution_manifest(
                [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
                statuses={
                    ReviewAgent.CORRECTNESS: "blocked",
                    ReviewAgent.REGRESSION: "blocked",
                },
            ),
        }
    )

    diagnostics = _review_execution_diagnostics(
        worker_result,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        evidence,
    )

    assert diagnostics.manifest_validated is True
    assert diagnostics.blocked_count == 2
    assert diagnostics.usable_count == 0


def test_v2_review_rollouts_reject_parent_status_that_contradicts_child_result(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.BLOCKED,
        },
    )
    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )
    worker_result = WorkerResult.model_validate(
        {
            "summary": "fabricated full result",
            "completeness": "full",
            "important_facts": [],
            "next_steps": [],
            "files_changed": [],
            "warnings": [],
            "refused": False,
            "file_diffs": [],
            "review_execution": _review_execution_manifest(
                [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION]
            ),
        }
    )

    diagnostics = _review_execution_diagnostics(
        worker_result,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        evidence,
    )

    assert diagnostics.manifest_validated is False
    assert any(
        issue.startswith(
            "dobby_review_regression reported completed but Codex rollout evidence shows blocked"
        )
        for issue in diagnostics.validation_issues
    )


def test_v2_review_rollouts_ignore_interim_messages_and_bind_exact_final_delivery(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    root_rollout = codex_home / "sessions" / "2026" / "08" / "12" / "root.jsonl"
    interim = {
        "type": "response_item",
        "payload": {
            "type": "agent_message",
            "author": "/root/dobby_review_correctness",
            "recipient": "/root",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "Message Type: MESSAGE\nTask name: /root/dobby_review_correctness\n"
                        "Sender: /root/dobby_review_correctness\nPayload:\nstill working"
                    ),
                }
            ],
        },
    }
    root_rollout.write_text(
        root_rollout.read_text(encoding="utf-8") + json.dumps(interim) + "\n",
        encoding="utf-8",
    )

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    assert evidence.agents[0].delivered_count == 1
    assert evidence.agents[0].observed_status == ReviewAgentExecutionStatus.COMPLETED

    root_rollout.write_text(
        root_rollout.read_text(encoding="utf-8").replace(
            "correctness finding",
            "different parent payload",
            1,
        ),
        encoding="utf-8",
    )
    mismatched = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    assert mismatched.agents[0].delivered_count == 2
    assert mismatched.agents[0].child_result is None
    assert mismatched.agents[0].observed_status == ReviewAgentExecutionStatus.FAILED


def test_v2_review_rollouts_accept_framework_encrypted_final_delivery_payload(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    root_rollout = codex_home / "sessions" / "2026" / "08" / "12" / "root.jsonl"
    items = [json.loads(line) for line in root_rollout.read_text(encoding="utf-8").splitlines()]
    for item in items:
        payload = item.get("payload", {})
        if (
            payload.get("type") == "agent_message"
            and payload.get("author") == "/root/dobby_review_correctness"
        ):
            text_part = payload["content"][0]
            text_part["text"] = text_part["text"].split("\nPayload:\n", 1)[0]
            payload["content"].append(
                {"type": "encrypted_content", "encrypted_content": "ciphertext"}
            )
    root_rollout.write_text(
        "\n".join(json.dumps(item) for item in items) + "\n",
        encoding="utf-8",
    )

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    assert evidence.agents[0].delivered_count == 1
    assert evidence.agents[0].child_result is not None
    assert evidence.agents[0].observed_status == ReviewAgentExecutionStatus.COMPLETED


def test_v2_review_rollouts_reject_malformed_delivered_payload(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    root_rollout = codex_home / "sessions" / "2026" / "08" / "12" / "root.jsonl"
    items = [json.loads(line) for line in root_rollout.read_text(encoding="utf-8").splitlines()]
    for item in items:
        payload = item.get("payload", {})
        if (
            payload.get("type") == "agent_message"
            and payload.get("author") == "/root/dobby_review_correctness"
        ):
            text_part = payload["content"][0]
            text_part["text"] = text_part["text"].split("\nPayload:\n", 1)[0] + "\nPayload:\nnot-json"
        if payload.get("type") == "function_call_output":
            listed = json.loads(payload["output"])
            for listed_agent in listed["agents"]:
                if listed_agent["agent_name"] == "/root/dobby_review_correctness":
                    listed_agent["agent_status"]["completed"] = "not-json"
            payload["output"] = json.dumps(listed)
    root_rollout.write_text(
        "\n".join(json.dumps(item) for item in items) + "\n",
        encoding="utf-8",
    )

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    assert evidence.agents[0].delivered_count == 1
    assert evidence.agents[0].child_result is None
    assert evidence.agents[0].observed_status == ReviewAgentExecutionStatus.FAILED


def test_v2_review_rollouts_accept_parent_observed_list_delivery_without_agent_message(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    root_rollout = codex_home / "sessions" / "2026" / "08" / "12" / "root.jsonl"
    items = [json.loads(line) for line in root_rollout.read_text(encoding="utf-8").splitlines()]
    items = [
        item
        for item in items
        if not (
            item.get("type") == "response_item"
            and item.get("payload", {}).get("type") == "agent_message"
            and item.get("payload", {}).get("author")
            == "/root/dobby_review_correctness"
        )
    ]
    root_rollout.write_text(
        "\n".join(json.dumps(item) for item in items) + "\n",
        encoding="utf-8",
    )

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    assert evidence.agents[0].delivered_count == 1
    assert evidence.agents[0].child_result is not None
    assert evidence.agents[0].observed_status == ReviewAgentExecutionStatus.COMPLETED


def test_v2_review_rollouts_reject_extra_malformed_spawn_attempt(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    root_rollout = codex_home / "sessions" / "2026" / "08" / "12" / "root.jsonl"
    malformed_spawn = {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "spawn_agent",
            "arguments": "not-json",
        },
    }
    root_rollout.write_text(
        root_rollout.read_text(encoding="utf-8")
        + json.dumps(malformed_spawn)
        + "\n",
        encoding="utf-8",
    )

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    # The unparseable spawn is itself a global failure: it cannot be attributed
    # to a lens. The raw spawn *count* deliberately is not — see
    # test_duplicate_spawn_of_one_lens_keeps_the_other_lenses_findings.
    assert "v2 spawn attempt contained malformed JSON arguments" in evidence.validation_issues


def test_duplicate_spawn_of_one_lens_keeps_the_other_lenses_findings(
    tmp_path: Path,
) -> None:
    """A duplicate spawn is per-lens integrity loss, never a global quarantine.

    AGENTS.md lists duplicate spawns among the deviations that "exclude that
    lens entirely ... but the lenses that passed still return their
    independently verified findings", and reserves whole-review quarantine for
    a *global* evidence failure. A raw `spawn_call_count > len(definitions)`
    check contradicted that: spawning one lens twice discarded every other
    lens's verified findings too.
    """
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    root_rollout = codex_home / "sessions" / "2026" / "08" / "12" / "root.jsonl"
    items = [json.loads(line) for line in root_rollout.read_text(encoding="utf-8").splitlines()]
    duplicate = next(
        item
        for item in items
        if item.get("payload", {}).get("name") == "spawn_agent"
        and "correctness" in item["payload"].get("arguments", "")
    )
    items.append(json.loads(json.dumps(duplicate)))
    root_rollout.write_text(
        "\n".join(json.dumps(item) for item in items) + "\n",
        encoding="utf-8",
    )

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    # No global quarantine: the review is still evaluated lens by lens.
    assert evidence.validation_issues == ()

    by_task = {agent.task_name: agent for agent in evidence.agents}
    correctness = by_task["dobby_review_correctness"]
    regression = by_task["dobby_review_regression"]

    # The duplicated lens is excluded as an integrity failure...
    assert correctness.spawn_attempt_count == 2
    assert any(
        "spawn attempts" in issue
        for issue in _rollout_validation_issues(
            correctness, ReviewAgentExecutionStatus.COMPLETED
        ).integrity
    )
    # ...and the untouched lens is untouched.
    assert regression.spawn_attempt_count == 1
    lens_issues = _rollout_validation_issues(
        regression, ReviewAgentExecutionStatus.COMPLETED
    )
    assert not lens_issues.integrity
    assert not lens_issues.coverage

    # The caller still receives the surviving lens's findings rather than the
    # empty quarantine result...
    result = _verified_review_worker_result_from_rollouts(evidence)
    assert result.summary != _quarantined_review_worker_result().summary
    assert regression.child_result is not None
    assert set(regression.child_result.important_facts) <= set(result.important_facts)

    # ...and the duplicated lens's findings do not reach the caller. Asserted on
    # the caller-visible result, not only on `_rollout_validation_issues`:
    # removing the redundant global spawn-count check left exclusion resting
    # solely on `observed_status`'s `spawn_attempt_count != 1`, and weakening
    # that single predicate leaks a compromised lens's findings while every
    # other assertion in this test still passes.
    assert not any("correctness" in fact for fact in result.important_facts)
    assert not any("correctness" in step for step in result.next_steps)


def test_rollout_without_readable_session_metadata_fails_closed(tmp_path: Path) -> None:
    """An unaccountable rollout file must quarantine, not be skipped.

    `codex_home` is the run's private runtime, so every rollout under it
    belongs to this run. Silently dropping a file whose `session_meta` is
    missing let an extra or nested child session escape classification while
    `manifest_validated` stayed true.
    """
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    stray = codex_home / "sessions" / "2026" / "08" / "12" / "stray.jsonl"
    # Deliberately a record type `_is_consumed_rollout_record` discards. Real
    # rollouts are mostly reasoning traces and streaming deltas, so a stray
    # session can easily contain no consumed record at all. Testing this with a
    # consumed type instead would also pass under the weaker `elif items:`
    # predicate and so would not pin the fail-closed rule to file content.
    stray.write_text(
        json.dumps({"type": "reasoning", "payload": {"text": "no session_meta here"}})
        + "\n",
        encoding="utf-8",
    )

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    assert any(
        "no readable session metadata" in issue for issue in evidence.validation_issues
    )
    assert _verified_review_worker_result_from_rollouts(evidence).summary == (
        _quarantined_review_worker_result().summary
    )


def test_unparseable_session_is_accounted_for_during_truncated_recovery(
    tmp_path: Path,
) -> None:
    """Recovery mode must not become a hole in the fail-closed rule.

    `allow_truncated_tail=True` is used when the run timed out or exited
    nonzero. A session file whose only line is a truncated `session_meta`
    parses to nothing under that tolerance, so keying the rule on *parsed*
    records rather than file content would skip exactly the unaccountable
    session the rule exists to catch.
    """
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    truncated = codex_home / "sessions" / "2026" / "08" / "12" / "truncated.jsonl"
    # No trailing newline: this is how a truncated tail presents.
    truncated.write_text('{"type": "session_meta", "payload": {"id": "gh', encoding="utf-8")

    recovered = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        allow_truncated_tail=True,
    )

    assert any(
        "no readable session metadata" in issue for issue in recovered.validation_issues
    )


def test_empty_rollout_file_is_not_treated_as_a_session(tmp_path: Path) -> None:
    """A file with no records is not evidence of a session, so it must not fail.

    Guards the fail-closed rule above from over-firing on a rollout Codex has
    created but not yet written to.
    """
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    (codex_home / "sessions" / "2026" / "08" / "12" / "empty.jsonl").write_text(
        "", encoding="utf-8"
    )

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    assert evidence.validation_issues == ()


def test_child_session_without_spawn_identity_is_an_unexpected_session(
    tmp_path: Path,
) -> None:
    """A non-root session that cannot prove it is an expected child fails closed.

    Previously a child rollout whose `source.subagent.thread_spawn.agent_path`
    was missing or non-string was skipped by the classification loop, so an
    unaccounted child session left no trace in `validation_issues`.
    """
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    rogue = codex_home / "sessions" / "2026" / "08" / "12" / "rogue.jsonl"
    rogue.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": "rogue-thread",
                    "session_id": "rogue-thread",
                    "originator": "codex_exec",
                    "thread_source": "subagent",
                    # Well-formed session metadata that simply never identifies
                    # which spawn produced it.
                    "source": {"subagent": {"thread_spawn": {"agent_path": None}}},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    assert any(
        "unexpected or nested v2 child sessions" in issue
        for issue in evidence.validation_issues
    )
    assert "rogue-thread" in " ".join(evidence.validation_issues)


def test_v2_review_rollouts_allow_only_truncated_final_record_during_recovery(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    root_rollout = codex_home / "sessions" / "2026" / "08" / "12" / "root.jsonl"
    root_rollout.write_text(
        root_rollout.read_text(encoding="utf-8") + '{"type":"response_item"',
        encoding="utf-8",
    )

    strict = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )
    recovered = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        allow_truncated_tail=True,
    )

    assert any("invalid JSON" in issue for issue in strict.validation_issues)
    assert recovered.validation_issues == ()
    assert all(
        agent.observed_status == ReviewAgentExecutionStatus.COMPLETED
        for agent in recovered.agents
    )


def test_v2_review_rollouts_bind_started_event_to_child_thread(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    root_rollout = codex_home / "sessions" / "2026" / "08" / "12" / "root.jsonl"
    items = [json.loads(line) for line in root_rollout.read_text(encoding="utf-8").splitlines()]
    for item in items:
        payload = item.get("payload", {})
        if (
            payload.get("type") == "sub_agent_activity"
            and payload.get("agent_path") == "/root/dobby_review_correctness"
        ):
            payload["agent_thread_id"] = "different-child-thread"
    root_rollout.write_text(
        "\n".join(json.dumps(item) for item in items) + "\n",
        encoding="utf-8",
    )

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    assert evidence.agents[0].started_thread_ids == ("different-child-thread",)
    assert evidence.agents[0].child_thread_ids == ("child-correctness",)
    assert evidence.agents[0].observed_status == ReviewAgentExecutionStatus.FAILED


def test_v2_review_rollouts_require_exactly_one_terminal_event(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    child_rollout = (
        codex_home
        / "sessions"
        / "2026"
        / "08"
        / "12"
        / "child-correctness.jsonl"
    )
    items = [json.loads(line) for line in child_rollout.read_text(encoding="utf-8").splitlines()]
    items.append(items[-1])
    child_rollout.write_text(
        "\n".join(json.dumps(item) for item in items) + "\n",
        encoding="utf-8",
    )

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    assert evidence.agents[0].terminal_event_count == 2
    assert evidence.agents[0].observed_status == ReviewAgentExecutionStatus.FAILED


def test_strict_child_result_matches_worker_schema_and_refusal_contract() -> None:
    valid = {
        "summary": "verified child result",
        "completeness": "full",
        "important_facts": ["finding"],
        "next_steps": [],
        "files_changed": [],
        "warnings": [],
        "refused": False,
        "file_diffs": [],
        "review_execution": None,
    }

    assert _strict_child_worker_result(json.dumps(valid)) is not None
    assert _strict_child_worker_result("not-json") is None
    assert _strict_child_worker_result(
        json.dumps({key: value for key, value in valid.items() if key != "review_execution"})
    ) is None
    assert _strict_child_worker_result(json.dumps({**valid, "unexpected": True})) is None
    assert _strict_child_worker_result(
        json.dumps({**valid, "files_changed": ["victim.py"]})
    ) is None
    assert _strict_child_worker_result(
        json.dumps(
            {
                **valid,
                "file_diffs": [
                    {
                        "path": "victim.py",
                        "oldText": "before",
                        "newText": "after",
                        "truncated": False,
                    }
                ],
            }
        )
    ) is None
    # A refusal paired with a non-blocked completeness is normalised rather
    # than discarded: the worker stated why it refused, and dropping the whole
    # payload replaced that with a bare "invalid structured output" error.
    normalised_refusal = _strict_child_worker_result(
        json.dumps({**valid, "refused": True})
    )
    assert normalised_refusal is not None
    assert normalised_refusal.refused is True
    assert normalised_refusal.completeness == Completeness.BLOCKED
    assert any(
        "refused=true with completeness=full" in warning
        for warning in normalised_refusal.warnings
    )

    blocked_refusal = _strict_child_worker_result(
        json.dumps({**valid, "completeness": "blocked", "refused": True})
    )
    assert blocked_refusal is not None
    evidence = ReviewRolloutAgentEvidence(
        review_agent="correctness",
        task_name="dobby_review_correctness",
        agent_role="dobby_review_correctness",
        spawn_attempt_count=1,
        started_count=1,
        child_thread_ids=("child",),
        child_roles=("dobby_review_correctness",),
        delivered_count=1,
        child_result=blocked_refusal,
        terminal_event_count=1,
        child_multi_agent_versions=("disabled",),
        started_thread_ids=("child",),
    )
    assert evidence.observed_status == ReviewAgentExecutionStatus.BLOCKED


def test_strict_top_level_result_rejects_schema_defaults_and_nested_extras() -> None:
    valid = {
        "summary": "strict top-level result",
        "completeness": "full",
        "important_facts": [],
        "next_steps": [],
        "files_changed": [],
        "warnings": [],
        "refused": False,
        "file_diffs": [],
        "review_execution": None,
    }

    assert _strict_worker_result(json.dumps(valid)) is not None
    assert _strict_worker_result(
        json.dumps({key: value for key, value in valid.items() if key != "refused"})
    ) is None
    assert _strict_worker_result(json.dumps({**valid, "unexpected": True})) is None
    assert _strict_worker_result(
        json.dumps(
            {
                **valid,
                "review_execution": {
                    "protocol": "multi_agent_v2",
                    "agents": [],
                    "unexpected": True,
                },
            }
        )
    ) is None
    assert _strict_worker_result(
        json.dumps(
            {
                **valid,
                "review_execution": {
                    "protocol": "multi_agent_v2",
                    "agents": [
                        {
                            "review_agent": "correctness",
                            "task_name": "dobby_review_correctness",
                            "status": "completed",
                            "summary": "complete",
                            "unexpected": True,
                        }
                    ],
                },
            }
        )
    ) is None


def test_v2_review_rollouts_retain_rejected_child_mutation_claims(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    child_rollout = (
        codex_home
        / "sessions"
        / "2026"
        / "08"
        / "12"
        / "child-correctness.jsonl"
    )
    child_items = [
        json.loads(line)
        for line in child_rollout.read_text(encoding="utf-8").splitlines()
    ]
    child_payload = json.loads(child_items[-1]["payload"]["last_agent_message"])
    child_payload["files_changed"] = ["victim.py"]
    child_payload["file_diffs"] = [
        {
            "path": "victim.py",
            "oldText": "before",
            "newText": "after",
            "truncated": False,
        }
    ]
    mutated_result = json.dumps(child_payload, separators=(",", ":"))
    child_items[-1]["payload"]["last_agent_message"] = mutated_result
    child_rollout.write_text(
        "\n".join(json.dumps(item) for item in child_items) + "\n",
        encoding="utf-8",
    )

    root_rollout = codex_home / "sessions" / "2026" / "08" / "12" / "root.jsonl"
    root_items = [
        json.loads(line)
        for line in root_rollout.read_text(encoding="utf-8").splitlines()
    ]
    for item in root_items:
        payload = item.get("payload", {})
        if payload.get("type") == "agent_message" and payload.get("author") == "/root/dobby_review_correctness":
            content = payload["content"][0]["text"]
            payload["content"][0]["text"] = content.split("\nPayload:\n", 1)[0] + "\nPayload:\n" + mutated_result
        if payload.get("type") == "function_call_output" and payload.get("call_id") == "list-agents-call":
            listed = json.loads(payload["output"])
            for listed_agent in listed["agents"]:
                if listed_agent["agent_name"] == "/root/dobby_review_correctness":
                    listed_agent["agent_status"]["completed"] = mutated_result
            payload["output"] = json.dumps(listed)
    root_rollout.write_text(
        "\n".join(json.dumps(item) for item in root_items) + "\n",
        encoding="utf-8",
    )

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    correctness = evidence.agents[0]
    assert correctness.observed_status == ReviewAgentExecutionStatus.FAILED
    assert correctness.child_result is None
    assert correctness.reported_mutation_paths == ("victim.py",)
    assert correctness.reported_file_diffs is True


def test_v2_review_rollouts_reject_nonisolated_spawn_contract(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    root_rollout = codex_home / "sessions" / "2026" / "08" / "12" / "root.jsonl"
    items = [json.loads(line) for line in root_rollout.read_text(encoding="utf-8").splitlines()]
    for item in items:
        payload = item.get("payload", {})
        if payload.get("type") != "function_call" or payload.get("name") != "spawn_agent":
            continue
        arguments = json.loads(payload["arguments"])
        if arguments.get("task_name") == "dobby_review_correctness":
            arguments["fork_turns"] = "all"
            arguments["model"] = "untrusted-override"
            payload["arguments"] = json.dumps(arguments)
    root_rollout.write_text(
        "\n".join(json.dumps(item) for item in items) + "\n",
        encoding="utf-8",
    )

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    assert evidence.agents[0].observed_status == ReviewAgentExecutionStatus.FAILED
    assert any("fork_turns" in issue for issue in evidence.agents[0].spawn_contract_issues)
    assert any("unexpected model" in issue for issue in evidence.agents[0].spawn_contract_issues)

    # A spawn that ignored the isolation contract is an integrity failure, not
    # coverage loss: a child ran that Dobby never asked for. Downgrading it
    # would let the remaining lenses be returned with manifest_validated=true,
    # contradicting the exact-role/task/fork guarantee in README and AGENTS.md.
    lens_issues = _rollout_validation_issues(
        evidence.agents[0], ReviewAgentExecutionStatus.FAILED
    )
    assert any("fork_turns" in issue for issue in lens_issues.integrity)

    honest_manifest = WorkerResult.model_validate(
        {
            "summary": "one lens failed its spawn contract",
            "completeness": "partial",
            "important_facts": [],
            "next_steps": [],
            "files_changed": [],
            "warnings": [],
            "refused": False,
            "file_diffs": [],
            "review_execution": _review_execution_manifest(
                [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
                statuses={ReviewAgent.CORRECTNESS: "failed"},
            ),
        }
    )
    diagnostics = _review_execution_diagnostics(
        honest_manifest,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        evidence,
    )
    assert diagnostics.manifest_validated is False

    # And no prose from the contract-violating child reaches the caller.
    verified = _verified_review_worker_result_from_rollouts(evidence)
    rendered = json.dumps(verified.model_dump(mode="json"))
    assert "correctness finding" not in rendered


@pytest.mark.parametrize("root_version", [None, "v1", "disabled"])
def test_v2_review_rollouts_require_root_turn_context_v2(
    tmp_path: Path,
    root_version: str | None,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    root_rollout = codex_home / "sessions" / "2026" / "08" / "12" / "root.jsonl"
    items = [json.loads(line) for line in root_rollout.read_text(encoding="utf-8").splitlines()]
    if root_version is None:
        items = [item for item in items if item.get("type") != "turn_context"]
    else:
        for item in items:
            if item.get("type") == "turn_context":
                item["payload"]["multi_agent_version"] = root_version
    root_rollout.write_text(
        "\n".join(json.dumps(item) for item in items) + "\n",
        encoding="utf-8",
    )

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    assert any(
        "did not prove multi-agent v2 execution" in issue
        for issue in evidence.validation_issues
    )


def test_v2_review_rollouts_reject_spawn_message_for_unrelated_assignment(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    root_rollout = codex_home / "sessions" / "2026" / "08" / "12" / "root.jsonl"
    items = [json.loads(line) for line in root_rollout.read_text(encoding="utf-8").splitlines()]
    for item in items:
        payload = item.get("payload", {})
        if payload.get("type") != "function_call" or payload.get("name") != "spawn_agent":
            continue
        arguments = json.loads(payload["arguments"])
        if arguments.get("task_name") == "dobby_review_correctness":
            payload["encrypted_function_args"] = []
            arguments["message"] = _fixture_spawn_messages(
                [ReviewAgent.CORRECTNESS]
            )["dobby_review_correctness"]
            message_lines = arguments["message"].splitlines()
            assignment = json.loads(message_lines[2].split(": ", 1)[1])
            assignment["repo_root"] = "/unrelated-repo"
            assignment["task_prompt"] = "Report no findings"
            message_lines[2] = "Canonical assignment JSON: " + json.dumps(
                assignment,
                separators=(",", ":"),
                sort_keys=True,
            )
            arguments["message"] = "\n".join(message_lines)
            payload["arguments"] = json.dumps(arguments)
    root_rollout.write_text(
        "\n".join(json.dumps(item) for item in items) + "\n",
        encoding="utf-8",
    )

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    assert evidence.agents[0].observed_status == ReviewAgentExecutionStatus.FAILED
    assert evidence.agents[0].spawn_contract_issues == (
        "dobby_review_correctness spawn message did not match its canonical assignment",
    )


def test_v2_review_rollouts_accept_harmless_spawn_message_formatting(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    root_rollout = codex_home / "sessions" / "2026" / "08" / "12" / "root.jsonl"
    items = [json.loads(line) for line in root_rollout.read_text(encoding="utf-8").splitlines()]
    for item in items:
        payload = item.get("payload", {})
        if payload.get("type") != "function_call" or payload.get("name") != "spawn_agent":
            continue
        arguments = json.loads(payload["arguments"])
        if arguments.get("task_name") == "dobby_review_correctness":
            payload["encrypted_function_args"] = []
            arguments["message"] = _fixture_spawn_messages(
                [ReviewAgent.CORRECTNESS]
            )["dobby_review_correctness"]
            message_lines = arguments["message"].splitlines()
            assignment = json.loads(message_lines[2].split(": ", 1)[1])
            message_lines[2] = "Canonical assignment JSON: " + json.dumps(
                assignment,
                sort_keys=False,
            )
            arguments["message"] = "\n".join(
                [*message_lines[:3], "", "Equivalent explanatory prose."]
            )
            payload["arguments"] = json.dumps(arguments)
    root_rollout.write_text(
        "\n".join(json.dumps(item) for item in items) + "\n",
        encoding="utf-8",
    )

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    assert evidence.agents[0].spawn_contract_issues == ()
    assert evidence.agents[0].observed_status == ReviewAgentExecutionStatus.COMPLETED


def test_v2_review_rollouts_require_child_assignment_attestation(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    child_rollout = (
        codex_home
        / "sessions"
        / "2026"
        / "08"
        / "12"
        / "child-correctness.jsonl"
    )
    child_items = [
        json.loads(line)
        for line in child_rollout.read_text(encoding="utf-8").splitlines()
    ]
    child_result = json.loads(child_items[-1]["payload"]["last_agent_message"])
    child_result["summary"] = "correctness child result without attestation"
    unattested_payload = json.dumps(child_result, separators=(",", ":"))
    child_items[-1]["payload"]["last_agent_message"] = unattested_payload
    child_rollout.write_text(
        "\n".join(json.dumps(item) for item in child_items) + "\n",
        encoding="utf-8",
    )

    root_rollout = codex_home / "sessions" / "2026" / "08" / "12" / "root.jsonl"
    root_items = [
        json.loads(line)
        for line in root_rollout.read_text(encoding="utf-8").splitlines()
    ]
    for item in root_items:
        payload = item.get("payload", {})
        if (
            payload.get("type") == "agent_message"
            and payload.get("author") == "/root/dobby_review_correctness"
        ):
            envelope = payload["content"][0]["text"]
            payload["content"][0]["text"] = (
                envelope.split("\nPayload:\n", 1)[0]
                + "\nPayload:\n"
                + unattested_payload
            )
        if (
            payload.get("type") == "function_call_output"
            and payload.get("call_id") == "list-agents-call"
        ):
            listed = json.loads(payload["output"])
            for listed_agent in listed["agents"]:
                if listed_agent["agent_name"] == "/root/dobby_review_correctness":
                    listed_agent["agent_status"]["completed"] = unattested_payload
            payload["output"] = json.dumps(listed)
    root_rollout.write_text(
        "\n".join(json.dumps(item) for item in root_items) + "\n",
        encoding="utf-8",
    )

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    assert evidence.agents[0].child_result is None
    assert evidence.agents[0].observed_status == ReviewAgentExecutionStatus.FAILED
    # Attestation failure is tracked apart from spawn-contract violations: the
    # unattested payload is discarded, so it costs this lens its slot without
    # quarantining the run, whereas a wrong role/fork/encryption shape means a
    # child ran that Dobby never asked for and must stay an integrity failure.
    assert evidence.agents[0].spawn_contract_issues == ()
    assert evidence.agents[0].attestation_issues == (
        "dobby_review_correctness child result did not attest its canonical assignment",
    )
    lens_issues = _rollout_validation_issues(
        evidence.agents[0], ReviewAgentExecutionStatus.FAILED
    )
    assert lens_issues.integrity == []
    assert any("did not attest" in issue for issue in lens_issues.coverage)


def test_verified_review_payload_is_built_only_from_child_results(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    verified = _verified_review_worker_result_from_rollouts(evidence)

    assert verified.important_facts == ["correctness finding", "regression finding"]
    assert verified.next_steps == ["correctness next step", "regression next step"]
    assert verified.review_execution is not None
    assert [entry.summary for entry in verified.review_execution.agents] == [
        "correctness child result",
        "regression child result",
    ]


def test_globally_invalid_rollout_evidence_quarantines_all_review_content() -> None:
    valid = _rollout_evidence(
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )
    invalid = ReviewRolloutEvidence(
        collected=True,
        agents=valid.agents,
        validation_issues=("unexpected nested child session",),
    )

    verified = _verified_review_worker_result_from_rollouts(invalid)
    salvaged = _salvaged_review_worker_result_from_rollouts(invalid)

    assert verified.completeness == Completeness.BLOCKED
    assert verified.important_facts == []
    assert verified.next_steps == []
    assert verified.warnings == []
    assert verified.review_execution is None
    assert salvaged is None


def test_v2_review_rollouts_salvage_delivered_child_results_after_parent_timeout(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.PARTIAL,
        },
    )
    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    salvaged = _salvaged_review_worker_result_from_rollouts(evidence)

    assert salvaged is not None
    assert salvaged.completeness == Completeness.PARTIAL
    assert salvaged.important_facts == ["correctness finding", "regression finding"]
    assert salvaged.review_execution is not None
    assert [entry.status for entry in salvaged.review_execution.agents] == [
        ReviewAgentExecutionStatus.COMPLETED,
        ReviewAgentExecutionStatus.PARTIAL,
    ]


def test_v2_review_execution_manifest_rejects_missing_duplicate_and_unexpected_entries() -> None:
    worker_result = WorkerResult.model_validate(
        {
            "summary": "review complete",
            "completeness": "full",
            "important_facts": [],
            "next_steps": [],
            "files_changed": [],
            "warnings": [],
            "refused": False,
            "file_diffs": [],
            "review_execution": {
                "protocol": "multi_agent_v2",
                "agents": [
                    {
                        "review_agent": "correctness",
                        "task_name": "wrong_task",
                        "status": "completed",
                        "summary": "first",
                    },
                    {
                        "review_agent": "correctness",
                        "task_name": "wrong_task",
                        "status": "completed",
                        "summary": "duplicate",
                    },
                    {
                        "review_agent": "security",
                        "task_name": "dobby_review_security",
                        "status": "completed",
                        "summary": "unexpected",
                    },
                ],
            },
        }
    )

    diagnostics = _review_execution_diagnostics(
        worker_result,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        _rollout_evidence([ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION]),
    )

    assert diagnostics.manifest_validated is False
    assert "missing selected review agents: regression" in diagnostics.validation_issues
    assert "duplicate review agent entries: correctness" in diagnostics.validation_issues
    assert "unexpected review agent entries: security" in diagnostics.validation_issues
    assert "duplicate task names: wrong_task" in diagnostics.validation_issues


def test_v2_review_execution_manifest_rejects_mismatched_task_name() -> None:
    manifest = _review_execution_manifest(
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION]
    )
    manifest["agents"][1]["task_name"] = "dobby_review_security"  # type: ignore[index]
    worker_result = WorkerResult.model_validate(
        {
            "summary": "review complete",
            "completeness": "full",
            "important_facts": [],
            "next_steps": [],
            "files_changed": [],
            "warnings": [],
            "refused": False,
            "file_diffs": [],
            "review_execution": manifest,
        }
    )

    diagnostics = _review_execution_diagnostics(
        worker_result,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        _rollout_evidence([ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION]),
    )

    assert diagnostics.manifest_validated is False
    assert diagnostics.validation_issues == (
        "mismatched task names: regression expected dobby_review_regression but reported dobby_review_security",
    )


def test_v2_review_execution_manifest_rejects_reversed_agent_order() -> None:
    manifest = _review_execution_manifest(
        [ReviewAgent.REGRESSION, ReviewAgent.CORRECTNESS]
    )
    worker_result = WorkerResult.model_validate(
        {
            "summary": "review complete",
            "completeness": "full",
            "important_facts": [],
            "next_steps": [],
            "files_changed": [],
            "warnings": [],
            "refused": False,
            "file_diffs": [],
            "review_execution": manifest,
        }
    )

    diagnostics = _review_execution_diagnostics(
        worker_result,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        _rollout_evidence([ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION]),
    )

    assert diagnostics.manifest_validated is False
    assert diagnostics.validation_issues == (
        "review agent order mismatch: expected correctness, regression; reported regression, correctness",
    )


@pytest.mark.parametrize(
    ("statuses", "reported_completeness", "expected_completeness"),
    [
        ({}, "partial", "full"),
        ({ReviewAgent.REGRESSION: "failed"}, "full", "partial"),
        (
            {
                ReviewAgent.CORRECTNESS: "failed",
                ReviewAgent.REGRESSION: "failed",
            },
            "partial",
            "blocked",
        ),
    ],
)
def test_v2_review_execution_manifest_rejects_completeness_contradictions(
    statuses: dict[ReviewAgent, str],
    reported_completeness: str,
    expected_completeness: str,
) -> None:
    worker_result = WorkerResult.model_validate(
        {
            "summary": "review result",
            "completeness": reported_completeness,
            "important_facts": [],
            "next_steps": [],
            "files_changed": [],
            "warnings": [],
            "refused": False,
            "file_diffs": [],
            "review_execution": _review_execution_manifest(
                [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
                statuses=statuses,
            ),
        }
    )

    diagnostics = _review_execution_diagnostics(
        worker_result,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        _rollout_evidence(
            [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
            statuses=statuses,
        ),
    )

    assert diagnostics.manifest_validated is False
    assert diagnostics.validation_issues == (
        "top-level completeness does not match agent statuses: expected "
        f"{expected_completeness}; reported {reported_completeness}",
    )


def test_v2_review_execution_manifest_distinguishes_valid_but_unusable_run() -> None:
    worker_result = WorkerResult.model_validate(
        {
            "summary": "review blocked",
            "completeness": "blocked",
            "important_facts": [],
            "next_steps": [],
            "files_changed": [],
            "warnings": [],
            "refused": False,
            "file_diffs": [],
            "review_execution": _review_execution_manifest(
                [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
                statuses={
                    ReviewAgent.CORRECTNESS: "failed",
                    ReviewAgent.REGRESSION: "failed",
                },
            ),
        }
    )

    diagnostics = _review_execution_diagnostics(
        worker_result,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        _rollout_evidence(
            [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
            statuses={
                ReviewAgent.CORRECTNESS: "failed",
                ReviewAgent.REGRESSION: "failed",
            },
        ),
    )

    assert diagnostics.manifest_validated is True
    assert diagnostics.usable_count == 0
    assert diagnostics.failure_summary() == (
        "Review multi-agent v2 execution produced no usable child results: "
        "0/2 selected review agents returned usable output."
    )


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
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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


def test_runtime_mirror_exclusions_resolve_symlinks_and_case(
    tmp_path: Path,
) -> None:
    """Lexical exclusion checks are bypassable; resolved ones are not.

    A symlink alias to the home or to sessions/, and a case variant on a
    case-insensitive filesystem, both reach the same outcome the exclusion
    exists to prevent: cloning credentials and historical rollouts into the
    child runtime.
    """
    source_root = tmp_path / "codex-home"
    (source_root / "sessions" / "2026").mkdir(parents=True)
    (source_root / "sessions" / "2026" / "old.jsonl").write_text("{}\n", encoding="utf-8")
    (source_root / "auth.json").write_text('{"token": "secret"}', encoding="utf-8")
    (source_root / "prompts").mkdir()
    (source_root / "prompts" / "extra.md").write_text("hello", encoding="utf-8")
    (source_root / "session-cache").symlink_to(
        source_root / "sessions", target_is_directory=True
    )
    (source_root / "home-alias").symlink_to(source_root, target_is_directory=True)
    # A symlink to sessions nested inside an otherwise mirrorable directory.
    (source_root / "prompts" / "history").symlink_to(
        source_root / "sessions", target_is_directory=True
    )
    target_root = tmp_path / "child-home"
    target_root.mkdir()

    payload = "\n".join(
        [
            f'alias = "{source_root / "session-cache"}"',
            f'home = "{source_root / "home-alias"}"',
            f'extra_prompt = "{source_root / "prompts" / "extra.md"}"',
            "",
        ]
    )
    _mirror_runtime_config_references(
        payload, source_root=source_root, target_root=target_root
    )

    assert not (target_root / "session-cache").exists()
    assert not (target_root / "home-alias").exists()
    assert not (target_root / "auth.json").exists()
    # The legitimate reference is still mirrored, minus the nested alias.
    assert (target_root / "prompts" / "extra.md").read_text(encoding="utf-8") == "hello"
    assert not (target_root / "prompts" / "history").exists()


def test_runtime_mirror_rejects_case_variant_of_excluded_subtree(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "codex-home"
    (source_root / "sessions").mkdir(parents=True, exist_ok=True)
    (source_root / "sessions" / "old.jsonl").write_text("{}\n", encoding="utf-8")
    target_root = tmp_path / "child-home"
    target_root.mkdir()

    _mirror_runtime_config_references(
        f'history = "{source_root}/Sessions"\n',
        source_root=source_root,
        target_root=target_root,
    )

    assert not any(
        child.name.casefold() == "sessions" for child in target_root.iterdir()
    )


def test_config_reference_to_the_runtime_home_itself_is_not_mirrored(
    tmp_path: Path,
) -> None:
    """A bare ``~/.codex`` in config.toml must not clone the whole parent home.

    ``reference.relative_to(source_root)`` succeeds with ``.`` for the root
    itself, which would copytree the entire home — auth.json, caches, and every
    historical rollout — into the child's private runtime. The stray rollouts
    alone would make Dobby see multiple root sessions and quarantine the review.
    """
    source_root = tmp_path / "codex-home"
    (source_root / "sessions" / "2026").mkdir(parents=True)
    (source_root / "sessions" / "2026" / "old-rollout.jsonl").write_text(
        '{"type": "session_meta"}\n', encoding="utf-8"
    )
    (source_root / "auth.json").write_text('{"token": "secret"}', encoding="utf-8")
    (source_root / "prompts").mkdir()
    (source_root / "prompts" / "extra.md").write_text("hello", encoding="utf-8")
    target_root = tmp_path / "child-home"
    target_root.mkdir()

    payload = (
        f'notify = ["{source_root}"]\n'
        f'extra_prompt = "{source_root}/prompts/extra.md"\n'
        f'history_dir = "{source_root}/sessions"\n'
    )
    _mirror_runtime_config_references(
        payload, source_root=source_root, target_root=target_root
    )

    # The specifically referenced subtree is still mirrored.
    assert (target_root / "prompts" / "extra.md").read_text(encoding="utf-8") == "hello"
    # The bare home reference must not drag the rest of the home along.
    assert not (target_root / "auth.json").exists()
    # An explicit sessions reference is refused for the same reason.
    assert not (target_root / "sessions").exists()


@pytest.mark.asyncio
async def test_child_scratch_runtime_is_removed_from_the_repo_after_a_run(
    tmp_path: Path,
) -> None:
    """The child's TMPDIR/cache tree must not accumulate inside the repo.

    It is pointed at ``.codex-dobby/runs/<task-id>/runtime`` so the child never
    writes to the shared system temp, which means nothing else will ever clean
    it up.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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

    observed_tmpdir: dict[str, Path] = {}

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        tmpdir = Path(kwargs["env"]["TMPDIR"])
        observed_tmpdir["path"] = tmpdir
        # A real codex child leaves scratch files behind here.
        (tmpdir / "child-scratch.bin").write_text("x" * 4096, encoding="utf-8")
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "summary": "planned the work",
                    "completeness": "full",
                    "important_facts": ["a fact"],
                    "next_steps": [],
                    "files_changed": [],
                    "warnings": [],
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": None,
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
    scratch_root = observed_tmpdir["path"].parent
    assert scratch_root.name == "runtime"
    run_dir = scratch_root.parent
    assert not scratch_root.exists(), "child runtime scratch tree leaked into the repo"
    # The run's own artifacts must survive the scratch cleanup.
    assert run_dir.exists()
    assert (run_dir / "result.json").exists()
    assert not result.warnings


@pytest.mark.asyncio
async def test_review_response_includes_review_details(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": None,
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
    assert result.review_details.orchestration_protocol is None
    assert result.review_details.execution_evidence == ReviewExecutionEvidence.DIRECT_PARENT
    assert result.review_details.execution_manifest is None
    assert result.review_details.manifest_validated is False
    assert result.review_details.coverage_complete is None


@pytest.mark.asyncio
async def test_reverse_engineer_response_includes_ghidra_details(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")

    codex_home = tmp_path / "codex-home"
    (codex_home / "auth.json").write_text('{"auth_mode":"chatgpt"}\n', encoding="utf-8")
    (codex_home / "config.toml").write_text('[mcp_servers.ghidra]\ncommand = "uv"\nargs = ["run", "bridge_mcp_ghidra.py"]\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

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

        async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
            stdout = b""
            stderr = (
                b"mcp: ghidra/list_instances started\n"
                b"mcp: ghidra/connect_instance started\n"
                b"mcp: ghidra/check_tools started\n"
                b"    open_programs = gh.dispatch_get('/list_open_programs')\n"
                b"        strings = gh.dispatch_get('/search_strings', params={'search_term': 'Dirac'})\n"
            )
            return (stdout, stderr)

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "summary": "Ghidra helper fallback succeeded.",
                    "completeness": "full",
                    "important_facts": [],
                    "next_steps": [],
                    "files_changed": [],
                    "warnings": [],
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": None,
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)

    result = await runner.run(ToolName.REVERSE_ENGINEER, InvocationRequest(prompt="inspect binary"))

    assert result.status == RunStatus.SUCCESS
    assert result.reverse_engineer_details is not None
    assert result.reverse_engineer_details.ghidra is not None
    assert result.reverse_engineer_details.ghidra.mode == GhidraUsageMode.HELPER_FALLBACK
    assert result.reverse_engineer_details.ghidra.mcp_calls == ["list_instances", "connect_instance", "check_tools"]
    assert result.reverse_engineer_details.ghidra.helper_calls == ["list_open_programs", "search_strings"]


@pytest.mark.asyncio
async def test_read_only_run_ignores_model_reported_file_changes_without_observed_writes(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": None,
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
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": None,
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

    # External worktree changes during a read-only run are NOT a sandbox
    # violation — codex's OS-level read-only sandbox blocks codex writes,
    # so anything we observe is the user editing in another terminal or
    # another process touching files. Surface as warning, do not error.
    assert result.status == RunStatus.SUCCESS
    assert "notes.txt" in result.files_changed
    assert any(
        "External worktree changes observed during the run" in w for w in result.warnings
    ), result.warnings


@pytest.mark.asyncio
async def test_runner_inherits_parent_environment_for_child_codex_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": None,
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
    expected_claude_config = Path(tempfile.gettempdir()).resolve() / "codex-dobby" / result.task_id / "claude-config"
    assert Path(captured_env["CLAUDE_CONFIG_DIR"]) == expected_claude_config
    assert "HOME" not in captured_env or captured_env["HOME"] == os.environ.get("HOME", captured_env["HOME"])
    assert captured_env["CLAUDE_CODE_DISABLE_CRON"] == "1"
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
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": None,
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
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": None,
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
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": None,
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
    (repo_root / "runner.py").write_text("", encoding="utf-8")
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
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": None,
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
    (repo_root / "runner.py").write_text("", encoding="utf-8")
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
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": None,
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
    (repo_root / "runner.py").write_text("", encoding="utf-8")
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
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": None,
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
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
async def test_v2_review_does_not_treat_legacy_v1_events_as_execution_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
                "refused": False,
                "file_diffs": [],
                "review_execution": None,
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
                "refused": False,
                "file_diffs": [],
                "review_execution": None,
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

    assert result.status == RunStatus.ERROR
    assert result.summary == "Codex run timed out after 10 seconds"
    assert result.completeness == Completeness.BLOCKED
    assert result.important_facts == []
    assert "Codex run timed out after 10 seconds" in result.warnings
    assert "worker result omitted the required review_execution manifest" in result.warnings
    assert result.review_details is not None
    assert result.review_details.execution_manifest is None
    assert result.review_details.execution_evidence is None
    assert result.review_details.manifest_validated is False


@pytest.mark.asyncio
async def test_review_run_returns_partial_success_when_timeout_has_valid_v2_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
                    "summary": "One child returned useful findings before the parent timeout.",
                    "completeness": "partial",
                    "important_facts": [
                        "runner.py leaves timeout cleanup unbounded in the streaming path"
                    ],
                    "next_steps": [
                        "Bound timeout cleanup and stream draining to the remaining deadline."
                    ],
                    "files_changed": [],
                    "warnings": [],
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": _review_execution_manifest(
                        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
                        statuses={ReviewAgent.REGRESSION: "failed"},
                    ),
                }
            ),
            encoding="utf-8",
        )
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
                "refused": False,
                "file_diffs": [],
                "review_execution": None,
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
    monkeypatch.setattr(
        "codex_dobby_mcp.runner._review_rollout_evidence",
        lambda *_args, **_kwargs: _rollout_evidence(
            [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
            statuses={ReviewAgent.REGRESSION: "failed"},
        ),
    )

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
    assert result.summary == (
        "Multi-agent review returned independently verified findings from 1/2 "
        "delivered v2 child results."
    )
    assert result.completeness == Completeness.PARTIAL
    assert result.important_facts == ["correctness independently verified finding"]
    assert "Codex run timed out after 10 seconds" in result.warnings
    assert (
        "Review execution manifest reports 1 completed, 0 partial, 0 blocked, and 1 failed of 2 selected review agents"
        in result.warnings
    )
    assert result.review_details is not None
    assert result.review_details.manifest_validated is True
    assert result.review_details.coverage_complete is False
    assert result.review_details.completed_agents == 1
    assert result.review_details.failed_agents == 1


@pytest.mark.asyncio
async def test_review_run_keeps_partial_parent_result_with_valid_v2_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": _review_execution_manifest(
                        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
                        statuses={ReviewAgent.CORRECTNESS: "failed"},
                    ),
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
    monkeypatch.setattr(
        "codex_dobby_mcp.runner._review_rollout_evidence",
        lambda *_args, **_kwargs: _rollout_evidence(
            [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
            statuses={ReviewAgent.CORRECTNESS: "failed"},
        ),
    )

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
    assert result.summary == (
        "Multi-agent review returned independently verified findings from 1/2 "
        "delivered v2 child results."
    )
    assert result.completeness == Completeness.PARTIAL
    assert result.important_facts == ["regression independently verified finding"]
    assert (
        "Review execution manifest reports 1 completed, 0 partial, 0 blocked, and 1 failed of 2 selected review agents"
        in result.warnings
    )
    assert result.review_details is not None
    assert result.review_details.manifest_validated is True
    assert result.review_details.coverage_complete is False
    assert result.review_details.completed_agents == 1
    assert result.review_details.failed_agents == 1


@pytest.mark.asyncio
async def test_single_agent_review_returns_partial_success_when_timeout_has_usable_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": None,
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
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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

    baseline = _build_repo_snapshot(
        repo_root, include_head=False, use_metadata_fingerprints=False
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
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": None,
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    baseline = _build_repo_snapshot(
        repo_root, include_head=False, use_metadata_fingerprints=False
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
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": None,
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    baseline = _build_repo_snapshot(
        repo_root, include_head=True, use_metadata_fingerprints=False
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
@pytest.mark.parametrize(
    ("stdout", "returncode", "should_raise"),
    [
        (b"multi_agent_v2                       stable             false\n", 0, False),
        (b"multi_agent                          stable             true\n", 0, True),
        (b"", 2, True),
    ],
)
async def test_multi_agent_v2_capability_probe_requires_listed_feature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: bytes,
    returncode: int,
    should_raise: bool,
) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = returncode

        async def communicate(self) -> tuple[bytes, bytes]:
            return stdout, b"unsupported command" if returncode else b""

    async def fake_exec(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return FakeProcess()

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    call = _ensure_multi_agent_v2_available(
        "/usr/bin/codex",
        tmp_path,
        {},
        time.monotonic() + 5,
    )
    if should_raise:
        with pytest.raises(RunnerError, match="multi_agent_v2"):
            await call
    else:
        await call


@pytest.mark.asyncio
async def test_multi_agent_v2_capability_start_timeout_returns_structured_timeout(
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

    probe_calls = 0

    async def timed_out_probe(*_args, **_kwargs) -> None:
        nonlocal probe_calls
        probe_calls += 1
        raise asyncio.TimeoutError

    monkeypatch.setattr(
        "codex_dobby_mcp.runner._ensure_multi_agent_v2_available",
        timed_out_probe,
    )

    result = await runner.run(
        ToolName.REVIEW,
        _request(
            prompt="review it",
            agents=[ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
            timeout_seconds=10,
        ),
    )

    assert result.status == RunStatus.ERROR
    assert result.completeness == Completeness.BLOCKED
    assert result.stop_reason == StopReason.TIMEOUT
    assert result.summary == "Codex run timed out after 10 seconds"
    assert probe_calls == 1


@pytest.mark.asyncio
async def test_nonzero_parent_exit_returns_verified_partial_review(
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
        returncode = 2

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return FakeProcess()

    async def fake_execute(*args, **_kwargs):  # type: ignore[no-untyped-def]
        args[2].write_text("", encoding="utf-8")
        args[3].write_text("parent API failure", encoding="utf-8")
        return (2, False, False)

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        "codex_dobby_mcp.runner._execute_process_with_streaming_logs",
        fake_execute,
    )
    monkeypatch.setattr(
        "codex_dobby_mcp.runner._review_rollout_evidence",
        lambda *_args, **_kwargs: _rollout_evidence(
            [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION]
        ),
    )

    result = await runner.run(
        ToolName.REVIEW,
        _request(
            prompt="review it",
            agents=[ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
            timeout_seconds=10,
        ),
    )

    assert result.status == RunStatus.SUCCESS
    assert result.completeness == Completeness.PARTIAL
    assert result.stop_reason == StopReason.ERROR
    assert result.important_facts == [
        "correctness independently verified finding",
        "regression independently verified finding",
    ]
    assert any("Codex parent exited with status 2" in warning for warning in result.warnings)


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
    (repo_root / "runner.py").write_text("", encoding="utf-8")

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
    (repo_root / "runner.py").write_text("", encoding="utf-8")
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
    (repo_root / "runner.py").write_text("", encoding="utf-8")
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
        lambda repo_root=None: [mcp_servers_root.resolve()],
    )
    monkeypatch.setattr(
        "codex_dobby_mcp.runner.reverse_engineer_default_readonly_roots",
        lambda: [],
    )

    resolved = runner._resolve(ToolName.REVERSE_ENGINEER, InvocationRequest(prompt="inspect firmware"))

    assert resolved.sandbox_roots == [repo_root.resolve(), mcp_servers_root.resolve()]
    assert resolved.writable_roots == [repo_root.resolve(), mcp_servers_root.resolve()]
    assert resolved.advisory_read_only_roots == []


@pytest.mark.asyncio
async def test_runner_rewrites_claude_config_references_into_private_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    parent_codex_home = tmp_path / "codex-home"
    (parent_codex_home / "sessions").mkdir(parents=True, exist_ok=True)
    (parent_codex_home / "auth.json").write_text('{"auth_mode":"chatgpt"}\n', encoding="utf-8")

    parent_claude_config = tmp_path / "claude-home"
    helpers_root = parent_claude_config / "mcp-servers" / "ghidra-mcp"
    helpers_root.mkdir(parents=True)
    (helpers_root / "bridge_mcp_ghidra.py").write_text("# bridge\n", encoding="utf-8")

    (parent_codex_home / "config.toml").write_text(
        (
            "[mcp_servers.ghidra]\n"
            'command = "uv"\n'
            f'args = ["run", "{helpers_root / "bridge_mcp_ghidra.py"}"]\n'
            f'helper_dir = "{helpers_root}"\n'
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("CODEX_HOME", str(parent_codex_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(parent_claude_config))

    seeded_config = ""
    mirrored_bridge = ""
    mirrored_helper_dir_exists = False

    class FakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal seeded_config, mirrored_bridge, mirrored_helper_dir_exists
        child_env = dict(kwargs["env"])
        child_codex_home = Path(child_env["CODEX_HOME"])
        child_claude_config = Path(child_env["CLAUDE_CONFIG_DIR"])
        seeded_config = (child_codex_home / "config.toml").read_text(encoding="utf-8")
        mirrored_bridge = (child_claude_config / "mcp-servers" / "ghidra-mcp" / "bridge_mcp_ghidra.py").read_text(
            encoding="utf-8"
        )
        mirrored_helper_dir_exists = (child_claude_config / "mcp-servers" / "ghidra-mcp").is_dir()
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
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": None,
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)

    result = await runner.run(ToolName.RESEARCH, InvocationRequest(prompt="inspect"))

    assert result.status == RunStatus.SUCCESS
    assert str(parent_claude_config) not in seeded_config
    assert "/claude-config/mcp-servers/ghidra-mcp/bridge_mcp_ghidra.py" in seeded_config
    assert mirrored_bridge == "# bridge\n"
    assert mirrored_helper_dir_exists is True


@pytest.mark.asyncio
async def test_runner_rewrites_symlinked_claude_config_references_into_private_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    parent_codex_home = tmp_path / "codex-home"
    (parent_codex_home / "sessions").mkdir(parents=True, exist_ok=True)
    (parent_codex_home / "auth.json").write_text('{"auth_mode":"chatgpt"}\n', encoding="utf-8")

    parent_claude_config = tmp_path / "claude-home"
    parent_claude_config.mkdir()
    shared_helpers_root = tmp_path / "shared-helpers"
    helpers_root = shared_helpers_root / "ghidra-mcp"
    helpers_root.mkdir(parents=True)
    (helpers_root / "bridge_mcp_ghidra.py").write_text("# bridge\n", encoding="utf-8")
    (parent_claude_config / "mcp-servers").symlink_to(shared_helpers_root, target_is_directory=True)
    bridge_path = parent_claude_config / "mcp-servers" / "ghidra-mcp" / "bridge_mcp_ghidra.py"

    (parent_codex_home / "config.toml").write_text(
        (
            "[mcp_servers.ghidra]\n"
            'command = "uv"\n'
            f'args = ["run", "{bridge_path}"]\n'
            f'helper_dir = "{bridge_path.parent}"\n'
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("CODEX_HOME", str(parent_codex_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(parent_claude_config))

    seeded_config = ""
    mirrored_bridge = ""
    mirrored_helper_dir_exists = False

    class FakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal seeded_config, mirrored_bridge, mirrored_helper_dir_exists
        child_env = dict(kwargs["env"])
        child_codex_home = Path(child_env["CODEX_HOME"])
        child_claude_config = Path(child_env["CLAUDE_CONFIG_DIR"])
        seeded_config = (child_codex_home / "config.toml").read_text(encoding="utf-8")
        mirrored_bridge = (child_claude_config / "mcp-servers" / "ghidra-mcp" / "bridge_mcp_ghidra.py").read_text(
            encoding="utf-8"
        )
        mirrored_helper_dir_exists = (child_claude_config / "mcp-servers" / "ghidra-mcp").is_dir()
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
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": None,
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)

    result = await runner.run(ToolName.RESEARCH, InvocationRequest(prompt="inspect"))

    assert result.status == RunStatus.SUCCESS
    assert str(parent_claude_config) not in seeded_config
    assert "/claude-config/mcp-servers/ghidra-mcp/bridge_mcp_ghidra.py" in seeded_config
    assert mirrored_bridge == "# bridge\n"
    assert mirrored_helper_dir_exists is True


def test_runner_reverse_engineer_uses_repo_local_codex_config_for_helper_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")
    (repo_root / ".codex").mkdir()

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(exist_ok=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("USER", "isolated-user")
    monkeypatch.setattr(
        "codex_dobby_mcp.paths._MACOS_GHIDRA_SOCKET_SCAN_ROOTS",
        (tmp_path / "no-live-sockets",),
    )

    ghidra_root = tmp_path / "ghidra-mcp"
    ghidra_root.mkdir()
    bridge_path = ghidra_root / "bridge_mcp_ghidra.py"
    bridge_path.write_text("# test\n", encoding="utf-8")
    ((repo_root / ".codex") / "config.toml").write_text(
        f'[mcp_servers.ghidra]\ncommand = "uv"\nargs = ["run", "{bridge_path}"]\n',
        encoding="utf-8",
    )

    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    runner = CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )

    resolved = runner._resolve(ToolName.REVERSE_ENGINEER, InvocationRequest(prompt="inspect firmware"))

    assert resolved.sandbox_roots == [repo_root.resolve(), ghidra_root.resolve()]
    assert resolved.writable_roots == [repo_root.resolve(), ghidra_root.resolve()]
    assert resolved.advisory_read_only_roots == []


def test_reverse_engineer_default_writable_roots_use_configured_ghidra_bridge_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(exist_ok=True)
    ghidra_root = tmp_path / "ghidra-mcp"
    ghidra_root.mkdir()
    (ghidra_root / "bridge_mcp_ghidra.py").write_text("# test\n", encoding="utf-8")
    (codex_home / "config.toml").write_text(
        f'[mcp_servers.ghidra]\ncommand = "uv"\nargs = ["run", "{ghidra_root / "bridge_mcp_ghidra.py"}"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("USER", "isolated-user")
    monkeypatch.setattr(
        "codex_dobby_mcp.paths._MACOS_GHIDRA_SOCKET_SCAN_ROOTS",
        (tmp_path / "no-live-sockets",),
    )

    assert reverse_engineer_default_writable_roots() == [ghidra_root.resolve()]


def test_reverse_engineer_default_writable_roots_ignore_unrelated_absolute_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(exist_ok=True)
    ghidra_root = tmp_path / "ghidra-mcp"
    ghidra_root.mkdir()
    bridge_path = ghidra_root / "bridge_mcp_ghidra.py"
    bridge_path.write_text("# test\n", encoding="utf-8")
    unrelated_dir = tmp_path / "unrelated-dir"
    unrelated_dir.mkdir()
    log_file = tmp_path / "ghidra.log"
    log_file.write_text("log\n", encoding="utf-8")
    (codex_home / "config.toml").write_text(
        (
            '[mcp_servers.ghidra]\n'
            'command = "uv"\n'
            f'args = ["run", "{bridge_path}", "--project-dir", "{unrelated_dir}", "--log-file", "{log_file}"]\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("USER", "isolated-user")
    monkeypatch.setattr(
        "codex_dobby_mcp.paths._MACOS_GHIDRA_SOCKET_SCAN_ROOTS",
        (tmp_path / "no-live-sockets",),
    )

    assert reverse_engineer_default_writable_roots() == [ghidra_root.resolve()]


def test_reverse_engineer_default_writable_roots_include_live_socket_runtime_from_tmpdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(exist_ok=True)
    ghidra_root = tmp_path / "ghidra-mcp"
    ghidra_root.mkdir()
    (ghidra_root / "bridge_mcp_ghidra.py").write_text("# test\n", encoding="utf-8")
    (codex_home / "config.toml").write_text(
        f'[mcp_servers.ghidra]\ncommand = "uv"\nargs = ["run", "{ghidra_root / "bridge_mcp_ghidra.py"}"]\n',
        encoding="utf-8",
    )
    runtime_tmp = tmp_path / "runtime-tmp"
    socket_dir = runtime_tmp / "ghidra-mcp-avery"
    socket_dir.mkdir(parents=True)
    (socket_dir / "ghidra-123.sock").write_text("", encoding="utf-8")

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("TMPDIR", str(runtime_tmp))
    monkeypatch.setenv("USER", "avery")
    monkeypatch.setattr(
        "codex_dobby_mcp.paths._MACOS_GHIDRA_SOCKET_SCAN_ROOTS",
        (tmp_path / "no-live-sockets",),
    )

    assert reverse_engineer_default_writable_roots() == [ghidra_root.resolve(), socket_dir.resolve()]


def test_reverse_engineer_default_writable_roots_fall_back_to_macos_socket_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(exist_ok=True)
    ghidra_root = tmp_path / "ghidra-mcp"
    ghidra_root.mkdir()
    (ghidra_root / "bridge_mcp_ghidra.py").write_text("# test\n", encoding="utf-8")
    (codex_home / "config.toml").write_text(
        f'[mcp_servers.ghidra]\ncommand = "uv"\nargs = ["run", "{ghidra_root / "bridge_mcp_ghidra.py"}"]\n',
        encoding="utf-8",
    )
    private_tmp = tmp_path / "private-tmp"
    private_tmp.mkdir()
    macos_scan_root = tmp_path / "var-folders"
    socket_dir = macos_scan_root / "aa" / "bb" / "T" / "ghidra-mcp-avery"
    socket_dir.mkdir(parents=True)
    (socket_dir / "ghidra-456.sock").write_text("", encoding="utf-8")

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("TMPDIR", str(private_tmp))
    monkeypatch.setenv("USER", "avery")
    monkeypatch.setattr(
        "codex_dobby_mcp.paths._MACOS_GHIDRA_SOCKET_SCAN_ROOTS",
        (macos_scan_root,),
    )

    assert reverse_engineer_default_writable_roots() == [ghidra_root.resolve(), socket_dir.resolve()]


def test_stall_threshold_for_effort_scales_with_effort() -> None:
    assert _stall_threshold_for_effort(ReasoningEffort.LOW) == 180.0
    assert _stall_threshold_for_effort(ReasoningEffort.MEDIUM) == 180.0
    assert _stall_threshold_for_effort(ReasoningEffort.HIGH) == 300.0
    assert _stall_threshold_for_effort(ReasoningEffort.XHIGH) == 300.0
    assert _stall_threshold_for_effort(ReasoningEffort.MAX) == 300.0
    assert _stall_threshold_for_effort(ReasoningEffort.ULTRA) == 300.0
    assert _stall_threshold_for_effort(None) == 180.0


def test_salvage_exec_trace_extracts_commands_with_outcomes() -> None:
    stderr = (
        "OpenAI Codex v0.121.0 (research preview)\n"
        "--------\n"
        "user\n"
        "some prompt text\n"
        "2026-04-19T00:00:00.000000Z TRACE codex_api::sse::responses: unhandled responses event: response.in_progress\n"
        "exec\n"
        "/bin/zsh -lc 'ls -la' in /repo\n"
        " succeeded in 3ms:\n"
        "total 0\n"
        "exec\n"
        "/bin/zsh -lc 'cat missing.txt' in /repo\n"
        " failed in 1ms:\n"
        "cat: missing.txt: No such file\n"
        "exec\n"
        "/bin/zsh -lc 'sleep 100' in /repo\n"
    )
    entries = _salvage_exec_trace(stderr)
    assert len(entries) == 3
    assert entries[0]["outcome"] == "succeeded"
    assert entries[0]["cmd"].startswith("/bin/zsh -lc 'ls -la'")
    assert entries[1]["outcome"] == "failed"
    assert entries[2]["outcome"] == "in-flight"


def test_salvage_worker_result_from_trace_builds_partial_result() -> None:
    stderr = (
        "response.in_progress\n"
        "exec\n"
        "/bin/zsh -lc 'rg foo' in /repo\n"
        " succeeded in 5ms:\n"
        "hit\n"
        "response.in_progress\n"
        "exec\n"
        "/bin/zsh -lc 'node script.js' in /repo\n"
    )
    result = _salvage_worker_result_from_trace(
        ToolName.BUILD, stderr, stall_hit=False, timeout_hit=True
    )
    assert result is not None
    assert result.completeness == Completeness.PARTIAL
    joined = "\n".join(result.important_facts)
    assert "2 model turn(s)" in joined
    assert "2 shell command(s)" in joined
    assert "rg foo" in joined
    assert any("salvaged" in w.lower() for w in result.warnings)


def test_salvage_worker_result_skips_when_not_stalled_or_timed_out() -> None:
    stderr = "exec\n/bin/zsh -lc 'ls' in /repo\n succeeded in 1ms:\n"
    assert _salvage_worker_result_from_trace(
        ToolName.BUILD, stderr, stall_hit=False, timeout_hit=False
    ) is None


def test_salvage_worker_result_skips_for_review_tool() -> None:
    stderr = "exec\n/bin/zsh -lc 'ls' in /repo\n succeeded in 1ms:\nresponse.in_progress\n"
    assert _salvage_worker_result_from_trace(
        ToolName.REVIEW, stderr, stall_hit=False, timeout_hit=True
    ) is None


def test_salvage_worker_result_returns_none_when_trace_is_empty() -> None:
    assert _salvage_worker_result_from_trace(
        ToolName.BUILD, "", stall_hit=True, timeout_hit=False
    ) is None


def test_stall_diagnostics_includes_bytes_and_last_line() -> None:
    stderr = (
        "OpenAI Codex v0.121.0\n"
        "2026-04-19T01:02:03.123456Z TRACE codex_api::sse::responses: unhandled responses event: response.in_progress\n"
        "user\n"
        "some task prompt line\n"
    )
    msg = _stall_diagnostics(stderr, "", 300.0)
    assert "300 seconds" in msg
    assert "stderr" in msg and "bytes" in msg
    assert "2026-04-19T01:02:03" in msg
    assert "some task prompt line" in msg


def test_stall_diagnostics_flags_pre_stream_hang_when_stderr_empty() -> None:
    msg = _stall_diagnostics("", "", 300.0)
    assert "before the model opened its response stream" in msg


@pytest.mark.asyncio
async def test_failed_run_persists_the_real_cause_not_the_cancelled_stub(
    tmp_path: Path,
) -> None:
    """A run that raises must not leave an artifact blaming cancellation.

    result.json starts as an aborted stub saying Dobby was cancelled or killed,
    which the normal completion path overwrites. When the run raised instead,
    that stub survived and misreported the cause to every later get_run.
    """
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
        codex_binary="/nonexistent/codex",
    )

    with pytest.raises(RunnerError, match="Codex executable not found"):
        await runner.run(ToolName.PLAN, InvocationRequest(prompt="inspect"))

    run_dirs = list((repo_root / ".codex-dobby" / "runs").iterdir())
    assert len(run_dirs) == 1
    persisted = json.loads((run_dirs[0] / "result.json").read_text(encoding="utf-8"))

    assert persisted["status"] == "error"
    assert "Codex executable not found" in persisted["summary"]
    assert "cancelled or killed" not in persisted["summary"]
    # The child scratch tree is still cleaned up on the exception path.
    assert not (run_dirs[0] / "runtime").exists()


@pytest.mark.asyncio
async def test_failure_artifact_is_not_recorded_as_a_sandbox_violation(
    tmp_path: Path,
) -> None:
    """An internal exception is not a sandbox boundary being crossed.

    The failure artifact reused the preflight builder, which puts its message in
    sandbox_violations. Any parse error or filesystem problem then looked like a
    sandbox escape to anyone acting on that field.
    """
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
        codex_binary="/nonexistent/codex",
    )

    with pytest.raises(RunnerError):
        await runner.run(ToolName.PLAN, InvocationRequest(prompt="inspect"))

    run_dir = next(iter((repo_root / ".codex-dobby" / "runs").iterdir()))
    persisted = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))

    assert persisted["status"] == "error"
    assert "Codex executable not found" in persisted["summary"]
    assert persisted["sandbox_violations"] == []


@pytest.mark.asyncio
async def test_failure_after_a_response_exists_still_persists_the_real_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If persisting a result is what failed, the artifact must say so.

    The caller receives the exception rather than the response, so keying the
    failure artifact on "no response was produced" left result.json claiming
    the run was cancelled.
    """
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
                    "summary": "planned",
                    "completeness": "full",
                    "important_facts": ["a fact"],
                    "next_steps": [],
                    "files_changed": [],
                    "warnings": [],
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": None,
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    real_write_json = runner_module.write_json
    calls = {"n": 0}

    def flaky_write_json(path, payload):  # type: ignore[no-untyped-def]
        # _finalize persists the result first and run_resolved persists it
        # again. Fail only the second write, so the failure happens with a
        # response already in hand — the case that previously left the aborted
        # stub in place while the caller received an exception.
        if path.name == "result.json" and payload.get("status") == "success":
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
        return real_write_json(path, payload)

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("codex_dobby_mcp.runner.write_json", flaky_write_json)

    with pytest.raises(OSError, match="disk full"):
        await runner.run(ToolName.PLAN, InvocationRequest(prompt="inspect"))

    assert calls["n"] == 2
    run_dir = next(iter((repo_root / ".codex-dobby" / "runs").iterdir()))
    persisted = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))

    assert persisted["status"] == "error"
    assert "disk full" in persisted["summary"]
    assert "cancelled or killed" not in persisted["summary"]


@pytest.mark.asyncio
async def test_cancelled_run_still_reports_a_private_runtime_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation must not hide credentials left on disk.

    The cancel path re-raises without producing a response, so a failed private
    runtime deletion was only logged and result.json kept the generic cancelled
    stub — while the copied auth and session data were still there.
    """
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

    async def cancel_mid_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise asyncio.CancelledError()

    def failing_private_cleanup(path, logger=None):  # type: ignore[no-untyped-def]
        return (
            "Failed to delete private runtime containing copied credentials and "
            f"session data at {path}: resource busy"
        )

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", cancel_mid_run)
    monkeypatch.setattr(
        "codex_dobby_mcp.runner._cleanup_private_child_runtime", failing_private_cleanup
    )

    with pytest.raises(asyncio.CancelledError):
        await runner.run(ToolName.PLAN, InvocationRequest(prompt="inspect"))

    run_dir = next(iter((repo_root / ".codex-dobby" / "runs").iterdir()))
    persisted = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))

    assert persisted["status"] == "error"
    assert "copied credentials and session data" in persisted["summary"]
    assert any("copied credentials" in warning for warning in persisted["warnings"])
    # Not misreported as a sandbox violation.
    assert persisted["sandbox_violations"] == []


def test_codex_home_warning_ignores_output_of_commands_the_child_ran() -> None:
    """A validate/build child's captured command output is not Dobby's diagnostics.

    Those tools exist to run commands, and the JSONL stream carries whatever
    those commands printed. Scanning it for Codex-home permission problems
    reported a project's own log text as a Dobby fault.
    """
    child_command_output = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": "/bin/zsh -lc 'pytest -q'",
                "aggregated_output": (
                    "codex cannot access session files at /users/x/.codex/sessions "
                    "(permission denied)"
                ),
            },
        }
    )

    assert _codex_home_permission_issue([], stdout=child_command_output) is None

    # A genuine Codex diagnostic on stderr is still reported.
    genuine = _codex_home_permission_issue(
        [],
        stderr="codex cannot access session files at /users/x/.codex/sessions (permission denied)",
    )
    assert genuine is not None
    assert "could not access" in genuine

    # And a non-command event on stdout is still scanned.
    non_command = json.dumps(
        {
            "type": "error",
            "message": (
                "codex cannot access session files at /users/x/.codex/sessions "
                "(permission denied)"
            ),
        }
    )
    assert _codex_home_permission_issue([], stdout=non_command) is not None


def test_quarantine_reports_the_real_global_evidence_failure() -> None:
    """The quarantine substitutes a manifest-less result and recomputes.

    That early-returned with only "omitted the required review_execution
    manifest", discarding the actual tamper alarm — a nested child session, a
    root that never proved v2 — and the rollouts proving it are deleted with
    the private runtime moments later.
    """
    evidence = ReviewRolloutEvidence(
        collected=True,
        agents=(),
        validation_issues=(
            "unexpected or nested v2 child sessions: /root/interloper",
        ),
    )

    diagnostics = _review_execution_diagnostics(
        _quarantined_review_worker_result(),
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        evidence,
    )

    assert diagnostics.manifest_validated is False
    assert any(
        "nested v2 child sessions" in issue
        for issue in diagnostics.validation_issues
    ), diagnostics.validation_issues
    assert "nested v2 child sessions" in diagnostics.failure_summary()


@pytest.mark.asyncio
async def test_runner_wires_out_of_band_assignments_into_the_private_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the production wiring of out-of-band assignment delivery.

    Review tests monkeypatch _review_rollout_evidence wholesale, so passing an
    empty assignment mapping, or writing the per-run agent definitions into the
    repository instead of the auto-deleted private runtime, both left the suite
    green. The second would persist the caller's task prompt, files and context
    under .codex-dobby/runs/<id>/ inside a directory mounted for the child.
    """
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

    captured: dict = {}
    real_build = runner_module.build_codex_command

    def capturing_build(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return real_build(*args, **kwargs)

    class FakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        Path(args[args.index("--output-last-message") + 1]).write_text(
            json.dumps(
                {
                    "summary": "s",
                    "completeness": "blocked",
                    "important_facts": [],
                    "next_steps": [],
                    "files_changed": [],
                    "warnings": [],
                    "refused": False,
                    "file_diffs": [],
                    "review_execution": None,
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    async def fake_v2_available(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr("codex_dobby_mcp.runner.build_codex_command", capturing_build)
    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        "codex_dobby_mcp.runner._ensure_multi_agent_v2_available", fake_v2_available
    )

    await runner.run(
        ToolName.REVIEW,
        InvocationRequest(
            prompt="review it",
            repo_root=str(repo_root),
            agents=[ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        ),
    )

    assignments = captured.get("review_assignments")
    assert assignments, "runner passed no assignments; out-of-band delivery is disabled"
    assert set(assignments) == {"dobby_review_correctness", "dobby_review_regression"}
    for assignment in assignments.values():
        assert assignment.message.strip(), "assignment message is empty"
        assert assignment.digest in assignment.message

    config_dir = captured.get("review_agent_config_dir")
    assert config_dir is not None
    # Must live in the private runtime, which is deleted with the run — never
    # in the repository, where it would persist the caller's prompt and context.
    assert ".codex-dobby" not in str(config_dir), config_dir
    assert not str(config_dir).startswith(str(repo_root)), config_dir
    assert config_dir.name == "dobby-agents"


def test_a_single_child_reporting_file_diffs_is_an_integrity_failure(
    tmp_path: Path,
) -> None:
    """A review child that reports file changes must fail integrity on its own.

    `reported_file_diffs = reported_file_diffs or has_file_diffs` accumulates
    across terminal events. Mutating that `or` to `and` leaves it False unless
    *every* observed event claimed diffs, so one mutating child would pass
    unreported — and review children are required to change nothing.
    """
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    child = codex_home / "sessions" / "2026" / "08" / "12" / "child-correctness.jsonl"
    items = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    for item in items:
        payload = item.get("payload", {})
        if payload.get("type") != "task_complete":
            continue
        result = json.loads(payload["last_agent_message"])
        result["file_diffs"] = [
            {
                "path": "sneaky.py",
                "oldText": "before",
                "newText": "after",
                "truncated": False,
            }
        ]
        payload["last_agent_message"] = json.dumps(result)
    child.write_text(
        "\n".join(json.dumps(item) for item in items) + "\n", encoding="utf-8"
    )

    evidence = _review_rollout_evidence(
        codex_home,
        [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )
    by_task = {agent.task_name: agent for agent in evidence.agents}
    correctness = by_task["dobby_review_correctness"]
    regression = by_task["dobby_review_regression"]

    assert correctness.reported_file_diffs is True
    assert "sneaky.py" in correctness.reported_mutation_paths

    issues = _rollout_validation_issues(
        correctness, ReviewAgentExecutionStatus.COMPLETED
    )
    assert any("non-empty file_diffs" in issue for issue in issues.integrity)

    # The untouched lens is unaffected: this is per-lens integrity loss.
    assert regression.reported_file_diffs is False
    assert not _rollout_validation_issues(
        regression, ReviewAgentExecutionStatus.COMPLETED
    ).integrity


# ---------------------------------------------------------------------------
# Malformed rollout records must be ignored, not counted.
#
# Every `isinstance(...) and isinstance(...)` in the rollout reader decides
# whether a record becomes evidence. Relaxing one to `or` admits a malformed
# record as a spawn, a start, or a delivery that never happened, which then
# feeds the per-lens counts the integrity verdict is built from.
# ---------------------------------------------------------------------------

_HEALTHY_COUNTS = {
    "spawn_attempt_count": 1,
    "started_count": 1,
    "delivered_count": 1,
    "terminal_event_count": 1,
}


def _rollouts_with_extra_root_records(tmp_path: Path, *extra: dict) -> Path:
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    root = codex_home / "sessions" / "2026" / "08" / "12" / "root.jsonl"
    with root.open("a", encoding="utf-8") as handle:
        for item in extra:
            handle.write(json.dumps(item) + "\n")
    return codex_home


@pytest.mark.parametrize(
    "record",
    [
        # started event whose agent_path is not a string
        {"type": "event_msg", "payload": {
            "type": "sub_agent_activity", "kind": "started", "agent_path": 7}},
        # a non-started sub_agent_activity
        {"type": "event_msg", "payload": {
            "type": "sub_agent_activity", "kind": "finished",
            "agent_path": "/root/dobby_review_correctness"}},
        # A *consumed* event_msg of the wrong payload type. `task_complete` is
        # the discriminating choice: an unconsumed type (say "something_else")
        # is dropped by `_is_consumed_rollout_record` before the branch is ever
        # reached, so it cannot distinguish `and` from `or` here.
        {"type": "event_msg", "payload": {
            "type": "task_complete", "kind": "started",
            "agent_path": "/root/dobby_review_correctness",
            "last_agent_message": "{}"}},
        # agent_message with a non-string author
        {"type": "response_item", "payload": {
            "type": "agent_message", "author": 7, "recipient": "/root",
            "content": [{"type": "input_text", "text": "Message Type: FINAL_ANSWER\nPayload:\n{}"}]}},
        # agent_message addressed somewhere other than the root
        {"type": "response_item", "payload": {
            "type": "agent_message", "author": "/root/dobby_review_correctness",
            "recipient": "/root/elsewhere",
            "content": [{"type": "input_text", "text": "Message Type: FINAL_ANSWER\nPayload:\n{}"}]}},
        # agent_message whose content is not a list
        {"type": "response_item", "payload": {
            "type": "agent_message", "author": "/root/dobby_review_correctness",
            "recipient": "/root", "content": "not a list"}},
        # response_item of an unrelated payload type
        {"type": "response_item", "payload": {
            "type": "reasoning", "author": "/root/dobby_review_correctness",
            "recipient": "/root", "content": []}},
    ],
)
def test_malformed_root_records_do_not_become_evidence(
    tmp_path: Path, record: dict
) -> None:
    codex_home = _rollouts_with_extra_root_records(tmp_path, record)

    evidence = _review_rollout_evidence(
        codex_home, [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION]
    )

    assert evidence.validation_issues == ()
    for agent in evidence.agents:
        for field, expected in _HEALTHY_COUNTS.items():
            assert getattr(agent, field) == expected, (agent.task_name, field)
        assert agent.observed_status == ReviewAgentExecutionStatus.COMPLETED


def test_a_spawn_with_a_blank_task_name_is_reported(tmp_path: Path) -> None:
    """Kills `isinstance(task_name, str) and task_name` -> `or`.

    An empty string is not a task name. Accepting it records a spawn attempt
    for a lens that does not exist instead of flagging the malformed call.
    """
    codex_home = _rollouts_with_extra_root_records(
        tmp_path,
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "spawn_agent",
            "arguments": json.dumps({"task_name": ""})}},
    )

    evidence = _review_rollout_evidence(
        codex_home, [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION]
    )

    assert "v2 spawn attempt omitted a valid task_name" in evidence.validation_issues


def test_list_agents_entries_need_string_path_and_payload(tmp_path: Path) -> None:
    """Kills `isinstance(agent_path, str) and isinstance(completed_payload, str)`.

    A parent-relayed `list_agents` result is only a corroborating signal, so a
    malformed entry must contribute nothing rather than be counted as a second
    delivery for the lens.
    """
    call_id = "call-listagents-malformed"
    codex_home = _rollouts_with_extra_root_records(
        tmp_path,
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "list_agents", "call_id": call_id,
            "arguments": "{}"}},
        {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": call_id,
            "output": json.dumps({"agents": [
                {"agent_name": 7, "agent_status": {"completed": "{}"}},
                {"agent_name": "/root/dobby_review_correctness",
                 "agent_status": {"completed": {"not": "a string"}}},
                "not a dict",
            ]})}},
    )

    evidence = _review_rollout_evidence(
        codex_home, [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION]
    )

    assert evidence.validation_issues == ()
    by_task = {a.task_name: a for a in evidence.agents}
    assert by_task["dobby_review_correctness"].delivered_count == 1


def test_a_function_call_without_a_string_call_id_is_not_correlatable(
    tmp_path: Path,
) -> None:
    """Kills `isinstance(call_id, str) and isinstance(call_name, str)` -> `or`.

    The call_id -> name map is what ties a `function_call_output` back to the
    `list_agents` call that produced it. An unusable id must not enter it.
    """
    codex_home = _rollouts_with_extra_root_records(
        tmp_path,
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "list_agents", "call_id": 99,
            "arguments": "{}"}},
        {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": 99,
            "output": json.dumps({"agents": [
                {"agent_name": "/root/dobby_review_correctness",
                 "agent_status": {"completed": json.dumps({"summary": "forged"})}},
            ]})}},
    )

    evidence = _review_rollout_evidence(
        codex_home, [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION]
    )

    by_task = {a.task_name: a for a in evidence.agents}
    assert by_task["dobby_review_correctness"].delivered_count == 1
    assert evidence.validation_issues == ()


def test_a_child_session_without_a_usable_thread_id_is_not_counted(
    tmp_path: Path,
) -> None:
    """Kills `isinstance(thread_id, str) and thread_id` -> `or`.

    `child_thread_ids` is compared against the started-event thread ids, so an
    empty or non-string id must not be admitted as a session identity.
    """
    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {
            ReviewAgent.CORRECTNESS: Completeness.FULL,
            ReviewAgent.REGRESSION: Completeness.FULL,
        },
    )
    child = codex_home / "sessions" / "2026" / "08" / "12" / "child-correctness.jsonl"
    items = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    for item in items:
        if item.get("type") == "session_meta":
            item["payload"]["id"] = ""
    child.write_text(
        "\n".join(json.dumps(item) for item in items) + "\n", encoding="utf-8"
    )

    evidence = _review_rollout_evidence(
        codex_home, [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION]
    )
    by_task = {a.task_name: a for a in evidence.agents}
    correctness = by_task["dobby_review_correctness"]

    assert correctness.child_thread_ids == ()
    assert correctness.observed_status == ReviewAgentExecutionStatus.FAILED
    # The other lens is untouched.
    assert by_task["dobby_review_regression"].observed_status == (
        ReviewAgentExecutionStatus.COMPLETED
    )

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from typing import BinaryIO, NamedTuple
from pathlib import Path

from pydantic import ValidationError

from codex_dobby_mcp.codex_cli import build_codex_command
from codex_dobby_mcp.events import map_codex_chunk
from codex_dobby_mcp.gitignore import ensure_codex_dobby_ignored
from codex_dobby_mcp.logging_utils import get_logger
from codex_dobby_mcp.models import (
    Completeness,
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORTS,
    FileDiff,
    GhidraDetails,
    GhidraUsageMode,
    InvocationRequest,
    MUTATING_TOOLS,
    READ_ONLY_TOOLS,
    RepoSnapshot,
    ResultArtifactState,
    ResolvedInvocation,
    ReverseEngineerDetails,
    ReviewDetails,
    RunArtifacts,
    RunStatus,
    ReasoningEffort,
    ReviewAgentExecutionStatus,
    ReviewExecutionEvidence,
    ReviewExecutionManifest,
    ReviewOrchestrationProtocol,
    StopReason,
    ToolName,
    ToolResponse,
    WorkerResult,
)
from codex_dobby_mcp.paths import (
    PathResolutionError,
    create_run_artifacts,
    mcp_server_is_enabled,
    prompt_git_worktrees,
    prompt_referenced_relative_paths,
    private_runtime_root,
    public_file_label,
    resolve_extra_roots,
    resolve_repo_root,
    reverse_engineer_default_readonly_roots,
    reverse_engineer_default_writable_roots,
    write_json,
)
from codex_dobby_mcp.prompts import PromptLoader
from codex_dobby_mcp.review_agents import (
    REVIEW_SUBAGENT_DEFAULT_MODEL,
    REVIEW_SUBAGENT_DEFAULT_REASONING_EFFORT,
    review_uses_orchestrator,
    selected_review_agents,
    selected_review_agent_definitions,
)
from codex_dobby_mcp.safeguards import child_environment, ensure_not_recursive


class RunnerError(RuntimeError):
    pass


_MISSING_PATH_FINGERPRINT = "<missing>"
_POST_TIMEOUT_TERMINATE_GRACE_SECONDS = 1.0
_POST_TIMEOUT_KILL_WAIT_SECONDS = 0.25
_POST_TIMEOUT_IO_DRAIN_SECONDS = 1.0
_CODEX_STALL_THRESHOLD_SECONDS = 180.0
_CODEX_STALL_THRESHOLD_HIGH_EFFORT_SECONDS = 300.0
_CODEX_STALL_CHECK_INTERVAL_SECONDS = 15.0
_SALVAGE_EXEC_TAIL_COUNT = 8
_SALVAGE_LINE_MAX_CHARS = 240
_POST_RUN_SNAPSHOT_MIN_BUDGET_SECONDS = 20.0
_GLOBAL_CLAUDE_DIR = Path("~/.claude").expanduser()
_GLOBAL_CODEX_DIR = Path("~/.codex").expanduser()
_CONFIG_PATH_DELIMITERS = " \t\r\n\"',[](){}"
_CONFIG_PATH_TRAILING_PUNCTUATION = ",.;:)]}>"
_GHIDRA_MCP_CALL_RE = re.compile(r"^mcp: ghidra/([a-z_]+) started$", re.MULTILINE)
_GHIDRA_HELPER_CALL_RE = re.compile(r"\bgh\.dispatch_(?:get|post)\('/([a-z_]+)'")
_GHIDRA_STARTUP_ONLY_CALLS = frozenset(
    {
        "list_instances",
        "connect_instance",
        "list_tool_groups",
        "load_tool_group",
        "check_tools",
    }
)

_FILE_DIFF_MAX_BYTES = 2 * 1024 * 1024  # per-file content cap for diff capture
_FILE_DIFF_BINARY_SCAN_BYTES = 8192


@dataclass(frozen=True)
class ReviewExecutionDiagnostics:
    expected_agents: tuple[str, ...]
    manifest: ReviewExecutionManifest | None
    execution_evidence: ReviewExecutionEvidence | None
    validation_issues: tuple[str, ...]
    completed_count: int
    partial_count: int
    blocked_count: int
    failed_count: int
    # Per-lens non-delivery. Kept apart from ``validation_issues`` because a
    # lens that produced nothing is ordinary partial coverage, not evidence
    # tampering: its findings are already excluded from the caller-visible
    # response, so the lenses that did deliver stay usable.
    coverage_issues: tuple[str, ...] = ()

    @property
    def expected_count(self) -> int:
        return len(self.expected_agents)

    @property
    def manifest_validated(self) -> bool:
        return self.manifest is not None and not self.validation_issues

    @property
    def usable_count(self) -> int:
        return self.completed_count + self.partial_count

    @property
    def all_completed(self) -> bool:
        return self.manifest_validated and self.completed_count == self.expected_count

    @property
    def warnings(self) -> list[str]:
        warnings = list(self.validation_issues)
        warnings.extend(self.coverage_issues)
        if self.manifest_validated and not self.all_completed:
            warnings.append(
                "Review execution manifest reports "
                f"{self.completed_count} completed, {self.partial_count} partial, "
                f"{self.blocked_count} blocked, and {self.failed_count} failed of "
                f"{self.expected_count} selected review agents"
            )
        return warnings

    def failure_summary(self) -> str:
        if self.validation_issues:
            return "Review multi-agent v2 execution manifest is invalid: " + "; ".join(
                self.validation_issues
            )
        return (
            "Review multi-agent v2 execution produced no usable child results: "
            f"0/{self.expected_count} selected review agents returned usable output."
        )


@dataclass(frozen=True)
class ReviewRolloutAgentEvidence:
    review_agent: str
    task_name: str
    agent_role: str
    spawn_attempt_count: int
    started_count: int
    child_thread_ids: tuple[str, ...]
    child_roles: tuple[str | None, ...]
    delivered_count: int
    child_result: WorkerResult | None
    # Deliberately has no default. A default of 1 silently claims the child
    # reached a terminal event, which is exactly the state a non-delivering
    # lens is in, so every construction site must say what it observed.
    terminal_event_count: int
    # How the parent spawned this lens: wrong agent_type, fork_turns other than
    # "none", missing/extra fields, or an unexpected encrypted argument shape.
    # These are isolation and role guarantees, so they stay integrity failures.
    spawn_contract_issues: tuple[str, ...] = ()
    # The delivered child payload did not attest its canonical assignment
    # digest. The payload is discarded, so this can only cost this lens its own
    # slot — it is coverage loss, not evidence tampering.
    attestation_issues: tuple[str, ...] = ()
    child_multi_agent_versions: tuple[str | None, ...] = ()
    started_thread_ids: tuple[str | None, ...] = ()
    reported_mutation_paths: tuple[str, ...] = ()
    reported_file_diffs: bool = False

    @property
    def observed_status(self) -> ReviewAgentExecutionStatus:
        if (
            self.spawn_attempt_count != 1
            or self.spawn_contract_issues
            or self.attestation_issues
            or self.started_count != 1
            or len(self.child_thread_ids) != 1
            or self.started_thread_ids != self.child_thread_ids
            or self.child_roles != (self.agent_role,)
            or self.child_multi_agent_versions != ("disabled",)
            or self.terminal_event_count != 1
            or self.delivered_count != 1
            or self.child_result is None
            or self.reported_mutation_paths
            or self.reported_file_diffs
        ):
            return ReviewAgentExecutionStatus.FAILED
        if self.child_result.refused:
            return ReviewAgentExecutionStatus.BLOCKED
        if self.child_result.completeness == Completeness.FULL:
            return ReviewAgentExecutionStatus.COMPLETED
        if self.child_result.completeness == Completeness.PARTIAL:
            return ReviewAgentExecutionStatus.PARTIAL
        return ReviewAgentExecutionStatus.BLOCKED


@dataclass(frozen=True)
class ReviewRolloutEvidence:
    collected: bool
    agents: tuple[ReviewRolloutAgentEvidence, ...]
    validation_issues: tuple[str, ...]

    @property
    def by_task_name(self) -> dict[str, ReviewRolloutAgentEvidence]:
        return {agent.task_name: agent for agent in self.agents}


@dataclass(frozen=True)
class ChildRuntimeContext:
    private_root: Path
    codex_home: Path
    claude_config_dir: Path
    home_config_path: Path
    env_overrides: dict[str, str]
    # The child's TMPDIR and cache roots, inside the run's artifact directory
    # in the user's repo. Holds no credentials — those live under
    # ``private_root`` — but it grows without bound if it is not removed.
    scratch_root: Path


class CodexRunner:
    def __init__(
        self,
        spawn_root: Path,
        prompts_root: Path,
        worker_schema_path: Path,
        review_agents_root: Path,
        codex_binary: str = "/opt/homebrew/bin/codex",
    ):
        self.spawn_root = spawn_root.resolve()
        self.prompts = PromptLoader(prompts_root)
        self.worker_schema_path = worker_schema_path.resolve()
        self.review_agents_root = review_agents_root.resolve()
        self.codex_binary = codex_binary
        self.logger = get_logger("runner")

    def prepare(self, tool: ToolName, request: InvocationRequest) -> ResolvedInvocation:
        ensure_not_recursive()
        return self._resolve(tool, request)

    async def run(
        self,
        tool: ToolName,
        request: InvocationRequest,
        *,
        event_sink: EventSink | None = None,
    ) -> ToolResponse:
        spec = self.prepare(tool, request)
        return await self.run_resolved(spec, event_sink=event_sink)

    async def run_resolved(
        self,
        spec: ResolvedInvocation,
        *,
        event_sink: EventSink | None = None,
    ) -> ToolResponse:
        started = time.monotonic()
        deadline = started + spec.request.timeout_seconds
        use_metadata_fingerprints = _snapshot_uses_metadata(spec.tool)
        self._persist_request(spec)
        # Write a placeholder result.json so an externally-killed run always
        # leaves an artifact behind. The normal path overwrites this on
        # completion (success or handled failure); if Dobby is cancelled or
        # killed before that happens, this stub remains as the record of
        # what the caller can still see.
        self._write_aborted_stub(spec)
        try:
            child_runtime = _prepare_child_runtime(spec.artifacts)
        except RunnerError as exc:
            preflight_response = self._preflight_response(spec, started, str(exc))
            write_json(spec.artifacts.result_json, preflight_response.model_dump(mode="json", by_alias=True))
            return preflight_response

        response: ToolResponse | None = None
        failure: BaseException | None = None
        cancelled = False
        try:
            if spec.tool == ToolName.REVIEW and review_uses_orchestrator(
                spec.request.agents
            ):
                try:
                    await _ensure_multi_agent_v2_available(
                        self.codex_binary,
                        spec.repo_root,
                        child_environment(
                            os.environ,
                            overrides=child_runtime.env_overrides,
                        ),
                        deadline,
                    )
                except asyncio.TimeoutError:
                    response = self._timeout_response(
                        spec,
                        started,
                        spec.request.timeout_seconds,
                    )
                except RunnerError as exc:
                    response = self._preflight_response(
                        spec,
                        started,
                        str(exc),
                    )
            if response is None:
                response = await self._run_with_child_runtime(
                    spec,
                    started,
                    deadline,
                    use_metadata_fingerprints,
                    child_runtime,
                    event_sink=event_sink,
                )
            write_json(
                spec.artifacts.result_json,
                response.model_dump(mode="json", by_alias=True),
            )
        except asyncio.CancelledError:
            # The aborted stub describes cancellation accurately on its own,
            # but a cleanup failure during cancellation still has to surface.
            cancelled = True
            raise
        except Exception as exc:
            # Remember why, so the persisted artifact reports the real cause
            # instead of the aborted stub's "cancelled or killed".
            failure = exc
            raise
        finally:
            # The child's scratch tree lives in the user's repo and can reach
            # hundreds of MB per run, so it is removed on every exit path.
            # Unlike the private runtime it holds no credentials, so a failed
            # delete is a warning rather than a run-failing error.
            scratch_issue = _cleanup_child_scratch_root(
                child_runtime.scratch_root,
                self.logger,
            )
            if scratch_issue is not None and response is not None:
                response = response.model_copy(
                    update={
                        "warnings": _merge_preserving_order(
                            response.warnings, [scratch_issue]
                        )
                    }
                )
            cleanup_issue = _cleanup_private_child_runtime(
                child_runtime.private_root,
                self.logger,
            )
            if cleanup_issue is not None and response is not None:
                response = _response_with_private_runtime_cleanup_failure(
                    response,
                    cleanup_issue,
                )
            cleanup_warnings = [
                issue for issue in (scratch_issue, cleanup_issue) if issue is not None
            ]
            if failure is not None:
                # The run raised. Replace the aborted stub, which claims Dobby
                # was cancelled or killed, with the actual cause plus any
                # cleanup issues that would otherwise only reach the log. This
                # runs even when a response object exists, because the caller
                # receives the exception rather than that response — for
                # example when persisting a successful result is what failed.
                self._write_failure_artifact(
                    spec,
                    started,
                    f"{type(failure).__name__}: {failure}",
                    cleanup_warnings,
                )
            elif cancelled and cleanup_issue is not None:
                # Cancellation itself is described by the aborted stub, but a
                # private-runtime deletion failure means copied credentials and
                # session data are still on disk. README promises that is
                # reported, so it must not be swallowed by the cancel path.
                self._write_failure_artifact(
                    spec, started, cleanup_issue, cleanup_warnings
                )
            elif (
                scratch_issue is not None or cleanup_issue is not None
            ) and response is not None:
                # Rewrite the persisted artifact so a later get_run sees the
                # same warnings the caller just received.
                write_json(
                    spec.artifacts.result_json,
                    response.model_dump(mode="json", by_alias=True),
                )
        assert response is not None
        return response

    async def _run_with_child_runtime(
        self,
        spec: ResolvedInvocation,
        started: float,
        deadline: float,
        use_metadata_fingerprints: bool,
        child_runtime: ChildRuntimeContext,
        *,
        event_sink: EventSink | None = None,
    ) -> ToolResponse:
        tool = spec.tool
        request = spec.request
        try:
            baseline = await _capture_repo_snapshot_with_deadline(
                spec.repo_root,
                deadline,
                include_head=tool in MUTATING_TOOLS,
                use_metadata_fingerprints=use_metadata_fingerprints,
            )
        except asyncio.TimeoutError:
            return self._timeout_response(spec, started, request.timeout_seconds)

        # Reading file contents is blocking I/O. With many dirty files at
        # baseline this could hold the event loop long enough to delay
        # streaming events for OTHER concurrent runs, so push the work to
        # a thread.
        baseline_dirty_contents = (
            await asyncio.to_thread(
                _capture_pre_run_contents, spec.repo_root, baseline.dirty_files
            )
            if tool in MUTATING_TOOLS
            else {}
        )

        rendered_prompt = self.prompts.render_prompt(
            tool=tool,
            request=spec.request,
            repo_root=spec.repo_root,
            sandbox_roots=spec.sandbox_roots,
            advisory_read_only_roots=spec.advisory_read_only_roots,
            model=spec.model,
            reasoning_effort=spec.reasoning_effort.value,
            fetchaller_available=spec.fetchaller_available,
            ghidra_available=spec.ghidra_available,
        )
        prompt_text = rendered_prompt.text
        spec.artifacts.prompt_txt.write_text(prompt_text, encoding="utf-8")
        spec.artifacts.output_schema_json.write_text(self.worker_schema_path.read_text(encoding="utf-8"), encoding="utf-8")

        exit_code: int | None = None
        timeout_hit = False
        stall_hit = False
        command = build_codex_command(
            spec,
            self.codex_binary,
            spec.artifacts.output_schema_json,
            self.review_agents_root,
            child_runtime.home_config_path,
            # The assignments the prompt embedded, delivered to each child
            # through its own agent definition in the private runtime so the
            # parent cannot decide what a reviewer reviews.
            review_assignments=rendered_prompt.review_subagent_assignments,
            review_agent_config_dir=child_runtime.codex_home / "dobby-agents",
        )
        child_env = child_environment(os.environ, overrides=child_runtime.env_overrides)

        process = None
        try:
            process = await _create_process_with_deadline(
                command.argv,
                spec.repo_root,
                child_env,
                deadline,
            )
            remaining = _seconds_remaining(deadline)
            exit_code, timeout_hit, stall_hit = await _execute_process_with_streaming_logs(
                process,
                prompt_text.encode("utf-8"),
                spec.artifacts.stdout_log,
                spec.artifacts.stderr_log,
                remaining,
                stall_threshold_seconds=_stall_threshold_for_effort(spec.reasoning_effort),
                events_jsonl=spec.artifacts.events_jsonl,
                event_sink=event_sink,
            )
        except FileNotFoundError as exc:
            raise RunnerError(f"Codex executable not found: {self.codex_binary}") from exc
        except asyncio.CancelledError:
            # Backstop: the streaming helper already reaps on cancel, but if
            # the process somehow survived, ensure it is killed before we
            # unwind. Shielded so the escalation is not itself cancelled.
            if process is not None:
                await _shielded_terminate_process(process, timeout=_POST_TIMEOUT_TERMINATE_GRACE_SECONDS)
            raise
        except asyncio.TimeoutError:
            timeout_hit = True
            if process is not None:
                await _terminate_process(process)

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = _read_log_text(spec.artifacts.stdout_log)
        stderr = _read_log_text(spec.artifacts.stderr_log)

        worker_result_error: str | None = None
        review_is_orchestrated = tool == ToolName.REVIEW and review_uses_orchestrator(spec.request.agents)
        review_rollout_evidence = (
            await asyncio.to_thread(
                _review_rollout_evidence,
                child_runtime.codex_home,
                spec.request.agents,
                # The exact strings the prompt embedded, not a second
                # derivation of them: each child attests a SHA-256 digest of
                # its assignment, and validation compares against these.
                rendered_prompt.review_subagent_messages,
                allow_truncated_tail=timeout_hit or exit_code not in (0, None),
            )
            if review_is_orchestrated
            else None
        )
        try:
            worker_result = self._load_worker_result(spec.artifacts, allow_missing=exit_code != 0 or timeout_hit)
        except RunnerError as exc:
            worker_result = None
            worker_result_error = str(exc)
        recovered_review_from_rollouts = False
        if worker_result is None and review_rollout_evidence is not None:
            worker_result = _salvaged_review_worker_result_from_rollouts(
                review_rollout_evidence
            )
            if worker_result is not None:
                recovered_review_from_rollouts = True
                worker_result_error = None
                _write_salvage_last_message(spec.artifacts, worker_result)
        if worker_result is None:
            salvaged = _salvage_worker_result_from_trace(
                tool,
                stderr,
                stall_hit=stall_hit,
                timeout_hit=timeout_hit,
            )
            if salvaged is not None:
                worker_result = salvaged
                worker_result_error = None
                _write_salvage_last_message(spec.artifacts, salvaged)
        review_execution_diagnostics = (
            _review_execution_diagnostics(
                worker_result,
                spec.request.agents,
                review_rollout_evidence,
            )
            if review_is_orchestrated
            else None
        )
        if (
            review_execution_diagnostics is not None
            and review_rollout_evidence is not None
            and review_rollout_evidence.collected
            and not review_rollout_evidence.validation_issues
            and not recovered_review_from_rollouts
        ):
            # The parent is responsible for orchestration, but its prose is
            # not independent evidence. Return only facts, next steps,
            # warnings, and per-agent summaries reconstructed from the exact
            # terminal child results that the rollout validator correlated.
            verified_worker_result = _verified_review_worker_result_from_rollouts(
                review_rollout_evidence
            )
            verified_diagnostics = _review_execution_diagnostics(
                verified_worker_result,
                spec.request.agents,
                review_rollout_evidence,
            )
            if (
                verified_diagnostics.manifest_validated
                and not review_execution_diagnostics.manifest_validated
            ):
                verified_worker_result = verified_worker_result.model_copy(
                    update={
                        "warnings": _merge_preserving_order(
                            verified_worker_result.warnings,
                            [
                                "Parent review synthesis or manifest was invalid; Dobby rebuilt the "
                                "response from independently verified child rollout results."
                            ],
                        )
                    }
                )
            worker_result = verified_worker_result
            review_execution_diagnostics = verified_diagnostics
        elif (
            review_execution_diagnostics is not None
            and review_rollout_evidence is not None
            and (
                not review_rollout_evidence.collected
                or review_rollout_evidence.validation_issues
            )
        ):
            # Global rollout failures break the evidence chain for every child.
            # Do not leak parent-authored or partially parsed child prose through
            # fields that callers may consume even when status=error.
            worker_result = _quarantined_review_worker_result()
            review_execution_diagnostics = _review_execution_diagnostics(
                worker_result,
                spec.request.agents,
                review_rollout_evidence,
            )
        timeout_with_usable_review = (
            tool == ToolName.REVIEW
            and timeout_hit
            and worker_result is not None
            and worker_result.completeness != Completeness.BLOCKED
            and (
                review_execution_diagnostics is None
                or (
                    review_execution_diagnostics.manifest_validated
                    and review_execution_diagnostics.usable_count > 0
                )
            )
        )
        nonzero_with_usable_review = (
            tool == ToolName.REVIEW
            and exit_code not in (0, None)
            and worker_result is not None
            and review_execution_diagnostics is not None
            and review_execution_diagnostics.manifest_validated
            and review_execution_diagnostics.usable_count > 0
        )
        post_run_snapshot_incomplete = False
        post_run_snapshot_deadline = max(
            deadline,
            time.monotonic() + _POST_RUN_SNAPSHOT_MIN_BUDGET_SECONDS,
        )
        try:
            after_snapshot = await _capture_repo_snapshot_with_deadline(
                spec.repo_root,
                post_run_snapshot_deadline,
                include_head=tool in MUTATING_TOOLS,
                use_metadata_fingerprints=use_metadata_fingerprints,
            )
            if tool in MUTATING_TOOLS:
                current_head = after_snapshot.head_commit
            else:
                current_head = baseline.head_commit
        except asyncio.TimeoutError:
            after_snapshot = baseline
            current_head = None
            post_run_snapshot_incomplete = True

        rollout_reported_files = (
            [
                path
                for agent_evidence in review_rollout_evidence.agents
                for path in agent_evidence.reported_mutation_paths
            ]
            if review_rollout_evidence is not None
            else []
        )
        reported_files = _merge_preserving_order(
            worker_result.files_changed if worker_result else [],
            rollout_reported_files,
        )
        detected_files = _changed_status_files(baseline, after_snapshot)
        files_changed = detected_files

        status = RunStatus.SUCCESS
        stop_reason: StopReason | None = None
        warnings = list(worker_result.warnings if worker_result else [])
        error_reasons: list[str] = []
        sandbox_violations = _collect_sandbox_violations(stderr, stdout)
        codex_home_issue = _codex_home_permission_issue(sandbox_violations, stderr=stderr, stdout=stdout)
        if post_run_snapshot_incomplete:
            warnings.append("Post-run repo snapshot timed out; repo change verification may be incomplete")

        if tool in READ_ONLY_TOOLS:
            unexpected_reported_files = [path for path in reported_files if path not in detected_files]
            if unexpected_reported_files:
                warnings.append(
                    "Worker reported file changes that wrapper did not observe: "
                    + ", ".join(unexpected_reported_files)
                )
        else:
            external_reported_files = [
                path
                for path in reported_files
                if path not in detected_files and not _path_is_within_repo(path, spec.repo_root)
            ]
            if external_reported_files:
                files_changed = _merge_preserving_order(files_changed, external_reported_files)

        if stall_hit:
            stall_warning = _stall_diagnostics(
                stderr,
                stdout,
                _stall_threshold_for_effort(spec.reasoning_effort),
            )
            warnings.append(stall_warning)
            status = RunStatus.ERROR
            stop_reason = StopReason.STALL
            error_reasons.append(stall_warning)
        elif timeout_hit:
            timeout_warning = _timeout_warning(spec)
            warnings.append(timeout_warning)
            stop_reason = StopReason.TIMEOUT
            if not timeout_with_usable_review:
                status = RunStatus.ERROR
                error_reasons.append(timeout_warning)
        elif exit_code != 0:
            stop_reason = StopReason.ERROR
            if nonzero_with_usable_review:
                warnings.append(
                    f"Codex parent exited with status {exit_code} after delivering "
                    "independently verified child review results; returning partial coverage."
                )
            else:
                status = RunStatus.ERROR
        if codex_home_issue is not None:
            warnings.append(codex_home_issue)
            if exit_code not in (0, None) and not stall_hit and not timeout_hit:
                error_reasons.append(codex_home_issue)
        if worker_result_error:
            status = RunStatus.ERROR
            warnings.append(worker_result_error)
            error_reasons.append(worker_result_error)
            if stop_reason is None:
                stop_reason = StopReason.ERROR

        recoverable_orchestration_only = False
        if review_execution_diagnostics is not None:
            warnings.extend(review_execution_diagnostics.warnings)
            if (
                not review_execution_diagnostics.manifest_validated
                or review_execution_diagnostics.usable_count == 0
            ):
                status = RunStatus.ERROR
                error_reasons.append(review_execution_diagnostics.failure_summary())
            elif not review_execution_diagnostics.all_completed:
                recoverable_orchestration_only = True

        if tool in READ_ONLY_TOOLS and detected_files:
            # The wrapper-detected diff combines (a) anything codex actually
            # wrote and (b) anything the user/another process changed in the
            # repo while codex was running. Codex's OS-level read-only
            # sandbox blocks (a) — Seatbelt on macOS, Landlock on Linux — so
            # in practice (a) is rare and (b) happens any time the user is
            # editing the repo from another terminal during a long research
            # run. Treat those cases differently:
            #   * Worker self-reported the same files → codex (or its
            #     worker prompt) tried to bypass read-only. Real sandbox
            #     violation, escalate to ERROR.
            #   * Worker reported nothing for those paths → external
            #     mutation. Surface as warning, do not error.
            worker_attributable = [path for path in detected_files if path in reported_files]
            if worker_attributable:
                status = RunStatus.ERROR
                worker_violation_warning = (
                    "Read-only tool reported worktree changes the wrapper "
                    "also observed: " + ", ".join(worker_attributable)
                )
                warnings.append(worker_violation_warning)
                error_reasons.append(worker_violation_warning)
                if stop_reason is None:
                    stop_reason = StopReason.SANDBOX_VIOLATION
            else:
                external_preview = ", ".join(detected_files[:10])
                overflow = (
                    f" (+{len(detected_files) - 10} more)"
                    if len(detected_files) > 10
                    else ""
                )
                warnings.append(
                    "External worktree changes observed during the run "
                    "(not attributable to codex; codex's read-only sandbox "
                    f"blocks writes). Affected paths: {external_preview}{overflow}"
                )
        if tool in MUTATING_TOOLS and post_run_snapshot_incomplete:
            status = RunStatus.ERROR
            verification_warning = "Post-run repo snapshot timed out; mutating tool results could not be fully verified"
            warnings.append(verification_warning)
            error_reasons.append(verification_warning)
            if stop_reason is None:
                stop_reason = StopReason.ERROR
        if tool in MUTATING_TOOLS and current_head is not None and current_head != baseline.head_commit:
            status = RunStatus.ERROR
            commit_warning = "Mutating tool changed git history or references, which Dobby does not allow"
            warnings.append(commit_warning)
            error_reasons.append(commit_warning)
            if stop_reason is None:
                stop_reason = StopReason.SANDBOX_VIOLATION
        if tool in READ_ONLY_TOOLS and spec.gitignore_updated:
            warnings.append("Wrapper updated .gitignore to add .codex-dobby/ before running")
        if stop_reason is None:
            # Defaults applied last: any ERROR path that didn't pick a more
            # specific reason (e.g. review orchestration warnings flipping
            # status) lands on ERROR. Successful runs land on END_TURN unless
            # the worker explicitly set ``refused: true``.
            if status == RunStatus.ERROR:
                stop_reason = StopReason.ERROR
            elif worker_result is not None and worker_result.refused:
                stop_reason = StopReason.REFUSAL
            else:
                stop_reason = StopReason.END_TURN

        summary = self._resolve_summary(
            worker_result,
            stdout,
            stderr,
            exit_code,
            timeout_hit,
            worker_result_error,
            error_reasons,
            stall_hit=stall_hit,
        )
        completeness = worker_result.completeness if worker_result else Completeness.BLOCKED
        if status == RunStatus.ERROR:
            completeness = Completeness.BLOCKED
        elif timeout_hit and completeness == Completeness.FULL:
            completeness = Completeness.PARTIAL
        elif recoverable_orchestration_only and completeness == Completeness.FULL:
            completeness = Completeness.PARTIAL
        elif (
            (nonzero_with_usable_review or recovered_review_from_rollouts)
            and completeness == Completeness.FULL
        ):
            completeness = Completeness.PARTIAL
        elif post_run_snapshot_incomplete and completeness == Completeness.FULL:
            completeness = Completeness.PARTIAL
        important_facts = worker_result.important_facts if worker_result else []
        next_steps = worker_result.next_steps if worker_result else []

        # Diff reconstruction shells out to ``git show`` and reads files
        # off disk, so move it to a thread for the same reason as the
        # baseline content capture above.
        file_diffs = await asyncio.to_thread(
            _file_diffs_for_run,
            tool=tool,
            repo_root=spec.repo_root,
            files_changed=files_changed,
            baseline_head=baseline.head_commit,
            baseline_dirty_contents=baseline_dirty_contents,
            worker_result=worker_result,
        )

        response = ToolResponse(
            task_id=spec.artifacts.run_dir.name,
            tool=tool,
            status=status,
            summary=summary,
            completeness=completeness,
            important_facts=important_facts,
            next_steps=next_steps,
            files_changed=files_changed,
            file_diffs=file_diffs,
            artifact_paths=spec.artifacts.as_public_dict(),
            sandbox_violations=sandbox_violations,
            repo_root=str(spec.repo_root),
            exit_code=exit_code,
            duration_ms=duration_ms,
            warnings=warnings,
            raw_output_available=True,
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
            stop_reason=stop_reason,
            review_details=_review_details_for_spec(
                spec,
                worker_result=worker_result,
                diagnostics=review_execution_diagnostics,
            ),
            reverse_engineer_details=_reverse_engineer_details_for_run(spec, stdout=stdout, stderr=stderr),
        )
        write_json(spec.artifacts.result_json, response.model_dump(mode="json", by_alias=True))
        return response

    def _write_failure_artifact(
        self,
        spec: ResolvedInvocation,
        started: float,
        issue: str,
        cleanup_warnings: list[str],
    ) -> None:
        """Persist the real cause of an abnormal exit over the aborted stub."""
        failed_response = self._preflight_response(
            spec, started, issue, as_sandbox_violation=False
        )
        if cleanup_warnings:
            failed_response = failed_response.model_copy(
                update={
                    "warnings": _merge_preserving_order(
                        failed_response.warnings, cleanup_warnings
                    )
                }
            )
        write_json(
            spec.artifacts.result_json,
            failed_response.model_dump(mode="json", by_alias=True),
        )

    @staticmethod
    def _preflight_response(
        spec: ResolvedInvocation,
        started: float,
        issue: str,
        *,
        as_sandbox_violation: bool = True,
    ) -> ToolResponse:
        return ToolResponse(
            task_id=spec.artifacts.run_dir.name,
            tool=spec.tool,
            status=RunStatus.ERROR,
            summary=issue,
            completeness=Completeness.BLOCKED,
            important_facts=[],
            next_steps=[],
            files_changed=[],
            artifact_paths=spec.artifacts.as_public_dict(),
            # Preflight failures are sandbox/permission problems. An arbitrary
            # internal exception is not, and recording it here would make
            # sandbox_violations meaningless to anyone acting on it.
            sandbox_violations=[issue] if as_sandbox_violation else [],
            repo_root=str(spec.repo_root),
            exit_code=None,
            duration_ms=int((time.monotonic() - started) * 1000),
            warnings=[issue],
            raw_output_available=False,
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
            stop_reason=StopReason.ERROR,
            review_details=_review_details_for_spec(spec),
            reverse_engineer_details=_reverse_engineer_failure_details(spec, prelaunch_failure=True),
        )

    @staticmethod
    def _write_aborted_stub(spec: ResolvedInvocation) -> None:
        stub_summary = (
            "Run did not complete. This placeholder indicates Dobby was cancelled "
            "or killed before the worker returned a result. The normal completion "
            "path would have overwritten this file."
        )
        stub = ToolResponse(
            task_id=spec.artifacts.run_dir.name,
            tool=spec.tool,
            status=RunStatus.ERROR,
            summary=stub_summary,
            completeness=Completeness.BLOCKED,
            important_facts=[],
            next_steps=[],
            files_changed=[],
            artifact_paths=spec.artifacts.as_public_dict(),
            sandbox_violations=[],
            repo_root=str(spec.repo_root),
            exit_code=None,
            duration_ms=None,
            warnings=[stub_summary],
            raw_output_available=False,
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
            result_state=ResultArtifactState.PLACEHOLDER,
            stop_reason=StopReason.CANCELLED,
            review_details=_review_details_for_spec(spec),
            reverse_engineer_details=_reverse_engineer_failure_details(spec),
        )
        write_json(spec.artifacts.result_json, stub.model_dump(mode="json", by_alias=True))

    @staticmethod
    def _timeout_response(spec: ResolvedInvocation, started: float, timeout_seconds: int) -> ToolResponse:
        warning = _timeout_warning(spec, fallback_timeout_seconds=timeout_seconds)
        response = ToolResponse(
            task_id=spec.artifacts.run_dir.name,
            tool=spec.tool,
            status=RunStatus.ERROR,
            summary=warning,
            completeness=Completeness.BLOCKED,
            important_facts=[],
            next_steps=[],
            files_changed=[],
            artifact_paths=spec.artifacts.as_public_dict(),
            sandbox_violations=[],
            repo_root=str(spec.repo_root),
            exit_code=None,
            duration_ms=int((time.monotonic() - started) * 1000),
            warnings=[warning],
            raw_output_available=False,
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
            stop_reason=StopReason.TIMEOUT,
            review_details=_review_details_for_spec(spec),
            reverse_engineer_details=_reverse_engineer_failure_details(spec),
        )
        write_json(spec.artifacts.result_json, response.model_dump(mode="json", by_alias=True))
        return response

    def _resolve(self, tool: ToolName, request: InvocationRequest) -> ResolvedInvocation:
        if tool != ToolName.REVIEW and request.agents:
            raise ValueError("agents is only supported when tool=review")

        requested_timeout_seconds = request.timeout_seconds
        requested_review_agents = list(request.agents) if tool == ToolName.REVIEW else []
        if request.repo_root is None:
            prompt_text = "\n".join(
                part for part in (request.prompt, request.important_context or "") if part
            )
            hinted_repo_roots = [
                repo
                for repo in prompt_git_worktrees(prompt_text)
                if repo != self.spawn_root
            ]
            if hinted_repo_roots:
                hinted_roots_text = ", ".join(str(path) for path in hinted_repo_roots)
                raise PathResolutionError(
                    "Request references external git worktree(s) "
                    f"{hinted_roots_text} but repo_root was not provided; "
                    f"refusing to default to server cwd {self.spawn_root}. "
                    "Pass repo_root explicitly."
                )

            relative_candidates: list[str] = []
            seen_relative: set[str] = set()
            for candidate in list(request.files) + prompt_referenced_relative_paths(prompt_text):
                token = candidate.strip()
                if not token or token.startswith("/") or token.startswith("~"):
                    continue
                if token in seen_relative:
                    continue
                seen_relative.add(token)
                relative_candidates.append(token)
            if relative_candidates:
                missing = [
                    token
                    for token in relative_candidates
                    if not (self.spawn_root / token).exists()
                ]
                if missing and len(missing) == len(relative_candidates):
                    missing_text = ", ".join(missing[:5])
                    suffix = "" if len(missing) <= 5 else f" (+{len(missing) - 5} more)"
                    raise PathResolutionError(
                        "Request references relative file(s) "
                        f"{missing_text}{suffix} that do not exist under server cwd "
                        f"{self.spawn_root} and repo_root was not provided; "
                        "refusing to default to the wrong repo. "
                        "Pass repo_root explicitly."
                    )
        repo_root = resolve_repo_root(self.spawn_root, request.repo_root)
        extra_roots = resolve_extra_roots(repo_root, request.extra_roots)
        gitignore_updated = False
        if tool in MUTATING_TOOLS:
            gitignore_updated = ensure_codex_dobby_ignored(repo_root)
        artifacts = create_run_artifacts(repo_root)
        sandbox_roots = [repo_root]
        writable_roots = [repo_root]
        advisory_read_only_roots: list[Path] = []
        # Review deliberately uses an empty MCP allowlist. Besides reducing
        # startup/context overhead, this makes recursive Dobby prevention
        # independent of server names, wrapper commands, or inherited env.
        fetchaller_available = tool != ToolName.REVIEW and mcp_server_is_enabled(
            "fetchaller",
            repo_root=repo_root,
        )
        ghidra_available = tool != ToolName.REVIEW and mcp_server_is_enabled(
            "ghidra",
            repo_root=repo_root,
        )

        if tool == ToolName.REVERSE_ENGINEER:
            for root in reverse_engineer_default_writable_roots(repo_root=repo_root):
                if root not in sandbox_roots:
                    sandbox_roots.append(root)
                if root not in writable_roots:
                    writable_roots.append(root)
            advisory_read_only_roots.extend(reverse_engineer_default_readonly_roots())

        for root in extra_roots:
            if tool in READ_ONLY_TOOLS:
                try:
                    root.relative_to(repo_root)
                except ValueError:
                    if root not in advisory_read_only_roots:
                        advisory_read_only_roots.append(root)
                    continue
                if root not in sandbox_roots:
                    sandbox_roots.append(root)
                continue
            if root not in sandbox_roots:
                sandbox_roots.append(root)
            if root not in writable_roots:
                writable_roots.append(root)

        if tool == ToolName.REVIEW and not review_uses_orchestrator(request.agents):
            model = request.model or REVIEW_SUBAGENT_DEFAULT_MODEL
            reasoning_effort = request.reasoning_effort or REVIEW_SUBAGENT_DEFAULT_REASONING_EFFORT
        elif tool == ToolName.REVIEW and review_uses_orchestrator(request.agents):
            model = request.model or DEFAULT_MODEL
            # The parent only launches, waits, and emits a manifest. Reviewer
            # reasoning is configured independently in _review_agent_overrides.
            reasoning_effort = ReasoningEffort.LOW
        else:
            model = request.model or DEFAULT_MODEL
            reasoning_effort = request.reasoning_effort or DEFAULT_REASONING_EFFORTS[tool]

        return ResolvedInvocation(
            tool=tool,
            request=request,
            requested_timeout_seconds=requested_timeout_seconds,
            requested_review_agents=requested_review_agents,
            repo_root=repo_root,
            model=model,
            reasoning_effort=reasoning_effort,
            sandbox_roots=sandbox_roots,
            writable_roots=writable_roots,
            advisory_read_only_roots=advisory_read_only_roots,
            fetchaller_available=fetchaller_available,
            ghidra_available=ghidra_available,
            artifacts=artifacts,
            gitignore_updated=gitignore_updated,
        )

    def _persist_request(self, spec: ResolvedInvocation) -> None:
        payload = {
            "task_id": spec.artifacts.run_dir.name,
            "tool": spec.tool.value,
            "request": {
                **spec.request.model_dump(mode="json"),
                "timeout_seconds": spec.requested_timeout_seconds,
            },
            "resolved": {
                "repo_root": str(spec.repo_root),
                "model": spec.model,
                "reasoning_effort": spec.reasoning_effort.value,
                "effective_timeout_seconds": spec.request.timeout_seconds,
                "requested_review_agents": [agent.value for agent in spec.requested_review_agents],
                "effective_review_agents": [agent.value for agent in selected_review_agents(spec.request.agents)],
                "sandbox_roots": [str(path) for path in spec.sandbox_roots],
                "writable_roots": [str(path) for path in spec.writable_roots],
                "advisory_read_only_roots": [str(path) for path in spec.advisory_read_only_roots],
                "fetchaller_available": spec.fetchaller_available,
                "ghidra_available": spec.ghidra_available,
            },
        }
        write_json(spec.artifacts.request_json, payload)

    def _load_worker_result(self, artifacts: RunArtifacts, *, allow_missing: bool) -> WorkerResult | None:
        if not artifacts.last_message_txt.exists():
            if allow_missing:
                return None
            raise RunnerError("Codex completed without writing the last message artifact")

        raw_last_message = artifacts.last_message_txt.read_text(encoding="utf-8").strip()
        if not raw_last_message:
            if allow_missing:
                return None
            raise RunnerError("Codex completed with an empty last message artifact")

        try:
            json.loads(raw_last_message)
        except json.JSONDecodeError as exc:
            if allow_missing:
                return None
            raise RunnerError("Codex completed with non-JSON structured output") from exc

        worker_result = _strict_worker_result(raw_last_message)
        if worker_result is None:
            if allow_missing:
                return None
            raise RunnerError("Codex completed with invalid structured output")
        return worker_result

    @staticmethod
    def _resolve_summary(
        worker_result: WorkerResult | None,
        stdout: str,
        stderr: str,
        exit_code: int | None,
        timeout_hit: bool,
        worker_result_error: str | None,
        error_reasons: list[str],
        stall_hit: bool = False,
    ) -> str:
        # A genuine top-level codex failure (a ``{"type":"error"}`` /
        # ``{"type":"turn.failed"}`` event on stdout) is the ROOT cause and
        # must not be buried behind a derived diagnostic such as the review
        # orchestrator's "completed 0/N subagents" summary — that message
        # merely describes the symptom (no subagents ran) and sends callers
        # chasing the wrong problem. Surface the real codex error first on a
        # hard failure. Stalls and timeouts are excluded: they carry their
        # own accurate, more specific reasons and (for a stall) never emit a
        # turn-level error anyway.
        codex_error = _first_codex_stdout_error(stdout)
        if codex_error and not timeout_hit and not stall_hit and exit_code not in (0, None):
            return _augment_codex_error(codex_error)
        if error_reasons:
            return error_reasons[0]
        if worker_result and worker_result.summary.strip():
            return worker_result.summary.strip()
        # Codex emits API failures (e.g. invalid response_format) as
        # ``{"type":"error",...}`` / ``{"type":"turn.failed",...}`` events on
        # stdout. Surface them before falling back to the (often noisy)
        # stderr trace, otherwise the user sees the TRACE preamble instead
        # of the actual model API error.
        if codex_error:
            return _augment_codex_error(codex_error)
        if timeout_hit:
            return "Codex run timed out before returning structured output"
        if worker_result_error:
            return worker_result_error
        for source in (stderr, stdout):
            first_line = _first_meaningful_output_line(source)
            if first_line:
                return first_line
        if exit_code is not None:
            return f"Codex exited with status {exit_code}"
        return "Codex run failed before producing output"


def _review_details_for_spec(
    spec: ResolvedInvocation,
    *,
    worker_result: WorkerResult | None = None,
    diagnostics: ReviewExecutionDiagnostics | None = None,
) -> ReviewDetails | None:
    if spec.tool != ToolName.REVIEW:
        return None
    effective_agents = selected_review_agents(spec.request.agents)
    orchestrated = review_uses_orchestrator(spec.request.agents)
    execution_manifest = (
        worker_result.review_execution
        if orchestrated and worker_result is not None
        else None
    )
    if orchestrated:
        execution_evidence = (
            diagnostics.execution_evidence
            if diagnostics is not None
            else ReviewExecutionEvidence.PARENT_REPORTED_STRUCTURED_OUTPUT
            if execution_manifest is not None
            else None
        )
    else:
        execution_evidence = (
            ReviewExecutionEvidence.DIRECT_PARENT
            if worker_result is not None
            else None
        )
    coverage_complete = (
        diagnostics.all_completed if diagnostics is not None else False
    ) if orchestrated else None
    completed_agents = (
        diagnostics.completed_count if diagnostics is not None else 0
    ) if orchestrated else None
    partial_agents = (
        diagnostics.partial_count if diagnostics is not None else 0
    ) if orchestrated else None
    blocked_agents = (
        diagnostics.blocked_count if diagnostics is not None else 0
    ) if orchestrated else None
    failed_agents = (
        diagnostics.failed_count if diagnostics is not None else 0
    ) if orchestrated else None
    return ReviewDetails(
        requested_review_agents=list(spec.requested_review_agents),
        effective_review_agents=effective_agents,
        orchestration_protocol=(
            ReviewOrchestrationProtocol.MULTI_AGENT_V2 if orchestrated else None
        ),
        execution_evidence=execution_evidence,
        execution_manifest=execution_manifest,
        manifest_validated=(
            diagnostics.manifest_validated if diagnostics is not None else False
        ),
        coverage_complete=coverage_complete,
        completed_agents=completed_agents,
        partial_agents=partial_agents,
        blocked_agents=blocked_agents,
        failed_agents=failed_agents,
    )


def _reverse_engineer_details_for_run(
    spec: ResolvedInvocation,
    *,
    stdout: str,
    stderr: str,
) -> ReverseEngineerDetails | None:
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

    combined = "\n".join(part for part in (stderr, stdout) if part)
    mcp_calls = _ordered_unique_regex_matches(_GHIDRA_MCP_CALL_RE, combined)
    helper_calls = _ordered_unique_regex_matches(_GHIDRA_HELPER_CALL_RE, combined)
    mode = _ghidra_usage_mode(mcp_calls, helper_calls)
    return ReverseEngineerDetails(
        ghidra=GhidraDetails(
            configured=True,
            mode=mode,
            summary=_ghidra_usage_summary(mode),
            mcp_calls=mcp_calls,
            helper_calls=helper_calls,
        )
    )


def _reverse_engineer_failure_details(
    spec: ResolvedInvocation,
    *,
    prelaunch_failure: bool = False,
) -> ReverseEngineerDetails | None:
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
    mode = GhidraUsageMode.PRELAUNCH_FAILURE if prelaunch_failure else GhidraUsageMode.NO_ACTIVITY
    return ReverseEngineerDetails(
        ghidra=GhidraDetails(
            configured=True,
            mode=mode,
            summary=_ghidra_usage_summary(mode),
        )
    )


def _ghidra_usage_mode(mcp_calls: list[str], helper_calls: list[str]) -> GhidraUsageMode:
    if helper_calls:
        return GhidraUsageMode.HELPER_FALLBACK
    if any(call not in _GHIDRA_STARTUP_ONLY_CALLS for call in mcp_calls):
        return GhidraUsageMode.DIRECT_MCP
    if mcp_calls:
        return GhidraUsageMode.STARTUP_ONLY
    return GhidraUsageMode.NO_ACTIVITY


def _ghidra_usage_summary(mode: GhidraUsageMode) -> str:
    if mode == GhidraUsageMode.NOT_CONFIGURED:
        return "Ghidra integration was not configured for this run."
    if mode == GhidraUsageMode.PRELAUNCH_FAILURE:
        return "Ghidra was configured, but Dobby failed before any child Ghidra activity could run."
    if mode == GhidraUsageMode.NO_ACTIVITY:
        return "Ghidra was configured for this run, but no Ghidra activity was observed."
    if mode == GhidraUsageMode.STARTUP_ONLY:
        return "Ghidra startup calls were observed, but no program-level Ghidra analysis call was observed."
    if mode == GhidraUsageMode.DIRECT_MCP:
        return "Program-level Ghidra calls ran directly through MCP tools."
    return "Ghidra startup used MCP, and program-level analysis used the mounted helper fallback."


def _ordered_unique_regex_matches(pattern: re.Pattern[str], text: str) -> list[str]:
    matches: list[str] = []
    for match in pattern.finditer(text):
        candidate = match.group(1)
        if candidate not in matches:
            matches.append(candidate)
    return matches


def _git_status(repo_root: Path) -> list[str]:
    import subprocess

    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ],
        capture_output=True,
        text=False,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RunnerError(f"Unable to capture git status for {repo_root}: {stderr}")

    files: list[str] = []
    entries = result.stdout.split(b"\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        if not entry:
            index += 1
            continue
        if len(entry) < 4:
            raise RunnerError(f"Unexpected git status entry for {repo_root}: {entry!r}")

        status_code = entry[:2].decode("ascii", errors="replace")
        path = entry[3:].decode("utf-8", errors="surrogateescape")

        is_rename = any(code in {"R", "C"} for code in status_code)
        # Skip ignored entries (``!!``): build artifacts and caches (.venv,
        # dist, __pycache__, .pytest_cache, ...). Snapshotting them made
        # mutating runs content-hash entire ignored trees twice per run and
        # let artifact churn leak into files_changed / file_diffs. ``git
        # status`` above no longer requests ``--ignored`` so this normally
        # never fires; it stays as a guard against inherited config that
        # re-enables ignored reporting.
        if status_code != "!!":
            files.append(path)
        index += 2 if is_rename else 1
    return files


def _git_head(repo_root: Path) -> str | None:
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    return head or None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_fingerprint(path: Path) -> str:
    if path.is_symlink():
        digest = hashlib.sha256()
        digest.update(b"symlink\0")
        digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        return digest.hexdigest()
    if path.is_file():
        return _sha256(path)
    if path.is_dir():
        digest = hashlib.sha256()
        digest.update(b"dir\0")
        for child in sorted(path.rglob("*")):
            relative = child.relative_to(path)
            digest.update(str(relative).encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            if child.is_symlink():
                digest.update(b"L\0")
                digest.update(os.readlink(child).encode("utf-8", errors="surrogateescape"))
            elif child.is_file():
                digest.update(b"F\0")
                with child.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(65536), b""):
                        digest.update(chunk)
            elif child.is_dir():
                digest.update(b"D\0")
        return digest.hexdigest()
    return _sha256(path)


def _repo_path_fingerprint(repo_root: Path, path: str) -> str | None:
    candidate = repo_root / path
    if candidate.exists() or candidate.is_symlink():
        return _path_fingerprint(candidate)
    return _MISSING_PATH_FINGERPRINT


def _repo_path_metadata_fingerprint(repo_root: Path, path: str) -> str | None:
    candidate = repo_root / path
    if not (candidate.exists() or candidate.is_symlink()):
        return _MISSING_PATH_FINGERPRINT

    stat_result = candidate.lstat()
    if candidate.is_symlink():
        kind = "symlink"
        target = os.readlink(candidate)
    elif candidate.is_dir():
        kind = "dir"
        target = ""
    elif candidate.is_file():
        kind = "file"
        target = ""
    else:
        kind = "other"
        target = ""

    return json.dumps(
        {
            "kind": kind,
            "size": stat_result.st_size,
            "mtime_ns": stat_result.st_mtime_ns,
            "mode": stat_result.st_mode,
            "target": target,
        },
        sort_keys=True,
    )


def _path_is_within_repo(path: str, repo_root: Path) -> bool:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
    else:
        resolved = (repo_root / candidate).resolve(strict=False)
    try:
        resolved.relative_to(repo_root)
    except ValueError:
        return False
    return True


_CHILD_RESULT_REQUIRED_FIELDS = frozenset(
    {
        "summary",
        "completeness",
        "important_facts",
        "next_steps",
        "files_changed",
        "warnings",
        "refused",
        "file_diffs",
        "review_execution",
    }
)
_CHILD_FILE_DIFF_REQUIRED_FIELDS = frozenset(
    {"path", "oldText", "newText", "truncated"}
)
_REVIEW_EXECUTION_REQUIRED_FIELDS = frozenset({"protocol", "agents"})
_REVIEW_EXECUTION_AGENT_REQUIRED_FIELDS = frozenset(
    {"review_agent", "task_name", "status", "summary"}
)
_SPAWN_REQUIRED_FIELDS = frozenset(
    {"message", "task_name", "agent_type", "fork_turns"}
)


def _strict_worker_result(payload: str) -> WorkerResult | None:
    """Parse external output with the exact public worker-schema contract."""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict) or set(decoded) != _CHILD_RESULT_REQUIRED_FIELDS:
        return None
    file_diffs = decoded.get("file_diffs")
    if not isinstance(file_diffs, list) or any(
        not isinstance(file_diff, dict)
        or set(file_diff) != _CHILD_FILE_DIFF_REQUIRED_FIELDS
        for file_diff in file_diffs
    ):
        return None
    review_execution = decoded.get("review_execution")
    if review_execution is not None:
        if (
            not isinstance(review_execution, dict)
            or set(review_execution) != _REVIEW_EXECUTION_REQUIRED_FIELDS
        ):
            return None
        agents = review_execution.get("agents")
        if not isinstance(agents, list) or any(
            not isinstance(agent, dict)
            or set(agent) != _REVIEW_EXECUTION_AGENT_REQUIRED_FIELDS
            for agent in agents
        ):
            return None
    try:
        result = WorkerResult.model_validate_json(payload, strict=True)
    except ValidationError:
        return None
    if result.refused and result.completeness != Completeness.BLOCKED:
        # A refusal always means the work did not happen, so the pairing is
        # real — but discarding the payload turned a worker that clearly stated
        # why it refused into a bare "invalid structured output" error with the
        # reason lost, for every tool. Normalise instead, and record the
        # deviation so it stays visible.
        result = result.model_copy(
            update={
                "completeness": Completeness.BLOCKED,
                "warnings": _merge_preserving_order(
                    result.warnings,
                    [
                        "Worker reported refused=true with completeness="
                        f"{result.completeness.value}; recorded as blocked."
                    ],
                ),
            }
        )
    return result


def _strict_child_worker_result(payload: str) -> WorkerResult | None:
    """Parse read-only child output and reject every mutation claim."""
    result = _strict_worker_result(payload)
    if result is None:
        return None
    if result.review_execution is not None:
        return None
    if result.files_changed or result.file_diffs:
        return None
    return result


def _child_reported_mutations(payload: str) -> tuple[tuple[str, ...], bool]:
    """Extract mutation claims even when the review payload is unusable.

    Review children must return empty mutation fields. Retaining claimed paths
    separately lets the outer snapshot attribute an observed write to the
    worker instead of erasing the evidence during verified reconstruction.
    """
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return (), False
    if not isinstance(decoded, dict):
        return (), False
    paths: list[str] = []
    files_changed = decoded.get("files_changed")
    if isinstance(files_changed, list):
        paths.extend(path for path in files_changed if isinstance(path, str) and path)
    file_diffs = decoded.get("file_diffs")
    has_file_diffs = isinstance(file_diffs, list) and bool(file_diffs)
    if isinstance(file_diffs, list):
        paths.extend(
            file_diff["path"]
            for file_diff in file_diffs
            if isinstance(file_diff, dict)
            and isinstance(file_diff.get("path"), str)
            and file_diff["path"]
        )
    return tuple(_merge_preserving_order([], paths)), has_file_diffs


def _spawn_contract_validation_issues(
    arguments: object,
    definition,
    expected_message: str | None,
    encrypted_function_args: object,
) -> list[str]:
    task_name = definition.codex_name
    if not isinstance(arguments, dict):
        return [f"{task_name} spawn arguments were not an object"]
    issues: list[str] = []
    if set(arguments) != _SPAWN_REQUIRED_FIELDS:
        unexpected = sorted(set(arguments) - _SPAWN_REQUIRED_FIELDS)
        missing = sorted(_SPAWN_REQUIRED_FIELDS - set(arguments))
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        issues.append(f"{task_name} spawn contract fields mismatch ({'; '.join(details)})")
    if arguments.get("agent_type") != task_name:
        issues.append(f"{task_name} spawn agent_type did not match its custom role")
    if arguments.get("fork_turns") != "none":
        issues.append(f'{task_name} spawn did not use fork_turns="none"')
    message = arguments.get("message")
    if not isinstance(message, str) or not message.strip():
        issues.append(f"{task_name} spawn message was empty")
    elif expected_message is None:
        issues.append(f"{task_name} expected canonical spawn assignment was unavailable")
    elif encrypted_function_args == ["message"] or (
        encrypted_function_args in (None, [])
        and _looks_like_opaque_encrypted_message(message)
        and _assignment_digest_from_message(expected_message) is not None
    ):
        # Codex 0.147 marks this schema field `with_encrypted()` and persists
        # only the provider ciphertext. The exact plaintext is bound later by
        # the independently correlated child's assignment-digest attestation.
        pass
    elif encrypted_function_args not in (None, []):
        issues.append(
            f"{task_name} spawn encrypted unexpected arguments instead of only message"
        )
    elif _canonical_spawn_contract(message) != _canonical_spawn_contract(expected_message):
        issues.append(f"{task_name} spawn message did not match its canonical assignment")
    return issues


def _looks_like_opaque_encrypted_message(message: str) -> bool:
    """Whether a persisted spawn message is provider ciphertext rather than plaintext.

    Codex 0.147's persisted ciphertext is URL-safe base64-like and currently
    starts with a Fernet-style `gAAAA`. Deliberately not bound to that prefix:
    the provider format may rotate while remaining an opaque single token.

    This is a shape check, not proof of encryption. Underscore-separated prose
    satisfies the same character class, so a parent could in principle present
    crafted plaintext here and skip the canonical-contract comparison below.
    Tightening it (entropy floor, required digit, character-class mix) needs a
    captured *real* ciphertext to calibrate against — the checked-in fixture is
    sanitized to a placeholder — because a threshold that rejects genuine
    ciphertext would quarantine valid reviews, which is worse than the bypass
    it closes. The load-bearing defence is instead that each child now receives
    its canonical assignment out of band, through the per-run agent definition
    Dobby writes into the child's private runtime, so a substituted spawn
    message cannot change what a reviewer reviews.
    """
    return len(message) >= 128 and re.fullmatch(r"[A-Za-z0-9_=-]+", message) is not None


def _canonical_spawn_contract(message: str) -> tuple[str, str, object] | None:
    """Return the stable, scope-bearing portion of a child assignment.

    Codex may make harmless whitespace or prose changes when it copies a long
    spawn message into a tool call. Bind validation to the fixed role/lens
    markers and the complete canonical assignment object instead of brittle
    byte equality across the explanatory requirements that follow it.
    """
    lines = message.splitlines()
    prefix = "Canonical assignment JSON: "
    if len(lines) < 3 or not lines[2].startswith(prefix):
        return None
    try:
        assignment = json.loads(lines[2][len(prefix) :])
    except json.JSONDecodeError:
        return None
    if not isinstance(assignment, dict):
        return None
    return lines[0], lines[1], assignment


def _assignment_digest_from_message(message: str) -> str | None:
    prefix = "Canonical assignment SHA-256: "
    matches = [line[len(prefix) :] for line in message.splitlines() if line.startswith(prefix)]
    if len(matches) != 1 or re.fullmatch(r"[0-9a-f]{64}", matches[0]) is None:
        return None
    return matches[0]


def _result_with_verified_assignment_attestation(
    result: WorkerResult,
    expected_digest: str | None,
) -> WorkerResult | None:
    if expected_digest is None:
        return None
    prefix = f"Dobby assignment {expected_digest}: "
    if not result.summary.startswith(prefix):
        return None
    summary = result.summary[len(prefix) :].strip()
    if not summary:
        return None
    return result.model_copy(update={"summary": summary})


def _review_rollout_evidence(
    codex_home: Path,
    agents,
    expected_spawn_messages: Mapping[str, str],
    *,
    allow_truncated_tail: bool = False,
) -> ReviewRolloutEvidence:
    definitions = selected_review_agent_definitions(agents)
    expected_tasks = {definition.codex_name: definition for definition in definitions}
    rollout_root = codex_home / "sessions"
    records: list[tuple[dict[str, object], list[dict[str, object]]]] = []
    read_issues: list[str] = []

    for rollout_path in sorted(rollout_root.glob("**/*.jsonl")):
        items: list[dict[str, object]] = []
        # Streamed rather than read whole: a multi-lens review with
        # history.persistence=save-all can write hundreds of MB of JSONL, and
        # holding the file text, its split lines, and the parsed items at once
        # tripled peak memory for no benefit. Only the parsed items are needed.
        try:
            with rollout_path.open("r", encoding="utf-8") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    # A line without a trailing newline can only be the final
                    # line, which is how a truncated rollout tail presents.
                    complete = raw_line.endswith(("\n", "\r"))
                    try:
                        item = json.loads(raw_line)
                    except json.JSONDecodeError:
                        if allow_truncated_tail and not complete:
                            continue
                        read_issues.append(
                            f"Codex rollout {rollout_path.name} has invalid JSON on line {line_number}"
                        )
                        continue
                    if isinstance(item, dict):
                        items.append(item)
        except OSError as exc:
            read_issues.append(f"could not read Codex rollout {rollout_path.name}: {exc}")
            continue
        metadata = next(
            (
                item.get("payload")
                for item in items
                if item.get("type") == "session_meta"
                and isinstance(item.get("payload"), dict)
            ),
            None,
        )
        if isinstance(metadata, dict):
            records.append((metadata, items))

    root_records = [
        record
        for record in records
        if record[0].get("originator") == "codex_exec"
        and record[0].get("thread_source") == "user"
        and record[0].get("source") == "exec"
    ]
    if len(root_records) != 1:
        issue = (
            "Codex session rollout evidence is unavailable"
            if not root_records
            else f"Codex session rollout evidence has {len(root_records)} root sessions; expected exactly 1"
        )
        return ReviewRolloutEvidence(
            collected=False,
            agents=tuple(
                ReviewRolloutAgentEvidence(
                    review_agent=definition.review_agent.value,
                    task_name=definition.codex_name,
                    agent_role=definition.codex_name,
                    spawn_attempt_count=0,
                    started_count=0,
                    child_thread_ids=(),
                    child_roles=(),
                    delivered_count=0,
                    child_result=None,
                    terminal_event_count=0,
                )
                for definition in definitions
            ),
            validation_issues=tuple([*read_issues, issue]),
        )

    root_metadata, root_items = root_records[0]
    root_thread_id = root_metadata.get("id")
    if not isinstance(root_thread_id, str) or not root_thread_id:
        read_issues.append("Codex root rollout omitted its thread id")
        root_thread_id = ""
    root_multi_agent_versions: list[object] = []
    for item in root_items:
        payload = item.get("payload")
        if item.get("type") == "turn_context" and isinstance(payload, dict):
            root_multi_agent_versions.append(payload.get("multi_agent_version"))
    if not root_multi_agent_versions or any(
        version != "v2" for version in root_multi_agent_versions
    ):
        rendered_versions = ", ".join(
            sorted(
                "<missing>" if version is None else str(version)
                for version in root_multi_agent_versions
            )
        ) or "<none>"
        read_issues.append(
            "Codex root rollout did not prove multi-agent v2 execution; "
            f"observed turn-context versions: {rendered_versions}"
        )

    spawn_attempts: list[str] = []
    spawn_call_count = 0
    spawn_contract_issues: dict[str, list[str]] = {}
    attestation_issues: dict[str, list[str]] = {}
    started_paths: list[str] = []
    started_thread_ids: dict[str, list[str | None]] = {}
    collaboration_calls: dict[str, str] = {}
    delivered_payloads: dict[str, set[str]] = {}
    delivered_results: dict[str, dict[str, WorkerResult]] = {}
    delivered_mutation_paths: dict[str, set[str]] = {}
    delivered_file_diffs: set[str] = set()

    def remember_delivered_result(agent_path: str, delivered_payload: str) -> None:
        delivered_payloads.setdefault(agent_path, set()).add(delivered_payload)
        mutation_paths, has_file_diffs = _child_reported_mutations(delivered_payload)
        delivered_mutation_paths.setdefault(agent_path, set()).update(mutation_paths)
        if has_file_diffs:
            delivered_file_diffs.add(agent_path)
        delivered_result = _strict_child_worker_result(delivered_payload)
        if delivered_result is None:
            return
        delivered_results.setdefault(agent_path, {})[
            delivered_payload
        ] = delivered_result

    for item in root_items:
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        if item.get("type") == "response_item" and payload.get("type") == "function_call":
            call_id = payload.get("call_id")
            call_name = payload.get("name")
            if isinstance(call_id, str) and isinstance(call_name, str):
                collaboration_calls[call_id] = call_name
            if payload.get("name") != "spawn_agent":
                continue
            spawn_call_count += 1
            raw_arguments = payload.get("arguments")
            if not isinstance(raw_arguments, str):
                read_issues.append("v2 spawn attempt omitted string arguments")
                continue
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                read_issues.append("v2 spawn attempt contained malformed JSON arguments")
                continue
            task_name = arguments.get("task_name") if isinstance(arguments, dict) else None
            if isinstance(task_name, str) and task_name:
                spawn_attempts.append(task_name)
                definition = expected_tasks.get(task_name)
                if definition is not None:
                    spawn_contract_issues.setdefault(task_name, []).extend(
                        _spawn_contract_validation_issues(
                            arguments,
                            definition,
                            expected_spawn_messages.get(task_name),
                            payload.get("encrypted_function_args"),
                        )
                    )
            else:
                read_issues.append("v2 spawn attempt omitted a valid task_name")
            continue
        if item.get("type") == "response_item" and payload.get("type") == "function_call_output":
            call_id = payload.get("call_id")
            raw_output = payload.get("output")
            if (
                not isinstance(call_id, str)
                or collaboration_calls.get(call_id) != "list_agents"
                or not isinstance(raw_output, str)
            ):
                continue
            try:
                list_output = json.loads(raw_output)
            except json.JSONDecodeError:
                continue
            listed_agents = list_output.get("agents") if isinstance(list_output, dict) else None
            if not isinstance(listed_agents, list):
                continue
            for listed_agent in listed_agents:
                if not isinstance(listed_agent, dict):
                    continue
                agent_path = listed_agent.get("agent_name")
                agent_status = listed_agent.get("agent_status")
                completed_payload = (
                    agent_status.get("completed")
                    if isinstance(agent_status, dict)
                    else None
                )
                if isinstance(agent_path, str) and isinstance(completed_payload, str):
                    remember_delivered_result(agent_path, completed_payload)
            continue
        if item.get("type") == "event_msg" and payload.get("type") == "sub_agent_activity":
            agent_path = payload.get("agent_path")
            if payload.get("kind") == "started" and isinstance(agent_path, str):
                started_paths.append(agent_path)
                agent_thread_id = payload.get("agent_thread_id")
                started_thread_ids.setdefault(agent_path, []).append(
                    agent_thread_id if isinstance(agent_thread_id, str) else None
                )
            continue
        if item.get("type") == "response_item" and payload.get("type") == "agent_message":
            author = payload.get("author")
            recipient = payload.get("recipient")
            content = payload.get("content")
            if not isinstance(author, str) or recipient != "/root" or not isinstance(content, list):
                continue
            text_parts = [
                part.get("text")
                for part in content
                if isinstance(part, dict)
                and part.get("type") == "input_text"
                and isinstance(part.get("text"), str)
            ]
            delivered_text = "".join(text_parts)
            if not delivered_text.startswith("Message Type: FINAL_ANSWER\n"):
                continue
            marker = "\nPayload:\n"
            if marker in delivered_text:
                remember_delivered_result(author, delivered_text.split(marker, 1)[1])

    child_records: dict[str, list[tuple[dict[str, object], list[dict[str, object]]]]] = {}
    unexpected_child_paths: list[str] = []
    for metadata, items in records:
        source = metadata.get("source")
        if not isinstance(source, dict):
            continue
        subagent = source.get("subagent")
        thread_spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
        if not isinstance(thread_spawn, dict):
            continue
        agent_path = thread_spawn.get("agent_path")
        if not isinstance(agent_path, str):
            continue
        if thread_spawn.get("parent_thread_id") != root_thread_id:
            unexpected_child_paths.append(agent_path)
            continue
        child_records.setdefault(agent_path, []).append((metadata, items))

    expected_paths = {f"/root/{task_name}" for task_name in expected_tasks}
    unexpected_attempts = sorted(set(spawn_attempts) - set(expected_tasks))
    unexpected_started = sorted(set(started_paths) - expected_paths)
    unexpected_children = sorted((set(child_records) - expected_paths) | set(unexpected_child_paths))
    if unexpected_attempts:
        read_issues.append("unexpected v2 spawn attempts: " + ", ".join(unexpected_attempts))
    if spawn_call_count > len(definitions):
        # Only an *excess* is a global integrity problem: it means the parent
        # ran something beyond the selected lenses. A shortfall removes a lens
        # and nothing else, so it is reported per-lens as coverage loss by
        # _rollout_validation_issues rather than quarantining every lens that
        # did deliver. Duplicate attempts for one lens are caught there too.
        read_issues.append(
            f"v2 root emitted {spawn_call_count} spawn attempts; expected at most {len(definitions)}"
        )
    if unexpected_started:
        read_issues.append("unexpected v2 started tasks: " + ", ".join(unexpected_started))
    if unexpected_children:
        read_issues.append("unexpected or nested v2 child sessions: " + ", ".join(unexpected_children))

    evidence_agents: list[ReviewRolloutAgentEvidence] = []
    for definition in definitions:
        task_name = definition.codex_name
        agent_path = f"/root/{task_name}"
        matching_children = child_records.get(agent_path, [])
        child_thread_ids: list[str] = []
        child_roles: list[str | None] = []
        child_multi_agent_versions: list[str | None] = []
        child_results: list[WorkerResult] = []
        terminal_event_count = 0
        reported_mutation_paths: set[str] = set(
            delivered_mutation_paths.get(agent_path, set())
        )
        reported_file_diffs = agent_path in delivered_file_diffs
        for metadata, items in matching_children:
            thread_id = metadata.get("id")
            if isinstance(thread_id, str) and thread_id:
                child_thread_ids.append(thread_id)
            source = metadata.get("source")
            subagent = source.get("subagent") if isinstance(source, dict) else None
            thread_spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
            agent_role = thread_spawn.get("agent_role") if isinstance(thread_spawn, dict) else None
            child_roles.append(agent_role if isinstance(agent_role, str) else None)
            multi_agent_version = metadata.get("multi_agent_version")
            child_multi_agent_versions.append(
                multi_agent_version if isinstance(multi_agent_version, str) else None
            )
            for item in items:
                payload = item.get("payload")
                if (
                    item.get("type") == "event_msg"
                    and isinstance(payload, dict)
                    and payload.get("type") == "task_complete"
                    and isinstance(payload.get("last_agent_message"), str)
                ):
                    terminal_event_count += 1
                    mutation_paths, has_file_diffs = _child_reported_mutations(
                        payload["last_agent_message"]
                    )
                    reported_mutation_paths.update(mutation_paths)
                    reported_file_diffs = reported_file_diffs or has_file_diffs
                    child_result = _strict_child_worker_result(
                        payload["last_agent_message"]
                    )
                    if child_result is not None:
                        child_results.append(child_result)

        delivered_child_results = list(delivered_results.get(agent_path, {}).values())
        final_delivery_count = len(delivered_payloads.get(agent_path, set()))
        matched_child_result = (
            child_results[0]
            if len(child_results) == 1
            and final_delivery_count == 1
            and len(delivered_child_results) == 1
            and child_results[0] == delivered_child_results[0]
            else None
        )
        expected_digest = _assignment_digest_from_message(
            expected_spawn_messages.get(task_name, "")
        )
        if matched_child_result is not None:
            attested_child_result = _result_with_verified_assignment_attestation(
                matched_child_result,
                expected_digest,
            )
            if attested_child_result is None:
                attestation_issues.setdefault(task_name, []).append(
                    f"{task_name} child result did not attest its canonical assignment"
                )
            matched_child_result = attested_child_result
        evidence_agents.append(
            ReviewRolloutAgentEvidence(
                review_agent=definition.review_agent.value,
                task_name=task_name,
                agent_role=task_name,
                spawn_attempt_count=spawn_attempts.count(task_name),
                started_count=started_paths.count(agent_path),
                child_thread_ids=tuple(child_thread_ids),
                child_roles=tuple(child_roles),
                delivered_count=final_delivery_count,
                child_result=matched_child_result,
                spawn_contract_issues=tuple(
                    spawn_contract_issues.get(task_name, [])
                ),
                attestation_issues=tuple(attestation_issues.get(task_name, [])),
                child_multi_agent_versions=tuple(child_multi_agent_versions),
                started_thread_ids=tuple(started_thread_ids.get(agent_path, [])),
                terminal_event_count=terminal_event_count,
                reported_mutation_paths=tuple(sorted(reported_mutation_paths)),
                reported_file_diffs=reported_file_diffs,
            )
        )

    return ReviewRolloutEvidence(
        collected=True,
        agents=tuple(evidence_agents),
        validation_issues=tuple(read_issues),
    )


class RolloutIssues(NamedTuple):
    """Per-lens rollout deviations, split by what they actually prove.

    ``integrity`` issues mean the evidence chain itself cannot be trusted:
    the manifest contradicts the rollouts, or a child did something a
    read-only reviewer must never do. Those quarantine the whole review,
    because a manifest that misreports one lens cannot be believed about any
    of them.

    ``coverage`` issues mean one lens simply did not deliver usable output.
    ``observed_status`` already demotes such a lens to ``failed``, and
    ``_review_worker_result_from_rollouts`` only ever reconstructs findings
    from ``completed``/``partial`` lenses, so a non-delivering lens cannot
    inject anything into the caller-visible response. Failing the entire run
    over one would discard the independently verified findings the other
    lenses did deliver, which is precisely what partial coverage exists to
    avoid.
    """

    integrity: list[str]
    coverage: list[str]


def _rollout_validation_issues(
    evidence: ReviewRolloutAgentEvidence,
    reported_status: ReviewAgentExecutionStatus,
) -> RolloutIssues:
    integrity: list[str] = []
    coverage: list[str] = []

    # Duplicates mean something ran that Dobby never asked for, so they stay
    # integrity failures. Zero of anything just means this lens produced
    # nothing, which the coverage summary below reports.
    if evidence.spawn_attempt_count > 1:
        integrity.append(
            f"{evidence.task_name} has {evidence.spawn_attempt_count} rollout spawn attempts; expected 1"
        )
    # How the lens was spawned is an isolation guarantee, not coverage: a
    # wrong agent_type, a fork_turns other than "none", or an unexpected
    # encrypted argument shape means a child ran that Dobby did not ask for,
    # which README and AGENTS.md both promise is cross-checked before the
    # manifest is accepted.
    integrity.extend(evidence.spawn_contract_issues)
    # A payload that does not attest its canonical assignment is discarded by
    # ``_result_with_verified_assignment_attestation``, so an unattested child
    # can only cost itself its own slot. Report it, do not quarantine on it.
    coverage.extend(evidence.attestation_issues)
    if len(evidence.child_thread_ids) > 1:
        integrity.append(
            f"{evidence.task_name} has {len(evidence.child_thread_ids)} child sessions; expected at most 1"
        )
    if evidence.started_count > 1:
        integrity.append(
            f"{evidence.task_name} has {evidence.started_count} started events; expected at most 1"
        )
    if (
        evidence.started_thread_ids
        and evidence.started_thread_ids != evidence.child_thread_ids
    ):
        integrity.append(
            f"{evidence.task_name} started-event thread ids do not match its child session"
        )
    if evidence.terminal_event_count > 1:
        integrity.append(
            f"{evidence.task_name} has {evidence.terminal_event_count} terminal events; expected at most 1"
        )
    if evidence.delivered_count > 1:
        integrity.append(
            f"{evidence.task_name} has {evidence.delivered_count} delivered results; expected at most 1"
        )
    if evidence.reported_mutation_paths:
        integrity.append(
            f"{evidence.task_name} review child reported file changes: "
            + ", ".join(evidence.reported_mutation_paths)
        )
    if evidence.reported_file_diffs:
        integrity.append(f"{evidence.task_name} review child returned non-empty file_diffs")
    if evidence.child_roles and evidence.child_roles != (evidence.agent_role,):
        rendered_roles = ", ".join(role or "<missing>" for role in evidence.child_roles)
        integrity.append(
            f"{evidence.task_name} child role mismatch: expected {evidence.agent_role}; "
            f"observed {rendered_roles}"
        )
    if (
        evidence.child_multi_agent_versions
        and evidence.child_multi_agent_versions != ("disabled",)
    ):
        rendered_versions = ", ".join(
            version or "<missing>" for version in evidence.child_multi_agent_versions
        )
        integrity.append(
            f"{evidence.task_name} child multi-agent isolation mismatch: expected disabled; "
            f"observed {rendered_versions}"
        )

    observed_status = evidence.observed_status
    missing_or_mismatched: list[str] = []
    if evidence.spawn_attempt_count != 1:
        missing_or_mismatched.append(f"spawn_attempts={evidence.spawn_attempt_count}")
    if evidence.started_count != 1:
        missing_or_mismatched.append(f"started_events={evidence.started_count}")
    if len(evidence.child_thread_ids) != 1:
        missing_or_mismatched.append(f"child_sessions={len(evidence.child_thread_ids)}")
    if evidence.started_thread_ids != evidence.child_thread_ids:
        missing_or_mismatched.append("started_child_thread_match=false")
    if evidence.child_roles != (evidence.agent_role,):
        missing_or_mismatched.append("expected_child_role=false")
    if evidence.child_multi_agent_versions != ("disabled",):
        missing_or_mismatched.append("child_multi_agent_disabled=false")
    if evidence.delivered_count != 1:
        missing_or_mismatched.append(f"final_deliveries={evidence.delivered_count}")
    if evidence.terminal_event_count != 1:
        missing_or_mismatched.append(f"terminal_events={evidence.terminal_event_count}")
    if evidence.child_result is None:
        missing_or_mismatched.append("matching_terminal_result=false")
    detail = (
        " (" + ", ".join(missing_or_mismatched) + ")" if missing_or_mismatched else ""
    )
    if reported_status != observed_status:
        integrity.append(
            f"{evidence.task_name} reported {reported_status.value} but Codex rollout evidence shows "
            f"{observed_status.value}{detail}"
        )
    elif observed_status == ReviewAgentExecutionStatus.FAILED:
        # The manifest and the rollouts agree that this lens failed. That is an
        # honest report of lost coverage, not a broken evidence chain.
        coverage.append(
            f"{evidence.task_name} delivered no usable review result{detail}"
        )
    return RolloutIssues(integrity=integrity, coverage=coverage)


def _verified_agent_summary(
    agent: ReviewRolloutAgentEvidence,
    fallback: str,
) -> str:
    """Return a per-agent manifest summary that never carries failed prose.

    ``child_result`` can be populated for a lens whose evidence still failed —
    a spawn that used the wrong role or fork policy, for instance. Keying the
    summary on the payload's mere existence let that child's own words reach
    the caller through ``review_details`` even though its findings were
    excluded everywhere else.
    """
    if (
        agent.child_result is None
        or agent.observed_status == ReviewAgentExecutionStatus.FAILED
    ):
        return fallback
    return agent.child_result.summary


def _review_worker_result_from_rollouts(
    evidence: ReviewRolloutEvidence,
    *,
    parent_result_missing: bool,
) -> WorkerResult | None:
    if not evidence.collected or evidence.validation_issues:
        return None
    usable_agents = [
        agent
        for agent in evidence.agents
        if agent.observed_status
        in {ReviewAgentExecutionStatus.COMPLETED, ReviewAgentExecutionStatus.PARTIAL}
        and agent.child_result is not None
    ]
    if not usable_agents:
        return None

    important_facts: list[str] = []
    next_steps: list[str] = []
    warnings = (
        [
            "Review parent did not return final structured output; Dobby recovered independently "
            "delivered child results from Codex session rollouts."
        ]
        if parent_result_missing
        else []
    )
    for agent in usable_agents:
        assert agent.child_result is not None
        important_facts = _merge_preserving_order(
            important_facts,
            agent.child_result.important_facts,
        )
        next_steps = _merge_preserving_order(next_steps, agent.child_result.next_steps)
        warnings = _merge_preserving_order(warnings, agent.child_result.warnings)

    statuses = [agent.observed_status for agent in evidence.agents]
    completeness = (
        Completeness.FULL
        if all(status == ReviewAgentExecutionStatus.COMPLETED for status in statuses)
        else Completeness.PARTIAL
    )
    summary = (
        "Review parent did not return final JSON; recovered independently verified findings "
        if parent_result_missing
        else "Multi-agent review returned independently verified findings "
    )
    return WorkerResult(
        summary=summary
        + f"from {len(usable_agents)}/{len(evidence.agents)} delivered v2 child results.",
        completeness=completeness,
        important_facts=important_facts,
        next_steps=next_steps,
        files_changed=[],
        warnings=warnings,
        refused=False,
        file_diffs=[],
        review_execution={
            "protocol": ReviewOrchestrationProtocol.MULTI_AGENT_V2,
            "agents": [
                {
                    "review_agent": agent.review_agent,
                    "task_name": agent.task_name,
                    "status": agent.observed_status,
                    "summary": _verified_agent_summary(
                        agent,
                        "No usable delivered child result was recorded in Codex session rollouts.",
                    ),
                }
                for agent in evidence.agents
            ],
        },
    )


def _salvaged_review_worker_result_from_rollouts(
    evidence: ReviewRolloutEvidence,
) -> WorkerResult | None:
    return _review_worker_result_from_rollouts(
        evidence,
        parent_result_missing=True,
    )


def _verified_review_worker_result_from_rollouts(
    evidence: ReviewRolloutEvidence,
) -> WorkerResult:
    if not evidence.collected or evidence.validation_issues:
        return _quarantined_review_worker_result()
    result = _review_worker_result_from_rollouts(
        evidence,
        parent_result_missing=False,
    )
    if result is not None:
        return result

    # A validated manifest can contain only blocked/failed children. Preserve
    # the exact observed execution state without accepting parent-authored
    # prose as findings.
    return WorkerResult(
        summary="Multi-agent review produced no independently verified usable child results.",
        completeness=Completeness.BLOCKED,
        important_facts=[],
        next_steps=[],
        files_changed=[],
        warnings=_merge_preserving_order(
            [],
            [
                warning
                for agent in evidence.agents
                if agent.child_result is not None
                and agent.observed_status != ReviewAgentExecutionStatus.FAILED
                for warning in agent.child_result.warnings
            ],
        ),
        refused=False,
        file_diffs=[],
        review_execution={
            "protocol": ReviewOrchestrationProtocol.MULTI_AGENT_V2,
            "agents": [
                {
                    "review_agent": agent.review_agent,
                    "task_name": agent.task_name,
                    "status": agent.observed_status,
                    "summary": _verified_agent_summary(
                        agent,
                        "No valid delivered child result was recorded in Codex session rollouts.",
                    ),
                }
                for agent in evidence.agents
            ],
        },
    )


def _quarantined_review_worker_result() -> WorkerResult:
    return WorkerResult(
        summary="Multi-agent review evidence failed validation; no findings were returned.",
        completeness=Completeness.BLOCKED,
        important_facts=[],
        next_steps=[],
        files_changed=[],
        warnings=[],
        refused=False,
        file_diffs=[],
        review_execution=None,
    )


def _review_execution_diagnostics(
    worker_result: WorkerResult | None,
    agents,
    rollout_evidence: ReviewRolloutEvidence | None = None,
) -> ReviewExecutionDiagnostics:
    definitions = selected_review_agent_definitions(agents)
    expected_by_agent = {
        definition.review_agent.value: definition.codex_name
        for definition in definitions
    }
    manifest = worker_result.review_execution if worker_result is not None else None
    if manifest is None:
        return ReviewExecutionDiagnostics(
            expected_agents=tuple(expected_by_agent),
            manifest=None,
            execution_evidence=(
                ReviewExecutionEvidence.CODEX_SESSION_ROLLOUTS
                if rollout_evidence is not None and rollout_evidence.collected
                else None
            ),
            validation_issues=("worker result omitted the required review_execution manifest",),
            completed_count=0,
            partial_count=0,
            blocked_count=0,
            failed_count=0,
        )

    issues: list[str] = []
    coverage_issues: list[str] = []
    if manifest.protocol != ReviewOrchestrationProtocol.MULTI_AGENT_V2:
        issues.append("review_execution.protocol was not multi_agent_v2")

    reported_agent_order = [entry.review_agent.value for entry in manifest.agents]
    expected_agent_order = list(expected_by_agent)
    if reported_agent_order != expected_agent_order:
        issues.append(
            "review agent order mismatch: expected "
            + ", ".join(expected_agent_order)
            + "; reported "
            + ", ".join(reported_agent_order)
        )

    entries_by_agent: dict[str, list] = {}
    task_counts: dict[str, int] = {}
    for entry in manifest.agents:
        entries_by_agent.setdefault(entry.review_agent.value, []).append(entry)
        task_counts[entry.task_name] = task_counts.get(entry.task_name, 0) + 1

    missing_agents = [agent for agent in expected_by_agent if agent not in entries_by_agent]
    duplicate_agents = [agent for agent, entries in entries_by_agent.items() if len(entries) > 1]
    unexpected_agents = [agent for agent in entries_by_agent if agent not in expected_by_agent]
    duplicate_tasks = [task for task, count in task_counts.items() if count > 1]
    mismatched_tasks = [
        f"{agent} expected {expected_by_agent[agent]} but reported {entries[0].task_name}"
        for agent, entries in entries_by_agent.items()
        if agent in expected_by_agent
        and len(entries) == 1
        and entries[0].task_name != expected_by_agent[agent]
    ]

    if missing_agents:
        issues.append("missing selected review agents: " + ", ".join(missing_agents))
    if duplicate_agents:
        issues.append("duplicate review agent entries: " + ", ".join(duplicate_agents))
    if unexpected_agents:
        issues.append("unexpected review agent entries: " + ", ".join(unexpected_agents))
    if duplicate_tasks:
        issues.append("duplicate task names: " + ", ".join(duplicate_tasks))
    if mismatched_tasks:
        issues.append("mismatched task names: " + "; ".join(mismatched_tasks))

    if rollout_evidence is None:
        issues.append("Codex session rollout evidence was not collected")
    else:
        issues.extend(rollout_evidence.validation_issues)
        rollout_by_task = rollout_evidence.by_task_name
        for entry in manifest.agents:
            expected_task = expected_by_agent.get(entry.review_agent.value)
            if expected_task is None or entry.task_name != expected_task:
                continue
            evidence = rollout_by_task.get(entry.task_name)
            if evidence is None:
                issues.append(f"Codex session rollouts omitted {entry.task_name}")
                continue
            lens_issues = _rollout_validation_issues(evidence, entry.status)
            issues.extend(lens_issues.integrity)
            coverage_issues.extend(lens_issues.coverage)

    completed_count = sum(
        entry.status == ReviewAgentExecutionStatus.COMPLETED
        for entry in manifest.agents
        if entry.review_agent.value in expected_by_agent
        and entry.task_name == expected_by_agent[entry.review_agent.value]
    )
    partial_count = sum(
        entry.status == ReviewAgentExecutionStatus.PARTIAL
        for entry in manifest.agents
        if entry.review_agent.value in expected_by_agent
        and entry.task_name == expected_by_agent[entry.review_agent.value]
    )
    blocked_count = sum(
        entry.status == ReviewAgentExecutionStatus.BLOCKED
        for entry in manifest.agents
        if entry.review_agent.value in expected_by_agent
        and entry.task_name == expected_by_agent[entry.review_agent.value]
    )
    failed_count = sum(
        entry.status == ReviewAgentExecutionStatus.FAILED
        for entry in manifest.agents
        if entry.review_agent.value in expected_by_agent
        and entry.task_name == expected_by_agent[entry.review_agent.value]
    )
    if not issues:
        if completed_count == len(expected_by_agent):
            expected_completeness = Completeness.FULL
        elif completed_count + partial_count > 0:
            expected_completeness = Completeness.PARTIAL
        else:
            expected_completeness = Completeness.BLOCKED
        if worker_result is not None and worker_result.completeness != expected_completeness:
            issues.append(
                "top-level completeness does not match agent statuses: expected "
                f"{expected_completeness.value}; reported {worker_result.completeness.value}"
            )
    return ReviewExecutionDiagnostics(
        expected_agents=tuple(expected_by_agent),
        manifest=manifest,
        execution_evidence=(
            ReviewExecutionEvidence.CODEX_SESSION_ROLLOUTS
            if rollout_evidence is not None and rollout_evidence.collected
            else None
        ),
        validation_issues=tuple(issues),
        completed_count=completed_count,
        partial_count=partial_count,
        blocked_count=blocked_count,
        failed_count=failed_count,
        coverage_issues=tuple(coverage_issues),
    )


def _collect_sandbox_violations(stderr: str, stdout: str = "") -> list[str]:
    violations: list[str] = []
    seen: set[str] = set()
    for line in stderr.splitlines():
        violation = _sandbox_violation_from_line(line, allow_plain_text=True)
        if violation is None or violation in seen:
            continue
        seen.add(violation)
        violations.append(violation)
    for line in stdout.splitlines():
        violation = _sandbox_violation_from_line(line, allow_plain_text=False)
        if violation is None or violation in seen:
            continue
        seen.add(violation)
        violations.append(violation)
    return violations


def _sandbox_violation_from_line(line: str, *, allow_plain_text: bool) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        if not allow_plain_text or _looks_like_json_fragment(stripped):
            return None
        candidates = [stripped]
    else:
        candidates = _sandbox_violation_candidates(payload)
        if not candidates and isinstance(payload, str):
            candidates = [payload]

    for candidate in candidates:
        normalized = " ".join(candidate.split())
        if _looks_like_code_or_test_snippet(normalized):
            continue
        lower = normalized.lower()
        if _contains_word(lower, "sandbox") and (
            any(_contains_word(lower, token) for token in ("blocked", "denied", "forbidden", "disallowed"))
            or "sandbox violation" in lower
            or "sandbox not permitted" in lower
        ):
            return normalized
        if any(token in lower for token in ("permission denied", "operation not permitted", "read-only file system")):
            if any(token in lower for token in ("sandbox", "write", "writing", "creating", "opening", "mkdir", "network", "exec", "access", "socket", "/")):
                return normalized
    return None


def _codex_home_permission_issue(
    sandbox_violations: list[str],
    *,
    stderr: str = "",
    stdout: str = "",
) -> str | None:
    candidates = list(sandbox_violations)
    candidates.extend(line.strip() for line in stderr.splitlines() if line.strip())
    candidates.extend(line.strip() for line in stdout.splitlines() if line.strip())

    for candidate in candidates:
        normalized = " ".join(candidate.split())
        lower = normalized.lower()
        if not any(token in lower for token in ("permission denied", "operation not permitted", "read-only file system")):
            continue
        if "codex cannot access" not in lower and "session files" not in lower and "codex_home" not in lower:
            continue
        path = _extract_access_path(normalized) or "~/.codex"
        subject = "its session files" if "session files" in lower else "its Codex state directory"
        return _codex_home_access_message(path, subject=subject, verb="could not access")
    return None


def _codex_home_access_message(path: str, *, subject: str, verb: str) -> str:
    return (
        f"Codex CLI {verb} {subject} at {path}. "
        "Dobby seeds a private per-run Codex home for child runs, so the server process needs read access "
        "to the parent Codex auth/config files and read/write access to the private runtime home it creates "
        "under the system temp directory."
    )


def _extract_access_path(text: str) -> str | None:
    match = re.search(r"\bat ((?:/|~)[^\s)]*)", text)
    if match is None:
        match = re.search(r"((?:/|~)[^\s)]*(?:sessions|auth\.json|config\.toml)[^\s)]*)", text)
    if match is None:
        return None
    return match.group(1).rstrip(".,")


def _sandbox_violation_candidates(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, dict):
        return []

    flattened: list[str] = []
    for key in ("message", "error", "stderr", "stdout", "output", "reason", "summary"):
        if key in value:
            flattened.extend(_string_values(value[key]))

    item = value.get("item")
    if isinstance(item, dict):
        for key in ("message", "error", "stderr", "stdout", "output", "reason"):
            if key in item:
                flattened.extend(_string_values(item[key]))
    return flattened


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        flattened: list[str] = []
        for nested in value.values():
            flattened.extend(_string_values(nested))
        return flattened
    if isinstance(value, list):
        flattened = []
        for nested in value:
            flattened.extend(_string_values(nested))
        return flattened
    return []


def _contains_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


def _looks_like_json_fragment(line: str) -> bool:
    if line in {"{", "}", "[", "]", "},", "],"}:
        return True
    if line.startswith('"') and ":" not in line:
        return True
    return False


def _looks_like_code_or_test_snippet(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False

    stripped = re.sub(r"^[^:\s][^:]*:\d+:\s+", "", stripped, count=1)
    stripped = re.sub(r"^\d+(?::|\s+)", "", stripped, count=1).lstrip()
    if re.match(r"^[\"'].*[\"'],?$", stripped):
        return True

    code_prefixes = (
        "assert ",
        "return ",
        "def ",
        "class ",
        "if ",
        "elif ",
        "else:",
        "for ",
        "while ",
        "with ",
        "try:",
        "except ",
        "raise ",
        "from ",
        "import ",
        "or ",
        "and ",
    )
    if stripped.startswith(code_prefixes):
        return True

    if "->" in stripped or "==" in stripped or "!=" in stripped:
        return True

    if stripped.startswith(("(", "[", "{")) and stripped.endswith((")", "]", "}")):
        return True

    return False


def _first_meaningful_output_line(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return stripped
        if isinstance(payload, dict) and payload.get("type"):
            # Codex --json wraps API errors in events. Surface them so the
            # final ToolResponse summary doesn't bury the actual reason in
            # the events.jsonl artifact while showing only stderr trace
            # noise to the caller.
            payload_type = payload.get("type")
            if payload_type in ("error", "turn.failed"):
                extracted = _extract_codex_error_message(payload)
                if extracted:
                    return extracted
            continue
        return stripped
    return None


# Substrings that identify a codex authentication/token failure in a
# top-level error message. Codex surfaces these when the saved login can no
# longer be used or refreshed (expired access token, rotated/"reused" refresh
# token, 401 from the ChatGPT backend or the built-in ``codex_apps``
# connector). None of these are recoverable by retrying — the user must
# re-authenticate — so we attach an explicit, actionable hint.
_AUTH_FAILURE_SIGNATURES = (
    "refresh token",
    "refresh_token_reused",
    "access token",
    "authentication token",
    "token_expired",
    "token expired",
    "sign in again",
    "signing in again",
    "sign back in",
    "log back in",
    "not authenticated",
    "401 unauthorized",
)

_CODEX_AUTH_HINT = (
    "Codex authentication has expired and cannot be refreshed automatically — "
    "run `codex logout && codex login` to re-authenticate, then retry."
)


def _augment_codex_error(message: str) -> str:
    """Append an actionable re-login hint when a codex error is an auth failure.

    ``codex login status`` reports "Logged in" even with a dead token, and a
    spent refresh token cannot self-heal, so without this hint callers retry a
    permanently failing run instead of re-authenticating.
    """
    lowered = message.lower()
    if any(signature in lowered for signature in _AUTH_FAILURE_SIGNATURES):
        return f"{message} [Dobby] {_CODEX_AUTH_HINT}"
    return message


def _extract_codex_error_message(payload: dict) -> str | None:
    candidate = payload.get("message")
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    error_obj = payload.get("error")
    if isinstance(error_obj, dict):
        for key in ("message", "summary"):
            value = error_obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    elif isinstance(error_obj, str) and error_obj.strip():
        return error_obj.strip()
    return None


def _first_codex_stdout_error(stdout: str) -> str | None:
    """Find the first codex top-level error event in JSONL stdout."""
    if not stdout:
        return None
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("type") in ("error", "turn.failed"):
            extracted = _extract_codex_error_message(payload)
            if extracted:
                return extracted
    return None


def _timeout_warning(spec: ResolvedInvocation, fallback_timeout_seconds: int | None = None) -> str:
    effective_timeout = spec.request.timeout_seconds if spec.request.timeout_seconds else fallback_timeout_seconds
    if effective_timeout is None:
        return "Codex run timed out"
    return f"Codex run timed out after {effective_timeout} seconds"


def _stall_threshold_for_effort(effort: ReasoningEffort | None) -> float:
    if effort in (ReasoningEffort.HIGH, ReasoningEffort.XHIGH):
        return _CODEX_STALL_THRESHOLD_HIGH_EFFORT_SECONDS
    return _CODEX_STALL_THRESHOLD_SECONDS


def _stall_diagnostics(stderr: str, stdout: str, threshold_seconds: float) -> str:
    stderr_bytes = len(stderr.encode("utf-8")) if stderr else 0
    stdout_bytes = len(stdout.encode("utf-8")) if stdout else 0
    last_line = _last_non_trace_line(stderr)
    last_line_ts = _last_trace_timestamp(stderr)
    parts = [
        f"Codex produced no stdout or stderr activity for {int(threshold_seconds)} seconds and was killed as stalled.",
        f"Totals at kill: stderr {stderr_bytes} bytes, stdout {stdout_bytes} bytes.",
    ]
    if last_line_ts:
        parts.append(f"Last trace timestamp: {last_line_ts}.")
    if last_line:
        snippet = last_line.strip()
        if len(snippet) > _SALVAGE_LINE_MAX_CHARS:
            snippet = snippet[:_SALVAGE_LINE_MAX_CHARS].rstrip() + "…"
        parts.append(f"Last stderr non-trace line: {snippet!r}.")
    if stderr_bytes == 0:
        parts.append("No SSE activity was observed — the request likely stalled before the model opened its response stream.")
    return " ".join(parts)


def _last_non_trace_line(stderr: str) -> str | None:
    if not stderr:
        return None
    for line in reversed(stderr.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if "codex_api::" in stripped or " TRACE " in stripped or " DEBUG " in stripped:
            continue
        return stripped
    return None


_TRACE_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)")


def _last_trace_timestamp(stderr: str) -> str | None:
    if not stderr:
        return None
    for line in reversed(stderr.splitlines()):
        match = _TRACE_TS_RE.match(line)
        if match:
            return match.group(1)
    return None


def _count_in_progress_events(stderr: str) -> int:
    if not stderr:
        return 0
    return stderr.count("response.in_progress")


def _salvage_exec_trace(stderr: str) -> list[dict[str, str]]:
    """Extract the `exec <cmd>\\n<detail>\\n succeeded in ...` blocks codex exec emits for tool calls.

    Each entry is {cmd, outcome} where outcome is "succeeded"/"failed"/"in-flight".
    """
    if not stderr:
        return []
    lines = stderr.splitlines()
    results: list[dict[str, str]] = []
    n = len(lines)
    i = 0
    while i < n:
        if lines[i].rstrip() == "exec":
            cmd_line = lines[i + 1].strip() if i + 1 < n else ""
            outcome = "in-flight"
            j = i + 2
            scan_limit = min(n, j + 200)
            while j < scan_limit:
                stripped = lines[j].lstrip()
                if stripped.startswith("succeeded in"):
                    outcome = "succeeded"
                    break
                if stripped.startswith("failed") or stripped.startswith("exited "):
                    outcome = "failed"
                    break
                if lines[j].rstrip() in ("exec", "codex", "tokens used"):
                    break
                j += 1
            if cmd_line:
                if len(cmd_line) > _SALVAGE_LINE_MAX_CHARS:
                    cmd_line = cmd_line[:_SALVAGE_LINE_MAX_CHARS].rstrip() + "…"
                results.append({"cmd": cmd_line, "outcome": outcome})
            i = j + 1 if j > i + 1 else i + 1
            continue
        i += 1
    return results


def _salvage_worker_result_from_trace(
    tool: ToolName,
    stderr: str,
    *,
    stall_hit: bool,
    timeout_hit: bool,
) -> WorkerResult | None:
    """Build a PARTIAL WorkerResult from the stderr trace when codex was killed without emitting its final JSON.

    Intended for non-review tools where the existing review-salvage path does not apply.
    """
    if tool in (ToolName.REVIEW,):  # review has its own salvage
        return None
    if not (stall_hit or timeout_hit):
        return None

    exec_blocks = _salvage_exec_trace(stderr)
    turn_count = _count_in_progress_events(stderr)

    if not exec_blocks and turn_count == 0:
        return None

    tail = exec_blocks[-_SALVAGE_EXEC_TAIL_COUNT:]
    succeeded = sum(1 for e in exec_blocks if e["outcome"] == "succeeded")
    failed = sum(1 for e in exec_blocks if e["outcome"] == "failed")
    in_flight = sum(1 for e in exec_blocks if e["outcome"] == "in-flight")

    facts: list[str] = [
        f"Salvaged partial progress from stderr trace: {turn_count} model turn(s) observed, "
        f"{len(exec_blocks)} shell command(s) attempted ({succeeded} succeeded, {failed} failed, "
        f"{in_flight} in-flight when killed).",
    ]
    if tail:
        facts.append("Most recent shell commands before kill:")
        for entry in tail:
            facts.append(f"  [{entry['outcome']}] {entry['cmd']}")

    cause = "stalled" if stall_hit else "timed out"
    summary = (
        f"Codex {cause} before writing its final JSON; wrapper salvaged {turn_count} turn(s) "
        f"and {len(exec_blocks)} tool call(s) from the stderr trace."
    )
    warnings = [
        "Result was salvaged from the stderr trace after codex was killed; no structured worker JSON was produced."
    ]
    return WorkerResult(
        summary=summary,
        completeness=Completeness.PARTIAL,
        important_facts=facts,
        next_steps=[],
        files_changed=[],
        warnings=warnings,
    )


def _write_salvage_last_message(artifacts: RunArtifacts, worker_result: WorkerResult) -> None:
    path = artifacts.last_message_txt
    if path.exists() and path.read_bytes().strip():
        return
    try:
        payload = worker_result.model_dump(mode="json", by_alias=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        return


def _seconds_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise asyncio.TimeoutError
    return remaining


def _snapshot_uses_metadata(tool: ToolName) -> bool:
    return tool in READ_ONLY_TOOLS


def _build_repo_snapshot(
    repo_root: Path,
    *,
    include_head: bool,
    use_metadata_fingerprints: bool,
) -> RepoSnapshot:
    status = _git_status(repo_root)
    dirty_files = [
        public_file_label(repo_root / path, repo_root)
        for path in status
        if not _is_wrapper_managed(path)
    ]
    fingerprint_fn = _repo_path_metadata_fingerprint if use_metadata_fingerprints else _repo_path_fingerprint
    path_fingerprints = {
        path: fingerprint_fn(repo_root, path)
        for path in dirty_files
    }
    return RepoSnapshot(
        head_commit=_git_head(repo_root) if include_head else None,
        status_entries=dirty_files,
        dirty_files=dirty_files,
        path_fingerprints=path_fingerprints,
    )


async def _capture_repo_snapshot_with_deadline(
    repo_root: Path,
    deadline: float,
    *,
    include_head: bool,
    use_metadata_fingerprints: bool,
) -> RepoSnapshot:
    argv = [
        sys.executable,
        "-m",
        "codex_dobby_mcp.snapshot_worker",
        "--repo-root",
        str(repo_root),
    ]
    if include_head:
        argv.append("--include-head")
    if use_metadata_fingerprints:
        argv.append("--use-metadata-fingerprints")

    process = await _create_process_with_deadline(
        argv,
        repo_root,
        os.environ.copy(),
        deadline,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=_seconds_remaining(deadline),
        )
    except asyncio.TimeoutError:
        await _terminate_process(process, timeout=_POST_TIMEOUT_TERMINATE_GRACE_SECONDS)
        raise

    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        detail = _first_meaningful_output_line(stderr_text) or _first_meaningful_output_line(stdout_text) or (
            f"Snapshot helper exited with status {process.returncode}"
        )
        raise RunnerError(f"Unable to capture repo snapshot for {repo_root}: {detail}")
    try:
        payload = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise RunnerError(f"Snapshot helper returned invalid JSON for {repo_root}") from exc
    try:
        return RepoSnapshot.model_validate(payload)
    except ValidationError as exc:
        raise RunnerError(f"Snapshot helper returned an invalid repo snapshot for {repo_root}") from exc


async def _ensure_multi_agent_v2_available(
    codex_binary: str,
    repo_root: Path,
    child_env: dict[str, str],
    deadline: float,
) -> None:
    probe_deadline = min(deadline, time.monotonic() + 10.0)
    try:
        process = await _create_process_with_deadline(
            [codex_binary, "features", "list"],
            repo_root,
            child_env,
            probe_deadline,
        )
    except FileNotFoundError as exc:
        raise RunnerError(f"Codex executable not found: {codex_binary}") from exc
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=_seconds_remaining(probe_deadline),
        )
    except asyncio.TimeoutError as exc:
        await _terminate_process(
            process,
            timeout=_POST_TIMEOUT_TERMINATE_GRACE_SECONDS,
        )
        raise RunnerError(
            "Could not verify Codex multi-agent v2 support within 10 seconds"
        ) from exc

    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    if process.returncode == 0 and re.search(
        r"^multi_agent_v2\s+",
        stdout_text,
        re.MULTILINE,
    ):
        return

    detail = (
        _first_meaningful_output_line(stderr_text)
        or _first_meaningful_output_line(stdout_text)
        or f"Codex exited with status {process.returncode}"
    )
    raise RunnerError(
        "Multi-agent review requires a Codex CLI with the `multi_agent_v2` feature "
        f"(verified with codex-cli 0.147.0). Installed CLI probe failed: {detail}"
    )


async def _create_process_with_deadline(
    argv: list[str],
    repo_root: Path,
    child_env: dict[str, str],
    deadline: float,
):
    process_task = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(repo_root),
            env=child_env,
        )
    )
    try:
        return await asyncio.wait_for(asyncio.shield(process_task), timeout=_seconds_remaining(deadline))
    except asyncio.TimeoutError:
        await _cleanup_timed_out_process_start(process_task)
        raise
    except asyncio.CancelledError:
        await _cleanup_timed_out_process_start(process_task)
        raise


async def _cleanup_timed_out_process_start(process_task: asyncio.Task):
    if not process_task.done():
        process_task.cancel()
        with suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(process_task, timeout=0.1)
    if process_task.done() and not process_task.cancelled():
        process = process_task.result()
        await _terminate_process(process, timeout=_POST_TIMEOUT_TERMINATE_GRACE_SECONDS)


async def _terminate_process(process, timeout: float | None = None) -> None:  # type: ignore[no-untyped-def]
    if getattr(process, "returncode", None) is not None:
        return
    terminate = getattr(process, "terminate", None)
    if callable(terminate):
        with suppress(ProcessLookupError):
            terminate()
    else:
        with suppress(ProcessLookupError):
            process.kill()
    wait = getattr(process, "wait", None)
    if not callable(wait):
        return
    try:
        if timeout is None:
            with suppress(ProcessLookupError):
                await wait()
        else:
            with suppress(ProcessLookupError):
                await asyncio.wait_for(wait(), timeout=timeout)
    except asyncio.TimeoutError:
        if getattr(process, "returncode", None) is not None:
            return
        with suppress(ProcessLookupError):
            process.kill()
        with suppress(ProcessLookupError, asyncio.TimeoutError):
            await asyncio.wait_for(wait(), timeout=_POST_TIMEOUT_KILL_WAIT_SECONDS)
        return


async def _shielded_terminate_process(process, timeout: float | None = None) -> None:  # type: ignore[no-untyped-def]
    """Terminate a live process so the SIGTERM→SIGKILL escalation always finishes.

    On client cancellation (Esc in Claude Code) the ambient cancel scope can be
    *level-triggered* (anyio): every ``await`` in the unwind path re-raises
    ``CancelledError``, which would abort ``_terminate_process`` mid-escalation
    and orphan a codex that ignored SIGTERM — it keeps running, burning credits
    and possibly still editing the repo. Run the escalation as a shielded task
    and keep re-awaiting it through re-delivered cancels until it actually
    reaps the process. Any pending cancellation still propagates once this
    returns (we never swallow the caller's exception, only our own awaits).
    """
    if getattr(process, "returncode", None) is not None:
        return
    task = asyncio.ensure_future(_terminate_process(process, timeout=timeout))
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Re-delivered by a level-triggered cancel scope. The shielded task
            # is unaffected and keeps running; loop until it has reaped.
            continue


def _prepare_child_runtime(
    artifacts: RunArtifacts,
    env: Mapping[str, str] | None = None,
) -> ChildRuntimeContext:
    current_env = env or os.environ
    source_codex_home = Path(current_env.get("CODEX_HOME", "~/.codex")).expanduser()
    source_claude_config = Path(current_env.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    if source_codex_home.exists() and (
        not source_codex_home.is_dir() or not os.access(source_codex_home, os.R_OK | os.X_OK)
    ):
        raise RunnerError(
            _codex_home_access_message(
                str(source_codex_home),
                subject="the parent Codex home directory",
                verb="cannot read",
            )
        )
    private_root: Path | None = None
    scratch_root: Path | None = None
    try:
        scratch_root = _child_scratch_root(artifacts)
        private_root = private_runtime_root(artifacts.run_dir.name)
        codex_home = _ensure_artifact_subdirectory(private_root / "codex-home", "child Codex home directory")
        _ensure_artifact_subdirectory(codex_home / "sessions", "child Codex sessions directory")
        claude_config_dir = _ensure_artifact_subdirectory(private_root / "claude-config", "child Claude config directory")
        home_config_path = codex_home / "config.toml"
        _copy_codex_home_seed_file(
            source_codex_home / "auth.json",
            codex_home / "auth.json",
            subject="the parent Codex auth file",
        )
        _seed_child_codex_config(
            source_codex_home / "config.toml",
            home_config_path,
            source_codex_home=source_codex_home,
            source_claude_config=source_claude_config,
            codex_home=codex_home,
            claude_config_dir=claude_config_dir,
        )
        return ChildRuntimeContext(
            private_root=private_root,
            codex_home=codex_home,
            claude_config_dir=claude_config_dir,
            home_config_path=home_config_path,
            env_overrides=_child_runtime_environment_overrides(
                scratch_root,
                codex_home=codex_home,
                claude_config_dir=claude_config_dir,
            ),
            scratch_root=scratch_root,
        )
    except RunnerError as exc:
        # The run never starts, so nothing will consume the scratch tree.
        if scratch_root is not None:
            _cleanup_child_scratch_root(scratch_root)
        if private_root is not None:
            cleanup_issue = _cleanup_private_child_runtime(private_root)
            if cleanup_issue is not None:
                raise RunnerError(f"{exc}. {cleanup_issue}") from exc
        raise
    except Exception as exc:
        cleanup_issue = None
        if scratch_root is not None:
            _cleanup_child_scratch_root(scratch_root)
        if private_root is not None:
            cleanup_issue = _cleanup_private_child_runtime(private_root)
        target = private_root / "codex-home" if private_root is not None else Path(tempfile.gettempdir()) / "codex-dobby"
        access_message = _codex_home_access_message(
            str(target),
            subject="its private runtime home",
            verb="cannot create",
        )
        if cleanup_issue is not None:
            access_message = f"{access_message}. {cleanup_issue}"
        raise RunnerError(access_message) from exc


def _rewrite_path_prefix(text: str, prefix: str, replacement: str) -> str:
    r"""Replace a path reference in ``text`` only when it ends at a path boundary.

    A bare ``str.replace("~/.codex", ...)`` (or the absolute equivalent) also
    rewrites unrelated sibling names such as ``~/.codex-backup`` →
    ``<codex_home>-backup``, corrupting the mirrored child config. Anchor the
    match so the next character is end-of-string or not a path-name character
    (``[\w-]``). A function replacement keeps characters in ``replacement`` from
    being interpreted as regex backreferences.
    """
    pattern = re.escape(prefix) + r"(?![\w-])"
    return re.sub(pattern, lambda _match: replacement, text)


def _write_private_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` with owner-only (0600) permissions.

    The mirrored child ``config.toml`` can embed MCP server env blocks holding
    API keys. A plain ``write_text`` creates it 0644 under the default umask,
    leaving secrets readable by other local users; force 0600 at creation.
    """
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text)


def _copy_codex_home_seed_file(source: Path, destination: Path, *, subject: str) -> None:
    if not source.exists():
        return
    if not source.is_file() or not os.access(source, os.R_OK):
        raise RunnerError(_codex_home_access_message(str(source), subject=subject, verb="cannot read"))
    try:
        shutil.copy2(source, destination)
    except OSError as exc:
        raise RunnerError(
            _codex_home_access_message(
                str(destination),
                subject=f"the private runtime copy of {subject.removeprefix('the ')}",
                verb="cannot write",
            )
        ) from exc


def _seed_child_codex_config(
    source: Path,
    destination: Path,
    *,
    source_codex_home: Path,
    source_claude_config: Path,
    codex_home: Path,
    claude_config_dir: Path,
) -> None:
    if not source.exists():
        return
    if not source.is_file() or not os.access(source, os.R_OK):
        raise RunnerError(
            _codex_home_access_message(str(source), subject="the parent Codex config file", verb="cannot read")
        )

    try:
        payload = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise RunnerError(
            _codex_home_access_message(str(source), subject="the parent Codex config file", verb="cannot read")
        ) from exc

    source_codex_home = _absolute_path(source_codex_home)
    source_claude_config = _absolute_path(source_claude_config)

    try:
        _mirror_runtime_config_references(payload, source_root=source_codex_home, target_root=codex_home)
        _mirror_runtime_config_references(payload, source_root=source_claude_config, target_root=claude_config_dir)
    except OSError as exc:
        failing_target = getattr(exc, "filename", None) or str(destination)
        raise RunnerError(
            _codex_home_access_message(
                str(failing_target),
                subject="the private runtime copy of the parent Codex config file",
                verb="cannot write",
            )
        ) from exc

    rewritten = _rewrite_path_prefix(payload, str(source_codex_home), str(codex_home))
    rewritten = _rewrite_path_prefix(rewritten, str(source_claude_config), str(claude_config_dir))
    if _same_path(source_codex_home, _GLOBAL_CODEX_DIR):
        rewritten = _rewrite_path_prefix(rewritten, "~/.codex", str(codex_home))
    if _same_path(source_claude_config, _GLOBAL_CLAUDE_DIR):
        rewritten = _rewrite_path_prefix(rewritten, "~/.claude", str(claude_config_dir))

    try:
        _write_private_text(destination, rewritten)
    except OSError as exc:
        raise RunnerError(
            _codex_home_access_message(
                str(destination),
                subject="the private runtime copy of the parent Codex config file",
                verb="cannot write",
            )
        ) from exc


def _mirror_runtime_config_references(payload: str, *, source_root: Path, target_root: Path) -> None:
    if not source_root.exists():
        return

    tilde_prefix: str | None = None
    if _same_path(source_root, _GLOBAL_CLAUDE_DIR):
        tilde_prefix = "~/.claude"
    elif _same_path(source_root, _GLOBAL_CODEX_DIR):
        tilde_prefix = "~/.codex"

    mirrored: set[Path] = set()
    for reference in _iter_config_path_references(payload, source_root=source_root, tilde_prefix=tilde_prefix):
        _mirror_runtime_path_reference(reference, source_root=source_root, target_root=target_root, mirrored=mirrored)


def _iter_config_path_references(payload: str, *, source_root: Path, tilde_prefix: str | None) -> list[Path]:
    discovered: list[Path] = []
    seen: set[Path] = set()
    prefixes = [str(source_root)]
    if tilde_prefix is not None:
        prefixes.append(tilde_prefix)

    for prefix in prefixes:
        start = 0
        while True:
            idx = payload.find(prefix, start)
            if idx == -1:
                break
            end = idx + len(prefix)
            while end < len(payload) and payload[end] not in _CONFIG_PATH_DELIMITERS:
                end += 1
            raw = payload[idx:end].rstrip(_CONFIG_PATH_TRAILING_PUNCTUATION)
            if raw:
                if tilde_prefix is not None and raw.startswith(tilde_prefix):
                    relative = raw.removeprefix(tilde_prefix).lstrip("/")
                    resolved = _absolute_path(source_root / relative)
                else:
                    resolved = _absolute_path(Path(raw))
                if resolved not in seen:
                    seen.add(resolved)
                    discovered.append(resolved)
            start = idx + len(prefix)

    return discovered


# Never mirrored out of a parent runtime home: the child gets a fresh
# sessions root, and Dobby's rollout evidence depends on it containing exactly
# one root session — this run's.
# Compared case-folded; see _is_excluded_runtime_reference.
_UNMIRRORABLE_RUNTIME_SUBTREES = frozenset({"sessions"})


def _mirror_runtime_path_reference(
    reference: Path,
    *,
    source_root: Path,
    target_root: Path,
    mirrored: set[Path],
) -> None:
    try:
        relative = reference.relative_to(source_root)
    except ValueError:
        return
    if not reference.exists():
        return
    # Every containment and exclusion decision below is made on the *resolved*
    # path. A lexical check is bypassable three ways that all end in the same
    # place — cloning credentials, caches, or the parent's rollout history into
    # the child runtime: a symlink alias to the home or to sessions/, a `..`
    # segment, or a case variant like `Sessions` on a case-insensitive
    # filesystem.
    if _is_excluded_runtime_reference(reference, source_root=source_root):
        return

    if reference.is_dir():
        source_item = reference
        target_item = target_root / relative
    elif reference.parent == source_root:
        source_item = reference
        target_item = target_root / relative
    else:
        source_item = reference.parent
        try:
            target_item = target_root / source_item.relative_to(source_root)
        except ValueError:
            return
        if _is_excluded_runtime_reference(source_item, source_root=source_root):
            return

    if source_item in mirrored:
        return
    mirrored.add(source_item)

    target_item.parent.mkdir(parents=True, exist_ok=True)
    if source_item.is_dir():
        # The ignore callback resolves each entry, so a symlink nested inside
        # an allowed directory cannot pull the home root or a sessions tree in.
        shutil.copytree(
            source_item,
            target_item,
            dirs_exist_ok=True,
            ignore=lambda directory, names: _excluded_runtime_names(
                Path(directory), names, source_root=source_root
            ),
        )
    else:
        shutil.copy2(source_item, target_item)


def _is_excluded_runtime_reference(reference: Path, *, source_root: Path) -> bool:
    """Whether a config path reference must never be mirrored.

    Decided on resolved paths so a symlink alias, a ``..`` segment, or a case
    variant cannot reach an excluded subtree. Resolving *outside* the runtime
    home is not itself excluded: a symlinked helper directory (for example
    ``~/.claude/mcp-servers`` pointing at a shared checkout) is a supported
    layout, and the caller has already required the reference to be lexically
    inside the home.
    """
    try:
        resolved = reference.resolve()
        resolved_root = source_root.resolve()
    except OSError:
        return True
    try:
        relative = resolved.relative_to(resolved_root)
    except ValueError:
        return False
    if relative == Path("."):
        # A bare reference to the runtime home itself (a plain "~/.codex"
        # mentioned anywhere in config.toml) is not a mirrorable artifact.
        # Copying it would clone the whole parent home into the child's private
        # runtime — auth.json, caches, and every historical session rollout —
        # which is both enormous and enough to break the review: extra rollouts
        # under the child's sessions root make Dobby observe more than one root
        # session and quarantine the run. The seeding path already copies the
        # specific files a child needs.
        return True
    # Case-folded: macOS filesystems are case-insensitive by default, so
    # "Sessions" and "sessions" name the same directory.
    return bool(relative.parts) and relative.parts[0].casefold() in _UNMIRRORABLE_RUNTIME_SUBTREES


def _excluded_runtime_names(
    directory: Path,
    names: list[str],
    *,
    source_root: Path,
) -> set[str]:
    return {
        name
        for name in names
        if _is_excluded_runtime_reference(directory / name, source_root=source_root)
    }


def _absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    return Path(os.path.abspath(os.fspath(expanded)))


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return _absolute_path(left) == _absolute_path(right)


def _cleanup_private_child_runtime(path: Path, logger=None) -> str | None:  # type: ignore[no-untyped-def]
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        issue = (
            "Failed to delete private runtime containing copied credentials and session data at "
            f"{path}: {exc}"
        )
        if logger is not None:
            logger.warning(issue)
        return issue
    return None


def _cleanup_child_scratch_root(path: Path, logger=None) -> str | None:  # type: ignore[no-untyped-def]
    """Remove the child's TMPDIR/cache tree from the run's artifact directory.

    Separate from ``_cleanup_private_child_runtime`` because the two have
    different consequences: the private runtime holds copied credentials, so
    failing to delete it fails the run. This tree holds only scratch data, so
    failing to delete it is reported and the result still stands.
    """
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        issue = f"Failed to delete the child runtime scratch directory at {path}: {exc}"
        if logger is not None:
            logger.warning(issue)
        return issue
    return None


def _response_with_private_runtime_cleanup_failure(
    response: ToolResponse,
    issue: str,
) -> ToolResponse:
    return response.model_copy(
        update={
            "status": RunStatus.ERROR,
            "summary": issue,
            "completeness": Completeness.BLOCKED,
            "warnings": _merge_preserving_order([issue], response.warnings),
            "stop_reason": StopReason.ERROR,
        }
    )


def _child_scratch_root(artifacts: RunArtifacts) -> Path:
    return _ensure_artifact_subdirectory(
        artifacts.run_dir / "runtime", "child runtime directory"
    )


def _child_runtime_environment_overrides(
    runtime_root: Path,
    *,
    codex_home: Path,
    claude_config_dir: Path,
) -> dict[str, str]:
    tmp_root = _ensure_artifact_subdirectory(runtime_root / "tmp", "child temp directory")
    cache_root = _ensure_artifact_subdirectory(runtime_root / "cache", "child cache directory")
    uv_cache_root = _ensure_artifact_subdirectory(cache_root / "uv", "child uv cache directory")
    xdg_cache_root = _ensure_artifact_subdirectory(cache_root / "xdg", "child xdg cache directory")

    return {
        "TMPDIR": str(tmp_root),
        "TMP": str(tmp_root),
        "TEMP": str(tmp_root),
        "CODEX_HOME": str(codex_home),
        "CLAUDE_CONFIG_DIR": str(claude_config_dir),
        "CLAUDE_CODE_DISABLE_CRON": "1",
        "UV_CACHE_DIR": str(uv_cache_root),
        "XDG_CACHE_HOME": str(xdg_cache_root),
        "PYTHONDONTWRITEBYTECODE": "1",
        # Works around Codex CLI issue #14048: response.in_progress SSE events
        # are unhandled and produce no visible stderr output during long
        # reasoning phases (especially xhigh), which looks like a hang. Forcing
        # trace-level logging on the SSE response module makes the silent
        # thinking phase emit regular stderr activity so our stall detector
        # can distinguish a real hang from an in-flight response.
        "RUST_LOG": os.environ.get("RUST_LOG") or "codex_api::sse::responses=trace",
    }


def _ensure_artifact_subdirectory(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise RunnerError(f"{label} must not be a symlink: {path}")
    if path.exists():
        if not path.is_dir():
            raise RunnerError(f"{label} is not a directory: {path}")
        return path
    # 0700: child codex-home / sessions / claude-config hold mirrored auth and
    # config that can embed secrets; the runtime cache dirs are private too.
    path.mkdir(parents=True, mode=0o700)
    return path


class _StreamActivityTracker:
    def __init__(self) -> None:
        self.last_activity = time.monotonic()

    def bump(self) -> None:
        self.last_activity = time.monotonic()


EventSink = Callable[[dict, int], Awaitable[None]]


class _EventEmitter:
    """Funnel codex stdout chunks into ACP-shaped events.

    For each chunk written to the codex stdout log, this also parses any
    complete JSONL events, maps them to ACP-shaped payloads, appends them to
    ``events.jsonl``, and forwards each one to an optional async sink (used
    for live MCP progress notifications). Sink errors are swallowed —
    streaming is best-effort and must never break the run.
    """

    def __init__(self, events_jsonl: Path, sink: EventSink | None) -> None:
        self.events_jsonl = events_jsonl
        self.sink = sink
        self._carryover = b""
        self._handle: BinaryIO | None = None
        self._count = 0

    def __enter__(self) -> "_EventEmitter":
        # Truncate / create. ``ab`` would append on retried runs, but the run
        # directory is freshly created per task_id so a fresh write is correct.
        self._handle = self.events_jsonl.open("wb")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is not None:
            try:
                self._handle.flush()
            except OSError:
                pass
            self._handle.close()
            self._handle = None

    @property
    def event_count(self) -> int:
        return self._count

    async def feed(self, chunk: bytes) -> None:
        events, self._carryover = map_codex_chunk(chunk, carryover=self._carryover)
        if not events:
            return
        for event in events:
            self._count += 1
            line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
            if self._handle is not None:
                try:
                    self._handle.write(line)
                    self._handle.flush()
                except OSError:
                    # If the log file is gone we keep the in-memory pipeline
                    # going; the durable artifact is best-effort too.
                    pass
            if self.sink is not None:
                try:
                    await self.sink(event, self._count)
                except Exception:
                    # Sink failure (e.g. client disconnected) must not abort
                    # the underlying codex run.
                    pass

    def feed_sync(self, chunk: bytes) -> list[dict]:
        """Variant for the non-streaming path: returns mapped events instead
        of awaiting the sink. Caller is responsible for forwarding them.
        """
        events, self._carryover = map_codex_chunk(chunk, carryover=self._carryover)
        if not events:
            return []
        for event in events:
            self._count += 1
            line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
            if self._handle is not None:
                try:
                    self._handle.write(line)
                    self._handle.flush()
                except OSError:
                    pass
        return events


async def _execute_process_with_streaming_logs(
    process,
    stdin_payload: bytes,
    stdout_log: Path,
    stderr_log: Path,
    timeout: float,
    stall_threshold_seconds: float | None = None,
    *,
    events_jsonl: Path | None = None,
    event_sink: EventSink | None = None,
) -> tuple[int | None, bool, bool]:
    effective_stall_threshold = (
        stall_threshold_seconds if stall_threshold_seconds is not None else _CODEX_STALL_THRESHOLD_SECONDS
    )
    emitter_cm = (
        _EventEmitter(events_jsonl, event_sink) if events_jsonl is not None else None
    )

    if not _supports_streaming_process_io(process):
        timeout_hit = False
        try:
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(stdin_payload), timeout=timeout)
            except asyncio.TimeoutError:
                timeout_hit = True
                await _terminate_process(process, timeout=_POST_TIMEOUT_TERMINATE_GRACE_SECONDS)
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        process.communicate(),
                        timeout=_POST_TIMEOUT_IO_DRAIN_SECONDS,
                    )
                except asyncio.TimeoutError:
                    stdout_bytes, stderr_bytes = b"", b""
        finally:
            # Reap on cancellation too (communicate() raises CancelledError
            # without terminating); no-op on the normal/timeout paths where
            # the child is already dead.
            if getattr(process, "returncode", None) is None:
                await _shielded_terminate_process(
                    process, timeout=_POST_TIMEOUT_TERMINATE_GRACE_SECONDS
                )
        _write_log_bytes(stdout_log, stdout_bytes)
        _write_log_bytes(stderr_log, stderr_bytes)
        if emitter_cm is not None and stdout_bytes:
            with emitter_cm as emitter:
                # Non-streaming branch: feed the whole captured stdout once.
                # Forward any events to the async sink if configured.
                events = emitter.feed_sync(stdout_bytes)
                if event_sink is not None:
                    base = emitter.event_count - len(events)
                    for offset, event in enumerate(events, start=1):
                        try:
                            await event_sink(event, base + offset)
                        except Exception:
                            pass
        return process.returncode, timeout_hit, False

    timeout_hit = False
    stall_flag: list[bool] = [False]
    tracker = _StreamActivityTracker()
    with stdout_log.open("wb") as stdout_handle, stderr_log.open("wb") as stderr_handle:
        emitter_ctx = emitter_cm.__enter__() if emitter_cm is not None else None
        try:
            stdin_task = asyncio.create_task(_write_process_stdin(process.stdin, stdin_payload))
            stdout_task = asyncio.create_task(
                _pump_process_stream(process.stdout, stdout_handle, tracker, emitter_ctx)
            )
            stderr_task = asyncio.create_task(_pump_process_stream(process.stderr, stderr_handle, tracker))
            stall_task = asyncio.create_task(
                _monitor_process_stall(process, tracker, effective_stall_threshold, stall_flag)
            )
            cleanup_timeout: float | None = None
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                timeout_hit = True
                cleanup_timeout = _POST_TIMEOUT_IO_DRAIN_SECONDS
                await _terminate_process(process, timeout=_POST_TIMEOUT_TERMINATE_GRACE_SECONDS)
            finally:
                stall_task.cancel()
                # Reap the child before draining and closing its pipes. On a
                # normal or timeout exit it is already dead; on client
                # cancellation nothing else here terminates it, so without this
                # the process is orphaned and the pump tasks leak writing to a
                # live pipe. Shielded so the kill escalation survives a
                # level-triggered cancel scope.
                if getattr(process, "returncode", None) is None:
                    await _shielded_terminate_process(
                        process, timeout=_POST_TIMEOUT_TERMINATE_GRACE_SECONDS
                    )
                if stall_flag[0] and cleanup_timeout is None:
                    cleanup_timeout = _POST_TIMEOUT_IO_DRAIN_SECONDS
                await _gather_process_io_tasks(
                    [stdin_task, stdout_task, stderr_task, stall_task],
                    timeout=cleanup_timeout,
                )
        finally:
            if emitter_cm is not None:
                emitter_cm.__exit__(None, None, None)
    return process.returncode, timeout_hit, stall_flag[0]


async def _monitor_process_stall(
    process,  # type: ignore[no-untyped-def]
    tracker: _StreamActivityTracker,
    threshold_seconds: float,
    stall_flag: list[bool],
) -> None:
    try:
        while True:
            await asyncio.sleep(_CODEX_STALL_CHECK_INTERVAL_SECONDS)
            if getattr(process, "returncode", None) is not None:
                return
            idle = time.monotonic() - tracker.last_activity
            if idle >= threshold_seconds:
                stall_flag[0] = True
                await _terminate_process(process, timeout=_POST_TIMEOUT_TERMINATE_GRACE_SECONDS)
                return
    except asyncio.CancelledError:
        return


async def _gather_process_io_tasks(tasks: list[asyncio.Task], timeout: float | None) -> None:
    if timeout is None:
        await asyncio.gather(*tasks, return_exceptions=True)
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        for task in tasks:
            task.cancel()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=0.1,
            )


async def _write_process_stdin(stdin, payload: bytes) -> None:  # type: ignore[no-untyped-def]
    if stdin is None:
        return
    try:
        stdin.write(payload)
        await stdin.drain()
        stdin.close()
        wait_closed = getattr(stdin, "wait_closed", None)
        if callable(wait_closed):
            await wait_closed()
    except (BrokenPipeError, ConnectionResetError):
        return


async def _pump_process_stream(
    stream,  # type: ignore[no-untyped-def]
    handle: BinaryIO,
    tracker: _StreamActivityTracker | None = None,
    emitter: "_EventEmitter | None" = None,
) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        handle.write(chunk)
        handle.flush()
        if tracker is not None:
            tracker.bump()
        if emitter is not None:
            await emitter.feed(chunk)


def _supports_streaming_process_io(process) -> bool:  # type: ignore[no-untyped-def]
    return all(
        hasattr(process, attribute)
        for attribute in ("stdin", "stdout", "stderr", "wait")
    )


def _write_log_bytes(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)


def _read_log_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_bytes().decode("utf-8", errors="replace")


def _is_wrapper_managed(path: str) -> bool:
    if path.startswith(".codex-dobby/"):
        return True
    return False


def _merge_preserving_order(first: list[str], second: list[str]) -> list[str]:
    combined: list[str] = []
    seen: set[str] = set()
    for item in [*first, *second]:
        if item not in seen:
            seen.add(item)
            combined.append(item)
    return combined


def _changed_status_files(before: RepoSnapshot, after: RepoSnapshot) -> list[str]:
    changed: list[str] = []
    for path in [*after.dirty_files, *before.dirty_files]:
        if path in changed:
            continue
        if before.path_fingerprints.get(path) != after.path_fingerprints.get(path):
            changed.append(path)
    return changed


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:_FILE_DIFF_BINARY_SCAN_BYTES]


def _capture_pre_run_contents(repo_root: Path, dirty_files: list[str]) -> dict[str, str | None]:
    """Read pre-run contents of files that were already dirty at baseline.

    Files that were dirty before the run started lose their pre-run state once
    codex modifies them again, so we read them off disk before launching the
    worker. Files clean at baseline keep `git show HEAD:path` as their oldText
    source. Stores ``None`` for files we cannot represent as text (binary,
    over the size cap, or unreadable).
    """
    captured: dict[str, str | None] = {}
    for relative_path in dirty_files:
        if relative_path.startswith("/"):
            # Absolute paths land here when the dirty file is outside repo_root
            # (extra writable roots). We don't capture those for diffs — the
            # worker is expected to keep mutations inside the repo.
            continue
        full_path = repo_root / relative_path
        try:
            if full_path.is_symlink() or not full_path.is_file():
                continue
            data = full_path.read_bytes()
        except OSError:
            continue
        if len(data) > _FILE_DIFF_MAX_BYTES or _looks_binary(data):
            captured[relative_path] = None
            continue
        captured[relative_path] = data.decode("utf-8", errors="replace")
    return captured


def _git_show_head_contents(repo_root: Path, head: str | None, relative_path: str) -> tuple[str | None, bool]:
    """Return (text, truncated) for the file's contents at the baseline HEAD.

    Returns ``(None, False)`` for paths not tracked at HEAD (new files), and
    ``(None, True)`` when the file is too large or binary at HEAD.
    """
    if head is None:
        return (None, False)
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"{head}:{relative_path}"],
            capture_output=True,
            check=False,
        )
    except (OSError, ValueError):
        return (None, False)
    if result.returncode != 0:
        # Untracked at HEAD (or path resolution failed). Treat as new file.
        return (None, False)
    data = result.stdout
    if len(data) > _FILE_DIFF_MAX_BYTES or _looks_binary(data):
        return (None, True)
    return (data.decode("utf-8", errors="replace"), False)


def _read_post_run_text(repo_root: Path, relative_path: str) -> tuple[str | None, bool]:
    """Return (text, truncated) for the file's contents on disk after the run.

    Returns ``(None, False)`` for files that no longer exist (deleted), and
    ``(None, True)`` for binary files or files over the size cap.
    """
    full_path = repo_root / relative_path
    try:
        if full_path.is_symlink():
            return (None, True)
        if not full_path.exists():
            return (None, False)
        if not full_path.is_file():
            return (None, True)
        data = full_path.read_bytes()
    except OSError:
        return (None, True)
    if len(data) > _FILE_DIFF_MAX_BYTES or _looks_binary(data):
        return (None, True)
    return (data.decode("utf-8", errors="replace"), False)


def _file_diffs_for_run(
    *,
    tool: ToolName,
    repo_root: Path,
    files_changed: list[str],
    baseline_head: str | None,
    baseline_dirty_contents: dict[str, str | None],
    worker_result: WorkerResult | None,
) -> list[FileDiff]:
    """Compute structured file diffs for a completed mutating run.

    For files that were dirty at baseline, ``baseline_dirty_contents`` carries
    their pre-run state. For files clean at baseline, the pre-run state is
    recovered from the baseline HEAD via ``git show``. New files have
    ``old_text=None``; deleted files have ``new_text=None``. Binary files,
    files over the size cap, and unreadable files surface as
    ``truncated=True`` with both texts ``None``.

    Read-only tools and runs with no detected changes return an empty list,
    even if the worker reported diffs (worker reports merge in only when the
    wrapper observed a real change).
    """
    if tool not in MUTATING_TOOLS or not files_changed:
        return []
    worker_diff_index: dict[str, FileDiff] = {}
    if worker_result is not None:
        for entry in worker_result.file_diffs:
            worker_diff_index.setdefault(entry.path, entry)

    diffs: list[FileDiff] = []
    for relative_path in files_changed:
        # files_changed may include absolute paths when worker mutates outside
        # the repo root via extra writable roots. Skip those — diffing
        # arbitrary external files is out of scope for the snapshot model.
        if relative_path.startswith("/"):
            continue

        if relative_path in baseline_dirty_contents:
            old_text = baseline_dirty_contents[relative_path]
            old_truncated = old_text is None
        else:
            old_text, old_truncated = _git_show_head_contents(
                repo_root, baseline_head, relative_path
            )

        new_text, new_truncated = _read_post_run_text(repo_root, relative_path)
        truncated = old_truncated or new_truncated

        # Worker-supplied diff fills gaps the wrapper couldn't capture (e.g.
        # the worker stashed pre-edit contents before applying a patch). We
        # only consult the worker when the wrapper's own value is None — but
        # we accept it regardless of the truncation flag, since a worker that
        # took a snippet before mutating the file may have text the wrapper
        # cannot recover from disk anymore.
        full_path = str((repo_root / relative_path).resolve())
        worker_entry = worker_diff_index.get(relative_path) or worker_diff_index.get(full_path)
        if worker_entry is not None:
            if old_text is None and worker_entry.old_text is not None:
                old_text = worker_entry.old_text
            if new_text is None and worker_entry.new_text is not None:
                new_text = worker_entry.new_text

        diffs.append(
            FileDiff(
                path=full_path,
                old_text=old_text,
                new_text=new_text,
                truncated=truncated,
            )
        )
    return diffs

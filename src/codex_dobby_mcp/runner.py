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
from typing import BinaryIO
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
    ReviewAgent,
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
class ReviewOrchestrationDiagnostics:
    expected_agents: tuple[str, ...]
    completed_spawn_count: int
    wait_started_early: bool
    prompt_missing_agents: tuple[str, ...]
    prompt_duplicate_agents: tuple[str, ...]
    ambiguous_prompt_count: int
    spawned_children: frozenset[str]
    completed_children: frozenset[str]
    child_results: tuple[WorkerResult, ...]

    @property
    def expected_count(self) -> int:
        return len(self.expected_agents)

    @property
    def completed_child_result_count(self) -> int:
        return len(self.child_results)

    @property
    def missing_completed_children(self) -> frozenset[str]:
        return self.spawned_children - self.completed_children

    @property
    def warnings(self) -> list[str]:
        warnings: list[str] = []
        if self.completed_spawn_count != self.expected_count:
            warnings.append(
                f"Review emitted {self.completed_spawn_count} completed spawn_agent calls; expected exactly {self.expected_count}"
            )
        if self.wait_started_early:
            warnings.append("Review started waiting before spawning all selected review agents")
        if self.prompt_missing_agents:
            warnings.append(
                "Review spawn prompts did not cover selected agents: " + ", ".join(self.prompt_missing_agents)
            )
        if self.prompt_duplicate_agents:
            warnings.append(
                "Review spawn prompts duplicated selected agents: " + ", ".join(self.prompt_duplicate_agents)
            )
        if self.ambiguous_prompt_count:
            warnings.append(
                f"Review emitted {self.ambiguous_prompt_count} ambiguous spawn prompts that could not be mapped to exactly one selected agent"
            )
        if self.spawned_children and self.missing_completed_children:
            warnings.append("Review did not record completed wait results for every spawned subagent")
        return warnings

    @property
    def has_missing_wait_only(self) -> bool:
        return self.warnings == ["Review did not record completed wait results for every spawned subagent"]

    @property
    def salvage_complete(self) -> bool:
        return self.completed_child_result_count >= self.expected_count

    def failure_summary(self) -> str:
        details: list[str] = []
        if self.expected_count:
            details.append(f"completed {self.completed_child_result_count}/{self.expected_count} subagents")
        if self.completed_spawn_count != self.expected_count:
            details.append(f"spawned {self.completed_spawn_count}/{self.expected_count} subagents")
        if self.wait_started_early:
            details.append("wait started before all selected review agents were spawned")
        if self.prompt_missing_agents:
            details.append("missing prompt coverage for " + ", ".join(self.prompt_missing_agents))
        if self.prompt_duplicate_agents:
            details.append("duplicate prompt coverage for " + ", ".join(self.prompt_duplicate_agents))
        if self.ambiguous_prompt_count:
            suffix = "" if self.ambiguous_prompt_count == 1 else "s"
            details.append(f"{self.ambiguous_prompt_count} ambiguous spawn prompt{suffix}")
        if self.spawned_children and self.missing_completed_children:
            details.append(
                "missing completed wait results for "
                f"{len(self.missing_completed_children)}/{len(self.spawned_children)} spawned subagents"
            )
        if not details:
            return "Review orchestration incomplete."
        return "Review orchestration incomplete: " + "; ".join(details) + "."

    def salvaged_summary(self) -> str:
        count = self.completed_child_result_count
        if count >= self.expected_count:
            return (
                "Review orchestrator did not return final JSON; "
                f"merged findings from {count}/{self.expected_count} completed subagents."
            )
        return (
            "Review orchestrator did not return final JSON; "
            f"surfaced findings from {count}/{self.expected_count} completed subagents."
        )

    def salvaged_warning(self) -> str:
        return self.salvaged_summary()

    def partial_salvage_warning(self) -> str:
        return (
            "Review returned partial findings from "
            f"{self.completed_child_result_count}/{self.expected_count} completed subagents "
            "because orchestration did not finish before timeout"
        )


@dataclass(frozen=True)
class ChildRuntimeContext:
    private_root: Path
    codex_home: Path
    claude_config_dir: Path
    home_config_path: Path
    env_overrides: dict[str, str]


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

        try:
            return await self._run_with_child_runtime(
                spec,
                started,
                deadline,
                use_metadata_fingerprints,
                child_runtime,
                event_sink=event_sink,
            )
        finally:
            _cleanup_private_child_runtime(child_runtime.private_root, self.logger)

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

        prompt_text = self.prompts.render(
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
            if process is not None:
                await _terminate_process(process, timeout=_POST_TIMEOUT_TERMINATE_GRACE_SECONDS)
            raise
        except asyncio.TimeoutError:
            timeout_hit = True
            if process is not None:
                await _terminate_process(process)

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = _read_log_text(spec.artifacts.stdout_log)
        stderr = _read_log_text(spec.artifacts.stderr_log)

        worker_result_error: str | None = None
        review_salvaged = False
        review_salvaged_complete = False
        review_is_orchestrated = tool == ToolName.REVIEW and review_uses_orchestrator(spec.request.agents)
        review_diagnostics = (
            _review_orchestration_diagnostics(stdout, spec.request.agents)
            if review_is_orchestrated
            else None
        )
        try:
            worker_result = self._load_worker_result(spec.artifacts, allow_missing=exit_code != 0 or timeout_hit)
        except RunnerError as exc:
            worker_result = None
            worker_result_error = str(exc)
        if review_is_orchestrated and worker_result is None:
            worker_result = _salvaged_review_worker_result(
                stdout,
                spec.request.agents,
                diagnostics=review_diagnostics,
            )
            if worker_result is not None:
                review_salvaged = True
                review_salvaged_complete = (
                    review_diagnostics.salvage_complete
                    if review_diagnostics is not None
                    else _review_salvage_complete(stdout, spec.request.agents)
                )
                worker_result_error = None
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
        recovered_partial_review = (
            review_is_orchestrated
            and review_salvaged
            and worker_result is not None
            and not review_salvaged_complete
        )
        timeout_with_usable_review = (
            tool == ToolName.REVIEW
            and timeout_hit
            and worker_result is not None
            and worker_result.completeness != Completeness.BLOCKED
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

        reported_files = worker_result.files_changed if worker_result else []
        detected_files = _changed_status_files(baseline, after_snapshot)
        files_changed = detected_files

        status = RunStatus.SUCCESS
        stop_reason: StopReason | None = None
        warnings = list(worker_result.warnings if worker_result else [])
        error_reasons: list[str] = []
        sandbox_violations = _collect_sandbox_violations(stderr, stdout)
        codex_home_issue = _codex_home_permission_issue(sandbox_violations, stderr=stderr, stdout=stdout)
        if review_salvaged:
            if review_diagnostics is not None:
                warnings.append(review_diagnostics.salvaged_warning())
            else:
                warnings.append(
                    "Review parent did not return final JSON; surfaced completed subagent findings from the orchestration log"
                )
        if recovered_partial_review:
            if review_diagnostics is not None:
                warnings.append(review_diagnostics.partial_salvage_warning())
            else:
                warnings.append(
                    "Review returned partial findings from completed subagents because orchestration did not finish before timeout"
                )
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
        elif exit_code != 0 and not recovered_partial_review:
            status = RunStatus.ERROR
            stop_reason = StopReason.ERROR
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
        if review_diagnostics is not None:
            orchestration_warnings = review_diagnostics.warnings
            if orchestration_warnings:
                warnings.extend(orchestration_warnings)
                recoverable_orchestration_only = (
                    worker_result is not None
                    and worker_result.completeness != Completeness.BLOCKED
                    and review_diagnostics.has_missing_wait_only
                )
                if not recovered_partial_review and not recoverable_orchestration_only:
                    status = RunStatus.ERROR
                    error_reasons.append(review_diagnostics.failure_summary())

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
        )
        completeness = worker_result.completeness if worker_result else Completeness.BLOCKED
        if status == RunStatus.ERROR:
            completeness = Completeness.BLOCKED
        elif timeout_hit and completeness == Completeness.FULL:
            completeness = Completeness.PARTIAL
        elif (recovered_partial_review or recoverable_orchestration_only) and completeness == Completeness.FULL:
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
            review_details=_review_details_for_spec(spec),
            reverse_engineer_details=_reverse_engineer_details_for_run(spec, stdout=stdout, stderr=stderr),
        )
        write_json(spec.artifacts.result_json, response.model_dump(mode="json", by_alias=True))
        return response

    @staticmethod
    def _preflight_response(
        spec: ResolvedInvocation,
        started: float,
        issue: str,
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
            sandbox_violations=[issue],
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
                    "Pass repo_root explicitly or send repo metadata."
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
                        "Pass repo_root explicitly or send repo metadata."
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
        fetchaller_available = mcp_server_is_enabled("fetchaller", repo_root=repo_root)
        ghidra_available = mcp_server_is_enabled("ghidra", repo_root=repo_root)

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
            reasoning_effort = request.reasoning_effort or REVIEW_SUBAGENT_DEFAULT_REASONING_EFFORT
        elif tool == ToolName.PLAN and request.reasoning_effort is None and request.timeout_seconds <= 120:
            model = request.model or DEFAULT_MODEL
            reasoning_effort = ReasoningEffort.MEDIUM
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
            payload = json.loads(raw_last_message)
        except json.JSONDecodeError as exc:
            if allow_missing:
                return None
            raise RunnerError("Codex completed with non-JSON structured output") from exc

        try:
            return WorkerResult.model_validate(payload)
        except ValidationError as exc:
            if allow_missing:
                return None
            raise RunnerError("Codex completed with invalid structured output") from exc

    def _capture_repo_snapshot(
        self,
        repo_root: Path,
        artifacts: RunArtifacts,
        gitignore_updated: bool,
        include_head: bool,
        *,
        use_metadata_fingerprints: bool = False,
    ) -> RepoSnapshot:
        _ = artifacts, gitignore_updated
        return _build_repo_snapshot(
            repo_root,
            include_head=include_head,
            use_metadata_fingerprints=use_metadata_fingerprints,
        )

    @staticmethod
    def _resolve_summary(
        worker_result: WorkerResult | None,
        stdout: str,
        stderr: str,
        exit_code: int | None,
        timeout_hit: bool,
        worker_result_error: str | None,
        error_reasons: list[str],
    ) -> str:
        if error_reasons:
            return error_reasons[0]
        if worker_result and worker_result.summary.strip():
            return worker_result.summary.strip()
        # Codex emits API failures (e.g. invalid response_format) as
        # ``{"type":"error",...}`` / ``{"type":"turn.failed",...}`` events on
        # stdout. Surface them before falling back to the (often noisy)
        # stderr trace, otherwise the user sees the TRACE preamble instead
        # of the actual model API error.
        codex_error = _first_codex_stdout_error(stdout)
        if codex_error:
            return codex_error
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


def _review_details_for_spec(spec: ResolvedInvocation) -> ReviewDetails | None:
    if spec.tool != ToolName.REVIEW:
        return None
    effective_agents = selected_review_agents(spec.request.agents)
    return ReviewDetails(
        requested_review_agents=list(spec.requested_review_agents),
        effective_review_agents=effective_agents,
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
            "--ignored=matching",
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
        files.append(path)

        if any(code in {"R", "C"} for code in status_code):
            index += 2
            continue
        index += 1
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


def _count_completed_spawn_agent_calls(stdout: str) -> int:
    count = 0
    top_level_thread_id: str | None = None
    started_calls: dict[str, str | None] = {}

    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue

        payload_type = payload.get("type")
        if payload_type == "thread.started" and top_level_thread_id is None:
            thread_id = payload.get("thread_id")
            if isinstance(thread_id, str):
                top_level_thread_id = thread_id
            continue

        item = payload.get("item") or {}
        if item.get("type") != "collab_tool_call" or item.get("tool") != "spawn_agent":
            continue

        item_id = item.get("id")
        if not isinstance(item_id, str):
            continue

        sender_thread_id = item.get("sender_thread_id")
        normalized_sender = sender_thread_id if isinstance(sender_thread_id, str) else None
        if payload_type == "item.started":
            if top_level_thread_id is not None and normalized_sender != top_level_thread_id:
                continue
            started_calls[item_id] = normalized_sender
            continue

        if payload_type != "item.completed" or item_id not in started_calls:
            continue
        if top_level_thread_id is not None:
            started_sender = started_calls[item_id]
            if started_sender != top_level_thread_id and normalized_sender != top_level_thread_id:
                continue
        count += 1

    return count


def _review_orchestration_warnings(stdout: str, agents) -> list[str]:
    return _review_orchestration_diagnostics(stdout, agents).warnings


def _review_orchestration_diagnostics(stdout: str, agents) -> ReviewOrchestrationDiagnostics:
    expected_agents = selected_review_agent_definitions(agents)
    collab_events = _top_level_collab_events(stdout)
    spawn_events = _completed_top_level_spawn_events(collab_events)
    wait_started_early = False
    completed_spawns_before_wait = 0
    for payload_type, item in collab_events:
        tool = item.get("tool")
        if tool == "spawn_agent" and payload_type == "item.completed":
            completed_spawns_before_wait += 1
            continue
        if tool == "wait" and payload_type == "item.started":
            if completed_spawns_before_wait < len(expected_agents):
                wait_started_early = True
            break

    missing_agents: list[str] = []
    duplicate_agents: list[str] = []
    ambiguous_prompts = 0
    if spawn_events:
        prompt_match_counts = {
            definition.review_agent.value: 0
            for definition in expected_agents
        }
        for item in spawn_events:
            prompt = item.get("prompt")
            matched_agent = _match_review_spawn_prompt(prompt, expected_agents)
            if matched_agent is None:
                ambiguous_prompts += 1
                continue
            prompt_match_counts[matched_agent] += 1

        missing_agents = [agent for agent, count in prompt_match_counts.items() if count == 0]
        duplicate_agents = [agent for agent, count in prompt_match_counts.items() if count > 1]

    spawned_children = {
        receiver
        for item in spawn_events
        for receiver in item.get("receiver_thread_ids", [])
        if isinstance(receiver, str)
    }
    return ReviewOrchestrationDiagnostics(
        expected_agents=tuple(definition.review_agent.value for definition in expected_agents),
        completed_spawn_count=len(spawn_events),
        wait_started_early=wait_started_early,
        prompt_missing_agents=tuple(missing_agents),
        prompt_duplicate_agents=tuple(duplicate_agents),
        ambiguous_prompt_count=ambiguous_prompts,
        spawned_children=frozenset(spawned_children),
        completed_children=frozenset(_completed_waited_child_threads(collab_events)),
        child_results=tuple(_completed_wait_worker_results(collab_events)),
    )


def _match_review_spawn_prompt(prompt: object, expected_agents) -> str | None:
    if not isinstance(prompt, str):
        return None

    prompt_lower = prompt.lower()
    line_matches: set[str] = set()
    for raw_line in prompt_lower.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line_matches.update(_match_review_spawn_prompt_line(line, expected_agents))

    if len(line_matches) == 1:
        return next(iter(line_matches))
    if len(line_matches) > 1:
        return None

    text_matches = {
        definition.review_agent.value
        for definition in expected_agents
        if any(marker in prompt_lower for marker in _review_prompt_markers(definition))
    }
    if len(text_matches) == 1:
        return next(iter(text_matches))
    return None


def _match_review_spawn_prompt_line(line: str, expected_agents) -> set[str]:
    normalized = line.strip().strip("`")
    for prefix in ("required custom agent:", "required agent type:", "assigned lens:"):
        if normalized.startswith(prefix):
            value = normalized.split(":", 1)[1].strip().strip("`")
            return {
                definition.review_agent.value
                for definition in expected_agents
                if value in _review_prompt_markers(definition)
            }

    if normalized.startswith("### spawn"):
        return {
            definition.review_agent.value
            for definition in expected_agents
            if definition.codex_name.lower() in normalized
        }
    return set()


def _review_prompt_markers(definition) -> set[str]:
    return {
        definition.review_agent.value.lower(),
        definition.label.lower(),
        definition.codex_name.lower(),
    }


def _top_level_collab_events(stdout: str) -> list[tuple[str, dict[str, object]]]:
    top_level_thread_id: str | None = None
    events: list[tuple[str, dict[str, object]]] = []

    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue

        payload_type = payload.get("type")
        if payload_type == "thread.started" and top_level_thread_id is None:
            thread_id = payload.get("thread_id")
            if isinstance(thread_id, str):
                top_level_thread_id = thread_id
            continue

        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "collab_tool_call":
            continue
        sender_thread_id = item.get("sender_thread_id")
        if top_level_thread_id is not None and sender_thread_id != top_level_thread_id:
            continue
        events.append((payload_type, item))

    return events


def _completed_top_level_spawn_events(collab_events: list[tuple[str, dict[str, object]]]) -> list[dict[str, object]]:
    started_ids: set[str] = set()
    completed: list[dict[str, object]] = []

    for payload_type, item in collab_events:
        if item.get("tool") != "spawn_agent":
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str):
            continue
        if payload_type == "item.started":
            started_ids.add(item_id)
            continue
        if payload_type == "item.completed" and item_id in started_ids:
            completed.append(item)

    return completed


def _completed_waited_child_threads(collab_events: list[tuple[str, dict[str, object]]]) -> set[str]:
    completed: set[str] = set()
    for payload_type, item in collab_events:
        if payload_type != "item.completed" or item.get("tool") != "wait":
            continue
        agents_states = item.get("agents_states")
        if not isinstance(agents_states, dict):
            continue
        for thread_id, state in agents_states.items():
            if not isinstance(thread_id, str) or not isinstance(state, dict):
                continue
            if state.get("status") == "completed":
                completed.add(thread_id)
    return completed


def _completed_wait_messages(collab_events: list[tuple[str, dict[str, object]]]) -> list[str]:
    messages: list[str] = []
    for payload_type, item in collab_events:
        if payload_type != "item.completed" or item.get("tool") != "wait":
            continue
        agents_states = item.get("agents_states")
        if not isinstance(agents_states, dict):
            continue
        for state in agents_states.values():
            if not isinstance(state, dict) or state.get("status") != "completed":
                continue
            message = state.get("message")
            if isinstance(message, str) and message.strip():
                messages.append(message)
    return messages


def _completed_wait_worker_results(collab_events: list[tuple[str, dict[str, object]]]) -> list[WorkerResult]:
    child_results: list[WorkerResult] = []
    for message in _completed_wait_messages(collab_events):
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            continue
        try:
            child_results.append(WorkerResult.model_validate(payload))
        except ValidationError:
            continue
    return child_results


def _salvaged_review_worker_result(
    stdout: str,
    agents,
    *,
    diagnostics: ReviewOrchestrationDiagnostics | None = None,
) -> WorkerResult | None:
    diagnostics = diagnostics or _review_orchestration_diagnostics(stdout, agents)
    child_results = list(diagnostics.child_results)
    if not child_results:
        return None
    if len(child_results) == 1 and diagnostics.expected_count <= 1:
        return child_results[0]

    return WorkerResult(
        summary=diagnostics.salvaged_summary(),
        completeness=Completeness.FULL if diagnostics.salvage_complete else Completeness.PARTIAL,
        important_facts=_merge_preserving_order(
            [fact for result in child_results for fact in result.important_facts],
            [],
        ),
        next_steps=_merge_preserving_order(
            [step for result in child_results for step in result.next_steps],
            [],
        ),
        files_changed=[],
        warnings=_merge_preserving_order(
            [warning for result in child_results for warning in result.warnings],
            [],
        ),
    )


def _review_salvage_complete(stdout: str, agents) -> bool:
    return _review_orchestration_diagnostics(stdout, agents).salvage_complete


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
    try:
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
                artifacts,
                codex_home=codex_home,
                claude_config_dir=claude_config_dir,
            ),
        )
    except RunnerError:
        if private_root is not None:
            _cleanup_private_child_runtime(private_root)
        raise
    except Exception as exc:
        if private_root is not None:
            _cleanup_private_child_runtime(private_root)
        target = private_root / "codex-home" if private_root is not None else Path(tempfile.gettempdir()) / "codex-dobby"
        raise RunnerError(
            _codex_home_access_message(
                str(target),
                subject="its private runtime home",
                verb="cannot create",
            )
        ) from exc


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

    rewritten = payload.replace(str(source_codex_home), str(codex_home)).replace(
        str(source_claude_config),
        str(claude_config_dir),
    )
    if _same_path(source_codex_home, _GLOBAL_CODEX_DIR):
        rewritten = rewritten.replace("~/.codex", str(codex_home))
    if _same_path(source_claude_config, _GLOBAL_CLAUDE_DIR):
        rewritten = rewritten.replace("~/.claude", str(claude_config_dir))

    try:
        destination.write_text(rewritten, encoding="utf-8")
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


def _mirror_runtime_path_reference(
    reference: Path,
    *,
    source_root: Path,
    target_root: Path,
    mirrored: set[Path],
) -> None:
    try:
        reference.relative_to(source_root)
    except ValueError:
        return
    if not reference.exists():
        return

    if reference.is_dir():
        source_item = reference
        target_item = target_root / reference.relative_to(source_root)
    elif reference.parent == source_root:
        source_item = reference
        target_item = target_root / reference.relative_to(source_root)
    else:
        source_item = reference.parent
        target_item = target_root / source_item.relative_to(source_root)

    if source_item in mirrored:
        return
    mirrored.add(source_item)

    target_item.parent.mkdir(parents=True, exist_ok=True)
    if source_item.is_dir():
        shutil.copytree(source_item, target_item, dirs_exist_ok=True)
    else:
        shutil.copy2(source_item, target_item)


def _absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    return Path(os.path.abspath(os.fspath(expanded)))


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return _absolute_path(left) == _absolute_path(right)


def _cleanup_private_child_runtime(path: Path, logger=None) -> None:  # type: ignore[no-untyped-def]
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        if logger is not None:
            logger.warning("Failed to remove private child runtime %s: %s", path, exc)


def _child_runtime_environment_overrides(
    artifacts: RunArtifacts,
    *,
    codex_home: Path,
    claude_config_dir: Path,
) -> dict[str, str]:
    runtime_root = _ensure_artifact_subdirectory(artifacts.run_dir / "runtime", "child runtime directory")
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
    path.mkdir(parents=True)
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

from __future__ import annotations

import asyncio
from collections.abc import Mapping
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
from codex_dobby_mcp.gitignore import ensure_codex_dobby_ignored
from codex_dobby_mcp.logging_utils import get_logger
from codex_dobby_mcp.models import (
    Completeness,
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORTS,
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
_CODEX_STALL_CHECK_INTERVAL_SECONDS = 15.0
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

    async def run(self, tool: ToolName, request: InvocationRequest) -> ToolResponse:
        spec = self.prepare(tool, request)
        return await self.run_resolved(spec)

    async def run_resolved(self, spec: ResolvedInvocation) -> ToolResponse:
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
            write_json(spec.artifacts.result_json, preflight_response.model_dump(mode="json"))
            return preflight_response

        try:
            return await self._run_with_child_runtime(
                spec,
                started,
                deadline,
                use_metadata_fingerprints,
                child_runtime,
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
            stall_warning = (
                f"Codex produced no stdout or stderr output for "
                f"{int(_CODEX_STALL_THRESHOLD_SECONDS)} seconds and was killed as stalled. "
                "This usually means the underlying model response hung before emitting any tokens."
            )
            warnings.append(stall_warning)
            status = RunStatus.ERROR
            error_reasons.append(stall_warning)
        elif timeout_hit:
            timeout_warning = _timeout_warning(spec)
            warnings.append(timeout_warning)
            if not timeout_with_usable_review:
                status = RunStatus.ERROR
                error_reasons.append(timeout_warning)
        elif exit_code != 0 and not recovered_partial_review:
            status = RunStatus.ERROR
        if codex_home_issue is not None:
            warnings.append(codex_home_issue)
            if exit_code not in (0, None) and not stall_hit and not timeout_hit:
                error_reasons.append(codex_home_issue)
        if worker_result_error:
            status = RunStatus.ERROR
            warnings.append(worker_result_error)
            error_reasons.append(worker_result_error)

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
            status = RunStatus.ERROR
            read_only_warning = "Read-only tool changed files outside wrapper-managed artifacts"
            warnings.append(read_only_warning)
            error_reasons.append(read_only_warning)
        if tool in MUTATING_TOOLS and post_run_snapshot_incomplete:
            status = RunStatus.ERROR
            verification_warning = "Post-run repo snapshot timed out; mutating tool results could not be fully verified"
            warnings.append(verification_warning)
            error_reasons.append(verification_warning)
        if tool in MUTATING_TOOLS and current_head is not None and current_head != baseline.head_commit:
            status = RunStatus.ERROR
            commit_warning = "Mutating tool changed git history or references, which Dobby does not allow"
            warnings.append(commit_warning)
            error_reasons.append(commit_warning)
        if tool in READ_ONLY_TOOLS and spec.gitignore_updated:
            warnings.append("Wrapper updated .gitignore to add .codex-dobby/ before running")

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

        response = ToolResponse(
            task_id=spec.artifacts.run_dir.name,
            tool=tool,
            status=status,
            summary=summary,
            completeness=completeness,
            important_facts=important_facts,
            next_steps=next_steps,
            files_changed=files_changed,
            artifact_paths=spec.artifacts.as_public_dict(),
            sandbox_violations=sandbox_violations,
            repo_root=str(spec.repo_root),
            exit_code=exit_code,
            duration_ms=duration_ms,
            warnings=warnings,
            raw_output_available=True,
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
            review_details=_review_details_for_spec(spec),
            reverse_engineer_details=_reverse_engineer_details_for_run(spec, stdout=stdout, stderr=stderr),
        )
        write_json(spec.artifacts.result_json, response.model_dump(mode="json"))
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
            review_details=_review_details_for_spec(spec),
            reverse_engineer_details=_reverse_engineer_failure_details(spec),
        )
        write_json(spec.artifacts.result_json, stub.model_dump(mode="json"))

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
            review_details=_review_details_for_spec(spec),
            reverse_engineer_details=_reverse_engineer_failure_details(spec),
        )
        write_json(spec.artifacts.result_json, response.model_dump(mode="json"))
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
            continue
        return stripped
    return None


def _timeout_warning(spec: ResolvedInvocation, fallback_timeout_seconds: int | None = None) -> str:
    effective_timeout = spec.request.timeout_seconds if spec.request.timeout_seconds else fallback_timeout_seconds
    if effective_timeout is None:
        return "Codex run timed out"
    return f"Codex run timed out after {effective_timeout} seconds"


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


async def _execute_process_with_streaming_logs(
    process,
    stdin_payload: bytes,
    stdout_log: Path,
    stderr_log: Path,
    timeout: float,
) -> tuple[int | None, bool, bool]:
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
        return process.returncode, timeout_hit, False

    timeout_hit = False
    stall_flag: list[bool] = [False]
    tracker = _StreamActivityTracker()
    with stdout_log.open("wb") as stdout_handle, stderr_log.open("wb") as stderr_handle:
        stdin_task = asyncio.create_task(_write_process_stdin(process.stdin, stdin_payload))
        stdout_task = asyncio.create_task(_pump_process_stream(process.stdout, stdout_handle, tracker))
        stderr_task = asyncio.create_task(_pump_process_stream(process.stderr, stderr_handle, tracker))
        stall_task = asyncio.create_task(
            _monitor_process_stall(process, tracker, _CODEX_STALL_THRESHOLD_SECONDS, stall_flag)
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

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tomllib
from typing import NamedTuple

from codex_dobby_mcp.models import (
    InvocationRequest,
    READ_ONLY_TOOLS,
    ToolName,
)
from codex_dobby_mcp.review_agents import selected_review_agent_definitions


class ReviewAssignment(NamedTuple):
    """One lens's canonical assignment.

    ``message`` is what the parent is asked to relay. ``canonical_json`` and
    ``digest`` are the same assignment delivered to the child out of band, via
    the per-run agent definition Dobby writes into the child's private runtime,
    so a parent cannot decide what a reviewer actually reviews.
    """

    task_name: str
    canonical_json: str
    digest: str
    message: str


class RenderedPrompt(NamedTuple):
    """A rendered worker prompt plus the canonical messages it embedded.

    ``review_subagent_messages`` is empty for every tool except an
    orchestrated multi-agent review.
    """

    text: str
    review_subagent_messages: dict[str, str]
    review_subagent_assignments: dict[str, ReviewAssignment]


class PromptLoader:
    def __init__(self, prompts_root: Path):
        self.prompts_root = prompts_root

    def load(self, relative_path: str) -> str:
        template_path = self.prompts_root / relative_path
        if not template_path.exists():
            raise RuntimeError(f"Prompt template not found: {template_path}")
        return template_path.read_text(encoding="utf-8").strip() + "\n"

    def render(
        self,
        tool: ToolName,
        request: InvocationRequest,
        repo_root: Path,
        sandbox_roots: list[Path],
        advisory_read_only_roots: list[Path],
        model: str,
        reasoning_effort: str,
        *,
        fetchaller_available: bool = False,
        ghidra_available: bool = False,
    ) -> str:
        return self.render_prompt(
            tool,
            request,
            repo_root,
            sandbox_roots,
            advisory_read_only_roots,
            model,
            reasoning_effort,
            fetchaller_available=fetchaller_available,
            ghidra_available=ghidra_available,
        ).text

    def render_prompt(
        self,
        tool: ToolName,
        request: InvocationRequest,
        repo_root: Path,
        sandbox_roots: list[Path],
        advisory_read_only_roots: list[Path],
        model: str,
        reasoning_effort: str,
        *,
        fetchaller_available: bool = False,
        ghidra_available: bool = False,
    ) -> RenderedPrompt:
        """Render the worker prompt and report what it actually embedded.

        The canonical subagent messages are returned rather than recomputed by
        the caller: rollout validation compares each child's attested
        assignment digest against these exact strings, so deriving them twice
        would let any future nondeterminism in rendering fail every
        multi-agent review with an unhelpful attestation error.
        """
        subagent_assignments: dict[str, ReviewAssignment] = {}
        tool_prompt = self.load(f"{tool.value}.md").strip()
        if tool == ToolName.REVIEW:
            selected_agents = selected_review_agent_definitions(request.agents)
            if len(selected_agents) == 1:
                agent = selected_agents[0]
                review_budget = self._review_short_timeout_budget(
                    request.timeout_seconds,
                    agent_count=1,
                    orchestrated=False,
                )
                tool_prompt = self.load("review-single.md").strip().format(
                    selected_review_agent_label=agent.label,
                    selected_review_agent_name=agent.codex_name,
                    selected_review_agent_description=agent.description,
                    selected_review_agent_instructions=self._review_agent_instructions(agent),
                    review_short_timeout_mode="yes" if review_budget["short_timeout_mode"] else "no",
                    review_named_file_budget=review_budget["named_file_budget"],
                    review_additional_file_budget=review_budget["additional_file_budget"],
                    review_shell_command_budget=review_budget["shell_command_budget"],
                )
            else:
                timeout_plan = self._review_timeout_plan(
                    request.timeout_seconds,
                    agent_count=len(selected_agents),
                )
                review_budget = self._review_short_timeout_budget(
                    request.timeout_seconds,
                    agent_count=len(selected_agents),
                    orchestrated=True,
                )
                subagent_assignments = self.review_subagent_assignments(
                    request=request,
                    repo_root=repo_root,
                    sandbox_roots=sandbox_roots,
                    advisory_read_only_roots=advisory_read_only_roots,
                    fetchaller_available=fetchaller_available,
                    ghidra_available=ghidra_available,
                )
                tool_prompt = tool_prompt.format(
                    selected_review_agents=self._format_list([agent.label for agent in selected_agents]),
                    selected_review_subagents=self._format_list(
                        [f"{agent.codex_name} ({agent.label})" for agent in selected_agents]
                    ),
                    review_subagent_jobs=self._render_review_subagent_jobs(
                        request.agents,
                        {
                            task_name: assignment.message
                            for task_name, assignment in subagent_assignments.items()
                        },
                    ),
                    review_timeout_seconds=request.timeout_seconds,
                    review_initial_wait_seconds=timeout_plan["initial_wait_seconds"],
                    review_initial_wait_timeout_ms=timeout_plan["initial_wait_timeout_ms"],
                    review_wrap_up_seconds=timeout_plan["wrap_up_seconds"],
                    review_interrupt_wait_seconds=timeout_plan["interrupt_wait_seconds"],
                    review_interrupt_wait_timeout_ms=timeout_plan["interrupt_wait_timeout_ms"],
                    review_synthesis_seconds=timeout_plan["synthesis_seconds"],
                    review_short_timeout_mode="yes" if review_budget["short_timeout_mode"] else "no",
                    review_named_file_budget=review_budget["named_file_budget"],
                    review_additional_file_budget=review_budget["additional_file_budget"],
                    review_shell_command_budget=review_budget["shell_command_budget"],
                )
        shared_prompt = self.load("shared.md").strip()
        read_only_budget = self._read_only_short_timeout_budget(tool, request.timeout_seconds)
        shared_rendered = shared_prompt.format(
            tool_name=tool.value,
            repo_root=str(repo_root),
            allow_edits="yes" if tool not in READ_ONLY_TOOLS else "no",
            model=model,
            reasoning_effort=reasoning_effort,
            sandbox_roots=self._format_paths(sandbox_roots),
            writable_roots=self._format_paths(sandbox_roots if tool not in READ_ONLY_TOOLS else []),
            advisory_read_only_roots=self._format_paths(advisory_read_only_roots),
            files=self._format_list(self._relevant_files(tool, request)),
            important_context=request.important_context.strip() if request.important_context else "(none)",
            task_prompt=request.prompt.strip(),
            extra_roots=self._format_list(request.extra_roots),
            extra_root_access_note=(
                "Requested extra roots are read-only context only here. Trust the sandbox-accessible roots and advisory read-only roots lists above for what is actually mounted."
                if tool in READ_ONLY_TOOLS
                else "Requested extra roots listed above are writable only when also present in the writable roots section."
            ),
            fetchaller_available="yes" if fetchaller_available else "no",
            ghidra_available="yes" if ghidra_available else "no",
            danger_mode="true" if request.danger else "false",
            timeout_seconds=request.timeout_seconds,
            read_only_short_timeout_mode="yes" if read_only_budget["short_timeout_mode"] else "no",
            read_only_named_file_budget=read_only_budget["named_file_budget"],
            read_only_additional_file_budget=read_only_budget["additional_file_budget"],
            read_only_shell_command_budget=read_only_budget["shell_command_budget"],
        )
        return RenderedPrompt(
            text="\n\n".join([tool_prompt, shared_rendered]).strip() + "\n",
            review_subagent_messages={
                task_name: assignment.message
                for task_name, assignment in subagent_assignments.items()
            },
            review_subagent_assignments=subagent_assignments,
        )

    @staticmethod
    def _format_list(items: list[str]) -> str:
        if not items:
            return "(none)"
        return "\n".join(f"- {item}" for item in items)

    @staticmethod
    def _format_paths(items: list[Path]) -> str:
        if not items:
            return "(none)"
        return "\n".join(f"- {path}" for path in items)

    @staticmethod
    def _relevant_files(tool: ToolName, request: InvocationRequest) -> list[str]:
        if request.files:
            return request.files
        if tool == ToolName.REVIEW:
            return ["(entire repo)"]
        return []

    def review_subagent_messages(
        self,
        *,
        request: InvocationRequest,
        repo_root: Path,
        sandbox_roots: list[Path],
        advisory_read_only_roots: list[Path],
        fetchaller_available: bool,
        ghidra_available: bool,
    ) -> dict[str, str]:
        return {
            task_name: assignment.message
            for task_name, assignment in self.review_subagent_assignments(
                request=request,
                repo_root=repo_root,
                sandbox_roots=sandbox_roots,
                advisory_read_only_roots=advisory_read_only_roots,
                fetchaller_available=fetchaller_available,
                ghidra_available=ghidra_available,
            ).items()
        }

    def review_subagent_assignments(
        self,
        *,
        request: InvocationRequest,
        repo_root: Path,
        sandbox_roots: list[Path],
        advisory_read_only_roots: list[Path],
        fetchaller_available: bool,
        ghidra_available: bool,
    ) -> dict[str, ReviewAssignment]:
        definitions = selected_review_agent_definitions(request.agents)
        if len(definitions) <= 1:
            return {}
        review_budget = self._review_short_timeout_budget(
            request.timeout_seconds,
            agent_count=len(definitions),
            orchestrated=True,
        )
        shared_assignment = {
            "advisory_read_only_roots": [str(path) for path in advisory_read_only_roots],
            "extra_roots": list(request.extra_roots),
            "fetchaller_available": fetchaller_available,
            "files": self._relevant_files(ToolName.REVIEW, request),
            "ghidra_available": ghidra_available,
            "important_context": request.important_context.strip() if request.important_context else None,
            "repo_root": str(repo_root),
            "sandbox_roots": [str(path) for path in sandbox_roots],
            "task_prompt": request.prompt.strip(),
            "timeout_seconds": request.timeout_seconds,
        }
        return {
            definition.codex_name: self._render_review_subagent_assignment(
                definition,
                shared_assignment,
                review_budget,
            )
            for definition in definitions
        }

    @staticmethod
    def _render_review_subagent_assignment(
        agent,
        shared_assignment: dict[str, object],
        review_budget: dict[str, int | bool],
    ) -> ReviewAssignment:
        assignment = {
            **shared_assignment,
            "manifest_review_agent": agent.review_agent.value,
            "review_lens": agent.label,
            "task_name": agent.codex_name,
        }
        canonical_assignment = json.dumps(
            assignment,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).replace("`", r"\u0060")
        assignment_digest = hashlib.sha256(
            canonical_assignment.encode("utf-8")
        ).hexdigest()
        output_example = json.dumps(
            {
                "summary": f"Dobby assignment {assignment_digest}: concise review summary",
                "completeness": "full",
                "important_facts": [],
                "next_steps": [],
                "files_changed": [],
                "warnings": [],
                "refused": False,
                "file_diffs": [],
                "review_execution": None,
            },
            separators=(",", ":"),
        )
        message = "\n".join(
            [
                f"Required custom agent: {agent.codex_name}",
                f"Assigned lens: {agent.label}",
                f"Canonical assignment JSON: {canonical_assignment}",
                f"Canonical assignment SHA-256: {assignment_digest}",
                "",
                "Requirements:",
                "- use the injected custom agent named above; do not substitute another agent type",
                "- stay read-only and do not spawn additional agents",
                "- treat README.md and repo-local instruction docs as the source of truth when present",
                "- do not flag documented behavior unless the implementation violates, broadens, or materially under-documents it",
                "- start with the named implementation files and expand only as needed to confirm concrete findings",
                "- inspect relevant surrounding code; use static inspection before shell commands",
                "- do not run broad test suites or uv run unless the caller explicitly requested execution",
                (
                    f"- start with at most {review_budget['named_file_budget']} named files, read at most "
                    f"{review_budget['additional_file_budget']} additional files, and avoid more than "
                    f"{review_budget['shell_command_budget']} shell commands before synthesizing"
                ),
                "- if interrupted for wrap-up, stop exploring and return the best JSON immediately with completeness=partial",
                "- return exactly one JSON object with fields summary, completeness, important_facts, next_steps, files_changed, warnings, refused, file_diffs, and review_execution",
                "- summary must be a string; completeness must be full, partial, or blocked; important_facts, next_steps, and warnings must be arrays of strings",
                "- files_changed and file_diffs must be empty arrays; refused must be the JSON boolean false unless you are explicitly refusing; review_execution must be null",
                f"- begin summary exactly with `Dobby assignment {assignment_digest}: ` followed by the actual concise review summary; Dobby removes this attestation before returning it",
                f"- follow this exact minimal shape (replace only the summary text, completeness, findings, next steps, and genuine warnings): `{output_example}`",
                "- return only concrete findings, risks, regressions, or missing tests, with file/symbol evidence when possible",
                "- do not include placeholders, orchestration markers, hidden tokens other than the required Dobby assignment prefix, or meta commentary",
            ]
        )
        return ReviewAssignment(
            task_name=agent.codex_name,
            canonical_json=canonical_assignment,
            digest=assignment_digest,
            message=message,
        )

    @staticmethod
    def _render_review_subagent_jobs(agents, messages: dict[str, str]) -> str:
        jobs: list[str] = []
        for agent in selected_review_agent_definitions(agents):
            message = messages[agent.codex_name]
            jobs.append(
                "\n".join(
                    [
                        f"### Spawn `{agent.codex_name}`",
                        f"Lens: {agent.label}",
                        f"Manifest review agent: `{agent.review_agent.value}`",
                        f"Required agent type: `{agent.codex_name}`",
                        f"Required task name: `{agent.codex_name}`",
                        "Call `spawn_agent` with this exact `agent_type` and `task_name`.",
                        "Set `fork_turns=\"none\"` so the custom agent configuration is accepted and the child receives only its explicit assignment.",
                        "Pass the following complete message. Preserve its first four lines exactly; they bind the child to the requested role, lens, repository, files, task, context, and assignment digest:",
                        "```text",
                        message,
                        "```",
                    ]
                ).strip()
            )
        return "\n\n".join(jobs)

    def _review_agent_instructions(self, agent) -> str:
        agent_config_path = self.prompts_root.parent / "codex_agents" / agent.filename
        payload = tomllib.loads(agent_config_path.read_text(encoding="utf-8"))
        instructions = payload.get("developer_instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            raise RuntimeError(f"Review agent instructions missing in {agent_config_path}")
        return instructions.strip()

    @staticmethod
    def _review_timeout_plan(timeout_seconds: int, agent_count: int) -> dict[str, int]:
        # Leave wrapper-side time for snapshots, Codex startup, spawning,
        # list_agents calls, and serialization of the final structured result.
        # Waiting phases must never consume the process's entire hard timeout.
        orchestration_margin_seconds = min(60, max(5, timeout_seconds // 10))
        available_seconds = max(3, timeout_seconds - orchestration_margin_seconds)

        # available_seconds is at least 270 given the validated 300s floor, so
        # the old <=90 clamp here was unreachable like the budget tiers below.
        wrap_up_seconds = max(10, min(120, available_seconds // 3))
        if wrap_up_seconds >= available_seconds:
            wrap_up_seconds = max(3, available_seconds // 2)

        initial_wait_seconds = max(1, available_seconds - wrap_up_seconds)
        synthesis_seconds = max(2, min(30, wrap_up_seconds // 4))
        interrupt_wait_seconds = max(1, wrap_up_seconds - synthesis_seconds)

        return {
            "initial_wait_seconds": initial_wait_seconds,
            "initial_wait_timeout_ms": min(initial_wait_seconds * 1000, 3_600_000),
            "wrap_up_seconds": wrap_up_seconds,
            "interrupt_wait_seconds": interrupt_wait_seconds,
            "interrupt_wait_timeout_ms": min(interrupt_wait_seconds * 1000, 3_600_000),
            "synthesis_seconds": synthesis_seconds,
        }

    @staticmethod
    def _review_short_timeout_budget(
        timeout_seconds: int,
        *,
        agent_count: int,
        orchestrated: bool,
    ) -> dict[str, int | bool]:
        # ``InvocationRequest.timeout_seconds`` is validated ``ge=300``, so the
        # former <=90/<=120/<=180 tiers were unreachable and this is the only
        # budget any run has ever rendered. If that floor is lowered,
        # reintroduce the tiers (see
        # test_timeout_floor_keeps_short_timeout_budgets_unreachable).
        _ = timeout_seconds, agent_count, orchestrated
        return {
            "short_timeout_mode": False,
            "named_file_budget": 4,
            "additional_file_budget": 8,
            "shell_command_budget": 8,
        }

    @staticmethod
    def _read_only_short_timeout_budget(tool: ToolName, timeout_seconds: int) -> dict[str, int | bool]:
        # Unreachable tiers removed for the same reason as the review budget
        # above: the validated timeout floor is 300 seconds.
        _ = tool, timeout_seconds
        return {
            "short_timeout_mode": False,
            "named_file_budget": 6,
            "additional_file_budget": 12,
            "shell_command_budget": 10,
        }

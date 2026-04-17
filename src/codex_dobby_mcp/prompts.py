from __future__ import annotations

from pathlib import Path
import tomllib

from codex_dobby_mcp.models import (
    InvocationRequest,
    READ_ONLY_TOOLS,
    ToolName,
)
from codex_dobby_mcp.review_agents import selected_review_agent_definitions


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
                tool_prompt = tool_prompt.format(
                    selected_review_agents=self._format_list([agent.label for agent in selected_agents]),
                    selected_review_subagents=self._format_list(
                        [f"{agent.codex_name} ({agent.label})" for agent in selected_agents]
                    ),
                    review_subagent_jobs=self._render_review_subagent_jobs(request.agents, review_budget),
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
        return "\n\n".join([tool_prompt, shared_rendered]).strip() + "\n"

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

    def _render_review_subagent_jobs(self, agents, review_budget: dict[str, int | bool]) -> str:
        jobs: list[str] = []
        for agent in selected_review_agent_definitions(agents):
            jobs.append(
                "\n".join(
                    [
                        f"### Spawn `{agent.codex_name}`",
                        f"Lens: {agent.label}",
                        f"Required agent type: `{agent.codex_name}`",
                        "Call `spawn_agent` and select this exact injected custom agent type.",
                        "Set `fork_context=false`.",
                        "The child message must begin with these exact two lines and must not paraphrase them:",
                        f"`Required custom agent: {agent.codex_name}`",
                        f"`Assigned lens: {agent.label}`",
                        "",
                        "Copy this exact opening into the child message before the rest of the assignment:",
                        f"Required custom agent: {agent.codex_name}",
                        f"Assigned lens: {agent.label}",
                        "",
                        "Subagent requirements:",
                        "- use the injected custom agent named above",
                        "- do not substitute `default`, `worker`, `explorer`, or any other agent type",
                        "- stay read-only",
                        "- treat `README.md` and repo-local instruction docs as the source of truth when present",
                        "- do not flag behavior that is explicitly required or intentionally documented unless the implementation violates, broadens, or fails to disclose it",
                        "- start with Claude's named files and expand outward only as needed",
                        "- start with the named implementation files before opening docs or tests",
                        "- do not open README.md, repo-local instruction docs, or large test files unless a specific contract or candidate issue requires that confirmation",
                        "- inspect surrounding code, not just the named files or diff",
                        "- default to static code inspection; only run shell commands when they are strictly necessary to confirm a finding",
                        "- do not run broad test suites or `uv run` from a read-only review unless Claude explicitly asked for execution",
                        f"- start with at most {review_budget['named_file_budget']} named files, read at most {review_budget['additional_file_budget']} additional files, and avoid more than {review_budget['shell_command_budget']} shell commands before synthesizing",
                        "- if the parent interrupts you to request wrap-up, stop exploring and return the best JSON you have immediately",
                        "- use `completeness` = `partial` if an interrupt forces you to return before finishing every check",
                        "- return only concrete findings, risks, regressions, or missing tests",
                        "- include file paths, symbols, or code-path notes when possible",
                        "- do not include placeholder strings, TODO markers, or meta commentary in any JSON field",
                        "- do not spawn additional subagents",
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
        if agent_count > 1 and timeout_seconds <= 90:
            initial_wait_seconds = min(20, max(10, timeout_seconds // 3))
            wrap_up_seconds = max(10, timeout_seconds - initial_wait_seconds)
            synthesis_seconds = max(2, min(15, wrap_up_seconds // 4))
            interrupt_wait_seconds = max(1, wrap_up_seconds - synthesis_seconds)
            return {
                "initial_wait_seconds": initial_wait_seconds,
                "initial_wait_timeout_ms": initial_wait_seconds * 1000,
                "wrap_up_seconds": wrap_up_seconds,
                "interrupt_wait_seconds": interrupt_wait_seconds,
                "interrupt_wait_timeout_ms": interrupt_wait_seconds * 1000,
                "synthesis_seconds": synthesis_seconds,
            }
        if agent_count > 1 and timeout_seconds <= 120:
            wrap_up_seconds = max(10, min(timeout_seconds - 2, (timeout_seconds * 2) // 3))
            initial_wait_seconds = max(1, timeout_seconds - wrap_up_seconds)
            synthesis_seconds = max(2, min(15, wrap_up_seconds // 4))
            interrupt_wait_seconds = max(1, wrap_up_seconds - synthesis_seconds)
            return {
                "initial_wait_seconds": initial_wait_seconds,
                "initial_wait_timeout_ms": initial_wait_seconds * 1000,
                "wrap_up_seconds": wrap_up_seconds,
                "interrupt_wait_seconds": interrupt_wait_seconds,
                "interrupt_wait_timeout_ms": interrupt_wait_seconds * 1000,
                "synthesis_seconds": synthesis_seconds,
            }

        if timeout_seconds <= 90:
            wrap_up_seconds = max(5, timeout_seconds // 2)
        else:
            wrap_up_seconds = max(10, min(120, timeout_seconds // 3))
        if wrap_up_seconds >= timeout_seconds:
            wrap_up_seconds = max(3, timeout_seconds // 2)

        initial_wait_seconds = max(1, timeout_seconds - wrap_up_seconds)
        synthesis_seconds = max(2, min(15, wrap_up_seconds // 4))
        interrupt_wait_seconds = max(1, wrap_up_seconds - synthesis_seconds)

        return {
            "initial_wait_seconds": initial_wait_seconds,
            "initial_wait_timeout_ms": initial_wait_seconds * 1000,
            "wrap_up_seconds": wrap_up_seconds,
            "interrupt_wait_seconds": interrupt_wait_seconds,
            "interrupt_wait_timeout_ms": interrupt_wait_seconds * 1000,
            "synthesis_seconds": synthesis_seconds,
        }

    @staticmethod
    def _review_short_timeout_budget(
        timeout_seconds: int,
        *,
        agent_count: int,
        orchestrated: bool,
    ) -> dict[str, int | bool]:
        if orchestrated and agent_count > 1 and timeout_seconds <= 120:
            return {
                "short_timeout_mode": True,
                "named_file_budget": 1,
                "additional_file_budget": 3,
                "shell_command_budget": 2,
            }
        if timeout_seconds <= 90:
            return {
                "short_timeout_mode": True,
                "named_file_budget": 2,
                "additional_file_budget": 4,
                "shell_command_budget": 4,
            }
        if timeout_seconds <= 180:
            return {
                "short_timeout_mode": True,
                "named_file_budget": 3,
                "additional_file_budget": 6,
                "shell_command_budget": 6,
            }
        return {
            "short_timeout_mode": False,
            "named_file_budget": 4,
            "additional_file_budget": 8,
            "shell_command_budget": 8,
        }

    @staticmethod
    def _read_only_short_timeout_budget(tool: ToolName, timeout_seconds: int) -> dict[str, int | bool]:
        focused_short_timeout_tools = {ToolName.PLAN, ToolName.RESEARCH, ToolName.BRAINSTORM}
        if tool in focused_short_timeout_tools and timeout_seconds <= 90:
            return {
                "short_timeout_mode": True,
                "named_file_budget": 2,
                "additional_file_budget": 4,
                "shell_command_budget": 4,
            }
        if tool in focused_short_timeout_tools and timeout_seconds <= 180:
            return {
                "short_timeout_mode": True,
                "named_file_budget": 3,
                "additional_file_budget": 6,
                "shell_command_budget": 6,
            }
        if timeout_seconds <= 90:
            return {
                "short_timeout_mode": True,
                "named_file_budget": 3,
                "additional_file_budget": 6,
                "shell_command_budget": 6,
            }
        if timeout_seconds <= 180:
            return {
                "short_timeout_mode": True,
                "named_file_budget": 4,
                "additional_file_budget": 8,
                "shell_command_budget": 8,
            }
        return {
            "short_timeout_mode": False,
            "named_file_budget": 6,
            "additional_file_budget": 12,
            "shell_command_budget": 10,
        }

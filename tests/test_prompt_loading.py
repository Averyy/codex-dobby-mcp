from pathlib import Path

from codex_dobby_mcp.models import DEFAULT_TIMEOUT_SECONDS, InvocationRequest, ReviewAgent, ToolName
from codex_dobby_mcp.prompts import PromptLoader


ASSETS_PROMPTS_ROOT = Path(__file__).resolve().parents[1] / "src" / "codex_dobby_mcp" / "assets" / "prompts"


def _request(**overrides) -> InvocationRequest:
    """Build an InvocationRequest bypassing the minimum timeout for prompt unit tests."""
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


def test_prompt_loader_expands_includes_and_renders_context() -> None:
    loader = PromptLoader(ASSETS_PROMPTS_ROOT)

    rendered = loader.render(
        tool=ToolName.REVERSE_ENGINEER,
        request=InvocationRequest(
            prompt="inspect the binary",
            files=["firmware.bin"],
            important_context="Use Ghidra if it helps",
            extra_roots=["/tmp/data"],
            danger=True,
        ),
        repo_root=Path("/repo"),
        sandbox_roots=[Path("/repo"), Path("/extra")],
        advisory_read_only_roots=[Path("/ro")],
        model="gpt-5.4",
        reasoning_effort="high",
    )

    assert "reverse-engineering worker" in rendered
    assert "Return a JSON object with exactly these fields" in rendered
    assert "firmware.bin" in rendered
    assert "/extra" in rendered
    assert "Danger mode: `true`" not in rendered
    assert "Danger mode: true" in rendered
    assert "startup and inspection calls in this workflow are explicitly allowed" in rendered
    assert "do not count as forbidden edits or note-writing" in rendered
    assert "Do not detour into helper-repo source inspection" in rendered
    assert "Do not broaden scope just to prove extra bridge internals" in rendered
    assert "prefer the mounted `bridge_mcp_ghidra.py` helper as a fallback" in rendered
    assert "immediately run the mounted helper against the connected bridge" in rendered


def test_review_prompt_renders_selected_agents() -> None:
    loader = PromptLoader(ASSETS_PROMPTS_ROOT)

    rendered = loader.render(
        tool=ToolName.REVIEW,
        request=_request(
            prompt="review the current diff",
            agents=[ReviewAgent.SECURITY, ReviewAgent.REGRESSION],
            timeout_seconds=180,
        ),
        repo_root=Path("/repo"),
        sandbox_roots=[Path("/repo")],
        advisory_read_only_roots=[],
        model="gpt-5.4",
        reasoning_effort="high",
    )

    assert "Selected review agents:" in rendered
    assert "Injected custom review agents available in this run:" in rendered
    assert "dobby_review_security (security)" in rendered
    assert "The parent review worker is an orchestrator, not a primary reviewer." in rendered
    assert "Your first substantive action should be to spawn the selected review subagents" in rendered
    assert "Spawn exactly one read-only Codex subagent for each selected custom review agent below." in rendered
    assert "When you call `spawn_agent`, specify the exact custom agent name from the assignment block and set `fork_context=false`." in rendered
    assert "Each child message must begin with the exact `Required custom agent:` and `Assigned lens:` lines" in rendered
    assert "If more than one review agent is selected, spawn those subagents in parallel" in rendered
    assert "Record the spawned child thread id from each `spawn_agent` completion" in rendered
    assert "without inheriting the parent orchestrator context" in rendered
    assert "start from Claude's named files and expand outward only as far as needed" in rendered
    assert "start with the named implementation files before opening docs or tests" in rendered
    assert "large test files unless a specific contract or candidate issue requires that confirmation" in rendered
    assert "Timeout plan for this run: total budget `180` seconds." in rendered
    assert "Short-timeout mode for this run: `yes`." in rendered
    assert "start with at most `3` named files" in rendered
    assert "avoid more than `6` shell commands" in rendered
    assert "Reserve the last `60` seconds for wrap-up and final synthesis." in rendered
    assert "call `wait_agent` on all of them with `timeout_ms=120000`" in rendered
    assert "Never let the first `wait_agent` call consume the wrap-up reserve." in rendered
    assert "Treat that first `wait_agent` timeout as a soft deadline or request-end" in rendered
    assert "Treat empty or missing `agents_states` as incomplete." in rendered
    assert "call `send_input` with `interrupt=true` for each unfinished child id" in rendered
    assert "Dobby timeout approaching. Stop exploring now and return the best JSON you have immediately." in rendered
    assert "do not inspect more code, deliberate further, or send any message other than those wrap-up interrupts" in rendered
    assert "call `wait_agent` on the unfinished subagents with `timeout_ms=45000`" in rendered
    assert "If some child ids still have no completed result, note that those review lenses are incomplete" in rendered
    assert "Once `wait` returns completed results, stop exploring and emit the final JSON immediately." in rendered
    assert "If exactly one review subagent was selected, adapt that completed subagent output directly" in rendered
    assert "### Spawn `dobby_review_security`" in rendered
    assert "### Spawn `dobby_review_regression`" in rendered
    assert "Lens: regression and patterns" in rendered
    assert "Required agent type: `dobby_review_security`" in rendered
    assert "Call `spawn_agent` and select this exact injected custom agent type." in rendered
    assert "Set `fork_context=false`." in rendered
    assert "The child message must begin with these exact two lines" in rendered
    assert "`Required custom agent: dobby_review_security`" in rendered
    assert "`Assigned lens: security`" in rendered
    assert "Copy this exact opening into the child message before the rest of the assignment:" in rendered
    assert "do not substitute `default`, `worker`, `explorer`, or any other agent type" in rendered
    assert "start with Claude's named files and expand outward only as needed" in rendered
    assert "default to static code inspection" in rendered
    assert "do not run broad test suites or `uv run` from a read-only review" in rendered
    assert "start with at most 3 named files, read at most 6 additional files, and avoid more than 6 shell commands before synthesizing" in rendered
    assert "if the parent interrupts you to request wrap-up" in rendered
    assert "use `completeness` = `partial` if an interrupt forces you to return" in rendered
    assert "Spend the parent turn on broad code reading" in rendered
    assert "Do not use nested delegation." in rendered
    assert "generalist quick pass" not in rendered


def test_review_prompt_marks_entire_repo_when_files_are_omitted() -> None:
    loader = PromptLoader(ASSETS_PROMPTS_ROOT)

    rendered = loader.render(
        tool=ToolName.REVIEW,
        request=InvocationRequest(
            prompt="review the repo",
            agents=[ReviewAgent.CORRECTNESS],
        ),
        repo_root=Path("/repo"),
        sandbox_roots=[Path("/repo")],
        advisory_read_only_roots=[],
        model="gpt-5.4",
        reasoning_effort="medium",
    )

    assert "Relevant files from Claude:\n- (entire repo)" in rendered


def test_single_agent_review_prompt_uses_direct_lens_mode() -> None:
    loader = PromptLoader(ASSETS_PROMPTS_ROOT)

    rendered = loader.render(
        tool=ToolName.REVIEW,
        request=_request(
            prompt="review the current diff",
            agents=[ReviewAgent.CORRECTNESS],
            timeout_seconds=60,
        ),
        repo_root=Path("/repo"),
        sandbox_roots=[Path("/repo")],
        advisory_read_only_roots=[],
        model="gpt-5.4",
        reasoning_effort="high",
    )

    assert "Role: direct single-lens review worker." in rendered
    assert "Selected review lens:" in rendered
    assert "- correctness" in rendered
    assert "- dobby_review_correctness" in rendered
    assert "This review uses the selected lens directly instead of spawning subagents." in rendered
    assert "Do not call `spawn_agent`, `wait_agent`, or `send_input`." in rendered
    assert "Short-timeout mode for this run: `yes`." in rendered
    assert "Start with at most `2` named files before branching outward." in rendered
    assert "Read at most `4` additional files beyond Claude's named files" in rendered
    assert "Run at most `4` shell commands" in rendered
    assert "Start with the named implementation files before opening docs or tests." in rendered
    assert "Do not open `README.md`, repo-local instruction docs, or large test files unless you need them to confirm a specific contract or candidate issue." in rendered
    assert "Do not read git status, git diff, or unrelated tests by default." in rendered
    assert "Operate as a correctness-focused code reviewer." in rendered
    assert "Find bugs, logic errors, and edge cases." in rendered
    assert "Stop once you have the highest-signal findings." in rendered
    assert "If time is running short, or you have reached the exploration budget without a concrete issue" in rendered
    assert "Call `spawn_agent` and select this exact injected custom agent type." not in rendered


def test_multi_agent_review_prompt_uses_early_soft_deadline_on_short_timeouts() -> None:
    loader = PromptLoader(ASSETS_PROMPTS_ROOT)

    rendered = loader.render(
        tool=ToolName.REVIEW,
        request=_request(
            prompt="review the current diff",
            agents=[ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
            timeout_seconds=90,
        ),
        repo_root=Path("/repo"),
        sandbox_roots=[Path("/repo")],
        advisory_read_only_roots=[],
        model="gpt-5.4",
        reasoning_effort="high",
    )

    assert "Short-timeout mode for this run: `yes`." in rendered
    assert "start with at most `1` named files" in rendered
    assert "read at most `3` additional files" in rendered
    assert "avoid more than `2` shell commands" in rendered
    assert "start with at most 1 named files, read at most 3 additional files, and avoid more than 2 shell commands before synthesizing" in rendered
    assert "Reserve the last `70` seconds for wrap-up and final synthesis." in rendered
    assert "call `wait_agent` on all of them with `timeout_ms=20000`" in rendered
    assert "call `wait_agent` on the unfinished subagents with `timeout_ms=55000`" in rendered


def test_research_and_brainstorm_prompts_include_external_research_rules() -> None:
    loader = PromptLoader(ASSETS_PROMPTS_ROOT)

    research = loader.render(
        tool=ToolName.RESEARCH,
        request=InvocationRequest(prompt="compare current MCP servers", extra_roots=["/shared"]),
        repo_root=Path("/repo"),
        sandbox_roots=[Path("/repo")],
        advisory_read_only_roots=[],
        model="gpt-5.4",
        reasoning_effort="medium",
    )
    brainstorm = loader.render(
        tool=ToolName.BRAINSTORM,
        request=InvocationRequest(prompt="Should we build this idea?"),
        repo_root=Path("/repo"),
        sandbox_roots=[Path("/repo")],
        advisory_read_only_roots=[],
        model="gpt-5.4",
        reasoning_effort="high",
    )

    assert "fetchaller" in research
    assert "Sandbox-accessible roots:" in research
    assert "Writable roots:\n(none)" in research
    assert "Optional MCP integrations visible to this run:" in research
    assert "- fetchaller: no" in research
    assert "- ghidra: no" in research
    assert "Requested extra roots are read-only context only here." in research
    assert "Trust the sandbox-accessible roots and advisory read-only roots lists above" in research
    assert "In read-only roles, prefer static inspection" in research
    assert "Hard timeout budget: 600 seconds" in research
    assert "Leave enough time to emit the final JSON before the hard timeout" in research
    assert "Prefer codebase evidence over web research" in research
    assert "Should we build this?" in brainstorm
    assert "In read-only roles, prefer static inspection" in brainstorm
    assert "No code examples." in brainstorm
    assert "~/Code/temp/profile.md" not in brainstorm


def test_short_timeout_read_only_prompts_add_exploration_budget() -> None:
    loader = PromptLoader(ASSETS_PROMPTS_ROOT)

    rendered = loader.render(
        tool=ToolName.RESEARCH,
        request=_request(prompt="summarize this", timeout_seconds=60),
        repo_root=Path("/repo"),
        sandbox_roots=[Path("/repo")],
        advisory_read_only_roots=[],
        model="gpt-5.4",
        reasoning_effort="medium",
    )

    assert "Short-timeout mode for this run: yes." in rendered
    assert "start with at most 2 named files" in rendered
    assert "read at most 4 additional files" in rendered
    assert "avoid more than 4 shell commands" in rendered
    assert "do not open `README.md`, repo-local instruction docs, or large test files" in rendered
    assert "do not run `git status`, `git diff`, or broad codebase searches by default" in rendered
    assert "Start from Claude's named files and only widen scope when they are insufficient to answer the question." in rendered


def test_validate_prompt_renders_validation_role() -> None:
    loader = PromptLoader(ASSETS_PROMPTS_ROOT)

    rendered = loader.render(
        tool=ToolName.VALIDATE,
        request=InvocationRequest(prompt="Run the unit tests for the parser"),
        repo_root=Path("/repo"),
        sandbox_roots=[Path("/repo")],
        advisory_read_only_roots=[],
        model="gpt-5.4",
        reasoning_effort="medium",
    )

    assert "validation worker" in rendered
    assert "Run existing repo validation commands and report what happened" in rendered
    assert "Do not:" in rendered
    assert "Edit source files" in rendered
    assert "`completeness`" in rendered
    assert "`next_steps`" in rendered

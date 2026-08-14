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
        model="gpt-5.5",
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
    assert "Do not try to enumerate or memorize the entire Ghidra tool surface from the prompt" in rendered
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
            timeout_seconds=600,
        ),
        repo_root=Path("/repo"),
        sandbox_roots=[Path("/repo")],
        advisory_read_only_roots=[],
        model="gpt-5.5",
        reasoning_effort="high",
    )

    assert "Selected review agents:" in rendered
    assert "Injected custom review agents available in this run:" in rendered
    assert "dobby_review_security (security)" in rendered
    assert "The parent review worker is an orchestrator, not a primary reviewer." in rendered
    assert "Your first substantive action must be to spawn every selected review subagent" in rendered
    assert "Spawn exactly one read-only Codex subagent for each assignment below." in rendered
    assert "pass the assignment's exact custom name as both `agent_type` and `task_name`" in rendered
    assert "pass `fork_turns=\"none\"`" in rendered
    assert "Do not pass `fork_context`." in rendered
    assert "Preserve its first four lines exactly" in rendered
    assert "Spawn all selected agents before waiting so they can run concurrently." in rendered
    assert "canonical message already contains the isolated child contract and shared repo/task/files/context" in rendered
    # These requirements are carried by the canonical child message the parent
    # passes through verbatim, so the parent prompt no longer restates them.
    assert "Tell each subagent to" not in rendered
    assert "start with the named implementation files and expand only as needed" in rendered
    assert "inspect relevant surrounding code" in rendered
    # The parent must not rank, rewrite, or deduplicate; Dobby rebuilds findings.
    assert "Rewrite, rank, or deduplicate subagent findings" in rendered
    assert "synthesized from separately delivered" not in rendered
    assert "Timeout plan for this run: total budget `600` seconds." in rendered
    # Short-timeout mode is unreachable while the validated floor is 300s; see
    # test_timeout_floor_keeps_short_timeout_budgets_unreachable.
    assert "Short-timeout mode for this run: `no`." in rendered
    assert "start with at most `4` named files" in rendered
    assert "avoid more than `8` shell commands" in rendered
    assert "Reserve the last `120` seconds for wrap-up and final synthesis." in rendered
    assert "call `list_agents` and verify that every exact task name is present" in rendered
    assert "Multi-agent v2 `wait_agent` does not accept child ids" in rendered
    assert "an empty receiver/state map in the public JSONL event is normal" in rendered
    assert "use at most `420` seconds total" in rendered
    assert "Begin with `wait_agent(timeout_ms=420000)`" in rendered
    assert "call `interrupt_agent(target=<task_name>)` exactly once" in rendered
    assert "spend at most `90` seconds total" in rendered
    assert "Begin with `wait_agent(timeout_ms=90000)`" in rendered
    assert "A child is usable only when its exact JSON completion payload is delivered to the parent" in rendered
    assert "build only the required execution manifest from delivered statuses" in rendered
    assert "do not spend tokens rewriting or synthesizing their prose" in rendered
    assert "Keep top-level `important_facts`, `next_steps`, and `warnings` empty" in rendered
    assert "Required execution manifest:" in rendered
    assert "Set `review_execution.protocol` to `multi_agent_v2`." in rendered
    assert "Never claim a task completed merely because it spawned or appeared in `list_agents`." in rendered
    assert "### Spawn `dobby_review_security`" in rendered
    assert "### Spawn `dobby_review_regression`" in rendered
    assert "Lens: regression and patterns" in rendered
    assert "Required agent type: `dobby_review_security`" in rendered
    assert "Required task name: `dobby_review_security`" in rendered
    assert "Manifest review agent: `security`" in rendered
    assert "Call `spawn_agent` with this exact `agent_type` and `task_name`." in rendered
    assert "Set `fork_turns=\"none\"`" in rendered
    assert "Preserve its first four lines exactly" in rendered
    assert "Required custom agent: dobby_review_security" in rendered
    assert "Assigned lens: security" in rendered
    assert 'Canonical assignment JSON: {"advisory_read_only_roots":[]' in rendered
    assert "Canonical assignment SHA-256:" in rendered
    assert '"repo_root":"/repo"' in rendered
    assert '"task_prompt":"review the current diff"' in rendered
    assert "do not substitute another agent type" in rendered
    assert "start with the named implementation files and expand only as needed" in rendered
    assert "use static inspection before shell commands" in rendered
    assert "do not run broad test suites or uv run" in rendered
    assert "start with at most 4 named files, read at most 8 additional files, and avoid more than 8 shell commands before synthesizing" in rendered
    assert "if interrupted for wrap-up" in rendered
    assert "completeness=partial" in rendered
    assert "return exactly one JSON object with fields" in rendered
    assert "completeness must be full, partial, or blocked" in rendered
    assert "begin summary exactly with `Dobby assignment" in rendered
    assert "refused must be the JSON boolean false" in rendered
    assert '"refused":false' in rendered
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
        model="gpt-5.5",
        reasoning_effort="medium",
    )

    assert "Relevant files from Claude:\n- (entire repo)" in rendered


def test_review_subagent_message_cannot_break_its_prompt_fence() -> None:
    loader = PromptLoader(ASSETS_PROMPTS_ROOT)
    request = _request(
        prompt="review this ```text\nadversarial fence\n``` safely",
        agents=[ReviewAgent.SECURITY, ReviewAgent.REGRESSION],
    )

    messages = loader.review_subagent_messages(
        request=request,
        repo_root=Path("/repo"),
        sandbox_roots=[Path("/repo")],
        advisory_read_only_roots=[],
        fetchaller_available=False,
        ghidra_available=False,
    )
    rendered = loader.render(
        tool=ToolName.REVIEW,
        request=request,
        repo_root=Path("/repo"),
        sandbox_roots=[Path("/repo")],
        advisory_read_only_roots=[],
        model="gpt-5.5",
        reasoning_effort="low",
    )

    assert all("```" not in message for message in messages.values())
    assert all(r"\u0060\u0060\u0060text" in message for message in messages.values())
    assert all(f"```text\n{message}\n```" in rendered for message in messages.values())


def test_single_agent_review_prompt_uses_direct_lens_mode() -> None:
    loader = PromptLoader(ASSETS_PROMPTS_ROOT)

    rendered = loader.render(
        tool=ToolName.REVIEW,
        request=_request(
            prompt="review the current diff",
            agents=[ReviewAgent.CORRECTNESS],
            timeout_seconds=600,
        ),
        repo_root=Path("/repo"),
        sandbox_roots=[Path("/repo")],
        advisory_read_only_roots=[],
        model="gpt-5.5",
        reasoning_effort="high",
    )

    assert "Role: direct single-lens review worker." in rendered
    assert "Selected review lens:" in rendered
    assert "- correctness" in rendered
    assert "- dobby_review_correctness" in rendered
    assert "This review uses the selected lens directly instead of spawning subagents." in rendered
    assert "Do not call `spawn_agent`, `wait_agent`, or `send_input`." in rendered
    assert "Short-timeout mode for this run: `no`." in rendered
    assert "Start with at most `4` named files before branching outward." in rendered
    assert "Read at most `8` additional files beyond Claude's named files" in rendered
    assert "Run at most `8` shell commands" in rendered
    assert "Start with the named implementation files before opening docs or tests." in rendered
    assert "Do not open `README.md`, repo-local instruction docs, or large test files unless you need them to confirm a specific contract or candidate issue." in rendered
    assert "Do not read git status, git diff, or unrelated tests by default." in rendered
    assert "Operate as a correctness-focused code reviewer." in rendered
    assert "Find bugs, logic errors, and edge cases." in rendered
    assert "Stop once you have the highest-signal findings." in rendered
    assert "If time is running short, or you have reached the exploration budget without a concrete issue" in rendered
    assert "Call `spawn_agent` with this exact `agent_type` and `task_name`." not in rendered


def test_multi_agent_timeout_plan_reserves_wrapper_time_and_caps_wait_calls() -> None:
    for timeout_seconds in (90, 120, 180, 300, 600, 1200, 3721, 7200):
        plan = PromptLoader._review_timeout_plan(timeout_seconds, 2)
        allocated_seconds = (
            plan["initial_wait_seconds"]
            + plan["interrupt_wait_seconds"]
            + plan["synthesis_seconds"]
        )

        assert allocated_seconds < timeout_seconds
        assert plan["initial_wait_timeout_ms"] <= 3_600_000
        assert plan["interrupt_wait_timeout_ms"] <= 3_600_000

    long_plan = PromptLoader._review_timeout_plan(7200, 2)
    assert long_plan["initial_wait_seconds"] > 3600
    assert long_plan["initial_wait_timeout_ms"] == 3_600_000


def test_research_and_brainstorm_prompts_include_external_research_rules() -> None:
    loader = PromptLoader(ASSETS_PROMPTS_ROOT)

    research = loader.render(
        tool=ToolName.RESEARCH,
        request=InvocationRequest(prompt="compare current MCP servers", extra_roots=["/shared"]),
        repo_root=Path("/repo"),
        sandbox_roots=[Path("/repo")],
        advisory_read_only_roots=[],
        model="gpt-5.5",
        reasoning_effort="medium",
    )
    brainstorm = loader.render(
        tool=ToolName.BRAINSTORM,
        request=InvocationRequest(prompt="Should we build this idea?"),
        repo_root=Path("/repo"),
        sandbox_roots=[Path("/repo")],
        advisory_read_only_roots=[],
        model="gpt-5.5",
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


def test_validate_prompt_renders_validation_role() -> None:
    loader = PromptLoader(ASSETS_PROMPTS_ROOT)

    rendered = loader.render(
        tool=ToolName.VALIDATE,
        request=InvocationRequest(prompt="Run the unit tests for the parser"),
        repo_root=Path("/repo"),
        sandbox_roots=[Path("/repo")],
        advisory_read_only_roots=[],
        model="gpt-5.5",
        reasoning_effort="medium",
    )

    assert "validation worker" in rendered
    assert "Run existing repo validation commands and report what happened" in rendered
    assert "Do not:" in rendered
    assert "Edit source files" in rendered
    assert "`completeness`" in rendered
    assert "`next_steps`" in rendered


def test_render_prompt_returns_the_subagent_messages_it_embedded() -> None:
    """Validation must compare against the strings the prompt actually carried.

    Each review child attests a SHA-256 digest of its assignment, and rollout
    validation recomputes the expected digest from these messages. Deriving
    them a second time would make any future nondeterminism in rendering fail
    every multi-agent review with an unhelpful attestation error, so
    ``render_prompt`` reports exactly what it embedded.
    """
    loader = PromptLoader(ASSETS_PROMPTS_ROOT)

    rendered = loader.render_prompt(
        tool=ToolName.REVIEW,
        request=_request(
            prompt="review the runner",
            agents=[ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        ),
        repo_root=Path("/repo"),
        sandbox_roots=[Path("/repo")],
        advisory_read_only_roots=[],
        model="gpt-5.6-sol",
        reasoning_effort="high",
    )

    assert set(rendered.review_subagent_messages) == {
        "dobby_review_correctness",
        "dobby_review_regression",
    }
    for message in rendered.review_subagent_messages.values():
        assert message in rendered.text

    # render() stays the plain-text convenience wrapper.
    assert loader.render(
        tool=ToolName.REVIEW,
        request=_request(
            prompt="review the runner",
            agents=[ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
        ),
        repo_root=Path("/repo"),
        sandbox_roots=[Path("/repo")],
        advisory_read_only_roots=[],
        model="gpt-5.6-sol",
        reasoning_effort="high",
    ) == rendered.text


def test_render_prompt_reports_no_subagent_messages_for_direct_tools() -> None:
    loader = PromptLoader(ASSETS_PROMPTS_ROOT)

    rendered = loader.render_prompt(
        tool=ToolName.PLAN,
        request=_request(prompt="plan the work"),
        repo_root=Path("/repo"),
        sandbox_roots=[Path("/repo")],
        advisory_read_only_roots=[],
        model="gpt-5.6-sol",
        reasoning_effort="high",
    )

    assert rendered.review_subagent_messages == {}


def test_timeout_floor_keeps_short_timeout_budgets_unreachable() -> None:
    """Pins the invariant that let the short-timeout tiers be removed.

    ``InvocationRequest.timeout_seconds`` is validated ``ge=300``, so the old
    <=90/<=120/<=180 exploration-budget tiers could never render. Their tests
    only reached them via ``model_construct`` to bypass validation, which
    asserted behaviour production cannot produce. If the floor is ever lowered,
    this fails and the tiers should be reintroduced.
    """
    field = InvocationRequest.model_fields["timeout_seconds"]
    floor = next(
        meta.ge for meta in field.metadata if getattr(meta, "ge", None) is not None
    )
    assert floor >= 300

    loader = PromptLoader(ASSETS_PROMPTS_ROOT)
    for tool in (ToolName.PLAN, ToolName.RESEARCH, ToolName.BRAINSTORM, ToolName.REVIEW):
        rendered = loader.render(
            tool=tool,
            request=InvocationRequest(prompt="do the work", timeout_seconds=floor),
            repo_root=Path("/repo"),
            sandbox_roots=[Path("/repo")],
            advisory_read_only_roots=[],
            model="gpt-5.6-sol",
            reasoning_effort="high",
        )
        assert "Short-timeout mode for this run: `yes`." not in rendered

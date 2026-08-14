from pathlib import Path

import pytest

from codex_dobby_mcp.codex_cli import (
    _dobby_mcp_disable_overrides,
    _review_mcp_disable_overrides,
    build_codex_command,
)
from codex_dobby_mcp.models import (
    InvocationRequest,
    ReasoningEffort,
    ResolvedInvocation,
    ReviewAgent,
    RunArtifacts,
    ToolName,
)
from codex_dobby_mcp.review_agents import REVIEW_SUBAGENT_DEFAULT_REASONING_EFFORT


REVIEW_AGENTS_ROOT = Path(__file__).resolve().parents[1] / "src" / "codex_dobby_mcp" / "assets" / "codex_agents"


def make_artifacts(tmp_path: Path) -> RunArtifacts:
    run_dir = tmp_path / ".codex-dobby" / "runs" / "task-1"
    run_dir.mkdir(parents=True)
    return RunArtifacts(
        run_dir=run_dir,
        request_json=run_dir / "request.json",
        prompt_txt=run_dir / "prompt.txt",
        stdout_log=run_dir / "stdout.log",
        stderr_log=run_dir / "stderr.log",
        last_message_txt=run_dir / "last_message.txt",
        result_json=run_dir / "result.json",
        output_schema_json=run_dir / "output-schema.json",
        events_jsonl=run_dir / "events.jsonl",
    )


def make_spec(tmp_path: Path, tool: ToolName, *, danger: bool = False) -> ResolvedInvocation:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    extra_root = tmp_path / "extra"
    extra_root.mkdir()
    return ResolvedInvocation(
        tool=tool,
        request=InvocationRequest(prompt="do the thing", danger=danger),
        requested_timeout_seconds=900,
        repo_root=repo_root,
        model="gpt-5.5",
        reasoning_effort=ReasoningEffort.HIGH,
        sandbox_roots=[repo_root, extra_root],
        writable_roots=[repo_root, extra_root],
        advisory_read_only_roots=[],
        artifacts=make_artifacts(tmp_path),
    )


def test_read_only_tool_maps_to_read_only_sandbox(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, ToolName.PLAN)

    command = build_codex_command(
        spec,
        "/opt/homebrew/bin/codex",
        spec.artifacts.output_schema_json,
        REVIEW_AGENTS_ROOT,
    )

    assert command.sandbox_mode == "read-only"
    assert command.uses_full_auto is False
    assert command.emits_json_events is True
    assert "--full-auto" not in command.argv
    assert ["-s", "read-only"] == command.argv[command.argv.index("-s") : command.argv.index("-s") + 2]
    add_dirs = [Path(command.argv[index + 1]) for index, value in enumerate(command.argv) if value == "--add-dir"]
    assert add_dirs == [spec.artifacts.run_dir, spec.sandbox_roots[1]]
    assert command.argv[-1] == "-"


def test_validate_tool_maps_to_full_auto_sandbox(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, ToolName.VALIDATE)

    command = build_codex_command(
        spec,
        "/opt/homebrew/bin/codex",
        spec.artifacts.output_schema_json,
        REVIEW_AGENTS_ROOT,
    )

    assert command.sandbox_mode == "workspace-write"
    assert command.uses_full_auto is True
    assert "--full-auto" in command.argv
    assert "-s" not in command.argv
    config_values = [command.argv[index + 1] for index, value in enumerate(command.argv) if value == "-c"]
    assert "sandbox_workspace_write.network_access=true" not in config_values


def test_read_only_tool_includes_advisory_read_only_roots(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, ToolName.RESEARCH)
    advisory_root = tmp_path / "shared"
    advisory_root.mkdir()
    spec.sandbox_roots = [spec.repo_root]
    spec.writable_roots = [spec.repo_root]
    spec.advisory_read_only_roots = [advisory_root]

    command = build_codex_command(
        spec,
        "/opt/homebrew/bin/codex",
        spec.artifacts.output_schema_json,
        REVIEW_AGENTS_ROOT,
    )

    add_dirs = [Path(command.argv[index + 1]) for index, value in enumerate(command.argv) if value == "--add-dir"]

    assert add_dirs == [spec.artifacts.run_dir, advisory_root]


def test_build_tool_maps_to_full_auto_and_extra_roots(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, ToolName.BUILD)

    command = build_codex_command(
        spec,
        "/opt/homebrew/bin/codex",
        spec.artifacts.output_schema_json,
        REVIEW_AGENTS_ROOT,
    )

    assert command.sandbox_mode == "workspace-write"
    assert command.uses_full_auto is True
    assert command.emits_json_events is True
    assert "--full-auto" in command.argv
    assert "--add-dir" in command.argv
    assert "approval_policy=\"never\"" in command.argv


def test_danger_mode_maps_to_danger_full_access(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, ToolName.REVERSE_ENGINEER, danger=True)

    command = build_codex_command(
        spec,
        "/opt/homebrew/bin/codex",
        spec.artifacts.output_schema_json,
        REVIEW_AGENTS_ROOT,
    )

    assert command.sandbox_mode == "danger-full-access"
    assert command.uses_full_auto is False
    assert command.emits_json_events is True
    assert "--full-auto" not in command.argv
    assert ["-s", "danger-full-access"] == command.argv[command.argv.index("-s") : command.argv.index("-s") + 2]


def test_reverse_engineer_tool_without_live_socket_root_does_not_enable_network_access(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, ToolName.REVERSE_ENGINEER)

    command = build_codex_command(
        spec,
        "/opt/homebrew/bin/codex",
        spec.artifacts.output_schema_json,
        REVIEW_AGENTS_ROOT,
    )

    config_values = [command.argv[index + 1] for index, value in enumerate(command.argv) if value == "-c"]

    assert command.sandbox_mode == "workspace-write"
    assert command.uses_full_auto is True
    assert "--full-auto" in command.argv
    assert "sandbox_workspace_write.network_access=true" not in config_values
    assert not any(value.startswith("network.allow_unix_sockets=") for value in config_values)


def test_reverse_engineer_tool_enables_unix_socket_network_access_when_live_socket_root_present(
    tmp_path: Path,
) -> None:
    spec = make_spec(tmp_path, ToolName.REVERSE_ENGINEER)
    socket_root = tmp_path / "ghidra-mcp-avery"
    socket_root.mkdir()
    (socket_root / "ghidra-123.sock").write_text("", encoding="utf-8")
    spec.sandbox_roots = [spec.repo_root, socket_root]
    spec.writable_roots = [spec.repo_root, socket_root]

    command = build_codex_command(
        spec,
        "/opt/homebrew/bin/codex",
        spec.artifacts.output_schema_json,
        REVIEW_AGENTS_ROOT,
    )

    config_values = [command.argv[index + 1] for index, value in enumerate(command.argv) if value == "-c"]

    assert command.sandbox_mode == "workspace-write"
    assert command.uses_full_auto is True
    assert "--full-auto" in command.argv
    assert "sandbox_workspace_write.network_access=true" in config_values
    assert f'network.allow_unix_sockets=["{socket_root}"]' in config_values


def test_review_tool_injects_selected_custom_agents(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, ToolName.REVIEW)
    spec.request = InvocationRequest(
        prompt="review the diff",
        agents=[ReviewAgent.SECURITY, ReviewAgent.REGRESSION],
    )

    command = build_codex_command(
        spec,
        "/opt/homebrew/bin/codex",
        spec.artifacts.output_schema_json,
        REVIEW_AGENTS_ROOT,
    )
    config_values = [command.argv[index + 1] for index, value in enumerate(command.argv) if value == "-c"]

    assert command.sandbox_mode == "read-only"
    assert command.emits_json_events is True
    assert "features.multi_agent_v2=true" in config_values
    assert "agents.enabled=true" in config_values
    assert "agents.max_concurrent_threads_per_session=2" in config_values
    assert 'history.persistence="save-all"' in config_values
    assert "features.multi_agent=true" not in config_values
    assert not any(value.startswith("agents.max_depth=") for value in config_values)
    assert not any(value.startswith("agents.max_threads=") for value in config_values)
    assert any("agents.dobby_review_security.config_file=" in value for value in config_values)
    assert any("agents.dobby_review_security.description=" in value for value in config_values)
    assert 'agents.default_subagent_model="gpt-5.5"' in config_values
    assert any(
        value
        == f'agents.default_subagent_reasoning_effort="{REVIEW_SUBAGENT_DEFAULT_REASONING_EFFORT.value}"'
        for value in config_values
    )
    assert not any(value.startswith("agents.dobby_review_security.model=") for value in config_values)
    assert not any(
        value.startswith("agents.dobby_review_security.model_reasoning_effort=")
        for value in config_values
    )
    assert any("agents.dobby_review_regression.config_file=" in value for value in config_values)
    assert not any("agents.dobby_review_architecture.config_file=" in value for value in config_values)


def test_single_agent_review_runs_without_multi_agent_overrides(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, ToolName.REVIEW)
    spec.request = InvocationRequest(
        prompt="review the diff",
        agents=[ReviewAgent.CORRECTNESS],
    )

    command = build_codex_command(
        spec,
        "/opt/homebrew/bin/codex",
        spec.artifacts.output_schema_json,
        REVIEW_AGENTS_ROOT,
    )
    config_values = [command.argv[index + 1] for index, value in enumerate(command.argv) if value == "-c"]

    assert command.sandbox_mode == "read-only"
    assert command.emits_json_events is True
    assert "--json" in command.argv
    assert "features.multi_agent_v2=true" not in config_values
    assert not any("agents.dobby_review_correctness.config_file=" in value for value in config_values)


def test_review_tool_default_single_generalist_is_not_orchestrated(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, ToolName.REVIEW)

    command = build_codex_command(
        spec,
        "/opt/homebrew/bin/codex",
        spec.artifacts.output_schema_json,
        REVIEW_AGENTS_ROOT,
    )
    config_values = [command.argv[index + 1] for index, value in enumerate(command.argv) if value == "-c"]

    assert command.sandbox_mode == "read-only"
    assert command.emits_json_events is True
    assert "--json" in command.argv
    assert "features.multi_agent_v2=true" not in config_values


def test_multi_agent_review_children_inherit_explicit_parent_model_and_use_requested_reasoning(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, ToolName.REVIEW)
    spec.request = InvocationRequest(
        prompt="review the diff",
        agents=[ReviewAgent.SECURITY, ReviewAgent.REGRESSION],
        model="gpt-5.5",
        reasoning_effort=ReasoningEffort.HIGH,
    )

    command = build_codex_command(
        spec,
        "/opt/homebrew/bin/codex",
        spec.artifacts.output_schema_json,
        REVIEW_AGENTS_ROOT,
    )
    config_values = [command.argv[index + 1] for index, value in enumerate(command.argv) if value == "-c"]

    assert 'agents.default_subagent_model="gpt-5.5"' in config_values
    assert 'agents.default_subagent_reasoning_effort="high"' in config_values
    assert not any(value.startswith("agents.dobby_review_security.model=") for value in config_values)
    assert not any(value.startswith("agents.dobby_review_security.model_reasoning_effort=") for value in config_values)


def test_dobby_mcp_server_entries_are_disabled_for_child_runs(tmp_path: Path) -> None:
    home_config = tmp_path / "config.toml"
    home_config.write_text(
        """
[mcp_servers.fetchaller]
command = "python"
args = ["-m", "fetchaller.main"]

[mcp_servers.dobby]
command = "uvx"
args = ["codex-dobby-mcp"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    repo_root = tmp_path / "repo"
    (repo_root / ".codex").mkdir(parents=True)
    ((repo_root / ".codex") / "config.toml").write_text(
        """
[mcp_servers.local_dobby]
command = "python"
args = ["-m", "codex_dobby_mcp.server"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    overrides = _dobby_mcp_disable_overrides(repo_root, home_config)

    assert "mcp_servers.dobby.enabled=false" in overrides
    assert "mcp_servers.local_dobby.enabled=false" in overrides
    assert not any("fetchaller" in override for override in overrides)


def test_review_disables_every_mcp_including_aliased_dobby_wrappers(
    tmp_path: Path,
) -> None:
    home_config = tmp_path / "config.toml"
    home_config.write_text(
        """
[mcp_servers.fetchaller]
command = "python"
args = ["-m", "fetchaller.main"]

[mcp_servers.helper]
command = "/usr/local/bin/company-agent-wrapper"
args = ["--profile", "review"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    repo_root = tmp_path / "repo"
    (repo_root / ".codex").mkdir(parents=True)
    (repo_root / ".codex" / "config.toml").write_text(
        """
[mcp_servers."repo.helper"]
url = "https://example.invalid/mcp"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    overrides = _review_mcp_disable_overrides(repo_root, home_config)

    assert overrides == [
        "mcp_servers.fetchaller.enabled=false",
        "mcp_servers.helper.enabled=false",
        'mcp_servers."repo.helper".enabled=false',
    ]


def test_review_command_applies_empty_mcp_allowlist(tmp_path: Path) -> None:
    home_config = tmp_path / "config.toml"
    home_config.write_text(
        """
[mcp_servers.helper]
command = "/usr/local/bin/company-agent-wrapper"
args = ["--profile", "review"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    spec = make_spec(tmp_path, ToolName.REVIEW)

    command = build_codex_command(
        spec,
        "/opt/homebrew/bin/codex",
        spec.artifacts.output_schema_json,
        REVIEW_AGENTS_ROOT,
        home_config,
    )

    config_values = [
        command.argv[index + 1]
        for index, value in enumerate(command.argv)
        if value == "-c"
    ]
    assert "mcp_servers.helper.enabled=false" in config_values


def test_dobby_mcp_disable_overrides_quote_dynamic_toml_key_segments(
    tmp_path: Path,
) -> None:
    home_config = tmp_path / "config.toml"
    home_config.write_text(
        """
[mcp_servers."codex.dobby"]
command = "uvx"
args = ["codex-dobby-mcp"]

[mcp_servers."local dobby"]
command = "python"
args = ["-m", "codex_dobby_mcp.server"]

[mcp_servers.'quoted"dobby']
command = "uvx"
args = ["codex-dobby-mcp"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    overrides = _dobby_mcp_disable_overrides(tmp_path, home_config)

    assert 'mcp_servers."codex.dobby".enabled=false' in overrides
    assert 'mcp_servers."local dobby".enabled=false' in overrides
    assert 'mcp_servers."quoted\\\"dobby".enabled=false' in overrides


def test_dobby_mcp_disable_overrides_respects_codex_home_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "alt-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        """
[mcp_servers.dobby]
command = "uvx"
args = ["codex-dobby-mcp"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    overrides = _dobby_mcp_disable_overrides(repo_root)

    assert overrides == ["mcp_servers.dobby.enabled=false"]


def test_build_codex_command_uses_explicit_home_config_path_for_dobby_disables(tmp_path: Path) -> None:
    seeded_config = tmp_path / "seeded-config.toml"
    seeded_config.write_text(
        """
[mcp_servers.dobby]
command = "uvx"
args = ["codex-dobby-mcp"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    spec = make_spec(tmp_path, ToolName.PLAN)

    command = build_codex_command(
        spec,
        "/opt/homebrew/bin/codex",
        spec.artifacts.output_schema_json,
        REVIEW_AGENTS_ROOT,
        seeded_config,
    )

    config_values = [command.argv[index + 1] for index, value in enumerate(command.argv) if value == "-c"]

    assert "mcp_servers.dobby.enabled=false" in config_values


def test_review_tool_requires_explicit_agent_assets_root_for_orchestrated(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, ToolName.REVIEW)
    spec.request = InvocationRequest(
        prompt="review the diff",
        agents=[ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    try:
        build_codex_command(spec, "/opt/homebrew/bin/codex", spec.artifacts.output_schema_json)
    except RuntimeError as exc:
        assert "explicit review agents root" in str(exc)
    else:
        raise AssertionError("expected RuntimeError when review agent assets root is missing")

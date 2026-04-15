from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tomllib

from codex_dobby_mcp.models import READ_ONLY_TOOLS, ReasoningEffort, ResolvedInvocation, ToolName
from codex_dobby_mcp.review_agents import (
    REVIEW_SUBAGENT_DEFAULT_MODEL,
    REVIEW_SUBAGENT_DEFAULT_REASONING_EFFORT,
    review_uses_orchestrator,
    selected_review_agent_definitions,
)


@dataclass(frozen=True)
class CodexCommand:
    argv: list[str]
    uses_full_auto: bool
    sandbox_mode: str
    emits_json_events: bool


def build_codex_command(
    spec: ResolvedInvocation,
    codex_binary: str,
    output_schema_path: Path,
    review_agents_root: Path | None = None,
    home_config_path: Path | None = None,
) -> CodexCommand:
    review_uses_json_orchestrator = spec.tool == ToolName.REVIEW and review_uses_orchestrator(spec.request.agents)
    reasoning_override = f"model_reasoning_effort={_toml_string(spec.reasoning_effort.value)}"
    approval_override = f"approval_policy={_toml_string('never')}"
    argv = [
        codex_binary,
        "exec",
        "-C",
        str(spec.repo_root),
        "--color",
        "never",
        "--output-schema",
        str(output_schema_path),
        "--output-last-message",
        str(spec.artifacts.last_message_txt),
        "-m",
        spec.model,
        "-c",
        reasoning_override,
        "-c",
        approval_override,
    ]
    for override in _dobby_mcp_disable_overrides(spec.repo_root, home_config_path=home_config_path):
        argv.extend(["-c", override])

    if review_uses_json_orchestrator:
        if review_agents_root is None:
            raise RuntimeError("Review runs require an explicit review agents root")
        argv.extend(_review_agent_overrides(spec, review_agents_root))
        argv.append("--json")

    if spec.tool in READ_ONLY_TOOLS:
        argv.extend(["-s", "read-only"])
        for extra_root in _read_only_add_dirs(spec):
            argv.extend(["--add-dir", str(extra_root)])
        return CodexCommand(
            argv=argv + ["-"],
            uses_full_auto=False,
            sandbox_mode="read-only",
            emits_json_events=review_uses_json_orchestrator,
        )

    if spec.request.danger:
        argv.extend(["-s", "danger-full-access"])
        sandbox_mode = "danger-full-access"
        uses_full_auto = False
    else:
        argv.append("--full-auto")
        sandbox_mode = "workspace-write"
        uses_full_auto = True
        if spec.tool == ToolName.REVERSE_ENGINEER:
            for override in _reverse_engineer_network_overrides(spec):
                argv.extend(["-c", override])

    for extra_root in spec.writable_roots:
        if extra_root != spec.repo_root:
            argv.extend(["--add-dir", str(extra_root)])

    return CodexCommand(
        argv=argv + ["-"],
        uses_full_auto=uses_full_auto,
        sandbox_mode=sandbox_mode,
        emits_json_events=False,
    )


def _review_agent_overrides(spec: ResolvedInvocation, agents_root: Path) -> list[str]:
    registrations = selected_review_agent_definitions(spec.request.agents)
    overrides: list[tuple[str, str]] = [
        ("features.multi_agent", "true"),
        ("agents.max_depth", "1"),
        ("agents.max_threads", str(len(registrations) + 1)),
    ]

    for agent in registrations:
        config_path = (agents_root / agent.filename).resolve()
        if not config_path.exists():
            raise RuntimeError(f"Missing injected review agent definition: {config_path}")
        overrides.extend(
            [
                (f"agents.{agent.codex_name}.description", _toml_string(agent.description)),
                (f"agents.{agent.codex_name}.config_file", _toml_string(str(config_path))),
            ]
        )
        if spec.request.model is None:
            overrides.append((f"agents.{agent.codex_name}.model", _toml_string(REVIEW_SUBAGENT_DEFAULT_MODEL)))

        child_reasoning_effort = spec.request.reasoning_effort or REVIEW_SUBAGENT_DEFAULT_REASONING_EFFORT
        if spec.request.timeout_seconds <= 120:
            child_reasoning_effort = ReasoningEffort.LOW
        overrides.append(
            (
                f"agents.{agent.codex_name}.model_reasoning_effort",
                _toml_string(child_reasoning_effort.value),
            )
        )

    argv: list[str] = []
    for key, value in overrides:
        argv.extend(["-c", f"{key}={value}"])
    return argv


def _read_only_add_dirs(spec: ResolvedInvocation) -> list[Path]:
    ordered_roots: list[Path] = []

    def remember(path: Path) -> None:
        if path not in ordered_roots:
            ordered_roots.append(path)

    remember(spec.artifacts.run_dir)
    for extra_root in spec.sandbox_roots:
        if extra_root != spec.repo_root:
            remember(extra_root)
    for extra_root in spec.advisory_read_only_roots:
        remember(extra_root)

    return ordered_roots


def _reverse_engineer_network_overrides(spec: ResolvedInvocation) -> list[str]:
    socket_roots = [
        root
        for root in spec.writable_roots
        if root != spec.repo_root and _looks_like_live_socket_root(root)
    ]
    if not socket_roots:
        return []

    return [
        "sandbox_workspace_write.network_access=true",
        f"network.allow_unix_sockets={json.dumps([str(root) for root in socket_roots])}",
    ]


def _looks_like_live_socket_root(path: Path) -> bool:
    try:
        return path.is_dir() and any(path.glob("*.sock"))
    except OSError:
        return False


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _dobby_mcp_disable_overrides(repo_root: Path, home_config_path: Path | None = None) -> list[str]:
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    config_paths = [
        home_config_path or (codex_home / "config.toml"),
        repo_root / ".codex" / "config.toml",
    ]
    overrides: list[str] = []
    seen: set[str] = set()

    for config_path in config_paths:
        if not config_path.exists():
            continue
        try:
            payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        servers = payload.get("mcp_servers")
        if not isinstance(servers, dict):
            continue
        for server_name, server_config in servers.items():
            if not isinstance(server_name, str) or server_name in seen:
                continue
            if _looks_like_dobby_server(server_name, server_config):
                overrides.append(f"mcp_servers.{server_name}.enabled=false")
                seen.add(server_name)

    return overrides


def _looks_like_dobby_server(server_name: str, server_config: object) -> bool:
    fragments = [server_name]
    if isinstance(server_config, dict):
        for key in ("command", "url"):
            value = server_config.get(key)
            if isinstance(value, str):
                fragments.append(value)
        args = server_config.get("args")
        if isinstance(args, list):
            fragments.extend(str(item) for item in args)

    haystack = " ".join(fragments).lower()
    return any(token in haystack for token in ("codex-dobby-mcp", "codex_dobby_mcp"))

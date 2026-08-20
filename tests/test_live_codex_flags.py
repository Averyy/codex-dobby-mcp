from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest

from codex_dobby_mcp.codex_cli import build_codex_command
from codex_dobby_mcp.models import InvocationRequest, ToolName
from codex_dobby_mcp.server import create_runner


REPO_ROOT = Path(__file__).resolve().parents[1]

# danger is only exposed on the mutating tools, so only those combinations
# exist to test; generating the impossible ones just to skip them would leave
# skips in the CI live lane, where any skip of a live test is a failure.
_DANGER_TOOLS = (ToolName.BUILD, ToolName.REVERSE_ENGINEER)
_FLAG_CASES = [(tool, False) for tool in ToolName] + [
    (tool, True) for tool in _DANGER_TOOLS
]


def _accepted_flags(codex_binary: str) -> set[str]:
    """Every long/short option `codex exec` advertises in its help output."""
    help_text = subprocess.run(
        [codex_binary, "exec", "--help"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    return set(re.findall(r"(?<![\w-])(--[a-z0-9][a-z0-9-]*|-[a-zA-Z])(?![\w-])", help_text))


@pytest.mark.live
@pytest.mark.parametrize(("tool", "danger"), _FLAG_CASES)
def test_generated_flags_are_accepted_by_the_installed_codex(
    codex_binary: str, tool: ToolName, danger: bool
) -> None:
    """Every flag Dobby emits must exist in the installed codex CLI.

    Dobby passed `--full-auto` for years after codex-cli removed it, so every
    mutating run died instantly with "unexpected argument" while the unit tests
    happily asserted the flag was present. Unit tests cannot catch that; only
    the real CLI can say what it accepts.
    """
    runner = create_runner(REPO_ROOT)
    spec = runner.prepare(
        tool,
        InvocationRequest(
            prompt="flag surface check",
            repo_root=str(REPO_ROOT),
            danger=danger,
        ),
    )
    command = build_codex_command(
        spec,
        codex_binary,
        spec.artifacts.output_schema_json,
        runner.review_agents_root,
    )

    accepted = _accepted_flags(codex_binary)
    emitted = {
        argument
        for argument in command.argv
        if argument.startswith("-") and argument != "-"
    }
    unknown = sorted(argument for argument in emitted if argument not in accepted)

    assert not unknown, (
        f"{tool.value} (danger={danger}) emits flags the installed codex "
        f"does not accept: {unknown}"
    )


@pytest.mark.live
def test_sandbox_values_are_accepted_by_the_installed_codex(
    codex_binary: str,
) -> None:
    help_text = subprocess.run(
        [codex_binary, "exec", "--help"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout

    for value in ("read-only", "workspace-write", "danger-full-access"):
        assert value in help_text, f"codex no longer advertises sandbox mode {value}"

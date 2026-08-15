import pytest

from codex_dobby_mcp.models import RECURSION_GUARD_ENV
from codex_dobby_mcp.safeguards import RecursionGuardError, child_environment, ensure_not_recursive


def test_recursion_guard_refuses_nested_dobby_env() -> None:
    with pytest.raises(RecursionGuardError):
        ensure_not_recursive({RECURSION_GUARD_ENV: "1"})


def test_child_environment_sets_guard() -> None:
    env = child_environment({})
    assert env[RECURSION_GUARD_ENV] == "1"


def test_child_environment_can_filter_and_override_env() -> None:
    env = child_environment(
        {"PATH": "/bin", "SECRET_TOKEN": "nope"},
        include={"PATH"},
        overrides={"CODEX_HOME": "/tmp/codex-home"},
    )

    assert env == {
        "PATH": "/bin",
        "CODEX_HOME": "/tmp/codex-home",
        RECURSION_GUARD_ENV: "1",
    }


@pytest.mark.asyncio
async def test_runner_refuses_to_run_inside_a_dobby_worker(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must still stop a real run when the ambient variable is set.

    conftest clears the inherited variable so the suite can run inside a Dobby
    worker, which means nothing else asserts that the runner itself honours it.
    Without this, disabling the guard entirely would keep every test green.
    """
    import subprocess
    from pathlib import Path

    from codex_dobby_mcp.models import InvocationRequest, ToolName
    from codex_dobby_mcp.server import create_runner

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)

    monkeypatch.setenv(RECURSION_GUARD_ENV, "1")
    runner = create_runner(repo_root)

    with pytest.raises(RecursionGuardError):
        await runner.run(
            ToolName.PLAN,
            InvocationRequest(prompt="inspect", repo_root=str(repo_root)),
        )

    # Nothing was launched: no run directory, no artifacts.
    runs_root = Path(repo_root) / ".codex-dobby" / "runs"
    assert not runs_root.exists() or not list(runs_root.iterdir())

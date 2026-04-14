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

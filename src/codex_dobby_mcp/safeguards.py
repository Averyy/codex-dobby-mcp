from __future__ import annotations

import os
from collections.abc import Mapping

from codex_dobby_mcp.models import RECURSION_GUARD_ENV


class RecursionGuardError(RuntimeError):
    pass


def ensure_not_recursive(env: Mapping[str, str] | None = None) -> None:
    current_env = env or os.environ
    if current_env.get(RECURSION_GUARD_ENV):
        raise RecursionGuardError(
            "codex-dobby-mcp refused to run because it is already running inside a Dobby worker process"
        )


def child_environment(
    env: Mapping[str, str] | None = None,
    *,
    include: set[str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    current_env = env or os.environ
    child_env = (
        {key: value for key, value in current_env.items() if include is None or key in include}
        if include is not None
        else dict(current_env)
    )
    child_env[RECURSION_GUARD_ENV] = "1"
    if overrides:
        child_env.update(overrides)
    return child_env

from __future__ import annotations

import tempfile

import pytest

from codex_dobby_mcp.models import RECURSION_GUARD_ENV


@pytest.fixture(autouse=True)
def _clear_ambient_recursion_guard(monkeypatch: pytest.MonkeyPatch):
    """Let the suite run inside a Dobby worker.

    ``ensure_not_recursive`` reads the ambient environment, so running these
    tests inside a Dobby child — exactly what ``validate`` on this repository
    does — made every test that drives a runner fail with RecursionGuardError.
    Seventy failures that say nothing about the code under test.

    Running the suite is not a nested Dobby invocation, and the guard's own
    behaviour is covered by passing explicit environments to
    ``ensure_not_recursive`` and ``child_environment``, so clearing the
    inherited variable costs no coverage.
    """
    monkeypatch.delenv(RECURSION_GUARD_ENV, raising=False)


@pytest.fixture(autouse=True)
def _contained_private_runtime_root(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
):
    """Keep private child runtimes out of the shared system temp.

    ``private_runtime_root`` defaults to ``tempfile.gettempdir()``, so any test
    that drives a real run puts a credential-shaped directory there. Most are
    cleaned on the way out, but the tests that deliberately simulate a failed
    private-runtime deletion cannot be — that is the state they are asserting —
    so each suite run left two behind in a location shared with everything else
    on the machine.

    Only ``tempfile.tempdir`` is redirected, not ``TMPDIR``: some tests assert
    on ``TMPDIR`` directly when resolving reverse-engineer writable roots.
    """
    monkeypatch.setattr(
        tempfile, "tempdir", str(tmp_path_factory.mktemp("private-runtime"))
    )

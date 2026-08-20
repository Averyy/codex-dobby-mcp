from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from codex_dobby_mcp.models import RECURSION_GUARD_ENV


def _live_lane_required() -> bool:
    """True when the environment demands the live codex tests actually run.

    CI's live lane sets ``DOBBY_REQUIRE_LIVE=1``. The lane exists because the
    ``--full-auto`` regression hid behind exactly one behaviour: tests that
    validate against the real codex CLI skipping when the CLI is absent, while
    the suite still reports green. Under this flag, "codex is missing" is a
    broken lane, not an excusable skip.
    """
    return os.environ.get("DOBBY_REQUIRE_LIVE") == "1"


@pytest.fixture
def codex_binary() -> str:
    """The real codex CLI, resolved at test time.

    Without ``DOBBY_REQUIRE_LIVE=1`` (a contributor's machine), a missing
    binary skips the test exactly as before. With it (CI's live lane), a
    missing binary is a hard failure — an uninstalled or unlocatable codex
    must never masquerade as a green lane. A binary that resolves but does
    not run correctly fails the test in both modes via the subprocess call.
    """
    binary = os.environ.get("CODEX_BINARY") or shutil.which("codex")
    if binary is None:
        if _live_lane_required():
            pytest.fail(
                "DOBBY_REQUIRE_LIVE=1 but no codex CLI was found: CODEX_BINARY "
                "is unset and 'codex' is not on PATH. The live lane must fail, "
                "not skip, when the binary it exists to exercise is missing."
            )
        pytest.skip("codex CLI not on PATH")
    return binary


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """Under DOBBY_REQUIRE_LIVE=1, a skipped no-auth live test is a failure.

    The ``codex_binary`` fixture already fails loud, but only for tests that
    use it. This hook closes the remaining hole structurally: any test marked
    ``live`` (and not ``live_auth``, which legitimately skips without
    credentials) that reports *skipped* for any reason — a new skipif, an
    importorskip, a stray ``pytest.skip`` — is converted into a failure, so
    the lane can never silently degrade back into the pre-``--full-auto``
    state where "never ran" looked identical to "passed".
    """
    report = yield
    if (
        report.skipped
        and _live_lane_required()
        and item.get_closest_marker("live") is not None
        and item.get_closest_marker("live_auth") is None
    ):
        reason = (
            report.longrepr[2]
            if isinstance(report.longrepr, tuple)
            else report.longrepr
        )
        report.outcome = "failed"
        report.longrepr = (
            f"DOBBY_REQUIRE_LIVE=1: live test {item.nodeid} was skipped "
            f"instead of running ({reason}). The live lane treats any skip "
            "of a no-auth live test as a failure."
        )
    return report


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

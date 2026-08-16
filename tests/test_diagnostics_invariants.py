"""Diagnostic and normalisation helpers surfaced by mutation testing.

These decide what the caller is told went wrong. A wrong value here does not
corrupt a review verdict, but it does produce a confident, wrong explanation —
and for a quarantined review the underlying rollouts are deleted moments later,
so the message is often the only surviving account.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from codex_dobby_mcp.models import GhidraUsageMode, InvocationRequest, ToolName
from codex_dobby_mcp.runner import (
    _codex_home_permission_issue,
    _collect_sandbox_violations,
    _extract_access_path,
    _ghidra_usage_summary,
    _looks_like_code_or_test_snippet,
    _looks_like_json_fragment,
    _sandbox_violation_from_line,
    _seconds_remaining,
    _snapshot_uses_metadata,
)


# --------------------------------------------------------- sandbox violations
@pytest.mark.parametrize(
    "line",
    [
        "sandbox blocked write to /etc/hosts",
        "Sandbox denied network access",
        "operation not permitted: writing /usr/lib/x",
        "permission denied while creating /var/db/y",
        "read-only file system: mkdir /opt/z",
    ],
)
def test_real_violations_are_reported_from_stderr(line: str) -> None:
    assert _sandbox_violation_from_line(line, allow_plain_text=True) is not None


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "everything is fine",
        # A bare permission phrase with no resource-ish token is not a violation.
        "permission denied",
    ],
)
def test_innocuous_lines_are_not_violations(line: str) -> None:
    assert _sandbox_violation_from_line(line, allow_plain_text=True) is None


@pytest.mark.parametrize(
    "line",
    ["{", "}", "[", "]", "},", "],", '"sandbox blocked write to /etc/hosts'],
)
def test_json_fragments_are_recognised(line: str) -> None:
    """Kills the two `return True` constants and the `":" not in line` guard.

    Pretty-printed JSON on stderr would otherwise have its fragments read as
    prose and reported as sandbox violations.
    """
    assert _looks_like_json_fragment(line) is True
    assert _sandbox_violation_from_line(line, allow_plain_text=True) is None


@pytest.mark.parametrize(
    "line",
    ['"key": "sandbox blocked write to /x"', "not a fragment", "{ok}"],
)
def test_non_fragments_are_not_misread_as_json(line: str) -> None:
    """A quoted line that *does* contain a colon is a JSON pair, not a fragment."""
    assert _looks_like_json_fragment(line) is False


@pytest.mark.parametrize(
    "line",
    [
        "assert x == 'sandbox blocked write to /tmp/a'",
        "if a != b: sandbox denied write /tmp/a",
        "def f() -> None: sandbox denied write /tmp/a",
        "(sandbox blocked write to /tmp/a)",
        "[sandbox blocked write to /tmp/a]",
        "raise RuntimeError('sandbox blocked write /tmp/a')",
        "import sandbox_blocked_write_denied /tmp/a",
        # These match no code prefix and no bracket wrapping, so they are the
        # only cases that actually exercise the ->/==/!= operator test.
        "value == expected sandbox denied write /tmp/a",
        "a != b sandbox denied write /tmp/a",
        "f -> g sandbox denied write /tmp/a",
    ],
)
def test_code_and_test_snippets_are_not_violations(line: str) -> None:
    """A worker printing its own source must not be reported as a violation."""
    assert _looks_like_code_or_test_snippet(line) is True
    assert _sandbox_violation_from_line(line, allow_plain_text=True) is None


def test_stdout_requires_structured_json_but_stderr_accepts_prose() -> None:
    """Kills `not allow_plain_text or _looks_like_json_fragment(...)` -> `and`.

    Prose on stdout is worker output, not a sandbox report, so only stderr may
    contribute plain-text violations.
    """
    prose = "sandbox blocked write to /etc/hosts"
    assert _sandbox_violation_from_line(prose, allow_plain_text=False) is None
    assert _sandbox_violation_from_line(prose, allow_plain_text=True) == prose

    # `message` is one of the keys the candidate extractor reads; `msg` is not.
    structured = '{"message": "sandbox denied write to /etc/hosts"}'
    assert _sandbox_violation_from_line(structured, allow_plain_text=False) is not None


def test_a_bare_json_string_payload_is_still_inspected() -> None:
    """Kills `if not candidates and isinstance(payload, str):` -> `drop not`."""
    assert _sandbox_violation_from_line(
        '"sandbox blocked write to /etc/hosts"', allow_plain_text=False
    ) == "sandbox blocked write to /etc/hosts"


def test_violations_are_deduplicated_across_both_streams() -> None:
    """Kills `violation is None or violation in seen` -> `and`."""
    line = "sandbox blocked write to /etc/hosts"
    out = _collect_sandbox_violations(f"{line}\n{line}\n", "")
    assert out == [line]

    # The same text on both streams still reports once, and stdout prose is
    # ignored entirely, so this also pins the allow_plain_text=False call.
    assert _collect_sandbox_violations(line, line) == [line]
    assert _collect_sandbox_violations("", line) == []

    # The stdout loop has its own dedupe. Prose there returns None and
    # short-circuits before the `in seen` test, so only a structured payload
    # repeated on stdout can pin it.
    structured = '{"message": "sandbox denied write to /etc/hosts"}'
    assert _collect_sandbox_violations("", f"{structured}\n{structured}\n") == [
        "sandbox denied write to /etc/hosts"
    ]


def test_sandbox_word_needs_a_blocking_verb() -> None:
    """Kills `_contains_word(lower, "sandbox") and (...)` -> `or`."""
    assert _sandbox_violation_from_line(
        "sandbox mode is workspace-write", allow_plain_text=True
    ) is None
    assert _sandbox_violation_from_line(
        "sandbox violation while writing", allow_plain_text=True
    ) is not None


# ------------------------------------------------- codex home permission hint
def test_permission_issue_requires_both_a_denial_and_a_codex_home_subject() -> None:
    """Kills the `any(token in lower ...)` guard and all three `not in` checks.

    This warning was previously emitted for unrelated permission errors, which
    told users to fix a Codex-home problem they did not have.
    """
    # A denial that is not about Codex's own state directory: no warning.
    assert _codex_home_permission_issue([], stderr="permission denied: /etc/shadow") is None
    # A Codex-home mention with no denial: no warning.
    assert _codex_home_permission_issue([], stderr="codex_home is /Users/x/.codex") is None


@pytest.mark.parametrize(
    "text, expect_subject",
    [
        ("permission denied reading codex_home at /Users/x/.codex",
         "its Codex state directory"),
        ("operation not permitted writing session files at /Users/x/.codex/sessions",
         "its session files"),
        ("read-only file system: codex cannot access at /Users/x/.codex",
         "its Codex state directory"),
    ],
)
def test_permission_issue_names_the_path_and_subject(text: str, expect_subject: str) -> None:
    message = _codex_home_permission_issue([], stderr=text)
    assert message is not None
    assert expect_subject in message
    assert "/Users/x/.codex" in message


@pytest.mark.parametrize(
    "text, expected",
    [
        ("failed at /Users/x/.codex/auth.json", "/Users/x/.codex/auth.json"),
        ("failed at ~/.codex/sessions", "~/.codex/sessions"),
        ("trailing punctuation at /tmp/a.", "/tmp/a"),
        ("no marker but /Users/x/.codex/config.toml here",
         "/Users/x/.codex/config.toml"),
        ("nothing path-like at all", None),
    ],
)
def test_access_path_extraction(text: str, expected) -> None:
    """Kills `if match is None:` -> `is not None` on the fallback pattern."""
    assert _extract_access_path(text) == expected


# ------------------------------------------------------------ ghidra summary
@pytest.mark.parametrize(
    "mode, fragment",
    [
        (GhidraUsageMode.NOT_CONFIGURED, "not configured"),
        (GhidraUsageMode.PRELAUNCH_FAILURE, "failed before"),
        (GhidraUsageMode.NO_ACTIVITY, "no Ghidra activity was observed"),
        (GhidraUsageMode.STARTUP_ONLY, "no program-level Ghidra analysis"),
        (GhidraUsageMode.DIRECT_MCP, "directly through MCP tools"),
    ],
)
def test_each_ghidra_mode_has_its_own_summary(mode, fragment: str) -> None:
    """Every mode must map to a distinct sentence.

    The chain is a run of `==` comparisons, so a single swapped operator makes
    one mode silently report another mode's story.
    """
    assert fragment in _ghidra_usage_summary(mode)


def test_ghidra_summaries_are_all_distinct() -> None:
    summaries = {mode: _ghidra_usage_summary(mode) for mode in GhidraUsageMode}
    assert len(set(summaries.values())) == len(summaries)


# ----------------------------------------------------------------- deadlines
def test_seconds_remaining_raises_exactly_at_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills `if remaining <= 0:` -> `< 0`. Zero seconds left is a timeout.

    The boundary has to be forced: reading the real clock always leaves a
    slightly negative remainder by the time the subtraction happens, so both
    the original and the mutant raise and the case proves nothing.
    """
    monkeypatch.setattr(
        "codex_dobby_mcp.runner.time.monotonic", lambda: 1000.0
    )
    with pytest.raises(asyncio.TimeoutError):
        _seconds_remaining(1000.0)
    with pytest.raises(asyncio.TimeoutError):
        _seconds_remaining(999.0)
    assert _seconds_remaining(1030.0) == 30.0
    monkeypatch.undo()
    with pytest.raises(asyncio.TimeoutError):
        _seconds_remaining(time.monotonic() - 5)
    assert _seconds_remaining(time.monotonic() + 30) > 0


# ------------------------------------------------------------- tool metadata
@pytest.mark.parametrize(
    "tool, expected",
    [
        (ToolName.PLAN, True), (ToolName.RESEARCH, True),
        (ToolName.BRAINSTORM, True), (ToolName.REVIEW, True),
        (ToolName.BUILD, False), (ToolName.VALIDATE, False),
        (ToolName.REVERSE_ENGINEER, False),
    ],
)
def test_snapshot_metadata_mode_follows_read_only_tools(tool, expected: bool) -> None:
    """Read-only tools use cheap metadata fingerprints; mutating tools hash."""
    assert _snapshot_uses_metadata(tool) is expected


# ------------------------------------------------------- input normalisation
def test_blank_file_entries_are_dropped_not_kept_as_empty_strings() -> None:
    """Kills `if item and item.strip()` -> `or`.

    An empty path would otherwise survive into `files`, and a whitespace-only
    one would be stripped to "" and then treated as a real path argument.
    """
    request = InvocationRequest(
        prompt="p",
        files=["  a.py  ", "", "   ", "b.py"],
        extra_roots=["  /tmp/x  ", ""],
    )
    assert request.files == ["a.py", "b.py"]
    assert request.extra_roots == ["/tmp/x"]


# ------------------------------------------------------------------ logging
def test_configure_logging_leaves_an_explicit_level_alone() -> None:
    """Kills `level is None and logger.level == logging.NOTSET` -> `is not None`.

    `get_logger` calls `configure_logging()` with no level on every lookup, so
    defaulting unconditionally would erase a caller's DEBUG the next time any
    module fetched a logger.
    """
    from codex_dobby_mcp import logging_utils

    logger = logging.getLogger(logging_utils.LOGGER_NAME)
    original_level, original_handlers = logger.level, list(logger.handlers)
    try:
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)

        logging_utils.configure_logging()
        assert logger.level == logging.DEBUG

        # With no level chosen at all, INFO is supplied.
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)
        logging_utils.configure_logging()
        assert logger.level == logging.INFO

        # An explicit level always wins.
        logging_utils.configure_logging(logging.WARNING)
        assert logger.level == logging.WARNING
    finally:
        logger.handlers[:] = original_handlers
        logger.setLevel(original_level)


def test_configure_logging_is_idempotent_and_does_not_propagate() -> None:
    """Kills `if not logger.handlers:` -> `if logger.handlers:` and the
    `propagate = False` constant.

    Dobby speaks JSON-RPC on stdout; a propagating logger would let records
    reach the root handler and corrupt the stdio transport, and duplicate
    handlers would emit every line twice.
    """
    from codex_dobby_mcp import logging_utils

    logger = logging.getLogger(logging_utils.LOGGER_NAME)
    original_level, original_handlers = logger.level, list(logger.handlers)
    original_propagate = logger.propagate
    try:
        logger.handlers.clear()
        logging_utils.configure_logging()
        assert len(logger.handlers) == 1
        logging_utils.configure_logging()
        logging_utils.configure_logging()
        assert len(logger.handlers) == 1
        assert logger.propagate is False
    finally:
        logger.handlers[:] = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate

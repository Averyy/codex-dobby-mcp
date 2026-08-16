"""Salvage and stall diagnostics.

When codex is killed before emitting its final JSON, these functions are the
only thing standing between the caller and an empty result. Their boundaries
were unpinned, so an off-by-one in the trace scanner or a miscounted outcome
would produce a confident, wrong account of what the run managed to do.
"""

from __future__ import annotations

import pytest

from codex_dobby_mcp.models import Completeness, ToolName
from codex_dobby_mcp.runner import (
    _SALVAGE_LINE_MAX_CHARS,
    _extract_codex_error_message,
    _first_meaningful_output_line,
    _last_non_trace_line,
    _salvage_exec_trace,
    _salvage_worker_result_from_trace,
    _stall_diagnostics,
)


def _trace(*blocks: str) -> str:
    return "\n".join(blocks) + "\n"


# ------------------------------------------------------------ exec trace
def test_exec_blocks_are_extracted_with_their_outcomes() -> None:
    stderr = _trace(
        "exec", "  ls -la", "  succeeded in 12ms",
        "exec", "  make build", "  failed",
        "exec", "  sleep 100",
    )
    assert _salvage_exec_trace(stderr) == [
        {"cmd": "ls -la", "outcome": "succeeded"},
        {"cmd": "make build", "outcome": "failed"},
        {"cmd": "sleep 100", "outcome": "in-flight"},
    ]


def test_exited_counts_as_failed_and_a_new_exec_ends_the_scan() -> None:
    stderr = _trace("exec", "  a", "  exited 3", "exec", "  b", "  succeeded in 1ms")
    assert _salvage_exec_trace(stderr) == [
        {"cmd": "a", "outcome": "failed"},
        {"cmd": "b", "outcome": "succeeded"},
    ]


def test_a_trailing_exec_with_no_command_line_is_dropped() -> None:
    """Kills `cmd_line = ... if i + 1 < n else ""` -> `<=`.

    An `exec` as the very last line has no command after it. Reading past the
    end would raise IndexError; recording an empty command would invent a
    shell call that never happened.
    """
    assert _salvage_exec_trace(_trace("exec")) == []
    assert _salvage_exec_trace("exec") == []


def test_empty_stderr_yields_no_blocks() -> None:
    assert _salvage_exec_trace("") == []


def test_scanner_terminates_on_a_trailing_exec_without_looping() -> None:
    """Kills `while i < n:` -> `<=`, which would read past the last line."""
    assert _salvage_exec_trace(_trace("noise", "exec", "  cmd")) == [
        {"cmd": "cmd", "outcome": "in-flight"}
    ]


@pytest.mark.parametrize(
    "length, truncated",
    [(_SALVAGE_LINE_MAX_CHARS, False), (_SALVAGE_LINE_MAX_CHARS + 1, True)],
)
def test_long_commands_truncate_at_the_cap(length: int, truncated: bool) -> None:
    """Kills `len(cmd_line) > _SALVAGE_LINE_MAX_CHARS` -> `>=`."""
    cmd = "x" * length
    (entry,) = _salvage_exec_trace(_trace("exec", f"  {cmd}", "  succeeded in 1ms"))
    assert entry["cmd"].endswith("…") is truncated
    if not truncated:
        assert entry["cmd"] == cmd


# --------------------------------------------------------- salvaged result
def test_salvage_requires_a_stall_or_timeout() -> None:
    stderr = _trace("exec", "  ls", "  succeeded in 1ms")
    assert _salvage_worker_result_from_trace(
        ToolName.BUILD, stderr, stall_hit=False, timeout_hit=False
    ) is None


def test_review_has_its_own_salvage_path() -> None:
    stderr = _trace("exec", "  ls", "  succeeded in 1ms")
    assert _salvage_worker_result_from_trace(
        ToolName.REVIEW, stderr, stall_hit=True, timeout_hit=False
    ) is None


def test_nothing_observed_salvages_nothing() -> None:
    """Kills `if not exec_blocks and turn_count == 0:` -> `or`.

    With `or`, a run that DID execute commands is discarded whenever no model
    turn was counted, throwing away the only evidence of partial progress.
    """
    assert _salvage_worker_result_from_trace(
        ToolName.BUILD, "quiet\n", stall_hit=True, timeout_hit=False
    ) is None

    # Commands but no counted turns: still salvageable.
    result = _salvage_worker_result_from_trace(
        ToolName.BUILD,
        _trace("exec", "  ls", "  succeeded in 1ms"),
        stall_hit=True,
        timeout_hit=False,
    )
    assert result is not None
    assert result.completeness is Completeness.PARTIAL


def test_salvaged_counts_are_reported_per_outcome() -> None:
    """Kills the three `e["outcome"] == ...` comparisons.

    Each is a separate `==`; swapping any one makes the summary attribute
    commands to the wrong outcome, so the counts must be distinct.
    """
    # Counts are deliberately asymmetric. With 3/6 succeeded, `== "succeeded"`
    # and `!= "succeeded"` both yield 3 and the comparison is unpinned.
    stderr = _trace(
        "exec", "  a", "  succeeded in 1ms",
        "exec", "  b", "  succeeded in 1ms",
        "exec", "  c", "  succeeded in 1ms",
        "exec", "  d", "  succeeded in 1ms",
        "exec", "  e", "  failed",
        "exec", "  f",
    )
    result = _salvage_worker_result_from_trace(
        ToolName.BUILD, stderr, stall_hit=False, timeout_hit=True
    )
    assert result is not None
    fact = result.important_facts[0]
    assert "6 shell command(s) attempted" in fact
    assert "4 succeeded" in fact
    assert "1 failed" in fact
    assert "1 in-flight" in fact


# ------------------------------------------------------ first output line
def test_first_meaningful_line_prefers_a_real_error_event() -> None:
    stdout = "\n".join([
        "",
        '{"type": "session.created"}',
        '{"type": "error", "message": "schema rejected"}',
    ])
    assert _first_meaningful_output_line(stdout) == "schema rejected"


def test_first_meaningful_line_falls_back_to_plain_text() -> None:
    assert _first_meaningful_output_line("not json at all\n") == "not json at all"


def test_typeless_json_object_is_returned_verbatim() -> None:
    """Kills `isinstance(payload, dict) and payload.get("type")` -> `or`.

    A dict with no `type` is not a codex event, so it is surfaced as-is
    instead of being skipped as envelope noise.
    """
    assert _first_meaningful_output_line('{"unexpected": 1}\n') == '{"unexpected": 1}'


def test_only_error_events_are_unwrapped() -> None:
    """Kills `payload_type in ("error", "turn.failed")` -> `not in`."""
    assert _first_meaningful_output_line('{"type": "item.started"}\n') is None
    assert _first_meaningful_output_line(
        '{"type": "turn.failed", "error": {"message": "boom"}}\n'
    ) == "boom"


def test_no_output_yields_nothing() -> None:
    assert _first_meaningful_output_line("") is None
    assert _first_meaningful_output_line("   \n\n") is None


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"message": "top"}, "top"),
        ({"message": "  spaced  "}, "spaced"),
        ({"error": {"message": "nested"}}, "nested"),
        ({"error": {"summary": "sum"}}, "sum"),
        ({"message": "  ", "error": {"message": "fallback"}}, "fallback"),
        ({}, None),
        ({"message": 7}, None),
    ],
)
def test_codex_error_message_extraction(payload: dict, expected) -> None:
    """Kills `isinstance(candidate, str) and candidate.strip()` -> `or`."""
    assert _extract_codex_error_message(payload) == expected


# --------------------------------------------------------- trace filtering
def test_last_non_trace_line_skips_every_trace_marker() -> None:
    """Kills the `or` chain -> `and`.

    With `and`, a line must contain all three markers to be skipped, so raw
    tracing noise becomes the "last meaningful line" shown to the caller.
    """
    stderr = "\n".join([
        "real failure here",
        "2026-01-01T00:00:00.0Z codex_api:: chatter",
        "2026-01-01T00:00:01.0Z  TRACE  more",
        "2026-01-01T00:00:02.0Z  DEBUG  more",
        "   ",
    ])
    assert _last_non_trace_line(stderr) == "real failure here"


def test_last_non_trace_line_handles_all_noise() -> None:
    assert _last_non_trace_line("codex_api:: only\n") is None
    assert _last_non_trace_line("") is None


@pytest.mark.parametrize(
    "length, truncated",
    [(_SALVAGE_LINE_MAX_CHARS, False), (_SALVAGE_LINE_MAX_CHARS + 1, True)],
)
def test_stall_snippet_truncates_at_the_cap(length: int, truncated: bool) -> None:
    """Kills `len(snippet) > _SALVAGE_LINE_MAX_CHARS` -> `>=`."""
    line = "e" * length
    text = _stall_diagnostics(line + "\n", "", 90.0)
    assert ("…" in text) is truncated


def test_stall_diagnostics_calls_out_a_silent_stream() -> None:
    text = _stall_diagnostics("", "", 90.0)
    assert "No SSE activity" in text
    assert "90 seconds" in text
    busy = _stall_diagnostics("some stderr\n", "", 90.0)
    assert "No SSE activity" not in busy

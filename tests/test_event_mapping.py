"""ACP event mapping.

`_meta.acpEvent` on every progress notification is a client-visible contract
(AGENTS.md, "Release verification"), and these mappers are what produce it.
Most of the module had no coverage, so a mis-mapped tool call, a dropped error,
or a wrong `kind` would have reached clients silently.
"""

from __future__ import annotations

import pytest

from codex_dobby_mcp.events import (
    _tool_kind_from_name,
    _truncate_text,
    map_codex_event,
)


def _item(payload_type: str, item: dict) -> dict:
    return {"type": payload_type, "item": item}


# ------------------------------------------------------------------ helpers
@pytest.mark.parametrize(
    "name, kind",
    [
        ("read_file", "read"), ("cat", "read"), ("view_thing", "read"),
        ("edit_file", "edit"), ("write_file", "edit"), ("apply_patch", "edit"),
        ("patch", "edit"),
        ("delete_file", "delete"), ("remove_x", "delete"), ("unlink", "delete"),
        ("move_file", "move"), ("rename", "move"),
        ("search_web", "search"), ("grep", "search"), ("find", "search"),
        ("list_dir", "search"),
        ("fetch", "fetch"), ("browse_reddit", "fetch"), ("web_search", "fetch"),
        ("get_alibaba_product", "fetch"), ("get_aliexpress_product", "fetch"),
        ("think", "think"),
        ("something_else", "other"), ("", "other"),
        ("READ_FILE", "read"),  # matching is case-insensitive
    ],
)
def test_tool_kind_is_derived_from_the_name_prefix(name: str, kind: str) -> None:
    assert _tool_kind_from_name(name) == kind


@pytest.mark.parametrize("length", [0, 1, 4095, 4096])
def test_short_text_is_returned_unchanged(length: int) -> None:
    """`len(text) <= limit` is the boundary; 4096 must NOT be truncated."""
    text = "x" * length
    assert _truncate_text(text) == text


def test_over_limit_text_is_truncated_with_an_accurate_count() -> None:
    text = "x" * 5000
    out = _truncate_text(text)
    assert out.startswith("x" * 4096)
    assert out.endswith("…[truncated 904 chars]")
    assert len(out) < len(text)


def test_truncation_limit_is_configurable() -> None:
    assert _truncate_text("abcdef", limit=3) == "abc\n…[truncated 3 chars]"


# ------------------------------------------------------------ command exec
def test_command_execution_start_and_completion() -> None:
    started = map_codex_event(
        _item("item.started", {"type": "command_execution", "id": "c1", "command": "ls -la"})
    )
    assert started == {
        "type": "tool_call",
        "toolCallId": "c1",
        "title": "ls -la",
        "kind": "execute",
        "status": "in_progress",
    }

    done = map_codex_event(
        _item(
            "item.completed",
            {
                "type": "command_execution",
                "id": "c1",
                "exit_code": 0,
                "aggregated_output": "out",
            },
        )
    )
    assert done["status"] == "completed"
    assert done["content"][0]["content"]["text"] == "out"


@pytest.mark.parametrize(
    "exit_code, status",
    [(0, "completed"), (None, "completed"), (1, "failed"), (127, "failed")],
)
def test_command_status_follows_the_exit_code(exit_code, status) -> None:
    done = map_codex_event(
        _item("item.completed", {"type": "command_execution", "id": "c", "exit_code": exit_code})
    )
    assert done["status"] == status


def test_blank_command_output_adds_no_content() -> None:
    done = map_codex_event(
        _item(
            "item.completed",
            {"type": "command_execution", "id": "c", "exit_code": 0, "aggregated_output": "   "},
        )
    )
    assert "content" not in done


# --------------------------------------------------------------- other items
def test_collab_tool_call_maps_both_phases() -> None:
    started = map_codex_event(
        _item("item.started", {"type": "collab_tool_call", "id": "s1", "tool": "spawn_agent"})
    )
    assert started["title"] == "spawn_agent"
    assert started["kind"] == "other"
    assert map_codex_event(
        _item("item.completed", {"type": "collab_tool_call", "id": "s1"})
    ) == {"type": "tool_call_update", "toolCallId": "s1", "status": "completed"}


def test_collab_tool_call_without_a_tool_name_gets_a_fallback_title() -> None:
    started = map_codex_event(
        _item("item.started", {"type": "collab_tool_call", "id": "s1", "tool": 7})
    )
    assert started["title"] == "Subagent call"


@pytest.mark.parametrize(
    "item, title, kind",
    [
        ({"id": "m", "server": "fetchaller", "tool": "fetch"}, "fetchaller.fetch", "fetch"),
        ({"id": "m", "server_name": "ghidra", "name": "read_mem"}, "ghidra.read_mem", "read"),
        ({"id": "m", "tool": "search_x"}, "search_x", "search"),
        ({"id": "m"}, "MCP tool", "other"),
    ],
)
def test_mcp_tool_call_title_and_kind(item: dict, title: str, kind: str) -> None:
    started = map_codex_event(_item("item.started", {"type": "mcp_tool_call", **item}))
    assert started["title"] == title
    assert started["kind"] == kind


def test_file_change_emits_a_diff_on_completion() -> None:
    started = map_codex_event(
        _item("item.started", {"type": "file_change", "id": "f1", "path": "a.py"})
    )
    assert started == {
        "type": "tool_call",
        "toolCallId": "f1",
        "title": "Edit a.py",
        "kind": "edit",
        "status": "in_progress",
    }

    done = map_codex_event(
        _item(
            "item.completed",
            {"type": "file_change", "id": "f1", "path": "a.py",
             "old_text": "before", "new_text": "after"},
        )
    )
    assert done["content"] == [
        {"type": "diff", "path": "a.py", "oldText": "before", "newText": "after"}
    ]


def test_file_change_without_text_carries_no_diff() -> None:
    done = map_codex_event(
        _item("item.completed", {"type": "file_change", "id": "f1", "path": "a.py"})
    )
    assert "content" not in done


def test_file_change_falls_back_to_the_file_key_and_unknown() -> None:
    assert map_codex_event(
        _item("item.started", {"type": "file_change", "id": "f", "file": "b.py"})
    )["title"] == "Edit b.py"
    assert map_codex_event(
        _item("item.started", {"type": "file_change", "id": "f"})
    )["title"] == "Edit <unknown>"


def test_web_search_maps_both_phases() -> None:
    started = map_codex_event(
        _item("item.started", {"type": "web_search", "id": "w", "query": "mcp sdk"})
    )
    assert started["title"] == "Search: mcp sdk"
    assert started["kind"] == "fetch"
    assert map_codex_event(
        _item("item.completed", {"type": "web_search", "id": "w"})
    )["status"] == "completed"


def test_web_search_without_a_query_uses_a_fallback() -> None:
    assert map_codex_event(
        _item("item.started", {"type": "web_search", "id": "w"})
    )["title"] == "Search: web search"


# ------------------------------------------------------- id / shape guards
@pytest.mark.parametrize(
    "item_type",
    ["command_execution", "collab_tool_call", "mcp_tool_call", "file_change", "web_search"],
)
@pytest.mark.parametrize("started", [True, False])
def test_items_without_a_string_id_are_dropped(item_type: str, started: bool) -> None:
    """A tool call with no usable id cannot be correlated, so it must not emit."""
    phase = "item.started" if started else "item.completed"
    assert map_codex_event(_item(phase, {"type": item_type})) is None
    assert map_codex_event(_item(phase, {"type": item_type, "id": 12})) is None


def test_non_dict_item_and_unknown_types_are_dropped() -> None:
    assert map_codex_event({"type": "item.started", "item": "nope"}) is None
    assert map_codex_event(_item("item.started", {"type": "unheard_of", "id": "x"})) is None
    assert map_codex_event({"type": "session.created"}) is None
    assert map_codex_event({}) is None


# -------------------------------------------------------------- item.failed
def test_item_failed_reports_failure_with_its_message() -> None:
    out = map_codex_event(
        _item("item.failed", {"type": "command_execution", "id": "c", "error": "boom"})
    )
    assert out["status"] == "failed"
    assert out["toolCallId"] == "c"
    assert out["content"][0]["content"]["text"] == "boom"


def test_item_failed_accepts_the_message_key_and_tolerates_neither() -> None:
    assert map_codex_event(
        _item("item.failed", {"id": "c", "message": "why"})
    )["content"][0]["content"]["text"] == "why"
    assert "content" not in map_codex_event(_item("item.failed", {"id": "c"}))
    assert map_codex_event(_item("item.failed", {})) is None
    assert map_codex_event({"type": "item.failed", "item": None}) is None


# ---------------------------------------------------------- top-level error
@pytest.mark.parametrize("payload_type", ["error", "turn.failed"])
@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"message": "top level"}, "top level"),
        ({"error": {"message": "nested"}}, "nested"),
        ({"error": {"summary": "only summary"}}, "only summary"),
        ({"error": "plain string"}, "plain string"),
        # `message` wins over `error` when both are present.
        ({"message": "outer", "error": {"message": "inner"}}, "outer"),
    ],
)
def test_top_level_errors_are_surfaced(payload_type, payload, expected) -> None:
    """Without this mapping the events.jsonl artifact looks empty on API errors."""
    out = map_codex_event({"type": payload_type, **payload})
    assert out == {
        "type": "agent_message_chunk",
        "content": {"type": "text", "text": expected},
    }


@pytest.mark.parametrize(
    "payload",
    [{}, {"message": "  "}, {"error": {}}, {"error": {"message": " "}}, {"error": 7}],
)
def test_errors_without_usable_text_are_dropped(payload: dict) -> None:
    assert map_codex_event({"type": "error", **payload}) is None


# ------------------------------------------------------------------- plans
def test_todo_list_maps_to_a_plan_on_the_started_side_only() -> None:
    item = {
        "type": "todo_list",
        "entries": [
            {"content": "first"},
            {"text": "second", "priority": "high", "status": "in_progress"},
            {"title": "third"},
            "not a dict",
            {"no_usable_key": 1},
        ],
    }
    plan = map_codex_event(_item("item.started", item))
    assert plan == {
        "type": "plan",
        "entries": [
            {"content": "first", "priority": "medium", "status": "pending"},
            {"content": "second", "priority": "high", "status": "in_progress"},
            {"content": "third", "priority": "medium", "status": "pending"},
        ],
    }
    # Completion must not duplicate the plan.
    assert map_codex_event(_item("item.completed", item)) is None


@pytest.mark.parametrize(
    "item",
    [
        {"type": "todo_list"},
        {"type": "todo_list", "entries": "nope"},
        {"type": "todo_list", "entries": []},
        {"type": "todo_list", "entries": [{"no_content": 1}]},
    ],
)
def test_empty_or_malformed_plans_are_dropped(item: dict) -> None:
    assert map_codex_event(_item("item.started", item)) is None


def test_todo_list_accepts_the_alternate_container_keys() -> None:
    for key in ("items", "todos"):
        plan = map_codex_event(
            _item("item.started", {"type": "todo_list", key: [{"content": "x"}]})
        )
        assert plan["entries"][0]["content"] == "x"


# ---------------------------------------------------------- agent messages
def test_agent_message_emits_once_on_completion() -> None:
    item = {"type": "agent_message", "text": "hello"}
    assert map_codex_event(_item("item.started", item)) is None
    assert map_codex_event(_item("item.completed", item)) == {
        "type": "agent_message_chunk",
        "content": {"type": "text", "text": "hello"},
    }


@pytest.mark.parametrize("text", [None, "", "   ", 7])
def test_blank_agent_messages_are_dropped(text) -> None:
    assert map_codex_event(
        _item("item.completed", {"type": "agent_message", "text": text})
    ) is None

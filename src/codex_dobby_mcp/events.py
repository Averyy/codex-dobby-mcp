"""Codex JSONL → ACP-shaped event mapper.

Codex `exec --json` writes one JSON object per line to stdout. This module
parses each line and yields ACP-shaped progress events that Dobby can forward
to MCP callers and persist to ``events.jsonl``. The output format mirrors
ACP's ``session/update`` payloads, minus the JSON-RPC envelope.

The mapper is intentionally conservative: codex events we don't recognize are
dropped, so this module ships without exhaustive coverage. Extend the
dispatcher rather than guessing on unknown event shapes.

The recognized vocabulary is the dispatch in ``map_codex_event`` below and its
``_map_*`` handlers; that is the source of truth for which codex events map to
which ACP-shaped update.
"""

from __future__ import annotations

import json
from typing import Any


_TOOL_KIND_PREFIX_MAP: tuple[tuple[tuple[str, ...], str], ...] = (
    (("read", "cat", "view"), "read"),
    (("edit", "write", "apply", "patch"), "edit"),
    (("delete", "remove", "unlink"), "delete"),
    (("move", "rename"), "move"),
    (("search", "grep", "find", "list"), "search"),
    (("fetch", "browse", "web_", "get_alibaba", "get_aliexpress"), "fetch"),
    (("think",), "think"),
)


def _tool_kind_from_name(name: str) -> str:
    lowered = name.lower()
    for prefixes, kind in _TOOL_KIND_PREFIX_MAP:
        if any(lowered.startswith(p) for p in prefixes):
            return kind
    return "other"


def _truncate_text(text: str, *, limit: int = 4096) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


def _map_command_execution(item: dict[str, Any], started: bool) -> dict[str, Any] | None:
    item_id = item.get("id")
    if not isinstance(item_id, str):
        return None
    command = item.get("command")
    title = command if isinstance(command, str) else "Run command"
    if started:
        return {
            "type": "tool_call",
            "toolCallId": item_id,
            "title": _truncate_text(title, limit=240),
            "kind": "execute",
            "status": "in_progress",
        }
    output = item.get("aggregated_output")
    exit_code = item.get("exit_code")
    status = "completed" if exit_code in (0, None) else "failed"
    update: dict[str, Any] = {
        "type": "tool_call_update",
        "toolCallId": item_id,
        "status": status,
    }
    if isinstance(output, str) and output.strip():
        update["content"] = [
            {
                "type": "content",
                "content": {"type": "text", "text": _truncate_text(output)},
            }
        ]
    return update


def _map_collab_tool_call(item: dict[str, Any], started: bool) -> dict[str, Any] | None:
    item_id = item.get("id")
    if not isinstance(item_id, str):
        return None
    tool = item.get("tool")
    title = tool if isinstance(tool, str) else "Subagent call"
    if started:
        return {
            "type": "tool_call",
            "toolCallId": item_id,
            "title": title,
            "kind": "other",
            "status": "in_progress",
        }
    return {
        "type": "tool_call_update",
        "toolCallId": item_id,
        "status": "completed",
    }


def _map_mcp_tool_call(item: dict[str, Any], started: bool) -> dict[str, Any] | None:
    item_id = item.get("id")
    if not isinstance(item_id, str):
        return None
    tool_name = item.get("tool") or item.get("name") or ""
    server = item.get("server") or item.get("server_name") or ""
    title = f"{server}.{tool_name}" if server and tool_name else (tool_name or "MCP tool")
    if started:
        return {
            "type": "tool_call",
            "toolCallId": item_id,
            "title": title,
            "kind": _tool_kind_from_name(tool_name) if isinstance(tool_name, str) else "other",
            "status": "in_progress",
        }
    return {
        "type": "tool_call_update",
        "toolCallId": item_id,
        "status": "completed",
    }


def _map_file_change(item: dict[str, Any], started: bool) -> dict[str, Any] | None:
    item_id = item.get("id")
    if not isinstance(item_id, str):
        return None
    path = item.get("path") or item.get("file") or "<unknown>"
    if started:
        return {
            "type": "tool_call",
            "toolCallId": item_id,
            "title": f"Edit {path}",
            "kind": "edit",
            "status": "in_progress",
        }
    update: dict[str, Any] = {
        "type": "tool_call_update",
        "toolCallId": item_id,
        "status": "completed",
    }
    old_text = item.get("old_text")
    new_text = item.get("new_text")
    if isinstance(path, str) and (isinstance(old_text, str) or isinstance(new_text, str)):
        update["content"] = [
            {
                "type": "diff",
                "path": path,
                "oldText": old_text if isinstance(old_text, str) else None,
                "newText": new_text if isinstance(new_text, str) else None,
            }
        ]
    return update


def _map_web_search(item: dict[str, Any], started: bool) -> dict[str, Any] | None:
    item_id = item.get("id")
    if not isinstance(item_id, str):
        return None
    query = item.get("query") or "web search"
    if started:
        return {
            "type": "tool_call",
            "toolCallId": item_id,
            "title": f"Search: {query}",
            "kind": "fetch",
            "status": "in_progress",
        }
    return {
        "type": "tool_call_update",
        "toolCallId": item_id,
        "status": "completed",
    }


def _map_todo_list(item: dict[str, Any]) -> dict[str, Any] | None:
    entries_raw = item.get("entries") or item.get("items") or item.get("todos")
    if not isinstance(entries_raw, list):
        return None
    entries: list[dict[str, Any]] = []
    for raw in entries_raw:
        if not isinstance(raw, dict):
            continue
        content = raw.get("content") or raw.get("text") or raw.get("title")
        if not isinstance(content, str):
            continue
        entries.append(
            {
                "content": content,
                "priority": raw.get("priority") or "medium",
                "status": raw.get("status") or "pending",
            }
        )
    if not entries:
        return None
    return {"type": "plan", "entries": entries}


def _map_agent_message(item: dict[str, Any]) -> dict[str, Any] | None:
    text = item.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return {
        "type": "agent_message_chunk",
        "content": {"type": "text", "text": _truncate_text(text)},
    }


def _map_item_event(payload: dict[str, Any], *, started: bool) -> dict[str, Any] | None:
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if item_type == "command_execution":
        return _map_command_execution(item, started)
    if item_type == "collab_tool_call":
        return _map_collab_tool_call(item, started)
    if item_type == "mcp_tool_call":
        return _map_mcp_tool_call(item, started)
    if item_type == "file_change":
        return _map_file_change(item, started)
    if item_type == "web_search":
        return _map_web_search(item, started)
    if item_type == "todo_list":
        # Plans only emit on the started side to avoid duplicate updates.
        return _map_todo_list(item) if started else None
    if item_type == "agent_message":
        # Agent messages stream as completed items (codex doesn't emit deltas
        # in --json mode), so report them once on completion.
        return None if started else _map_agent_message(item)
    return None


def _map_item_failed(payload: dict[str, Any]) -> dict[str, Any] | None:
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    item_id = item.get("id")
    if not isinstance(item_id, str):
        return None
    update: dict[str, Any] = {
        "type": "tool_call_update",
        "toolCallId": item_id,
        "status": "failed",
    }
    error = item.get("error") or item.get("message")
    if isinstance(error, str) and error.strip():
        update["content"] = [
            {
                "type": "content",
                "content": {"type": "text", "text": _truncate_text(error)},
            }
        ]
    return update


def _map_top_level_error(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Surface codex's top-level ``error`` and ``turn.failed`` events.

    Codex emits these on stdout when the underlying model API rejects the
    request (e.g. invalid response_format schema). Without this mapping the
    event would be dropped, the events.jsonl artifact would look empty, and
    the runner's summary fallback would never see the actual error message.
    """
    text: str | None = None
    candidate = payload.get("message")
    if isinstance(candidate, str) and candidate.strip():
        text = candidate
    else:
        error_obj = payload.get("error")
        if isinstance(error_obj, dict):
            for key in ("message", "summary"):
                value = error_obj.get(key)
                if isinstance(value, str) and value.strip():
                    text = value
                    break
        elif isinstance(error_obj, str) and error_obj.strip():
            text = error_obj
    if text is None:
        return None
    return {
        "type": "agent_message_chunk",
        "content": {"type": "text", "text": _truncate_text(text)},
    }


def map_codex_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Translate one parsed codex JSONL payload to an ACP-shaped event.

    Returns ``None`` when the event has no streaming significance (envelope
    events, unrecognized shapes, malformed entries).
    """
    payload_type = payload.get("type")
    if payload_type == "item.started":
        return _map_item_event(payload, started=True)
    if payload_type == "item.completed":
        return _map_item_event(payload, started=False)
    if payload_type == "item.failed":
        return _map_item_failed(payload)
    if payload_type in ("error", "turn.failed"):
        return _map_top_level_error(payload)
    return None


def parse_codex_jsonl_chunk(chunk: bytes, *, carryover: bytes = b"") -> tuple[list[dict[str, Any]], bytes]:
    """Parse a chunk of codex stdout, returning (parsed JSONL payloads, carryover).

    Codex emits JSONL one event per line, but stdout chunks split arbitrarily.
    Callers should stitch successive chunks by passing each call's carryover
    back in. Lines that fail to parse are dropped silently — codex
    occasionally emits non-JSON noise (banners, errors) and we don't want to
    fail the run for that.
    """
    blob = carryover + chunk
    if not blob:
        return ([], b"")
    *lines, tail = blob.split(b"\n")
    payloads: list[dict[str, Any]] = []
    for raw in lines:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            payloads.append(parsed)
    # If the trailing fragment ends with a newline the split above produces an
    # empty bytes object, which is correct: nothing to carry over.
    return payloads, tail


def map_codex_chunk(
    chunk: bytes, *, carryover: bytes = b""
) -> tuple[list[dict[str, Any]], bytes]:
    """One-shot helper: parse a chunk and return (ACP events, carryover)."""
    payloads, tail = parse_codex_jsonl_chunk(chunk, carryover=carryover)
    events = [event for event in (map_codex_event(p) for p in payloads) if event is not None]
    return events, tail

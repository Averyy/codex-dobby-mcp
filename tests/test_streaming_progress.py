"""Tests for the codex-JSONL → ACP streaming progress pipeline."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from codex_dobby_mcp.events import map_codex_chunk, map_codex_event, parse_codex_jsonl_chunk
from codex_dobby_mcp.models import InvocationRequest, ToolName
from codex_dobby_mcp.review_agents import review_agents_root
from codex_dobby_mcp.runner import CodexRunner, _build_repo_snapshot


# --- mapper unit tests ----------------------------------------------------


def test_mapper_handles_command_execution_started_and_completed() -> None:
    started = map_codex_event(
        {
            "type": "item.started",
            "item": {
                "id": "item_0",
                "type": "command_execution",
                "command": "/bin/zsh -lc pwd",
                "status": "in_progress",
            },
        }
    )
    assert started == {
        "type": "tool_call",
        "toolCallId": "item_0",
        "title": "/bin/zsh -lc pwd",
        "kind": "execute",
        "status": "in_progress",
    }

    completed = map_codex_event(
        {
            "type": "item.completed",
            "item": {
                "id": "item_0",
                "type": "command_execution",
                "command": "/bin/zsh -lc pwd",
                "aggregated_output": "/repo\n",
                "exit_code": 0,
                "status": "completed",
            },
        }
    )
    assert completed is not None
    assert completed["type"] == "tool_call_update"
    assert completed["toolCallId"] == "item_0"
    assert completed["status"] == "completed"
    assert completed["content"][0]["content"]["text"] == "/repo\n"


def test_mapper_treats_nonzero_exit_as_failed() -> None:
    completed = map_codex_event(
        {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": "false",
                "exit_code": 1,
                "status": "completed",
            },
        }
    )
    assert completed is not None
    assert completed["status"] == "failed"


def test_mapper_emits_agent_message_only_on_completion() -> None:
    started = map_codex_event(
        {"type": "item.started", "item": {"id": "m1", "type": "agent_message"}}
    )
    assert started is None
    completed = map_codex_event(
        {
            "type": "item.completed",
            "item": {"id": "m1", "type": "agent_message", "text": "hello world"},
        }
    )
    assert completed == {
        "type": "agent_message_chunk",
        "content": {"type": "text", "text": "hello world"},
    }


def test_mapper_drops_unknown_events() -> None:
    assert map_codex_event({"type": "thread.started", "thread_id": "abc"}) is None
    assert map_codex_event({"type": "turn.started"}) is None
    assert map_codex_event({"type": "turn.completed"}) is None
    assert map_codex_event({"type": "weird-shape"}) is None


def test_mapper_surfaces_top_level_error_event() -> None:
    """Codex emits ``{"type":"error","message":...}`` for API rejections;
    surface those as agent_message_chunk so events.jsonl records the cause."""
    event = map_codex_event(
        {"type": "error", "message": "schema rejected: missing oldText"}
    )
    assert event == {
        "type": "agent_message_chunk",
        "content": {"type": "text", "text": "schema rejected: missing oldText"},
    }


def test_mapper_surfaces_turn_failed_with_nested_error() -> None:
    event = map_codex_event(
        {"type": "turn.failed", "error": {"message": "rate_limit_exceeded"}}
    )
    assert event is not None
    assert event["type"] == "agent_message_chunk"
    assert "rate_limit_exceeded" in event["content"]["text"]


def test_chunked_jsonl_parser_handles_split_lines() -> None:
    chunk1 = b'{"type":"item.started","item":{"id":"a","type":"command_execution","command":"echo"}'
    chunk2 = b'}\n{"type":"item.completed","item":{"id":"a","type":"command_execution","command":"echo","exit_code":0}}\n'

    payloads_a, carryover = parse_codex_jsonl_chunk(chunk1)
    assert payloads_a == []
    assert carryover == chunk1

    payloads_b, carryover2 = parse_codex_jsonl_chunk(chunk2, carryover=carryover)
    assert len(payloads_b) == 2
    assert carryover2 == b""


def test_chunked_mapper_returns_acp_events() -> None:
    blob = (
        b'{"type":"item.started","item":{"id":"x","type":"command_execution","command":"ls"}}\n'
        b'{"type":"item.completed","item":{"id":"x","type":"command_execution","command":"ls","exit_code":0}}\n'
    )
    events, carryover = map_codex_chunk(blob)
    assert carryover == b""
    assert [event["type"] for event in events] == ["tool_call", "tool_call_update"]


def test_mapper_drops_malformed_lines() -> None:
    blob = b"not-json\n{\"type\":\"item.started\",\"item\":{\"id\":\"a\",\"type\":\"agent_message\"}}\n"
    payloads, carryover = parse_codex_jsonl_chunk(blob)
    assert carryover == b""
    # Only the well-formed line is parsed.
    assert len(payloads) == 1


def test_mapper_emits_plan_for_todo_list() -> None:
    plan = map_codex_event(
        {
            "type": "item.started",
            "item": {
                "id": "todo",
                "type": "todo_list",
                "entries": [
                    {"content": "Inspect tests", "priority": "high", "status": "pending"},
                    {"content": "Write fix", "priority": "medium"},
                ],
            },
        }
    )
    assert plan is not None
    assert plan["type"] == "plan"
    assert plan["entries"][0]["content"] == "Inspect tests"
    assert plan["entries"][1]["status"] == "pending"


# --- end-to-end: events.jsonl artifact ------------------------------------


@pytest.fixture(autouse=True)
def stub_snapshot_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_capture(repo_root: Path, deadline: float, *, include_head: bool, use_metadata_fingerprints: bool):
        _ = deadline
        return _build_repo_snapshot(
            repo_root,
            include_head=include_head,
            use_metadata_fingerprints=use_metadata_fingerprints,
        )

    monkeypatch.setattr("codex_dobby_mcp.runner._capture_repo_snapshot_with_deadline", fake_capture)


@pytest.fixture(autouse=True)
def writable_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home = tmp_path / "codex-home"
    (codex_home / "sessions").mkdir(parents=True)
    (codex_home / "auth.json").write_text('{"auth_mode":"chatgpt"}\n', encoding="utf-8")
    (codex_home / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))


def _make_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    subprocess.run(["git", "-C", str(repo_root), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(repo_root), "config", "user.name", "t"], check=True)
    (repo_root / "runner.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_root), "commit", "-q", "-m", "init"], check=True)
    return repo_root


def _make_runner(repo_root: Path) -> CodexRunner:
    project_root = Path(__file__).resolve().parents[1]
    assets_root = project_root / "src" / "codex_dobby_mcp" / "assets"
    return CodexRunner(
        spawn_root=repo_root,
        prompts_root=assets_root / "prompts",
        worker_schema_path=assets_root / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agents_root(assets_root),
    )


SAMPLE_JSONL = (
    b'{"type":"thread.started","thread_id":"t1"}\n'
    b'{"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"pwd"}}\n'
    b'{"type":"item.completed","item":{"id":"item_0","type":"command_execution","command":"pwd","aggregated_output":"/repo\\n","exit_code":0}}\n'
    b'{"type":"item.completed","item":{"id":"m1","type":"agent_message","text":"final answer"}}\n'
)


class _FakeProcess:
    def __init__(self, *, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
        return (self._stdout, self._stderr)

    def kill(self) -> None:
        self.returncode = -9


@pytest.mark.asyncio
async def test_events_jsonl_is_written_for_each_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo(tmp_path)
    runner = _make_runner(repo_root)

    payload = {
        "summary": "ok",
        "completeness": "full",
        "important_facts": [],
        "next_steps": [],
        "files_changed": [],
        "warnings": [],
    }

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return _FakeProcess(stdout=SAMPLE_JSONL)

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)

    result = await runner.run(ToolName.PLAN, InvocationRequest(prompt="inspect"))

    events_path = Path(result.artifact_paths["events_jsonl"])
    assert events_path.exists()
    written = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    assert any(event["type"] == "tool_call" for event in written)
    assert any(event["type"] == "agent_message_chunk" for event in written)


@pytest.mark.asyncio
async def test_event_sink_receives_acp_events_during_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo(tmp_path)
    runner = _make_runner(repo_root)
    payload = {
        "summary": "ok",
        "completeness": "full",
        "important_facts": [],
        "next_steps": [],
        "files_changed": [],
        "warnings": [],
    }

    received: list[tuple[dict, int]] = []

    async def sink(event: dict, count: int) -> None:
        received.append((event, count))

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return _FakeProcess(stdout=SAMPLE_JSONL)

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)

    await runner.run(ToolName.PLAN, InvocationRequest(prompt="inspect"), event_sink=sink)

    assert received, "sink was never called"
    types = [event["type"] for event, _ in received]
    assert "tool_call" in types
    assert "tool_call_update" in types
    counts = [count for _, count in received]
    assert counts == sorted(counts)
    assert counts[0] >= 1


@pytest.mark.asyncio
async def test_event_sink_errors_do_not_kill_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo(tmp_path)
    runner = _make_runner(repo_root)
    payload = {
        "summary": "ok",
        "completeness": "full",
        "important_facts": [],
        "next_steps": [],
        "files_changed": [],
        "warnings": [],
    }

    async def angry_sink(event: dict, count: int) -> None:
        raise RuntimeError("client gone")

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return _FakeProcess(stdout=SAMPLE_JSONL)

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)

    result = await runner.run(
        ToolName.PLAN, InvocationRequest(prompt="inspect"), event_sink=angry_sink
    )
    # The run still finalizes successfully even though the sink raised.
    assert result.summary == "ok"


@pytest.mark.asyncio
async def test_wait_run_forwards_live_events_to_subscriber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subscribers registered via wait_run get live events from in-flight runs."""
    from codex_dobby_mcp.background_runs import BackgroundRunManager

    repo_root = _make_repo(tmp_path)
    runner = _make_runner(repo_root)
    manager = BackgroundRunManager(runner)

    payload = {
        "summary": "ok",
        "completeness": "full",
        "important_facts": [],
        "next_steps": [],
        "files_changed": [],
        "warnings": [],
    }

    # Gate the codex run so the test gets a chance to subscribe BEFORE codex
    # finishes. The fake codex blocks until ``release`` is set.
    release = asyncio.Event()

    class GatedFakeProcess:
        returncode = 0

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            await release.wait()
            return (SAMPLE_JSONL, b"")

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return GatedFakeProcess()

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)

    spec = runner.prepare(ToolName.PLAN, InvocationRequest(prompt="inspect"))
    handle = manager.start(spec)

    received: list[tuple[dict, int, str]] = []

    async def sink(event: dict, count: int, task_id: str) -> None:
        received.append((event, count, task_id))

    # Subscribe via wait_run, then unblock the gated process.
    async def waiter():
        return await manager.wait(
            spec.repo_root,
            task_id=handle.task_id,
            task_ids=None,
            timeout_seconds=10.0,
            event_sink=sink,
        )

    wait_task = asyncio.create_task(waiter())
    # Yield once so wait_run can register its subscriber before we release.
    await asyncio.sleep(0.05)
    release.set()
    lookup = await wait_task

    # The wait completed and forwarded events.
    assert lookup.state.value == "finished"
    assert received, "wait_run forwarder produced no events"
    assert all(task_id == handle.task_id for _e, _c, task_id in received)


@pytest.mark.asyncio
async def test_codex_top_level_error_surfaces_in_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When codex --json emits a top-level error event (e.g. API rejected
    the request), the runner must surface that message instead of the
    stderr TRACE noise."""
    repo_root = _make_repo(tmp_path)
    runner = _make_runner(repo_root)

    error_stdout = (
        b'{"type":"thread.started","thread_id":"abc"}\n'
        b'{"type":"error","message":"Invalid schema for response_format"}\n'
    )
    trace_stderr = b"2026-04-29T21:13:39.385019Z TRACE codex_api::sse::responses: unhandled\n"

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        # Don't write last_message — codex failed before producing it.
        return _FakeProcess(returncode=1, stdout=error_stdout, stderr=trace_stderr)

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)

    result = await runner.run(ToolName.PLAN, InvocationRequest(prompt="inspect"))

    # Summary must include the API error, not just the stderr TRACE noise.
    assert "Invalid schema" in result.summary, f"got summary: {result.summary!r}"


@pytest.mark.asyncio
async def test_no_event_sink_means_no_streaming_overhead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs without a sink still write events.jsonl but otherwise silent."""
    repo_root = _make_repo(tmp_path)
    runner = _make_runner(repo_root)
    payload = {
        "summary": "ok",
        "completeness": "full",
        "important_facts": [],
        "next_steps": [],
        "files_changed": [],
        "warnings": [],
    }

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return _FakeProcess(stdout=SAMPLE_JSONL)

    monkeypatch.setattr("codex_dobby_mcp.runner.asyncio.create_subprocess_exec", fake_exec)

    result = await runner.run(ToolName.PLAN, InvocationRequest(prompt="inspect"))
    # Run completes normally with no sink wired up.
    assert result.summary == "ok"
    events_path = Path(result.artifact_paths["events_jsonl"])
    assert events_path.exists()
    # Artifact still got events from the captured stdout.
    assert events_path.read_text().strip() != ""

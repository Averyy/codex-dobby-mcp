"""The tool handler bodies and the progress-notification sinks.

Every `@server.tool` body was uncovered: the suite inspected the registered
schemas but never ran a handler. That left the tool-name dispatch, the event
sink wiring, and the `repo_root` resolution in the run-lookup tools untested,
which is how a fix to `_build_event_sink` shipped with no regression test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codex_dobby_mcp import server as server_module
from codex_dobby_mcp.models import (
    AsyncRunHandle,
    RunListResponse,
    RunLookupResponse,
    ToolName,
    ToolResponse,
)


def _response(tool: ToolName) -> ToolResponse:
    return ToolResponse(
        task_id="t1",
        tool=tool,
        status="success",
        summary="ok",
        completeness="full",
        model="m",
        reasoning_effort="high",
        artifact_paths={},
        repo_root="/tmp/repo",
    )


class _RecordingRunner:
    """Stands in for CodexRunner so a handler can be driven without codex."""

    def __init__(self, spawn_root: Path):
        self.spawn_root = spawn_root
        self.calls: list[dict[str, Any]] = []

    async def run(self, tool, request, *, event_sink=None):
        self.calls.append({"tool": tool, "request": request, "event_sink": event_sink})
        return _response(tool)

    def prepare(self, tool, request):
        self.calls.append({"tool": tool, "request": request, "prepared": True})
        return {"tool": tool, "request": request}


class _RecordingBackground:
    def __init__(self, runner):
        self.runner = runner
        self.calls: list[tuple[str, tuple, dict]] = []

    def start(self, spec):
        self.calls.append(("start", (spec,), {}))
        return AsyncRunHandle(
            task_id="bg1",
            tool=spec["tool"],
            state="running",
            summary="started",
            repo_root="/tmp/repo",
            artifact_paths={},
            model="m",
            reasoning_effort="high",
        )

    def get(self, repo_root, task_id):
        self.calls.append(("get", (repo_root, task_id), {}))
        return RunLookupResponse(task_id=task_id, state="finished", summary="s",
                                 repo_root=str(repo_root), artifact_paths={})

    async def wait(self, repo_root, *, task_id, task_ids, timeout_seconds, event_sink):
        self.calls.append(("wait", (repo_root,), {
            "task_id": task_id, "task_ids": task_ids,
            "timeout_seconds": timeout_seconds, "event_sink": event_sink}))
        return RunLookupResponse(task_id=task_id or "x", state="finished", summary="s",
                                 repo_root=str(repo_root), artifact_paths={})

    def list(self, repo_root, *, limit):
        self.calls.append(("list", (repo_root,), {"limit": limit}))
        return RunListResponse(repo_root=str(repo_root), runs=[])


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    runner = _RecordingRunner(repo)
    background: dict[str, _RecordingBackground] = {}

    monkeypatch.setattr(server_module, "create_runner", lambda spawn_root=None: runner)

    def fake_bg(r):
        background["obj"] = _RecordingBackground(r)
        return background["obj"]

    monkeypatch.setattr(server_module, "BackgroundRunManager", fake_bg)
    server = server_module.create_server(repo)
    tools = {t.name: t for t in server._tool_manager.list_tools()}
    return server, tools, runner, background["obj"], repo


@pytest.mark.parametrize(
    "tool_name, expected",
    [
        ("plan", ToolName.PLAN),
        ("research", ToolName.RESEARCH),
        ("brainstorm", ToolName.BRAINSTORM),
        ("build", ToolName.BUILD),
        ("validate", ToolName.VALIDATE),
        ("review", ToolName.REVIEW),
        ("reverse_engineer", ToolName.REVERSE_ENGINEER),
    ],
)
@pytest.mark.asyncio
async def test_each_tool_dispatches_its_own_tool_name(
    wired, tool_name: str, expected: ToolName
) -> None:
    """A copy-paste slip in any handler would route work to the wrong tool.

    The tools differ in sandbox mode, default timeout, and prompt, so a
    mis-wired name is a real behaviour change rather than a label mistake.
    """
    from mcp.server.mcpserver import Context

    _server, tools, runner, _bg, repo = wired
    await tools[tool_name].run({"prompt": "do it", "repo_root": str(repo)}, Context())

    assert [c["tool"] for c in runner.calls] == [expected]
    assert runner.calls[0]["request"].prompt == "do it"
    # No progressToken in this Context, so streaming stays off.
    assert runner.calls[0]["event_sink"] is None


@pytest.mark.asyncio
async def test_tool_arguments_reach_the_invocation_request(wired) -> None:
    from mcp.server.mcpserver import Context

    _server, tools, runner, _bg, repo = wired
    await tools["build"].run(
        {
            "prompt": "p",
            "repo_root": str(repo),
            "files": ["a.py", "b.py"],
            "important_context": '{"ticket": "ABC-1"}',
            "timeout_seconds": 900,
            "model": "gpt-x",
            "reasoning_effort": "low",
        },
        Context(),
    )

    request = runner.calls[0]["request"]
    assert request.files == ["a.py", "b.py"]
    assert request.important_context == '{"ticket": "ABC-1"}'
    assert request.timeout_seconds == 900
    assert request.model == "gpt-x"
    assert request.reasoning_effort == "low"


@pytest.mark.asyncio
async def test_start_run_prepares_then_hands_off_to_the_background_manager(wired) -> None:
    from mcp.server.mcpserver import Context

    _server, tools, runner, bg, repo = wired
    handle = await tools["start_run"].run(
        {"tool": "review", "prompt": "look", "repo_root": str(repo),
         "agents": ["security", "correctness"]},
        Context(),
    )

    assert runner.calls[0]["prepared"] is True
    assert runner.calls[0]["tool"] == ToolName.REVIEW
    # Multi-agent review takes the 1200s default rather than review's own.
    assert runner.calls[0]["request"].timeout_seconds == 1200
    assert bg.calls[0][0] == "start"
    assert handle.task_id == "bg1"


@pytest.mark.asyncio
async def test_run_lookup_tools_resolve_repo_root_before_delegating(wired) -> None:
    from mcp.server.mcpserver import Context

    _server, tools, _runner, bg, repo = wired

    await tools["get_run"].run({"task_id": "abc", "repo_root": str(repo)}, Context())
    await tools["list_runs"].run({"repo_root": str(repo), "limit": 3}, Context())
    await tools["wait_run"].run(
        {"repo_root": str(repo), "task_id": "abc", "timeout_seconds": 5}, Context()
    )

    kinds = [c[0] for c in bg.calls]
    assert kinds == ["get", "list", "wait"]
    for call in bg.calls:
        # A resolved Path, not the raw string the caller passed.
        assert isinstance(call[1][0], Path)
        assert call[1][0] == repo.resolve()
    assert bg.calls[1][2]["limit"] == 3
    assert bg.calls[2][2]["timeout_seconds"] == 5


@pytest.mark.parametrize(
    "requested, clamped",
    [(0, 1), (-5, 1), (5, 5), (100_000, 100_000), (999_999, 100_000)],
)
@pytest.mark.asyncio
async def test_wait_run_clamps_its_timeout(wired, requested: int, clamped: int) -> None:
    """The documented clamp is [1, 100_000]; both ends must hold."""
    from mcp.server.mcpserver import Context

    _server, tools, _runner, bg, repo = wired
    await tools["wait_run"].run(
        {"repo_root": str(repo), "timeout_seconds": requested}, Context()
    )
    assert bg.calls[0][2]["timeout_seconds"] == clamped


# --------------------------------------------------------------- event sinks
class _FakeSession:
    def __init__(self):
        self.sent: list[Any] = []

    async def send_notification(self, notification):
        self.sent.append(notification)


class _FakeRequestContext:
    def __init__(self, meta, session):
        self.meta = meta
        self.session = session


class _FakeCtx:
    def __init__(self, meta, session):
        self._request_context = _FakeRequestContext(meta, session)

    @property
    def request_context(self):
        return self._request_context


@pytest.mark.parametrize("meta", [None, {}, {"progress_token": None}])
def test_no_progress_token_means_no_sink(meta) -> None:
    """Runs stay silent for clients that never asked for streaming."""
    ctx = _FakeCtx(meta, _FakeSession())
    assert server_module._build_event_sink(ctx) is None
    assert server_module._build_wait_event_sink(ctx) is None


def test_absent_context_means_no_sink() -> None:
    assert server_module._build_event_sink(None) is None
    assert server_module._build_wait_event_sink(None) is None


@pytest.mark.asyncio
async def test_worker_sink_payload_shape() -> None:
    """`_meta.acpEvent` and the caller's token are a client-visible contract."""
    session = _FakeSession()
    sink = server_module._build_event_sink(
        _FakeCtx({"progress_token": "tok-1"}, session)
    )
    assert sink is not None

    await sink({"type": "tool_call", "title": "ls -la"}, 1)
    await sink({"type": "agent_message_chunk"}, 2)

    params = [n.params for n in session.sent]
    assert [p.progress_token for p in params] == ["tok-1", "tok-1"]
    assert [p.progress for p in params] == [1.0, 2.0]
    # `title` is preferred as the human-readable message, else `type`.
    assert [p.message for p in params] == ["ls -la", "agent_message_chunk"]
    assert params[0].meta == {"acpEvent": {"type": "tool_call", "title": "ls -la"}}


@pytest.mark.asyncio
async def test_worker_sink_message_is_none_for_a_non_string_title() -> None:
    session = _FakeSession()
    sink = server_module._build_event_sink(
        _FakeCtx({"progress_token": 7}, session)
    )
    await sink({"title": 42}, 1)
    assert session.sent[0].params.message is None
    # An integer progressToken is legal on the wire and must pass through.
    assert session.sent[0].params.progress_token == 7


@pytest.mark.asyncio
async def test_wait_sink_counter_is_monotonic_across_tasks() -> None:
    """MCP requires strictly increasing `progress` per token.

    A single wait_run forwards events from several background runs, each with
    its own per-run counter restarting at 1, so the wait-level counter must
    keep climbing while `perTaskCount` preserves the per-run number.
    """
    session = _FakeSession()
    sink = server_module._build_wait_event_sink(
        _FakeCtx({"progress_token": "tok-2"}, session)
    )
    assert sink is not None

    await sink({"type": "a"}, 1, "task-A")
    await sink({"type": "b"}, 1, "task-B")   # per-task counter restarts...
    await sink({"type": "c"}, 2, "task-A")

    params = [n.params for n in session.sent]
    progress = [p.progress for p in params]
    assert progress == [1.0, 2.0, 3.0]       # ...wait-level counter does not
    assert progress == sorted(progress)
    assert len(set(progress)) == len(progress)
    assert [p.meta["taskId"] for p in params] == ["task-A", "task-B", "task-A"]
    assert [p.meta["perTaskCount"] for p in params] == [1, 1, 2]
    assert params[0].meta["acpEvent"] == {"type": "a"}


@pytest.mark.asyncio
async def test_wait_sink_message_is_none_for_a_non_string_title() -> None:
    """The wait sink normalises its message exactly like the worker sink.

    Both build the same field independently, so covering only one leaves the
    other free to send a stringified non-string where the protocol expects
    either a string or nothing.
    """
    session = _FakeSession()
    sink = server_module._build_wait_event_sink(
        _FakeCtx({"progress_token": "tok-3"}, session)
    )

    await sink({"title": 42}, 1, "task-A")
    await sink({}, 2, "task-A")

    assert [n.params.message for n in session.sent] == [None, None]

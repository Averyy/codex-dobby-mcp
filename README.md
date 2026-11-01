# Codex Dobby MCP

A local stdio MCP server that lets Claude delegate scoped work to Codex with structured results, persistent artifacts, and guardrails around filesystem access, review fan-out, and reverse-engineering workflows.

It gives Claude a sharper tool surface than a raw shell handoff: planning stays read-only, builds stay scoped, reviews can require specialist subagents, and every run leaves behind inspectable logs and outputs. It's especially useful for long-running analysis, Ghidra sessions, and deeper code review passes.

## Tools

| Tool | Description |
| --- | --- |
| `plan` | Break down a task and propose a scoped plan without editing files. |
| `research` | Investigate code, docs, and context in read-only mode and report findings. |
| `brainstorm` | Evaluate an idea, scope an MVP, and recommend whether it is worth building. |
| `build` | Implement a change, run focused verification, and report results. |
| `validate` | Run existing repo validation commands (build, test, lint) and report the results. |
| `review` | Review code or changes directly for one selected agent, or fan out to one Codex subagent per selected review agent when multiple agents are selected. |
| `reverse_engineer` | Use reverse-engineering tooling and broader roots to investigate binaries. |
| `start_run` | Start any Dobby tool in the background and return immediately with a task id. |
| `get_run` | Fetch the status or final result for a background run by task id. |
| `wait_run` | Block until one or more background runs finish (or a timeout elapses), then return the first result. |
| `list_runs` | List recent runs for a repo so you can recover task ids and results after timeouts. |

## Requirements

- Python 3.12+
- `uv`
- MCP Python SDK 2.x (`mcp[cli]>=2.0.0,<3`), installed automatically as a dependency
- Codex CLI installed and authenticated. Multi-agent review requires a CLI that lists `multi_agent_v2` in `codex features list`; Dobby verifies this before fan-out. The rollout parser retains a `0.147.0` compatibility fixture, and the authenticated live baseline is `codex-cli 0.164.0`.
- A git worktree for the target repo

### Optional MCP dependencies

Some tools work best (or only work) when specific MCP servers are available in the parent environment:

- **`research` and `brainstorm`**: [fetchaller-mcp](https://github.com/Averyy/fetchaller-mcp) MCP server for web search, URL fetching, and Reddit browsing. Without it, these tools fall back to codebase-only analysis.
- **`reverse_engineer`**: [ghidra-mcp](https://github.com/bethington/ghidra-mcp) bridge for binary analysis via Ghidra. Without it, reverse engineering is limited to whatever other tools are available in the sandbox.

Dobby auto-detects these integrations from the active Codex MCP config. If an integration is not installed or not configured for the run, Dobby does not try to use it: the worker prompt explicitly tells Codex not to call it and to continue with the best non-integration path available.

## Install

Recommended local tool install:

```bash
uv tool install .
codex-dobby-mcp
```

One-off local execution without installing:

```bash
uvx --from . codex-dobby-mcp
```

When this package is published to PyPI, replace `.` with `codex-dobby-mcp`.

Development checkout:

```bash
uv sync
```

## Run From Source Checkout

```bash
uv run codex-dobby-mcp
```

Every tool requires an explicit absolute `repo_root`. Dobby does not use MCP metadata or the server process's cwd as an implicit target: stdio clients do not reliably transmit their active workspace, and source-checkout launchers often run from Dobby's own repo. Requiring the root prevents a request from silently running against the Dobby checkout or another unrelated worktree.

Example launch with an installed tool:

```json
{
  "mcpServers": {
    "codex-dobby": {
      "command": "codex-dobby-mcp"
    }
  }
}
```

Example launch from a source checkout:

```json
{
  "mcpServers": {
    "codex-dobby": {
      "command": "uv",
      "args": ["--project", "/ABSOLUTE/PATH/TO/codex-dobby-mcp", "run", "codex-dobby-mcp"]
    }
  }
}
```

## Recommended Claude Code setup

If you use Dobby from Claude Code, add this to your `CLAUDE.md` so Claude delegates correctly:

```markdown
## Delegating Work to Dobby (codex-dobby MCP)

Offload grunt work — build/test, code review, research, planning, implementation, brainstorming, reverse engineering — to the `codex-dobby` MCP tools instead of doing it inline. Saves tokens and context.

- Give focused prompts with a concrete outcome. One task per call — if you have multiple things to ask, make multiple Dobby calls (in parallel when independent) instead of bundling them into one vague prompt.
- Call `mcp__codex-dobby__*` directly. Never wrap them in a general-purpose Agent/Task subagent.
- Always pass the absolute path of the current repository as `repo_root` on every Dobby call, including `get_run`, `wait_run`, and `list_runs`.
- Don't lower `timeout_seconds` below the default. Err too long — a short timeout kills the run; a long one costs nothing because Dobby returns as soon as it's ready.
- For `review`, omit `agents` or pass one lens for a direct single-agent review. Pass two or more of `generalist`, `security`, `performance`, `architecture`, `correctness`, `ux`, and `regression` for v2 fan-out; use `timeout_seconds=1200` for multi-agent review.
- For long work, start it with `mcp__codex-dobby__start_run` and then either block on `mcp__codex-dobby__wait_run` (parent sleeps in the tool call) or poll `mcp__codex-dobby__get_run` (parent keeps working). On Claude Code, `/loop` or `ScheduleWakeup` can schedule the polls for you so the parent is free between checks.
- Always wait for `start_run` to return its `task_id` before calling `wait_run` or `get_run`; do not race the start and lookup calls in parallel.
- To resume, continue, or check on a background run, call `wait_run`/`get_run` with its `task_id` — a Dobby run takes no further input once started, so to change course let it finish (or cancel) and `start_run` again.
```

## Requests

Every tool requires `repo_root`, the absolute path to the target git worktree. Worker tools also take `prompt`; their other common params are `files`, `important_context`, `timeout_seconds`, `extra_roots`, `model`, and `reasoning_effort`. Tool-specific: `danger` (`build`, `reverse_engineer`), `agents` (`review`).

`review` agents: `generalist` (default), `security`, `performance`, `architecture`, `correctness`, `ux`, `regression`. Zero or one effective agent runs directly. Pass two or more for explicit multi-agent v2 fan-out, normally with `timeout_seconds=1200`. Multi-agent review costs more tokens and latency than a direct review because it runs a parent plus every selected child, so select only lenses that materially help the task.

`start_run` takes the same params as the target tool, plus required `tool`. If `timeout_seconds` is omitted, it defaults to that target tool's normal timeout, except multi-agent review automatically uses 1200 seconds. Non-empty `agents` are only accepted when `tool` is `review`; other tools reject them with a validation error. Independent `start_run` calls may execute in parallel, but wait for each call's own response before looking up its returned `task_id`.

`get_run` params: `task_id`, `repo_root`.

`wait_run` params: `repo_root`, optional `task_id` (single run), optional `task_ids` (list — first-to-finish wins), `timeout_seconds` (default 540s / 9 min, clamped to `[1, 100_000]` / ~27.8 hours — matching Claude Code's `MCP_TOOL_TIMEOUT` default of `100000000` ms). Omit both `task_id` and `task_ids` to wait on every currently-live run for the repo. Passing both is rejected; an empty `task_ids` list is rejected. On timeout returns a `running` lookup whose `pending_task_ids` lists the ids still outstanding, and whose `summary` instructs the caller to re-call `wait_run` with that list until one finishes. Pick `timeout_seconds` below your MCP client's own `tools/call` ceiling — Claude Code defaults to ~28 hours (so the full clamp is usable); Codex CLI defaults to 60s per `[mcp_servers.<id>].tool_timeout_sec` (so raise that in `~/.codex/config.toml` before using long waits from Codex); Claude Desktop / Cursor / Cline / Continue vary and may cap low.

`list_runs` params: `repo_root`, optional `limit`.

For clients with a short `tools/call` ceiling (Claude Desktop ~60s, unconfigured Codex CLI 60s), prefer `start_run` + `get_run`/`list_runs` for long `review`, `research`, `build`, `validate`, or `reverse_engineer` work. Where the ceiling is raised (Claude Code defaults to ~28h, Codex CLI with `tool_timeout_sec` overridden), `start_run` + `wait_run` is usually fewer round-trips than polling.

## Defaults

Default model is `gpt-6.1-sol` for all tools and review subagents. Any explicit `timeout_seconds` must be at least 300s.

Pass `model` to pick a different one. The tool schema advertises these known IDs, but the field is a free string, so any other ID Codex accepts is passed through unchanged:

| `model` | Notes |
| --- | --- |
| `gpt-6.1-sol` | Default. Latest workhorse model for coding and everyday work. |
| `gpt-6-astra` | Frontier intelligence for the most demanding work. |
| `gpt-6.1-terra` | GPT-6.1 Terra Codex model. |
| `gpt-6-sol` | Previous-generation workhorse model. |
| `gpt-6-luna` | Fast and affordable model for easier tasks. Does not support `ultra`. |

`reasoning_effort` accepts `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, and `ultra`.

| Tool | Timeout | Reasoning | Sandbox |
| --- | --- | --- | --- |
| `plan` | 600s | medium | read-only |
| `research` | 1200s | medium | read-only |
| `brainstorm` | 600s | medium | read-only |
| `review` | 600s default, 1200s recommended for multi-agent | medium | read-only |
| `validate` | 600s | medium | `-s workspace-write` |
| `build` | 1200s | high | `-s workspace-write` |
| `reverse_engineer` | 1800s | high | `-s workspace-write` |

These defaults start GPT-6.1 Sol at medium for most technical work, following [OpenAI's model selection guidance](https://developers.openai.com/api/docs/guides/model-selection). Build and reverse engineering use high for code-writing and deeper analysis. Pass `reasoning_effort` to override a default for a specific task: lower it (`low`) for easy, well-scoped work such as a quick lookup or a simple `research` question, and raise it for hard or high-stakes ones.

`build` and `reverse_engineer` switch to `danger-full-access` when `danger=true`.

`start_run`, `get_run`, and `list_runs` are control-plane tools and return immediately. `start_run` uses the selected target tool's timeout budget. `wait_run` is also a control-plane tool, but it intentionally blocks up to its own `timeout_seconds` (capped at 100_000s / ~27.8h, matching Claude Code's `MCP_TOOL_TIMEOUT` default) waiting for a background run to finish.

## Behavior

- Child Codex runs inherit the parent's environment, but Dobby seeds a private per-run `CODEX_HOME` under the system temp directory (`.../codex-dobby/<task-id>/codex-home`) instead of pointing children at the user's global Codex home directly.
- `research` prefers codebase evidence and uses fetchaller MCP tools when available. If fetchaller is not installed or not configured for the run, the worker is told not to call it and to continue without web MCP support.
- `validate` runs in the `workspace-write` sandbox with `approval_policy="never"` because validation often needs temp or cache writes; the worker prompt still forbids source edits and commits.
- `review` uses a direct single-lens path for one agent, or explicitly enables Codex multi-agent v2 for multiple. The v2 parent spawns one deterministic task per selected custom reviewer with the exact custom role, a canonical assignment bound to the requested repo/task/files/context, and `fork_turns="none"`, then collects parent-observed completion results. Codex 0.147 intentionally persists the `message` tool argument as opaque provider ciphertext, so Dobby requires every other spawn field to remain exact, accepts only a single high-entropy opaque message value provisionally, and binds it to a SHA-256 assignment identity attested by the independently correlated child terminal result. Before accepting the execution manifest, Dobby also cross-checks root v2 mode, canonical child task/thread/role, verified child identity and no child collaboration calls, one strict read-only terminal JSON result, and an identical completed payload observed by the parent through v2 agent state. Global evidence failures quarantine all findings. The wrapper strips the internal attestation and reconstructs returned findings, next steps, warnings, and per-agent summaries from verified child results instead of trusting parent-authored synthesis. Review runs disable every inherited MCP server, and Dobby caps v2 session slots and rejects any reviewer collaboration call. Codex 0.164 keeps children in v2 mode and may omit started events, so Dobby correlates each spawn output with the child session path. Single-agent review defaults to `gpt-6.1-sol` at `medium` reasoning. Multi-agent review uses the resolved model with a `low`-reasoning orchestration parent and `medium` reviewer children by default; an explicit reasoning override applies to reviewers while the parent stays `low`.
- `reverse_engineer` includes a Ghidra MCP workflow only when Ghidra is installed and configured for the run. When Dobby can discover Ghidra from the active Codex configs (`CODEX_HOME/config.toml` and repo-local `.codex/config.toml`), it adds the configured Ghidra MCP helper repo as a writable helper root. When a live Ghidra UDS socket runtime directory is discoverable, Dobby also mounts that runtime path so child reverse-engineering workers can reach the already-running Ghidra instance. In that live-UDS case, Dobby enables workspace-write network access and passes the discovered socket roots through `network.allow_unix_sockets` for the child Codex run. If Ghidra is not installed or not configured, the worker is told not to call `mcp__ghidra__*`.
- `reverse_engineer` responses include `reverse_engineer_details.ghidra`, which reports whether Ghidra was configured, whether the run used direct MCP calls or the mounted helper fallback, and which Ghidra calls were observed.
- `start_run` launches the selected Dobby tool in the server process and returns a `task_id` immediately. `get_run` first checks any still-live in-memory run, then falls back to the run artifacts on disk.
- `wait_run` awaits the same live in-memory task via `asyncio.wait_for` with `asyncio.shield`, so if the caller is cancelled (client disconnect, outer MCP timeout) the underlying background run keeps going and can still be recovered with `get_run`/`wait_run`. If the task isn't in the live registry — e.g. the server restarted — `wait_run` falls through to the on-disk artifact lookup without polling.
- Multi-task `wait_run` (`task_ids=[...]` or no ids → all-live) uses `asyncio.wait(..., return_when=FIRST_COMPLETED)` over shielded futures and returns the first run to finish. The response's `pending_task_ids` lists the still-outstanding ids; callers should re-call `wait_run(task_ids=pending_task_ids)` until it's empty. On timeout the primary entry is the first id in the input list; `pending_task_ids` holds every id still waiting.
- Result artifacts are replaced atomically, so `get_run` sees either the startup placeholder or the final persisted response instead of a partially written `result.json`.
- Synchronous worker tool calls (`review`, `research`, etc.) can still hit the caller's outer `tools/call` ceiling. Prefer `start_run` + `wait_run` / `get_run` when that's a concern.
- If an orchestrated review parent times out after one or more children delivered valid results, Dobby reconstructs a partial manifest and salvages those findings from the private Codex session rollouts. Undelivered, malformed, or blocked child output is not counted as usable coverage.

## Filesystem and Safety

- Read-only tools run in Codex `read-only`. Dobby still mounts the per-run artifact directory plus any in-repo `extra_roots`; `extra_roots` outside the repo are exposed as additional read-only roots, not writable roots.
- Mutating tools run in workspace-write and mount `extra_roots` writable via `--add-dir`.
- Mutating tools also ensure `.codex-dobby/` is present in `.gitignore`. Unsafe `.gitignore` targets, such as symlinks or multiply-linked files, fail closed.
- External worktree mutations during a read-only run (you editing files in another terminal, another process touching the repo) are surfaced as a warning, not an error. Codex's OS-level read-only sandbox blocks codex itself from writing, so detected changes are by definition external. The run still escalates to `stop_reason: sandbox_violation` only when the worker self-reports having modified files that the wrapper also observed.
- Child Codex runs no longer need write access to the parent `~/.codex/sessions`. Dobby seeds the child home from `CODEX_HOME/auth.json` and `CODEX_HOME/config.toml` when those files exist, mirrors referenced helper files from `CODEX_HOME` and `CLAUDE_CONFIG_DIR` into a current-user-owned `0700` private runtime, then points the child at that temp home. Existing task directories are rejected. Orchestrated review forces `history.persistence="save-all"` only in this isolated runtime because rollout evidence is required. Dobby deletes the runtime in finalization; if deletion fails, the persisted response is changed to `status: "error"`, names the retained sensitive path, and uses `completeness: "blocked"`. The server process therefore needs read access to the parent Codex and Claude config files plus read/write access to the temp runtime directory.
- `CODEX_DOBBY_ACTIVE=1` is set on child runs and Dobby refuses to run if already set. Every inherited MCP server is disabled for review, preventing recursive calls through renamed or wrapped Dobby entries. Other tool roles retain useful MCPs but disable recognizable Dobby entries.
- Commits are forbidden. A mutating worker that creates or moves a commit returns `status: "error"`.
- Artifact access fails closed on invalid `task_id` values and symlinked artifact roots or paths. Wrapper writes also fail closed on unsafe `.gitignore` targets.

## Artifacts

Each run writes to `<target-root>/.codex-dobby/runs/<task-id>/`: `request.json`, `prompt.txt`, `stdout.log`, `stderr.log`, `last_message.txt`, `result.json`, `output-schema.json`, `events.jsonl`. Multi-agent `review` logs are also JSONL inside `stdout.log`. `events.jsonl` is the durable record of streaming progress events for the run (one ACP-shaped event per line; see [Streaming progress](#streaming-progress) below). Treat `.codex-dobby/` as unredacted local logs.

Worker-facing tools (`plan`, `research`, `brainstorm`, `build`, `validate`, `review`, `reverse_engineer`) return `task_id`, `tool`, `status`, `summary`, `completeness`, `important_facts`, `next_steps`, `files_changed` (this run only), `file_diffs` (mutating tools only — see [File diffs](#file-diffs)), `artifact_paths`, `sandbox_violations`, `repo_root`, `exit_code`, `duration_ms`, `warnings`, `raw_output_available`, `model`, `reasoning_effort`, `result_state`, and `stop_reason` (see [Stop reasons](#stop-reasons)). `review` responses also include `review_details`: the raw requested and normalized effective reviewers, the orchestration protocol, evidence source, per-task manifest, integrity flag, coverage flag, and completed/partial/blocked/failed counts. For multi-agent review, `manifest_validated: true` proves that the manifest matches independently correlated Codex rollouts; it does **not** mean every reviewer finished. `coverage_complete: true` is the separate all-lenses-completed signal. Scope is not delivered through the orchestrating parent. Dobby writes a per-run copy of each reviewer's agent definition into the child's private runtime with that lens's canonical assignment inlined, and names it in the child's own configuration, so the parent chooses which reviewer starts but not what it reviews. A child that receives a task payload carrying a different canonical assignment is required to review the assignment Dobby gave it and report the discrepancy in `warnings`. The digest each child attests is a consistency check on top of that, not the only binding — on its own it would prove a child **saw** the assignment identity rather than **received** the assignment, since the parent knows the digest too. Together with the rollout correlation this rules out substituted reviewer roles, inherited parent context, nested delegation, undeclared sessions, and fabricated child results. Scope substitution is bound rather than made impossible: the child holds the authoritative assignment, but enforcement is an instruction the reviewer model follows, so a child persuaded by a crafted task payload while still echoing the expected digest would pass every mechanical check. Direct reviews use `coverage_complete: null`. When one lens fails an integrity check the run reports `status: error` and `completeness: blocked`, but `important_facts` may still carry findings from the lenses that passed — that content was independently verified from their own delivered payloads, and the failing lens contributes nothing, not even its summary. A successful verified fan-out reports `execution_evidence: "codex_session_rollouts"`. The public JSONL CLI stream still omits those v2 events, so Dobby reads the private rollout files before deleting the temporary runtime. `reverse_engineer` responses also include `reverse_engineer_details.ghidra`, with `mode`, `summary`, and the observed `mcp_calls` and `helper_calls`.

## Streaming progress

Every run writes ACP-shaped events to `<run-dir>/events.jsonl`, regardless of whether anyone is listening. When the MCP caller passes a `progressToken` in the `tools/call` `_meta`, Dobby also forwards each event live as an MCP `notifications/progress`. The numeric `progress` field is an event counter, `message` is a short human-readable label (the tool title or event type), and the structured event lives in `_meta.acpEvent` for clients that want it. Clients that don't pass a token receive nothing — runs continue normally.

Event vocabulary borrows ACP's `session/update` payload shapes: `agent_message_chunk` for streamed model text, `tool_call` / `tool_call_update` for tool invocations (with `kind` ∈ `read|edit|delete|move|search|execute|think|fetch|other`), and `plan` for worker outlines. The codex `--json` event types we map (and ignore) are documented in `src/codex_dobby_mcp/events.py`.

For background runs (`start_run` + `wait_run`), live events are forwarded only to whichever `wait_run` caller is currently subscribed. The full history stays available in `events.jsonl` regardless.

## Stop reasons

Each `ToolResponse` carries a `stop_reason` that classifies why the run ended. The first five values mirror ACP's `StopReason` enum verbatim:

- `end_turn` — model finished naturally (the typical success case)
- `max_tokens` — *reserved*; defined for a future ACP bridge but not currently emitted (codex surfaces no token-budget signal the runner maps)
- `max_turn_requests` — *reserved*; defined but not currently emitted
- `refusal` — worker explicitly refused the task (worker output set `refused: true`)
- `cancelled` — Dobby was cancelled or killed before completion

Plus four Dobby-specific extensions for failure modes ACP doesn't model:

- `timeout` — run hit `timeout_seconds`
- `stall` — codex went idle for too long and Dobby killed it
- `sandbox_violation` — read-only tool tried to write, mutating tool tried to commit
- `error` — codex exited non-zero or returned malformed output

Legacy run artifacts written before this field existed are still readable; `stop_reason` is optional on `RunLookupResponse`.

## File diffs

Mutating tools (`build`, `validate`, `reverse_engineer`) populate a `file_diffs: list[FileDiff]` field with one entry per changed file:

```jsonc
{
  "path": "/abs/repo/src/foo.py",
  "oldText": "...",      // null for new files
  "newText": "...",      // null for deleted files
  "truncated": false     // true for files Dobby couldn't represent as text
}
```

Field names use ACP's camelCase shape (`oldText`/`newText`) on the wire, in `result.json`, and in `events.jsonl`. The Pydantic model uses Pythonic snake_case fields internally with camelCase aliases, and every serialization path passes `by_alias=True` so on-disk and on-the-wire shapes match. Worker JSON also uses camelCase (the `oldText`/`newText` schema is enforced).

Files larger than 2 MB or that look binary are reported with `truncated: true` and both texts left as `null`. Files outside the repo root are not diffed. The wrapper detects diffs from snapshot deltas; workers may also attach `file_diffs` in their structured output, which Dobby uses to fill in gaps the wrapper couldn't capture (e.g. a stashed snippet of a very large file).

Read-only tools always have `file_diffs: []`.

## Async control tool return shapes

- `start_run` returns an `AsyncRunHandle` with `task_id`, `tool`, `state`, `summary`, `repo_root`, `artifact_paths`, `model`, and `reasoning_effort`
- `get_run` returns a `RunLookupResponse` with `task_id`, `state`, `summary`, `repo_root`, optional `tool`, optional `status`, optional `result_state`, optional `stop_reason`, optional final `result`, artifact metadata, and warnings
- `wait_run` returns the same `RunLookupResponse` shape as `get_run` plus an optional `pending_task_ids` list (populated whenever the caller passed `task_ids` or used the all-live mode). On timeout `state` is `running` and `result` is unset, on completion `state` is `finished` with the final `result` populated
- `list_runs` returns the resolved `repo_root` plus recent run summaries for that repo

## Async Runs

If your MCP client gives up on long blocking tool calls before Dobby finishes, use the async path:

```json
{
  "tool": "start_run",
  "arguments": {
    "tool": "review",
    "prompt": "Review the current uncommitted state",
    "repo_root": "/ABSOLUTE/PATH/TO/TARGET-REPO",
    "files": ["src/foo.ts", "ui/main.ts"]
  }
}
```

This returns quickly with a `task_id`. Then poll or recover the result:

```json
{
  "tool": "get_run",
  "arguments": {
    "task_id": "<task-id>",
    "repo_root": "/ABSOLUTE/PATH/TO/TARGET-REPO"
  }
}
```

If you lost the id because a previous blocking call timed out, `list_runs` reads `.codex-dobby/runs/` and shows recent task ids and summaries.

If you'd rather block than poll, call `wait_run`:

```json
{
  "tool": "wait_run",
  "arguments": {
    "task_id": "<task-id>",
    "repo_root": "/ABSOLUTE/PATH/TO/TARGET-REPO"
  }
}
```

To be woken by whichever of several runs finishes first, pass `task_ids` instead:

```json
{
  "tool": "wait_run",
  "arguments": {
    "task_ids": ["<task-id-1>", "<task-id-2>", "<task-id-3>"],
    "repo_root": "/ABSOLUTE/PATH/TO/TARGET-REPO"
  }
}
```

Omit both `task_id` and `task_ids` to wait on every currently-live run for the repo. On completion / timeout semantics, see the `wait_run` params in [Requests](#requests) — the short version: the response's `pending_task_ids` tells you what to re-call `wait_run` with.

States reported by `get_run` and `wait_run`:

- `running`: the server still has the run alive in memory
- `finished`: a final `ToolResponse` is available and `result_state` is `final`
- `unknown`: the run directory exists but no readable final result is available; if `result_state` is `placeholder`, only the startup placeholder artifact was written
- `not_found`: there is no matching run directory, or the supplied `task_id` is invalid

Important limitation: live background tracking is in-process. If the server restarts, running background work is lost. Completed results remain recoverable from `.codex-dobby/runs/`.

## Development

```bash
uv run pytest
uv build --offline --no-build-isolation
uv run mcp dev src/codex_dobby_mcp/server.py:app
uv run python -m codex_dobby_mcp
```

The default suite includes a sanitized production-shape rollout corpus captured from a successful `codex-cli 0.147.0` multi-agent v2 run and synthetic coverage for the `0.164.0` child-session shape. Before deploying against a new Codex CLI release, also run the authenticated opt-in smoke test:

```bash
CODEX_DOBBY_RUN_LIVE_V2=1 uv run pytest -m live tests/test_live_codex_v2.py
```

Tests marked `live` validate the flags Dobby emits against the real `codex exec --help`. On a machine without codex they skip cleanly. CI runs them in a dedicated lane against the latest published `@openai/codex` with `DOBBY_REQUIRE_LIVE=1`, which turns any skip of a no-auth live test — including a missing codex — into a hard failure, so upstream flag drift is caught and the lane can never silently degrade into a skip. Tests additionally marked `live_auth` need an authenticated Codex CLI and remain local-only.

Release tags must exactly match `project.version`; for version `0.1.4`, publish tag `v0.1.4`. The publish workflow checks this before both testing and building. A release candidate is ready only after the normal test suite, the live v2 smoke test, `uv build`, and an installed-wheel MCP initialize check all pass. `AGENTS.md`, `CHANGELOG.md`, release checks, tests, and the sanitized v2 corpus are included in the source distribution so downstream maintainers and coding agents receive the same operating guidance.

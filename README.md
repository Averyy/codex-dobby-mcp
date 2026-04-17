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
| `list_runs` | List recent runs for a repo so you can recover task ids and results after timeouts. |

## Requirements

- Python 3.12+
- `uv`
- Codex CLI installed and authenticated
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

Target repo is resolved in this order: explicit `repo_root` arg → MCP metadata (`_meta.repo_root`, `repo_root`, `repoRoot`, `working_directory`, `workingDirectory`, `cwd`) → server cwd. If your client sends working-directory metadata, that is enough. Otherwise wrap the launch with `cd`.

Safety guard: if `repo_root` is omitted and the prompt clearly references an absolute path inside a different git worktree, Dobby fails fast instead of silently defaulting to the server cwd. It also refuses to guess when the request only names relative files that do not exist under the server cwd. The caller should retry with explicit `repo_root` or correct working-directory metadata.

Example launch with an installed tool:

```json
{
  "mcpServers": {
    "codex-dobby": {
      "command": "sh",
      "args": ["-lc", "cd /ABSOLUTE/PATH/TO/TARGET-REPO && codex-dobby-mcp"]
    }
  }
}
```

Example launch from a source checkout:

```json
{
  "mcpServers": {
    "codex-dobby": {
      "command": "sh",
      "args": ["-lc", "cd /ABSOLUTE/PATH/TO/TARGET-REPO && uv --directory /ABSOLUTE/PATH/TO/codex-dobby-mcp run codex-dobby-mcp"]
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
- Don't lower `timeout_seconds` below the default. Err too long — a short timeout kills the run; a long one costs nothing because Dobby returns as soon as it's ready.
- If your MCP client has a short `tools/call` timeout, start long work with `mcp__codex-dobby__start_run` and then poll `mcp__codex-dobby__get_run`.
```

## Requests

Common params: `prompt`, `repo_root`, `files`, `important_context`, `timeout_seconds`, `extra_roots`, `model`, `reasoning_effort`. Tool-specific: `danger` (`build`, `reverse_engineer`), `agents` (`review`).

`review` agents: `generalist` (default), `security`, `performance`, `architecture`, `correctness`, `ux`, `regression`. Pass multiple for multi-agent review.

`start_run` takes the same params as the target tool, plus required `tool`. If `timeout_seconds` is omitted, it defaults to that target tool's normal timeout. Non-empty `agents` are only accepted when `tool` is `review`; other tools reject them with a validation error.

`get_run` params: `task_id`, optional `repo_root`.

`list_runs` params: optional `repo_root`, optional `limit`.

For clients with a hard `tools/call` timeout around 120 seconds, prefer `start_run` + `get_run`/`list_runs` for long `review`, `research`, `build`, `validate`, or `reverse_engineer` work.

## Defaults

Default model is `gpt-5.4`, except `review`: single-agent review defaults to `gpt-5.4-mini`, while multi-agent review keeps a `gpt-5.4` parent and injects `gpt-5.4-mini` review subagents. Any explicit `timeout_seconds` must be at least 300s.

| Tool | Timeout | Reasoning | Sandbox |
| --- | --- | --- | --- |
| `plan` | 600s | high | read-only |
| `research` | 1200s | medium | read-only |
| `brainstorm` | 600s | high | read-only |
| `review` | 600s default, 1200s recommended for multi-agent | medium | read-only |
| `validate` | 600s | medium | workspace-write via `--full-auto` |
| `build` | 1200s | high | workspace-write via `--full-auto` |
| `reverse_engineer` | 1800s | high | workspace-write via `--full-auto` |

`build` and `reverse_engineer` switch to `danger-full-access` when `danger=true`.

`start_run`, `get_run`, and `list_runs` are control-plane tools and return immediately. `start_run` uses the selected target tool's timeout budget.

## Behavior

- Child Codex runs inherit the parent's environment, but Dobby seeds a private per-run `CODEX_HOME` under the system temp directory (`.../codex-dobby/<task-id>/codex-home`) instead of pointing children at the user's global Codex home directly.
- `research` prefers codebase evidence and uses fetchaller MCP tools when available. If fetchaller is not installed or not configured for the run, the worker is told not to call it and to continue without web MCP support.
- `validate` runs in workspace-write `--full-auto` because validation often needs temp or cache writes; the worker prompt still forbids source edits and commits.
- `review` uses a direct single-lens path for one agent, or multi-agent orchestration (via `spawn_agent` over `codex exec --json`) for multiple. Single-agent review defaults to `gpt-5.4-mini` at `medium` reasoning. Multi-agent review uses a `gpt-5.4` parent at `medium` reasoning and injects `gpt-5.4-mini` reviewer subagents, also at `medium` by default.
- `reverse_engineer` includes a Ghidra MCP workflow only when Ghidra is installed and configured for the run. When Dobby can discover Ghidra from the active Codex configs (`CODEX_HOME/config.toml` and repo-local `.codex/config.toml`), it adds the configured Ghidra MCP helper repo as a writable helper root. When a live Ghidra UDS socket runtime directory is discoverable, Dobby also mounts that runtime path so child reverse-engineering workers can reach the already-running Ghidra instance. In that live-UDS case, Dobby enables workspace-write network access and passes the discovered socket roots through `network.allow_unix_sockets` for the child Codex run. If Ghidra is not installed or not configured, the worker is told not to call `mcp__ghidra__*`.
- `start_run` launches the selected Dobby tool in the server process and returns a `task_id` immediately. `get_run` first checks any still-live in-memory run, then falls back to the run artifacts on disk.
- Result artifacts are replaced atomically, so `get_run` sees either the startup placeholder or the final persisted response instead of a partially written `result.json`.
- Blocking tools like `review` and `research` still behave exactly as before. If a caller insists on waiting synchronously, that caller can still hit its own outer MCP timeout.

## Filesystem and Safety

- Read-only tools run in Codex `read-only`. Dobby still mounts the per-run artifact directory plus any in-repo `extra_roots`; `extra_roots` outside the repo are exposed as additional read-only roots, not writable roots.
- Mutating tools run in workspace-write and mount `extra_roots` writable via `--add-dir`.
- Mutating tools also ensure `.codex-dobby/` is present in `.gitignore`. Unsafe `.gitignore` targets, such as symlinks or multiply-linked files, fail closed.
- Child Codex runs no longer need write access to the parent `~/.codex/sessions`. Dobby seeds the child home from `CODEX_HOME/auth.json` and `CODEX_HOME/config.toml` when those files exist, mirrors referenced helper files from `CODEX_HOME` and `CLAUDE_CONFIG_DIR` into a private runtime, then points the child at that private temp home. The server process therefore needs read access to the parent Codex and Claude config files plus read/write access to the temp runtime directory.
- `CODEX_DOBBY_ACTIVE=1` is set on child runs and Dobby refuses to run if already set. Inherited `codex-dobby-mcp` entries are disabled so workers can't call back.
- Commits are forbidden. A mutating worker that creates or moves a commit returns `status: "error"`.
- Artifact access fails closed on invalid `task_id` values and symlinked artifact roots or paths. Wrapper writes also fail closed on unsafe `.gitignore` targets.

## Artifacts

Each run writes to `<target-root>/.codex-dobby/runs/<task-id>/`: `request.json`, `prompt.txt`, `stdout.log`, `stderr.log`, `last_message.txt`, `result.json`, `output-schema.json`. Multi-agent `review` logs are JSONL. Treat `.codex-dobby/` as unredacted local logs.

Worker-facing tools (`plan`, `research`, `brainstorm`, `build`, `validate`, `review`, `reverse_engineer`) return `task_id`, `tool`, `status`, `summary`, `completeness`, `important_facts`, `next_steps`, `files_changed` (this run only), `artifact_paths`, `sandbox_violations`, `repo_root`, `exit_code`, `duration_ms`, `warnings`, `raw_output_available`, `model`, `reasoning_effort`, and `result_state`. `review` responses also include `review_details`, where `requested_review_agents` is the raw caller-supplied list and `effective_review_agents` is the normalized/defaulted list Dobby actually used.

Async control tools return different structured payloads:

- `start_run` returns an `AsyncRunHandle` with `task_id`, `tool`, `state`, `summary`, `repo_root`, `artifact_paths`, `model`, and `reasoning_effort`
- `get_run` returns a `RunLookupResponse` with `task_id`, `state`, `summary`, `repo_root`, optional `tool`, optional `status`, optional `result_state`, optional final `result`, artifact metadata, and warnings
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

States reported by `get_run`:

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

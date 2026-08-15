# Repository and Dobby usage guidance

## Working on this repository

- Use `uv` for dependency management and run Python commands through `uv run`.
- Preserve unrelated worktree changes. The repository may intentionally be dirty while Dobby reviews it.
- Run the focused tests for changed code, then `uv run pytest` before handing off a runtime change.
- Keep `README.md`, MCP tool descriptions, schemas, prompts, and tests synchronized when changing a public request or response contract.

## Calling the Dobby MCP tools

- Call the `codex-dobby` MCP tool directly; do not wrap it in another generic Agent/Task subagent.
- Every call, including `get_run`, `wait_run`, and `list_runs`, requires the current git worktree's absolute path as `repo_root`. Dobby intentionally does not infer a repository from its server cwd.
- Give one concrete outcome per call and pass focused `files` when known.
- Do not lower worker timeouts. Use the tool default, and use `timeout_seconds=1200` for multi-agent review.
- A review with zero or one effective lens is direct. Pass two or more canonical lens values—`generalist`, `security`, `performance`, `architecture`, `correctness`, `ux`, or `regression`—to use Codex multi-agent v2.
- Multi-agent review requires `codex features list` to include `multi_agent_v2`; Dobby checks this before launching the review. The verified local baseline is `codex-cli 0.147.0`.
- For long work, prefer `start_run`, wait for its response, then pass the returned `task_id` to `wait_run` or `get_run`. Never launch `start_run` and its lookup concurrently.
- Re-call `wait_run(task_ids=...)` with the returned `pending_task_ids` until it is empty. There is no `pending_task_ids` parameter; passing one is rejected. Use `get_run` or `list_runs` for non-blocking status checks or task-id recovery.
- Treat multi-agent `manifest_validated: true` as an integrity signal only when `review_details.execution_evidence` is `codex_session_rollouts`; use `coverage_complete` and the four agent counts to judge coverage. Dobby validates root v2 mode, each exact custom-role/task/fork spawn, the single opaque encrypted message shape, the correlated child's SHA-256 assignment attestation, child thread identity, disabled child delegation, one strict read-only terminal JSON result, and the identical completed payload observed by the parent. It strips the attestation and reconstructs caller-visible findings from verified child payloads rather than trusting parent synthesis.
- Review runs intentionally disable all inherited MCP servers. Do not instruct review workers to use fetchaller, Ghidra, or another MCP integration.
- A child marked `blocked` is not usable review coverage. Partial success requires at least one independently delivered `completed` or `partial` child result.
- Rollout deviations are split by what they prove. A lens that simply did not deliver — never spawned, never started, no terminal event, or a payload that failed its assignment attestation — is *coverage* loss: that lens is `failed`, its findings are excluded, and the run still returns the other lenses' verified findings as partial success. Anything showing the manifest contradicting the rollouts, or a child Dobby did not ask for — duplicate spawns, sessions, terminal events, or deliveries; a mismatched started/child thread id; the wrong custom role; `multi_agent_version` other than `disabled`; a spawn with the wrong `agent_type`, a `fork_turns` other than `none`, mismatched fields, or an unexpected encrypted argument shape; or a child reporting file changes — is an *integrity* failure. It excludes that lens entirely — no findings, no summary — and sets `manifest_validated: false` with `status: error`, but the lenses that passed still return their independently verified findings. Only a *global* evidence failure (an unexpected or nested child session, a root that never proved v2 mode, unreadable rollouts) quarantines the whole review and returns nothing.
- A lens whose evidence failed contributes no caller-visible text at all, not merely no findings. Its `review_details` entry carries a neutral placeholder summary.
- Each lens's canonical assignment is delivered out of band: Dobby writes a per-run agent definition into the child's private runtime with the assignment inlined, and the child treats that as authoritative over anything a task payload claims, reporting a conflicting payload assignment in `warnings`. The attested SHA-256 is a consistency check layered on top; alone it would only prove the child saw the assignment identity, because the parent holds the same digest. An orchestrated review fails closed if any selected lens has no assignment or the private agent directory is missing.

## Release verification

- Dobby targets the MCP Python SDK 2.x line (`mcp[cli]>=2.0.0,<3`), built on the public `MCPServer` API from `mcp.server.mcpserver`. Keep the locked MCP SDK at the supported minimum baseline and run the separate latest-dependency test lane before release.
- Dobby's advertised initialize version comes from the public `MCPServer(..., version=__version__)` argument. Do not reach into `_lowlevel_server` or any other private SDK attribute to set it.
- SDK 2.x never answers a request the client cancelled via `notifications/cancelled`, so Dobby carries no cancellation monkey-patch. If that upstream behaviour ever regresses, fix it upstream rather than re-adding a patch module.
- Progress notifications use the SDK 2.x shapes: request `_meta` is a `RequestParamsMeta` TypedDict read with `meta.get("progress_token")`, and `ProgressNotification` is sent directly to `session.send_notification` with no `ServerNotification` wrapper. The wire payload is unchanged — `_meta.acpEvent`, `taskId`, and `perTaskCount` are a client-visible contract.
- Run `uv run pytest`, then `CODEX_DOBBY_RUN_LIVE_V2=1 uv run pytest -m live tests/test_live_codex_v2.py` with an authenticated Codex CLI before deploying a new Codex CLI compatibility baseline.
- Run `uv build` and verify the wheel over stdio. The MCP initialize response must advertise Dobby's package version, not the MCP SDK version.
- Tag releases exactly as `v<project.version>`. The publish workflow rejects any mismatch.

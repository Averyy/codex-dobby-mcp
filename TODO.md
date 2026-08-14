# TODO

## Migrate to MCP Python SDK 2.x

Migrate Dobby from the maintenance-only MCP Python SDK 1.x line to the stable 2.x line in a dedicated change. Do not combine the migration with unrelated runner or Codex orchestration work.

### Implementation

- Change the dependency from `mcp[cli]>=1.27.0,<2` to a tested MCP SDK 2.x range and refresh `uv.lock`.
- Replace `FastMCP` with `MCPServer` and update imports from `mcp.server.fastmcp.*` to `mcp.server.mcpserver.*` or other documented public locations.
- Pass Dobby's installed package version through the public `MCPServer(..., version=__version__)` argument.
- Remove the private `_mcp_server.version` compatibility helper.
- Remove `mcp_spec_patches.py` and its tests. SDK 2.x no longer responds to cancelled requests, so the v1 `RequestResponder.cancel` monkey-patch must not remain.
- Update progress-token access for snake_case protocol fields and send `ProgressNotification` directly without the removed `ServerNotification` root wrapper.
- Preserve Dobby's `_meta.acpEvent`, `taskId`, and `perTaskCount` progress payloads exactly.
- Update server tests for MCP SDK 2.x tool errors, protocol-model attributes, and any changed testing APIs.
- Update README, AGENTS.md, changelog, package metadata, and release compatibility notes.

### Compatibility risks to recheck

- MCP SDK 2.x is a breaking API migration and replaces `httpx` with `httpx2` internally.
- OpenTelemetry API tracing is enabled by default, although it is a no-op without an SDK/exporter and does not record tool arguments.
- Confirm current open MCP SDK 2.x issues do not affect Dobby's Pydantic-returning tool handlers, optional `Context` injection, structured output, or stdio transport.
- Recheck whether a newer 2.x patch release supersedes 2.0.0 before starting the migration.

### Required verification before deployment

- Run the full locked test suite on Python 3.12, 3.13, and 3.14.
- Run the full suite against the latest allowed dependencies.
- Test required and invalid `repo_root` inputs over a real MCP client boundary.
- Verify Pydantic structured output and every tool's input/output schema.
- Verify live progress notifications, including custom `_meta` fields and monotonic multi-run counters.
- Verify client cancellation does not emit a response for the cancelled request and does not drop the stdio transport.
- Verify a legacy 2025-era MCP initialize request, `tools/list`, and `tools/call` over stdio.
- Verify the initialize response advertises Dobby's package version rather than the MCP SDK version.
- Build wheel and source distributions, install the wheel in a clean environment, and repeat the MCP handshake there.
- Run a real Claude client smoke test for synchronous tools and `start_run` / `wait_run`.
- Run `CODEX_DOBBY_RUN_LIVE_V2=1 uv run pytest -m live tests/test_live_codex_v2.py` with an authenticated Codex CLI.

### Investigation references

- MCP SDK v2 release: <https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0>
- Migration guide: <https://py.sdk.modelcontextprotocol.io/migration/>
- What's new in v2: <https://py.sdk.modelcontextprotocol.io/whats-new/>

## Completed on 2026-08-14

The MCP SDK 2.x migration above is implemented; its verification checklist is
recorded there and all of it ran except the live Codex test, a real Claude
client smoke test, and the Linux CI matrix.

The review findings that previously filled this file are fixed, each with a
regression test verified to fail when its fix is reverted:

- Partial coverage survives a lens that never delivered. `_rollout_validation_issues`
  now returns `RolloutIssues(integrity, coverage)`; only integrity failures
  quarantine the review. Spawn-contract violations (wrong `agent_type`, a
  `fork_turns` other than `none`, mismatched fields, unexpected encrypted
  argument shape) stay integrity; only assignment-attestation failure and plain
  non-delivery are coverage.
- A failed lens contributes no caller-visible prose. Its `review_details`
  summary is a neutral placeholder, and the all-failed fallback no longer
  merges its warnings.
- `ReviewRolloutAgentEvidence.terminal_event_count` has no default, so no
  construction site can silently claim a terminal event it did not observe.
- The child runtime scratch tree is deleted after every run.
- Runtime-config mirroring refuses the home root and `sessions/`, decided on
  resolved paths and case-folded, so symlink aliases and case variants cannot
  bypass it.
- `_review_rollout_evidence` streams rollout JSONL instead of holding text,
  split lines, and parsed items at once.
- The runner uses the subagent messages the prompt actually embedded
  (`PromptLoader.render_prompt`) rather than deriving them a second time.
- `refused: true` with a non-blocked completeness is normalised to blocked with
  a warning; the pairing is documented in `assets/prompts/shared.md`.
- `review()` uses the same multi-agent timeout default as `start_run`.
- Dead code removed: the legacy multi-agent-v1 parsing cluster,
  `_capture_repo_snapshot`, and the `timeout_seconds <= 90/120/180` branches
  that the `ge=300` floor made unreachable. `review.md` no longer tells the
  parent to synthesise, prioritise, or deduplicate.
- `important_context` survives the MCP SDK's JSON pre-parse of optional string
  arguments (python-sdk #3055).
- Ruff is configured (`E4`, `E7`, `E9`, `F`, `B`) with a CI lint lane, and the
  latest-dependency lane now imports the installed distribution.

## Open

### Assignment attestation binds the digest, not the assignment

Raised by review of the fixes on 2026-08-14. Not introduced by them — it is a
property of the v2 evidence chain as designed — but the partial-coverage split
rests on it, so it is worth resolving deliberately.

`_result_with_verified_assignment_attestation` proves a child *saw* the
canonical assignment digest. It does not prove the child *received* the
canonical assignment. The parent holds the digest itself: it is line 4 of the
message embedded in the parent's own prompt. A parent steered by repository
content could therefore spawn the correct custom role with a substituted
message that instructs the child to open its summary with the expected
`Dobby assignment <sha>: ` prefix, and the lens would land `completed` with
`manifest_validated: true` and `execution_evidence: codex_session_rollouts`.

Two narrower weaknesses feed the same path:

- `_looks_like_opaque_encrypted_message` accepts any run of 128+ characters
  from `[A-Za-z0-9_=-]`, so underscore-separated English passes as ciphertext
  and skips the canonical-contract comparison entirely, with no encryption
  marker present.
- `_canonical_spawn_contract` binds only the first three lines, so the trailing
  requirements block of a spawn message is substitutable even on the
  plaintext-comparison path. The scope JSON is bound.

Nothing in the suite covers a child that attests the right digest from a wrong
message. Fixing this needs a binding the parent cannot forge — a per-run secret
the child echoes, or provider-side verification of the delivered plaintext —
rather than a digest the parent already knows.

### Smaller open items

- `_review_rollout_evidence` still retains every parsed record for the whole
  call. Streaming removed the text and split-lines copies (roughly 3x to 1x),
  but peak memory is still proportional to the whole rollout tree.
- `tests/fixtures/codex-0.147-v2-review` captures only the all-completed case.
  The never-delivered and partial-failure shapes still rely on the synthetic
  `_write_review_rollouts` writer, which can drift from real Codex output.
  Capturing a real partial-failure rollout would close that gap.
- When `_run_with_child_runtime` raises before returning a response (for
  example `Codex executable not found`), the cleanup warnings are logged but
  not surfaced, and `result.json` keeps the aborted stub that reports
  `stop_reason: cancelled`, which misstates the cause.
- A per-lens integrity failure returns `status: error` while the response still
  carries other lenses' verified `important_facts`. That content is
  independently verified, so it is deliberate, but it is worth stating in
  README alongside the integrity/coverage split.

- python-sdk #3067: tool argument models default to `extra="ignore"`, so a
  misspelled argument name is silently dropped rather than rejected. Upstream;
  no local workaround applied.
- Consider widening the ruff rule selection (import sorting, `UP`, `SIM`,
  `ASYNC`) in a dedicated change. `ASYNC240`/`ASYNC221` report ~130 findings
  that need individual judgement about which blocking calls are deliberate.

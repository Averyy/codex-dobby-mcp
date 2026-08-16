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

The MCP SDK 2.x migration above is implemented and its verification checklist
has now run in full, including `CODEX_DOBBY_RUN_LIVE_V2=1 uv run pytest -m live
tests/test_live_codex_v2.py` and a real Claude Code client smoke test. That
smoke test drove a live three-lens orchestrated review end to end against
codex-cli 0.147.0 and returned `manifest_validated: true`,
`coverage_complete: true`, `execution_evidence: codex_session_rollouts`, and
3/3 delivered child results. The Linux CI matrix still only runs in CI.

## Completed on 2026-08-15

- `important_context` reaches the worker byte for byte. The SDK's
  `pre_parse_json` rewrites any string argument whose annotation is not
  *exactly* `str`, so the previous `str | None` annotation silently reformatted
  JSON-shaped context and turned the literal text `null` into no argument at
  all. The parameter is now annotated plain `str` with a `WithJsonSchema` that
  keeps the published nullable contract byte-identical, verified by diffing all
  eleven published input schemas before and after.
- A duplicate spawn of a selected lens no longer quarantines the whole review.
  The global `spawn_call_count > len(definitions)` check double-counted an event
  that `_rollout_validation_issues` already reports per lens, contradicting
  AGENTS.md and discarding every other lens's verified findings.
- Sessions Dobby cannot account for fail closed. A rollout file with no readable
  `session_meta`, and a non-root session whose `thread_spawn.agent_path` is
  missing or non-string, were both skipped silently and could leave
  `manifest_validated: true` beside an unaccounted child session. An entirely
  empty rollout file is still not treated as a session.

The review findings that previously filled this file are fixed, each with a
regression test verified to fail when its fix is reverted:

- Partial coverage survives a lens that never delivered. `_rollout_validation_issues`
  now returns `RolloutIssues(integrity, coverage)`; only integrity failures
  exclude that lens and set `manifest_validated: false`; only a global evidence
  failure returns nothing at all. Spawn-contract violations (wrong `agent_type`, a
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

### Tightening the ciphertext shape check needs a real capture

`_looks_like_opaque_encrypted_message` accepts any run of 128+ characters from
`[A-Za-z0-9_=-]`, so underscore-separated prose passes as ciphertext and skips
the canonical-contract comparison. Calibrating an entropy or character-mix
threshold needs a captured *real* provider ciphertext: the checked-in fixture is
sanitized to a placeholder, and a threshold that rejected genuine ciphertext
would quarantine valid reviews — worse than the bypass it closes. Blocked on the
same fixture-capture item below.

The assignment-substitution weakness this used to feed is closed separately:
each lens's canonical assignment is now delivered out of band through the
per-run agent definition, so a substituted spawn message cannot change what a
reviewer reviews.

### Smaller open items

- `tests/fixtures/codex-0.147-v2-review` still captures only the all-completed
  case. A real partial-failure capture would let the never-delivered and
  spawn-contract paths be checked against genuine Codex output rather than the
  synthetic writer. Mitigated for now by
  `test_synthetic_rollout_writer_still_models_the_captured_shape`, which fails
  if the writer stops modelling any record or field the real capture contains.

- Consider widening the ruff rule selection (import sorting, `UP`, `SIM`,
  `ASYNC`) in a dedicated change. `ASYNC240`/`ASYNC221` report ~130 findings
  that need individual judgement about which blocking calls are deliberate.

### The verification chain rests on one property Dobby cannot enforce

Every rollout check assumes the read-only-sandboxed parent Codex process cannot
write under the private `CODEX_HOME/sessions`. That is Codex/Seatbelt sandbox
behaviour, not something this repo controls, and Codex writes its own root
rollout there. If a future Codex ever made `CODEX_HOME` agent-writable, a
prompt-injected parent could fabricate a child rollout and defeat every check
above at once. Worth re-confirming against Codex's actual sandbox policy when
moving the compatibility baseline off `codex-cli 0.147.0`.

### Known-equivalent mutants (do not "fix" with tests)

Mutation testing over every executed comparison, boolean operator, `not`, and
bool constant reports these as surviving. Each was checked and is genuinely
unkillable, so a test for it would assert nothing:

- `@dataclass(frozen=True)` -> `frozen=False` on six dataclasses. Nothing
  mutates these instances, so immutability has no observable effect.
- `_resolve`'s `elif tool == ToolName.REVIEW and review_uses_orchestrator(...)`.
  An earlier guard raises when a non-review tool carries `agents`, so by this
  line either the tool is REVIEW (both operands true) or `agents` is empty
  (both false). Pinned indirectly by
  `test_agents_on_a_non_review_tool_are_rejected_outright`.
- `_salvage_exec_trace`'s `i = j + 1 if j > i + 1 else i + 1`. `j` is
  initialised to `i + 2` and the inner loop only increments it, so the guard is
  a tautology and the `else` arm is unreachable.
- `_sandbox_violation_from_line`'s `if not candidates and isinstance(payload,
  str):` fallback is unreachable. `_sandbox_violation_candidates` already
  returns `[value]` for any string, including `""`, so `not candidates` is
  never true when the payload is a string. Dead defensive code rather than a
  test gap; safe to delete in a cleanup change.
- `write_json(..., by_alias=True)` at `runner.py` line 890. `FileDiff` is the
  only aliased model, so `by_alias` is observable — but that write is
  immediately overwritten by the one in `run_resolved`, whose own mutant *is*
  killed by `test_result_json_artifact_uses_camelcase_for_file_diffs`. The
  redundant write is worth removing on its own merits; both callers serialise
  the same response back-to-back.
- `_review_timeout_plan`'s `if wrap_up_seconds >= available_seconds:`. The
  `ge=300` timeout floor makes `available_seconds` at least 270 while
  `wrap_up_seconds` is capped at 120, so the branch is unreachable, as its
  comment already notes.

The remaining survivors are concentrated in salvage, stall-diagnostic, and
event-mapping text — paths that produce a human-readable message rather than a
verdict. Worth revisiting, but a wrong value there misleads a reader instead of
changing what Dobby returns.

### Smaller open items (continued)

- `_result_with_verified_assignment_attestation` proves the child *saw* the
  correct assignment identity, not that it reviewed that scope, and it does not
  bind the parent's spawn-message plaintext. The load-bearing scope defence is
  the out-of-band agent definition plus the child's instruction to prefer it.
  The code comment that overstated this is corrected; keep the distinction in
  mind before treating the digest as a scope binding.

- `encrypted_function_args == ["message"]` skips both the canonical-contract
  comparison and the opaque-shape check. That marker is Codex's own record that
  it encrypted the field, and the parent cannot write rollouts, so trusting it
  is deliberate; the plaintext is bound afterwards by the child's out-of-band
  assignment and attested digest. It would only fail open if a future Codex
  marked the field encrypted while persisting plaintext. Adding a shape check
  there is nearly free today but risks quarantining valid reviews if the
  provider format rotates — the same calibration problem as the item above, and
  blocked on the same real-ciphertext capture.

- `_reject_unknown_tool_arguments` reaches into `server._tool_manager`,
  `tool.fn_metadata.arg_model`, and `tool.parameters`. The SDK exposes no public
  hook for `extra="forbid"` (python-sdk #3067), and dropping the guard would
  silently restore whole-repo runs from a misspelled `files`. The declared range
  is `mcp>=2.0.0,<3`, so a compatible 2.x release could change those internals
  and break `create_server()` at import. The latest-dependency CI lane exercises
  exactly this path, so the breakage surfaces there rather than at a user's
  first call; revisit if the SDK ever adds a supported hook.

- Scope substitution is bound by instruction, not made impossible. The child
  holds the authoritative assignment and is told to report a conflicting task
  payload, but a reviewer model persuaded by a crafted payload while still
  echoing the expected digest would pass every mechanical check. Closing that
  needs provider-side verification of the delivered plaintext.

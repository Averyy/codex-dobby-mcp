# Changelog

All notable user-facing changes to Codex Dobby MCP are recorded here.

## 0.1.4 - unreleased

- Default to GPT-6.1 Sol. Start routine technical tools at medium reasoning, keep reverse engineering at high, and keep the multi-agent coordinator at low. Explicit model and effort overrides still work.
- Support Codex CLI 0.164 multi-agent review rollouts: correlate v2 child sessions with spawn outputs when started events are absent, cap session slots, and reject reviewer collaboration calls. Retain the Codex 0.147 rollout compatibility fixture.
- Require an explicit absolute `repo_root` on every MCP tool, including run lookup and wait tools.
- Move multi-lens review fan-out to Codex multi-agent v2 with deterministic custom reviewer roles, bounded concurrency, and independently verified rollout evidence.
- Distinguish manifest integrity (`manifest_validated`) from complete review coverage (`coverage_complete`) and salvage only strictly validated delivered child results.
- Harden private runtime ownership, cleanup, recursion prevention, review read-only enforcement, and malformed-output handling.
- Improve Claude-facing descriptions, multi-agent timeout defaults, background-run polling guidance, and packaged repository instructions.
- Advertise Dobby's own package version during MCP initialization and reject release tags that do not exactly match `project.version`.
- Migrate to the MCP Python SDK 2.x line (`mcp[cli]>=2.0.0,<3`), replacing `FastMCP` with the public `MCPServer` API and setting the advertised initialize version through the supported `version=` constructor argument instead of a private compatibility helper.
- Drop the MCP 1.x cancellation monkey-patch. SDK 2.x already declines to answer a cancelled request, so Dobby no longer patches the SDK at import time.
- Progress notification payloads are unchanged: `_meta.acpEvent`, `taskId`, and `perTaskCount` still arrive exactly as before.
- Fix multi-agent review discarding good results when one lens failed. A lens that never delivered is now reported as lost coverage and the run returns the remaining independently verified findings as partial success, instead of failing the whole review with `status: error` and `completeness: blocked`. Evidence that contradicts the manifest, or that shows a child spawned with the wrong role, a `fork_turns` other than `none`, an unexpected encrypted argument shape, a second session, or a file mutation, still quarantines the review.
- A review lens whose evidence failed no longer contributes any caller-visible text. Its per-agent `review_details` summary is a neutral placeholder rather than the excluded child's own words.
- `review(agents=[...])` now defaults to the same 1200-second budget as `start_run` for multi-agent fan-out, instead of half of it.
- A worker that returns `refused: true` with a non-blocked `completeness` is recorded as blocked with a warning, instead of failing the run with `Codex completed with invalid structured output` and losing the stated refusal reason. The pairing is now documented in the shared worker prompt.
- Delete the child's runtime scratch tree (`.codex-dobby/runs/<task-id>/runtime`) after every run. It held the child's `TMPDIR` and caches inside the repository and was never removed.
- Never mirror the parent runtime home itself or its `sessions/` tree into a child runtime, including through symlink aliases and case variants, which could clone credentials and historical rollouts and quarantine an otherwise valid review.
- Deliver each review lens's canonical assignment to the child out of band, through the per-run agent definition Dobby writes into the child's private runtime, rather than only through the orchestrating parent's spawn message. The parent still chooses which reviewer starts; it no longer chooses what that reviewer reviews, and a conflicting payload assignment must now be reported instead of followed.
- Reject unknown tool arguments instead of silently dropping them. A misspelled `files` previously became a whole-repo run; every tool now publishes `additionalProperties: false` and fails the call.
- Report the real cause when a run raises before producing a response. `result.json` kept the aborted placeholder blaming cancellation, which misled every later `get_run`.
- Fix non-ASCII caller input breaking multi-agent review. A prompt containing U+2028, U+2029, or U+0085 split the canonical assignment across lines and quarantined the whole review, and an emoji made the per-run agent definition unparseable. The canonical assignment is now ASCII-only, and TOML values are encoded as TOML rather than JSON, which cannot represent astral characters as valid TOML escapes.
- Preserve the runner's failure detail on background runs. `start_run` overwrote `result.json` with a thinner response, discarding the real cause and any warning that a private runtime with copied credentials could not be deleted.
- Accept JSON-looking text in `important_context` again. The MCP SDK JSON-decodes optional string arguments, so such values were rejected as "Input should be a valid string".
- Deliver `important_context` to the worker exactly as written. The SDK's JSON pre-parse silently rewrote JSON-shaped context — collapsing layout, reformatting numbers (`1e3` became `1000.0`), dropping duplicate keys, and unescaping `\uXXXX` — and turned the literal text `null` into no context at all. The published nullable schema is unchanged.
- Stop discarding a whole multi-agent review when one lens is spawned twice. A duplicate spawn is now that lens's integrity failure, and the lenses spawned exactly once still return their independently verified findings, matching the documented contract.
- Fail closed on a session Dobby cannot account for. A rollout file with any content but no readable session metadata, or a non-root session that never identifies which spawn produced it, previously was skipped silently and could leave `manifest_validated: true` despite an unaccounted child session. This holds during truncated-rollout recovery too, where such a file parses to no records at all.


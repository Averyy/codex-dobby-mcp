# Changelog

All notable user-facing changes to Codex Dobby MCP are recorded here.

## 0.1.4 - unreleased

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
- Accept JSON-looking text in `important_context` again. The MCP SDK JSON-decodes optional string arguments, so such values were rejected as "Input should be a valid string".


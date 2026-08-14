# Codex 0.147 multi-agent v2 rollout fixture

This is a sanitized, path-redacted compatibility fixture derived from a successful authenticated `codex-cli 0.147.0` multi-agent v2 review captured on 2026-08-14.

Only the production JSONL record types consumed by Dobby are retained. Thread IDs, repository paths, task text, findings, timestamps, and encrypted assignment ciphertext were replaced with deterministic test values. The nesting, field names, event ordering, opaque-message shape, child terminal payload, and parent delivery shape remain representative of the captured rollout.

This fixture is intentionally static. It prevents the production rollout parser and synthetic fixture writer from drifting together without detection.


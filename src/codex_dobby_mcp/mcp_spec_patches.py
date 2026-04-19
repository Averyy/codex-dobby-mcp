"""Spec-compliance patches for the `mcp` Python SDK.

The upstream SDK (<=1.27.0) sends an error response for a request that the client
has cancelled via `notifications/cancelled`. MCP spec 2025-03-26 explicitly forbids
this ("the receiver MUST NOT respond to the cancelled request"). Claude Code (and
other spec-compliant clients) purges the request id on cancel and then treats the
stray error response as an unknown-id protocol violation, which drops the stdio
transport and marks the server as failed.

We cannot fix this upstream fast enough to help current users, so we monkey-patch
`RequestResponder.cancel` to drop the `_send_response` call. The patch is applied
exactly once, and guarded so that any future library change which relocates the
relevant symbols surfaces as a loud ImportError/RuntimeError at startup rather
than silently reverting to the buggy behaviour.
"""

from __future__ import annotations

import inspect

_APPLIED = False
_ORIGINAL_CANCEL = None


def apply_spec_patches() -> None:
    """Idempotently apply the MCP spec-compliance patches."""
    global _APPLIED, _ORIGINAL_CANCEL
    if _APPLIED:
        return

    from mcp.shared import session as _session_module

    RequestResponder = getattr(_session_module, "RequestResponder", None)
    if RequestResponder is None:
        raise RuntimeError(
            "codex-dobby-mcp expected mcp.shared.session.RequestResponder; the MCP SDK "
            "layout has changed. Update mcp_spec_patches.py before upgrading."
        )

    original_cancel = getattr(RequestResponder, "cancel", None)
    if original_cancel is None or not inspect.iscoroutinefunction(original_cancel):
        raise RuntimeError(
            "codex-dobby-mcp expected RequestResponder.cancel to be an async method; "
            "the MCP SDK layout has changed. Update mcp_spec_patches.py."
        )

    required_attrs = ("_entered", "_cancel_scope", "_completed", "_session", "request_id")
    source = inspect.getsource(original_cancel)
    for attr in required_attrs:
        if attr not in source:
            raise RuntimeError(
                f"codex-dobby-mcp: RequestResponder.cancel no longer references '{attr}'. "
                "Update mcp_spec_patches.py after auditing the new upstream behaviour."
            )
    if "_send_response" not in source:
        # Upstream already stopped sending a response on cancel — nothing to patch.
        _APPLIED = True
        return

    async def _cancel_without_response(self) -> None:
        """Spec-compliant replacement: cancel and mark complete, no response.

        MCP spec 2025-03-26: 'the receiver MUST NOT respond to the cancelled request'.
        """
        if not self._entered:
            raise RuntimeError("RequestResponder must be used as a context manager")
        if not self._cancel_scope:
            raise RuntimeError("No active cancel scope")
        self._cancel_scope.cancel()
        self._completed = True

    _ORIGINAL_CANCEL = original_cancel
    RequestResponder.cancel = _cancel_without_response
    _APPLIED = True


def is_applied() -> bool:
    return _APPLIED

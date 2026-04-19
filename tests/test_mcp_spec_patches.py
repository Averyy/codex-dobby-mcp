"""Tests for the MCP SDK spec-compliance monkey-patch."""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest

from mcp.shared import session as _session_module

import codex_dobby_mcp.mcp_spec_patches as patches


@pytest.fixture(autouse=True)
def _reset_patch_state(monkeypatch: pytest.MonkeyPatch):
    """Isolate each test: restore the pristine upstream cancel method before every run.

    Another test module may have already imported server.py, which calls
    apply_spec_patches() at import time. Force one apply() if nothing has yet,
    so `_ORIGINAL_CANCEL` is populated with the true upstream method, then use
    it to restore before/after every test.
    """
    if patches._ORIGINAL_CANCEL is None:
        patches.apply_spec_patches()
    original_cancel = patches._ORIGINAL_CANCEL
    assert original_cancel is not None  # apply_spec_patches populates it
    responder_cls = _session_module.RequestResponder
    responder_cls.cancel = original_cancel
    monkeypatch.setattr(patches, "_APPLIED", False)
    yield
    # monkeypatch.delattr from within a test may have removed RequestResponder;
    # if so, pytest's monkeypatch teardown restores it after this fixture's teardown.
    current_cls = getattr(_session_module, "RequestResponder", None)
    if current_cls is not None:
        current_cls.cancel = original_cancel
    monkeypatch.setattr(patches, "_APPLIED", False)


def test_apply_spec_patches_replaces_cancel_with_non_responding_variant() -> None:
    original = _session_module.RequestResponder.cancel
    patches.apply_spec_patches()
    patched = _session_module.RequestResponder.cancel

    assert patched is not original
    assert inspect.iscoroutinefunction(patched)
    assert "_send_response" not in inspect.getsource(patched)
    assert patches.is_applied() is True


def test_apply_spec_patches_is_idempotent() -> None:
    patches.apply_spec_patches()
    first = _session_module.RequestResponder.cancel
    patches.apply_spec_patches()
    second = _session_module.RequestResponder.cancel
    assert first is second


@pytest.mark.asyncio
async def test_patched_cancel_does_not_call_send_response() -> None:
    patches.apply_spec_patches()

    session = AsyncMock()
    session._send_response = AsyncMock()

    class CancelScope:
        def __init__(self) -> None:
            self.cancel_called = False

        def cancel(self) -> None:
            self.cancel_called = True

    scope = CancelScope()

    class FakeResponder:
        cancel = _session_module.RequestResponder.cancel

        def __init__(self) -> None:
            self._entered = True
            self._cancel_scope = scope
            self._completed = False
            self._session = session
            self.request_id = 42

    responder = FakeResponder()
    await responder.cancel()

    assert scope.cancel_called is True
    assert responder._completed is True
    session._send_response.assert_not_awaited()


def test_apply_spec_patches_errors_when_cancel_signature_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def replacement(self, extra_arg) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr(_session_module.RequestResponder, "cancel", replacement)
    # Drop the _APPLIED flag so apply tries again on this mutated class.
    monkeypatch.setattr(patches, "_APPLIED", False)

    with pytest.raises(RuntimeError, match="no longer references"):
        patches.apply_spec_patches()


def test_apply_spec_patches_errors_when_responder_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(_session_module, "RequestResponder", raising=True)
    with pytest.raises(RuntimeError, match="RequestResponder"):
        patches.apply_spec_patches()

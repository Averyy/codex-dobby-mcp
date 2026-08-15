from __future__ import annotations

import tempfile

import pytest


@pytest.fixture(autouse=True)
def _contained_private_runtime_root(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
):
    """Keep private child runtimes out of the shared system temp.

    ``private_runtime_root`` defaults to ``tempfile.gettempdir()``, so any test
    that drives a real run puts a credential-shaped directory there. Most are
    cleaned on the way out, but the tests that deliberately simulate a failed
    private-runtime deletion cannot be — that is the state they are asserting —
    so each suite run left two behind in a location shared with everything else
    on the machine.

    Only ``tempfile.tempdir`` is redirected, not ``TMPDIR``: some tests assert
    on ``TMPDIR`` directly when resolving reverse-engineer writable roots.
    """
    monkeypatch.setattr(
        tempfile, "tempdir", str(tmp_path_factory.mktemp("private-runtime"))
    )

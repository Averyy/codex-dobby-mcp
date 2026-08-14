from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

from codex_dobby_mcp.models import ReviewAgent, ReviewAgentExecutionStatus
from codex_dobby_mcp.runner import _review_rollout_evidence


FIXTURE_ROOT = (
    Path(__file__).resolve().parent / "fixtures" / "codex-0.147-v2-review"
)
AGENTS = [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION]


def _expected_spawn_messages() -> dict[str, str]:
    return json.loads(
        (FIXTURE_ROOT / "expected_spawn_messages.json").read_text(encoding="utf-8")
    )


def test_captured_codex_0_147_v2_rollouts_remain_compatible(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    shutil.copytree(FIXTURE_ROOT / "sessions", codex_home / "sessions")
    expected_messages = _expected_spawn_messages()

    evidence = _review_rollout_evidence(codex_home, AGENTS, expected_messages)

    assert evidence.collected is True
    assert evidence.validation_issues == ()
    assert [agent.observed_status for agent in evidence.agents] == [
        ReviewAgentExecutionStatus.COMPLETED,
        ReviewAgentExecutionStatus.COMPLETED,
    ]
    assert [agent.child_result.summary for agent in evidence.agents if agent.child_result] == [
        "Confirmed correctness fixture result.",
        "Confirmed regression fixture result.",
    ]
    assert all(agent.spawn_contract_issues == () for agent in evidence.agents)


def test_fixture_assignment_digests_bind_the_complete_canonical_json() -> None:
    for message in _expected_spawn_messages().values():
        lines = message.splitlines()
        canonical_json = lines[2].removeprefix("Canonical assignment JSON: ")
        expected_digest = lines[3].removeprefix("Canonical assignment SHA-256: ")

        assert hashlib.sha256(canonical_json.encode("utf-8")).hexdigest() == expected_digest


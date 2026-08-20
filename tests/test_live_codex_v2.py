from __future__ import annotations

import os
from pathlib import Path
import shutil

import pytest

from codex_dobby_mcp.models import (
    InvocationRequest,
    ReviewAgent,
    ReviewExecutionEvidence,
    RunStatus,
    ToolName,
)
from codex_dobby_mcp.server import create_runner


RUN_LIVE_V2 = os.environ.get("CODEX_DOBBY_RUN_LIVE_V2") == "1"
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.live
@pytest.mark.live_auth
@pytest.mark.skipif(
    not RUN_LIVE_V2,
    reason="set CODEX_DOBBY_RUN_LIVE_V2=1 to use the authenticated Codex CLI",
)
@pytest.mark.asyncio
async def test_authenticated_codex_cli_multi_agent_v2_smoke() -> None:
    codex_binary = os.environ.get("CODEX_BINARY") or shutil.which("codex")
    assert codex_binary is not None, "CODEX_BINARY or codex on PATH is required"
    runner = create_runner(REPO_ROOT)
    runner.codex_binary = codex_binary
    request = InvocationRequest(
        prompt=(
            "Perform a narrow read-only smoke review of the MCP version helper. "
            "Return no finding unless there is a concrete defect."
        ),
        repo_root=str(REPO_ROOT),
        files=["src/codex_dobby_mcp/_version.py"],
        timeout_seconds=600,
        agents=[ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION],
    )

    response = await runner.run(ToolName.REVIEW, request)

    assert response.status == RunStatus.SUCCESS
    assert response.review_details is not None
    assert response.review_details.execution_evidence == ReviewExecutionEvidence.CODEX_SESSION_ROLLOUTS
    assert response.review_details.manifest_validated is True
    assert response.review_details.coverage_complete is True
    assert response.review_details.completed_agents == 2
    assert response.review_details.partial_agents == 0
    assert response.review_details.blocked_agents == 0
    assert response.review_details.failed_agents == 0

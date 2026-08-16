"""Type guards in the rollout reader, path resolution, and failure carry-over.

Each `isinstance(...) and isinstance(...)` in the rollout reader decides whether
a record is admitted as evidence. Relaxing one to `or` lets a malformed record
through into the verification chain, where it is counted as a spawn, a start, or
a delivery that never happened.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codex_dobby_mcp.models import (
    Completeness,
    GhidraUsageMode,
    InvocationRequest,
    ReviewAgent,
    ToolName,
)
from codex_dobby_mcp.paths import PathResolutionError
from codex_dobby_mcp.review_agents import review_agent_assets_root
from codex_dobby_mcp.runner import CodexRunner, _path_is_within_repo

ASSETS = Path(__file__).resolve().parents[1] / "src" / "codex_dobby_mcp" / "assets"


def _runner(spawn_root: Path) -> CodexRunner:
    return CodexRunner(
        spawn_root=spawn_root,
        prompts_root=ASSETS / "prompts",
        worker_schema_path=ASSETS / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agent_assets_root(ASSETS),
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


# ------------------------------------------------- relative-path preflight
def test_all_relative_files_missing_is_refused(tmp_path: Path) -> None:
    """Kills `if missing and len(missing) == len(relative_candidates):` -> `or`.

    Dobby refuses rather than silently running against an unrelated cwd. With
    `or`, an empty `missing` list still satisfies `len([]) == len([])` for a
    request whose files all exist, turning a valid run into a hard error.
    """
    repo = _repo(tmp_path)
    runner = _runner(repo)

    with pytest.raises(PathResolutionError, match="do not exist under server cwd"):
        runner._resolve(
            ToolName.PLAN,
            InvocationRequest(prompt="p", files=["nope/a.py", "nope/b.py"]),
        )


def test_a_partially_existing_file_set_is_allowed(tmp_path: Path) -> None:
    """Only a *complete* miss is refused; one real file means the cwd is right."""
    repo = _repo(tmp_path)
    (repo / "real.py").write_text("x\n", encoding="utf-8")
    runner = _runner(repo)

    spec = runner._resolve(
        ToolName.PLAN,
        InvocationRequest(prompt="p", files=["real.py", "missing.py"]),
    )
    assert spec.repo_root == repo.resolve()


def test_every_file_existing_is_allowed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "real.py").write_text("x\n", encoding="utf-8")
    runner = _runner(repo)
    assert runner._resolve(
        ToolName.PLAN, InvocationRequest(prompt="p", files=["real.py"])
    ).repo_root == repo.resolve()


def test_absolute_and_tilde_files_do_not_drive_the_preflight(tmp_path: Path) -> None:
    """Kills the `or` chain in the candidate filter.

    Absolute and `~` paths are not repo-relative, so they must not be collected
    as candidates — otherwise a request naming only absolute files would be
    refused for "not existing under server cwd".
    """
    repo = _repo(tmp_path)
    runner = _runner(repo)
    spec = runner._resolve(
        ToolName.PLAN,
        InvocationRequest(prompt="p", files=["/nowhere/a.py", "~/nowhere/b.py"]),
    )
    assert spec.repo_root == repo.resolve()


def test_missing_file_list_is_capped_at_five_with_a_remainder(tmp_path: Path) -> None:
    """Kills `"" if len(missing) <= 5 else ...` -> `< 5`.

    Exactly five missing files must render without a "+N more" suffix.
    """
    repo = _repo(tmp_path)
    runner = _runner(repo)

    with pytest.raises(PathResolutionError) as five:
        runner._resolve(
            ToolName.PLAN,
            InvocationRequest(prompt="p", files=[f"missing/f{i}.py" for i in range(5)]),
        )
    assert "more)" not in str(five.value)

    with pytest.raises(PathResolutionError) as six:
        runner._resolve(
            ToolName.PLAN,
            InvocationRequest(prompt="p", files=[f"missing/f{i}.py" for i in range(6)]),
        )
    assert "(+1 more)" in str(six.value)


# ------------------------------------------------------ review model routing
def test_direct_and_orchestrated_review_pick_different_defaults(tmp_path: Path) -> None:
    """Kills `tool == REVIEW and review_uses_orchestrator(...)` -> `or`.

    A direct review runs the reviewer model; an orchestrated one runs a cheap
    parent that only launches and waits. Collapsing the two would silently
    change the model and reasoning effort of every review.
    """
    from codex_dobby_mcp.review_agents import (
        REVIEW_SUBAGENT_DEFAULT_MODEL,
        REVIEW_SUBAGENT_DEFAULT_REASONING_EFFORT,
    )

    repo = _repo(tmp_path)
    runner = _runner(repo)

    direct = runner._resolve(
        ToolName.REVIEW, InvocationRequest(prompt="p", agents=[ReviewAgent.SECURITY])
    )
    assert direct.model == REVIEW_SUBAGENT_DEFAULT_MODEL
    assert direct.reasoning_effort.value == REVIEW_SUBAGENT_DEFAULT_REASONING_EFFORT

    orchestrated = runner._resolve(
        ToolName.REVIEW,
        InvocationRequest(
            prompt="p", agents=[ReviewAgent.SECURITY, ReviewAgent.CORRECTNESS]
        ),
    )
    # The two paths currently share a default model, so reasoning effort is
    # the discriminator: the orchestration parent runs cheap, the reviewer
    # does not.
    assert orchestrated.reasoning_effort.value == "low"
    assert direct.reasoning_effort.value != orchestrated.reasoning_effort.value


# --------------------------------------------------------------- path guard
@pytest.mark.parametrize(
    "path, inside",
    [
        ("inside.py", True),
        ("nested/deep.py", True),
        ("../outside.py", False),
        ("/etc/passwd", False),
    ],
)
def test_path_within_repo(tmp_path: Path, path: str, inside: bool) -> None:
    repo = _repo(tmp_path).resolve()
    assert _path_is_within_repo(path, repo) is inside


def test_path_within_repo_resolves_non_existent_paths(tmp_path: Path) -> None:
    """Kills `candidate.resolve(strict=False)` -> `strict=True`.

    Paths are checked before the worker creates them, so strict resolution
    would raise instead of answering.
    """
    repo = _repo(tmp_path).resolve()
    assert _path_is_within_repo("does/not/exist/yet.py", repo) is True
    assert _path_is_within_repo(str(tmp_path / "elsewhere" / "x.py"), repo) is False


# ------------------------------------------- background failure carry-over
def _persisted(tmp_path: Path, **overrides) -> Path:
    payload = {
        "status": "error",
        "summary": "RuntimeError: the real cause",
        "warnings": ["private runtime could not be deleted"],
        "duration_ms": 4321,
        "result_state": "final",
    }
    payload.update(overrides)
    path = tmp_path / "result.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _response(**overrides):
    from codex_dobby_mcp.models import ToolResponse

    base = dict(
        task_id="t", tool=ToolName.BUILD, status="error",
        summary="Background run failed: the real cause",
        completeness=Completeness.BLOCKED, model="m", reasoning_effort="high",
        artifact_paths={}, repo_root="/tmp/repo", warnings=["Background run failed: the real cause"],
    )
    base.update(overrides)
    return ToolResponse(**base)


class _Spec:
    def __init__(self, path: Path):
        class _A:
            result_json = path
        self.artifacts = _A()


def test_runner_summary_and_warnings_are_carried_into_the_background_response(
    tmp_path: Path,
) -> None:
    """Kills the `isinstance(x, str) and x.strip()` guards on both fields."""
    from codex_dobby_mcp.background_runs import BackgroundRunManager

    merged = BackgroundRunManager._with_persisted_failure_detail(
        _Spec(_persisted(tmp_path)), _response()
    )

    assert merged.warnings == [
        "RuntimeError: the real cause",
        "private runtime could not be deleted",
    ]
    assert merged.duration_ms == 4321


def test_blank_persisted_fields_are_not_carried(tmp_path: Path) -> None:
    from codex_dobby_mcp.background_runs import BackgroundRunManager

    merged = BackgroundRunManager._with_persisted_failure_detail(
        _Spec(_persisted(tmp_path, summary="   ", warnings=["  ", 7, "kept"])),
        _response(),
    )
    assert merged.warnings == ["kept"]


def test_a_recorded_duration_does_not_overwrite_a_real_one(tmp_path: Path) -> None:
    """Kills `response.duration_ms in (None, 0)` and the isinstance guard.

    The runner's measurement fills a gap; it must not replace a duration the
    background layer already measured.
    """
    from codex_dobby_mcp.background_runs import BackgroundRunManager

    merged = BackgroundRunManager._with_persisted_failure_detail(
        _Spec(_persisted(tmp_path)), _response(duration_ms=999)
    )
    assert merged.duration_ms == 999

    merged_zero = BackgroundRunManager._with_persisted_failure_detail(
        _Spec(_persisted(tmp_path)), _response(duration_ms=0)
    )
    assert merged_zero.duration_ms == 4321


def test_a_placeholder_artifact_is_never_folded_in(tmp_path: Path) -> None:
    from codex_dobby_mcp.background_runs import BackgroundRunManager

    original = _response()
    merged = BackgroundRunManager._with_persisted_failure_detail(
        _Spec(_persisted(tmp_path, result_state="placeholder")), original
    )
    assert merged is original


# ------------------------------------- reverse-engineer details on failure
def test_reverse_engineer_failure_details_only_for_that_tool(tmp_path: Path) -> None:
    """Kills `if spec.tool != ToolName.REVERSE_ENGINEER:` -> `==`."""
    from codex_dobby_mcp.background_runs import (
        _reverse_engineer_details_for_background_failure,
    )

    repo = _repo(tmp_path)
    runner = _runner(repo)

    plan_spec = runner._resolve(ToolName.PLAN, InvocationRequest(prompt="p"))
    assert _reverse_engineer_details_for_background_failure(plan_spec) is None

    re_spec = runner._resolve(
        ToolName.REVERSE_ENGINEER, InvocationRequest(prompt="p")
    )
    details = _reverse_engineer_details_for_background_failure(re_spec)
    assert details is not None
    assert details.ghidra.mode in {
        GhidraUsageMode.NOT_CONFIGURED,
        GhidraUsageMode.PRELAUNCH_FAILURE,
    }


def test_agents_on_a_non_review_tool_are_rejected_outright(tmp_path: Path) -> None:
    """`_resolve` refuses review lenses on any other tool.

    This guard is why the later `tool == REVIEW and review_uses_orchestrator(...)`
    branch cannot be reached with a non-review tool, and therefore why mutating
    that `and` to `or` is an equivalent mutant rather than a test gap: by then
    either the tool is REVIEW or `agents` is empty.
    """
    repo = _repo(tmp_path)
    runner = _runner(repo)

    with pytest.raises(ValueError, match="agents is only supported when tool=review"):
        runner._resolve(
            ToolName.PLAN,
            InvocationRequest(
                prompt="p", agents=[ReviewAgent.SECURITY, ReviewAgent.CORRECTNESS]
            ),
        )

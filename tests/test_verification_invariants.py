"""Invariants that mutation testing proved no other test was pinning.

Every test here was written against a specific surviving mutant: a one-token
change to the source that the rest of the suite accepted silently. They are
grouped by the guarantee they protect, and each docstring names the mutation it
kills so the intent survives future refactoring.
"""

from pathlib import Path
import subprocess

import pytest

from codex_dobby_mcp import runner as runner_module
from codex_dobby_mcp.codex_cli import build_codex_command
from codex_dobby_mcp.models import (
    Completeness,
    InvocationRequest,
    ReviewAgent,
    ToolName,
    WorkerResult,
)
from codex_dobby_mcp.paths import (
    PathResolutionError,
    _validate_private_directory_metadata,
    is_git_worktree,
)
from codex_dobby_mcp.prompts import PromptLoader
from codex_dobby_mcp.review_agents import review_agent_assets_root
from codex_dobby_mcp.runner import (
    CodexRunner,
    _assignment_digest_from_message,
    _canonical_spawn_contract,
    _looks_like_opaque_encrypted_message,
    _strict_worker_result,
)

ASSETS = Path(__file__).resolve().parents[1] / "src" / "codex_dobby_mcp" / "assets"


def _runner(spawn_root: Path) -> CodexRunner:
    return CodexRunner(
        spawn_root=spawn_root,
        prompts_root=ASSETS / "prompts",
        worker_schema_path=ASSETS / "schemas" / "worker-output.schema.json",
        review_agents_root=review_agent_assets_root(ASSETS),
    )


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


# --------------------------------------------------------------- strict parse
def _worker_payload(**overrides) -> str:
    import json

    base = {
        "summary": "did the thing",
        "completeness": "full",
        "important_facts": [],
        "next_steps": [],
        "files_changed": [],
        "warnings": [],
        "refused": False,
        "file_diffs": [],
        "review_execution": None,
    }
    base.update(overrides)
    return json.dumps(base)


@pytest.mark.parametrize(
    "field, loose_value",
    [
        ("refused", "false"),  # str -> bool
        ("refused", 0),  # int -> bool
        ("refused", 1),  # int -> bool
    ],
)
def test_worker_payload_parsing_is_strict(field: str, loose_value: object) -> None:
    """Kills `model_validate_json(..., strict=True)` -> `strict=False`.

    Worker output is a contract, and for a review child it is the only
    evidence Dobby trusts. Lax mode coerces types, so a payload that does not
    actually conform would be accepted and treated as a verified result.
    """
    payload = _worker_payload(**{field: loose_value})

    # The mutation is only meaningful if lax mode really would accept this.
    assert WorkerResult.model_validate_json(payload) is not None

    assert _strict_worker_result(payload) is None


def test_conforming_worker_payload_still_parses() -> None:
    result = _strict_worker_result(_worker_payload())
    assert result is not None
    assert result.completeness is Completeness.FULL


# ------------------------------------------------------- private dir symlinks
def test_private_directory_metadata_does_not_follow_symlinks(tmp_path: Path) -> None:
    """Kills `path.stat(follow_symlinks=False)` -> `follow_symlinks=True`.

    The private runtime holds copied credentials. Validating through a symlink
    would report the *target's* mode and owner, so an attacker-controlled link
    pointing at a legitimately 0700 directory would pass the check while the
    link itself was never vetted.
    """
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    # Following the link would see the compliant target and accept it.
    _validate_private_directory_metadata(real, "private root")

    with pytest.raises(PathResolutionError):
        _validate_private_directory_metadata(link, "private root")


# ------------------------------------------------- review disables MCP servers
@pytest.mark.parametrize("tool", [ToolName.REVIEW])
def test_review_disables_inherited_mcp_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: ToolName
) -> None:
    """Kills `tool != ToolName.REVIEW and ...` -> `or` / `==` on both servers.

    README and AGENTS.md state review runs disable every inherited MCP server;
    the comment in `_resolve` notes this is also what makes recursive-Dobby
    prevention independent of server names and wrapper commands. Nothing
    asserted it, so review could have silently regained fetchaller and Ghidra.
    """
    monkeypatch.setattr(runner_module, "mcp_server_is_enabled", lambda *a, **k: True)
    repo = _git_repo(tmp_path)
    runner = _runner(repo)

    review = runner._resolve(tool, InvocationRequest(prompt="look", agents=[]))
    assert review.fetchaller_available is False
    assert review.ghidra_available is False

    # The same configuration must still enable them for a non-review tool,
    # otherwise this test would pass simply because nothing is ever enabled.
    plan = runner._resolve(ToolName.PLAN, InvocationRequest(prompt="look"))
    assert plan.fetchaller_available is True
    assert plan.ghidra_available is True


# ------------------------------------------------------ spawn-message parsing
@pytest.mark.parametrize(
    "length, expected",
    [(127, False), (128, True), (129, True)],
)
def test_opaque_ciphertext_length_boundary(length: int, expected: bool) -> None:
    """Kills `len(message) >= 128` -> `> 128`."""
    assert _looks_like_opaque_encrypted_message("A" * length) is expected


def test_opaque_ciphertext_rejects_out_of_class_characters() -> None:
    assert _looks_like_opaque_encrypted_message("A" * 127 + " ") is False
    assert _looks_like_opaque_encrypted_message("A" * 127 + ".") is False


@pytest.mark.parametrize(
    "message",
    [
        "",  # no lines at all
        "role\nlens",  # only two lines: index 2 does not exist
        "role\nlens\nnot the prefix at all",  # third line lacks the prefix
        'role\nlens\nCanonical assignment JSON: ["not", "a", "dict"]',
        "role\nlens\nCanonical assignment JSON: {not json}",
    ],
)
def test_canonical_spawn_contract_rejects_malformed_messages(message: str) -> None:
    """Kills `len(lines) < 3` -> `<= 3` and the `or` -> `and` in the same guard.

    A parent that reshapes the spawn message must not be able to make the
    canonical-contract comparison silently unavailable.
    """
    assert _canonical_spawn_contract(message) is None


@pytest.mark.parametrize(
    "message",
    [
        # Exactly three lines. This is the only input that separates `< 3` from
        # `<= 3`: every shorter message is rejected by both, and every longer
        # one is accepted by both, so without this case the boundary is unpinned
        # and a mutation to `<= 3` would silently reject valid spawn messages.
        'role\nlens\nCanonical assignment JSON: {"task": "x"}',
        'role\nlens\nCanonical assignment JSON: {"task": "x"}\ntrailing prose',
    ],
)
def test_canonical_spawn_contract_accepts_the_real_shape(message: str) -> None:
    assert _canonical_spawn_contract(message) == ("role", "lens", {"task": "x"})


@pytest.mark.parametrize(
    "message",
    [
        "no digest here",
        # Two attestations: which one is authoritative is undefined, so neither is.
        "Canonical assignment SHA-256: " + "a" * 64 + "\nCanonical assignment SHA-256: " + "b" * 64,
        "Canonical assignment SHA-256: " + "a" * 63,  # too short
        "Canonical assignment SHA-256: " + "a" * 65,  # too long
        "Canonical assignment SHA-256: " + "A" * 64,  # uppercase is not the canonical form
        "Canonical assignment SHA-256: " + "g" * 64,  # not hex
    ],
)
def test_assignment_digest_rejects_non_canonical_attestations(message: str) -> None:
    """Kills the `len(matches) != 1 or re.fullmatch(...) is None` guard.

    The digest identifies which assignment a child attested to. Accepting two
    of them, or a non-hex string, would let a crafted message satisfy the
    attestation check.
    """
    assert _assignment_digest_from_message(message) is None


def test_assignment_digest_accepts_exactly_one_canonical_attestation() -> None:
    digest = "0123456789abcdef" * 4
    assert _assignment_digest_from_message(
        f"line\nCanonical assignment SHA-256: {digest}\nmore"
    ) == digest


# ------------------------------------------------------------- sandbox roots
def test_repo_root_is_not_passed_as_an_extra_add_dir(tmp_path: Path) -> None:
    """Kills `if extra_root != spec.repo_root:` -> `==`.

    The repo root is already the sandbox cwd. Inverting this both drops the
    genuinely extra writable roots and re-adds the repo, so the child's
    writable set stops matching what Dobby resolved.
    """
    repo = _git_repo(tmp_path)
    extra = tmp_path / "extra"
    extra.mkdir()
    runner = _runner(repo)
    spec = runner._resolve(
        ToolName.BUILD,
        InvocationRequest(prompt="build", extra_roots=[str(extra)]),
    )

    command = build_codex_command(
        spec,
        "/opt/homebrew/bin/codex",
        spec.artifacts.output_schema_json,
        review_agent_assets_root(ASSETS),
    )
    added = [
        command.argv[i + 1]
        for i, token in enumerate(command.argv)
        if token == "--add-dir"
    ]

    assert str(spec.repo_root) not in added
    assert str(extra.resolve()) in added


# ------------------------------------------------------------ git worktree
@pytest.mark.parametrize(
    "returncode, stdout, expected",
    [
        (0, "true\n", True),
        (1, "true\n", False),  # git failed; its stdout must not be trusted
        (0, "false\n", False),
        (128, "", False),
    ],
)
def test_is_git_worktree_requires_both_success_and_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
    expected: bool,
) -> None:
    """Kills `returncode == 0 and stdout == "true"` -> `or`."""

    class _Result:
        pass

    def fake_run(*args, **kwargs):
        result = _Result()
        result.returncode = returncode
        result.stdout = stdout
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert is_git_worktree(tmp_path) is expected


# ------------------------------------------------------- assignment fan-out
@pytest.mark.parametrize(
    "agents, expect_assignments",
    [
        ([], False),
        ([ReviewAgent.SECURITY], False),
        ([ReviewAgent.SECURITY, ReviewAgent.CORRECTNESS], True),
    ],
)
def test_out_of_band_assignments_only_for_real_fan_out(
    tmp_path: Path, agents: list[ReviewAgent], expect_assignments: bool
) -> None:
    """Kills `if len(definitions) <= 1:` -> `< 1`.

    With `< 1`, a single-lens review would build a per-run agent definition for
    a direct (non-orchestrated) run, writing the caller's prompt and context
    into a child runtime that direct review never launches.
    """
    loader = PromptLoader(ASSETS / "prompts")
    assignments = loader.review_subagent_assignments(
        request=InvocationRequest(prompt="review", agents=agents, timeout_seconds=1200),
        repo_root=tmp_path,
        sandbox_roots=[tmp_path],
        advisory_read_only_roots=[],
        fetchaller_available=False,
        ghidra_available=False,
    )

    assert bool(assignments) is expect_assignments

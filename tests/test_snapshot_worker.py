"""The out-of-process snapshot entry point.

`_capture_repo_snapshot_with_deadline` spawns this module with `python -m` and
parses its stdout as JSON, so the module's contract is a real cross-process
interface even though nothing in-process imports it. It had no coverage at all.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from codex_dobby_mcp.models import RepoSnapshot
from codex_dobby_mcp.snapshot_worker import main


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "tracked.py").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@e", "-c", "user.name=t",
         "commit", "-q", "-m", "init"],
        check=True,
    )
    return repo


def test_main_emits_one_json_snapshot_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    # Dirty the tree so the snapshot has something to report; a clean repo
    # serialises to all-empty collections and would assert nothing.
    (repo / "tracked.py").write_text("two\n", encoding="utf-8")
    (repo / "untracked.py").write_text("new\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["snapshot_worker", "--repo-root", str(repo)])

    assert main() == 0

    out = capsys.readouterr().out.strip()
    assert "\n" not in out, "the caller parses stdout as a single JSON document"
    snapshot = RepoSnapshot.model_validate(json.loads(out))
    assert "tracked.py" in snapshot.dirty_files
    assert "untracked.py" in snapshot.dirty_files
    assert snapshot.status_entries


@pytest.mark.parametrize(
    "flag, attr",
    [("--include-head", "include_head"), ("--use-metadata-fingerprints", "use_metadata_fingerprints")],
)
def test_flags_are_forwarded_to_the_snapshot_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str, attr: str
) -> None:
    """The two store_true flags must reach `_build_repo_snapshot` by name.

    The runner appends them conditionally, so a swapped or dropped flag would
    silently change snapshot semantics with no visible error.
    """
    repo = _repo(tmp_path)
    seen: dict[str, object] = {}

    from codex_dobby_mcp import snapshot_worker as worker

    def fake_build(root, *, include_head, use_metadata_fingerprints):
        seen.update(
            root=root,
            include_head=include_head,
            use_metadata_fingerprints=use_metadata_fingerprints,
        )
        return RepoSnapshot()

    monkeypatch.setattr(worker, "_build_repo_snapshot", fake_build)
    monkeypatch.setattr(sys, "argv", ["snapshot_worker", "--repo-root", str(repo), flag])

    assert main() == 0

    assert seen["root"] == Path(repo)
    assert seen[attr] is True
    other = (
        "use_metadata_fingerprints" if attr == "include_head" else "include_head"
    )
    assert seen[other] is False


def test_repo_root_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["snapshot_worker"])
    with pytest.raises(SystemExit):
        main()


def test_module_runs_as_a_subprocess_the_way_the_runner_spawns_it(
    tmp_path: Path,
) -> None:
    """End-to-end over the real `python -m` boundary the runner uses."""
    repo = _repo(tmp_path)

    (repo / "tracked.py").write_text("changed\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-m", "codex_dobby_mcp.snapshot_worker",
         "--repo-root", str(repo), "--include-head"],
        capture_output=True,
        text=True,
        cwd=str(repo),
    )

    assert completed.returncode == 0, completed.stderr
    snapshot = RepoSnapshot.model_validate(json.loads(completed.stdout))
    # --include-head is what makes the pre-run content available for diffing.
    assert snapshot.head_commit, "expected --include-head to resolve HEAD"
    assert "tracked.py" in snapshot.dirty_files

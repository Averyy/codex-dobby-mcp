"""File-diff reconstruction.

`file_diffs` is caller-visible output and the only record of what a mutating
run changed. The capture helpers and the merge in `_file_diffs_for_run` had no
boundary tests, so the size cap, the binary and symlink guards, the
deleted-vs-unreadable distinction, and the worker-fills-gaps rule were all
unpinned.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codex_dobby_mcp.models import FileDiff, ToolName, WorkerResult
from codex_dobby_mcp.runner import (
    _FILE_DIFF_MAX_BYTES,
    _capture_pre_run_contents,
    _file_diffs_for_run,
    _git_show_head_contents,
    _read_post_run_text,
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _commit(repo: Path, message: str = "c") -> str:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@e", "-c", "user.name=t",
         "commit", "-q", "-m", message],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _worker(diffs: list[FileDiff]) -> WorkerResult:
    return WorkerResult(
        summary="s", completeness="full", important_facts=[], next_steps=[],
        files_changed=[], warnings=[], refused=False, file_diffs=diffs,
        review_execution=None,
    )


# ----------------------------------------------------------- pre-run capture
def test_pre_run_capture_reads_text_and_skips_absolute_paths(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("hello\n", encoding="utf-8")

    captured = _capture_pre_run_contents(repo, ["a.py", "/outside/b.py"])

    assert captured == {"a.py": "hello\n"}


def test_pre_run_capture_skips_symlinks_and_non_files(tmp_path: Path) -> None:
    """Kills `full_path.is_symlink() or not full_path.is_file()` -> `and`/drop-not.

    Following a symlink here would read a file outside the repo into a
    caller-visible diff.
    """
    repo = _repo(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("SECRET\n", encoding="utf-8")
    (repo / "link.py").symlink_to(outside)
    (repo / "adir").mkdir()

    captured = _capture_pre_run_contents(repo, ["link.py", "adir"])

    assert captured == {}


@pytest.mark.parametrize(
    "size, expect_none",
    [(_FILE_DIFF_MAX_BYTES, False), (_FILE_DIFF_MAX_BYTES + 1, True)],
)
def test_pre_run_capture_size_cap_boundary(
    tmp_path: Path, size: int, expect_none: bool
) -> None:
    """Kills `len(data) > _FILE_DIFF_MAX_BYTES` -> `>=`. Exactly at the cap is kept."""
    repo = _repo(tmp_path)
    (repo / "big.txt").write_bytes(b"x" * size)

    captured = _capture_pre_run_contents(repo, ["big.txt"])

    assert ("big.txt" in captured) is True
    assert (captured["big.txt"] is None) is expect_none


def test_pre_run_capture_marks_binary_as_unrepresentable(tmp_path: Path) -> None:
    """Kills the `or _looks_binary(data)` -> `and` in the same guard."""
    repo = _repo(tmp_path)
    (repo / "bin.dat").write_bytes(b"ab\x00cd")

    assert _capture_pre_run_contents(repo, ["bin.dat"]) == {"bin.dat": None}


# -------------------------------------------------------------- HEAD lookup
def test_git_show_head_returns_committed_text(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("v1\n", encoding="utf-8")
    head = _commit(repo)

    assert _git_show_head_contents(repo, head, "a.py") == ("v1\n", False)


def test_git_show_head_treats_untracked_as_a_new_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("v1\n", encoding="utf-8")
    head = _commit(repo)

    assert _git_show_head_contents(repo, head, "never_committed.py") == (None, False)


@pytest.mark.parametrize(
    "size, expected",
    [(_FILE_DIFF_MAX_BYTES, False), (_FILE_DIFF_MAX_BYTES + 1, True)],
)
def test_git_show_head_size_cap_boundary(tmp_path: Path, size: int, expected: bool) -> None:
    """Kills `>` -> `>=` and the `return (None, True)` truncation flag."""
    repo = _repo(tmp_path)
    (repo / "big.txt").write_bytes(b"x" * size)
    head = _commit(repo)

    text, truncated = _git_show_head_contents(repo, head, "big.txt")

    assert truncated is expected
    assert (text is None) is expected


# ------------------------------------------------------------- post-run read
def test_post_run_read_distinguishes_deleted_from_unreadable(tmp_path: Path) -> None:
    """Kills `return (None, False)` -> `(None, True)` for a missing file.

    A deleted file must report `truncated=False`; conflating it with an
    unreadable one tells the caller the content was suppressed when in fact
    the file is gone, which is the whole point of the diff.
    """
    repo = _repo(tmp_path)

    assert _read_post_run_text(repo, "gone.py") == (None, False)

    (repo / "adir").mkdir()
    assert _read_post_run_text(repo, "adir") == (None, True)

    outside = tmp_path / "secret.txt"
    outside.write_text("SECRET\n", encoding="utf-8")
    (repo / "link.py").symlink_to(outside)
    assert _read_post_run_text(repo, "link.py") == (None, True)


@pytest.mark.parametrize(
    "size, expected",
    [(_FILE_DIFF_MAX_BYTES, False), (_FILE_DIFF_MAX_BYTES + 1, True)],
)
def test_post_run_read_size_cap_boundary(tmp_path: Path, size: int, expected: bool) -> None:
    repo = _repo(tmp_path)
    (repo / "big.txt").write_bytes(b"x" * size)

    text, truncated = _read_post_run_text(repo, "big.txt")

    assert truncated is expected
    assert (text is None) is expected


# ------------------------------------------------------------ diff assembly
@pytest.mark.parametrize("tool", [ToolName.PLAN, ToolName.RESEARCH, ToolName.REVIEW])
def test_read_only_tools_never_produce_diffs(tmp_path: Path, tool: ToolName) -> None:
    """Kills `tool not in MUTATING_TOOLS or not files_changed` -> `and`."""
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("x\n", encoding="utf-8")

    assert _file_diffs_for_run(
        tool=tool, repo_root=repo, files_changed=["a.py"],
        baseline_head=None, baseline_dirty_contents={}, worker_result=None,
    ) == []


def test_no_detected_changes_produces_no_diffs(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _file_diffs_for_run(
        tool=ToolName.BUILD, repo_root=repo, files_changed=[],
        baseline_head=None, baseline_dirty_contents={}, worker_result=None,
    ) == []


def test_diff_uses_baseline_contents_when_the_file_was_already_dirty(
    tmp_path: Path,
) -> None:
    """Kills `old_truncated = old_text is None` -> `is not None`.

    A file dirty at baseline has its pre-run text captured; when that capture
    succeeded the diff is not truncated.
    """
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("after\n", encoding="utf-8")

    diffs = _file_diffs_for_run(
        tool=ToolName.BUILD, repo_root=repo, files_changed=["a.py"],
        baseline_head=None,
        baseline_dirty_contents={"a.py": "before\n"},
        worker_result=None,
    )

    assert len(diffs) == 1
    assert diffs[0].old_text == "before\n"
    assert diffs[0].new_text == "after\n"
    assert diffs[0].truncated is False


def test_unrepresentable_baseline_marks_the_diff_truncated(tmp_path: Path) -> None:
    """A baseline capture of None means "could not represent", so truncated."""
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("after\n", encoding="utf-8")

    diffs = _file_diffs_for_run(
        tool=ToolName.BUILD, repo_root=repo, files_changed=["a.py"],
        baseline_head=None,
        baseline_dirty_contents={"a.py": None},
        worker_result=None,
    )

    assert diffs[0].truncated is True


def test_truncation_is_sticky_from_either_side(tmp_path: Path) -> None:
    """Kills `truncated = old_truncated or new_truncated` -> `and`.

    Either side being unrepresentable makes the whole diff untrustworthy.
    """
    repo = _repo(tmp_path)
    (repo / "big.txt").write_bytes(b"x" * (_FILE_DIFF_MAX_BYTES + 1))

    # new side truncated, old side fine
    diffs = _file_diffs_for_run(
        tool=ToolName.BUILD, repo_root=repo, files_changed=["big.txt"],
        baseline_head=None,
        baseline_dirty_contents={"big.txt": "small\n"},
        worker_result=None,
    )
    assert diffs[0].truncated is True

    # old side truncated, new side fine
    (repo / "small.txt").write_text("new\n", encoding="utf-8")
    diffs = _file_diffs_for_run(
        tool=ToolName.BUILD, repo_root=repo, files_changed=["small.txt"],
        baseline_head=None,
        baseline_dirty_contents={"small.txt": None},
        worker_result=None,
    )
    assert diffs[0].truncated is True


def test_absolute_paths_in_files_changed_are_skipped(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _file_diffs_for_run(
        tool=ToolName.BUILD, repo_root=repo, files_changed=["/outside/a.py"],
        baseline_head=None, baseline_dirty_contents={}, worker_result=None,
    ) == []


def test_worker_diff_fills_only_the_gaps_the_wrapper_could_not_capture(
    tmp_path: Path,
) -> None:
    """Kills the `and` -> `or` and `is None` -> `is not None` in the merge.

    The worker's text is consulted *only* where the wrapper's own value is
    None. Accepting it otherwise would let worker-authored content overwrite
    what the wrapper actually observed on disk.
    """
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("wrapper-new\n", encoding="utf-8")

    worker = _worker([FileDiff(path="a.py", old_text="worker-old",
                               new_text="worker-new", truncated=False)])

    diffs = _file_diffs_for_run(
        tool=ToolName.BUILD, repo_root=repo, files_changed=["a.py"],
        baseline_head=None,
        # Wrapper has no old text (file was not dirty at baseline and is not in
        # HEAD), so the worker's old_text fills that gap...
        baseline_dirty_contents={},
        worker_result=worker,
    )

    assert diffs[0].old_text == "worker-old"
    # ...but the wrapper DID read new text, so the worker must not override it.
    assert diffs[0].new_text == "wrapper-new\n"


def test_worker_diff_is_ignored_when_it_offers_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("wrapper-new\n", encoding="utf-8")
    worker = _worker([FileDiff(path="a.py", old_text=None, new_text=None,
                               truncated=False)])

    diffs = _file_diffs_for_run(
        tool=ToolName.BUILD, repo_root=repo, files_changed=["a.py"],
        baseline_head=None, baseline_dirty_contents={}, worker_result=worker,
    )

    assert diffs[0].old_text is None
    assert diffs[0].new_text == "wrapper-new\n"


def test_worker_diff_matches_on_the_resolved_absolute_path_too(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("wrapper-new\n", encoding="utf-8")
    absolute = str((repo / "a.py").resolve())
    worker = _worker([FileDiff(path=absolute, old_text="worker-old",
                               new_text=None, truncated=False)])

    diffs = _file_diffs_for_run(
        tool=ToolName.BUILD, repo_root=repo, files_changed=["a.py"],
        baseline_head=None, baseline_dirty_contents={}, worker_result=worker,
    )

    assert diffs[0].old_text == "worker-old"


def test_deleted_file_reports_new_text_none_and_is_not_truncated(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("v1\n", encoding="utf-8")
    head = _commit(repo)
    (repo / "a.py").unlink()

    diffs = _file_diffs_for_run(
        tool=ToolName.BUILD, repo_root=repo, files_changed=["a.py"],
        baseline_head=head, baseline_dirty_contents={}, worker_result=None,
    )

    assert diffs[0].old_text == "v1\n"
    assert diffs[0].new_text is None
    assert diffs[0].truncated is False


def test_worker_diff_never_overrides_old_text_the_wrapper_captured(
    tmp_path: Path,
) -> None:
    """Kills `old_text is None and worker_entry.old_text is not None` -> `or`.

    With `or`, any worker-supplied old_text replaces what the wrapper actually
    read from disk, so a worker could rewrite the "before" side of its own
    diff. The wrapper's observation must win wherever it has one.
    """
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("wrapper-new\n", encoding="utf-8")
    worker = _worker([FileDiff(path="a.py", old_text="worker-old",
                               new_text="worker-new", truncated=False)])

    diffs = _file_diffs_for_run(
        tool=ToolName.BUILD, repo_root=repo, files_changed=["a.py"],
        baseline_head=None,
        baseline_dirty_contents={"a.py": "wrapper-old-captured\n"},
        worker_result=worker,
    )

    assert diffs[0].old_text == "wrapper-old-captured\n"
    assert diffs[0].new_text == "wrapper-new\n"


def test_worker_new_text_fills_a_deleted_files_missing_side(tmp_path: Path) -> None:
    """Kills `worker_entry.new_text is not None` -> `is None`.

    The wrapper cannot read a file that no longer exists, so this is exactly
    the gap the worker's stashed copy is meant to fill. With the mutation the
    worker's text is dropped and the caller sees no "after" side at all.
    """
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("v1\n", encoding="utf-8")
    head = _commit(repo)
    (repo / "a.py").unlink()

    worker = _worker([FileDiff(path="a.py", old_text=None,
                               new_text="worker-recovered\n", truncated=False)])

    diffs = _file_diffs_for_run(
        tool=ToolName.BUILD, repo_root=repo, files_changed=["a.py"],
        baseline_head=head, baseline_dirty_contents={}, worker_result=worker,
    )

    assert diffs[0].old_text == "v1\n"
    assert diffs[0].new_text == "worker-recovered\n"

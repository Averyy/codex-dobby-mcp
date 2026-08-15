import os
from pathlib import Path

import pytest

from codex_dobby_mcp.gitignore import ensure_codex_dobby_ignored
from codex_dobby_mcp.paths import PathResolutionError


def test_gitignore_appends_codex_dobby_entry_once(tmp_path: Path) -> None:
    repo_root = tmp_path
    gitignore = repo_root / ".gitignore"
    gitignore.write_text("node_modules/\n", encoding="utf-8")

    assert ensure_codex_dobby_ignored(repo_root) is True
    assert ensure_codex_dobby_ignored(repo_root) is False

    lines = gitignore.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == ".codex-dobby/"
    assert lines.count(".codex-dobby/") == 1


def test_gitignore_rejects_symlinked_gitignore(tmp_path: Path) -> None:
    external = tmp_path / "outside.txt"
    external.write_text("", encoding="utf-8")
    (tmp_path / ".gitignore").symlink_to(external)

    with pytest.raises(PathResolutionError, match="symlinked \\.gitignore"):
        ensure_codex_dobby_ignored(tmp_path)


def test_gitignore_rejects_multiply_linked_gitignore(tmp_path: Path) -> None:
    external = tmp_path / "outside.txt"
    external.write_text("", encoding="utf-8")
    (tmp_path / ".gitignore").hardlink_to(external)

    with pytest.raises(PathResolutionError, match="multiply-linked \\.gitignore"):
        ensure_codex_dobby_ignored(tmp_path)


def test_gitignore_guards_survive_a_swap_after_the_check(tmp_path: Path) -> None:
    """The refusals must bind the opened file, not the pathname.

    Checking the path and then reopening it to append leaves a window in which
    the target can be replaced, so the append would follow a symlink the guard
    had just refused.
    """
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("coverage-html/\n", encoding="utf-8")

    real_fstat = os.fstat
    real_stat = os.stat

    swapped = False

    def swap() -> None:
        # Stand in for a racing process: replace the path with a symlink right
        # after it has been inspected. Uses no stat call of its own, since
        # os.stat is patched below.
        nonlocal swapped
        if swapped:
            return
        swapped = True
        gitignore.unlink()
        gitignore.symlink_to(outside)

    def swap_then_fstat(fileno):  # type: ignore[no-untyped-def]
        swap()
        return real_fstat(fileno)

    def swap_then_stat(target, *args, **kwargs):  # type: ignore[no-untyped-def]
        result = real_stat(target, *args, **kwargs)
        swap()
        return result

    # Hook whichever call the implementation uses to inspect the file, so the
    # swap lands in the window between inspection and write either way.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("codex_dobby_mcp.gitignore.os.fstat", swap_then_fstat)
    monkeypatch.setattr("codex_dobby_mcp.gitignore.os.stat", swap_then_stat)
    try:
        assert ensure_codex_dobby_ignored(tmp_path) is True
    finally:
        monkeypatch.undo()

    # The entry landed in the originally opened file, never in the swap target.
    contents = outside.read_text(encoding="utf-8")
    assert "secret" in contents
    assert ".codex-dobby/" not in contents


def test_gitignore_creates_the_file_when_absent(tmp_path: Path) -> None:
    assert not (tmp_path / ".gitignore").exists()

    assert ensure_codex_dobby_ignored(tmp_path) is True

    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == ".codex-dobby/\n"
    assert ensure_codex_dobby_ignored(tmp_path) is False


def test_gitignore_rejects_a_directory(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").mkdir()

    with pytest.raises(PathResolutionError, match="not a regular file"):
        ensure_codex_dobby_ignored(tmp_path)

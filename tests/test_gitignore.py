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

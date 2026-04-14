from __future__ import annotations

import os
from pathlib import Path

from codex_dobby_mcp.models import CODEX_DOBBY_GITIGNORE_ENTRY
from codex_dobby_mcp.paths import PathResolutionError


def ensure_codex_dobby_ignored(repo_root: Path) -> bool:
    gitignore_path = repo_root / ".gitignore"
    existing_lines: list[str] = []

    if gitignore_path.is_symlink():
        raise PathResolutionError(f"Refusing to update symlinked .gitignore: {gitignore_path}")
    if gitignore_path.exists():
        if not gitignore_path.is_file():
            raise PathResolutionError(f".gitignore is not a regular file: {gitignore_path}")
        if os.stat(gitignore_path).st_nlink > 1:
            raise PathResolutionError(f"Refusing to update multiply-linked .gitignore: {gitignore_path}")
        existing_lines = gitignore_path.read_text(encoding="utf-8").splitlines()
        if any(line.strip() in {CODEX_DOBBY_GITIGNORE_ENTRY, CODEX_DOBBY_GITIGNORE_ENTRY.rstrip("/")} for line in existing_lines):
            return False

    with gitignore_path.open("a", encoding="utf-8") as handle:
        if existing_lines and existing_lines[-1] != "":
            handle.write("\n")
        handle.write(f"{CODEX_DOBBY_GITIGNORE_ENTRY}\n")

    return True

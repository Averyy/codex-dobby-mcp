from __future__ import annotations

import errno
import os
from pathlib import Path
import stat

from codex_dobby_mcp.models import CODEX_DOBBY_GITIGNORE_ENTRY
from codex_dobby_mcp.paths import PathResolutionError


def ensure_codex_dobby_ignored(repo_root: Path) -> bool:
    """Append Dobby's ignore entry, refusing symlinked or multiply-linked files.

    The refusals are enforced against an open descriptor rather than the path.
    Checking ``is_symlink``/``st_nlink`` on the pathname and then reopening that
    pathname to append leaves a window in which the file can be replaced, so the
    append could follow a symlink or write through a second hard link — exactly
    what these guards exist to prevent.
    """
    gitignore_path = repo_root / ".gitignore"

    try:
        # O_NOFOLLOW fails rather than following a final-component symlink.
        descriptor = os.open(
            gitignore_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644
        )
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise PathResolutionError(
                f"Refusing to update symlinked .gitignore: {gitignore_path}"
            ) from exc
        if exc.errno == errno.EISDIR:
            raise PathResolutionError(
                f".gitignore is not a regular file: {gitignore_path}"
            ) from exc
        raise

    with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise PathResolutionError(
                f".gitignore is not a regular file: {gitignore_path}"
            )
        if info.st_nlink > 1:
            raise PathResolutionError(
                f"Refusing to update multiply-linked .gitignore: {gitignore_path}"
            )

        existing_lines = handle.read().splitlines()
        if any(
            line.strip()
            in {CODEX_DOBBY_GITIGNORE_ENTRY, CODEX_DOBBY_GITIGNORE_ENTRY.rstrip("/")}
            for line in existing_lines
        ):
            return False

        handle.seek(0, os.SEEK_END)
        if existing_lines and existing_lines[-1] != "":
            handle.write("\n")
        handle.write(f"{CODEX_DOBBY_GITIGNORE_ENTRY}\n")

    return True

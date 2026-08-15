from __future__ import annotations

import errno
import os
from pathlib import Path
import stat

from codex_dobby_mcp.models import CODEX_DOBBY_GITIGNORE_ENTRY
from codex_dobby_mcp.paths import PathResolutionError


# Absent on Windows, where os.open cannot refuse a final-component symlink.
# The pre-check below covers that platform instead.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def ensure_codex_dobby_ignored(repo_root: Path) -> bool:
    """Append Dobby's ignore entry, refusing symlinked or multiply-linked files.

    The refusals are enforced against an open descriptor rather than the path.
    Checking ``is_symlink``/``st_nlink`` on the pathname and then reopening that
    pathname to append leaves a window in which the file can be replaced, so the
    append could follow a symlink or write through a second hard link — exactly
    what these guards exist to prevent.
    """
    gitignore_path = repo_root / ".gitignore"

    if _O_NOFOLLOW == 0 and gitignore_path.is_symlink():
        # Best effort where the open flag does not exist. Racy by nature, which
        # is why it is only the fallback.
        raise PathResolutionError(
            f"Refusing to update symlinked .gitignore: {gitignore_path}"
        )

    try:
        # O_APPEND so every write lands atomically at end of file: a plain seek
        # to EOF can be overtaken by another process appending, and this file is
        # shared with the user's own tooling.
        descriptor = os.open(
            gitignore_path,
            os.O_RDWR | os.O_APPEND | os.O_CREAT | _O_NOFOLLOW,
            0o644,
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

        existing = handle.read()
        # rstrip, not strip: leading whitespace is significant in a gitignore
        # pattern, so " .codex-dobby/" does not ignore the directory and must
        # not be mistaken for an entry that does.
        if any(
            line.rstrip()
            in {CODEX_DOBBY_GITIGNORE_ENTRY, CODEX_DOBBY_GITIGNORE_ENTRY.rstrip("/")}
            for line in existing.split("\n")
        ):
            return False

        # Decided from the raw text, not splitlines(): a file ending in exactly
        # one newline already terminates its last entry, and treating it as
        # unterminated inserted a stray blank line before ours.
        if existing and not existing.endswith(("\n", "\r")):
            handle.write("\n")
        handle.write(f"{CODEX_DOBBY_GITIGNORE_ENTRY}\n")

    return True

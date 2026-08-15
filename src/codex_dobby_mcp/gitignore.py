from __future__ import annotations

import errno
import os
from pathlib import Path
import stat

from codex_dobby_mcp.models import CODEX_DOBBY_GITIGNORE_ENTRY
from codex_dobby_mcp.paths import PathResolutionError


# Present on macOS and Linux, the platforms Dobby supports. Kept as a lookup
# with a pre-check fallback so a platform without it degrades to a racy check
# rather than an AttributeError that would break every mutating run.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _entry_present(text: str) -> bool:
    # rstrip, not strip: leading whitespace is significant in a gitignore
    # pattern, so " .codex-dobby/" does not ignore the directory and must not
    # be mistaken for an entry that does.
    return any(
        line.rstrip()
        in {CODEX_DOBBY_GITIGNORE_ENTRY, CODEX_DOBBY_GITIGNORE_ENTRY.rstrip("/")}
        for line in text.split("\n")
    )


def _already_ignored(gitignore_path: Path) -> bool:
    """Whether the entry is present, reading only. Used when writing is denied."""
    try:
        return _entry_present(gitignore_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return False


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
        if exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
            # Opening for write before checking whether anything needs writing
            # would fail a run whose .gitignore is read-only but already
            # correct. Fall back to a read to answer that.
            if _already_ignored(gitignore_path):
                return False
            raise PathResolutionError(
                f"Cannot update .gitignore to ignore {CODEX_DOBBY_GITIGNORE_ENTRY}: "
                f"{gitignore_path} is not writable"
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

        try:
            existing = handle.read()
        except UnicodeDecodeError as exc:
            # A raw ValueError here escapes prepare() as an opaque failure with
            # no run directory; name the cause instead.
            raise PathResolutionError(
                f".gitignore is not valid UTF-8: {gitignore_path}"
            ) from exc
        if _entry_present(existing):
            return False

        # Decided from the raw text, not splitlines(): a file ending in exactly
        # one newline already terminates its last entry, and treating it as
        # unterminated inserted a stray blank line before ours.
        if existing and not existing.endswith(("\n", "\r")):
            handle.write("\n")
        handle.write(f"{CODEX_DOBBY_GITIGNORE_ENTRY}\n")

    return True

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


def test_gitignore_append_does_not_insert_a_blank_line(tmp_path: Path) -> None:
    """A file already ending in one newline is terminated; do not add another."""
    (tmp_path / ".gitignore").write_text("target/\n", encoding="utf-8")

    assert ensure_codex_dobby_ignored(tmp_path) is True

    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == (
        "target/\n.codex-dobby/\n"
    )


def test_gitignore_append_terminates_a_file_without_a_trailing_newline(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text("target/", encoding="utf-8")

    assert ensure_codex_dobby_ignored(tmp_path) is True

    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == (
        "target/\n.codex-dobby/\n"
    )


def test_gitignore_does_not_accept_an_indented_pattern_as_the_entry(
    tmp_path: Path,
) -> None:
    """Leading whitespace is significant, so ` .codex-dobby/` ignores nothing."""
    (tmp_path / ".gitignore").write_text(" .codex-dobby/\n", encoding="utf-8")

    assert ensure_codex_dobby_ignored(tmp_path) is True

    contents = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert contents == " .codex-dobby/\n.codex-dobby/\n"


def test_gitignore_append_survives_a_concurrent_append(tmp_path: Path) -> None:
    """Another process appending must not be overwritten.

    Positioning at end of file and then writing can be overtaken by a
    concurrent append; O_APPEND makes each write land atomically at the real
    end, and .gitignore is shared with the user's own tooling.
    """
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("target/\n", encoding="utf-8")

    real_open = os.open
    seen_flags: list[int] = []

    def record_flags(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    real_fstat = os.fstat
    raced = False

    def append_then_fstat(fileno):  # type: ignore[no-untyped-def]
        nonlocal raced
        if not raced:
            raced = True
            with open(gitignore, "a", encoding="utf-8") as other:
                other.write("from-another-process/\n")
        return real_fstat(fileno)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("codex_dobby_mcp.gitignore.os.open", record_flags)
    monkeypatch.setattr("codex_dobby_mcp.gitignore.os.fstat", append_then_fstat)
    try:
        assert ensure_codex_dobby_ignored(tmp_path) is True
    finally:
        monkeypatch.undo()

    # The guarantee is structural: O_APPEND makes every write land at the real
    # end of file, so no interleaving can be constructed that overwrites a
    # concurrent append. Asserting the flag is what actually discriminates —
    # a seek-to-EOF implementation passes the content check below by luck of
    # ordering.
    assert seen_flags, "os.open was not called"
    assert seen_flags[0] & os.O_APPEND, "gitignore is not opened for atomic append"

    contents = gitignore.read_text(encoding="utf-8")
    assert "from-another-process/" in contents, "concurrent append was overwritten"
    assert contents.rstrip("\n").endswith(".codex-dobby/")


def test_gitignore_refuses_symlink_without_o_nofollow(tmp_path: Path) -> None:
    """Platforms lacking O_NOFOLLOW (Windows) must still refuse a symlink.

    Referencing os.O_NOFOLLOW unconditionally also raised AttributeError there,
    which would have broken every mutating run on a platform the package
    advertises support for.
    """
    outside = tmp_path / "outside.txt"
    outside.write_text("", encoding="utf-8")
    (tmp_path / ".gitignore").symlink_to(outside)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("codex_dobby_mcp.gitignore._O_NOFOLLOW", 0)
    try:
        with pytest.raises(PathResolutionError, match="symlinked \\.gitignore"):
            ensure_codex_dobby_ignored(tmp_path)
    finally:
        monkeypatch.undo()


def test_gitignore_readonly_but_already_correct_is_not_an_error(tmp_path: Path) -> None:
    """Opening for write before checking need turned a no-op into a failed run.

    A read-only .gitignore that already lists the entry aborted build, validate
    and reverse_engineer with a raw PermissionError.
    """
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(".codex-dobby/\n", encoding="utf-8")
    gitignore.chmod(0o444)
    try:
        assert ensure_codex_dobby_ignored(tmp_path) is False
    finally:
        gitignore.chmod(0o644)


def test_gitignore_readonly_and_missing_entry_reports_a_clear_cause(
    tmp_path: Path,
) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("coverage-html/\n", encoding="utf-8")
    gitignore.chmod(0o444)
    try:
        with pytest.raises(PathResolutionError, match="not writable"):
            ensure_codex_dobby_ignored(tmp_path)
    finally:
        gitignore.chmod(0o644)


def test_gitignore_rejects_non_utf8_with_a_named_cause(tmp_path: Path) -> None:
    """A raw UnicodeDecodeError escapes prepare() with no run directory."""
    (tmp_path / ".gitignore").write_bytes(b"\xff\xfe binary junk\n")

    with pytest.raises(PathResolutionError, match="not valid UTF-8"):
        ensure_codex_dobby_ignored(tmp_path)

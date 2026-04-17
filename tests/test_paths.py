import json
import os
from pathlib import Path

import pytest

from codex_dobby_mcp.paths import (
    PathResolutionError,
    create_run_artifacts,
    mcp_server_is_enabled,
    private_runtime_root,
    prompt_git_worktrees,
    prompt_referenced_relative_paths,
    resolve_extra_roots,
    resolve_repo_root,
    run_artifacts_for_task,
    runs_root_for_repo,
    write_json,
)


def init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()


def test_resolve_repo_root_defaults_to_spawn_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    init_git_repo(repo_root)

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        path = Path(args[0][2]).resolve()

        class Result:
            returncode = 0 if path == repo_root.resolve() else 1
            stdout = "true\n" if path == repo_root.resolve() else ""
            stderr = "" if path == repo_root.resolve() else "fatal"

        return Result()

    monkeypatch.setattr("codex_dobby_mcp.paths.subprocess.run", fake_run)

    assert resolve_repo_root(repo_root) == repo_root.resolve()


def test_resolve_repo_root_rejects_non_git_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        class Result:
            returncode = 1
            stdout = ""
            stderr = "fatal"

        return Result()

    monkeypatch.setattr("codex_dobby_mcp.paths.subprocess.run", fake_run)

    with pytest.raises(PathResolutionError):
        resolve_repo_root(repo_root)


def test_resolve_extra_roots_uses_spawn_root_for_relative_paths(tmp_path: Path) -> None:
    spawn_root = tmp_path / "spawn"
    spawn_root.mkdir()
    extra = spawn_root / "artifacts"
    extra.mkdir()

    assert resolve_extra_roots(spawn_root, ["artifacts"]) == [extra.resolve()]


def test_resolve_extra_roots_can_be_based_on_repo_root(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    extra = repo_root / "external"
    extra.mkdir()

    assert resolve_extra_roots(repo_root, ["external"]) == [extra.resolve()]


def test_create_run_artifacts_rejects_symlinked_artifact_root(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo_root / ".codex-dobby").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathResolutionError, match="must not be a symlink"):
        create_run_artifacts(repo_root)


def test_run_artifacts_for_task_rejects_traversal_task_id(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with pytest.raises(PathResolutionError, match="single safe path segment"):
        run_artifacts_for_task(repo_root, "../../outside")


def test_runs_root_for_repo_rejects_symlinked_runs_root(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    artifacts_root = repo_root / ".codex-dobby"
    artifacts_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (artifacts_root / "runs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathResolutionError, match="must not be a symlink"):
        runs_root_for_repo(repo_root)


def test_private_runtime_root_uses_codex_dobby_namespace(tmp_path: Path) -> None:
    runtime_root = private_runtime_root("task-1", temp_root=tmp_path)

    assert runtime_root == tmp_path / "codex-dobby" / "task-1"
    assert runtime_root.is_dir()


def test_private_runtime_root_rejects_symlinked_namespace(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "codex-dobby").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathResolutionError, match="must not be a symlink"):
        private_runtime_root("task-1", temp_root=tmp_path)


def test_resolve_extra_roots_rejects_relative_symlink_escape(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo_root / "external").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathResolutionError, match="resolves outside the base directory"):
        resolve_extra_roots(repo_root, ["external"])


def test_prompt_git_worktrees_finds_repo_root_from_nested_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    init_git_repo(repo_root)
    nested = repo_root / "src" / "module"
    nested.mkdir(parents=True)
    target_file = nested / "main.ts"
    target_file.write_text("// test\n", encoding="utf-8")

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        path = Path(args[0][2]).resolve()

        class Result:
            returncode = 0 if path == repo_root.resolve() else 1
            stdout = "true\n" if path == repo_root.resolve() else ""
            stderr = "" if path == repo_root.resolve() else "fatal"

        return Result()

    monkeypatch.setattr("codex_dobby_mcp.paths.subprocess.run", fake_run)

    prompt = f"Investigate `{target_file}:42` in detail."

    assert prompt_git_worktrees(prompt) == [repo_root.resolve()]


def test_prompt_referenced_relative_paths_extracts_backtick_and_bare_tokens() -> None:
    prompt = (
        "Review `native/mic-capture/src/win_capture.cpp` and also "
        "src/main/mic-capture/service.ts — ignore README.md and /abs/path/foo.ts."
    )

    tokens = prompt_referenced_relative_paths(prompt)

    assert "native/mic-capture/src/win_capture.cpp" in tokens
    assert "src/main/mic-capture/service.ts" in tokens
    assert all(not token.startswith("/") for token in tokens)
    assert "README.md" not in tokens  # no slash
    assert "/abs/path/foo.ts" not in tokens  # absolute


def test_prompt_referenced_relative_paths_strips_line_suffix_and_punctuation() -> None:
    tokens = prompt_referenced_relative_paths(
        "See `src/foo/bar.ts:42`, also check src/foo/baz.ts."
    )

    assert "src/foo/bar.ts" in tokens
    assert "src/foo/baz.ts" in tokens


def test_prompt_referenced_relative_paths_ignores_prose_without_extensions() -> None:
    assert prompt_referenced_relative_paths("look at the foo/bar system") == []


def test_mcp_server_is_enabled_reads_home_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[mcp_servers.fetchaller]\ncommand = "python"\nargs = ["-m", "fetchaller.main"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert mcp_server_is_enabled("fetchaller") is True
    assert mcp_server_is_enabled("ghidra") is False


def test_mcp_server_is_enabled_honors_repo_override_disable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[mcp_servers.fetchaller]\ncommand = "python"\nargs = ["-m", "fetchaller.main"]\n',
        encoding="utf-8",
    )
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".codex").mkdir()
    ((repo_root / ".codex") / "config.toml").write_text(
        '[mcp_servers.fetchaller]\nenabled = false\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert mcp_server_is_enabled("fetchaller", repo_root=repo_root) is False


def test_write_json_replaces_existing_file_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "result.json"
    previous_payload = {"old": True}
    next_payload = {"new": True}
    expected_previous = json.dumps(previous_payload, indent=2, sort_keys=True) + "\n"
    expected_next = json.dumps(next_payload, indent=2, sort_keys=True) + "\n"
    path.write_text(expected_previous, encoding="utf-8")

    observed: dict[str, str] = {}
    real_replace = os.replace

    def fake_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        observed["target_before_replace"] = path.read_text(encoding="utf-8")
        observed["temp_payload"] = Path(src).read_text(encoding="utf-8")
        real_replace(src, dst)

    monkeypatch.setattr("codex_dobby_mcp.paths.os.replace", fake_replace)

    write_json(path, next_payload)

    assert observed["target_before_replace"] == expected_previous
    assert observed["temp_payload"] == expected_next
    assert path.read_text(encoding="utf-8") == expected_next

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import tomllib
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath

from codex_dobby_mcp.models import CODEX_DOBBY_DIRNAME, RunArtifacts, ToolName


class PathResolutionError(ValueError):
    pass


_ABSOLUTE_PATH_TOKEN_RE = re.compile(r"/[^\s'\"`()\[\]{}<>]+")
_TRAILING_PATH_PUNCTUATION = ",.;:)]}>"
_BACKTICK_TOKEN_RE = re.compile(r"`([^`\n]+)`")
_BARE_RELATIVE_PATH_TOKEN_RE = re.compile(r"(?<![/\w.-])([\w.-]+(?:/[\w.-]+)+)")
_FILENAME_EXT_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,6}$")
_MACOS_GHIDRA_SOCKET_SCAN_ROOTS = (Path("/var/folders"), Path("/private/var/folders"))
_MACOS_GHIDRA_SOCKET_SCAN_PATTERNS = ("*/*/T/ghidra-mcp-{user}", "*/*/*/T/ghidra-mcp-{user}")
_GHIDRA_BRIDGE_SCRIPT = "bridge_mcp_ghidra.py"


def resolve_path(value: str, base_dir: Path) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = (base_dir / candidate).resolve()
    return candidate.resolve()


def resolve_repo_root(spawn_root: Path, explicit_root: str | None = None) -> Path:
    repo_root = resolve_path(explicit_root, spawn_root) if explicit_root else spawn_root.resolve()
    if not repo_root.exists():
        raise PathResolutionError(f"Repo root does not exist: {repo_root}")
    if not repo_root.is_dir():
        raise PathResolutionError(f"Repo root is not a directory: {repo_root}")
    if not is_git_worktree(repo_root):
        raise PathResolutionError(f"Repo root is not inside a git worktree: {repo_root}")
    return repo_root


def prompt_git_worktrees(text: str) -> list[Path]:
    if not text.strip():
        return []

    discovered: list[Path] = []
    seen: set[Path] = set()

    for raw_token in _ABSOLUTE_PATH_TOKEN_RE.findall(text):
        normalized = _normalize_prompt_path_token(raw_token)
        if normalized is None:
            continue
        for candidate in _candidate_git_worktrees(Path(normalized).expanduser()):
            if not is_git_worktree(candidate):
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            discovered.append(candidate)
            break

    return discovered


def prompt_referenced_relative_paths(text: str) -> list[str]:
    """Extract relative file-path tokens referenced in prompt text.

    Returns tokens that look like relative file paths (contain `/`, end in a
    file-extension suffix, and do not start with `/` or `~`). Matches:
      - backtick-quoted tokens: `native/mic-capture/src/win_capture.cpp`
      - bare path tokens with at least one `/` and a file extension

    Used to detect when a prompt references files that should resolve inside a
    specific repo, so the runner can refuse to default to an unrelated cwd.
    """
    if not text.strip():
        return []

    tokens: list[str] = []
    seen: set[str] = set()

    def _accept(raw: str) -> None:
        token = raw.strip()
        if not token:
            return
        token = token.rstrip(_TRAILING_PATH_PUNCTUATION)
        line_match = re.match(r"^(?P<path>.+?):\d+$", token)
        if line_match:
            token = line_match.group("path")
        if not token or token.startswith("/") or token.startswith("~"):
            return
        if "/" not in token:
            return
        if any(ch.isspace() for ch in token):
            return
        if not _FILENAME_EXT_RE.search(token):
            return
        if token in seen:
            return
        seen.add(token)
        tokens.append(token)

    for match in _BACKTICK_TOKEN_RE.finditer(text):
        _accept(match.group(1))
    for match in _BARE_RELATIVE_PATH_TOKEN_RE.finditer(text):
        _accept(match.group(1))

    return tokens


def resolve_extra_roots(spawn_root: Path, extra_roots: list[str]) -> list[Path]:
    resolved: list[Path] = []
    for item in extra_roots:
        raw = Path(item).expanduser()
        if raw.is_absolute():
            path = raw.resolve()
        else:
            path = (spawn_root / raw).resolve()
            try:
                path.relative_to(spawn_root.resolve())
            except ValueError as exc:
                raise PathResolutionError(
                    f"Relative extra root resolves outside the base directory through symlinks: {raw}"
                ) from exc
        if not path.exists():
            raise PathResolutionError(f"Extra root does not exist: {path}")
        if not path.is_dir():
            raise PathResolutionError(f"Extra root is not a directory: {path}")
        if path not in resolved:
            resolved.append(path)
    return resolved


def _normalize_prompt_path_token(raw_token: str) -> str | None:
    token = raw_token.rstrip(_TRAILING_PATH_PUNCTUATION)
    line_match = re.match(r"^(?P<path>.+?):\d+$", token)
    if line_match:
        token = line_match.group("path")
    return token or None


def _candidate_git_worktrees(path: Path) -> list[Path]:
    candidates: list[Path] = []
    for candidate in (path, *path.parents):
        if not candidate.exists():
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_dir():
            continue
        candidates.append(resolved)
    return candidates


def reverse_engineer_default_writable_roots(repo_root: Path | None = None) -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    for ghidra_config in _mcp_server_configs("ghidra", repo_root=repo_root):
        for candidate in _mcp_server_path_candidates(ghidra_config):
            if candidate in seen:
                continue
            seen.add(candidate)
            roots.append(candidate)
    for candidate in _ghidra_socket_runtime_roots():
        if candidate in seen:
            continue
        seen.add(candidate)
        roots.append(candidate)
    return roots


def reverse_engineer_default_readonly_roots() -> list[Path]:
    return []


def _ghidra_socket_runtime_roots() -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()

    xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime_dir:
        candidate = Path(xdg_runtime_dir).expanduser() / "ghidra-mcp"
        if _looks_like_live_ghidra_socket_dir(candidate):
            resolved = candidate.resolve()
            roots.append(resolved)
            seen.add(resolved)

    user = os.environ.get("USER", "unknown")
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        candidate = Path(tmpdir).expanduser() / f"ghidra-mcp-{user}"
        if _looks_like_live_ghidra_socket_dir(candidate):
            resolved = candidate.resolve()
            if resolved not in seen:
                roots.append(resolved)
                seen.add(resolved)

    for scan_root in _MACOS_GHIDRA_SOCKET_SCAN_ROOTS:
        for pattern in _MACOS_GHIDRA_SOCKET_SCAN_PATTERNS:
            for candidate in sorted(scan_root.glob(pattern.format(user=user))):
                if not _looks_like_live_ghidra_socket_dir(candidate):
                    continue
                resolved = candidate.resolve()
                if resolved in seen:
                    continue
                roots.append(resolved)
                seen.add(resolved)

    fallback = Path(f"/tmp/ghidra-mcp-{user}")
    if _looks_like_live_ghidra_socket_dir(fallback):
        resolved = fallback.resolve()
        if resolved not in seen:
            roots.append(resolved)

    return roots


def _looks_like_live_ghidra_socket_dir(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    return any(path.glob("*.sock"))


def _mcp_server_configs(server_name: str, *, repo_root: Path | None = None) -> list[dict[object, object]]:
    configs: list[dict[object, object]] = []
    for config_path in _codex_config_paths(repo_root=repo_root):
        server_config = _load_mcp_server_config(config_path, server_name)
        if server_config is not None:
            configs.append(server_config)
    return configs


def _codex_config_paths(*, repo_root: Path | None = None) -> list[Path]:
    config_paths = [Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "config.toml"]
    if repo_root is not None:
        config_paths.append(repo_root / ".codex" / "config.toml")
    return config_paths


def _load_mcp_server_config(config_path: Path, server_name: str) -> dict[object, object] | None:
    if not config_path.exists():
        return None

    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None

    servers = payload.get("mcp_servers")
    if not isinstance(servers, dict):
        return None

    server_config = servers.get(server_name)
    return server_config if isinstance(server_config, dict) else None


def _mcp_server_path_candidates(server_config: dict[object, object]) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    helper_dir = server_config.get("helper_dir")
    if isinstance(helper_dir, str):
        candidate = _existing_helper_root_from_hint(helper_dir)
        if candidate is not None and candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)

    values: list[str] = []

    command = server_config.get("command")
    if isinstance(command, str):
        values.append(command)

    args = server_config.get("args")
    if isinstance(args, list):
        values.extend(item for item in args if isinstance(item, str))

    for value in values:
        candidate = _existing_bridge_root_from_config_value(value)
        if candidate is not None and candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)

    return candidates


def _existing_helper_root_from_hint(value: str) -> Path | None:
    if not value.startswith(("/", "~/")):
        return None

    path = Path(value).expanduser()
    if not path.exists():
        return None
    resolved = path.resolve()
    return resolved if resolved.is_dir() else resolved.parent


def _existing_bridge_root_from_config_value(value: str) -> Path | None:
    if not value.startswith(("/", "~/")):
        return None

    path = Path(value).expanduser()
    if not path.exists():
        return None

    resolved = path.resolve()
    if resolved.is_dir():
        return resolved if (resolved / _GHIDRA_BRIDGE_SCRIPT).exists() else None
    if resolved.name != _GHIDRA_BRIDGE_SCRIPT:
        return None
    return resolved.parent


def create_run_artifacts(repo_root: Path, task_id: str | None = None) -> RunArtifacts:
    run_id = _validate_task_id(task_id or uuid.uuid4().hex)
    artifacts_root = _ensure_safe_directory(
        repo_root / CODEX_DOBBY_DIRNAME,
        f"{CODEX_DOBBY_DIRNAME} artifact directory",
    )
    runs_root = _ensure_safe_directory(artifacts_root / "runs", f"{CODEX_DOBBY_DIRNAME}/runs directory")
    run_dir = _ensure_safe_directory(runs_root / run_id, f"{CODEX_DOBBY_DIRNAME} run directory")
    return run_artifacts_for_task(repo_root, run_id)


def run_artifacts_for_task(repo_root: Path, task_id: str) -> RunArtifacts:
    run_id = _validate_task_id(task_id)
    run_dir = _validate_optional_directory(
        runs_root_for_repo(repo_root) / run_id,
        f"{CODEX_DOBBY_DIRNAME} run directory",
    )
    return RunArtifacts(
        run_dir=run_dir,
        request_json=run_dir / "request.json",
        prompt_txt=run_dir / "prompt.txt",
        stdout_log=run_dir / "stdout.log",
        stderr_log=run_dir / "stderr.log",
        last_message_txt=run_dir / "last_message.txt",
        result_json=run_dir / "result.json",
        output_schema_json=run_dir / "output-schema.json",
    )


def runs_root_for_repo(repo_root: Path) -> Path:
    artifacts_root = _validate_optional_directory(
        repo_root / CODEX_DOBBY_DIRNAME,
        f"{CODEX_DOBBY_DIRNAME} artifact directory",
    )
    return _validate_optional_directory(artifacts_root / "runs", f"{CODEX_DOBBY_DIRNAME}/runs directory")


def private_runtime_root(task_id: str, temp_root: Path | None = None) -> Path:
    base_root = (temp_root or Path(tempfile.gettempdir())).resolve()
    if not base_root.exists():
        raise PathResolutionError(f"Private runtime base directory does not exist: {base_root}")
    if not base_root.is_dir():
        raise PathResolutionError(f"Private runtime base directory is not a directory: {base_root}")

    namespace_root = _ensure_safe_directory(base_root / "codex-dobby", "codex-dobby private runtime directory")
    return _ensure_safe_directory(namespace_root / task_id, "codex-dobby private task runtime directory")


def is_git_worktree(path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def write_json(path: Path, payload: object) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def public_file_label(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def _ensure_safe_directory(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise PathResolutionError(f"{label} must not be a symlink: {path}")
    if path.exists():
        if not path.is_dir():
            raise PathResolutionError(f"{label} is not a directory: {path}")
        return path
    path.mkdir()
    return path


def _validate_optional_directory(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise PathResolutionError(f"{label} must not be a symlink: {path}")
    if path.exists() and not path.is_dir():
        raise PathResolutionError(f"{label} is not a directory: {path}")
    return path


def _validate_task_id(task_id: str) -> str:
    posix = PurePosixPath(task_id)
    windows = PureWindowsPath(task_id)
    if (
        not task_id
        or posix.is_absolute()
        or windows.is_absolute()
        or len(posix.parts) != 1
        or len(windows.parts) != 1
        or posix.parts[0] in {".", ".."}
    ):
        raise PathResolutionError(f"Run task id must be a single safe path segment: {task_id!r}")
    return task_id

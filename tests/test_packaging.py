import os
from pathlib import Path
import subprocess
import tarfile
import tomllib
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
PROJECT_NAME = PYPROJECT["project"]["name"]
DIST_BASENAME = PROJECT_NAME.replace("-", "_")
VERSION = PYPROJECT["project"]["version"]


def _build_artifacts(tmp_path: Path) -> tuple[Path, Path]:
    out_dir = tmp_path / "dist"
    cache_dir = tmp_path / "uv-cache"
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = str(cache_dir)
    subprocess.run(
        ["uv", "build", "--offline", "--no-build-isolation", "--out-dir", str(out_dir)],
        cwd=REPO_ROOT,
        env=env,
        check=True,
    )
    wheel = out_dir / f"{DIST_BASENAME}-{VERSION}-py3-none-any.whl"
    sdist = out_dir / f"{DIST_BASENAME}-{VERSION}.tar.gz"
    return wheel, sdist


def test_package_tree_contains_no_ds_store_files() -> None:
    package_root = REPO_ROOT / "src" / "codex_dobby_mcp"
    assert list(package_root.rglob(".DS_Store")) == []


def test_built_distributions_include_runtime_assets_and_metadata(tmp_path: Path) -> None:
    wheel, sdist = _build_artifacts(tmp_path)

    wheel_expected_paths = {
        "codex_dobby_mcp/background_runs.py",
        "codex_dobby_mcp/snapshot_worker.py",
        "codex_dobby_mcp/assets/prompts/review-single.md",
        "codex_dobby_mcp/assets/prompts/validate.md",
        "codex_dobby_mcp/assets/codex_agents/review-security.toml",
        "codex_dobby_mcp/assets/schemas/worker-output.schema.json",
    }
    sdist_prefix = f"{DIST_BASENAME}-{VERSION}"
    sdist_expected_paths = {
        f"{sdist_prefix}/README.md",
        f"{sdist_prefix}/pyproject.toml",
        f"{sdist_prefix}/src/codex_dobby_mcp/background_runs.py",
        f"{sdist_prefix}/src/codex_dobby_mcp/snapshot_worker.py",
        f"{sdist_prefix}/src/codex_dobby_mcp/assets/prompts/review-single.md",
        f"{sdist_prefix}/src/codex_dobby_mcp/assets/prompts/validate.md",
        f"{sdist_prefix}/src/codex_dobby_mcp/assets/codex_agents/review-security.toml",
    }

    with zipfile.ZipFile(wheel) as archive:
        wheel_paths = set(archive.namelist())
        assert wheel_expected_paths <= wheel_paths

        metadata = archive.read(f"{DIST_BASENAME}-{VERSION}.dist-info/METADATA").decode("utf-8")
        entry_points = archive.read(f"{DIST_BASENAME}-{VERSION}.dist-info/entry_points.txt").decode("utf-8")

    assert "Project-URL: Homepage, https://github.com/Averyy/codex-dobby-mcp" in metadata
    assert "Project-URL: Repository, https://github.com/Averyy/codex-dobby-mcp" in metadata
    assert "Project-URL: Issues, https://github.com/Averyy/codex-dobby-mcp/issues" in metadata
    assert "Classifier: Development Status :: 3 - Alpha" in metadata
    assert "Classifier: Environment :: Console" in metadata
    assert "# Codex Dobby MCP" in metadata
    assert "codex-dobby-mcp = codex_dobby_mcp.server:main" in entry_points

    with tarfile.open(sdist, "r:gz") as archive:
        sdist_paths = set(archive.getnames())

    assert sdist_expected_paths <= sdist_paths

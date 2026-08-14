from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_release_tag.py"


def _write_pyproject(path: Path, version: str) -> None:
    path.write_text(
        f'[project]\nname = "example"\nversion = "{version}"\n',
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("tag", "expected_returncode"),
    [("v0.1.4", 0), ("0.1.4", 2), ("v0.1.3", 2), ("v0.1.4-rc1", 2)],
)
def test_release_tag_must_exactly_match_project_version(
    tmp_path: Path,
    tag: str,
    expected_returncode: int,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    _write_pyproject(pyproject, "0.1.4")

    completed = subprocess.run(
        [sys.executable, str(CHECK_SCRIPT), tag, "--pyproject", str(pyproject)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == expected_returncode
    if expected_returncode:
        assert "expected 'v0.1.4'" in completed.stderr

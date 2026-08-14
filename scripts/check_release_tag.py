from __future__ import annotations

import argparse
from pathlib import Path
import tomllib


DEFAULT_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def expected_release_tag(pyproject_path: Path = DEFAULT_PYPROJECT) -> str:
    with pyproject_path.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    return f"v{project['version']}"


def check_release_tag(tag: str, pyproject_path: Path = DEFAULT_PYPROJECT) -> None:
    expected = expected_release_tag(pyproject_path)
    if tag != expected:
        raise ValueError(
            f"Release tag {tag!r} does not match the package version; expected {expected!r}."
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Require the release tag to equal v<project.version>."
    )
    parser.add_argument("tag", help="Git tag being published, such as v0.1.4")
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=DEFAULT_PYPROJECT,
        help="pyproject.toml to inspect",
    )
    args = parser.parse_args()
    try:
        check_release_tag(args.tag, args.pyproject)
    except (KeyError, OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

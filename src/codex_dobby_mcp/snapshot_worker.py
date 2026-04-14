from __future__ import annotations

import argparse
import json
from pathlib import Path

from codex_dobby_mcp.runner import _build_repo_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture a repo snapshot for Codex Dobby.")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--include-head", action="store_true")
    parser.add_argument("--use-metadata-fingerprints", action="store_true")
    args = parser.parse_args()

    snapshot = _build_repo_snapshot(
        Path(args.repo_root),
        include_head=args.include_head,
        use_metadata_fingerprints=args.use_metadata_fingerprints,
    )
    print(json.dumps(snapshot.model_dump(mode="json")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

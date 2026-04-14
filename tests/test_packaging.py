from pathlib import Path


def test_package_tree_contains_no_ds_store_files() -> None:
    package_root = Path(__file__).resolve().parents[1] / "src" / "codex_dobby_mcp"
    assert list(package_root.rglob(".DS_Store")) == []

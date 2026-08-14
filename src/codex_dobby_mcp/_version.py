from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


DISTRIBUTION_NAME = "codex-dobby-mcp"


def installed_version() -> str:
    """Return Dobby's installed distribution version.

    Normal entry points always have distribution metadata. The explicit
    fallback keeps direct ``PYTHONPATH=src`` imports usable without falsely
    advertising the MCP SDK's unrelated version.
    """

    try:
        return version(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return "0.0.0+unknown"


__version__ = installed_version()


from __future__ import annotations

import logging
import sys


LOGGER_NAME = "codex_dobby_mcp"


def configure_logging(level: int | None = None) -> logging.Logger:
    """Attach Dobby's stderr handler, and set the level only when asked.

    ``level=None`` means "ensure logging is configured, leave the level alone".
    ``get_logger`` calls it that way on every lookup, so passing a default here
    would reset an explicitly configured level: a caller that asked for DEBUG
    lost it the next time any module fetched a logger, which is at runner
    construction, making debug output impossible to keep on.
    """
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        # Only supply the default when no level has been chosen at all.
        # Keying it on "no handler yet" clobbered a level set directly on the
        # logger before anything fetched one.
        if level is None and logger.level == logging.NOTSET:
            level = logging.INFO
    if level is not None:
        logger.setLevel(level)
    logger.propagate = False
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    configure_logging()
    if name:
        return logging.getLogger(f"{LOGGER_NAME}.{name}")
    return logging.getLogger(LOGGER_NAME)

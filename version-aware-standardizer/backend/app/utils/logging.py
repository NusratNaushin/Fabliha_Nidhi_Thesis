"""Structured-ish logging setup.

Hard Rule 15: never swallow an error silently.  Every service in this project
logs through :func:`get_logger` so that release imports, audit runs, resolution
decisions and approvals all end up in the same stream.

Hard Rule (section 50): never log patient data.  Only terminology codes,
release versions, counts and decisions are logged.
"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False
_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def configure_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT))
    root = logging.getLogger("vas")
    root.setLevel(level.upper())
    root.handlers = [handler]
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(f"vas.{name}")

"""JSON-structured logging setup."""

from __future__ import annotations

import logging
import sys

from pythonjsonlogger import jsonlogger

_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger with a JSON formatter (idempotent)."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(jsonlogger.JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level.upper())
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a named child logger (call :func:`configure_logging` first)."""
    return logging.getLogger(name)

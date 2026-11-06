"""Logging setup shared by the CLI and agents.

Every log record is stamped with the active OpenTelemetry trace and span IDs,
so a log line can be matched to its span in the JSON-lines trace output.
"""

from __future__ import annotations

import logging
import os

from opentelemetry import trace

LOG_LEVEL_ENV = "NORTHWIND_LOG_LEVEL"
DEFAULT_LEVEL = "WARNING"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [trace=%(trace_id)s span=%(span_id)s] %(message)s"


class TraceContextFilter(logging.Filter):
    """Attach ``trace_id`` and ``span_id`` from the current span to each record."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            record.trace_id = f"{context.trace_id:032x}"
            record.span_id = f"{context.span_id:016x}"
        else:
            record.trace_id = "-"
            record.span_id = "-"
        return True


class _NorthwindHandler(logging.StreamHandler):
    """Marker subclass so repeated configuration replaces only our own handler."""


_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def resolve_level(level: str | None = None) -> int:
    name = (level or os.environ.get(LOG_LEVEL_ENV) or DEFAULT_LEVEL).upper()
    try:
        return _LEVELS[name]
    except KeyError as exc:
        raise ValueError(f"unknown log level {name!r}") from exc


def configure_logging(level: str | None = None) -> logging.Handler:
    """Install the trace-aware stderr handler on the root logger (idempotent)."""
    root = logging.getLogger()
    for existing in [h for h in root.handlers if isinstance(h, _NorthwindHandler)]:
        root.removeHandler(existing)
    handler = _NorthwindHandler()
    handler.addFilter(TraceContextFilter())
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)
    root.setLevel(resolve_level(level))
    return handler

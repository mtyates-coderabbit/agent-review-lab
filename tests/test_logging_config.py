from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from northwind.logging_config import (
    TraceContextFilter,
    _NorthwindHandler,
    configure_logging,
    resolve_level,
)
from northwind.tracing import traced


@pytest.fixture
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def _record() -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)


def test_filter_adds_ids_inside_a_span():
    record = _record()
    with traced("test.logging") as span:
        TraceContextFilter().filter(record)
        context = span.get_span_context()
    assert record.trace_id == f"{context.trace_id:032x}"
    assert record.span_id == f"{context.span_id:016x}"


def test_filter_uses_placeholder_outside_a_span():
    record = _record()
    TraceContextFilter().filter(record)
    assert record.trace_id == "-"
    assert record.span_id == "-"


@pytest.mark.usefixtures("restore_root_logger")
def test_configure_logging_is_idempotent():
    configure_logging("INFO")
    configure_logging("DEBUG")
    root = logging.getLogger()
    ours = [h for h in root.handlers if isinstance(h, _NorthwindHandler)]
    assert len(ours) == 1
    assert root.level == logging.DEBUG


def test_resolve_level_reads_env(monkeypatch):
    monkeypatch.setenv("NORTHWIND_LOG_LEVEL", "error")
    assert resolve_level() == logging.ERROR
    assert resolve_level("info") == logging.INFO
    with pytest.raises(ValueError, match="unknown log level"):
        resolve_level("LOUD")

"""OpenTelemetry tracing with a local JSON-lines exporter.

Spans are written to ``$NORTHWIND_TRACE_DIR/traces-<date>.jsonl`` (default
``./traces``, which is gitignored). Strands Agents emits its own spans through
the global tracer provider, so they land in the same file as ours.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Span, Status, StatusCode

from northwind.config import load_runtime_settings

TRACER_NAME = "northwind.agents"
_setup_lock = threading.Lock()
_provider: TracerProvider | None = None


class JsonlFileSpanExporter(SpanExporter):
    """Append finished spans to a JSON-lines file, one span per line."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        return self._directory / f"traces-{day}.jsonl"

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            lines = [json.dumps(_span_to_dict(s), default=str) for s in spans]
            with self._lock, self.path.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


def _span_to_dict(span: ReadableSpan) -> dict[str, Any]:
    ctx = span.get_span_context()
    return {
        "name": span.name,
        "trace_id": f"{ctx.trace_id:032x}" if ctx else None,
        "span_id": f"{ctx.span_id:016x}" if ctx else None,
        "parent_id": f"{span.parent.span_id:016x}" if span.parent else None,
        "start": span.start_time,
        "end": span.end_time,
        "status": span.status.status_code.name,
        "attributes": dict(span.attributes or {}),
        "events": [{"name": e.name, "attributes": dict(e.attributes or {})} for e in span.events],
    }


def configure_tracing(trace_dir: Path | None = None) -> TracerProvider:
    """Install the global tracer provider once per process."""
    global _provider  # noqa: PLW0603 - process-wide singleton by design
    with _setup_lock:
        if _provider is not None:
            return _provider
        directory = trace_dir or load_runtime_settings().trace_dir
        provider = TracerProvider(resource=Resource.create({"service.name": "northwind-agents"}))
        provider.add_span_processor(SimpleSpanProcessor(JsonlFileSpanExporter(directory)))
        trace.set_tracer_provider(provider)
        _provider = provider
        return provider


def get_tracer() -> trace.Tracer:
    configure_tracing()
    return trace.get_tracer(TRACER_NAME)


@contextmanager
def traced(
    name: str,
    *,
    control_flow: tuple[type[BaseException], ...] = (),
    **attributes: Any,
) -> Iterator[Span]:
    """Start a span; mark it as an error if the block raises, then re-raise.

    Exceptions listed in ``control_flow`` (for example LangGraph's interrupt
    signal) are recorded as span events rather than errors.
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(
        name, record_exception=False, set_status_on_exception=False
    ) as span:
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, value)
        try:
            yield span
        except control_flow as exc:
            span.add_event("control_flow", {"type": type(exc).__name__})
            raise
        except BaseException as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise


def record_decision(agent: str, decision: str, **attributes: Any) -> None:
    """Record an agent decision as an event on the current span and as its own span."""
    attrs = {"agent.name": agent, "decision": decision}
    attrs.update({k: v for k, v in attributes.items() if v is not None})
    with traced(f"{agent}.decision", **attrs) as span:
        span.add_event("decision", attributes=attrs)

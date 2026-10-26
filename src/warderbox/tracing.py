"""Distributed tracing for AI agent actions.

OpenTelemetry-style span system that tracks every action through
the shield pipeline. Zero dependency — generates trace/span IDs and
exports a JSON span format (and an OTLP-shaped JSON view).

Usage:
    tracer = Tracer()
    shield.set_tracer(tracer)

    # After some actions...
    spans = tracer.spans()
    tracer.export_json("traces.json")

    # Nested spans for complex workflows
    with tracer.span("data_pipeline") as parent:
        with tracer.span("extract", parent=parent):
            ...
        with tracer.span("transform", parent=parent):
            ...
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field


def _new_id(length: int = 16) -> str:
    return uuid.uuid4().hex[:length]


@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    start_time: float = 0.0
    end_time: float = 0.0
    status: str = "unset"
    attributes: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        if self.end_time and self.start_time:
            return (self.end_time - self.start_time) * 1000
        return 0.0

    def set_attribute(self, key: str, value) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, attributes: dict | None = None) -> None:
        self.events.append({
            "name": name,
            "timestamp": time.time(),
            "attributes": attributes or {},
        })

    def to_dict(self) -> dict:
        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id,
            "name": self.name,
            "startTimeUnixNano": int(self.start_time * 1e9),
            "endTimeUnixNano": int(self.end_time * 1e9),
            "durationMs": round(self.duration_ms, 2),
            "status": self.status,
            "attributes": self.attributes,
            "events": self.events,
        }


class Tracer:
    """Lightweight tracing system for agent actions."""

    def __init__(
        self, service_name: str = "warderbox", max_spans: int = 100_000
    ) -> None:
        self._service_name = service_name
        self._spans: deque[Span] = deque(maxlen=max_spans)
        self._active_trace_id: str | None = None
        self._lock = threading.Lock()

    def start_span(
        self,
        name: str,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        attributes: dict | None = None,
    ) -> Span:
        tid = trace_id or self._active_trace_id or _new_id(32)
        s = Span(
            name=name,
            trace_id=tid,
            span_id=_new_id(16),
            parent_span_id=parent_span_id,
            start_time=time.time(),
            attributes=attributes or {},
        )
        s.set_attribute("service.name", self._service_name)
        with self._lock:
            self._spans.append(s)
        return s

    def end_span(self, span: Span, status: str = "ok") -> None:
        span.end_time = time.time()
        span.status = status

    @contextmanager
    def span(
        self,
        name: str,
        parent: Span | None = None,
        attributes: dict | None = None,
    ):
        s = self.start_span(
            name=name,
            trace_id=parent.trace_id if parent else None,
            parent_span_id=parent.span_id if parent else None,
            attributes=attributes,
        )
        try:
            yield s
            self.end_span(s, "ok")
        except Exception as e:
            s.add_event("exception", {"message": str(e), "type": type(e).__name__})
            self.end_span(s, "error")
            raise

    def start_trace(self) -> str:
        self._active_trace_id = _new_id(32)
        return self._active_trace_id

    def end_trace(self) -> None:
        self._active_trace_id = None

    def record_action(
        self,
        tool_name: str,
        args: dict,
        result_status: str,
        cost: float = 0.0,
        risk: str = "LOW",
        duration_ms: float = 0.0,
        error: str | None = None,
        blocked: bool = False,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
    ) -> Span:
        attrs = {
            "tool.name": tool_name,
            "tool.cost": cost,
            "tool.risk": risk,
            "tool.blocked": blocked,
        }
        if error:
            attrs["error.message"] = error

        s = self.start_span(
            name=f"tool.{tool_name}",
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            attributes=attrs,
        )

        if duration_ms > 0:
            s.end_time = s.start_time + (duration_ms / 1000)
        else:
            s.end_time = time.time()

        s.status = result_status
        return s

    def _snapshot(self) -> list[Span]:
        with self._lock:
            return list(self._spans)

    def spans(self, trace_id: str | None = None) -> list[Span]:
        snap = self._snapshot()
        if trace_id:
            return [s for s in snap if s.trace_id == trace_id]
        return snap

    def traces(self) -> dict[str, list[Span]]:
        grouped: dict[str, list[Span]] = {}
        for s in self._snapshot():
            grouped.setdefault(s.trace_id, []).append(s)
        return grouped

    def export_json(self, path: str) -> None:
        data = {
            "resourceSpans": [{
                "resource": {"attributes": {"service.name": self._service_name}},
                "scopeSpans": [{
                    "scope": {"name": "warderbox"},
                    "spans": [s.to_dict() for s in self._snapshot()],
                }],
            }]
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def export_otlp(self) -> dict:
        return {
            "resourceSpans": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": self._service_name}},
                    ]
                },
                "scopeSpans": [{
                    "scope": {"name": "warderbox"},
                    "spans": [self._span_to_otlp(s) for s in self._snapshot()],
                }],
            }]
        }

    def _span_to_otlp(self, s: Span) -> dict:
        attrs = [
            {"key": k, "value": self._otlp_value(v)}
            for k, v in s.attributes.items()
        ]
        return {
            "traceId": s.trace_id,
            "spanId": s.span_id,
            "parentSpanId": s.parent_span_id or "",
            "name": s.name,
            "startTimeUnixNano": str(int(s.start_time * 1e9)),
            "endTimeUnixNano": str(int(s.end_time * 1e9)),
            "status": {"code": 1 if s.status == "ok" else 2},
            "attributes": attrs,
        }

    @staticmethod
    def _otlp_value(v) -> dict:
        if isinstance(v, bool):
            return {"boolValue": v}
        if isinstance(v, int):
            return {"intValue": str(v)}
        if isinstance(v, float):
            return {"doubleValue": v}
        return {"stringValue": str(v)}

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()
        self._active_trace_id = None

    def stats(self) -> dict:
        snap = self._snapshot()
        total = len(snap)
        errors = sum(1 for s in snap if s.status == "error")
        avg_duration = 0.0
        if total:
            avg_duration = sum(s.duration_ms for s in snap) / total
        return {
            "total_spans": total,
            "traces": len(self.traces()),
            "errors": errors,
            "avg_duration_ms": round(avg_duration, 2),
        }

from __future__ import annotations

from contextlib import contextmanager
from time import monotonic_ns, time_ns
from threading import RLock
from functools import wraps
from contextvars import ContextVar
from collections import defaultdict

from .canonical import digest

STAGES = frozenset(
    {
        "queue",
        "controller",
        "search",
        "rank",
        "inference",
        "recheck",
        "proof",
        "storage",
        "attestation",
        "normalize",
        "admission",
        "publish",
        "freeze",
        "impact",
        "optimize",
        "cas",
        "policy",
        "narration",
    }
)


class Trace:
    def __init__(
        self, correlation_id, *, clock=monotonic_ns, host_class="warm", simulation=True
    ):
        if host_class not in {"cold", "warm"}:
            raise ValueError("invalid host class")
        self._lock = RLock()
        self.epoch_ns, self.monotonic_origin = time_ns(), clock()
        self.trace_id, self.clock = digest({"correlation_id": correlation_id}), clock
        self.host_class, self.simulation, self.spans = host_class, simulation, []

    @contextmanager
    def span(self, stage, *, parent=None, provider=None):
        if stage not in STAGES or provider not in {
            None,
            "qloo",
            "gemini",
            "groq",
            "operator",
            "offline",
        }:
            raise ValueError("unregistered trace dimension")
        if parent is not None and parent not in {s["span_id"] for s in self.spans}:
            raise ValueError("unknown trace parent")
        with self._lock:
            span_id = str(len(self.spans) + 1)
            self.spans.append({"span_id": span_id, "pending": True})
        item = {
            "span_id": span_id,
            "parent_id": parent,
            "stage": stage,
            "provider": provider,
            "start_ns": self.clock(),
            "duration_us": 0,
            "outcome": "RUNNING",
        }
        with self._lock:
            self.spans[int(span_id) - 1] = item
        try:
            yield item["span_id"]
            item["outcome"] = "COMPLETE"
        except BaseException:
            item["outcome"] = "FAILED"
            raise
        finally:
            item["duration_us"] = max(0, (self.clock() - item["start_ns"]) // 1000)

    def export(self):
        return {
            "schema": "ofc.trace/1",
            "trace_id": self.trace_id,
            "host_class": self.host_class,
            "simulation": self.simulation,
            "spend_minor": 0,
            "measurement_scope": "PROCESS_AND_TRANSPORT_OBSERVATION",
            "spans": [
                {k: v for k, v in s.items() if k != "start_ns"} for s in self.spans
            ],
        }

    def otlp(self):
        spans = []
        for item in self.spans:
            start = self.epoch_ns + item["start_ns"] - self.monotonic_origin
            span = {
                "traceId": self.trace_id[:32],
                "spanId": format(int(item["span_id"]), "016x"),
                "name": item["stage"],
                "kind": 3 if item["provider"] else 1,
                "startTimeUnixNano": str(start),
                "endTimeUnixNano": str(start + item["duration_us"] * 1000),
                "attributes": [
                    {"key": "ofc.simulation", "value": {"boolValue": self.simulation}},
                    {
                        "key": "ofc.host_class",
                        "value": {"stringValue": self.host_class},
                    },
                ],
                "status": {"code": 1 if item["outcome"] == "COMPLETE" else 2},
            }
            if item["parent_id"]:
                span["parentSpanId"] = format(int(item["parent_id"]), "016x")
            if item["provider"]:
                span["attributes"].append(
                    {"key": "ofc.provider", "value": {"stringValue": item["provider"]}}
                )
            spans.append(span)
        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "ofc-control"},
                            }
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "ofc-control", "version": "1"},
                            "spans": spans,
                        }
                    ],
                }
            ]
        }


def summarize(traces):
    groups = defaultdict(list)
    for trace in traces:
        for s in trace["spans"]:
            groups[
                (trace["host_class"], trace["simulation"], s["stage"], s["provider"])
            ].append(s["duration_us"])
    result = []
    for (host, simulation, stage, provider), values in sorted(groups.items(), key=str):
        values.sort()

        def percentile(p):
            return values[
                min(len(values) - 1, max(0, (len(values) * p + 99) // 100 - 1))
            ]

        result.append(
            {
                "host_class": host,
                "simulation": simulation,
                "stage": stage,
                "provider": provider,
                "n": len(values),
                "p50_us": percentile(50),
                "p95_us": percentile(95),
                "p99_us": percentile(99),
            }
        )
    return result


_active_trace = ContextVar("ofc_trace", default=None)


@contextmanager
def capture(trace, parent=None):
    token = _active_trace.set((trace, parent))
    try:
        yield
    finally:
        _active_trace.reset(token)


def instrument(stage):
    def decorate(method):
        @wraps(method)
        def wrapped(self, *args, **kwargs):
            selected = getattr(self, "trace", None) or _active_trace.get()
            if selected is None:
                return method(self, *args, **kwargs)
            trace, parent = (
                selected if isinstance(selected, tuple) else (selected, None)
            )
            selected_stage=stage(self,args,kwargs) if callable(stage) else stage
            with trace.span(
                selected_stage, parent=parent, provider=getattr(self, "provider", None)
            ):
                return method(self, *args, **kwargs)

        return wrapped

    return decorate

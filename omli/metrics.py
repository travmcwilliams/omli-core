"""Process-global, in-process metrics registry for OMLi's telemetry (the artifact chain,
gates, model calls).

Three instrument kinds, all thread-safe and dependency-free:

  * counters  — monotonically increasing tallies (``incr("gate.pre.pass")``)
  * timers    — bounded ring of recent durations, summarised as count/mean/p50/p95/max
                (``with timer("agent.run"): ...`` or ``observe("agent.run", ms)``)
  * gauges    — last-value point samples (``gauge("queue.depth", 3)``)

Design mirrors the rest of OMLi: no daemon, no orchestrator, no external process. The
registry is a plain module-level singleton that any module increments in-line; a caller
reads a ``snapshot()`` when it wants one. History is process-local, resets on restart, and is
never written to disk.

``record_event(name, payload)`` is the single tap the EventBus calls for every emitted
event, so instrumenting the whole architecture is one edit in ``events/bus.py`` rather
than a counter at every call site. It is intentionally total and best-effort: it maps a
known event name to its counter, counts any other name as ``event.<name>``, harvests token
counts from ``post_gate`` payloads, and swallows its own errors — telemetry must never break
a real run.
"""

from __future__ import annotations

import threading
from collections import deque
from contextlib import contextmanager
from time import perf_counter
from typing import Any, Deque, Iterator, Mapping

# How many recent samples each timer keeps for its percentile window. Bounded so a
# long-running process can never grow this without limit.
_TIMER_WINDOW = 512

_LOCK = threading.RLock()
_counters: dict[str, float] = {}
_gauges: dict[str, float] = {}
_timers: dict[str, Deque[float]] = {}


# --------------------------------------------------------------------------- #
# primitive instruments
# --------------------------------------------------------------------------- #
def incr(name: str, n: float = 1.0) -> None:
    """Add ``n`` (default 1) to counter ``name``."""
    with _LOCK:
        _counters[name] = _counters.get(name, 0.0) + n


def gauge(name: str, value: float) -> None:
    """Record the latest point value of gauge ``name`` (overwrites)."""
    with _LOCK:
        _gauges[name] = float(value)


def observe(name: str, ms: float) -> None:
    """Record one duration (milliseconds) into timer ``name``'s rolling window."""
    with _LOCK:
        buf = _timers.get(name)
        if buf is None:
            buf = _timers[name] = deque(maxlen=_TIMER_WINDOW)
        buf.append(float(ms))


@contextmanager
def timer(name: str) -> Iterator[None]:
    """Time the wrapped block and record its wall-clock duration into timer ``name``.

    Always records, even if the block raises — a failed run's latency is still signal.
    """
    start = perf_counter()
    try:
        yield
    finally:
        observe(name, (perf_counter() - start) * 1000.0)


# --------------------------------------------------------------------------- #
# EventBus tap — one entry point for the whole architecture's event stream
# --------------------------------------------------------------------------- #
# Map raw EventBus event names onto stable counter names, grouped by architectural
# subsystem. Unknown names fall through to a
# generic ``event.<name>`` counter so nothing is silently dropped.
_EVENT_COUNTER: dict[str, str] = {
    "pre_gate": "gate.pre.checked",
    "blocked_pre": "gate.pre.blocked",
    "post_gate": "gate.post.checked",
    "retrieved": "model.retrieved",
    "model_call": "model.call",
    "model_error": "model.error",
    "escalated": "model.escalated",
    "artifact_written": "artifact.written",
}


def record_event(name: str, payload: Mapping[str, Any] | None = None) -> None:
    """Single tap the EventBus calls for every emitted event. Total and best-effort.

    Increments the event's mapped counter (or ``event.<name>`` if unmapped), and harvests
    side-signals worth their own series: post-gate pass/fail and model token totals.
    """
    try:
        incr(_EVENT_COUNTER.get(name, f"event.{name}"))
        data = payload or {}
        if name == "post_gate":
            incr("gate.post.passed" if data.get("passed") else "gate.post.rejected")
            it = data.get("in_tok")
            ot = data.get("out_tok")
            if isinstance(it, (int, float)):
                incr("model.tokens.in", float(it))
            if isinstance(ot, (int, float)):
                incr("model.tokens.out", float(ot))
        elif name == "model_call":
            # Per-model load. The Agent emits only registry model ids here (the registry is
            # the only place a model is named), so the number of these counters stays bounded.
            model = data.get("model")
            if isinstance(model, str) and model:
                incr(f"model.call.{model}")
    except Exception:
        # Telemetry must never break a real run.
        pass


# --------------------------------------------------------------------------- #
# read side
# --------------------------------------------------------------------------- #
def _percentile(values: list[float], pct: float) -> float:
    """Percentile by rounding to the nearest index of the sorted values (pct in 0..100)."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    k = max(0, min(len(ordered) - 1, round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[k]


def _timer_stats(buf: Deque[float]) -> dict[str, float]:
    vals = list(buf)
    if not vals:
        return {"count": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    return {
        "count": len(vals),
        "mean": round(sum(vals) / len(vals), 2),
        "p50": round(_percentile(vals, 50), 2),
        "p95": round(_percentile(vals, 95), 2),
        "max": round(max(vals), 2),
    }


def snapshot() -> dict[str, Any]:
    """A consistent point-in-time copy of every instrument, for a caller to report or export."""
    with _LOCK:
        counters = dict(_counters)
        gauges = dict(_gauges)
        timers = {name: _timer_stats(buf) for name, buf in _timers.items()}
    return {"counters": counters, "gauges": gauges, "timers": timers}


def reset() -> None:
    """Drop every instrument (test hook / manual reset)."""
    with _LOCK:
        _counters.clear()
        _gauges.clear()
        _timers.clear()

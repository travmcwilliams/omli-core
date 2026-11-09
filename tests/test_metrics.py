"""The in-process metrics registry — counters, timers, gauges, and the EventBus tap."""

from __future__ import annotations

import pytest

from omli import metrics


@pytest.fixture(autouse=True)
def _clean():
    metrics.reset()
    yield
    metrics.reset()


def test_counters_accumulate():
    metrics.incr("gate.pre.checked")
    metrics.incr("gate.pre.checked")
    metrics.incr("gate.pre.blocked", 3)
    snap = metrics.snapshot()["counters"]
    assert snap["gate.pre.checked"] == 2
    assert snap["gate.pre.blocked"] == 3


def test_gauge_holds_last_value():
    metrics.gauge("queue.depth", 4)
    metrics.gauge("queue.depth", 7)
    assert metrics.snapshot()["gauges"]["queue.depth"] == 7


def test_timer_percentiles():
    for ms in [10, 20, 30, 40, 100]:
        metrics.observe("model.invoke_ms", ms)
    stats = metrics.snapshot()["timers"]["model.invoke_ms"]
    assert stats["count"] == 5
    assert stats["max"] == 100
    assert stats["p50"] == 30  # nearest-rank median of the five
    assert stats["p95"] >= stats["p50"]


def test_timer_context_manager_records():
    with metrics.timer("agent.roundtrip"):
        pass
    assert metrics.snapshot()["timers"]["agent.roundtrip"]["count"] == 1


def test_record_event_maps_known_names():
    metrics.record_event("pre_gate", {"artifact": "a1"})
    metrics.record_event("model_call", {"model": "example-model"})
    metrics.record_event("artifact_written", {"id": "a2"})
    c = metrics.snapshot()["counters"]
    assert c["gate.pre.checked"] == 1
    assert c["model.call"] == 1
    assert c["artifact.written"] == 1


def test_record_event_post_gate_harvests_pass_and_tokens():
    metrics.record_event("post_gate", {"passed": True, "in_tok": 120, "out_tok": 40})
    metrics.record_event("post_gate", {"passed": False})
    c = metrics.snapshot()["counters"]
    assert c["gate.post.checked"] == 2
    assert c["gate.post.passed"] == 1
    assert c["gate.post.rejected"] == 1
    assert c["model.tokens.in"] == 120
    assert c["model.tokens.out"] == 40


def test_record_event_unknown_name_falls_through():
    metrics.record_event("something_new", {})
    assert metrics.snapshot()["counters"]["event.something_new"] == 1


@pytest.mark.parametrize("payload", [None, ["not", "a", "mapping"], "text", 42,
                                     {"in_tok": object(), "out_tok": "x", "passed": object()}])
def test_record_event_never_raises_on_bad_payload(payload):
    # Telemetry must never break a run — a payload of the wrong shape is swallowed.
    metrics.record_event("post_gate", payload)
    assert "gate.post.checked" in metrics.snapshot()["counters"]


def test_eventbus_emit_feeds_the_registry():
    from omli.events.bus import EventBus

    bus = EventBus()
    bus.emit("model_call", {"model": "x"})
    bus.emit("post_gate", {"passed": True, "out_tok": 10})
    c = metrics.snapshot()["counters"]
    assert c["model.call"] == 1
    assert c["gate.post.passed"] == 1
    assert c["model.tokens.out"] == 10

"""Traffic guard: a deterministic pre-gate that admits, defers, or
downgrades a model call from measured local hardware/traffic state + declared priority.

Primitive 2: cheap rule-based logic BEFORE the model call. Primitive 1: every threshold,
priority class, and rule lives in a pack (packs/traffic_guard.yaml); the gate code holds
zero domain constants. Primitive 4: given a bus, the guard emits every decision as an appended,
structured event.

The one structural guarantee tested hardest here: priority inversion is impossible — a
call at or above the pack's protected priority is never deferred or downgraded, no matter
what the probe reads or what rules the pack authors.
"""

from pathlib import Path

import pytest

from omli.events.bus import EventBus
from omli.events import sink
from omli.gates.traffic import (
    HardwareState,
    TrafficGuard,
    TrafficPolicy,
    load_traffic_policy,
    parse_traffic_policy,
)

REPO = Path(__file__).resolve().parent.parent


def _policy_data(**overrides):
    data = {
        "policy": "traffic_guard",
        "version": "0.1.0",
        "priority_classes": [
            {"name": "critical", "rank": 30},
            {"name": "interactive", "rank": 20},
            {"name": "normal", "rank": 10},
            {"name": "bulk", "rank": 0},
        ],
        "default_priority": "normal",
        "protected_priority": "critical",
        "rules": [
            {
                "id": "defer-on-cpu-saturation",
                "action": "defer",
                "applies_to_max_priority": "normal",
                "when": {"cpu_percent_at_least": 90},
            },
            {
                "id": "defer-bulk-on-queue-flood",
                "action": "defer",
                "applies_to_max_priority": "bulk",
                "when": {"inflight_at_least": 4},
            },
            {
                "id": "downgrade-on-low-ram",
                "action": "downgrade",
                "applies_to_max_priority": "interactive",
                "when": {"ram_available_mb_below": 1024},
            },
        ],
    }
    data.update(overrides)
    return data


def _probe(state):
    return lambda: state


CALM = HardwareState(cpu_percent=10.0, ram_available_mb=16000.0,
                     vram_available_mb=8000.0, inflight=0)
SATURATED = HardwareState(cpu_percent=97.0, ram_available_mb=512.0,
                          vram_available_mb=256.0, inflight=9)


# --------------------------------------------------------------------------- #
# decisions from synthetic state
# --------------------------------------------------------------------------- #

def test_calm_state_admits_everything():
    guard = TrafficGuard(parse_traffic_policy(_policy_data()), probe=_probe(CALM))
    for prio in ("critical", "interactive", "normal", "bulk"):
        assert guard.check(prio).decision == "admit"


def test_cpu_saturation_defers_low_priority_work():
    guard = TrafficGuard(parse_traffic_policy(_policy_data()), probe=_probe(
        HardwareState(cpu_percent=95.0, ram_available_mb=16000.0)))
    d = guard.check("bulk")
    assert d.decision == "defer"
    assert d.rule_id == "defer-on-cpu-saturation"


def test_low_ram_downgrades_interactive_work():
    guard = TrafficGuard(parse_traffic_policy(_policy_data()), probe=_probe(
        HardwareState(cpu_percent=20.0, ram_available_mb=500.0)))
    d = guard.check("interactive")
    assert d.decision == "downgrade"
    assert d.rule_id == "downgrade-on-low-ram"


def test_queue_flood_defers_only_bulk():
    state = HardwareState(cpu_percent=30.0, ram_available_mb=8000.0, inflight=6)
    guard = TrafficGuard(parse_traffic_policy(_policy_data()), probe=_probe(state))
    assert guard.check("bulk").decision == "defer"
    assert guard.check("normal").decision == "admit"


def test_first_matching_rule_wins_in_pack_order():
    guard = TrafficGuard(parse_traffic_policy(_policy_data()), probe=_probe(SATURATED))
    d = guard.check("bulk")
    assert d.decision == "defer"
    assert d.rule_id == "defer-on-cpu-saturation"  # listed before the flood rule


def test_default_priority_comes_from_the_pack():
    guard = TrafficGuard(parse_traffic_policy(_policy_data()), probe=_probe(
        HardwareState(cpu_percent=95.0)))
    # No priority declared -> pack default ("normal"), which the cpu rule covers.
    d = guard.check()
    assert d.priority == "normal"
    assert d.decision == "defer"


def test_unknown_priority_fails_loud():
    guard = TrafficGuard(parse_traffic_policy(_policy_data()), probe=_probe(CALM))
    with pytest.raises(ValueError):
        guard.check("no-such-priority")


# --------------------------------------------------------------------------- #
# priority inversion is impossible
# --------------------------------------------------------------------------- #

def test_critical_is_never_deferred_or_downgraded_even_when_saturated():
    guard = TrafficGuard(parse_traffic_policy(_policy_data()), probe=_probe(SATURATED))
    d = guard.check("critical")
    assert d.decision == "admit"
    assert "protected" in d.tags


def test_pack_cannot_author_a_rule_that_covers_the_protected_class():
    data = _policy_data(rules=[{
        "id": "bad", "action": "defer",
        "applies_to_max_priority": "critical",
        "when": {"cpu_percent_at_least": 1},
    }])
    with pytest.raises(ValueError):
        parse_traffic_policy(data)


# --------------------------------------------------------------------------- #
# thresholds are pack data, not code
# --------------------------------------------------------------------------- #

def test_pack_thresholds_drive_the_decision():
    state = HardwareState(cpu_percent=80.0, ram_available_mb=8000.0)
    lenient = parse_traffic_policy(_policy_data())          # cpu floor 90 -> admit
    strict_rules = _policy_data()["rules"]
    strict_rules[0] = dict(strict_rules[0], when={"cpu_percent_at_least": 75})
    strict = parse_traffic_policy(_policy_data(rules=strict_rules))  # floor 75 -> defer
    assert TrafficGuard(lenient, probe=_probe(state)).check("bulk").decision == "admit"
    assert TrafficGuard(strict, probe=_probe(state)).check("bulk").decision == "defer"


def test_shipped_pack_loads_and_validates():
    policy = load_traffic_policy(REPO / "packs" / "traffic_guard.yaml")
    assert isinstance(policy, TrafficPolicy)
    assert policy.version
    assert policy.rules  # the shipped pack carries real rules
    # And it drives real decisions.
    guard = TrafficGuard(policy, probe=_probe(SATURATED))
    lowest = min(policy.priority_classes, key=lambda c: c.rank).name
    protected = policy.protected_priority
    assert guard.check(lowest).decision in ("defer", "downgrade")
    assert guard.check(protected).decision == "admit"


# --------------------------------------------------------------------------- #
# probe failure path — fail open, loudly tagged
# --------------------------------------------------------------------------- #

def test_probe_failure_fails_open_and_is_tagged():
    def broken_probe():
        raise OSError("sensor exploded")

    bus = EventBus()
    guard = TrafficGuard(parse_traffic_policy(_policy_data()), probe=broken_probe, bus=bus)
    d = guard.check("bulk")
    assert d.decision == "admit"
    assert "probe_failed" in d.tags
    ev = [e for e in bus.log() if e.name == "traffic_gate"]
    assert ev and ev[-1].payload["decision"] == "admit"
    assert "probe_failed" in ev[-1].payload["tags"]


def test_unknown_reading_cannot_trigger_a_rule():
    # cpu unknown (None) -> the cpu rule must NOT fire; measured, never guessed.
    guard = TrafficGuard(parse_traffic_policy(_policy_data()), probe=_probe(
        HardwareState(cpu_percent=None, ram_available_mb=8000.0)))
    assert guard.check("bulk").decision == "admit"


# --------------------------------------------------------------------------- #
# measurable + loggable: structured, appended decision records
# --------------------------------------------------------------------------- #

def test_every_decision_is_emitted_with_state_rule_and_decision():
    bus = EventBus()
    guard = TrafficGuard(parse_traffic_policy(_policy_data()),
                         probe=_probe(SATURATED), bus=bus)
    guard.check("bulk")
    guard.check("critical")
    events = [e for e in bus.log() if e.name == "traffic_gate"]
    assert len(events) == 2
    first = events[0].payload
    assert first["decision"] == "defer"
    assert first["rule"] == "defer-on-cpu-saturation"
    assert first["priority"] == "bulk"
    assert first["cpu_percent"] == 97.0
    assert first["policy_version"] == "0.1.0"
    assert events[1].payload["decision"] == "admit"


def test_traffic_gate_events_are_persisted_by_the_sink(tmp_path):
    assert "traffic_gate" in sink.PERSISTED_EVENTS
    bus = EventBus()
    guard = TrafficGuard(parse_traffic_policy(_policy_data()),
                         probe=_probe(SATURATED), bus=bus)
    sink.enable(tmp_path)
    try:
        guard.check("bulk")
    finally:
        sink.disable()
    files = list(tmp_path.glob("*.jsonl"))
    assert files, "decision should land as an appended fact"
    assert '"traffic_gate"' in files[0].read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# in-flight tracking seam
# --------------------------------------------------------------------------- #

def test_track_context_manager_supplies_inflight_when_probe_does_not():
    policy = parse_traffic_policy(_policy_data())
    guard = TrafficGuard(policy, probe=_probe(
        HardwareState(cpu_percent=10.0, ram_available_mb=8000.0, inflight=None)))
    with guard.track(), guard.track(), guard.track(), guard.track():
        assert guard.check("bulk").decision == "defer"   # 4 in flight >= 4
    assert guard.check("bulk").decision == "admit"        # back to 0


# --------------------------------------------------------------------------- #
# loud validation
# --------------------------------------------------------------------------- #

def test_validation_rejects_bad_action():
    bad = _policy_data(rules=[{"id": "x", "action": "explode",
                               "applies_to_max_priority": "bulk",
                               "when": {"cpu_percent_at_least": 1}}])
    with pytest.raises(ValueError):
        parse_traffic_policy(bad)


def test_validation_rejects_empty_conditions():
    bad = _policy_data(rules=[{"id": "x", "action": "defer",
                               "applies_to_max_priority": "bulk", "when": {}}])
    with pytest.raises(ValueError):
        parse_traffic_policy(bad)


def test_validation_rejects_unknown_condition_key():
    bad = _policy_data(rules=[{"id": "x", "action": "defer",
                               "applies_to_max_priority": "bulk",
                               "when": {"moon_phase_at_least": 3}}])
    with pytest.raises(ValueError):
        parse_traffic_policy(bad)


def test_validation_rejects_unknown_protected_class():
    with pytest.raises(ValueError):
        parse_traffic_policy(_policy_data(protected_priority="emperor"))


def test_validation_rejects_duplicate_rule_ids():
    r = {"id": "dup", "action": "defer", "applies_to_max_priority": "bulk",
         "when": {"cpu_percent_at_least": 1}}
    with pytest.raises(ValueError):
        parse_traffic_policy(_policy_data(rules=[r, dict(r)]))

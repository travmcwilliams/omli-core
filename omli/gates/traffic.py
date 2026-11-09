"""Traffic guard — a deterministic pre-gate over measured local hardware/traffic state.

Before a model call is dispatched, this gate ADMITS, DEFERS, or DOWNGRADES it from two
inputs only: a sampled :class:`HardwareState` (CPU load, available RAM, VRAM when
discoverable, in-flight request count) and the call's declared priority class. Cheap,
rule-based, pre-model — exactly Primitive 2's shape. There is no orchestrator here and
none is needed: a caller about to dispatch a call may ask the guard first, the same
choreographed pattern as `omli/connections/scope_gate.py`. `omli.agents.base.Agent` does not
call it, and nothing in this package acts on a downgrade: the decision is returned to the caller.

Primitive 1: this module holds ZERO domain constants. Priority classes, the protected
class, and every threshold/rule live in a pack (``packs/traffic_guard.yaml``), parsed
loudly by :func:`parse_traffic_policy` / :func:`load_traffic_policy` into a frozen
:class:`TrafficPolicy`. The condition VOCABULARY (``cpu_percent_at_least``, …) is generic
hardware language and belongs in code, like ``min_context_window`` in the pack schema;
the VALUES are always pack data.

Primitive 4: every decision is measurable and loggable — when given a bus, the guard emits a
structured ``traffic_gate`` event (state sampled + rule fired + decision), which the durable
sink (``omli/events/sink.py``) appends to its daily JSONL files when a caller has enabled it.
Nothing is mutated.

Fail-open vs fail-closed — decided deliberately:

* A **probe failure fails OPEN** (admit, tagged ``probe_failed`` and logged). Broken
  telemetry must degrade to "no throttling", never to refusing all work — refusing
  all work because a sensor died is strictly worse than proceeding unthrottled.
* Fail-open cannot cause priority inversion, because protection does not depend on the
  probe at all: a call at or above the pack's ``protected_priority`` rank is admitted
  BEFORE any rule is evaluated, and the policy parser rejects any pack whose rules even
  claim to cover the protected class. A critical call is structurally incapable of being
  deferred behind bulk work — under load, under probe failure, or under a hostile pack.
* An **unknown reading never triggers a rule**: a condition on a ``None`` field simply
  does not match. Decisions are made on measurements, never on guesses.
* A **reading that is present but unusable fails CLOSED**: NaN, an infinity, or a value that
  is not an ``int`` or ``float`` (a bool or a string, for example) in any field defers the
  call, tagged ``probe_invalid``, for every priority below the protected class. Comparisons
  against NaN are always false, so without this a NaN reading would silently match no rule.
  The protected class is still admitted first, as above.
"""

from __future__ import annotations

import math
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Tuple

import yaml

# Actions a rule may take. "admit" is never a rule action — it is the default outcome.
_ACTIONS = ("defer", "downgrade")

# The generic condition vocabulary. Each maps a state field to a comparison direction:
# True  -> fires when reading >= threshold (pressure counts up),
# False -> fires when reading <  threshold (headroom counts down).
_CONDITIONS: Mapping[str, Tuple[str, bool]] = {
    "cpu_percent_at_least": ("cpu_percent", True),
    "ram_available_mb_below": ("ram_available_mb", False),
    "vram_available_mb_below": ("vram_available_mb", False),
    "inflight_at_least": ("inflight", True),
}


@dataclass(frozen=True)
class HardwareState:
    """One sample of local machine/traffic state. ``None`` = not measurable here."""

    cpu_percent: Optional[float] = None
    ram_available_mb: Optional[float] = None
    vram_available_mb: Optional[float] = None
    inflight: Optional[int] = None


@dataclass(frozen=True)
class PriorityClass:
    name: str
    rank: int


@dataclass(frozen=True)
class TrafficRule:
    """One pack-authored rule: WHEN all conditions hold, ACT on work at/below a rank."""

    id: str
    action: str
    max_priority_rank: int
    when: Mapping[str, float]

    def matches(self, state: HardwareState, rank: int) -> bool:
        if rank > self.max_priority_rank:
            return False
        for key, threshold in self.when.items():
            field_name, fires_at_or_above = _CONDITIONS[key]
            reading = getattr(state, field_name)
            if reading is None:
                return False  # unknown reading never triggers a rule
            if fires_at_or_above:
                if not reading >= threshold:
                    return False
            else:
                if not reading < threshold:
                    return False
        return True


@dataclass(frozen=True)
class TrafficPolicy:
    """Frozen, pack-loaded policy: classes, protection floor, and ordered rules."""

    version: str
    priority_classes: Tuple[PriorityClass, ...]
    default_priority: str
    protected_priority: str
    rules: Tuple[TrafficRule, ...]

    def rank_of(self, priority: str) -> int:
        for cls in self.priority_classes:
            if cls.name == priority:
                return cls.rank
        raise ValueError(
            f"unknown priority {priority!r}; policy defines "
            f"{tuple(c.name for c in self.priority_classes)}"
        )

    @property
    def protected_rank(self) -> int:
        return self.rank_of(self.protected_priority)


@dataclass(frozen=True)
class TrafficDecision:
    """The structured, loggable outcome of one guard check."""

    decision: str  # "admit" | "defer" | "downgrade"
    priority: str
    rank: int
    state: HardwareState
    rule_id: Optional[str] = None
    reason: str = ""
    tags: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def admitted(self) -> bool:
        return self.decision in ("admit", "downgrade")


# --------------------------------------------------------------------------- #
# policy parsing — loud validation, values are pack data
# --------------------------------------------------------------------------- #

def _finite_threshold(value: Any, rule_id: str, key: str) -> float:
    """A rule threshold as a float. Only an int or a float (never a bool or a string) that is
    finite as a float is accepted: NaN would make its rule never fire, and an infinity would
    make it always or never fire."""
    problem = None
    if type(value) not in (int, float):
        problem = "must be a number"
    else:
        try:
            number = float(value)
        except OverflowError:
            number = math.inf
        if not math.isfinite(number):
            problem = "must be a finite number"
    if problem is not None:
        raise ValueError(f"rule {rule_id!r}: threshold for {key!r} {problem}")
    return number


def parse_traffic_policy(data: Mapping[str, Any]) -> TrafficPolicy:
    """Validate a mapping (from YAML) into a frozen TrafficPolicy. Fails loud."""
    if not isinstance(data, Mapping):
        raise ValueError("traffic policy must be a mapping")
    version = str(data.get("version") or "")
    if not version:
        raise ValueError("traffic policy requires a 'version'")

    raw_classes = data.get("priority_classes") or []
    if not isinstance(raw_classes, list) or not raw_classes:
        raise ValueError("traffic policy requires a non-empty list of 'priority_classes'")
    classes: list[PriorityClass] = []
    for row in raw_classes:
        if not isinstance(row, Mapping):
            raise ValueError("every priority class must be a mapping")
        name = str(row.get("name") or "")
        if not name:
            raise ValueError("every priority class requires a 'name'")
        rank = row.get("rank")
        if type(rank) is not int:  # not a bool, a float or a string
            raise ValueError(f"priority class {name!r}: 'rank' must be an integer")
        classes.append(PriorityClass(name=name, rank=rank))
    names = [c.name for c in classes]
    if len(set(names)) != len(names):
        raise ValueError("priority class names must be unique")
    if len({c.rank for c in classes}) != len(classes):
        raise ValueError("priority class ranks must be unique")

    def _rank(name: str, what: str) -> int:
        for c in classes:
            if c.name == name:
                return c.rank
        raise ValueError(f"{what} {name!r} is not a defined priority class")

    default_priority = str(data.get("default_priority") or "")
    protected_priority = str(data.get("protected_priority") or "")
    _rank(default_priority, "default_priority")
    protected_rank = _rank(protected_priority, "protected_priority")

    rules: list[TrafficRule] = []
    seen_ids: set[str] = set()
    raw_rules = data.get("rules") or []
    if not isinstance(raw_rules, list):
        raise ValueError("traffic policy 'rules' must be a list")
    for row in raw_rules:
        if not isinstance(row, Mapping):
            raise ValueError("every traffic rule must be a mapping")
        rule_id = str(row.get("id") or "")
        if not rule_id:
            raise ValueError("every traffic rule requires an 'id'")
        if rule_id in seen_ids:
            raise ValueError(f"duplicate traffic rule id {rule_id!r}")
        seen_ids.add(rule_id)
        action = str(row.get("action") or "")
        if action not in _ACTIONS:
            raise ValueError(f"rule {rule_id!r}: action must be one of {_ACTIONS}")
        max_rank = _rank(str(row.get("applies_to_max_priority") or ""),
                         f"rule {rule_id!r} applies_to_max_priority")
        # Structural anti-inversion, enforced at load time: no rule may even claim to
        # cover the protected class. (The guard re-checks at runtime, belt and braces.)
        if max_rank >= protected_rank:
            raise ValueError(
                f"rule {rule_id!r} covers the protected priority "
                f"{protected_priority!r}; protected work is never deferred/downgraded"
            )
        when_raw = row.get("when") or {}
        if not isinstance(when_raw, Mapping):
            raise ValueError(f"rule {rule_id!r}: 'when' must be a mapping")
        if not when_raw:
            raise ValueError(f"rule {rule_id!r}: 'when' must name at least one condition")
        when: dict[str, float] = {}
        for key, value in when_raw.items():
            if key not in _CONDITIONS:
                raise ValueError(
                    f"rule {rule_id!r}: unknown condition {key!r}; "
                    f"known: {tuple(_CONDITIONS)}"
                )
            when[str(key)] = _finite_threshold(value, rule_id, key)
        rules.append(TrafficRule(id=rule_id, action=action,
                                 max_priority_rank=max_rank, when=when))

    return TrafficPolicy(
        version=version,
        priority_classes=tuple(classes),
        default_priority=default_priority,
        protected_priority=protected_priority,
        rules=tuple(rules),
    )


def load_traffic_policy(path: str | Path) -> TrafficPolicy:
    """Load + validate a traffic policy pack from YAML."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"traffic policy {path} must contain a mapping at top level")
    return parse_traffic_policy(data)


# --------------------------------------------------------------------------- #
# probes — small, injectable, never network
# --------------------------------------------------------------------------- #

def sample_local_state() -> HardwareState:
    """Best-effort local sample: psutil for CPU/RAM, nvidia-smi for VRAM.

    Purely local (no network). Any probe half that is unavailable reports ``None`` for
    its fields rather than raising — an unknown reading never triggers a rule.
    """
    cpu = ram = None
    try:
        import psutil  # optional; absent -> fields stay None

        cpu = float(psutil.cpu_percent(interval=None))
        ram = float(psutil.virtual_memory().available) / (1024.0 * 1024.0)
    except Exception:
        pass
    return HardwareState(cpu_percent=cpu, ram_available_mb=ram,
                         vram_available_mb=_nvidia_free_vram_mb(), inflight=None)


def _nvidia_free_vram_mb() -> Optional[float]:
    import shutil
    import subprocess

    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        out = subprocess.run(
            [smi, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode != 0:
            return None
        first = (out.stdout or "").strip().splitlines()
        return float(first[0].strip()) if first else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# the guard
# --------------------------------------------------------------------------- #

def _unusable_fields(state: HardwareState) -> list[str]:
    """Names of the state fields holding a reading that is present but not a finite number."""
    bad = []
    for name in ("cpu_percent", "ram_available_mb", "vram_available_mb", "inflight"):
        value = getattr(state, name)
        if value is None:
            continue
        if type(value) not in (int, float):
            bad.append(name)
            continue
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            bad.append(name)
    return bad


class TrafficGuard:
    """Deterministic admit/defer/downgrade decision for one prospective model call.

    ``probe`` is the injectable measurement seam (tests feed synthetic state); it
    defaults to :func:`sample_local_state`. ``bus`` receives one appended
    ``traffic_gate`` event per check. The guard also keeps its own thread-safe
    in-flight counter (see :meth:`track`), used when the probe does not measure
    ``inflight`` itself.
    """

    def __init__(
        self,
        policy: TrafficPolicy,
        *,
        probe: Optional[Callable[[], HardwareState]] = None,
        bus=None,
    ) -> None:
        self._policy = policy
        self._probe = probe or sample_local_state
        self._bus = bus
        self._inflight = 0
        self._lock = threading.Lock()

    @property
    def policy(self) -> TrafficPolicy:
        return self._policy

    @contextmanager
    def track(self):
        """Count a request as in flight for the duration of the block."""
        with self._lock:
            self._inflight += 1
        try:
            yield
        finally:
            with self._lock:
                self._inflight -= 1

    def check(self, priority: Optional[str] = None) -> TrafficDecision:
        """Decide whether a call of ``priority`` may run right now."""
        prio = priority if priority is not None else self._policy.default_priority
        rank = self._policy.rank_of(prio)  # unknown priority = config error, fail loud

        tags: list[str] = []
        try:
            state = self._probe()
        except Exception as exc:
            # Fail OPEN (see module docstring): telemetry loss must not stop all work.
            state = HardwareState()
            tags.append("probe_failed")
            decision = TrafficDecision(
                decision="admit", priority=prio, rank=rank, state=state,
                reason=f"probe failed ({type(exc).__name__}); failing open",
                tags=tuple(tags),
            )
            self._log(decision)
            return decision

        if state.inflight is None:
            with self._lock:
                state = HardwareState(
                    cpu_percent=state.cpu_percent,
                    ram_available_mb=state.ram_available_mb,
                    vram_available_mb=state.vram_available_mb,
                    inflight=self._inflight,
                )

        # Protection first, independent of the probe: at/above the protected rank the
        # rules are never consulted, so priority inversion is structurally impossible.
        if rank >= self._policy.protected_rank:
            tags.append("protected")
            decision = TrafficDecision(
                decision="admit", priority=prio, rank=rank, state=state,
                reason=f"priority {prio!r} is protected", tags=tuple(tags),
            )
            self._log(decision)
            return decision

        invalid = _unusable_fields(state)
        if invalid:
            tags.append("probe_invalid")
            decision = TrafficDecision(
                decision="defer", priority=prio, rank=rank, state=state,
                reason=f"probe returned an unusable reading ({', '.join(invalid)}); failing closed",
                tags=tuple(tags),
            )
            self._log(decision)
            return decision

        for rule in self._policy.rules:
            if rule.matches(state, rank):
                decision = TrafficDecision(
                    decision=rule.action, priority=prio, rank=rank, state=state,
                    rule_id=rule.id,
                    reason=f"rule {rule.id!r} matched", tags=tuple(tags),
                )
                self._log(decision)
                return decision

        decision = TrafficDecision(
            decision="admit", priority=prio, rank=rank, state=state,
            reason="no rule matched", tags=tuple(tags),
        )
        self._log(decision)
        return decision

    def _log(self, d: TrafficDecision) -> None:
        """Append the structured decision record (state + rule + decision)."""
        if self._bus is None:
            return
        self._bus.emit(
            "traffic_gate",
            {
                "decision": d.decision,
                "priority": d.priority,
                "rank": d.rank,
                "rule": d.rule_id,
                "reason": d.reason,
                "tags": list(d.tags),
                "cpu_percent": d.state.cpu_percent,
                "ram_available_mb": d.state.ram_available_mb,
                "vram_available_mb": d.state.vram_available_mb,
                "inflight": d.state.inflight,
                "policy_version": self._policy.version,
            },
        )

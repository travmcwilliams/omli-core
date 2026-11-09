"""Agent base — the pack-driven model path, always sandwiched by gates. (`omli.models` also has
direct helpers for a caller that wants a model without an agent, and those have no gates.)

Pipeline (Primitive 2): pre-gate -> model -> post-gate -> write successor (Primitive 4).
The model is structurally prevented from being the first or last decision-maker: the
pre-gate gates entry, the post-gate gates the write. When the pack configures escalation,
a rejected attempt may retry on a bigger model — bounded to `1 + max_escalations` total
attempts. Every step is announced on the event bus (choreography); there is
no orchestrator.

The agent applies its pack (Primitive 1) and never mutates it (Primitive 5). It names no
model: it resolves the pack's `model_selection` against the registry via the Router (at
construction, fail loud) and calls the resolved provider — so the only place a model is
named is the registry. Model identity is stamped into the successor's
provenance.
"""

from __future__ import annotations

from time import perf_counter
from typing import Iterable, Optional, Protocol, Tuple

from omli import hardware, metrics
from omli.artifacts.provenance import ModelRef
from omli.artifacts.schema import Artifact, succeed, thaw
from omli.core.router import Router
from omli.events.bus import EventBus
from omli.gates.escalation import parse_signals, should_escalate
from omli.gates.post import PostGate
from omli.gates.pre import PreGate
from omli.gates.result import GateResult
from omli.models.errors import ModelError
from omli.models.provider import Message, ModelRequest, ProviderTable, SamplingParams
from omli.models.registry import ModelEntry, ModelRegistry
from omli.packs.schema import Pack
from omli.models.registry import is_loopback_endpoint
from omli.sensitivity import blocked_nonlocal_model_tags, is_local_provider_kind


class _RetrievedContext(Protocol):
    """What a retriever hands back: render() for the prompt, refs for provenance."""

    @property
    def refs(self) -> Tuple[str, ...]: ...
    def render(self) -> str: ...


class Retriever(Protocol):
    """Structural contract for pre-context retrieval. No retriever ships in this package.

    Kept as a Protocol so `base.py` depends on no concrete retriever.
    """

    def retrieve(self, query_text: str, *,
                 exclude_artifact_id: Optional[str] = None) -> _RetrievedContext: ...


class Agent:
    """Applies a pack to advance an artifact one step along the chain."""

    def __init__(
        self,
        pack: Pack,
        registry: ModelRegistry,
        providers: ProviderTable,
        store,
        *,
        pre_gate: Optional[PreGate] = None,
        post_gate: Optional[PostGate] = None,
        bus: Optional[EventBus] = None,
        retriever: Optional[Retriever] = None,
    ) -> None:
        # The pack is held read-only; the agent applies it but never mutates it.
        self._pack = pack
        self._registry = registry
        self._providers = providers
        self._store = store
        self._pre = pre_gate or PreGate()
        self._post = post_gate or PostGate()
        self._bus = bus
        # Optional, default off: when present, relevant prior artifacts are pulled into the
        # model's pre-context. None -> no retrieval runs and the request carries no extra context.
        self._retriever = retriever

        # Resolve + validate the pack's model(s) against the registry at construction (fail loud).
        resolved = Router(registry).resolve(pack)
        self._entry = resolved.entry
        self._escalation_entry = resolved.escalation_entry
        for adapter in self._required_adapters():
            if adapter not in providers:
                raise ValueError(f"pack {pack.name!r}: no provider for adapter {adapter!r}")

        escalation = pack.model_selection.escalation if pack.model_selection else None
        self._trigger = escalation.trigger if escalation else None
        self._max_escalations = escalation.max_escalations if escalation else 0

    def _required_adapters(self) -> tuple[str, ...]:
        adapters = [self._entry.adapter]
        if self._escalation_entry is not None:
            adapters.append(self._escalation_entry.adapter)
        return tuple(adapters)

    @property
    def pack(self) -> Pack:
        return self._pack

    def _emit(self, name: str, **payload) -> None:
        if self._bus is not None:
            self._bus.emit(name, payload)

    def run(
        self,
        input_artifact: Artifact,
        output_kind: str,
        *,
        seen_source_refs: Optional[Iterable[str]] = None,
    ) -> Optional[Artifact]:
        """Advance `input_artifact` to a successor of `output_kind`, or None if gated out.

        Returns None (writing nothing) when the pre-gate blocks, the sensitivity route check
        blocks, the model is unreachable, or the post-gate rejects every (bounded) attempt.
        Model output that is not a usable number (NaN, an infinity, an integer too large for a
        float) is a post-gate rejection, not an exception.
        """
        # 1) PRE-GATE — decide whether the model may run at all.
        self._emit("pre_gate", artifact=input_artifact.id)
        pre = self._pre.check(input_artifact, seen_source_refs=seen_source_refs)
        if not pre.passed:
            self._emit("blocked_pre", artifact=input_artifact.id, reason=pre.reason)
            return None

        # 1b) RETRIEVE — pull relevant prior artifacts into pre-context.
        # Deterministic and pre-model, so the model is not the first decision-maker.
        # The emitted event is the memory-recall record: what was queried, what came back,
        # with what score, for which task. It is an appended fact; the durable sink
        # (omli/events/sink.py) persists it when a caller has enabled the sink.
        retrieved_context, retrieved_refs, retrieved_meta, query = \
            self._retrieve(input_artifact)
        if retrieved_refs:
            self._emit("retrieved", artifact=input_artifact.id,
                       k=len(retrieved_refs), refs=list(retrieved_refs),
                       scores=[m["score"] for m in retrieved_meta],
                       kinds=[m["kind"] for m in retrieved_meta],
                       query=query[:200])

        # 2) MODEL with bounded escalation. Total attempts = 1 + max_escalations.
        # Every event below carries the pack + attempt so a persisted event row is a
        # self-contained (configuration, decision, cost) fact.
        pack_ref = f"{self._pack.name}@{self._pack.version}"
        run_started = perf_counter()
        entry = self._entry
        escalations_used = 0
        while True:
            # Local-only sensitivity: fail closed BEFORE any non-local provider call
            # (structural route control — omli/sensitivity.py). Checked on every attempt so
            # a hosted escalation cannot slip past a local primary.
            # The endpoint is checked here as well as at registry parse, so an entry that did
            # not come through the parser cannot claim to be local and send elsewhere.
            reason = _route_block_reason(input_artifact.provenance.sensitivity, entry)
            if reason:
                self._emit(
                    "blocked_pre",
                    artifact=input_artifact.id,
                    reason=reason,
                    model=entry.model_id,
                    provider_kind=entry.provider_kind,
                    pack=pack_ref,
                )
                return None

            self._emit("model_call", artifact=input_artifact.id, model=entry.model_id,
                       pack=pack_ref, attempt=escalations_used + 1,
                       output_kind=output_kind)
            # Sample the machine BEFORE starting the clock. An uncached sample spawns a
            # subprocess, so sampling inside the timed region would corrupt the very
            # latency number this telemetry exists to explain. None when unavailable —
            # never a zeroed reading (see omli/hardware.py).
            gpu_at_call = hardware.gpu_state()
            invoke_started = perf_counter()
            try:
                # Time the inference call itself — the single most useful latency series,
                # and what correlates GPU/CPU load to real model work.
                with metrics.timer("model.invoke_ms"):
                    response = self._providers[entry.adapter].invoke(
                        entry,
                        build_model_request(self._pack, input_artifact, entry,
                                            retrieved_context=retrieved_context),
                    )
            except ModelError as exc:
                # Availability is not an escalation trigger — it is the escape hatch (P2).
                self._emit(
                    "model_error",
                    artifact=input_artifact.id,
                    error=type(exc).__name__,
                    reason=str(exc),
                    model=entry.model_id,
                    pack=pack_ref,
                    ms=round((perf_counter() - invoke_started) * 1000.0, 1),
                )
                return None
            invoke_ms = round((perf_counter() - invoke_started) * 1000.0, 1)

            signals = parse_signals(response.text)
            candidate = self._build_candidate(response.text, signals, retrieved_refs)

            # 3) POST-GATE — schema + (when a confidence was parsed) threshold.
            post = self._check_post(candidate, signals)
            threshold = self._pack.thresholds.get("min_confidence")
            self._emit(
                "post_gate",
                artifact=input_artifact.id,
                confidence=signals.confidence,
                threshold=threshold,
                passed=post.passed,
                model=entry.model_id,
                in_tok=response.input_tokens,
                out_tok=response.output_tokens,
                ms=invoke_ms,
                pack=pack_ref,
                attempt=escalations_used + 1,
                gpu=gpu_at_call,
            )

            if (
                self._escalation_entry is not None
                and escalations_used < self._max_escalations
                and should_escalate(self._trigger or "", post, signals)
            ):
                # Fail closed BEFORE announcing escalation: a hosted rescue must not
                # appear as a successful route switch when sensitivity forbids it.
                reason = _route_block_reason(
                    input_artifact.provenance.sensitivity, self._escalation_entry
                )
                if reason:
                    self._emit(
                        "blocked_pre",
                        artifact=input_artifact.id,
                        reason=reason,
                        model=self._escalation_entry.model_id,
                        provider_kind=self._escalation_entry.provider_kind,
                        pack=pack_ref,
                    )
                    return None
                self._emit(
                    "escalated",
                    artifact=input_artifact.id,
                    from_model=entry.model_id,
                    to_model=self._escalation_entry.model_id,
                    trigger=self._trigger,
                    reason=post.reason or "explicit_flag",
                    confidence=signals.confidence,
                    threshold=threshold,
                )
                entry = self._escalation_entry
                escalations_used += 1
                continue
            break

        if not post.passed:
            self._emit(
                "blocked_post", artifact=input_artifact.id, reason=post.reason,
                confidence=signals.confidence, threshold=threshold,
            )
            return None

        # 4) WRITE SUCCESSOR — stamped with the model that actually produced it.
        model_ref = ModelRef(
            model_id=entry.model_id,
            registry_version=self._registry.metadata.registry_version,
            pin=entry.pin.ref,
            quant=entry.pin.quant,
            reproducible=entry.reproducible,
            served_model_id=response.served_model_id,
        )
        out = succeed(
            input_artifact,
            output_kind,
            candidate,
            agent=type(self).__name__,
            pack_name=self._pack.name,
            pack_version=self._pack.version,
            model_ref=model_ref,
        )
        self._store.write(out)
        # The terminal event of a chain step is a self-contained record: which
        # configuration (pack, model), what it cost (tokens, wall-clock, escalations),
        # and that it succeeded (a blocked run emits blocked_* and never gets here).
        self._emit(
            "wrote", artifact=out.id, kind=out.kind,
            served=response.served_model_id, confidence=candidate.get("confidence"),
            model=entry.model_id, pack=pack_ref,
            escalations=escalations_used,
            in_tok=response.input_tokens, out_tok=response.output_tokens,
            run_ms=round((perf_counter() - run_started) * 1000.0, 1),
        )
        return out

    def _retrieve(
        self, input_artifact: Artifact
    ) -> "tuple[str, Tuple[str, ...], Tuple[dict, ...], str]":
        """Ask the retriever (if any) for relevant prior context.

        Returns (rendered_context, refs, per-snippet meta, query). Meta is read
        best-effort from a ``snippets`` attribute (score/kind per snippet) so the
        recall event can record HOW relevant each memory was; a protocol-minimal
        retriever (refs + render only) yields empty meta and the run proceeds.
        """
        if self._retriever is None:
            return "", (), (), ""
        query = _stringify(input_artifact, input_cap(self._pack))
        ctx = self._retriever.retrieve(query, exclude_artifact_id=input_artifact.id)
        snippets = tuple(getattr(ctx, "snippets", ()) or ())
        meta = tuple(
            {"score": getattr(s, "score", None), "kind": getattr(s, "kind", None)}
            for s in snippets
        )
        return ctx.render(), tuple(ctx.refs), meta, query

    def _build_candidate(self, text: str, signals,
                         retrieved_refs: Tuple[str, ...] = ()) -> dict:
        candidate = {
            "text": text,
            "produced_by_pack": f"{self._pack.name}@{self._pack.version}",
        }
        if signals.confidence is not None:
            candidate["confidence"] = signals.confidence
        if signals.escalate:
            candidate["escalate"] = True
        # Carry a pack-named domain score (e.g. "score") from the model's JSON output into
        # the payload so the PostGate can enforce the pack's floor on it. The field NAME is
        # pack data, never a domain string in core (Primitive 1).
        scoring = self._pack.scoring
        if scoring is not None:
            raw = signals.fields.get(scoring.score_field)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                candidate[scoring.score_field] = raw
        # Audit trail: what prior context informed this output (Primitive 5 inspectability).
        if retrieved_refs:
            candidate["retrieved_from"] = list(retrieved_refs)
        return candidate

    def _check_post(self, candidate: dict, signals):
        required = self._pack.required_output_fields
        # A pack that declares `scoring` floors a domain-named score, and that makes the score
        # required whether or not the pack also lists it: a missing score is a schema violation,
        # one below the threshold goes to the escape hatch, and the model can't skip the floor.
        scoring = self._pack.scoring
        if scoring is not None:
            scored = self._post.check(
                candidate,
                required_fields=tuple(dict.fromkeys((*required, scoring.score_field))),
                score_field=scoring.score_field,
                threshold=scoring.threshold,
            )
            if not scored.passed:
                return scored
        # The confidence floor applies as well, with or without `scoring`. A confidence the
        # model did not report is allowed (a plain-text answer passes); one it reported
        # that is not a finite number is rejected, never read as absent.
        threshold = self._pack.thresholds.get("min_confidence")
        if threshold is not None and signals.confidence_invalid:
            return GateResult(False, reason="confidence is not a finite number",
                              tags=("escape_hatch", "schema_violation"))
        if threshold is not None and signals.confidence is not None:
            return self._post.check(
                candidate,
                required_fields=required,
                score_field="confidence",
                threshold=threshold,
            )
        if scoring is not None:
            return scored
        return self._post.check(candidate, required_fields=required)


def build_model_request(pack: Pack, input_artifact: Artifact, entry: ModelEntry,
                        *, retrieved_context: str = "") -> ModelRequest:
    """Build a normalized request: system = pack identity, user = rendered prompt.

    When `retrieved_context` is supplied, it is inserted as a SEPARATE system message between
    the pack identity and the user prompt, so a model can treat it as reference rather than as
    instructions. Empty context -> identical to the request without retrieval.

    Module-level so a caller can rebuild exactly the request the Agent would have issued for a
    given input + model.
    """
    user_text = pack.render_prompt(_stringify(input_artifact, input_cap(pack)))
    defaults = entry.params_default
    params = SamplingParams(
        temperature=defaults.temperature,
        top_p=defaults.top_p,
        max_tokens=defaults.max_tokens,
        stop=defaults.stop,
        num_ctx=defaults.num_ctx,
        keep_alive=defaults.keep_alive,
    )
    messages = [Message(role="system", content=pack.identity)]
    if retrieved_context:
        messages.append(Message(role="system", content=retrieved_context))
    messages.append(Message(role="user", content=user_text))
    return ModelRequest(messages=tuple(messages), params=params)


def _route_block_reason(tags, entry) -> Optional[str]:
    """Why this model route is illegal, or None. Deterministic; runs before every model call."""
    if is_local_provider_kind(entry.provider_kind) and not is_loopback_endpoint(entry.endpoint):
        return f"local-endpoint-not-loopback: provider_kind={entry.provider_kind!r}"
    blocked = blocked_nonlocal_model_tags(tags, entry.provider_kind)
    if blocked:
        return f"sensitivity-local-only: tags={list(blocked)} block provider_kind={entry.provider_kind!r}"
    return None


def input_cap(pack: Pack) -> Optional[int]:
    """The pack's max prompt-input size, or None for uncapped.

    Sourced from pack DATA (`thresholds.max_input_chars`) — the same generic numeric map
    `min_confidence` lives in — so the ceiling is config a human versions, never a magic
    number in core (Primitive 1). Absent, non-positive or non-numeric means uncapped.
    """
    try:
        value = int(pack.thresholds.get("max_input_chars") or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _stringify(artifact: Artifact, max_chars: Optional[int] = None) -> str:
    """Flatten an artifact payload into prompt text (generic; no domain assumptions).

    `max_chars` caps an oversized predecessor payload deterministically. Truncation is
    VISIBLE — the marker names how many characters were dropped — because a silently
    shortened prompt looks complete to both the model and the reader.
    """
    parts = [f"{k}={v}" for k, v in thaw(artifact.payload).items()]
    text = " ".join(parts)
    if max_chars is not None and max_chars > 0 and len(text) > max_chars:
        return f"{text[:max_chars]} …[truncated {len(text) - max_chars} chars]"
    return text

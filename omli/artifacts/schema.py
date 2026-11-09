"""Artifact schema and the canonical chain (Primitives 3 & 4).

The chain is fixed and ordered:

    CanonicalInput -> Spec -> DecisionRecord -> WorkResult -> OutcomeRecord

Artifacts are frozen dataclasses with read-only payloads. A successor must name a
later kind in the chain and link back to its predecessor. There are deliberately no
mutators — no update*, no set_status.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from omli.artifacts.provenance import ModelRef, Provenance
from omli.frozen import freeze, thaw  # noqa: F401  (thaw is re-exported)


def _model_ref_to_dict(ref: "ModelRef | None") -> "dict[str, Any] | None":
    if ref is None:
        return None
    return {
        "model_id": ref.model_id,
        "registry_version": ref.registry_version,
        "pin": ref.pin,
        "quant": ref.quant,
        "reproducible": ref.reproducible,
        "served_model_id": ref.served_model_id,
    }


def _model_ref_from_dict(data: "Mapping[str, Any] | None") -> "ModelRef | None":
    if data is None:
        return None
    return ModelRef(
        model_id=data["model_id"],
        registry_version=data["registry_version"],
        pin=data["pin"],
        quant=data["quant"],
        reproducible=data["reproducible"],
        served_model_id=data["served_model_id"],
    )


# One implementation shared with events and governance findings (see omli/frozen.py).
_freeze = freeze


# The canonical, ordered artifact chain. Order is meaningful: a successor's kind must
# come strictly after its predecessor's kind.
ARTIFACT_KINDS: tuple[str, ...] = (
    "CanonicalInput",
    "Spec",
    "DecisionRecord",
    "WorkResult",
    "OutcomeRecord",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Artifact:
    """An immutable, provenance-bearing unit of workflow state.

    The payload is deep-frozen at creation: nested mappings are read-only and lists become
    tuples, so neither the artifact nor its JSON-shaped contents can be mutated afterwards.
    A value of any other type is kept as given; payloads are expected to be JSON data.
    """

    kind: str
    payload: Mapping[str, Any]
    provenance: Provenance
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(default_factory=_utc_now_iso)

    def __post_init__(self) -> None:
        if self.kind not in ARTIFACT_KINDS:
            raise ValueError(
                f"unknown artifact kind {self.kind!r}; expected one of {ARTIFACT_KINDS}"
            )
        # Freeze the payload so primitive 4 holds all the way down.
        object.__setattr__(self, "payload", _freeze(self.payload))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to plain JSON-able data (for the append-only store log).

        The sensitivity tags and the connection stamps are OMITTED when unset rather than
        emitted as null/empty, so an artifact that carries none of them serializes without
        those keys. `model_ref` is always present, as null when no model produced the
        artifact.
        """
        provenance: dict[str, Any] = {
            "agent": self.provenance.agent,
            "pack_name": self.provenance.pack_name,
            "pack_version": self.provenance.pack_version,
            "predecessor_ids": list(self.provenance.predecessor_ids),
            "source_refs": list(self.provenance.source_refs),
            "model_ref": _model_ref_to_dict(self.provenance.model_ref),
        }
        # Every optional provenance field is serialized here and read back by from_dict, so a
        # stamp survives a reload. A field that was dropped on reload would look
        # enforced while recording nothing.
        if self.provenance.sensitivity:
            provenance["sensitivity"] = list(self.provenance.sensitivity)
        for name in ("connection_id", "connection_version", "selected_resource_id"):
            value = getattr(self.provenance, name, None)
            if value is not None:
                provenance[name] = value
        return {
            "id": self.id,
            "kind": self.kind,
            "created_at": self.created_at,
            "payload": thaw(self.payload),
            "provenance": provenance,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Artifact":
        """Rehydrate an artifact from its `to_dict()` form (for store reload).

        The inverse of `to_dict()`: it restores the original `id` and `created_at`
        rather than minting new ones, so a reloaded store is byte-identical to the log.
        This is a read path only — it never mutates an existing artifact.
        """
        prov = data["provenance"]
        provenance = Provenance(
            agent=prov["agent"],
            pack_name=prov["pack_name"],
            pack_version=prov["pack_version"],
            predecessor_ids=tuple(prov.get("predecessor_ids", ())),
            source_refs=tuple(prov.get("source_refs", ())),
            model_ref=_model_ref_from_dict(prov.get("model_ref")),
            # to_dict() omits these when unset, so an absent key defaults rather than raises.
            sensitivity=tuple(prov.get("sensitivity", ())),
            connection_id=prov.get("connection_id"),
            connection_version=prov.get("connection_version"),
            selected_resource_id=prov.get("selected_resource_id"),
        )
        return cls(
            kind=data["kind"],
            payload=data["payload"],
            provenance=provenance,
            id=data["id"],
            created_at=data["created_at"],
        )


def new_artifact(
    kind: str,
    payload: Mapping[str, Any],
    provenance: Provenance,
) -> Artifact:
    """Create a root or standalone artifact (typically a CanonicalInput)."""
    return Artifact(kind=kind, payload=payload, provenance=provenance)


def succeed(
    predecessor: Artifact,
    kind: str,
    payload: Mapping[str, Any],
    *,
    agent: str,
    pack_name: str,
    pack_version: str,
    extra_source_refs: tuple[str, ...] = (),
    model_ref: "ModelRef | None" = None,
    sensitivity: tuple[str, ...] = (),
) -> Artifact:
    """Write a linked successor (Primitive 4).

    The successor inherits the predecessor's source refs, links back via
    `predecessor_ids`, and must name a kind strictly later in the chain. When a model
    produced it, `model_ref` stamps the model identity into provenance.

    Sensitivity is ACCUMULATE-ONLY: `sensitivity` is unioned with the predecessor's tags,
    never substituted for them, so a caller passing a narrower set does not narrow the
    result. A shrinking classification is therefore not expressible through this API.
    The guarantee is structural: a narrower `sensitivity` argument is not an error, it
    simply has no narrowing effect.
    """
    pred_idx = ARTIFACT_KINDS.index(predecessor.kind)
    succ_idx = ARTIFACT_KINDS.index(kind)
    if succ_idx <= pred_idx:
        raise ValueError(
            f"{kind!r} does not follow {predecessor.kind!r} in the artifact chain"
        )
    provenance = Provenance(
        agent=agent,
        pack_name=pack_name,
        pack_version=pack_version,
        predecessor_ids=(predecessor.id,),
        source_refs=tuple(predecessor.provenance.source_refs) + tuple(extra_source_refs),
        model_ref=model_ref,
        sensitivity=_merge_sensitivity(predecessor.provenance.sensitivity, sensitivity),
    )
    return Artifact(kind=kind, payload=payload, provenance=provenance)


def _merge_sensitivity(inherited: tuple[str, ...], added: tuple[str, ...]) -> tuple[str, ...]:
    """Union, first-seen order, deduplicated.

    Order is deliberately NOT set iteration order: the same chain must serialize identically
    on every run, or a rebuilt log would not reproduce its integrity hashes.
    """
    out: list[str] = []
    for tag in tuple(inherited) + tuple(added):
        if tag and tag not in out:
            out.append(tag)
    return tuple(out)

"""Router — resolve a pack's model selection against the registry, and validate it.

A pack names a model only by `model_id`; the Router maps that id to a registry `ModelEntry`
and its adapter key. It fails loud: `resolve` checks `model_requirements ⊆ entry.capabilities`
and raises on a mismatch. `Agent` calls it in its constructor and `validate_packs` calls it for
a set of packs, so an impossible pack/model pairing fails before any model call.

The Router is also the status gate. Only ``staging`` and ``production``
entries resolve; ``draft`` and ``deprecated`` fail loud, so a draft model cannot quietly run
like a production one. (Moving an entry to ``production`` is meant to be a human edit to a new
registry version; see `omli.governance.promotion`.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from omli.models.registry import ModelEntry, ModelRegistry
from omli.packs.schema import Pack

#: Model-entry statuses the Router will run. draft/deprecated are catalog-only.
RESOLVABLE_STATUSES = frozenset({"staging", "production"})


@dataclass(frozen=True)
class ResolvedModel:
    """The outcome of resolving a pack: its primary model + optional escalation model."""

    entry: ModelEntry
    adapter: str
    escalation_entry: Optional[ModelEntry] = None


class Router:
    """Resolves pack model selections against a single immutable registry."""

    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

    def _get(self, model_id: str, *, what: str, pack_name: str) -> ModelEntry:
        try:
            return self._registry.get(model_id)
        except KeyError:
            raise ValueError(
                f"pack {pack_name!r}: {what} model_id {model_id!r} is not in registry "
                f"{self._registry.metadata.registry_id}@{self._registry.metadata.registry_version}"
            ) from None

    def _require_resolvable(self, entry: ModelEntry, *, what: str, pack_name: str) -> None:
        """Draft/deprecated never run. Loud ValueError names model + status."""
        if entry.status not in RESOLVABLE_STATUSES:
            raise ValueError(
                f"pack {pack_name!r}: {what} model {entry.model_id!r} has status "
                f"{entry.status!r}; Router.resolve accepts only "
                f"{sorted(RESOLVABLE_STATUSES)} (status gate)"
            )

    def resolve(self, pack: Pack) -> ResolvedModel:
        """Resolve and validate a pack's model selection. Raises ValueError on any problem."""
        sel = pack.model_selection
        if sel is None:
            raise ValueError(f"pack {pack.name!r} has no model_selection to resolve")

        entry = self._get(sel.model_id, what="selected", pack_name=pack.name)
        self._require_resolvable(entry, what="selected", pack_name=pack.name)

        if pack.model_requirements is not None:
            unmet = pack.model_requirements.unmet(entry.capabilities)
            if unmet:
                raise ValueError(
                    f"pack {pack.name!r}: model {entry.model_id!r} does not meet "
                    f"model_requirements: {'; '.join(unmet)}"
                )

        escalation_entry: Optional[ModelEntry] = None
        if sel.escalation is not None:
            escalation_entry = self._get(
                sel.escalation.model_id, what="escalation", pack_name=pack.name
            )
            self._require_resolvable(
                escalation_entry, what="escalation", pack_name=pack.name
            )
            # The escalation target must satisfy the same role requirements.
            if pack.model_requirements is not None:
                unmet = pack.model_requirements.unmet(escalation_entry.capabilities)
                if unmet:
                    raise ValueError(
                        f"pack {pack.name!r}: escalation model {escalation_entry.model_id!r} "
                        f"does not meet model_requirements: {'; '.join(unmet)}"
                    )

        return ResolvedModel(entry=entry, adapter=entry.adapter, escalation_entry=escalation_entry)


def validate_packs(registry: ModelRegistry, packs: Iterable[Pack]) -> None:
    """Resolve every pack that declares a model selection; raise on the first that fails.

    Packs without `model_selection` are skipped. A pack with requirements but no selection
    is rejected by the loader, and is rejected here too.
    """
    router = Router(registry)
    for pack in packs:
        if pack.model_selection is None:
            if pack.model_requirements is not None:
                raise ValueError(f"pack {pack.name!r} declares model_requirements without model_selection")
            continue
        router.resolve(pack)

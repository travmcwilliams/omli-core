"""OMLi (Open Model Local integrator) — shared core.

This package contains ONLY generic machinery. All domain logic lives in packs, as data
(Primitive 1). Nothing here may reference a vertical name,
business term, or hardcoded domain threshold.

Comments throughout the code cite five design rules by number. They are defined here:

1. Shared core + config packs: domain logic lives in packs (data), never in core code.
2. Deterministic gates wrap model calls: rule-based checks run before and after a model,
   so a model is never the first or last decision-maker on the Agent path.
3. Artifacts are the state: workflow state lives in the artifact chain
   (CanonicalInput -> Spec -> DecisionRecord -> WorkResult -> OutcomeRecord), not in a
   coordinator or a status field.
4. Immutable and append-only: an artifact is written once; a change is a linked successor,
   and status changes are appended events.
5. Read-only self-data: no agent rewrites its own memory, identity or record; only the
   Reflector writes learned memory, and nothing tunes itself without a human approval.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"

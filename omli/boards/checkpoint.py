"""Checkpoint a board: its own explicit action, recorded as an artifact chain and a log entry.

Each checkpoint appends, to ``OMLI_HOME/var/artifacts.jsonl``:

    CanonicalInput  the board as replayed from its log, its hash and the last operation id
      -> Spec           a deterministic summary of what the snapshot holds
        -> DecisionRecord   the decision to checkpoint, with its label

then appends a ``checkpoint`` operation to the board's log naming the CanonicalInput. All
three artifacts are produced by deterministic code: no model is called and none is named.
An unchanged board (same hash as its latest checkpoint) writes nothing (Primitive 3), and a
new checkpoint links to the previous one, which is never rewritten (Primitive 4).
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from omli.artifacts import ArtifactStore, Provenance, new_artifact
from omli.artifacts.schema import succeed
from omli.boards.log import BoardLog
from omli.boards.vocab import identity
from omli.home import omli_home

AGENT = "MindmapCheckpointer"
ARTIFACT_LOG = Path("var") / "artifacts.jsonl"
MAX_LABEL = 200
_LOCK = threading.Lock()


def artifact_log(home: str | Path | None = None) -> Path:
    root = Path(home).resolve() if home is not None else omli_home()
    return root / ARTIFACT_LOG


def _hash(state: Dict[str, Any]) -> str:
    blob = json.dumps(state, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _ref(board: str) -> str:
    return f"board:{board}"


def _checkpoints(store: ArtifactStore, board: str):
    ref = _ref(board)
    return [a for a in store.by_kind("CanonicalInput")
            if a.provenance.agent == AGENT and ref in a.provenance.source_refs]


def _successor(store: ArtifactStore, artifact_id: Optional[str], kind: str):
    if artifact_id is None:
        return None
    return next((a for a in store.successors(artifact_id) if a.kind == kind), None)


def create_checkpoint(board: str, label: str, *, by: Optional[str] = None,
                      home: str | Path | None = None) -> Dict[str, Any]:
    """Append a checkpoint chain for the board as it stands. ``created`` is False, and
    nothing is written, when the board has not changed since its latest checkpoint."""
    if not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL:
        raise ValueError(f"a checkpoint needs a label of 1-{MAX_LABEL} characters")
    log = BoardLog(board, home)
    pack_name, pack_version = identity()
    with _LOCK:
        records = log.ops()
        if not records:
            raise KeyError(f"there is no board {board!r}")
        state = log.replay()
        head = records[-1]["op"]
        board_hash = _hash(state)
        store = ArtifactStore.from_log(artifact_log(home))
        prior = _checkpoints(store, board)
        if prior and prior[-1].payload.get("board_hash") == board_hash:
            last = prior[-1]
            spec = _successor(store, last.id, "Spec")
            decision = _successor(store, spec.id if spec else None, "DecisionRecord")
            return {"created": False, "checkpoint_id": last.id,
                    "spec_id": spec.id if spec else None,
                    "decision_id": decision.id if decision else None,
                    "board_hash": board_hash, "head": last.payload.get("head")}

        ci = new_artifact("CanonicalInput", {
            "board": board, "board_hash": board_hash, "head": head, "label": label,
            "nodes": state["nodes"], "links": state["links"]},
            Provenance(agent=AGENT, pack_name=pack_name, pack_version=pack_version,
                       predecessor_ids=(prior[-1].id,) if prior else (),
                       source_refs=(_ref(board), f"boardhash:{board_hash}")))
        spec = succeed(ci, "Spec", {
            "text": (f"Board {board} at operation {head}: {len(state['nodes'])} nodes, "
                     f"{len(state['links'])} links."),
            "nodes": len(state["nodes"]), "links": len(state["links"])},
            agent=AGENT, pack_name=pack_name, pack_version=pack_version)
        decision = succeed(spec, "DecisionRecord", {
            "decision": "checkpoint", "label": label, "board_hash": board_hash, "head": head,
            "by": by}, agent=AGENT, pack_name=pack_name, pack_version=pack_version)
        for artifact in (ci, spec, decision):  # persisted before the log names it
            store.write(artifact)
        log.append("checkpoint", by=by, label=label, artifact=ci.id)
    return {"created": True, "checkpoint_id": ci.id, "spec_id": spec.id,
            "decision_id": decision.id, "board_hash": board_hash, "head": head}


def list_checkpoints(board: str, home: str | Path | None = None) -> List[Dict[str, Any]]:
    """The board's checkpoint lineage, oldest first, answered from the artifact store alone."""
    BoardLog(board, home)  # validates the board id
    path = artifact_log(home)
    if not path.exists():
        return []
    store = ArtifactStore.from_log(path)
    out = []
    for ci in _checkpoints(store, board):
        spec = _successor(store, ci.id, "Spec")
        decision = _successor(store, spec.id if spec else None, "DecisionRecord")
        out.append({"checkpoint_id": ci.id, "created_at": ci.created_at,
                    "label": ci.payload.get("label"), "board_hash": ci.payload.get("board_hash"),
                    "head": ci.payload.get("head"),
                    "predecessor_id": (ci.provenance.predecessor_ids or (None,))[0],
                    "spec_id": spec.id if spec else None,
                    "decision_id": decision.id if decision else None})
    return out

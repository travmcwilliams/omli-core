"""Check a retrieval ladder drawn on a board: a pure graph check, no model, no file access.

Rung nodes carry ``kind`` (source, context, retriever, reranker, agent, prompt, output) and
are joined by links whose ``relation`` is ``flow``. A ladder is well formed when the flow is
acyclic, there is at least one source bound to a folder (``catalog_ref.path``) and one agent
bound to a model role (``catalog_ref.model_id``), and it ends in an output.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

RUNG_KINDS = ("source", "context", "retriever", "reranker", "agent", "prompt", "output")
FLOW = "flow"


def _ref(node: Mapping[str, Any]) -> Mapping[str, Any]:
    ref = node.get("catalog_ref")
    return ref if isinstance(ref, dict) else {}


def _label(nid: str, node: Mapping[str, Any]) -> str:
    ref = _ref(node)
    return str(ref.get("name") or ref.get("model_id") or node.get("text") or nid)


def _toposort(ids: List[str], succ: Dict[str, List[str]],
              pred: Dict[str, List[str]]) -> Optional[List[str]]:
    rank = {nid: i for i, nid in enumerate(ids)}
    indeg = {i: len(pred[i]) for i in ids}
    ready = sorted((i for i in ids if indeg[i] == 0), key=rank.get)
    out: List[str] = []
    while ready:
        cur = ready.pop(0)
        out.append(cur)
        for nxt in succ[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
                ready.sort(key=rank.get)
    return out if len(out) == len(ids) else None


def _reachable(starts, succ) -> set:
    seen, stack = set(), list(starts)
    while stack:
        cur = stack.pop()
        if cur not in seen:
            seen.add(cur)
            stack.extend(succ.get(cur, ()))
    return seen


def rungs(board: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    """The ladder's rung nodes on ``board``, keyed by id, in board order."""
    nodes = board.get("nodes", {})
    return {nid: n for nid, n in nodes.items()
            if isinstance(n, dict) and n.get("kind") in RUNG_KINDS}


def validate_ladder(board: Mapping[str, Any]) -> Dict[str, Any]:
    """``{"ok", "errors", "warnings", "order"}`` for the ladder on ``board``."""
    steps = rungs(board)
    if not steps:
        return {"ok": False, "errors": ["no ladder rungs on the board"], "warnings": [],
                "order": []}
    ids = list(steps)
    succ: Dict[str, List[str]] = {i: [] for i in ids}
    pred: Dict[str, List[str]] = {i: [] for i in ids}
    for link in (board.get("links") or {}).values():
        if link.get("relation") == FLOW and link.get("from") in steps and link.get("to") in steps:
            succ[link["from"]].append(link["to"])
            pred[link["to"]].append(link["from"])

    errors: List[str] = []
    warnings: List[str] = []
    order = _toposort(ids, succ, pred)
    if order is None:
        errors.append("flow has a cycle")
    by_kind = {k: [i for i in ids if steps[i]["kind"] == k] for k in RUNG_KINDS}
    if not by_kind["source"]:
        errors.append("the ladder has no source rung")
    for i in by_kind["source"]:
        if not _ref(steps[i]).get("path"):
            errors.append(f"source '{_label(i, steps[i])}' is not bound to a folder")
    if not by_kind["agent"]:
        errors.append("the ladder has no agent rung")
    for i in by_kind["agent"]:
        if not _ref(steps[i]).get("model_id"):
            errors.append(f"agent '{_label(i, steps[i])}' is not bound to a model role")
    if not by_kind["output"]:
        errors.append("the ladder does not end in an output rung")
    if order is not None:
        from_sources = _reachable(by_kind["source"], succ)
        for i in by_kind["agent"]:
            if by_kind["source"] and i not in from_sources:
                warnings.append(f"agent '{_label(i, steps[i])}' is not reachable from a source")
        from_agents = _reachable(by_kind["agent"], succ)
        ends = [i for i in by_kind["output"] if not succ[i]]
        if by_kind["output"] and not any(i in from_agents for i in ends):
            warnings.append("no output rung is reachable from an agent")
    for i in by_kind["retriever"]:
        k = (steps[i].get("params") or {}).get("k")
        if k is not None and (isinstance(k, bool) or not isinstance(k, int) or k <= 0):
            warnings.append(f"retriever '{_label(i, steps[i])}' has an invalid k")
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "order": order if order is not None else ids}

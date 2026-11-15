"""Turn a target board into the operations that reach it from the current one.

A save sends the whole board it shows; the server derives the operations, so the log records
what changed and who changed it. Deterministic: the same two boards always give the same
operations, in this order: link deletes, node deletes, node adds, node edits and moves, link
adds. A node's ``x``, ``y`` and ``parent`` travel as ``node.move``; every other field as
``node.add`` data or a ``node.edit`` (a field that disappears is edited to null).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PLACE = ("x", "y", "parent")
MAX_NODES = 5000
MAX_LINKS = 20000


def _ids(where: str, items: Any, limit: int) -> Mapping[str, dict]:
    if not isinstance(items, dict):
        raise ValueError(f"{where} must be an object keyed by id")
    if len(items) > limit:
        raise ValueError(f"{where}: at most {limit} entries")
    for key, value in items.items():
        if not isinstance(key, str) or not _ID.fullmatch(key):
            raise ValueError(f"{where}: bad id {key!r}")
        if not isinstance(value, dict):
            raise ValueError(f"{where}[{key}] must be an object")
    return items


def check_board(target: Any) -> Dict[str, Dict[str, dict]]:
    """``target`` as ``{"nodes": {...}, "links": {...}}``, or ValueError naming what is wrong."""
    if not isinstance(target, dict):
        raise ValueError("a board is an object with nodes and links")
    nodes = _ids("nodes", target.get("nodes", {}), MAX_NODES)
    links = _ids("links", target.get("links", {}) or {}, MAX_LINKS)
    for key, node in nodes.items():
        for axis in ("x", "y"):
            if axis in node and (isinstance(node[axis], bool)
                                 or not isinstance(node[axis], (int, float))):
                raise ValueError(f"nodes[{key}].{axis} must be a number")
        if node.get("parent") is not None and not isinstance(node["parent"], str):
            raise ValueError(f"nodes[{key}].parent must be a node id")
    for key, link in links.items():
        for end in ("from", "to"):
            if not isinstance(link.get(end), str) or link[end] not in nodes:
                raise ValueError(f"links[{key}].{end} must name a node on the board")
    return {"nodes": dict(nodes), "links": dict(links)}


def ops_for(current: Mapping[str, Mapping[str, dict]], target: Any) -> List[dict]:
    """The operations that turn ``current`` (a replayed board) into ``target``."""
    want = check_board(target)
    have_nodes, have_links = current["nodes"], current["links"]
    want_nodes, want_links = want["nodes"], want["links"]
    gone = [n for n in have_nodes if n not in want_nodes]
    ops: List[dict] = []

    for lid, link in have_links.items():
        if link["from"] in gone or link["to"] in gone:
            continue  # dropped with its node
        if want_links.get(lid) != link:
            ops.append({"kind": "link.delete", "link": lid})
    for nid in gone:
        ops.append({"kind": "node.delete", "node": nid})
    for nid, node in want_nodes.items():
        if nid not in have_nodes:
            ops.append({"kind": "node.add", "node": nid, "data": dict(node)})
            continue
        old = have_nodes[nid]
        fields = {k: v for k, v in node.items() if k not in _PLACE and old.get(k) != v}
        fields.update({k: None for k in old if k not in _PLACE and k not in node})
        if fields:
            ops.append({"kind": "node.edit", "node": nid, "data": fields})
        if any(old.get(k) != node.get(k) for k in _PLACE):
            move = {"kind": "node.move", "node": nid,
                    "x": node.get("x", old.get("x", 0)), "y": node.get("y", old.get("y", 0))}
            if "parent" in node or "parent" in old:
                move["parent"] = node.get("parent")
            ops.append(move)
    for lid, link in want_links.items():
        if have_links.get(lid) != link:
            data = {k: v for k, v in link.items() if k not in ("from", "to")}
            ops.append({"kind": "link.add", "link": lid, "from": link["from"],
                        "to": link["to"], "data": data})
    return ops

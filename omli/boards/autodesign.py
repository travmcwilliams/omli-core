"""Auto-design, offline only: a fixed set of questions from pack data and a starter board built from
the answers by a template. No model is called and nothing is written; the page saves the
board it is given like any other edit.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping

from omli.boards.vocab import section

MAX_ANSWER = 2000
MAX_PARTS = 12


def _questions():
    return tuple(section("autodesign").get("questions", ()))


def next_question(answers: Mapping[str, Any]) -> Dict[str, Any]:
    """The first question not answered yet, or ``done`` when every one has an answer."""
    for q in _questions():
        value = answers.get(q["id"]) if isinstance(answers, Mapping) else None
        if not isinstance(value, str) or not value.strip():
            return {"done": False, "question": {"id": str(q["id"]), "text": str(q["text"])}}
    return {"done": True, "question": None}


def _parts(value: Any):
    if not isinstance(value, str):
        return []
    return [p.strip()[:200] for p in value[:MAX_ANSWER].split(",") if p.strip()][:MAX_PARTS]


def _slug(text: str, used: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "node"
    nid, n = base, 2
    while nid in used:
        nid, n = f"{base}-{n}", n + 1
    used.add(nid)
    return nid


def build_board(answers: Mapping[str, Any]) -> Dict[str, Dict[str, dict]]:
    """A starter board: the first answer is the centre, each later answer a branch whose
    comma-separated parts hang off it. The same answers always give the same board."""
    nodes: Dict[str, dict] = {}
    links: Dict[str, dict] = {}
    used: set = set()
    qs = _questions()
    centre_text = (_parts(answers.get(qs[0]["id"])) or ["Untitled board"])[0] if qs else "Board"
    centre = _slug(centre_text, used)
    nodes[centre] = {"text": centre_text, "x": 0, "y": 0}
    for i, q in enumerate(qs[1:], 1):
        parts = _parts(answers.get(q["id"]))
        if not parts:
            continue
        branch = _slug(str(q["id"]), used)
        nodes[branch] = {"text": str(q["id"]), "x": 240, "y": 160 * (i - 1)}
        links[f"l-{branch}"] = {"from": centre, "to": branch}
        for j, part in enumerate(parts):
            leaf = _slug(part, used)
            nodes[leaf] = {"text": part, "x": 480, "y": 160 * (i - 1) + 40 * j}
            links[f"l-{leaf}"] = {"from": branch, "to": leaf}
    return {"nodes": nodes, "links": links}

"""Views derived from the board logs, never stored as truth.

* ``node_stats`` — last editor and edit counts per node on the current board.
* ``list_boards`` — every board under OMLI_HOME with a short summary, optionally searched.

``OMLI_HOME/boards/index.json`` caches the summaries. It is a cache only: an entry is used
while its board log has the same size and modification time it was built from, and is rebuilt
otherwise. A missing, stale or unreadable index is rebuilt from the logs, so deleting it loses
nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from omli.boards.log import BOARDS_DIR, BoardLog, _BOARD_ID, board_path
from omli.home import omli_home
from omli.integrity.ledger import atomic_write_text

INDEX = "index.json"
INDEX_FORMAT = 1


def node_stats(log: BoardLog) -> Dict[str, Dict[str, Any]]:
    """For each node on the board now: how many operations added, edited or moved it, and
    the last of them (who, when, which op). History stays history across a restore."""
    stats: Dict[str, Dict[str, Any]] = {}
    for rec in log.ops():
        if rec["kind"] in ("node.add", "node.edit", "node.move"):
            s = stats.setdefault(rec["node"], {"edits": 0})
            s["edits"] += 1
            s["last_by"], s["last_at"], s["last_op"] = rec["by"], rec["at"], rec["op"]
    on_board = log.replay()["nodes"]
    return {node: s for node, s in stats.items() if node in on_board}


def _summary(log: BoardLog) -> Dict[str, Any]:
    ops = log.ops()
    board = log.replay()
    text: List[str] = [log.board]
    for node in board["nodes"].values():
        text.extend(v for v in node.values() if isinstance(v, str))
    last = ops[-1] if ops else {}
    return {"board": log.board, "nodes": len(board["nodes"]), "links": len(board["links"]),
            "ops": len(ops), "last_at": last.get("at"), "last_by": last.get("by"),
            "text": text}


def _root(home: str | Path | None) -> Path:
    return (Path(home).resolve() if home is not None else omli_home()) / BOARDS_DIR


def _read_index(root: Path) -> Dict[str, Any]:
    try:
        data = json.loads((root / INDEX).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("v") != INDEX_FORMAT:
        return {}
    boards = data.get("boards")
    return boards if isinstance(boards, dict) else {}


def _build(home: str | Path | None, cached: Dict[str, Any]) -> Dict[str, Any]:
    root = _root(home)
    entries: Dict[str, Any] = {}
    for path in sorted(root.glob("*.jsonl")) if root.is_dir() else []:
        board = path.stem
        if not _BOARD_ID.fullmatch(board):
            continue
        try:
            board_path(board, home)  # a name the log refuses (such as a device name) is skipped
        except ValueError:
            continue
        st = path.stat()
        key = [st.st_size, st.st_mtime_ns]
        old = cached.get(board)
        if isinstance(old, dict) and old.get("key") == key and isinstance(old.get("summary"), dict):
            entries[board] = old
        else:
            entries[board] = {"key": key, "summary": _summary(BoardLog(board, home))}
    if entries != cached:
        root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(root / INDEX, json.dumps({"v": INDEX_FORMAT, "boards": entries},
                                                   sort_keys=True) + "\n")
    return entries


def rebuild_index(home: str | Path | None = None) -> None:
    """Rebuild the cache from the logs alone."""
    _build(home, {})


def list_boards(query: Optional[str] = None,
                home: str | Path | None = None) -> List[Dict[str, Any]]:
    """Every board, sorted by id. With ``query``, only boards whose id or node text contains
    it, ignoring case."""
    entries = _build(home, _read_index(_root(home)))
    needle = query.casefold() if query else None
    out = []
    for board in sorted(entries):
        summary = dict(entries[board]["summary"])
        text = summary.pop("text", [])
        if needle is None or any(needle in t.casefold() for t in text):
            out.append(summary)
    return out

"""Boards: one append-only edit log per board under OMLI_HOME, and views derived from it.

* ``log``    — ``BoardLog``: append, append_many (one save, one write), replay (whole or up to an
  operation), restore, checkpoint.
* ``repair`` — ``python -m omli.boards repair-tail``: a person keeps a half-written last line
  aside; never automatic.
* ``derive`` — last editor and edit counts per node; the board list with search, cached in a
  rebuildable index.
* ``seal_board`` — seal a board log with ``omli.integrity``; verify it with the existing
  command, ``python -m omli.integrity verify <board log> --no-anchor --no-sign``.
* The "How OMLi works" starter board ships as package data (``starter/``).

The Mind-map's core, used by ``omli.ui.mindmap`` and free of any web stack:

* ``diff``       — a whole board from a save -> the operations that reach it
* ``checkpoint`` — a checkpoint as CanonicalInput -> Spec -> DecisionRecord, no model
* ``assist``     — every model call: Agent.run between the public pre- and post-gate
* ``ladder``, ``retrieve`` — the retrieval ladder check, and retrieval inside listed folders
* ``extract``, ``icon``, ``autodesign`` — deterministic helpers; vocabularies in ``vocab``
  come from ``omli/defaults/mindmap.yaml``.

Only the public core is imported.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from omli.boards.derive import list_boards, node_stats, rebuild_index
from omli.boards.log import KINDS, BoardConflict, BoardLog, BoardLogError, board_path
from omli.integrity.verifier import SealResult, seal

STARTER_BOARD = "how-omli-works"


def seal_board(board: str, home: str | Path | None = None) -> SealResult:
    """Seal a board's log as it stands, holding its write lock so no half-written line is
    sealed. The seal sits beside the log as ``<board>.seal.json``."""
    log = BoardLog(board, home)
    with log.locked():
        return seal(log.path)


def starter_bytes() -> bytes:
    """The starter board's log, exactly as shipped in the package."""
    return resources.files("omli.boards").joinpath("starter", f"{STARTER_BOARD}.jsonl").read_bytes()


def install_starter(home: str | Path | None = None) -> BoardLog:
    """Create the starter board under OMLI_HOME if it does not exist yet. An existing board
    of that name is left exactly as it is."""
    log = BoardLog(STARTER_BOARD, home)
    data = starter_bytes()
    with log.locked():
        log.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(log.path, "xb") as fh:
                fh.write(data)
        except FileExistsError:
            pass
    log.ops()  # the shipped file must read cleanly
    return log


__all__ = ["KINDS", "STARTER_BOARD", "BoardConflict", "BoardLog", "BoardLogError", "board_path",
           "install_starter", "list_boards", "node_stats", "rebuild_index", "seal_board",
           "starter_bytes"]

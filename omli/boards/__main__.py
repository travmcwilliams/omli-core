"""Board commands a person runs: ``python -m omli.boards``.

    python -m omli.boards repair-tail <board>   # keep a half-written last line aside

``repair-tail`` acts only on an unterminated final line outside the sealed part of the board's
log under OMLI_HOME (see ``omli.boards.repair``). It exits 0 when it repaired the log or found
nothing to repair, and 1 when it refused; a refusal changes nothing.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from omli.boards.repair import RepairRefused, repair_tail


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omli.boards")
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("repair-tail", help="keep a half-written last line aside")
    rep.add_argument("board")
    args = parser.parse_args(argv)

    try:
        result = repair_tail(args.board)
    except (RepairRefused, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    if result.side_file is None:
        print(f"{result.log.name}: the last line is complete; nothing to repair")
    else:
        print(f"{result.log.name}: kept {result.torn_bytes} torn bytes in "
              f"{result.side_file.name}; every complete line is unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())

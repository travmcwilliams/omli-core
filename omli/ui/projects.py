"""The user's project list, kept as append-only events under OMLI_HOME (``ui/projects.jsonl``).

Each line is one event, ``project.added`` or ``project.removed``; the list is derived by
replaying them, never stored (Primitive 4: change by appending, never by rewriting). Every
path is resolved here, on the server, to an existing absolute folder, and stored in the case
its directory listing gives: its case is not folded and no drive letter is assumed. Two
spellings are one entry when the file system says they are the same folder.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from omli.ui.home import ui_home

EVENTS = "ui/projects.jsonl"
ADDED = "project.added"
REMOVED = "project.removed"
# A scan stops descending past this many levels below the folder it was given, and after
# this many folders in all, so one scan of a large disk cannot run away.
MAX_SCAN_DEPTH = 6
MAX_SCAN_FOLDERS = 20_000

_LOCK = threading.Lock()


class ProjectLogError(ValueError):
    """The event file exists but a line cannot be read: fail closed, never guess the list."""


def _same_folder(a: str | Path, b: str | Path) -> bool:
    """True when two paths name one folder on disk. A path that cannot be checked is not."""
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def real_case(path: Path, *, listdir: Callable[[Path], Iterable[str]] = os.listdir,
              same: Callable[[Path, Path], bool] = _same_folder) -> Path:
    """``path`` spelled as the directory listings spell it.

    ``Path.resolve()`` does not correct case on every platform (macOS keeps the typed case),
    so each name below the anchor is taken from its parent's listing: the exact name when the
    listing has it, else the one listed name that differs only in case AND is the same folder.
    On a case-sensitive disk an exact match always wins, so ``Repo`` and ``repo`` stay apart.
    A folder that cannot be listed keeps the rest of the path as given.
    """
    out = Path(path.anchor)
    parts = path.parts[1:]
    for i, part in enumerate(parts):
        try:
            names = list(listdir(out))
        except OSError:
            return out.joinpath(*parts[i:])
        if part in names:
            out = out / part
            continue
        folded = part.casefold()
        match = next((n for n in sorted(names)
                      if n.casefold() == folded and same(out / n, out / part)), None)
        out = out / (match if match is not None else part)
    return out


def resolve_folder(raw: object) -> Path:
    """An absolute, existing folder, fully resolved and in its real case. Anything else
    raises ValueError."""
    if not isinstance(raw, str) or not raw:
        raise ValueError("a folder path is required")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise ValueError("the folder path must be absolute")
    try:
        p = p.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("the folder does not exist") from None
    if not p.is_dir():
        raise ValueError("the path is not a folder")
    return real_case(p)


def _listed_as(path: str, listed: Iterable[str]) -> Optional[str]:
    """The listed entry for ``path``: the same string, else the same folder on disk."""
    listed = list(listed)
    if path in listed:
        return path
    return next((entry for entry in listed if _same_folder(entry, path)), None)


def _is_repo(folder: Path) -> bool:
    # A directory for a normal clone, a file for a worktree or submodule.
    return (folder / ".git").exists()


class ProjectStore:
    def __init__(self, home: str | Path | None = None):
        root = ui_home(home)
        self.path = root / EVENTS

    def _events(self) -> List[dict]:
        if not self.path.exists():
            return []
        events = []
        for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                raise ProjectLogError(f"{EVENTS} line {n} is not JSON") from None
            if (not isinstance(ev, dict) or ev.get("type") not in (ADDED, REMOVED)
                    or not isinstance(ev.get("path"), str)):
                raise ProjectLogError(f"{EVENTS} line {n} is not a project event")
            events.append(ev)
        return events

    def list(self) -> List[str]:
        current: dict = {}
        for ev in self._events():
            if ev["type"] == ADDED:
                current[ev["path"]] = True
            else:
                current.pop(ev["path"], None)
        return list(current)

    def _append(self, kind: str, path: str, by: Optional[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ev = {"type": kind, "path": path, "at": datetime.now(timezone.utc).isoformat()}
        if by:
            ev["by"] = by
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(ev, sort_keys=True, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def add(self, raw: object, by: Optional[str] = None) -> str:
        """Add a folder; returns its resolved path. Adding a listed folder writes nothing."""
        path = str(resolve_folder(raw))
        with _LOCK:
            existing = _listed_as(path, self.list())
            if existing is not None:
                return existing
            self._append(ADDED, path, by)
        return path

    def remove(self, raw: object, by: Optional[str] = None) -> str:
        """Remove a listed entry. The entry is matched as listed, so a folder that has since
        been deleted can still be removed. An unlisted path raises KeyError."""
        if not isinstance(raw, str) or not raw:
            raise ValueError("a folder path is required")
        with _LOCK:
            listed = self.list()
            path = raw if raw in listed else None
            if path is None:
                try:
                    candidate = str(resolve_folder(raw))
                except ValueError:
                    candidate = None
                path = _listed_as(candidate, listed) if candidate is not None else None
            if path is None:
                raise KeyError("that folder is not in the project list")
            self._append(REMOVED, path, by)
        return path

    def scan(self, raw: object, by: Optional[str] = None) -> List[str]:
        """Add every git repository under a folder; returns the ones that were new. Hidden
        folders are skipped, and a repository's own subfolders are not searched."""
        root = resolve_folder(raw)
        found: List[str] = []
        seen = 0
        for dirpath, dirnames, _ in os.walk(root):
            seen += 1
            here = Path(dirpath)
            if _is_repo(here):
                found.append(str(real_case(here.resolve())))
                dirnames[:] = []
                continue
            depth = len(here.relative_to(root).parts)
            if depth >= MAX_SCAN_DEPTH or seen >= MAX_SCAN_FOLDERS:
                dirnames[:] = []
                continue
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        added = []
        with _LOCK:
            listed = self.list()
            for path in found:
                if _listed_as(path, listed) is None:
                    self._append(ADDED, path, by)
                    listed.append(path)
                    added.append(path)
        return added

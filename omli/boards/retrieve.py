"""Retrieval for the ladder: only the user's project folders, and only inside them.

* ``catalog`` lists the folders in the user's project list as sources, and the model roles in
  the user's registry as agents. Nothing else is offered.
* ``source_root`` accepts a folder only when, once resolved (``..`` and symlinks followed), it
  is a listed folder or inside one; anything else raises ``OutsideProjects``.
* ``retrieve`` walks a source without following symlinks, skips any file whose real path is
  not inside the source, keeps to the pack's text types and size limit, and scores by shared
  words. Deterministic; no model.
* Never read, so never sent to a model: hidden folders and files (``skip_hidden``), folders in
  ``skip_dirs`` (``.git`` and the like), files whose names match the pack's ``skip_names``
  (environment files, keys, certificates, credential and secret stores), files over
  ``max_file_bytes``, and any folder passed in ``exclude`` (the UI's own data folder).
* One request walks all its sources together: each real folder is visited once, decided by
  file identity, so a junction or link loop ends and sources that name the same or
  overlapping folders are read once. A folder is entered only when its real path is no more
  than ``max_depth`` folders below the source, whatever path led to it. The request examines
  no more than ``max_dirs`` folders (each source, and each subfolder considered for entry,
  counts once) and looks at no more than ``max_files`` files in all, across every source.
  Only regular files are read (never a pipe or a device).
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
from pathlib import Path, PurePath
from typing import Dict, Iterable, Iterator, List, Optional, Sequence

from omli.boards.vocab import section

_WORD = re.compile(r"[a-z0-9]{2,}")


class OutsideProjects(ValueError):
    """The path is not inside a folder on the user's project list."""


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _same_or_inside(path: Path, root: Path) -> bool:
    if _inside(path, root):
        return True
    try:  # a different spelling of the same folder (case, short names)
        return os.path.samefile(path, root)
    except (OSError, ValueError):
        return False


def catalog(folders: Iterable[str], registry=None) -> Dict[str, list]:
    """Sources from the project list; agents from the registry's model roles."""
    sources = [{"id": str(f), "name": PurePath(str(f)).name, "path": str(f)} for f in folders]
    agents = []
    if registry is not None:
        for entry in registry:
            agents.append({"id": entry.model_id, "name": entry.display_name,
                           "provider_kind": entry.provider_kind, "status": entry.status})
    return {"sources": sources, "agents": agents}


def source_root(raw: object, folders: Sequence[str]) -> Path:
    """The resolved folder, when it is a listed project folder or inside one."""
    if not isinstance(raw, str) or not raw.strip():
        raise OutsideProjects("a source needs a folder path")
    path = Path(raw)
    if not path.is_absolute():
        raise OutsideProjects("a source must be an absolute path inside a listed project")
    real = path.resolve()
    limits = section("retrieval")
    for folder in folders:
        root = Path(folder).resolve()
        if _same_or_inside(real, root) and real.is_dir():
            parts = real.relative_to(root).parts if _inside(real, root) else ()
            if any(blocked_dir(part, limits) for part in parts):
                raise OutsideProjects("retrieval never reads a hidden, skipped or "
                                      "secret-looking folder")
            return real
    raise OutsideProjects("that folder is not inside a project on your list")


def excluded_name(name: str, limits=None) -> bool:
    """True when a file of this name is never read: hidden, or matching ``skip_names``."""
    limits = limits or section("retrieval")
    lower = name.lower()
    if limits.get("skip_hidden") and lower.startswith("."):
        return True
    return any(fnmatch.fnmatchcase(lower, str(pat).lower()) for pat in limits["skip_names"])


def blocked_dir(name: str, limits=None) -> bool:
    """True for a folder retrieval never enters: one in ``skip_dirs``, a hidden one, or one
    named like a secret store (the same ``skip_names`` patterns as files)."""
    limits = limits or section("retrieval")
    return (name.lower() in {str(d).lower() for d in limits["skip_dirs"]}
            or excluded_name(name, limits))


def _identity(folder: Path):
    """(device, file number) of a folder, following links; None when it cannot be read."""
    try:
        st = os.stat(folder)
    except OSError:
        return None
    return (st.st_dev, st.st_ino) if st.st_ino else ("path", os.path.normcase(str(folder.resolve())))


def _enter(here: str, d: str, root: Path, limits, shut: Sequence[Path]) -> bool:
    """Whether the walk goes into folder ``d`` of ``here``."""
    p = Path(here, d)
    if blocked_dir(d, limits) or os.path.islink(p):
        return False
    real = p.resolve()
    # A junctioned folder whose real path leaves the source is never entered, nor a junction
    # into a blocked folder of the same source, nor an excluded folder (or one inside it),
    # nor a folder whose real depth below the source is over the limit (a junction is no
    # shortcut past it).
    if not _inside(real, root):
        return False
    parts = real.relative_to(root).parts
    return (len(parts) <= int(limits["max_depth"])
            and not any(blocked_dir(part, limits) for part in parts)
            and not any(_same_or_inside(real, e) for e in shut))


class _Walk:
    """What one request has walked so far, shared by all its sources."""

    def __init__(self, limits) -> None:
        self.seen: set = set()
        self.looked = 0
        self.examined = 0
        self.max_files = int(limits["max_files"])
        self.max_dirs = int(limits["max_dirs"])


def _files(root: Path, limits, exclude: Sequence[Path] = (),
           walk: Optional[_Walk] = None) -> Iterator[Path]:
    types = {str(t).lower() for t in limits["text_types"]}
    shut = [Path(e).resolve() for e in exclude]
    walk = walk if walk is not None else _Walk(limits)
    walk.examined += 1  # the source itself
    if walk.examined > walk.max_dirs:
        return
    stack = [str(root)]
    while stack:
        here = stack.pop()
        key = _identity(Path(here))
        if key is None or key in walk.seen:
            continue  # each real folder once: a loop, or a source already walked, ends here
        walk.seen.add(key)
        try:
            with os.scandir(here) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        dirs, names = [], []
        for e in entries:
            try:
                (dirs if e.is_dir() else names).append(e.name)
            except OSError:
                names.append(e.name)
        for name in names:
            walk.looked += 1
            if walk.looked > walk.max_files:
                return
            p = Path(here) / name
            if (p.is_symlink() or p.suffix.lstrip(".").lower() not in types
                    or excluded_name(name, limits)):
                continue
            try:
                st = p.stat()
                if (not stat.S_ISREG(st.st_mode) or not _inside(p.resolve(), root)
                        or st.st_size > int(limits["max_file_bytes"])):
                    continue  # a pipe or device is never opened: reading one could block
            except OSError:
                continue
            yield p
        accepted = []
        for d in dirs:
            walk.examined += 1  # counted before any work on it, so a wide folder is bounded too
            if walk.examined > walk.max_dirs:
                break
            if _enter(here, d, root, limits, shut):
                accepted.append(os.path.join(here, d))
        stack.extend(reversed(accepted))


def retrieve(query: str, roots: Sequence[Path], k: Optional[int] = None,
             exclude: Sequence[Path] = ()) -> List[dict]:
    """The best ``k`` snippets for ``query`` across ``roots`` (each from ``source_root``),
    never reading inside a folder in ``exclude``."""
    limits = section("retrieval")
    k = int(k or limits["k"])
    width = int(limits["snippet_chars"])
    words = set(_WORD.findall((query or "").lower()))
    if not words:
        return []
    hits = []
    walk = _Walk(limits)  # shared: overlapping sources are read once, and the limits are per request
    for root in roots:
        if any(_same_or_inside(root, Path(e).resolve()) for e in exclude):
            continue
        for p in _files(root, limits, exclude, walk):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lower = text.lower()
            score = sum(lower.count(w) for w in words)
            if not score:
                continue
            first = min((lower.find(w) for w in words if w in lower), default=0)
            start = max(0, first - width // 4)
            hits.append({"root": str(root), "file": p.relative_to(root).as_posix(),
                         "score": score, "snippet": text[start:start + width]})
    hits.sort(key=lambda h: (-h["score"], h["root"], h["file"]))
    return hits[:k]

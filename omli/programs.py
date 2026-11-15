"""Find and start an external program the way OMLi allows.

``shutil.which`` is not used. On Windows it searches the current folder first (always before
Python 3.12, and by default after), and on every platform a PATH entry that is ``.``, empty or
relative resolves against the current folder. Any of those would let a folder someone else
prepared supply a program that OMLi then runs.

The rule, for every program OMLi starts:

* A PATH entry is used only when it is fully absolute for the platform: on Windows a drive
  letter followed by a separator, or a UNC share (``\\\\server\\share``), optionally behind a
  ``\\\\?\\`` or ``\\\\.\\`` prefix; never an entry with no drive, such as ``\\tools``. On
  macOS and Linux, a path that starts with ``/``.
* An entry that is the current folder under any spelling (long-path or device prefixes, an
  admin share, a junction or symlink, a trailing dot) is skipped. "Is the current folder" is
  decided by file identity (device and file number), never by comparing strings.
* The folder that really holds the program chosen (after following links) is checked the
  same way.
* The program runs with its own folder as the working folder, never the folder OMLi was
  started in, and with a PATH made only of the accepted entries, so a helper it looks up by
  name is never taken from the start folder either.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Dict, List, Optional, Sequence

_DRIVE = re.compile(r"[A-Za-z]:\\")
_SHARE = re.compile(r"\\\\(?![?.]\\)[^\\]+\\[^\\]+")
_ENV_LOCK = threading.Lock()


def _extensions() -> List[str]:
    if os.name != "nt":
        return [""]
    exts = [e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
    return exts or [".com", ".exe", ".bat", ".cmd"]


def fully_absolute(entry: str) -> bool:
    """True when ``entry`` names a folder without reference to the current folder or drive."""
    if os.name != "nt":
        return entry.startswith("/")
    p = entry.replace("/", "\\")
    if p[:4] in ("\\\\?\\", "\\\\.\\"):
        p = p[4:]
        if p[:4].upper() == "UNC\\":
            p = "\\\\" + p[4:]
    return bool(_DRIVE.match(p) or _SHARE.match(p))


def _here() -> Optional[os.stat_result]:
    try:
        return os.stat(os.curdir)
    except OSError:
        return None


def is_current_folder(folder: str, here: Optional[os.stat_result] = None) -> bool:
    """True when ``folder`` is the current folder, by file identity. A folder that cannot be
    read counts as the current folder, so it is never used."""
    here = here if here is not None else _here()
    try:
        st = os.stat(folder)
    except (OSError, ValueError):
        return True
    return here is not None and os.path.samestat(st, here)


def accepted_entries() -> List[str]:
    """The PATH entries OMLi may take a program from, in order."""
    here = _here()
    out: List[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not fully_absolute(entry) or not os.path.isdir(entry):
            continue
        if is_current_folder(entry, here):
            continue  # the current folder, under some spelling
        out.append(entry)
    return out


def find_program(name: str) -> Optional[str]:
    """The path of ``name`` in the first accepted PATH folder that holds it, or None.
    ``name`` must be a bare program name, with no folder part."""
    if not name or os.sep in name or (os.altsep and os.altsep in name):
        return None
    here = _here()
    exts = _extensions()
    has_ext = os.name == "nt" and os.path.splitext(name)[1].lower() in exts
    for entry in accepted_entries():
        for ext in ([""] if has_ext else exts):
            candidate = Path(entry) / (name + ext)
            if not (candidate.is_file() and os.access(candidate, os.X_OK)):
                continue
            if is_current_folder(os.path.dirname(os.path.realpath(candidate)), here):
                continue  # a link whose target lives in the current folder
            return str(candidate)
    return None


def program_env() -> Dict[str, str]:
    """This process's environment with PATH reduced to the accepted entries."""
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(accepted_entries())
    return env


def _work_folder(program: str) -> str:
    folder = os.path.dirname(os.path.realpath(program))
    if is_current_folder(folder):
        raise OSError(f"refusing to start a program from the current folder: {program}")
    return folder


def run_program(args: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run`` for a program from ``find_program``: started in its own folder,
    with the filtered PATH."""
    return subprocess.run(list(args), cwd=_work_folder(args[0]), env=program_env(), **kwargs)


def start_program(args: Sequence[str], **kwargs) -> subprocess.Popen:
    """``subprocess.Popen`` with the same rules as ``run_program``."""
    return subprocess.Popen(list(args), cwd=_work_folder(args[0]), env=program_env(), **kwargs)


def system_folder() -> Optional[str]:
    """Windows' own system folder, read from the system rather than from the environment."""
    if os.name != "nt":
        return None
    import ctypes

    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetSystemDirectoryW(buf, len(buf))  # type: ignore[attr-defined]
    return buf.value if 0 < n < len(buf) else None


def start_file(target: str) -> None:
    """Windows only: open ``target`` with its file association (``os.startfile``), with the
    system folder as the working folder and the filtered PATH. ``os.startfile`` takes no
    environment, so PATH is narrowed in this process for the moment of the call."""
    folder = system_folder()
    if not folder or is_current_folder(folder):
        raise OSError("no working folder other than the current folder")
    with _ENV_LOCK:
        saved = os.environ.get("PATH")
        os.environ["PATH"] = os.pathsep.join(accepted_entries())
        try:
            os.startfile(target, cwd=folder)  # type: ignore[attr-defined,call-arg]
        finally:
            if saved is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = saved


__all__ = ["accepted_entries", "find_program", "fully_absolute", "is_current_folder",
           "program_env", "run_program", "start_program", "start_file", "system_folder"]

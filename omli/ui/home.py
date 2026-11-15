"""Where the local UI keeps its data: OMLI_HOME when it is set, else one fixed per-user folder.

The core library writes to OMLI_HOME, or to the current folder when it is unset
(``omli.home.omli_home``); that rule is unchanged. The UI never uses the current folder: a
folder someone else prepared could hold a key registry, a project list, a model registry or
boards, and the server would trust them. With OMLI_HOME unset, the UI uses
``<home folder>/.omli/data`` instead, for every command and the server alike.
"""

from __future__ import annotations

from pathlib import Path

from omli.home import configured_home

DATA_DIR = (".omli", "data")


def ui_home(home: str | Path | None = None) -> Path:
    """The UI's data folder: an explicit ``home``, else OMLI_HOME, else the per-user folder."""
    if home is not None:
        return Path(home).expanduser().resolve()
    return configured_home() or Path.home().joinpath(*DATA_DIR).resolve()


__all__ = ["DATA_DIR", "ui_home"]

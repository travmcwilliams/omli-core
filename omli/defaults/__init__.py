"""Example packs shipped inside the package, so a normal install has working defaults.

Read through importlib.resources. These are neutral examples, not deployment config: a deployment's
own packs come from explicit paths or from OMLI_HOME. Nothing is ever written here.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path


def default_pack_path(name: str) -> Path:
    """The file for a shipped example pack. Fails loud when it is absent or not on a filesystem."""
    resource = resources.files(__name__).joinpath(name)
    if not isinstance(resource, Path):
        raise RuntimeError(f"default pack {name!r} is not on a filesystem; install OMLi unpacked")
    if not resource.is_file():
        raise FileNotFoundError(f"no default pack named {name!r} in {__name__}")
    return resource

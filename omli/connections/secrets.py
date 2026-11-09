"""Resolve a ``secret://`` credential reference to its value.

The Connection Registry NAMES a credential as a ``secret://path`` reference and never stores the
value (schema.py enforces the prefix, refuses inline tokens). This is the piece that turns such a
reference into a real value at use time, so an adapter can authenticate without the secret ever
touching the registry, a pack, an artifact, or a log.

Resolution order (fail-closed — a missing secret returns None / raises, never a blank string):
  1. Environment variable ``OMLI_SECRET_<PATH>``: the path upper-cased with each ``/`` written
     as ``__`` — e.g. ``secret://connections/example_api/api_key`` ->
     ``OMLI_SECRET_CONNECTIONS__EXAMPLE_API__API_KEY``.
  2. A file under $OMLI_SECRETS_DIR, or under ``var/secrets`` in $OMLI_HOME, whose stripped contents
     are the value — e.g. ``var/secrets/connections/example_api/api_key``. With neither variable set
     there is no file store and nothing is read: the current directory is never searched, so a file
     planted where a process happens to run cannot become a credential.

**Distinct references never share a variable.** The rule chosen is to refuse, not to escape: each
path segment must be lower-case letters and digits, optionally joined by single underscores
(``[a-z0-9]+(_[a-z0-9]+)*``). Upper case, ``-``, ``.``, other punctuation, and leading, trailing or
doubled underscores are refused. A segment then never contains ``__``, so the variable name splits
back into exactly one path, and upper-casing loses nothing because no segment has a capital.

The value is returned to the caller and NOTHING here prints, logs, or repr()s it. ``secret_status``
exists so callers/tests can confirm a secret is wired without being handed it (presence + source
only). For a file it reads the contents to tell an empty file from a real value, then drops them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from omli.home import omli_home

_PREFIX = "secret://"
# None means the default store: $OMLI_SECRETS_DIR, else var/secrets under $OMLI_HOME, else no store.
# Resolved at call time. Set it to point at another store.
_SECRETS_DIR: Optional[Path] = None
_SEGMENT = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")


class SecretNotFound(RuntimeError):
    """A required secret reference resolved to nothing (fail closed)."""


def _path_of(ref: str) -> str:
    """The validated relative path of a reference. Raises ValueError on any unsafe shape."""
    # No message in this module repeats `ref` or anything derived from it: a rejected value may
    # be a credential pasted in by mistake.
    if not ref.startswith(_PREFIX):
        raise ValueError(f"not a secret reference: it must start with {_PREFIX!r} (value not shown)")
    path = ref[len(_PREFIX):]
    if "\\" in path:
        raise ValueError("a secret reference may not contain a backslash (value not shown)")
    for segment in path.split("/"):
        if not segment or segment != segment.strip():
            raise ValueError("a secret reference has an empty or padded path segment (value not shown)")
        if segment in (".", "..") or ":" in segment:
            raise ValueError("a secret reference must be a relative path inside the store (value not shown)")
        if _SEGMENT.fullmatch(segment) is None:
            raise ValueError("a secret reference path segment must be lower-case letters and digits, "
                             "optionally joined by single underscores (value not shown)")
    return path


def validate_ref(ref: str) -> None:
    """Refuse an unsafe reference without reading anything. The registry parser calls this."""
    _path_of(ref)


def _secrets_dir() -> Optional[Path]:
    """The file store, or None when none is configured."""
    if _SECRETS_DIR is not None:
        return Path(_SECRETS_DIR)
    env = os.environ.get("OMLI_SECRETS_DIR")
    if env:
        return Path(env)
    return omli_home() / "var" / "secrets" if os.environ.get("OMLI_HOME") else None


def env_var_for(ref: str) -> str:
    """The environment-variable name a secret reference maps to (no value involved): the path
    upper-cased with each ``/`` written as ``__``. Distinct valid references give distinct names
    (see the module docstring)."""
    return "OMLI_SECRET_" + _path_of(ref).replace("/", "__").upper()


def _file_for(ref: str) -> Optional[Path]:
    """The file for a reference, or None when no store is configured. The RESOLVED path must stay
    inside the store, so a symlink cannot carry a reference out of it either."""
    segments = _path_of(ref).split("/")
    store = _secrets_dir()
    if store is None:
        return None
    root = store.resolve()
    path = root.joinpath(*segments).resolve()
    if not path.is_relative_to(root):
        raise ValueError("a secret reference resolves outside the secrets directory (value not shown)")
    return path


def resolve_secret(ref: str, *, required: bool = False) -> Optional[str]:
    """Return the secret value for a ``secret://`` reference, or None if unset.

    Never logs or prints the value. Raises SecretNotFound when ``required`` and nothing is wired.
    """
    env = os.environ.get(env_var_for(ref))
    if env is not None and env.strip():
        return env.strip()
    path = _file_for(ref)
    if path is not None and path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    if required:
        # Neither the reference nor anything derived from it (the variable name, the file path)
        # goes in the message: a value pasted after `secret://` would ride along in all three.
        # `secret_status(ref)` tells an operator where the value is read from.
        raise SecretNotFound(
            "no value wired for a required secret reference (reference not shown): set its "
            "OMLI_SECRET_ variable, or write its file under OMLI_SECRETS_DIR or OMLI_HOME/var/secrets; "
            "secret_status() on the reference names both"
        )
    return None


@dataclass(frozen=True)
class SecretStatus:
    """Safe, value-free view of whether a secret is wired and from where."""

    ref: str
    present: bool
    source: str  # "env" | "file" | "missing"
    env_var: str


def secret_status(ref: str) -> SecretStatus:
    """Report whether a secret is wired without returning its value (for verification / dry-run)."""
    env_name = env_var_for(ref)
    if (os.environ.get(env_name) or "").strip():
        return SecretStatus(ref=ref, present=True, source="env", env_var=env_name)
    path = _file_for(ref)
    if path is not None and path.is_file() and path.read_text(encoding="utf-8").strip():
        return SecretStatus(ref=ref, present=True, source="file", env_var=env_name)
    return SecretStatus(ref=ref, present=False, source="missing", env_var=env_name)


def credential_for(connection, *, required: bool = False) -> Optional[str]:
    """Resolve a ConnectionEntry's credential_ref (None for an anonymous connection)."""
    ref = getattr(connection.target, "credential_ref", None)
    if not ref:
        return None
    return resolve_secret(ref, required=required)

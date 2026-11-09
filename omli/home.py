"""Where OMLi writes by default: $OMLI_HOME if set, else the current working directory.

Never the package location. Resolved at call time, so a process that changes directory or sets
the variable gets the answer that applies then. An explicit path argument always wins.
"""

from __future__ import annotations

import os
from pathlib import Path


def omli_home() -> Path:
    """The root for default writes."""
    env = os.environ.get("OMLI_HOME")
    return Path(env).expanduser().resolve() if env else Path.cwd().resolve()

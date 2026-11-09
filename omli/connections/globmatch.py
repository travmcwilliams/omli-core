"""Tiny pure-Python glob matcher (no dependency).

Stdlib `fnmatch` does not distinguish `*` from `**`, and `PurePath.full_match` is 3.13+ (above
this repo's 3.10 floor). This matcher translates a glob to an anchored regex, with `**/` handled
so a leading `**/` may match zero leading segments (important for
`forbidden_paths` like `**/secrets/**`, which must also catch a top-level `secrets/`).

Segment semantics:
  *   -> one path segment            ([^/]*)
  **  -> any number of segments      (.*)
  **/ -> zero or more leading dirs   ((?:.*/)?)
"""

from __future__ import annotations

import re

# Escape every regex metacharacter EXCEPT '*' (which we translate ourselves).
_SPECIAL = re.compile(r"([.+^${}()|\[\]\\?])")

# Private sentinels so the ordered replacements don't collide.
_DBL_SLASH = "\x00"  # from "**/"
_DBL = "\x01"        # from "**"


def glob_match(pattern: str, value: str, *, ignore_case: bool = False) -> bool:
    """Return True if `value` matches the glob `pattern` (anchored, full match).

    ``ignore_case`` folds both sides first. The scope gate uses it for every forbidden pattern
    (a deny must not be dodged by changing case) and for mail address rows.
    """
    if ignore_case:
        pattern, value = pattern.casefold(), value.casefold()
    escaped = _SPECIAL.sub(r"\\\1", pattern)
    escaped = escaped.replace("**/", _DBL_SLASH).replace("**", _DBL).replace("*", "[^/]*")
    regex = escaped.replace(_DBL_SLASH, "(?:.*/)?").replace(_DBL, ".*")
    return re.fullmatch(regex, value) is not None

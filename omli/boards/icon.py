"""A node's icon: a named icon when a pack rule's keyword is in its text, else a stable seed.

Pure and deterministic: no model, no network, no files beyond the shipped vocabulary. The
keyword rules are pack data (``icons.rules`` in ``mindmap.yaml``); this module names none.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Optional

from omli.boards.vocab import section

_TOKEN = re.compile(r"[a-z0-9]+")


def keyword_icon(text: str) -> Optional[str]:
    words = set(_TOKEN.findall((text or "").lower()))
    for rule in section("icons").get("rules", ()):
        if words.intersection(str(k).lower() for k in rule.get("keywords", ())):
            return str(rule["icon"])
    return None


def procedural_seed(text: str, glyph: str = "") -> Dict[str, Any]:
    forms = section("icons").get("forms", ()) or ("orbit",)
    digest = hashlib.sha256(f"{text}\x00{glyph}".encode("utf-8")).digest()
    return {"kind": "seed", "seed": digest[:8].hex(), "hue": int.from_bytes(digest[8:10], "big") % 360,
            "form": forms[digest[10] % len(forms)], "arms": 3 + digest[11] % 6,
            "glyph": glyph or None}


def icon_for(text: str, glyph: str = "") -> Dict[str, Any]:
    """``{"kind": "named", "name"}`` when a rule matches, else a procedural seed."""
    name = keyword_icon(text)
    if name:
        return {"kind": "named", "name": name}
    return procedural_seed(text, glyph)

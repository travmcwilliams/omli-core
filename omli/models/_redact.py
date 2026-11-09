"""Remove the hosted adapter's API key from text and decoded data (private to `provider.py`).

A key can come back through things we do not control: a server's error body or a JSON
response. One pattern per key matches it character by character, where each character may
appear raw or in any of these encodings, mixed freely within one occurrence:

  * a JSON or JavaScript escape, ``\\uXXXX`` (either hex case, any number of leading
    backslashes, so an escape inside a nested JSON string is matched too), a surrogate pair
    for a character outside the BMP, and the short escapes (``\\/``, ``\\"``, ``\\\\``, ...);
  * a percent escape of each UTF-8 byte (either hex case, and ``%25`` repeated for double
    encoding), and ``+`` for a space;
  * an HTML numeric character reference (``&#x2B;`` or ``&#43;``, with any leading zeros).

A ``\\u`` escape is matched only with its four hex digits and a percent escape only with its
two, so text that merely resembles an escape (``\\u41``, say) is left alone.

`scrub_text` replaces every match with `MASK`; `scrub_value` walks decoded data. The adapter
scrubs the raw bytes, then the decoded JSON, then every value it decodes further (tool-call
arguments), so no decoding step can turn an escaped key back into plain text.

This is a backstop, not the control: the control is never putting the key into a message.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Iterable, Optional

MASK = "[redacted]"

_SHORT_ESCAPES = {'"': '"', "\\": "\\\\", "/": "/", "\b": "b", "\f": "f", "\n": "n",
                  "\r": "r", "\t": "t"}


def _hex(n: int, width: int) -> str:
    """A case-insensitive pattern for ``n`` in hex, written with exactly ``width`` digits
    (zero-padded). ``n`` is always below ``16 ** width`` here."""
    return "".join(f"[{d.lower()}{d.upper()}]" if d.isalpha() else d for d in f"{n:0{width}x}")


def _char_pattern(c: str) -> str:
    code = ord(c)
    alts = [re.escape(c)]
    if code <= 0xFFFF:
        alts.append(r"\\+[uU]" + _hex(code, 4))
    else:
        hi = 0xD800 + ((code - 0x10000) >> 10)
        lo = 0xDC00 + ((code - 0x10000) & 0x3FF)
        alts.append(r"\\+[uU]" + _hex(hi, 4) + r"\\+[uU]" + _hex(lo, 4))
    if c in _SHORT_ESCAPES:
        alts.append(r"\\+" + re.escape(_SHORT_ESCAPES[c]))
    alts.append("".join(r"%(?:25)*" + _hex(b, 2) for b in c.encode("utf-8")))
    if c == " ":
        alts.append(r"\+")
    alts.append(r"&#[xX]0*" + _hex(code, len(f"{code:x}")) + ";")
    alts.append(r"&#0*" + str(code) + ";")
    return "(?:" + "|".join(alts) + ")"


@lru_cache(maxsize=32)
def _pattern(secret: str) -> "re.Pattern[str]":
    return re.compile("".join(_char_pattern(c) for c in secret))


def scrub_text(text: str, secrets: Iterable[Optional[str]]) -> str:
    """``text`` with every occurrence of every secret, in any encoding above, replaced by `MASK`."""
    out = text
    for secret in secrets:
        if secret:
            out = _pattern(secret).sub(MASK, out)
    return out


def scrub_bytes(raw: bytes, secrets: Iterable[Optional[str]]) -> bytes:
    """``raw`` scrubbed the same way. Bytes that are not UTF-8 are kept as they were."""
    text = raw.decode("utf-8", "surrogateescape")
    return scrub_text(text, secrets).encode("utf-8", "surrogateescape")


def scrub_value(value: Any, secrets: Iterable[Optional[str]]) -> Any:
    """A copy of decoded JSON data with every string, key or value, scrubbed. Dicts, lists and
    tuples are walked; numbers, booleans and None pass through; anything else is replaced by
    its type name, because its text form is not ours to vouch for."""
    secrets = tuple(s for s in secrets if s)
    if isinstance(value, str):
        return scrub_text(value, secrets)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {(scrub_value(k, secrets) if isinstance(k, str) else f"<{type(k).__name__}>"):
                scrub_value(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_value(v, secrets) for v in value]
    return f"<{type(value).__name__}>"

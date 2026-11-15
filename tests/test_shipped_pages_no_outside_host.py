"""No shipped page, script or style names an address on another host.

Runs everywhere (no web package needed), including the core job with no extras. It reads
every page file in the package (``omli/ui/static/``) and the pages built as strings in
``omli/ui/pages.py``. An address is ``scheme://host`` or a protocol-relative ``//host``
in a URL position. The only lines allowed to hold one are on the allow-list below, each
with its reason; a licence file is allowed as a whole.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "omli" / "ui"
PAGE_SUFFIXES = (".html", ".js", ".css", ".svg")
LICENCE_NAMES = ("LICENSE", ".LICENSE")

# scheme://host, or //host where a URL is expected (after a quote, (, = or url( ).
ADDRESS = re.compile(
    r"""(?:\b[a-z][a-z0-9+.-]*://[^\s'"`)<>]+)|(?:(?<=['"(=`])//[a-z0-9.-]+\.[a-z]{2,})""",
    re.IGNORECASE)

# (file suffix, exact address): an XML namespace is a name, never fetched by a browser.
ALLOWED_ADDRESSES = {
    "http://www.w3.org/2000/svg",
}


def _page_files():
    for path in sorted((ROOT / "static").rglob("*")):
        if path.is_file() and (path.suffix in PAGE_SUFFIXES or path.name.endswith(LICENCE_NAMES)):
            yield path


def _hits(text):
    return [m.group(0).rstrip(".,;") for m in ADDRESS.finditer(text)]


def _scan():
    found = []
    for path in _page_files():
        if path.name.endswith(LICENCE_NAMES):
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for addr in _hits(line):
                if addr not in ALLOWED_ADDRESSES:
                    found.append((str(path.relative_to(ROOT)), n, addr))
    pages = (ROOT / "pages.py").read_text(encoding="utf-8")
    for n, line in enumerate(pages.splitlines(), 1):
        for addr in _hits(line):
            if addr not in ALLOWED_ADDRESSES:
                found.append(("pages.py", n, addr))
    return found


def test_there_are_page_files_to_check():
    names = {p.name for p in _page_files()}
    assert {"index.html", "mindmap.js", "mindmap.css"} <= names


def test_no_shipped_page_script_or_style_names_another_host():
    assert _scan() == []


def test_the_check_finds_an_outside_address(tmp_path, monkeypatch):
    for sample in ['<img src="https://cdn.example.org/x.png">',
                   "import x from 'http://evil.example/m.js'",
                   "url(//fonts.example.net/a.woff)",
                   "fetch(`wss://socket.example.com/ws`)"]:
        assert _hits(sample), sample
    for sample in ["// a comment", "a//b", "x = 1 // 2", 'href="/mindmap/x.js"']:
        assert not _hits(sample), sample


def test_the_allow_list_is_only_the_svg_namespace():
    assert ALLOWED_ADDRESSES == {"http://www.w3.org/2000/svg"}

"""The shipped Mind-map page describes only itself.

* Every ``#id`` the stylesheet styles is on the page or made by its scripts.
* Every class the stylesheet styles is named by the page or its scripts, or built from
  a prefix the scripts add at run time.
* Every CSS variable the stylesheet uses is defined.
* Every ``omli.*`` module a page comment names exists.
* No comment refers to the private process that built the page or to another product's screens.
* No comment and no visible text, on the Mind-map page or the sign-in pages, names another
  company's or project's product.

Runs with no extras.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "omli" / "ui" / "static" / "mindmap"
SCRIPTS = [PAGE / "mindmap.js"] + sorted((PAGE / "js").glob("*.js"))


def _text(path):
    return path.read_text(encoding="utf-8")


def _ids_made():
    ids = set(re.findall(r'\bid="([A-Za-z][\w-]*)"', _text(PAGE / "index.html")))
    for js in SCRIPTS:
        src = _text(js)
        ids |= set(re.findall(r"""\bid=\\?["']([A-Za-z][\w-]*)\\?["']""", src))
        ids |= set(re.findall(r"""\.id\s*=\s*["']([A-Za-z][\w-]*)["']""", src))
        ids |= set(re.findall(r"""setAttribute\(\s*['"]id['"]\s*,\s*['"]([A-Za-z][\w-]*)['"]""", src))
    return ids


def test_every_styled_id_is_on_the_page_or_made_by_a_script():
    css = re.sub(r"/\*.*?\*/", "", _text(PAGE / "mindmap.css"), flags=re.S)
    selectors = "".join(re.findall(r"([^{}]*)\{", css))
    styled = set(re.findall(r"#([A-Za-z][\w-]*)", selectors))
    styled -= {h for h in styled if re.fullmatch(r"[0-9a-fA-F]{3,8}", h)}
    missing = sorted(styled - _ids_made())
    assert not missing, f"styles for elements that do not exist: {missing}"


def test_every_css_variable_used_is_defined():
    css = _text(PAGE / "mindmap.css")
    defined = set(re.findall(r"(--[\w-]+)\s*:", css))
    for js in SCRIPTS:
        defined |= set(re.findall(r"""setProperty\(\s*['"](--[\w-]+)""", _text(js)))
    used = set(re.findall(r"var\((--[\w-]+)", css))
    assert not sorted(used - defined), sorted(used - defined)


def _comments(text):
    return re.findall(r"/\*.*?\*/|//[^\n]*|<!--.*?-->", text, flags=re.S)


def _exists(module):
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False


def test_every_module_a_page_comment_names_exists():
    named = set()
    for path in SCRIPTS + [PAGE / "mindmap.css", PAGE / "index.html"]:
        if "vendor" in path.parts:
            continue
        for comment in _comments(_text(path)):
            # a module name, not a storage-key pattern such as omli.mm.<id>.pos
            named |= set(re.findall(r"\b(omli(?:\.[a-z_][a-z0-9_]*)+)(?![\w.<])", comment))
    missing = sorted(m for m in named if not _exists(m))
    assert not missing, missing


def test_no_comment_refers_to_the_build_process_or_another_product():
    phrases = ("the user asked for", "priority card", "filter card", "brain pill",
               "live presence", "spec pattern", "ask a project", "snapshot -> spec",
               "payload.background", "assets/js/", "the pill toggles", "first ship",
               "remote-edit stream", "refreshes as the board changes", "three rag choices",
               "unsaved-draft recovery", "same prng", "used to be", "no longer",
               "back-compat", "legacy", "rag rung palette flyout */\n/* checkpoint")
    hits = [f"{p.name}: {ph}" for p in SCRIPTS + [PAGE / "mindmap.css", PAGE / "index.html"]
            if "vendor" not in p.parts for ph in phrases if ph in _text(p).lower()]
    assert not hits, hits


def test_every_styled_class_is_named_by_the_page_or_built_by_a_script():
    css = re.sub(r"/\*.*?\*/", "", _text(PAGE / "mindmap.css"), flags=re.S)
    selectors = "".join(re.findall(r"([^{}]*)\{", css))
    styled = set(re.findall(r"\.([A-Za-z][\w-]*)", selectors))
    source = _text(PAGE / "index.html") + "".join(_text(js) for js in SCRIPTS)
    # prefixes the scripts join to a value at run time, such as ' r-'+status or s-${tag}
    built = set(re.findall(r"""['" ]([a-z]+-)['"]\s*\+""", source))
    built |= set(re.findall(r"\b([a-z]+-)\$\{", source))
    missing = sorted(c for c in styled
                     if c not in source and not any(c.startswith(b) for b in built))
    assert not missing, f"styles for classes nothing uses: {missing}"


_PRODUCTS = re.compile(r"\b(miro|excalidraw|figma|notion|tldraw|lucidchart|whimsical|tabler"
                       r"|rough\.?js|safari|firefox|google chrome|microsoft edge)\b", re.I)


def _visible(html):
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"<!--.*?-->|<[^>]+>", " ", html, flags=re.S)


def test_no_comment_or_visible_text_names_another_product():
    hits = []
    # The whole of each page file, code as well as comments: an identifier or a value the
    # page shows can name a product too.
    for path in SCRIPTS + [PAGE / "mindmap.css", PAGE / "index.html"]:
        if "vendor" in path.parts:
            continue
        hits += [f"{path.name}: {m}" for m in _PRODUCTS.findall(_text(path))]
    # The sign-in pages and the sign-in hand-off page are written in these modules.
    ui = PAGE.parent.parent
    for module in ("pages.py", "client.py"):
        hits += [f"{module}: {m}" for m in _PRODUCTS.findall(_text(ui / module))]
    assert not hits, hits


def test_the_frame_popover_offers_no_layer_controls():
    # Covers are not saved in this release (README: "Freehand ink and scratch covers are not
    # in this release"), so the frame popover must not offer to add or remove one.
    src = _text(PAGE / "mindmap.js")
    start = src.index("function openFramePop(")
    body = src[start:src.index("\nfunction ", start + 1)]
    assert "Layer" not in body and "fpCover" not in body and "fpPopLayer" not in body

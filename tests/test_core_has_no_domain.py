"""Primitive 1: a keyword guard on the core/pack seam.

Domain logic must live in packs/. This walks omli/*.py and fails if any term in
FORBIDDEN_TERMS appears in core. It checks only the listed terms, so it is a guard, not a
proof that core holds no domain strings. Add a vertical's terms here when one appears.
"""

import re
from pathlib import Path

# Business terms from common verticals, chosen as examples of what a pack may name and the core may
# not. None appears anywhere in the core package. This is a keyword guard on the seam, not a proof:
# a deployment that adds a vertical should add that vertical's terms here.
FORBIDDEN_TERMS = (
    "mortgage",
    "payroll",
    "invoice",
    "tuition",
    "patient record",
    "insurance claim",
    "real estate",
    "sports betting",
    "loan officer",
    "purchase order",
)

CORE_DIR = Path(__file__).resolve().parent.parent / "omli"


def _core_py_files():
    return sorted(CORE_DIR.rglob("*.py"))


def test_core_dir_exists_and_has_files():
    files = _core_py_files()
    assert files, "expected core package files under omli/"


def _terms_in(text):
    # Whole-word match only: a forbidden term must appear as its own word, not as an
    # accidental substring (e.g. "loan" inside "loaned").
    lowered = text.lower()
    return [t for t in FORBIDDEN_TERMS if re.search(rf"\b{re.escape(t)}\b", lowered)]


def test_the_guard_fires_on_every_term():
    for term in FORBIDDEN_TERMS:
        assert _terms_in(f"# handles {term.upper()} data") == [term]
    assert _terms_in("mortgaged invoices") == []


def test_no_domain_terms_in_core():
    offenders = []
    for path in _core_py_files():
        for term in _terms_in(path.read_text(encoding="utf-8")):
            offenders.append((path.relative_to(CORE_DIR.parent), term))
    assert not offenders, (
        "domain terms leaked into core (move them to a pack): "
        + ", ".join(f"{p} contains {t!r}" for p, t in offenders)
    )

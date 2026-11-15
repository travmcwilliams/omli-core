"""The shipped package, its tests and its public documents carry no references to the private
process that built them: no round numbers, no step or section labels from a work plan.

Runs with no extras, so the core CI job checks it too.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()
PATTERNS = {
    "round number": re.compile(r"\bRound \d+\b"),
    "work-plan section label": re.compile(r"#\s*-+\s*(?:KEEP|REWRITE|STUB|R\d+)\s*:"),
    "work-plan reference": re.compile(r"\bthe plan (?:leaves|says)\b|\bthe KEEP and STUB\b"),
}


def _files():
    for base in ("omli", "tests"):
        for path in sorted((ROOT / base).rglob("*")):
            if path.is_file() and path.suffix in {".py", ".js", ".html", ".css", ".yaml", ".md"} \
                    and "vendor" not in path.parts and path.resolve() != SELF:
                yield path
    for name in ("README.md", "SECURITY.md", "CONTRIBUTING.md", "NOTICE"):
        if (ROOT / name).is_file():
            yield ROOT / name


def test_no_process_references_in_shipped_or_public_files():
    hits = []
    for path in _files():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for kind, rx in PATTERNS.items():
                if rx.search(line):
                    hits.append(f"{path.relative_to(ROOT).as_posix()}:{n} ({kind})")
    assert not hits, hits


def test_the_readme_does_not_deny_the_ui_it_documents():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## The local UI and the Mind-map" in text
    assert "There is no web UI" not in text


def test_security_names_the_program_a_model_call_can_start():
    """The core's GPU sampler runs nvidia-smi during model calls, so SECURITY.md must say so
    and say how to turn it off; it must not claim the UI starts no program."""
    if "nvidia-smi" not in (ROOT / "omli" / "hardware.py").read_text(encoding="utf-8"):
        return
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "nvidia-smi" in text and "OMLI_GPU_TELEMETRY=0" in text
    assert "Nothing in the UI starts a program" not in text

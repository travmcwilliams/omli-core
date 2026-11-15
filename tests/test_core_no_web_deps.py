"""A plain install depends on PyYAML only, and importing omli pulls in no web package.

The UI's web stack (fastapi, uvicorn, websockets) is the optional "ui" install. This runs in
the CI job that installs no extras, so it must not need any of them.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ("fastapi", "starlette", "uvicorn", "websockets", "pydantic", "anyio", "h11", "httpx", "httpx2",
       "cryptography")


def _deps_block(text, header):
    m = re.search(rf"^{re.escape(header)}\s*=\s*\[(.*?)\]", text, re.M | re.S)
    assert m, f"{header} not found"
    return re.findall(r'"([^"]+)"', m.group(1))


def test_a_plain_install_depends_on_pyyaml_only():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert _deps_block(text, "dependencies") == ["pyyaml>=6.0"]


def test_the_ui_install_carries_the_web_stack():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    names = {re.split(r"[<>=\[ ]", d, 1)[0].lower() for d in _deps_block(text, "ui")}
    assert {"fastapi", "uvicorn", "websockets"} <= names


def test_importing_omli_pulls_in_no_web_package():
    code = (
        "import importlib, pkgutil, sys\n"
        "import omli\n"
        "skip = ('omli.ui.server', 'omli.ui.__main__')\n"
        "for m in pkgutil.walk_packages(omli.__path__, 'omli.'):\n"
        "    if m.name.endswith('__main__') or m.name in skip:\n"
        "        continue\n"
        "    importlib.import_module(m.name)\n"
        "import omli.ui, omli.ui.auth, omli.ui.projects\n"
        f"web = {WEB!r}\n"
        "print(sorted({n.split('.')[0] for n in sys.modules} & set(web)))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "[]", f"web packages loaded by import: {out.stdout.strip()}"
